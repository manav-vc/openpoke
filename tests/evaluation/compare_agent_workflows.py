"""Paired live evaluation of upstream and the fork, with one synthetic mailbox.

The supervisor runs each implementation in a separate temporary source tree.
Upstream application source is read from Git without edits. Adapters replace
external email tools, inject the metered model transport, and observe outcomes.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import types
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.evaluation.evaluate_agent_workflows import (
    FIXTURE, SyntheticMailbox, evaluation_environment, load_fixture, score_turn,
    snapshot, write_json,
)

BASELINE = "5b5f635935a64ab37884c025d70abb0ed731c094"


class ActionStore:
    """Small transaction store that releases Windows file handles promptly."""
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self, write=False):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def application_source_sha256(directory):
    digest = hashlib.sha256()
    for path in sorted([*directory.glob("server/**/*.py"), *directory.glob("server/**/*.md")]):
        if "data" not in path.relative_to(directory).parts:
            digest.update(path.relative_to(directory).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def describe_source(directory, variant, fork_source):
    # Each arm identifies the files it actually imports. The fork's working-tree
    # hash belongs only to the fork; upstream is the unedited archived revision.
    source = dict(fork_source, application_sha256=application_source_sha256(directory))
    if variant == "upstream":
        source.update(git_commit=BASELINE, working_tree=False)
    return source


def observe_conversation(conversation, entry_cursor):
    # load_transcript() expands escaped newlines into multiline XML. Splitting
    # that rendering into lines silently drops draft previews and long replies.
    # Read the native parsed entries instead and retain the observed evidence.
    entries = [dict(tag=tag, timestamp=timestamp, content=content)
               for tag, timestamp, content in list(conversation.iter_entries())[entry_cursor:]]
    reply = "\n".join(entry["content"] for entry in entries if entry["tag"] == "poke_reply")
    return entries, reply


def pin_models(settings, model):
    # Historical Settings predates OPENPOKE_MODEL. Inject the same configuration
    # into its cached settings object, without modifying upstream source.
    for field in ("interaction_agent_model", "execution_agent_model", "execution_agent_search_model",
                  "summarizer_model", "email_classifier_model"):
        setattr(settings, field, model)


def common_score(row):
    # Compare requested work: correct saved owner, facts, persisted draft, and
    # no attempted send. The fork adds persisted approval metadata and exact
    # preview rendering; keep those native checks, and report preview separately.
    checks = {k: v for k, v in row["checks"].items()
              if k not in {"waiting_for_approval", "draft_presented_verbatim"}}
    calls = [c for c in row["model_calls"] if c["kind"] == "chat"]
    return dict(passed=all(checks.values()), checks=checks,
                prompt_tokens=sum((c.get("usage") or {}).get("prompt_tokens", 0) for c in calls),
                chat_calls=len(calls), wrong_owner=row["wrong_owner"],
                duplicate_agents=row["duplicate_agents"],
                draft_preview_exact=row["checks"].get("draft_presented_verbatim"))


def summarize_common(rows, planned):
    timings = sorted(r["elapsed_ms"] for r in rows)
    return dict(planned_turns=planned, evaluated_turns=len(rows),
                passed=sum(r["common"]["passed"] for r in rows),
                complete=len(rows) == planned,
                wrong_owner_turns=sum(r["common"]["wrong_owner"] for r in rows),
                duplicate_agents=sum(r["common"]["duplicate_agents"] for r in rows),
                exact_draft_previews=sum(r["common"]["draft_preview_exact"] is True for r in rows),
                draft_preview_cases=sum(r["common"]["draft_preview_exact"] is not None for r in rows),
                prompt_tokens=sum(r["common"]["prompt_tokens"] for r in rows),
                p50_workflow_ms=statistics.median(timings) if timings else None,
                p95_workflow_ms=timings[min(len(timings) - 1, int(len(timings) * .95))] if timings else None)


def bootstrap_upstream():
    # Skip only server/__init__.py's eager FastAPI application construction.
    # No app server, scheduler, or real integration is needed for either arm.
    package = types.ModuleType("server")
    package.__path__ = [str(ROOT / "server")]
    sys.modules["server"] = package
    services = types.ModuleType("server.services")
    services.__path__ = [str(ROOT / "server/services")]
    sys.modules["server.services"] = services
    # The transport's token estimator is harness support, not upstream behavior.
    spec = importlib.util.spec_from_file_location(
        "server.services.execution.budgets", ROOT / "tests/evaluation/_budget_support.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if os.name == "nt":
        # Upstream imports Unix fcntl. Use a real one-byte Windows file lock for
        # its roster writer; multi-process locking is outside this evaluation.
        import msvcrt
        shim = types.ModuleType("fcntl")
        shim.LOCK_EX, shim.LOCK_NB, shim.LOCK_UN = 2, 4, 8

        def flock(fd, operation):
            offset = os.lseek(fd, 0, os.SEEK_CUR)
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                mode = msvcrt.LK_UNLCK if operation & 8 else msvcrt.LK_NBLCK if operation & 4 else msvcrt.LK_LOCK
                msvcrt.locking(fd, mode, 1)
            except OSError as exc:
                raise BlockingIOError(str(exc)) from exc
            finally:
                os.lseek(fd, offset, os.SEEK_SET)

        shim.flock = flock
        sys.modules["fcntl"] = shim


async def run_upstream_case(case, fixture, directory, provider, timeout, noise_count):
    from server.agents.interaction_agent import runtime as ia_runtime, tools as ia_tools, agent as ia_agent
    from server.agents.execution_agent import runtime as ea_runtime, agent as ea_agent, batch_manager
    from server.services.conversation.log import ConversationLog
    from server.services.conversation.summarization.working_memory_log import WorkingMemoryLog
    from server.services.execution.roster import AgentRoster
    from server.services.execution.log_store import ExecutionAgentLogStore
    from server.config import get_settings

    directory.mkdir()
    roster = AgentRoster(directory / "roster.json")
    logs = ExecutionAgentLogStore(directory / "agents")
    memory = WorkingMemoryLog(directory / "memory.log")
    conversation = ConversationLog(directory / "conversation.log")
    conversation._working_memory_log = memory
    conversation._notify_summarization = lambda: None
    settings = get_settings()
    ids = {a["key"]: a["name"] for a in fixture["agents"]}
    for item in fixture["agents"]:
        roster.add_agent(item["name"])
        message = dict(id="message-" + item["key"], thread_id=item["thread_id"],
                       sender=item["email"], subject=item["name"], clean_text=item["fact"])
        result = ([message] if not (case.get("tool_failure") and item["key"] == "beta_invoice")
                  else {"error": "No verified due date in saved history"})
        logs.record_tool_response(item["name"], "task_email_search", json.dumps(result))
    for index in range(noise_count):
        roster.add_agent(f"Equipment inventory {index}")

    mailbox = SyntheticMailbox(ActionStore(directory / "actions.sqlite"), fixture,
                               fail_reads=case.get("tool_failure", False))
    jobs, deliveries = [], []
    turn_request = ""

    class EvaluationExecution(ea_runtime.ExecutionAgentRuntime):
        def __init__(self, agent_name):
            super().__init__(agent_name)
            self.tool_registry = mailbox.tools(agent_name, turn_request)
            self.tool_schemas = [s for s in self.tool_schemas
                                 if s["function"]["name"] in self.tool_registry]

        async def _make_llm_call(self, system_prompt, messages, with_tools):
            return await provider.chat(model=self.model, system=system_prompt, messages=messages,
                                       tools=self.tool_schemas if with_tools else None)

        async def execute(self, instructions):
            job = dict(agent_id=self.agent.name, instructions=instructions, status="running")
            jobs.append(job)
            result = await super().execute(instructions)
            job.update(status="succeeded" if result.success else "failed", response=result.response,
                       error=result.error)
            return result

    class EvaluationBatches(batch_manager.ExecutionBatchManager):
        async def _dispatch_to_interaction_agent(self, payload):
            runtime = ia_runtime.InteractionAgentRuntime()
            deliveries.append(asyncio.create_task(runtime.handle_agent_message(payload)))

    async def metered_ia(self, system_prompt, messages):
        return await provider.chat(model=self.model, system=system_prompt, messages=messages,
                                   tools=self.tool_schemas)

    manager = EvaluationBatches(timeout_seconds=timeout)
    rows = []
    with ExitStack() as stack:
        for module, name, value in (
            (ia_agent, "get_agent_roster", lambda: roster),
            (ia_tools, "get_agent_roster", lambda: roster),
            (ia_tools, "get_execution_agent_logs", lambda: logs),
            (ea_agent, "get_execution_agent_logs", lambda: logs),
            (ia_tools, "get_conversation_log", lambda: conversation),
            (ia_runtime, "get_conversation_log", lambda: conversation),
            (ia_runtime, "get_working_memory_log", lambda: memory),
            (ia_tools, "_EXECUTION_BATCH_MANAGER", manager),
            (batch_manager, "ExecutionAgentRuntime", EvaluationExecution),
            (ia_runtime.InteractionAgentRuntime, "_make_llm_call", metered_ia),
        ):
            stack.enter_context(patch.object(module, name, value))
        settings.conversation_summary_threshold = 0
        for index, expected in enumerate(case["turns"]):
            turn_request = f"{case['id']}:turn:{index}"
            provider.case_id = turn_request
            before = {name: {} for name in roster.get_agents()}
            old_job_count, old_action_count = len(jobs), len(mailbox.actions())
            entry_cursor = len(list(conversation.iter_entries()))
            usage_cursor = len(provider.ledger.calls)
            started = time.perf_counter()
            error, settled, result = None, False, None
            try:
                async with asyncio.timeout(timeout):
                    result = await ia_runtime.InteractionAgentRuntime().execute(expected["text"])
                    # Observe upstream's background tasks without replacing its
                    # submission, execution, batch, or interaction loops.
                    while True:
                        await asyncio.sleep(.03)
                        other = [task for task in asyncio.all_tasks()
                                 if task is not asyncio.current_task() and not task.done()]
                        if not other and not manager._pending and manager._batch_state is None:
                            break
                    settled = True
            except Exception as exc:
                error = type(exc).__name__ + ": " + str(exc).replace(provider.api_key, "[redacted]")
                active = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
                for task in active:
                    task.cancel()
                await asyncio.gather(*active, return_exceptions=True)
            elapsed = (time.perf_counter() - started) * 1000
            conversation_entries, reply = observe_conversation(conversation, entry_cursor)
            after = {name: {} for name in roster.get_agents()}
            turn_jobs, actions = jobs[old_job_count:], mailbox.actions()[old_action_count:]
            scored = score_turn(expected, jobs=turn_jobs, actions=actions, reply=reply,
                                agents_before=before, agents_after=after, owner_ids=ids,
                                runtime_success=bool(result and result.success and not error)
                                and all(t.done() and not t.cancelled() and t.exception() is None
                                        and t.result().success for t in deliveries), settled=settled)
            row = dict(case_id=case["id"], category=case["category"], turn=index,
                       user_text=expected["text"], elapsed_ms=round(elapsed, 2),
                       reply=reply, conversation_entries=conversation_entries,
                       error=error, jobs=turn_jobs, actions=actions,
                       model_calls=provider.ledger.calls[usage_cursor:], **scored)
            row["common"] = common_score(row)
            rows.append(row)
            print(f"upstream {case['id']} turn {index+1}: {'PASS' if row['common']['passed'] else 'FAIL'}", flush=True)
            if not settled or provider.ledger.stop_reason:
                break
    return rows


def worker(args):
    if args.variant == "upstream":
        bootstrap_upstream()
    from server.config import get_settings
    from tests.evaluation.live_provider import LiveProvider, UsageLedger

    settings = get_settings()
    pin_models(settings, os.environ["OPENPOKE_MODEL"])
    fixture = load_fixture()
    if args.case:
        fixture = dict(fixture, cases=[c for c in fixture["cases"] if c["id"] in args.case])
    planned = sum(len(c["turns"]) for c in fixture["cases"])
    ledger = UsageLedger(max_calls=args.max_calls, max_tokens=args.max_tokens, max_cost_usd=args.max_cost_usd)
    embedding_model = getattr(settings, "agent_embedding_model", "openai/text-embedding-3-small")
    provider = LiveProvider(settings.openrouter_api_key, ledger, args.output.with_suffix(".calls.jsonl"),
                            max_output_tokens=1600, catalog_timeout_seconds=30)
    report = dict(variant=args.variant, baseline=BASELINE, planned_turns=planned, turns=[], status="starting",
                  started_at=datetime.now(timezone.utc).isoformat(),
                  models=dict(chat=settings.interaction_agent_model, embedding=embedding_model,
                              embedding_used=args.variant == "fork"),
                  fixture_sha256=hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                  source=json.loads(os.environ["OPENPOKE_EVALUATION_SOURCE"]))
    write_json(args.output, report)

    async def evaluate():
        with tempfile.TemporaryDirectory(prefix="paired-state-") as temporary:
            if args.variant == "upstream":
                for case in fixture["cases"]:
                    rows = await run_upstream_case(case, fixture, Path(temporary)/case["id"], provider,
                                                   args.case_timeout, args.noise_agents)
                    report["turns"].extend(rows)
                    write_json(args.output, report)
                    if ledger.stop_reason:
                        break
            else:
                from tests.evaluation.evaluate_agent_workflows import evaluate as evaluate_fork
                args.repeat = 1
                # The shared runner already records all native queue/approval checks.
                await evaluate_fork(args, fixture, provider, report)
                for row in report["turns"]:
                    row["common"] = common_score(row)

    try:
        report["catalog_pricing"] = provider.preflight(settings.interaction_agent_model, embedding_model)
        asyncio.run(evaluate())
        report["status"] = "completed" if len(report["turns"]) == planned else "incomplete"
    except Exception as exc:
        report["status"] = "incomplete"
        report["error"] = type(exc).__name__ + ": " + str(exc).replace(provider.api_key or "NO_KEY", "[redacted]")
    finally:
        for row in report["turns"]:
            row.setdefault("common", common_score(row))
        report["summary"] = summarize_common(report["turns"], planned)
        report["usage"] = ledger.summary()
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(args.output, report)
    print(json.dumps({k: report.get(k) for k in ("variant", "status", "summary", "usage", "error")}, indent=2), flush=True)
    return 0 if report["status"] == "completed" else 2


def prepare_source(destination, variant):
    snapshot(destination)
    if variant == "upstream":
        # destination is a newly created, task-owned temporary directory.
        budget = (destination / "server/services/execution/budgets.py").read_bytes()
        shutil.rmtree(destination / "server")
        archive = subprocess.check_output(["git", "archive", "--format=zip", BASELINE, "server"], cwd=ROOT)
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            zipped.extractall(destination)
        (destination / "tests/evaluation/_budget_support.py").write_bytes(budget)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--case", action="append")
    parser.add_argument("--noise-agents", type=int, default=40)
    parser.add_argument("--max-calls", type=int, default=150)
    parser.add_argument("--max-tokens", type=int, default=750000)
    parser.add_argument("--max-cost-usd", type=float, default=2.0, help="Estimated budget per implementation")
    parser.add_argument("--case-timeout", type=int, default=180)
    parser.add_argument("--run-timeout", type=int, default=1200)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--only", choices=("upstream", "fork"), help="Evaluate just one arm; retain separate original reports")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--variant", choices=("upstream", "fork"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    fixture = load_fixture()
    if args.case and set(args.case) - {c["id"] for c in fixture["cases"]}:
        parser.error("Unknown --case")
    if (not 0 <= args.noise_agents <= 1000
            or min(args.max_calls, args.max_tokens, args.case_timeout, args.run_timeout) <= 0
            or not math.isfinite(args.max_cost_usd) or args.max_cost_usd <= 0):
        parser.error("Invalid limits")
    if args.worker:
        return worker(args)
    if not args.live:
        print("Validated paired fixture; use --live to authorize metered requests.")
        return 0
    output = (args.output or ROOT / "server/data/evaluations" /
              (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-paired") / "comparison.json").resolve()
    if output.exists() or output.parent.joinpath("upstream.json").exists() or output.parent.joinpath("fork.json").exists():
        parser.error("Choose a fresh output directory")
    source = dict(application_sha256=application_source_sha256(ROOT),
                  git_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                  working_tree=True)
    env = evaluation_environment(args.model)
    # Each source revision has a different historical default; pin one model
    # before either worker imports configuration.
    env.setdefault("OPENPOKE_MODEL", "anthropic/claude-sonnet-5.5")
    reports = {}
    for variant in ([args.only] if args.only else ("upstream", "fork")):
        report_path = output.parent / f"{variant}.json"
        print(f"Paired {variant} evidence: {report_path}", flush=True)
        with tempfile.TemporaryDirectory(prefix=f"paired-{variant}-code-") as temporary:
            destination = Path(temporary)
            prepare_source(destination, variant)
            env["OPENPOKE_EVALUATION_SOURCE"] = json.dumps(describe_source(destination, variant, source))
            env["OPENPOKE_AGENT_REGISTRY_PATH"] = str(destination/"server/data/agents/registry.db")
            arguments = ["--worker", "--variant", variant, "--output", str(report_path),
                         "--noise-agents", str(args.noise_agents), "--max-calls", str(args.max_calls),
                         "--max-tokens", str(args.max_tokens), "--max-cost-usd", str(args.max_cost_usd),
                         "--case-timeout", str(args.case_timeout)]
            for case in args.case or []:
                arguments += ["--case", case]
            try:
                subprocess.run([sys.executable, str(destination/"tests/evaluation"/Path(__file__).name), *arguments],
                               cwd=destination, env=env, timeout=args.run_timeout, check=False)
            except subprocess.TimeoutExpired:
                print(f"{variant}: deadline exceeded; partial journal retained", flush=True)
            reports[variant] = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else dict(status="blocked", turns=[])
    comparison = dict(schema_version=1, source=source, baseline=BASELINE,
                      fixture_sha256=hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                      reports=reports,
                      limitations=[
                          "One paired run on ten authored turns; no statistical reliability or latency claim.",
                          "Identical model, requests, source mailbox, saved task names/facts and noise in fresh stores.",
                          "Native prompts, tool schemas, histories, routing, loops and dispatch differ by implementation.",
                          "Upstream lacks archive and pending-approval metadata; tasks are all seeded into its native roster.",
                          "Shared task success omits persisted approval metadata and exact draft preview; preview is reported separately, and every send attempt fails task success.",
                          "Synthetic email boundary replaces nested Gmail search and does not validate real Gmail.",
                          "Latency includes interaction, execution and delivery; excludes fork embedding preindex/refresh.",
                          "Chat prompt tokens are provider-reported across all per-turn IA/EA calls; embedding setup is separate.",
                          "Temporary import bootstrap avoids app startup; Windows upstream roster uses an msvcrt fcntl adapter.",
                      ])
    write_json(output, comparison)
    print(json.dumps({variant: report.get("summary") for variant, report in reports.items()}, indent=2))
    return 0 if all(r.get("status") == "completed" for r in reports.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
