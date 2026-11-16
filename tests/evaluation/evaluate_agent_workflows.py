"""Small real-model workflow evaluation with synthetic email tools.

Run with --live to make billable OpenRouter calls. Without it, validate fixtures.
The supervisor copies application code to a temporary checkout before importing
the runtimes; even import-time stores cannot touch the user's application data.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "tests/fixtures/agent_workflow_cases.json"


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Close the handle before replacement on Windows. Unique sibling files avoid
    # reusing a name still held by an editor, antivirus scanner, or sync client.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    delays = (.05, .1, .2, .4, .8, 1.6)
    for attempt in range(len(delays) + 1):
        try:
            temporary.replace(path)
            return path
        except PermissionError as exc:
            if attempt == len(delays):
                # Keep both the last good report and the complete new snapshot.
                raise PermissionError(
                    f"Could not replace the report after {attempt + 1} attempts: {path}. "
                    f"The complete new report is preserved at {temporary}. "
                    "Close programs holding the report open, or use --output with a fresh path "
                    "outside a synced folder."
                ) from exc
            time.sleep(delays[attempt])


def load_fixture(path=FIXTURE):
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    keys = {agent["key"] for agent in fixture["agents"]}
    ids = [case["id"] for case in fixture["cases"]]
    if len(keys) != len(fixture["agents"]) or len(ids) != len(set(ids)):
        raise ValueError("Fixture keys must be unique")
    for case in fixture["cases"]:
        for turn in case["turns"]:
            if turn["expect"] not in {"draft", "read", "clarify", "tool_failure"}:
                raise ValueError("Unknown expected workflow")
            if turn.get("owner") not in keys | {"new", None}:
                raise ValueError("Unknown expected owner")
    return fixture


def score_turn(expected, *, jobs, actions, reply, agents_before, agents_after, owner_ids,
               runtime_success=True, settled=True, history_owner_ids=()):
    """Judge actions and identities, never a model's assertion that it succeeded."""
    kind = expected["expect"]
    added = set(agents_after) - set(agents_before)
    wanted = expected.get("owner")
    expected_ids = added if wanted == "new" else {owner_ids[wanted]} if wanted else set()
    dispatched = {job["agent_id"] for job in jobs}
    history_answer = kind == "read" and not jobs and bool(expected_ids & set(history_owner_ids))
    checks = {"interaction_succeeded": runtime_success, "queue_settled": settled,
              "no_unapproved_send_attempt": not any(a["kind"] == "blocked_send" for a in actions)}
    if kind == "clarify":
        words = reply.casefold()
        checks.update(
            no_dispatch=not jobs, no_new_agent=not added, no_actions=not actions,
            relevant_question="?" in reply and any(
                all(term.casefold() in words for term in group)
                for group in expected.get("clarification_terms", [])))
    else:
        checks["correct_owner"] = ((bool(jobs) and dispatched == expected_ids) or history_answer) and len(expected_ids) == 1
        checks["correct_creation_count"] = len(added) == (1 if wanted == "new" else 0)
        allowed_statuses = {"succeeded", "failed"} if kind == "tool_failure" else {"succeeded"}
        checks["job_outcomes_valid"] = history_answer or bool(jobs) and all(j["status"] in allowed_statuses for j in jobs)
        if kind == "draft":
            drafts = [a for a in actions if a["kind"] == "draft"]
            checks["exactly_one_persisted_draft"] = len(drafts) == 1
            draft = drafts[0] if len(drafts) == 1 else {}
            checks["correct_draft_owner"] = draft.get("agent_id") in expected_ids
            checks["correct_recipient"] = str(draft.get("recipient_email", "")).casefold() == expected["recipient"].casefold()
            checks["correct_thread"] = not expected.get("thread_id") or draft.get("thread_id") == expected["thread_id"]
            body = str(draft.get("body", "")).casefold()
            checks["nonempty_draft"] = bool(body.strip() and str(draft.get("subject", "")).strip())
            rendered = f"To: {draft.get('recipient_email', '')}\nSubject: {draft.get('subject', '')}\n\n{draft.get('body', '')}"
            checks["draft_presented_verbatim"] = bool(draft) and rendered in reply
            checks["required_body_facts"] = all(t.casefold() in body for t in expected.get("body_terms", []))
            checks["no_conflicting_body_facts"] = not any(t.casefold() in body for t in expected.get("forbidden_body_terms", []))
            checks["waiting_for_approval"] = bool(expected_ids) and all(
                agents_after.get(i, {}).get("approval_pending")
                and agents_after[i]["state"] == "waiting_for_user" for i in expected_ids)
        elif kind == "read":
            checks["no_draft_or_send"] = not any(a["kind"] != "read" for a in actions)
            checks["required_reply_facts"] = all(t.casefold() in reply.casefold() for t in expected.get("reply_terms", []))
            # Saved source history is valid evidence too; tool calls are not mandatory.
        else:
            checks["failure_observed"] = any(a["kind"] == "read_error" for a in actions)
            checks["failure_reported"] = any(t.casefold() in reply.casefold() for t in expected["reply_terms_any"])
            checks["no_invented_due_date"] = not any(t.casefold() in reply.casefold()
                                                      for t in expected["forbidden_reply_terms"])
            checks["no_write_after_failure"] = not any(a["kind"] in {"draft", "blocked_send"} for a in actions)
    return dict(passed=all(checks.values()), checks=checks, expected=kind, expected_owner=wanted,
                dispatched_agent_ids=sorted(dispatched), new_agent_ids=sorted(added),
                answered_from_saved_history=history_answer, wrong_owner=bool(dispatched - expected_ids),
                duplicate_agents=len(added) if wanted != "new" else max(0, len(added) - 1))


def summarize(rows, planned_turns):
    latencies = sorted(r["elapsed_ms"] for r in rows)
    passed = sum(r["passed"] for r in rows)
    owner_rows = [r for r in rows if "correct_owner" in r.get("checks", {})]
    draft_rows = [r for r in rows if r["expected"] == "draft"]
    return dict(planned_turns=planned_turns, evaluated_turns=len(rows), passed=passed,
                pass_rate=passed / len(rows) if rows else None,
                coverage=len(rows) / planned_turns if planned_turns else 0,
                wrong_owner_turns=sum(r["wrong_owner"] for r in rows),
                duplicate_agents=sum(r["duplicate_agents"] for r in rows),
                correct_owner_turns=sum(r["checks"]["correct_owner"] for r in owner_rows),
                owner_selection_cases=len(owner_rows),
                draft_workflow_passes=sum(r["passed"] for r in draft_rows),
                draft_workflow_cases=len(draft_rows),
                clarification_passes=sum(r["passed"] for r in rows if r["expected"] == "clarify"),
                clarification_cases=sum(r["expected"] == "clarify" for r in rows),
                p50_workflow_ms=statistics.median(latencies) if latencies else None,
                p95_workflow_ms=latencies[min(len(latencies) - 1, int(len(latencies) * .95))] if latencies else None,
                complete=len(rows) == planned_turns,
                all_passed=len(rows) == planned_turns and passed == planned_turns)


def seed_registry(registry, fixture, noise_count):
    ids = {}
    for item in fixture["agents"]:
        agent = registry.create_agent(item["name"], item["purpose"])
        ids[item["key"]] = agent["id"]
        message = dict(id="message-" + item["key"], thread_id=item["thread_id"],
                       sender=item["email"], subject=item["name"], clean_text=item["fact"])
        event = registry.append_event("tool_result", dict(tool="task_email_search", success=True,
                                                         arguments={}, result=[message]), agent_id=agent["id"])
        for kind, value in (("email", item["email"]), ("thread_id", item["thread_id"]),
                            ("invoice_id", item.get("invoice_id"))):
            if value:
                registry.add_reference(agent["id"], kind, value, confirmed=True, source_event_id=event)
        if item.get("archived"):
            registry.update_agent(agent["id"], state="completed", archived=True)
    for index in range(noise_count):
        registry.create_agent(f"Equipment inventory {index}", f"Maintain unrelated office equipment inventory {index}")
    return ids


class SyntheticMailbox:
    """Global seeded mailbox; never uses expected labels to choose an answer."""
    def __init__(self, registry, fixture, *, fail_reads=False):
        self.registry, self.fail_reads = registry, fail_reads
        self.messages = [dict(id="message-" + a["key"], thread_id=a["thread_id"],
                              sender=a["email"], subject=a["name"], clean_text=a["fact"])
                         for a in fixture["agents"]]
        with registry.connect() as conn:
            conn.execute("""CREATE TABLE evaluation_actions(
                id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL)""")

    def record(self, agent_id, request_id, kind, payload):
        with self.registry.connect(write=True) as conn:
            cursor = conn.execute("INSERT INTO evaluation_actions(agent_id,request_id,kind,payload) VALUES(?,?,?,?)",
                                  (agent_id, request_id, kind, json.dumps(payload)))
        return cursor.lastrowid

    def tools(self, agent_id, request_id):
        async def search(search_query):
            if self.fail_reads:
                self.record(agent_id, request_id, "read_error", {"error": "Synthetic email service unavailable"})
                return {"error": "Synthetic email service unavailable. No current email facts were retrieved."}
            terms = set(re.findall(r"[\w@.-]+", search_query.casefold()))
            scored = [(len(terms & set(re.findall(r"[\w@.-]+", json.dumps(m).casefold()))), m)
                      for m in self.messages]
            found = [m for score, m in sorted(scored, key=lambda pair: -pair[0]) if score > 0][:5]
            self.record(agent_id, request_id, "read", {"message_ids": [m["id"] for m in found]})
            return found

        async def draft(recipient_email, subject, body, thread_id=None, **kwargs):
            # Persist exactly what the model requested; scoring catches wrong owners/recipients.
            if not all(isinstance(value, str) and value.strip() for value in (recipient_email, subject, body)):
                return {"error": "Draft recipient, subject and body must be nonempty strings"}
            payload = dict(recipient_email=recipient_email, subject=subject, body=body,
                           thread_id=thread_id, **kwargs)
            number = self.record(agent_id, request_id, "draft", payload)
            return dict(id=f"synthetic-draft-{number}", draft_id=f"synthetic-draft-{number}",
                        thread_id=thread_id, recipient=recipient_email, synthetic=True)

        async def blocked_send(**kwargs):
            self.record(agent_id, request_id, "blocked_send", kwargs)
            return {"error": "No approval to send. All operations in this evaluation are synthetic."}

        return dict(task_email_search=search, gmail_create_draft=draft, gmail_execute_draft=blocked_send)

    def actions(self, after=0):
        with self.registry.connect() as conn:
            return [dict(id=r["id"], agent_id=r["agent_id"], request_id=r["request_id"],
                         kind=r["kind"], **json.loads(r["payload"]))
                    for r in conn.execute("SELECT * FROM evaluation_actions WHERE id>? ORDER BY id", (after,))]


async def run_case(case, fixture, template, ids, directory, provider, settings, timeout):
    from server.agents.execution_agent.runtime import ExecutionAgentRuntime
    from server.agents.interaction_agent.runtime import InteractionAgentRuntime
    from server.agents.execution_agent.tools import get_tool_schemas
    from server.services.conversation.log import ConversationLog
    from server.services.conversation.summarization.working_memory_log import WorkingMemoryLog
    from server.services.execution.registry import AgentRegistry
    from server.services.execution.queue import ExecutionService
    from server.services.execution.retrieval import SearchService
    from tests.evaluation.live_provider import LiveEmbedder

    directory.mkdir()
    registry = AgentRegistry(directory / "registry.db")
    with template.connect() as source, registry.connect() as target:
        source.backup(target)
    mailbox = SyntheticMailbox(registry, fixture, fail_reads=case.get("tool_failure", False))
    if case.get("tool_failure"):
        # Historical source records must not reveal the fact whose fresh read is failing.
        with registry.connect(write=True) as conn:
            conn.execute("UPDATE events SET payload=? WHERE agent_id=? AND kind='tool_result'",
                         (json.dumps({"tool": "task_email_search", "success": False,
                                      "result": {"error": "No verified due date in saved history"}}), ids["beta_invoice"]))
    memory = WorkingMemoryLog(directory / "memory.log")
    log = ConversationLog(directory / "conversation.log")
    log._working_memory_log = memory
    log._notify_summarization = lambda: None
    search = SearchService(registry, LiveEmbedder(provider, settings), settings)

    class EvaluationExecution(ExecutionAgentRuntime):
        def __init__(self, job):
            super().__init__(agent_id=job["agent_id"], request_id=job["request_id"],
                             generation=job["generation"], registry=registry)
            self.tool_registry = mailbox.tools(self.agent_id, self.request_id) | {
                "report_task_state": self._report_state, "read_agent_history": self._read_history,
                "confirm_task_reference": self._confirm_reference}
            self.tool_schemas = [s for s in get_tool_schemas()
                                 if s["function"]["name"] in self.tool_registry] + self._state_schemas()

        async def _make_llm_call(self, system_prompt, messages, with_tools):
            return await provider.chat(model=self.model, system=system_prompt, messages=messages,
                                       tools=self.tool_schemas if with_tools else None)

    async def runner(job):
        return await EvaluationExecution(job).execute(job["instructions"])

    async def deliver(payload, context):
        return await runtime.handle_agent_message(payload, batch_context=context)

    queue = ExecutionService(registry, runner=runner, delivery=deliver, settings=settings)
    runtime = InteractionAgentRuntime(registry=registry, queue=queue, search=search,
                                      conversation_log=log, working_memory_log=memory, llm=provider.chat)
    rows = []
    await queue.start()
    try:
        for turn_index, expected in enumerate(case["turns"]):
            provider.case_id = case["id"] + ":" + str(turn_index)
            with registry.connect() as conn:
                before = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM agents")}
                old_jobs = {r[0] for r in conn.execute("SELECT id FROM execution_jobs")}
                event_cursor = conn.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
            action_cursor = max([a["id"] for a in mailbox.actions()] or [0])
            usage_cursor = len(provider.ledger.calls)
            # Reindex changed/new agent cards between turns, outside workflow latency.
            index_started = time.perf_counter()
            while await search.index_pending():
                pass
            index_refresh_ms = (time.perf_counter() - index_started) * 1000
            started = time.perf_counter()
            error, result, settled = None, None, False
            try:
                async with asyncio.timeout(timeout):
                    result = await runtime.execute(expected["text"], request_id=f"{case['id']}:turn:{turn_index}")
                    # A worker may still be waiting for the scheduler to claim its result batch.
                    while True:
                        with registry.connect() as conn:
                            pending = conn.execute(
                                "SELECT COUNT(*) FROM execution_batches WHERE delivery_state!='delivered'").fetchone()[0]
                        if not pending and not queue._workers and not queue._deliveries:
                            break
                        await asyncio.sleep(.03)
                    settled = True
            except Exception as exc:
                error = type(exc).__name__ + ": " + str(exc).replace(provider.api_key, "[redacted]")
                # Cancel owned work before inspecting a stable failed outcome.
                await queue.stop()
            elapsed = (time.perf_counter() - started) * 1000
            with registry.connect() as conn:
                after = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM agents")}
                jobs = [queue._job_dict(r) for r in conn.execute("SELECT * FROM execution_jobs")
                        if r["id"] not in old_jobs]
                events = [dict(r, payload=json.loads(r["payload"])) for r in
                          conn.execute("SELECT * FROM events WHERE id>? ORDER BY id", (event_cursor,))]
                operations = [dict(r) for r in conn.execute(
                    "SELECT request_id,tool_name,result FROM interaction_operations")]
            replies = [e["payload"]["content"] for e in events
                       if e["kind"] == "conversation" and e["payload"].get("tag") == "poke_reply"]
            reply = "\n".join(replies)
            actions = mailbox.actions(action_cursor)
            history_owner_ids = []
            for operation in operations:
                value = json.loads(operation["result"])
                if (operation["tool_name"] == "read_agent_history" and value.get("success")
                        and operation["request_id"] == f"{case['id']}:turn:{turn_index}"):
                    history_owner_ids.append((value.get("payload") or {}).get("agent_id"))
            scored = score_turn(expected, jobs=jobs, actions=actions, reply=reply,
                                agents_before=before, agents_after=after, owner_ids=ids,
                                runtime_success=bool(result and result.success and not error), settled=settled,
                                history_owner_ids=history_owner_ids)
            row = dict(case_id=case["id"], category=case["category"], turn=turn_index,
                       user_text=expected["text"], elapsed_ms=round(elapsed, 2),
                       index_refresh_ms=round(index_refresh_ms, 2), reply=reply,
                       error=error or (result.error if result else None), **scored,
                       jobs=jobs, actions=actions, source_events=events, interaction_operations=operations,
                       model_calls=provider.ledger.calls[usage_cursor:])
            rows.append(row)
            print(f"{case['id']} turn {turn_index + 1}: {'PASS' if row['passed'] else 'FAIL'}", flush=True)
            if not settled or provider.ledger.stop_reason:
                break
    finally:
        await queue.stop()
    return rows


async def evaluate(args, fixture, provider, report):
    from server.config import get_settings
    from server.services.execution.registry import AgentRegistry
    from server.services.execution.retrieval import SearchService
    from tests.evaluation.live_provider import LiveEmbedder

    settings = get_settings()
    with tempfile.TemporaryDirectory(prefix="workflow-state-") as temporary:
        directory = Path(temporary)
        template = AgentRegistry(directory / "template.db")
        ids = seed_registry(template, fixture, args.noise_agents)
        search = SearchService(template, LiveEmbedder(provider, settings), settings)
        started = time.perf_counter()
        while await search.index_pending():
            pass
        with template.connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM index_outbox").fetchone()[0]:
                raise RuntimeError("Live seed indexing failed; refusing to score degraded retrieval")
        report["preindex"] = dict(elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                                  agents=len(ids) + args.noise_agents, usage=provider.ledger.summary())
        write_json(args.output, report)
        for repeat in range(args.repeat):
            for case in fixture["cases"]:
                if args.case and case["id"] not in args.case:
                    continue
                report["current_case"] = dict(id=case["id"], repeat=repeat)
                write_json(args.output, report)
                rows = await run_case(case, fixture, template, ids, directory / f"{repeat}-{case['id']}",
                                      provider, settings, args.case_timeout)
                report["turns"].extend(dict(repeat=repeat, **row) for row in rows)
                report["summary"] = summarize(report["turns"], report["planned_turns"])
                report["usage"] = provider.ledger.summary()
                report.pop("current_case", None)
                write_json(args.output, report)
                if provider.ledger.stop_reason:
                    return


def snapshot(destination):
    """Copy code only; runtime data and environment files stay outside the copy."""
    shutil.copytree(ROOT / "server", destination / "server",
                    ignore=shutil.ignore_patterns("data", "__pycache__", "*.pyc", ".env", ".env.*", "*.log", "*.db*"))
    (destination / "tests").mkdir()
    (destination / "tests/__init__.py").write_text("", encoding="utf-8")
    shutil.copytree(ROOT / "tests/evaluation", destination / "tests/evaluation",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "results"))
    (destination / "tests/fixtures").mkdir()
    shutil.copy2(FIXTURE, destination / "tests/fixtures" / FIXTURE.name)


def evaluation_environment(model=None):
    """Read only model/app settings without importing the application's eager package."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSIO")}
    dotenv = ROOT / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key, value = stripped.split("=", 1)
                key, value = key.strip(), value.strip().strip("'\"")
                if (key.startswith("OPENPOKE_") or key == "OPENROUTER_API_KEY") and key not in env and value:
                    env[key] = value
    if model:
        env["OPENPOKE_MODEL"] = model
    env.update(OPENPOKE_AGENT_COMPACTION="0", OPENPOKE_AGENT_AUTO_ARCHIVE="0",
               OPENPOKE_AGENT_HYBRID_SEARCH="1")
    return env


def worker(args):
    from server.config import get_settings
    from tests.evaluation.live_provider import LiveProvider, UsageLedger
    fixture = load_fixture()
    selected = [c for c in fixture["cases"] if not args.case or c["id"] in args.case]
    planned = args.repeat * sum(len(c["turns"]) for c in selected)
    settings = get_settings()
    ledger = UsageLedger(max_calls=args.max_calls, max_tokens=args.max_tokens, max_cost_usd=args.max_cost_usd)
    provider = LiveProvider(settings.openrouter_api_key, ledger,
                            Path(args.output).with_suffix(".calls.jsonl"), max_output_tokens=args.max_output_tokens,
                            catalog_timeout_seconds=args.catalog_timeout)
    report = dict(schema_version=1, fixture_version=fixture["version"],
                  fixture_sha256=hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                  source=json.loads(os.environ["OPENPOKE_EVALUATION_SOURCE"]),
                  started_at=datetime.now(timezone.utc).isoformat(), mode="live_models_synthetic_tools",
                  models=dict(chat=settings.interaction_agent_model, embedding=settings.agent_embedding_model),
                  planned_turns=planned, turns=[], status="starting",
                  limitations=[
                      "Small authored exploratory suite; not an independent or production benchmark.",
                      "Real IA/EA models, hybrid retrieval, queue and delivery; synthetic email search/draft/send boundary.",
                      "Nested Gmail search model, real mailbox, scheduler, restart recovery and summary quality are not evaluated.",
                      "Deterministic artifact checks; clarification and reply checks use disclosed word matching, not semantic grading.",
                      "Seed index is built once; each case starts from an isolated copy. Workflow latency excludes preindexing.",
                      "No upstream baseline comparison. One repeat does not establish statistical reliability.",
                  ])
    write_json(args.output, report)
    try:
        report["catalog_pricing"] = provider.preflight(settings.interaction_agent_model, settings.agent_embedding_model)
        report["status"] = "running"
        write_json(args.output, report)
        asyncio.run(evaluate(args, fixture, provider, report))
        report["status"] = "completed" if len(report["turns"]) == planned else "incomplete"
    except Exception as exc:
        report["status"] = "blocked" if not ledger.calls else "incomplete"
        report["error"] = type(exc).__name__ + ": " + str(exc).replace(provider.api_key or "NO_KEY", "[redacted]")
    finally:
        report["catalog_pricing_raw"] = provider.catalog_pricing
        report["usage"] = ledger.summary()
        report["summary"] = summarize(report["turns"], planned)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(args.output, report)
    print(json.dumps({k: report.get(k) for k in ("status", "summary", "usage", "error")}, indent=2), flush=True)
    return 0 if report["summary"]["all_passed"] else 1 if report["status"] == "completed" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Authorize billable model and embedding calls")
    parser.add_argument("--model", help="Override OPENPOKE_MODEL for this evaluation only")
    parser.add_argument("--case", action="append", help="Run only this case ID; repeat flag to select multiple")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--noise-agents", type=int, default=40)
    parser.add_argument("--max-calls", type=int, default=100)
    parser.add_argument("--max-tokens", type=int, default=250_000)
    parser.add_argument("--max-cost-usd", type=float, default=2.0)
    parser.add_argument("--max-output-tokens", type=int, default=1200)
    parser.add_argument("--catalog-timeout", type=int, default=30,
                        help="Seconds per read-only OpenRouter catalog request (one retry)")
    parser.add_argument("--case-timeout", type=int, default=150)
    parser.add_argument("--run-timeout", type=int, default=900)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    fixture = load_fixture()
    if args.case and set(args.case) - {c["id"] for c in fixture["cases"]}:
        parser.error("Unknown --case ID")
    if (not 1 <= args.repeat <= 10 or not 0 <= args.noise_agents <= 1000
            or min(args.max_calls, args.max_tokens, args.max_output_tokens, args.catalog_timeout,
                   args.case_timeout, args.run_timeout) < 1
            or not 0 < args.max_cost_usd < float("inf")):
        parser.error("Limits must be positive and finite; repeat 1..10, noise agents 0..1000")
    if not args.live:
        print(json.dumps(dict(mode="fixture_validation_only", billable_calls=0,
                              cases=[dict(id=c["id"], turns=len(c["turns"])) for c in fixture["cases"]
                                     if not args.case or c["id"] in args.case]), indent=2))
        return 0
    if args.worker:
        return worker(args)
    output = (args.output or ROOT / "server/data/evaluations" /
              (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(3).hex()) / "report.json").resolve()
    if output.exists() or output.with_suffix(".calls.jsonl").exists():
        parser.error("Choose a fresh --output path; earlier evidence is never overwritten")
    source_files = sorted([*ROOT.glob("server/**/*.py"), *ROOT.glob("server/**/*.md"),
                           *ROOT.glob("tests/evaluation/*.py"), FIXTURE])
    digest = hashlib.sha256()
    for path in source_files:
        if "data" not in path.relative_to(ROOT).parts:
            digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes())
    source = dict(code_sha256=digest.hexdigest(), git_commit=None)
    try:
        source["git_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                                       text=True, timeout=5).strip()
    except (OSError, subprocess.SubprocessError):
        pass
    env = evaluation_environment(args.model)
    env["OPENPOKE_EVALUATION_SOURCE"] = json.dumps(source)
    # Output paths are arguments; credentials stay out of command lines and copied files.
    arguments = [argument for argument in sys.argv[1:]]
    if "--output" in arguments:
        position = arguments.index("--output")
        del arguments[position:position + 2]
    arguments = [a for a in arguments if not a.startswith("--output=")]
    arguments += ["--output", str(output), "--worker"]
    print(f"Evaluation report: {output}", flush=True)
    with tempfile.TemporaryDirectory(prefix="openpoke-live-code-") as temporary:
        destination = Path(temporary)
        snapshot(destination)
        env["OPENPOKE_AGENT_REGISTRY_PATH"] = str(destination / "server/data/agents/registry.db")
        try:
            result = subprocess.run([sys.executable, str(destination / "tests/evaluation" / Path(__file__).name),
                                     *arguments], cwd=destination, env=env, timeout=args.run_timeout)
            return result.returncode
        except subprocess.TimeoutExpired:
            report = json.loads(output.read_text()) if output.exists() else {"turns": [], "planned_turns": 0}
            report.update(status="incomplete", error="Run deadline exceeded (including event-loop initialization)",
                          usage_incomplete=True,
                          usage_note="Consult reserved/finished call events for in-flight attempts; provider charges may be unknown.")
            if "summary" in report:
                report["summary"].update(complete=False, all_passed=False)
            write_json(output, report)
            print("Run deadline exceeded; partial evidence preserved.", flush=True)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
