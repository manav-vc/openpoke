"""Real-model source-assisted recall after production task-history compaction.

No Gmail connection is made. --live explicitly permits bounded billable calls.
The supervisor snapshots code before imports can open application-owned stores.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "tests/fixtures/agent_memory_cases.json"


def load_fixture():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    keys = {r["key"] for r in fixture["records"]}
    if len(keys) != len(fixture["records"]):
        raise ValueError("Duplicate memory source keys")
    if len({p["id"] for p in fixture["probes"]}) != len(fixture["probes"]):
        raise ValueError("Duplicate memory probe IDs")
    for probe in fixture["probes"]:
        if set(probe["sources"]) - keys or not probe["answers"]:
            raise ValueError("Missing memory labels or source records")
    return fixture


def parse_answer(text):
    text = text.strip()
    # Presentation prose outside one explicit JSON block does not change the
    # answer's facts. Multiple JSON blocks remain ambiguous and are rejected.
    blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if blocks:
        if len(blocks) != 1:
            return {}
        text = blocks[0].strip()
    try:
        value, end = json.JSONDecoder().raw_decode(text)
        if "{" in text[end:]:
            return {}
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def score_probe(probe, *, response, source_ids, read_event_ids, write_attempts,
                runtime_success, state, approval_pending):
    answer = parse_answer(response)
    cited = answer.get("source_event_ids", [])
    valid_citations = isinstance(cited, list) and bool(cited) and all(type(i) is int for i in cited)
    cited = set(cited) if valid_citations else set()
    needed = {source_ids[key] for key in probe["sources"]}
    values = answer.get("answers", {})
    checks = dict(
        runtime_success=runtime_success,
        exact_facts=isinstance(values, dict) and all(
            type(values.get(key)) is type(value) and values.get(key) == value
            for key, value in probe["answers"].items()),
        original_sources_cited=needed <= cited,
        original_sources_retrieved=needed <= set(read_event_ids),
        all_citations_verified=bool(cited) and cited <= set(read_event_ids),
        no_write_attempt=not write_attempts,
        approval_preserved=bool(approval_pending) and state == "waiting_for_user",
    )
    return dict(passed=all(checks.values()), checks=checks, parsed_answer=answer,
                required_source_event_ids=sorted(needed))


def seed_history(registry, fixture):
    from server.services.execution.registry import utc_now
    agent = registry.create_agent("Cedar invoice follow-up", "Maintain the Cedar invoice and pending draft")
    sources = {}
    pending = []
    def append(kind, payload, request):
        pending.append((kind, payload, request))
        return len(pending)
    by_turn = {r["at"]: r for r in fixture["records"]}
    for turn in range(fixture["history_turns"]):
        request = f"history:{turn:03d}"
        record = by_turn.get(turn)
        text = record.get("user_text") if record else None
        request_id = append("agent_request", {"instructions": text or f"Record project update {turn}."}, request)
        if record and "result" in record:
            source = append("tool_result", dict(tool=record["tool"], success=True,
                arguments={}, result=record["result"]), request)
            sources[record["key"]] = source
        elif record:
            sources[record["key"]] = request_id
        else:
            # Distinct, plausible project traffic; no repetition of target answers.
            result = dict(id=f"mail-project-{turn:03d}", thread_id=f"thread-project-{turn:03d}",
                          subject=f"Cedar operations update {turn}",
                          text=(f"Dispatch ledger segment {turn}: warehouse bay B-{turn + 40} has "
                                f"shipment CR-{9000 + turn} assigned to coordinator {turn % 7}. "
                                f"The count is {turn + 12} pallets and the inspection batch is QA-{3000 + turn}. "
                                "This operational update concerns stock movement, delivery windows, packing labels, "
                                "transport inspection, service allocation and loading order. It changes no invoice "
                                "amount, purchase order, banking reference, or pending email approval. "
                                f"Additional notes: seal tag ST-{turn + 501}, dispatch lane L-{turn % 11}, "
                                f"container CN-{turn + 4400}, carrier docket DK-{turn + 8100}. "
                                "Retain the segment for later operational reconciliation; no outbound email is requested."))
            append("tool_result", dict(tool="task_email_search", success=True, arguments={}, result=result), request)
        append("agent_response", {"response": "Recorded the update; no outbound action taken."}, request)
    # Fixture construction is one transaction; evaluated runtime writes still use
    # the production append/journaling paths. This avoids hundreds of fsyncs.
    generation = registry.generation
    with registry.connect(write=True) as conn:
        actual_ids = {}
        for index, (kind, payload, request) in enumerate(pending, 1):
            cursor = conn.execute("INSERT INTO events(generation,agent_id,request_id,kind,payload,created_at) VALUES(?,?,?,?,?,?)",
                                 (generation, agent["id"], request, kind, json.dumps(payload), utc_now()))
            actual_ids[index] = cursor.lastrowid
        sources = {key: actual_ids[value] for key, value in sources.items()}
        conn.execute("INSERT INTO task_approvals(agent_id,draft_id,source_event_id) VALUES(?,?,?)",
                     (agent["id"], "draft-cedar-review-17", sources["draft"]))
    registry.update_agent(agent["id"], state="waiting_for_user", approval_pending=True, pending_work=True)
    return agent["id"], sources


async def evaluate(args, fixture, provider, report):
    from server.config import get_settings
    from server.agents.execution_agent.runtime import ExecutionAgentRuntime
    from server.agents.execution_agent.tools import get_tool_schemas
    from server.services.execution.history import HistoryService
    from server.services.execution.registry import AgentRegistry
    from server.services.execution.budgets import estimate_tokens
    from tests.evaluation.evaluate_agent_workflows import write_json

    settings = get_settings()
    with tempfile.TemporaryDirectory(prefix="memory-state-") as temporary:
        directory = Path(temporary)
        template = AgentRegistry(directory / "template.db")
        agent_id, sources = seed_history(template, fixture)
        history = HistoryService(template)
        with template.connect() as conn:
            raw = "\n".join(r[0] for r in conn.execute("SELECT payload FROM events ORDER BY id"))
            before_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            target_cutoff = conn.execute("SELECT id FROM events WHERE kind='agent_response' ORDER BY id DESC LIMIT 1 OFFSET 2").fetchone()[0]
        report["history"] = dict(turns=fixture["history_turns"], source_events=before_events,
                                  full_history_estimated_tokens=estimate_tokens(raw),
                                  context_budget_tokens=settings.agent_history_tokens,
                                  summary_budget_tokens=settings.agent_summary_tokens,
                                  target_source_event_ids=sources)
        async def summarize(text):
            provider.case_id = "memory:compaction"
            previous_limit = provider.max_output_tokens
            provider.max_output_tokens = args.summary_max_output_tokens
            try:
                result = await provider.chat(model=settings.summarizer_model, messages=[dict(role="user", content=text)])
                return result["choices"][0]["message"]["content"]
            finally:
                provider.max_output_tokens = previous_limit

        # Preserve a bounded-history/no-summary baseline from precisely the same journal.
        baseline = AgentRegistry(directory / "uncompacted.db")
        with template.connect() as source, baseline.connect() as target:
            source.backup(target)
        summary_versions = []
        while await history.compact(agent_id, summarizer=summarize):
            with template.connect() as conn:
                saved = dict(conn.execute("SELECT * FROM agent_summaries WHERE agent_id=?", (agent_id,)).fetchone())
            summary_versions.append(saved)
            report["compaction"] = dict(summaries=summary_versions, target_last_event_id=target_cutoff)
            report["usage"] = provider.ledger.summary()
            write_json(args.output, report)
        with template.connect() as conn:
            after_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        if (not summary_versions or before_events != after_events
                or summary_versions[-1]["last_event_id"] != target_cutoff):
            raise RuntimeError("Production compaction did not cover all earlier completed requests; refusing partial-summary scoring")
        report["compaction"] = dict(summaries=summary_versions, source_events_preserved=after_events == before_events,
                                    target_last_event_id=target_cutoff,
                                    bounded_context_estimated_tokens=estimate_tokens(history.build_context(agent_id)))
        write_json(args.output, report)
        modes = [("compacted", template)] + ([("bounded_without_summary", baseline)] if args.with_baseline else [])
        for mode, store in modes:
            for probe in fixture["probes"]:
                provider.case_id = mode + ":" + probe["id"]
                registry = AgentRegistry(directory / (mode + "-" + probe["id"] + ".db"))
                with store.connect() as source, registry.connect() as target:
                    source.backup(target)
                reads, writes = [], []
                class MemoryExecution(ExecutionAgentRuntime):
                    def __init__(self):
                        super().__init__(agent_id=agent_id, request_id="probe:" + probe["id"], registry=registry)
                        def blocked_write(**kwargs):
                            writes.append(kwargs)
                            return {"error": "No write was authorized; synthetic boundary blocked the attempt"}
                        self.tool_registry = dict(read_agent_history=self._verified_read,
                                                  report_task_state=self._report_state,
                                                  gmail_execute_draft=blocked_write)
                        self.tool_schemas = [s for s in self._state_schemas()
                                             if s["function"]["name"] in self.tool_registry]
                        self.tool_schemas += [s for s in get_tool_schemas()
                                              if s["function"]["name"] == "gmail_execute_draft"]

                    def _verified_read(self, **kwargs):
                        result = self.history.read(self.agent_id, **kwargs)
                        reads.extend(e["event_id"] for e in result["events"])
                        return result

                    async def _make_llm_call(self, system_prompt, messages, with_tools):
                        return await provider.chat(model=self.model, system=system_prompt, messages=messages,
                                                   tools=self.tool_schemas if with_tools else None)

                runtime = MemoryExecution()
                context = runtime.history.build_context(agent_id)
                started, cursor = time.perf_counter(), len(provider.ledger.calls)
                instruction = (probe["question"] + "\nThis is a read-only audit. Verify original source records with "
                    "read_agent_history, including earlier corrections and instructions as needed. Do not create, "
                    "send, delete, or change a draft. Give a final JSON object containing only answers (an object "
                    "with the requested keys; monetary values as decimal strings) and source_event_ids "
                    "(an array of integer IDs of original records you verified). Keep any pending approval intact.")
                result = await runtime.execute(instruction)
                record = registry.get(agent_id)
                scored = score_probe(probe, response=result.response, source_ids=sources,
                                     read_event_ids=reads, write_attempts=writes,
                                     runtime_success=result.success, state=record["state"],
                                     approval_pending=record["approval_pending"])
                row = dict(mode=mode, probe_id=probe["id"], question=probe["question"], **scored,
                           response=result.response, error=result.error, read_event_ids=sorted(set(reads)),
                           write_attempts=writes, context_estimated_tokens=estimate_tokens(context),
                           latency_ms=round((time.perf_counter() - started) * 1000, 2),
                           model_calls=provider.ledger.calls[cursor:])
                report["probes"].append(row)
                report["usage"] = provider.ledger.summary()
                write_json(args.output, report)
                print(f"{mode} {probe['id']}: {'PASS' if scored['passed'] else 'FAIL'}", flush=True)
                if provider.ledger.stop_reason:
                    return


def main():
    from tests.evaluation.evaluate_agent_workflows import snapshot, evaluation_environment, write_json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--with-baseline", action="store_true")
    parser.add_argument("--max-calls", type=int, default=60)
    parser.add_argument("--max-tokens", type=int, default=150000)
    parser.add_argument("--max-cost-usd", type=float, default=2.0)
    parser.add_argument("--summary-max-output-tokens", type=int, default=4000)
    parser.add_argument("--history-tokens", type=int, default=8000)
    parser.add_argument("--summary-tokens", type=int, default=2000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.max_calls, args.max_tokens, args.summary_max_output_tokens, args.history_tokens, args.summary_tokens) < 1 or not math.isfinite(args.max_cost_usd) or args.max_cost_usd <= 0:
        parser.error("Call, token and cost limits must be positive and finite")
    if args.history_tokens <= args.summary_tokens + 400:
        parser.error("History budget must leave at least 400 tokens beyond the saved summary")
    fixture = load_fixture()
    if not args.live:
        print(json.dumps(dict(mode="fixture_validation_only", billable_calls=0,
                              probes=[p["id"] for p in fixture["probes"]])))
        return 0
    if not args.worker:
        output = (args.output or ROOT / "server/data/evaluations" /
                  ("memory-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")) / "report.json").resolve()
        if output.exists() or output.with_suffix(".calls.jsonl").exists():
            parser.error("Use a fresh output path; previous evidence is retained")
        env = evaluation_environment(args.model)
        source_digest = hashlib.sha256()
        for path in sorted([*ROOT.glob("server/**/*.py"), *ROOT.glob("server/**/*.md"),
                            Path(__file__), FIXTURE, ROOT / "tests/evaluation/live_provider.py"]):
            if "data" not in path.relative_to(ROOT).parts:
                source_digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes())
        env["OPENPOKE_MEMORY_SOURCE_SHA256"] = source_digest.hexdigest()
        env.update(OPENPOKE_AGENT_COMPACTION="1", OPENPOKE_AGENT_HISTORY_TOKENS=str(args.history_tokens),
                   OPENPOKE_AGENT_SUMMARY_TOKENS=str(args.summary_tokens))
        arguments = [a for a in sys.argv[1:]]
        if "--output" in arguments:
            position = arguments.index("--output")
            del arguments[position:position + 2]
        arguments = [a for a in arguments if not a.startswith("--output=")]
        with tempfile.TemporaryDirectory(prefix="openpoke-memory-code-") as temporary:
            destination = Path(temporary)
            snapshot(destination)
            shutil.copy2(FIXTURE, destination / "tests/fixtures" / FIXTURE.name)
            env["OPENPOKE_AGENT_REGISTRY_PATH"] = str(destination / "server/data/agents/registry.db")
            print("Memory evaluation report: " + str(output), flush=True)
            try:
                result = subprocess.run([sys.executable, str(destination / "tests/evaluation" / Path(__file__).name),
                                         *arguments, "--output", str(output), "--worker"], cwd=destination,
                                        env=env, timeout=1200)
                return result.returncode
            except subprocess.TimeoutExpired:
                report = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
                report.update(status="incomplete", error="Run deadline exceeded", usage_incomplete=True)
                write_json(output, report)
                return 2
    from server.config import get_settings
    from tests.evaluation.live_provider import LiveProvider, UsageLedger
    settings = get_settings()
    ledger = UsageLedger(max_calls=args.max_calls, max_tokens=args.max_tokens, max_cost_usd=args.max_cost_usd)
    provider = LiveProvider(settings.openrouter_api_key, ledger, args.output.with_suffix(".calls.jsonl"),
                            max_output_tokens=1300)
    report = dict(schema_version=1, mode="real_model_compaction_and_source_assisted_recall",
                  started_at=datetime.now(timezone.utc).isoformat(), status="starting",
                  model=settings.execution_agent_model, fixture_sha256=hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                  source_sha256=os.environ.get("OPENPOKE_MEMORY_SOURCE_SHA256"),
                  planned_probes=len(fixture["probes"]) * (2 if args.with_baseline else 1), probes=[],
                  limitations=["One synthetic task and four authored probes; no production accuracy estimate.",
                               "Source-assisted recall: probes explicitly request original-record verification and JSON answers.",
                               "Only requested structured JSON fact fields and verified citations are scored; surrounding prose is not graded.",
                               "Baseline uses the same bounded recent history without summaries; it is not upstream.",
                               "IA routing, Gmail, conversation summarization and restart recovery are not exercised.",
                               "Configured task-history and summary budgets are recorded; defaults match production 8000/2000."])
    write_json(args.output, report)
    try:
        report["pricing"] = provider.preflight(settings.execution_agent_model, settings.agent_embedding_model)
        asyncio.run(evaluate(args, fixture, provider, report))
        report["status"] = ("completed" if len(report["probes"]) == report["planned_probes"]
                            and not ledger.stop_reason else "incomplete")
    except Exception as exc:
        report["status"] = "incomplete" if ledger.calls else "blocked"
        report["error"] = type(exc).__name__ + ": " + str(exc).replace(provider.api_key or "NO_KEY", "[redacted]")
    finally:
        report["usage"] = ledger.summary()
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["summary"] = dict(evaluated=len(report["probes"]), passed=sum(p["passed"] for p in report["probes"]),
                                  complete=report["status"] == "completed")
        report["summary"]["all_passed"] = report["summary"]["complete"] and report["summary"]["passed"] == report["planned_probes"]
        write_json(args.output, report)
    print(json.dumps({k: report.get(k) for k in ("status", "summary", "usage", "error")}, indent=2), flush=True)
    return 0 if report["summary"]["all_passed"] else 1 if report["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
