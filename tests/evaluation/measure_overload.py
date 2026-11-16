"""Reproduce the three documentation charts without model or Gmail calls.

Run with the project's Python environment from the repository root.
The concurrency fixture exercises SQLite admission, with simulated completions;
it does not measure real execution time or run the asynchronous scheduler.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import typing
from html import escape
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENROUTER_API_KEY", "offline-chart-fixture")

from server.config import Settings
from server.services.execution import budgets
from server.services.execution.history import HistoryService
from server.services.execution.queue import ExecutionService
from server.services.execution.registry import AgentRegistry

BASELINE = "5b5f635935a64ab37884c025d70abb0ed731c094"
OUTPUT = ROOT / "docs/charts/measurements.json"


def original(path):
    return subprocess.check_output(["git", "show", f"{BASELINE}:{path}"], cwd=ROOT).decode("utf-8")


def definitions(source, names, namespace):
    """Load unmodified baseline definitions without their application singletons."""
    tree = ast.parse(source)
    selected = [node for node in tree.body if getattr(node, "name", None) in names]
    assert len(selected) == len(names), names
    # Deferred annotations keep the historical module's Optional annotation valid.
    tree.body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected]
    exec(compile(ast.fix_missing_locations(tree), "<upstream-chart-fixture>", "exec"), namespace)
    return namespace


def immediate(coroutine):
    """These admission coroutines do synchronous SQL and never suspend for I/O."""
    try:
        yielded = coroutine.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coroutine.close()
    raise AssertionError(f"Admission now requires an event loop: {yielded!r}")


def roster_measurements(report):
    source = original("server/agents/interaction_agent/agent.py")
    points = []
    for item in report["benchmarks"]:
        size = item["agents"]
        names = [f"Synthetic task {i:05d}" for i in range(size)]
        roster = SimpleNamespace(load=lambda: None, get_agents=lambda: names)
        scope = definitions(source, {"_render_active_agents"},
                            {"get_agent_roster": lambda: roster, "escape": escape})
        count = scope["_render_active_agents"]().count('<agent name="')
        assert count == size and item["max_candidates"] <= 30
        points.append({"saved_agents": size, "upstream_roster_entries": count,
                       "new_search_candidates": item["max_candidates"],
                       "new_first_page_estimated_tokens": item["max_first_page_estimated_tokens"]})
    return points


def history_measurements(directory):
    source = original("server/services/execution/log_store.py")
    scope = dict(vars(typing))
    import re
    import threading
    from html import unescape
    scope.update(Path=Path, re=re, threading=threading, escape=escape, unescape=unescape,
                 logger=logging.getLogger("chart-fixture"),
                 now_in_user_timezone=lambda _: "2026-11-16 00:00:00",
                 _ATTR_PATTERN=re.compile(r'(\w+)\s*=\s*"([^"]*)"'))
    definitions(source, {"_slugify", "_encode_payload", "_decode_payload", "ExecutionAgentLogStore"}, scope)
    store = scope["ExecutionAgentLogStore"](directory / "legacy")
    agent_scope = definitions(original("server/agents/execution_agent/agent.py"), {"ExecutionAgent"},
                              {"get_execution_agent_logs": lambda: store,
                               "SYSTEM_PROMPT_TEMPLATE": ""})
    old = agent_scope["ExecutionAgent"]("Invoice follow-ups")
    registry = AgentRegistry(directory / "history.sqlite")
    agent = registry.create_agent("Invoice follow-ups", "Read invoice status updates")
    history = HistoryService(registry)
    history.settings = Settings(agent_history_tokens=8000, agent_summary_tokens=2000, agent_excerpt_tokens=2000)
    points, tool_events = [], []
    checkpoints = {1, 2, 5, 10, 25, 50, 75, 100}
    for i in range(1, 101):
        request_id = f"history-{i:03d}"
        instruction = f"Read the latest status for invoice INV-420, update {i}. Report the saved facts without sending email."
        arguments = '{"invoice_id":"INV-420","thread_id":"synthetic-thread"}'
        # Same tool output delivered to both implementations. Upstream's native
        # record_tool_execution retains only its first 500 characters.
        result = "\n".join(f"Update {j:02d}: Invoice INV-420 is CAD 420; due March 15. This synthetic email reports processing progress."
                           for j in range(1, 61))
        response = f"Update {i}: invoice INV-420 remains CAD 420, due March 15; no email sent."
        store.record_request(old.name, instruction)
        old.record_tool_execution("task_email_search", arguments, result)
        old.record_response(response)
        registry.append_event("agent_request", {"instructions": instruction}, agent["id"], request_id)
        event = history.record_tool(agent["id"], request_id, "task_email_search", arguments, result,
                                    True, f"tool-{i}", registry.generation)
        tool_events.append((event, result))
        registry.append_event("agent_response", {"response": response}, agent["id"], request_id)
        if i in checkpoints:
            before = budgets.estimate_tokens(old.build_system_prompt_with_history())
            after = budgets.estimate_tokens(history.build_context(agent["id"]))
            assert after <= 8000
            points.append({"completed_exchanges": i, "upstream_estimated_tokens": before,
                           "new_estimated_tokens": after})
    assert all(registry.get_event(event)["payload"]["result"] == result for event, result in tool_events)
    return {"points": points, "context_budget": 8000, "source_tool_results_preserved": len(tool_events),
            "summary": "No summary generated; measures identity plus the two most recent completed exchanges.",
            "tokenizer": "cl100k_base" if budgets._encoding() else "conservative character fallback",
            "safety_margin": budgets.TOKEN_SAFETY_MARGIN,
            "fixture": "100 identical-shape synthetic invoice exchanges; one 60-line tool result per exchange. "
                       "Upstream applies its native 500-character tool-result truncation. Static system instructions excluded."}


def concurrency_measurements(directory):
    # No model execution: advance a deterministic clock and complete each claimed
    # job exactly one simulated second later. Claim decisions are production SQL.
    now = [0.0]
    registry = AgentRegistry(directory / "queue.sqlite")
    queue = ExecutionService(registry, clock=lambda: now[0],
                             settings=SimpleNamespace(agent_max_concurrency=4))
    for i in range(24):
        agent = registry.create_agent(f"Independent task {i}", f"Synthetic responsibility {i}")
        batch = queue.create_batch(f"independent-request-{i}")
        job = immediate(queue.submit(agent["id"], "Complete synthetic work", batch_id=batch["id"], request_id=f"job-{i}"))
        assert job["status"] == "queued"
        immediate(queue.seal_batch(batch["id"]))
    points, completions, finished = [], {}, 0
    for tick in range(7):
        now[0] = float(tick)
        for job_id, end in list(completions.items()):
            if end <= now[0]:
                with registry.connect(write=True) as conn:
                    conn.execute("UPDATE execution_jobs SET status='succeeded',completed_at=? WHERE id=?", (now[0], job_id))
                del completions[job_id]
                finished += 1
        while (job := queue._claim_next()) is not None:
            completions[job["id"]] = now[0] + 1.0
        with registry.connect() as conn:
            counts = dict(conn.execute("SELECT status,COUNT(*) FROM execution_jobs GROUP BY status"))
        assert counts.get("running", 0) <= 4
        assert sum(counts.values()) == 24
        points.append({"simulated_seconds": tick, "upstream_running": 24 if tick == 0 else 0,
                       "new_running": counts.get("running", 0), "new_waiting": counts.get("queued", 0),
                       "new_completed": finished})
    assert finished == 24 and not completions
    baseline = original("server/agents/execution_agent/batch_manager.py")
    return {"points": points, "jobs": 24, "job_duration_seconds": 1, "max_concurrency": 4,
            "upstream_method": "Source-derived: each caller immediately awaits its own runtime; no global admission limit.",
            "new_method": "Production submit() and _claim_next(); deterministic simulated completions; scheduler/model/network excluded.",
            "baseline_source_sha256": hashlib.sha256(baseline.encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retrieval-report", type=Path, default=ROOT / "server/data/agent_evaluation_report.json")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    if args.retrieval_report.exists():
        raw = args.retrieval_report.read_bytes()
        report = json.loads(raw)
        evidence = {"source": "server/data/agent_evaluation_report.json", "sha256": hashlib.sha256(raw).hexdigest(),
                    "mode": report["evaluation"]["mode"], "benchmarks": report["benchmarks"]}
    else:
        evidence = json.loads(OUTPUT.read_text(encoding="utf-8"))["retrieval_benchmark"]
        report = evidence
    with tempfile.TemporaryDirectory(prefix="openpoke-charts-") as temp:
        directory = Path(temp)
        data = {"baseline_commit": BASELINE, "retrieval_benchmark": evidence,
                "roster": roster_measurements(report), "history": history_measurements(directory),
                "concurrency": concurrency_measurements(directory)}
    paths = ["server/services/execution/history.py", "server/services/execution/queue.py",
             "server/services/execution/budgets.py", "tests/evaluation/measure_overload.py"]
    data["source_sha256"] = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "roster": data["roster"],
                      "history": data["history"]["points"][-1], "concurrency_peak": 4, "completed": 24}, indent=2))


if __name__ == "__main__":
    main()
