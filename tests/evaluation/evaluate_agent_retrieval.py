"""Offline discovery evaluation, optional live embeddings, and local benchmarks.

Run from the repository root:
    python tests/evaluation/evaluate_agent_retrieval.py --benchmark
    python tests/evaluation/evaluate_agent_retrieval.py --live

All data is synthetic and stored in a temporary directory. --live explicitly
opts into billable OpenRouter embedding requests; it never accesses Gmail.
Offline semantic vectors exercise routing/index logic, not hosted-model quality.
Neither mode claims end-to-end interaction-agent reuse accuracy.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import re
import statistics
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.config import Settings
from server.services.execution.budgets import estimate_tokens
from server.services.execution.registry import AgentRegistry
from server.services.execution.retrieval import OpenRouterEmbedder, SearchService

DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "agent_retrieval_cases.json"
SYNONYMS = {
    **dict.fromkeys("invoice invoices payment payments receivable unpaid overdue settlement settle balance bill bills outstanding".split(), "invoice"),
    **dict.fromkeys("contract contracts agreement agreements renew renewal negotiating".split(), "contract"),
    **dict.fromkeys("meeting appointment appointments arrange schedule planning".split(), "meeting"),
    **dict.fromkeys("travel flight flights itinerary booking book conference".split(), "travel"),
    **dict.fromkeys("hiring recruit recruiting candidates candidate engineering".split(), "hiring"),
    **dict.fromkeys("bug defect authentication login issue".split(), "bug"),
    **dict.fromkeys("release rollout ship application".split(), "release"),
    **dict.fromkeys("research investigation investigate market opportunity".split(), "research"),
    **dict.fromkeys("refund chargeback customer".split(), "refund"),
    **dict.fromkeys("expense receipts reconcile".split(), "expense"),
    **dict.fromkeys("reminder remind alert deadline filing".split(), "reminder"),
    "sarahs": "sarah", "sams": "sam", "priyas": "priya", "alexs": "alex",
}
STOP_WORDS = set("a an and are at about ask continue draft find follow for from get in is it of on prepare resume review saved task the this to up with finish resolve collect old archived service supplier look into outstanding several".split())
COMPANIES = {"acme", "beta", "delta", "atlas", "cedar", "orbit"}


class OfflineSemanticEmbedder:
    """Deterministic synonym-aware feature hashing, independent of case labels."""

    def __init__(self, dimensions=1536):
        self.dimensions = dimensions

    def vector(self, text):
        vector = np.zeros(self.dimensions, dtype=np.float32)
        words = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.lower())
        counts = Counter(SYNONYMS.get(word, word) for word in words if word not in STOP_WORDS)
        for word, count in counts.items():
            digest = hashlib.sha256(word.encode()).digest()
            index = int.from_bytes(digest[:8], "little") % self.dimensions
            weight = 3 if word in COMPANIES else 2 if word in set(SYNONYMS.values()) else 1
            vector[index] += weight * min(count, 2)
        if not vector.any():
            vector[0] = 1
        return (vector / np.linalg.norm(vector)).tolist()

    async def embed(self, texts):
        return [self.vector(text) for text in texts]


def seed_records(registry, fixture, noise_count=200):
    ids = {}
    for item in fixture["agents"]:
        record = registry.create_agent(item["name"], item["purpose"])
        event_id = registry.append_event("tool_result", {"references": item["references"]}, agent_id=record["id"])
        for reference in item["references"]:
            registry.add_reference(record["id"], reference["kind"], reference["value"], confirmed=True, source_event_id=event_id)
        if item.get("archived"):
            registry.update_agent(record["id"], state="completed", completed_at=datetime.now(timezone.utc).isoformat(), archived=True)
        ids[item["key"]] = record["id"]
    now = datetime.now(timezone.utc).isoformat()
    with registry.connect(write=True) as conn:
        conn.executemany(
            "INSERT INTO agents(id,name,purpose,last_activity,created_at) VALUES(?,?,?,?,?)",
            [(f"noise-{index:05}", f"Resource inventory project {index}", "Maintain unrelated inventory records", now, now) for index in range(noise_count)],
        )
        conn.execute("INSERT INTO agent_fts(agent_id,name,purpose,summary,reference_text) SELECT id,name,purpose,summary,'' FROM agents WHERE id LIKE 'noise-%'")
        conn.execute("INSERT INTO index_outbox(agent_id,search_version) SELECT id,search_version FROM agents WHERE id LIKE 'noise-%'")
    return ids


async def evaluate(fixture_path=DEFAULT_FIXTURE, *, live=False, noise_count=200):
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    settings = Settings(agent_hybrid_search_enabled=True)
    if live and not settings.openrouter_api_key:
        raise ValueError("--live requires OPENROUTER_API_KEY")
    with tempfile.TemporaryDirectory(prefix="openpoke-evaluation-") as directory:
        registry = AgentRegistry(Path(directory) / "registry.db")
        ids = seed_records(registry, fixture, noise_count)
        embedder = OpenRouterEmbedder(settings) if live else OfflineSemanticEmbedder(settings.agent_embedding_dimensions)
        service = SearchService(registry, embedder, settings)
        indexed = 0
        while True:
            count = await service.index_pending()
            indexed += count
            if not count:
                break
        hits, expected_count, conflict_violations, ambiguous_failures = 0, 0, 0, 0
        results = []
        for case in fixture["cases"]:
            result = await service.search(case["query"], case.get("references", []))
            actual = {record["id"] for record in result["candidates"]}
            expected = {ids[key] for key in case.get("expected_keys", [])}
            forbidden = {ids[key] for key in case.get("forbidden_keys", [])}
            hits += len(actual & expected)
            expected_count += len(expected)
            conflict_violations += len(actual & forbidden)
            if case["expected_action"] == "clarify" and not result["requires_clarification"]:
                ambiguous_failures += 1
            results.append({
                "case_id": case["id"], "category": case["category"],
                "expected_action": case["expected_action"], "expected_candidate_ids": sorted(expected),
                "retrieved_candidate_ids": sorted(actual), "all_expected_retrieved": expected <= actual,
                "requires_clarification": result["requires_clarification"], "degraded": result["degraded"],
            })
        with registry.connect() as conn:
            index_pending = conn.execute("SELECT COUNT(*) FROM index_outbox").fetchone()[0]
        recall = hits / expected_count if expected_count else 1
        return {
            "mode": "live_openrouter_embeddings" if live else "offline_test_double",
            "embedding_model": settings.agent_embedding_model if live else "synonym_feature_hashing",
            "case_count": len(results), "indexed_records": indexed, "index_backlog": index_pending,
            "recall_at_30": round(recall, 4), "identity_conflict_violations": conflict_violations,
            "ambiguity_guard_failures": ambiguous_failures,
            "retrieval_gates_pass": recall >= .95 and conflict_violations == 0 and ambiguous_failures == 0 and index_pending == 0,
            "end_to_end_wrong_reuse": None,
            "limitation": "Measures candidate retrieval and identity guards. Interaction-model selection and external actions are not exercised; offline vectors do not establish hosted-model semantic quality.",
            "cases": results,
        }


async def benchmark(count, repeats=12):
    settings = Settings(agent_hybrid_search_enabled=True)
    embedder = OfflineSemanticEmbedder(settings.agent_embedding_dimensions)
    with tempfile.TemporaryDirectory(prefix="openpoke-benchmark-") as directory:
        registry = AgentRegistry(Path(directory) / "registry.db")
        now = datetime.now(timezone.utc).isoformat()
        records = [(f"agent-{i:06}", f"{['Acme','Beta','Delta','Atlas','Cedar','Orbit'][i % 6]} invoice {i}", f"Follow up on invoice INV-{i:06}", now, now) for i in range(count)]
        with registry.connect(write=True) as conn:
            conn.executemany("INSERT INTO agents(id,name,purpose,last_activity,created_at) VALUES(?,?,?,?,?)", records)
            conn.executemany("INSERT INTO embeddings VALUES(?,?,?,?,?,?)", [
                (key, settings.agent_embedding_model, settings.agent_embedding_dimensions, 1, np.asarray(embedder.vector(name + ' ' + purpose), dtype=np.float32).tobytes(), now)
                for key, name, purpose, _, _ in records
            ])
            conn.execute("INSERT INTO agent_fts(agent_id,name,purpose,summary,reference_text) SELECT id,name,purpose,summary,'' FROM agents")
        service = SearchService(registry, embedder, settings)
        await service.search("Acme unpaid bill")
        timings, sizes, pools = [], [], []
        for _ in range(repeats):
            started = time.perf_counter()
            result = await service.search("Acme unpaid bill")
            timings.append((time.perf_counter() - started) * 1000)
            sizes.append(estimate_tokens(json.dumps(result["candidates"][:10])))
            pools.append(len(result["candidates"]))
        timings.sort()
        return {
            "agents": count, "dimensions": settings.agent_embedding_dimensions, "repeats": repeats,
            "p50_ms": round(statistics.median(timings), 2),
            "p95_ms": round(timings[min(len(timings)-1, int(len(timings)*.95))], 2),
            "max_candidates": max(pools), "max_first_page_estimated_tokens": max(sizes),
            "vector_matrix_mib": round(count * settings.agent_embedding_dimensions * 4 / 1024**2, 2),
            "provider_latency_included": False, "provider_usage": 0,
        }


async def main_async(args):
    report = {"platform": platform.platform(), "python": platform.python_version()}
    if not args.skip_evaluation:
        report["evaluation"] = await evaluate(args.fixture, live=args.live)
    if args.benchmark:
        report["benchmarks"] = [await benchmark(count) for count in (1000, 10000)]
    text = json.dumps(report, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        concise = {**report, "case_results_saved_to": str(output)}
        if "evaluation" in concise:
            concise["evaluation"] = {key: value for key, value in concise["evaluation"].items() if key != "cases"}
        print(json.dumps(concise, indent=2))
    else:
        print(text)
    return 1 if report.get("evaluation", {}).get("retrieval_gates_pass") is False else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly enable billable OpenRouter embeddings for evaluation only")
    parser.add_argument("--benchmark", action="store_true", help="Run synthetic local 1k/10k retrieval benchmarks without network")
    parser.add_argument("--skip-evaluation", action="store_true")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.live and args.skip_evaluation:
        parser.error("--live is only relevant to evaluation")
    try:
        return asyncio.run(main_async(args))
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
