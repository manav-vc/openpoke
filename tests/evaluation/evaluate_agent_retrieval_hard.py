"""Dense near-collision retrieval ablation and real production IA/EA routing.

No --live: validate authored fixture, without model calls. --live runs hosted
embeddings and four production workflow turns against 288 similar saved agents.
All runtime imports and state are isolated in a temporary source snapshot.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "tests/fixtures/agent_retrieval_hard_cases.json"


def build_fixture(path=FIXTURE):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    agents, queries = [], []
    for contact_index, (person, company, email) in enumerate(config["contacts"]):
        for service_index, (service, paraphrase) in enumerate(config["services"]):
            for period_index, (month, season) in enumerate(config["periods"]):
                key = f"contact-{contact_index}-service-{service_index}-period-{period_index}"
                invoice = f"INV-{1000 + 100 * contact_index + 2 * service_index + period_index}"
                amount = 500 + 100 * contact_index + 2 * service_index + period_index
                archived = key in config["archived_keys"]
                fact = f"Invoice {invoice}: {person}, {company}, {month} {service}. Amount CAD {amount}."
                agents.append(dict(key=key, name=f"{person} {company} invoice {invoice}",
                                   purpose=f"Reconcile {month} {service} invoice {invoice} with {person} at {company}",
                                   email=email, invoice_id=invoice, thread_id=f"billing-{company.casefold()}",
                                   fact=fact, archived=archived,
                                   changed=key in config["changed_keys"]))
                # One semantic target per contact/service pair, balanced across periods.
                if contact_index < 4 and service_index < 4 and period_index == contact_index % 2:
                    queries.append(dict(id=f"paraphrase-{key}", category="paraphrase",
                                        query=f"Find {person.split()[0]}'s {season} {company} bill concerning {paraphrase}.",
                                        expected_keys=[key]))
                if service_index == contact_index % 12 and period_index == 0:
                    queries.append(dict(id=f"exact-{key}", category="exact_identity",
                                        query=f"Resume {person}'s invoice {invoice}",
                                        references=[dict(kind="invoice_id", value=invoice)], expected_keys=[key]))
                if archived:
                    queries.append(dict(id=f"archived-{key}", category="archived_paraphrase",
                                        query=f"Find the old completed {season} task with {person} at {company} for {paraphrase}.",
                                        include_archived=True, expected_keys=[key]))
                if key in config["changed_keys"]:
                    queries.append(dict(id=f"changed-{key}", category="changed_instructions",
                                        query=f"Find {person}'s {month} {service} invoice where the correction put collection on hold.",
                                        expected_keys=[key]))
    # Each contact has 24 invoices sharing a thread, exercising explicit
    # ambiguity below the 30-candidate overflow shortcut.
    for contact_index in range(2):
        person, company, _ = config["contacts"][contact_index]
        queries.append(dict(id=f"ambiguous-{contact_index}", category="shared_thread_ambiguity",
                            query=f"Continue {person}'s invoice", references=[dict(kind="thread_id", value=f"billing-{company.casefold()}")],
                            expected_keys=[], requires_clarification=True))
    return dict(version=config["version"], agents=agents, queries=queries,
                cases=config["workflow_cases"], contacts=len(config["contacts"]),
                invoices_per_contact=len(config["services"]) * len(config["periods"]))


def score_ranking(case, keys, clarification):
    expected = set(case["expected_keys"])
    return dict(case_id=case["id"], category=case["category"], expected_keys=sorted(expected),
                ranked_keys=keys, hits_at_10=len(expected & set(keys[:10])),
                hits_at_30=len(expected & set(keys[:30])), expected_count=len(expected),
                requires_clarification=clarification,
                identity_conflict_violations=len(set(keys) - expected)
                    if case["category"] == "exact_identity" else 0,
                ambiguity_correct=(clarification == case["requires_clarification"])
                    if "requires_clarification" in case else None)


def ranking_summary(rows):
    def group(values):
        denominator = sum(r["expected_count"] for r in values)
        return dict(cases=len(values), expected_targets=denominator,
                    recall_at_10=sum(r["hits_at_10"] for r in values) / denominator if denominator else None,
                    recall_at_30=sum(r["hits_at_30"] for r in values) / denominator if denominator else None)
    return dict(**group(rows), by_category={category: group([r for r in rows if r["category"] == category])
                                          for category in sorted({r["category"] for r in rows})},
                ambiguity_checks=sum(r["ambiguity_correct"] is not None for r in rows),
                ambiguity_failures=sum(r["ambiguity_correct"] is False for r in rows),
                identity_conflict_violations=sum(r["identity_conflict_violations"] for r in rows))


def score_hard_turn(expected, row):
    """Extend ordinary workflow checks with exact normalized numeric facts.

    Comma thousands separators are formatting, whereas 1180 and 11800 are
    different values. Require bounded numeric tokens rather than substrings.
    """
    row = {**row, "checks": dict(row["checks"])}
    if expected.get("reply_amounts") or expected.get("forbidden_reply_amounts"):
        row["checks"]["required_reply_facts"] = all(
            term.casefold() in row["reply"].casefold() for term in expected.get("reply_terms", []))
        amounts = {float(value.replace(",", "")) for value in
                   re.findall(r"(?<![\w.])\d+(?:,\d{3})*(?:\.\d+)?(?!\w|\.\d)", row["reply"])}
        row["checks"]["required_numeric_facts"] = set(expected.get("reply_amounts", [])) <= amounts
        row["checks"]["no_superseded_amount"] = not set(expected.get("forbidden_reply_amounts", [])) & amounts
        row["normalized_reply_amounts"] = sorted(amounts)
    row["passed"] = all(row["checks"].values())
    return row


def export_rescored(raw_path, output):
    """Preserve original outcomes beside disclosed numeric-only rescoring."""
    from tests.evaluation.evaluate_agent_workflows import summarize, write_json
    raw = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    fixture = build_fixture()
    expected = {(case["id"], index): turn for case in fixture["cases"]
                for index, turn in enumerate(case["turns"])}
    turns = []
    for original in raw["turns"]:
        label = expected[(original["case_id"], original["turn"])]
        if original["user_text"] != label["text"]:
            raise ValueError("Rescoring requires identical evaluated user requests")
        row = score_hard_turn(label, original)
        turns.append({key: value for key, value in row.items()
                      if key not in {"source_events", "interaction_operations"}} |
                     dict(original_scoring=dict(passed=original["passed"], checks=original["checks"])))
    result = {key: value for key, value in raw.items() if key not in {"turns", "workflow_summary"}}
    journal = Path(raw_path).with_suffix(".calls.jsonl")
    result.update(turns=turns, workflow_summary=summarize(turns, 4),
                  original_workflow_summary=raw["workflow_summary"],
                  evidence_export=dict(
                      raw_report_sha256=hashlib.sha256(Path(raw_path).read_bytes()).hexdigest(),
                      call_journal_sha256=hashlib.sha256(journal.read_bytes()).hexdigest(),
                      scoring_fixture_sha256=hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                      rescoring="Exact numeric tokens accept thousands separators: CAD 1,180 equals CAD 1180. "
                                "Required corrected amount and forbidden superseded amount are checked separately. "
                                "Original checks and 3/4 outcome are retained. No model response or action was changed.",
                      new_model_calls=0, omitted_fields=["source_events", "interaction_operations"]))
    write_json(output, result)
    return result


async def evaluate(args, fixture, provider, report):
    from server.config import get_settings
    from server.services.execution.registry import AgentRegistry
    from server.services.execution.retrieval import SearchService
    from tests.evaluation.evaluate_agent_workflows import seed_registry, run_case, summarize, write_json
    from tests.evaluation.live_provider import LiveEmbedder

    class CachedEmbedder(LiveEmbedder):
        def __init__(self, *values):
            super().__init__(*values)
            self.cache = {}

        async def embed(self, texts):
            missing = list(dict.fromkeys(t for t in texts if t not in self.cache))
            if missing:
                vectors = await super().embed(missing)
                self.cache.update(zip(missing, vectors))
            return [self.cache[t] for t in texts]

    class AblationSearch(SearchService):
        def __init__(self, *values, mode):
            super().__init__(*values)
            self.mode = mode

        def _keyword(self, query, eligible):
            return [] if self.mode == "semantic_only" else super()._keyword(query, eligible)

        async def _semantic(self, query, eligible):
            return ([], []) if self.mode == "keyword_only" else await super()._semantic(query, eligible)

    settings = get_settings()
    with tempfile.TemporaryDirectory(prefix="hard-retrieval-state-") as temporary:
        directory = Path(temporary)
        registry = AgentRegistry(directory / "template.db")
        ids = seed_registry(registry, fixture, 0)
        reverse = {value: key for key, value in ids.items()}
        for item in fixture["agents"]:
            if item["changed"]:
                correction = ("Correction: the previous amount is superseded. Corrected amount CAD 1180. "
                              "Collection is on hold for compliance review. No sending is approved.")
                registry.append_event("tool_result", dict(tool="task_email_search", success=True,
                                                          result=[dict(id="correction-" + item["key"],
                                                                       clean_text=correction)]), agent_id=ids[item["key"]])
                registry.update_agent(ids[item["key"]], summary=correction,
                                      purpose=item["purpose"] + "; corrected: collection on hold, compliance review")
        embedder = CachedEmbedder(provider, settings)
        production = SearchService(registry, embedder, settings)
        provider.case_id = "dense-seed-index"
        while await production.index_pending():
            pass
        with registry.connect() as conn:
            backlog = conn.execute("SELECT COUNT(*) FROM index_outbox").fetchone()[0]
        report["index_backlog"] = backlog
        if backlog:
            raise RuntimeError("Incomplete live indexing; refusing to score degraded retrieval")
        for mode in ("hybrid", "keyword_only", "semantic_only"):
            service = AblationSearch(registry, embedder, settings, mode=mode)
            rows = []
            for case in fixture["queries"]:
                provider.case_id = mode + ":" + case["id"]
                found = await service.search(case["query"], case.get("references", []),
                                             include_archived=case.get("include_archived", False))
                if found["degraded"]:
                    raise RuntimeError("Degraded search: " + str(found["degradation_reasons"]))
                rows.append(score_ranking(case, [reverse[r["id"]] for r in found["candidates"]],
                                          found["requires_clarification"]))
            report["ablation"][mode] = dict(summary=ranking_summary(rows), cases=rows)
            write_json(args.output, report)
        # These execute production interaction/execution loops and queue delivery;
        # no selector model or fixture label participates in choosing an owner.
        for case in fixture["cases"]:
            rows = await run_case(case, fixture, registry, ids, directory / case["id"],
                                  provider, settings, 150)
            for row, expected in zip(rows, case["turns"]):
                report["turns"].append(score_hard_turn(expected, row))
            report["workflow_summary"] = summarize(report["turns"], 4)
            write_json(args.output, report)


def worker(args):
    from server.config import get_settings
    from tests.evaluation.live_provider import LiveProvider, UsageLedger
    from tests.evaluation.evaluate_agent_workflows import write_json, summarize
    settings = get_settings()
    fixture = build_fixture()
    ledger = UsageLedger(max_calls=100, max_tokens=350000, max_cost_usd=args.max_cost_usd)
    provider = LiveProvider(settings.openrouter_api_key, ledger, args.output.with_suffix(".calls.jsonl"))
    report = dict(schema_version=1, started_at=datetime.now(timezone.utc).isoformat(),
                  mode="live_embeddings_and_production_IA_EA_synthetic_mailbox", status="starting",
                  fixture_version=fixture["version"], fixture_sha256=hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                  models=dict(chat=settings.interaction_agent_model, embedding=settings.agent_embedding_model),
                  corpus=dict(agents=len(fixture["agents"]), contacts=fixture["contacts"],
                              invoices_per_contact=fixture["invoices_per_contact"],
                              unrelated_noise_agents=0, changed_instructions=2, archived_agents=4),
                  ablation={}, turns=[], limitations=[
                      "Small authored dense invoice corpus; not an independent or production benchmark.",
                      "Ablations share the same production identity guard and embeddings; only ranking channels differ.",
                      "Exact identifiers bypass embeddings in every mode; category metrics disclose this separately.",
                      "Workflow checks use production IA/EA, queue, and synthetic email tools; four turns only.",
                      "Candidate recall is separate from final owner selection. No real Gmail or nested search model.",
                      "One execution per turn; no statistical reliability claim."])
    try:
        report["catalog_pricing"] = provider.preflight(settings.interaction_agent_model, settings.agent_embedding_model)
        report["status"] = "running"
        write_json(args.output, report)
        asyncio.run(evaluate(args, fixture, provider, report))
        report["status"] = "completed"
    except Exception as exc:
        report["status"] = "incomplete" if ledger.calls else "blocked"
        report["error"] = type(exc).__name__ + ": " + str(exc).replace(provider.api_key or "NO_KEY", "[redacted]")
    report["usage"] = ledger.summary()
    report["workflow_summary"] = summarize(report["turns"], 4)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(args.output, report)
    print(json.dumps({k: report.get(k) for k in ("status", "workflow_summary", "usage", "error")}, indent=2), flush=True)
    return 0 if report["status"] == "completed" and report["workflow_summary"]["all_passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--max-cost-usd", type=float, default=1.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rescore", type=Path, help="Export numeric-format rescoring of an existing run; no model calls")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    fixture = build_fixture()
    if args.rescore:
        if not args.output or args.output.exists():
            parser.error("Rescoring requires a fresh --output path")
        result = export_rescored(args.rescore, args.output)
        print(json.dumps(dict(workflow_summary=result["workflow_summary"],
                              original_workflow_summary=result["original_workflow_summary"],
                              billable_calls=0), indent=2))
        return 0 if result["workflow_summary"]["all_passed"] else 1
    if not args.live:
        print(json.dumps(dict(mode="fixture_validation_only", agents=len(fixture["agents"]),
                              queries=len(fixture["queries"]), workflow_turns=4, billable_calls=0), indent=2))
        return 0
    if args.worker:
        return worker(args)
    from tests.evaluation.evaluate_agent_workflows import snapshot, evaluation_environment
    output = (args.output or ROOT / "server/data/evaluations" /
              ("hard-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")) / "report.json").resolve()
    if output.exists() or output.with_suffix(".calls.jsonl").exists():
        parser.error("Choose a fresh output path; existing evidence is never overwritten")
    if not 0 < args.max_cost_usd < float("inf"):
        parser.error("The cost limit must be finite and positive")
    env = evaluation_environment(args.model)
    print(f"Evaluation report: {output}", flush=True)
    with tempfile.TemporaryDirectory(prefix="hard-retrieval-code-") as temporary:
        destination = Path(temporary)
        snapshot(destination)
        shutil.copy2(FIXTURE, destination / "tests/fixtures" / FIXTURE.name)
        env["OPENPOKE_AGENT_REGISTRY_PATH"] = str(destination / "server/data/agents/registry.db")
        return subprocess.run([sys.executable, str(destination / "tests/evaluation" / Path(__file__).name),
                               "--live", "--worker", "--output", str(output),
                               "--max-cost-usd", str(args.max_cost_usd)],
                              cwd=destination, env=env, timeout=900).returncode


if __name__ == "__main__":
    raise SystemExit(main())
