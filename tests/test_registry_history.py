"""Check stable task identity, migration, archival, source history, and compaction."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from server.services.execution.registry import AgentRegistry, DuplicateAgentError, StaleAgentError, StaleGenerationError
from server.services.execution.history import HistoryService
from server.services.execution.budgets import estimate_tokens


@pytest.fixture
def registry(tmp_path):
    return AgentRegistry(tmp_path / "registry.db")


def test_stable_identity_and_atomic_idempotent_creation(registry):
    first = registry.create_agent("Acme invoice", "Follow up INV-1042", operation_key="create:one")
    again = registry.create_agent("Acme invoice", "Follow up INV-1042", operation_key="create:one")
    assert first["id"] == again["id"]
    renamed = registry.update_agent(first["id"], expected_version=1, name="Sarah payment")
    assert renamed["id"] == first["id"]
    with pytest.raises(StaleAgentError):
        registry.update_agent(first["id"], expected_version=1, summary="stale")


def test_model_references_untrusted_until_observed(registry):
    agent = registry.create_agent("Invoice", "Follow up", references=[{"kind": "invoice_id", "value": "INV-1042", "confirmed": True}])
    assert not agent["references"][0]["confirmed"]
    with pytest.raises(ValueError):
        registry.add_reference(agent["id"], "invoice_id", "INV-1042", confirmed=True, source_event_id=9999)
    event = registry.append_event("tool_result", {"result": "Invoice INV-1042"}, agent_id=agent["id"])
    registry.add_reference(agent["id"], "invoice_id", "INV-1042", confirmed=True, source_event_id=event)
    with pytest.raises(DuplicateAgentError) as error:
        registry.create_agent("Another name", "Same task", references=[{"kind": "invoice_id", "value": "INV-1042"}])
    assert error.value.candidate_ids == [agent["id"]]


def test_journal_dedup_generation_and_reset(registry):
    agent = registry.create_agent("Task", "Task purpose")
    generation = registry.generation
    first = registry.append_event("draft", {"body": "hello"}, agent_id=agent["id"], event_key="draft:one")
    assert first == registry.append_event("draft", {"body": "hello"}, agent_id=agent["id"], event_key="draft:one")
    registry.reset()
    with pytest.raises(StaleGenerationError):
        registry.append_event("late", {}, generation=generation)
    assert registry.eligible_agents(True) == []


def test_migration_preserves_files_and_flags_slug_collisions(registry, tmp_path):
    directory = tmp_path / "old"
    directory.mkdir()
    roster = directory / "roster.json"
    roster.write_text(json.dumps(["Sarah / invoice", "Sarah - invoice"]), encoding="utf-8")
    log = directory / "sarah-invoice.log"
    log.write_text("legacy data already truncated", encoding="utf-8")
    registry.migrate_legacy(roster, directory)
    registry.migrate_legacy(roster, directory)
    records = registry.eligible_agents(True)
    assert len(records) == 2
    assert all(r["history_ambiguous"] for r in records)
    assert log.read_text() == "legacy data already truncated"
    assert roster.exists()


def test_migration_does_not_duplicate_canonical_trigger_owners(registry, tmp_path):
    agent = registry.create_agent("Invoice", "Follow up")
    trigger_db = tmp_path / "triggers.db"
    with sqlite3.connect(trigger_db) as conn:
        conn.execute("CREATE TABLE triggers(agent_name TEXT)")
        conn.execute("INSERT INTO triggers VALUES(?)", (agent["id"],))
    registry.migrate_legacy(tmp_path / "absent.json", tmp_path, trigger_db)
    assert len(registry.eligible_agents(True)) == 1


def test_later_migration_marks_both_colliding_histories(registry, tmp_path):
    roster = tmp_path / "roster.json"
    (tmp_path / "sarah-invoice.log").write_text("old history")
    roster.write_text(json.dumps(["Sarah / invoice"]))
    registry.migrate_legacy(roster, tmp_path)
    roster.write_text(json.dumps(["Sarah / invoice", "Sarah - invoice"]))
    registry.migrate_legacy(roster, tmp_path)
    assert all(a["history_ambiguous"] for a in registry.eligible_agents(True))


def test_archive_only_explicitly_completed_without_pending_work(registry):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=40)).isoformat()
    eligible = registry.create_agent("Completed", "Goal done")
    protected = registry.create_agent("Reminder", "Await reminder")
    merely_successful = registry.create_agent("Open", "One invocation succeeded")
    for agent in (eligible, protected):
        registry.update_agent(agent["id"], state="completed", completed_at=old, last_activity=old)
    registry.add_reference(protected["id"], "enabled_trigger", "12")
    assert registry.archive_completed(now=now) == 1
    assert registry.get(eligible["id"])["archived"]
    assert not registry.get(protected["id"])["archived"]
    assert not registry.get(merely_successful["id"])["archived"]
    restored = registry.restore_agent(eligible["id"])
    assert restored["state"] == "open" and not restored["archived"]


def test_lossless_large_tool_artifact_and_bounded_source_read(registry):
    agent = registry.create_agent("Invoice", "Investigate invoice")
    history = HistoryService(registry)
    result = {"body": "background " * 20000, "exact_invoice": "INV-1042", "exact_amount": "1234.56"}
    event_id = history.record_tool(agent["id"], "run1", "email_search", {"query": "Acme"}, result, True, "tool:one", registry.generation)
    event = registry.get_event(event_id)
    assert "artifact_id" in event["payload"]
    full = history._payload(event)
    assert full["result"] == result
    read = history.read(agent["id"], event_ids=[event_id], max_tokens=300)
    assert read["events"][0]["event_id"] == event_id
    assert read["events"][0]["truncated"]
    assert read["estimated_tokens"] <= 300
    targeted = history.read(agent["id"], query="1234.56", max_tokens=300)
    assert "1234.56" in targeted["events"][0]["content"]
    continued = history.read(agent["id"], cursor=read["events"][0]["next_source_cursor"], max_tokens=300)
    assert continued["events"][0]["source_offset"] > 0


def test_history_task_identity_and_completed_segment_compaction(registry):
    async def scenario():
        agent = registry.create_agent("Invoice", "Collect payment")
        history = HistoryService(registry)
        for index in range(4):
            request = f"run{index}"
            registry.append_event("agent_request", {"instructions": "Check status"}, agent["id"], request)
            registry.append_event("agent_response", {"response": "Await user review"}, agent["id"], request)
        async def summarize(prompt):
            assert "source_event=" in prompt
            return "Await approval. Invoice INV-1042; source_event=2."
        assert await history.compact(agent["id"], summarize)
        context = history.build_context(agent["id"])
        assert agent["id"] in context and "Await approval" in context
        assert estimate_tokens(context) <= history.settings.agent_history_tokens
        async def failed(prompt):
            raise RuntimeError("model unavailable")
        with registry.connect() as conn:
            before = dict(conn.execute("SELECT * FROM agent_summaries").fetchone())
        assert not await history.compact(agent["id"], failed)
        with registry.connect() as conn:
            assert dict(conn.execute("SELECT * FROM agent_summaries").fetchone()) == before
    asyncio.run(scenario())


def test_grouped_conversation_links_survive_prose_compaction(registry):
    first = registry.create_agent("First", "First task")
    second = registry.create_agent("Second", "Second task")
    event = registry.append_event("conversation", {"tag": "poke_reply", "content": "Both drafts ready", "agent_ids": [first["id"], second["id"]]})
    links = registry.recent_links()
    assert {r["agent_id"] for r in links} == {first["id"], second["id"]}
    assert all(r["event_id"] == event for r in links)
