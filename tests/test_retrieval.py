"""Check hybrid retrieval, identity matching, embedding failures, and candidate limits."""
import asyncio
import json
from datetime import datetime, timezone

import httpx
import numpy as np
import pytest

from server.config import Settings
from server.services.execution.budgets import estimate_tokens
from server.services.execution.registry import AgentRegistry
from server.services.execution.retrieval import OpenRouterEmbedder, SearchService, reference_evidence


class FakeEmbedder:
    def __init__(self, vector=None, error=None):
        self.vector = vector or [1, 0, 0]
        self.error = error
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        if self.error:
            raise self.error
        return [self.vector for _ in texts]


def settings(**overrides):
    return Settings(**{"agent_embedding_dimensions": 3, **overrides})


def add_reference(registry, record, kind, value, scope=""):
    event_id = registry.append_event("tool_result", {kind: value}, agent_id=record["id"])
    registry.add_reference(record["id"], kind, value, scope, True, event_id)
    return registry.get(record["id"])


def save_vector(registry, record, vector, search_version=None):
    vector = np.asarray(vector, dtype=np.float32)
    vector /= np.linalg.norm(vector)
    with registry.connect(write=True) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?,?,?)",
            (record["id"], "openai/text-embedding-3-small", len(vector), search_version or record["search_version"], vector.tobytes(), datetime.now(timezone.utc).isoformat()),
        )


@pytest.fixture
def registry(tmp_path):
    return AgentRegistry(tmp_path / "registry.db")


def test_exact_identity_overrides_semantics_and_resolves_archives(registry):
    correct = add_reference(registry, registry.create_agent("Acme payment", "Outstanding invoice"), "invoice_id", "INV-1042")
    wrong = add_reference(registry, registry.create_agent("Sarah invoice follow-up", "Sarah unpaid invoice"), "invoice_id", "INV-1088")
    registry.update_agent(correct["id"], archived=True)
    service = SearchService(registry, FakeEmbedder(error=AssertionError("Exact routing must bypass embedding")), settings())
    result = asyncio.run(service.search("Follow up with Sarah", [{"kind": "invoice_id", "value": "INV-1042"}]))
    assert [candidate["id"] for candidate in result["candidates"]] == [correct["id"]]
    assert result["candidates"][0]["archived"] is True
    assert not result["degraded"]
    assert wrong["id"] not in [candidate["id"] for candidate in result["candidates"]]


def test_observed_reference_is_not_trusted_identity(registry):
    record = registry.create_agent("Search results", "Found several invoices", [{"kind": "invoice_id", "value": "INV-1088", "confirmed": True}])
    record = registry.get(record["id"])
    matches, conflicts = reference_evidence(record, [{"kind": "invoice_id", "value": "INV-1042"}])
    assert matches == conflicts == []


def test_distinct_explicit_invoices_keep_both_tasks_without_false_ambiguity(registry):
    records = []
    for invoice in ("INV-1042", "INV-1088"):
        record = add_reference(registry, registry.create_agent("Sarah Acme invoice", "Review payment"), "invoice_id", invoice)
        record = add_reference(registry, record, "thread_id", "shared-billing-thread")
        records.append(record)
    references = [{"kind": "invoice_id", "value": "INV-1042"}, {"kind": "invoice_id", "value": "INV-1088"}, {"kind": "thread_id", "value": "shared-billing-thread"}]
    result = asyncio.run(SearchService(registry, FakeEmbedder(), settings(agent_hybrid_search_enabled=False)).search("Compare INV-1042 and INV-1088", references))
    assert {candidate["id"] for candidate in result["candidates"]} == {record["id"] for record in records}
    assert not result["requires_clarification"]
    assert not result["ambiguous_identifiers"]
    assert not result["unresolved_identifiers"]


def test_one_identifier_shared_by_two_tasks_still_requires_clarification(registry):
    for index in range(2):
        record = registry.create_agent(f"Invoice activity {index}", "Separate invoice objective")
        add_reference(registry, record, "invoice_id", "INV-1042")
    result = asyncio.run(SearchService(registry, FakeEmbedder(), settings(agent_hybrid_search_enabled=False)).search("Continue INV-1042", [{"kind": "invoice_id", "value": "INV-1042"}]))
    assert len(result["candidates"]) == 2
    assert result["requires_clarification"]
    assert result["ambiguous_identifiers"][0]["value"] == "INV-1042"


def test_weak_model_added_alternative_cannot_relax_explicit_identity(registry):
    record = add_reference(registry, registry.create_agent("Invoice", "Payment"), "invoice_id", "INV-1088")
    matches, conflicts = reference_evidence(record, [{"kind": "invoice_id", "value": "INV-1042", "trusted": True}, {"kind": "invoice_id", "value": "INV-1088", "trusted": False}])
    assert matches
    assert conflicts


def test_hybrid_finds_paraphrase_and_keyword_candidates(registry):
    semantic = registry.create_agent("Acme receivable", "Collect outstanding payment")
    keyword = registry.create_agent("Sarah invoice", "Review invoice INV-1088")
    save_vector(registry, semantic, [1, 0, 0])
    save_vector(registry, keyword, [0, 1, 0])
    result = asyncio.run(SearchService(registry, FakeEmbedder(), settings()).search("unpaid invoice"))
    assert {candidate["id"] for candidate in result["candidates"]} == {semantic["id"], keyword["id"]}


def test_provider_failure_keeps_keyword_results_and_reports_degradation(registry):
    record = registry.create_agent("Acme invoice", "Draft payment follow-up")
    save_vector(registry, record, [1, 0, 0])
    result = asyncio.run(SearchService(registry, FakeEmbedder(error=TimeoutError("provider timeout")), settings()).search("invoice"))
    assert result["candidates"][0]["id"] == record["id"]
    assert result["degraded"]
    assert "Semantic discovery is unavailable" in result["degradation_reasons"]


def test_missing_index_is_not_reported_as_clean_no_match(registry):
    registry.create_agent("Acme receivable", "Collect payment")
    result = asyncio.run(SearchService(registry, FakeEmbedder(), settings()).search("unrelated paraphrase"))
    assert result["candidates"] == []
    assert result["degraded"]


def test_changed_content_excludes_stale_vector_and_updates_keyword_immediately(registry):
    record = registry.create_agent("Aqueduct repair", "Maintain water pipes")
    save_vector(registry, record, [1, 0, 0])
    registry.update_agent(record["id"], purpose="Prepare Acme invoice")
    embedder = FakeEmbedder(error=AssertionError("No current vector should be queried"))
    result = asyncio.run(SearchService(registry, embedder, settings()).search("invoice"))
    assert result["candidates"][0]["id"] == record["id"]
    assert result["degraded"]
    assert embedder.calls == 0


def test_account_scope_applies_before_ranking_and_same_contact_is_not_unique(registry):
    for index in range(35):
        record = registry.create_agent(f"invoice account A {index}", "invoice invoice invoice")
        add_reference(registry, record, "email", "sarah@example.test", "account-a")
        save_vector(registry, registry.get(record["id"]), [1, 0, 0])
    desired = add_reference(registry, registry.create_agent("invoice B", "invoice"), "email", "sarah@example.test", "account-b")
    save_vector(registry, desired, [1, 0, 0])
    result = asyncio.run(SearchService(registry, FakeEmbedder(), settings()).search("invoice", scope="account-b"))
    assert [candidate["id"] for candidate in result["candidates"]] == [desired["id"]]
    matches, conflicts = reference_evidence(desired, [{"kind": "email", "value": "SARAH@example.test"}])
    assert len(matches) == 1 and not conflicts


def test_too_many_exact_matches_requires_narrowing(registry):
    for index in range(4):
        record = registry.create_agent(f"Invoice task {index}", "Separate objective")
        add_reference(registry, record, "thread_id", "shared-thread")
    result = asyncio.run(SearchService(registry, FakeEmbedder(), settings(agent_candidate_pool=3)).search("invoice", [{"kind": "thread_id", "value": "shared-thread"}]))
    assert result["requires_clarification"]
    assert result["exact_match_count"] == 4
    assert result["candidates"] == []


def test_durable_indexing_normalizes_vectors_and_is_version_guarded(registry):
    record = registry.create_agent("Invoice", "Collect payment")
    service = SearchService(registry, FakeEmbedder([3, 4, 0]), settings())
    assert asyncio.run(service.index_pending()) == 1
    with registry.connect() as conn:
        row = conn.execute("SELECT * FROM embeddings WHERE agent_id=?", (record["id"],)).fetchone()
        vector = np.frombuffer(row["vector"], dtype=np.float32)
        assert np.linalg.norm(vector) == pytest.approx(1)
        assert conn.execute("SELECT COUNT(*) FROM index_outbox").fetchone()[0] == 0

    class UpdatingEmbedder:
        async def embed(self, texts):
            registry.update_agent(record["id"], summary="Changed while request was running")
            return [[1, 0, 0] for _ in texts]

    registry.update_agent(record["id"], summary="Needs indexing")
    assert asyncio.run(SearchService(registry, UpdatingEmbedder(), settings()).index_pending()) == 0
    with registry.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM index_outbox").fetchone()[0] == 1


def test_index_failure_preserves_retryable_outbox(registry):
    registry.create_agent("Invoice", "Collect payment")
    service = SearchService(registry, FakeEmbedder(error=RuntimeError("offline")), settings())
    assert asyncio.run(service.index_pending()) == 0
    with registry.connect() as conn:
        row = conn.execute("SELECT * FROM index_outbox").fetchone()
        assert row["attempts"] == 1
        assert row["last_error"] == "offline"
        assert row["available_at"]


def test_reset_during_embedding_cannot_repopulate_index(registry):
    registry.create_agent("Invoice", "Collect payment")

    class ResettingEmbedder:
        async def embed(self, texts):
            registry.reset()
            return [[1, 0, 0] for _ in texts]

    assert asyncio.run(SearchService(registry, ResettingEmbedder(), settings()).index_pending()) == 0
    with registry.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM index_outbox").fetchone()[0] == 0


def test_large_candidate_fields_are_bounded_without_changing_storage(registry):
    original = registry.create_agent("invoice " * 30, "Invoice details " * 200)
    registry.update_agent(original["id"], summary="Long summary " * 2000)
    result = asyncio.run(SearchService(registry, FakeEmbedder(), settings(agent_hybrid_search_enabled=False)).search("invoice"))
    assert len(result["candidates"]) == 1
    assert estimate_tokens(json.dumps(result["candidates"][0], default=str)) <= 200
    assert registry.get(original["id"])["purpose"] == original["purpose"]


def test_query_embeddings_are_cached_but_index_scope_is_fresh(registry):
    first = registry.create_agent("one", "one")
    save_vector(registry, first, [1, 0, 0])
    embedder = FakeEmbedder()
    service = SearchService(registry, embedder, settings())

    async def run():
        await service.search("outstanding payment")
        second = registry.create_agent("two", "two")
        save_vector(registry, second, [1, 0, 0])
        result = await service.search("outstanding payment")
        assert len(result["candidates"]) == 2

    asyncio.run(run())
    assert embedder.calls == 1


def test_openrouter_adapter_validates_indexes_dimensions_and_usage_without_network():
    async def response(request):
        payload = json.loads(request.content)
        assert payload["model"] == "openai/text-embedding-3-small"
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [0, 2, 0]}, {"index": 0, "embedding": [3, 0, 0]}], "usage": {"prompt_tokens": 4}})

    adapter = OpenRouterEmbedder(settings(openrouter_api_key="test-key"), transport=httpx.MockTransport(response))
    result = asyncio.run(adapter.embed(["one", "two"]))
    assert result == [[1, 0, 0], [0, 1, 0]]

    async def invalid(request):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0, 0, 0]}]})

    adapter.transport = httpx.MockTransport(invalid)
    with pytest.raises(ValueError):
        asyncio.run(adapter.embed(["one"]))


def test_ten_thousand_agents_have_a_bounded_candidate_pool(registry):
    now = datetime.now(timezone.utc).isoformat()
    with registry.connect(write=True) as conn:
        conn.executemany("INSERT INTO agents(id,name,purpose,last_activity,created_at) VALUES(?,?,?,?,?)", [(f"agent-{index:05}", f"Invoice task {index}", "Unpaid invoice follow-up", now, now) for index in range(10000)])
        conn.execute("INSERT INTO agent_fts(agent_id,name,purpose,summary,reference_text) SELECT id,name,purpose,summary,'' FROM agents")
    service = SearchService(registry, FakeEmbedder(), settings(agent_hybrid_search_enabled=False))
    result = asyncio.run(service.search("invoice"))
    assert len(result["candidates"]) == 30
    assert all(estimate_tokens(json.dumps(candidate, default=str)) <= 200 for candidate in result["candidates"])


def test_labeled_sixty_case_evaluation_passes_offline_with_explicit_limitations():
    from tests.evaluation.evaluate_agent_retrieval import evaluate

    report = asyncio.run(evaluate())
    assert report["case_count"] >= 60
    assert report["recall_at_30"] >= .95
    assert report["identity_conflict_violations"] == 0
    assert report["ambiguity_guard_failures"] == 0
    assert report["end_to_end_wrong_reuse"] is None
