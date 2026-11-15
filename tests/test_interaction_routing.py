"""Check task routing, candidate budgets, identity guards, and crash replay offline."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.agents.interaction_agent.agent import prepare_message_with_history
from server.agents.interaction_agent.runtime import InteractionAgentRuntime, resume_interactions
from server.agents.interaction_agent.tools import ToolContext, explicit_references, handle_tool_call
from server.services.conversation.log import ConversationLog
from server.services.conversation.summarization.working_memory_log import WorkingMemoryLog
from server.services.execution.budgets import estimate_tokens
from server.services.execution.registry import AgentRegistry


class FakeQueue:
    def __init__(self):
        self.batches = {}
        self.calls = []
        self.sealed = []

    def create_batch(self, request_id, **kwargs):
        return self.batches.setdefault(request_id, {"id": "batch-" + request_id})

    async def seal_batch(self, batch_id):
        self.sealed.append(batch_id)

    async def submit(self, agent_id, instructions, **kwargs):
        old = next((j for j in self.calls if j["request_id"] == kwargs["request_id"]), None)
        if old:
            return old
        result = {"id": str(len(self.calls)), "agent_id": agent_id, "status": "queued", **kwargs}
        self.calls.append(result)
        return result

    async def resolve_interrupted(self, execution_id, resolution, explicit_user=False):
        return {"id": execution_id, "resolution": resolution, "explicit_user": explicit_user}


class FakeSearch:
    def __init__(self, candidates=None, degraded=False):
        self.candidates = candidates or []
        self.degraded = degraded
        self.calls = []

    async def search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return {"candidates": self.candidates, "degraded": self.degraded, "total_candidates": len(self.candidates)}


@pytest.fixture
def services(tmp_path, monkeypatch):
    registry = AgentRegistry(tmp_path / "registry.db")
    memory = WorkingMemoryLog(tmp_path / "memory.log")
    monkeypatch.setattr("server.services.conversation.log._resolve_working_memory_log", lambda: memory)
    log = ConversationLog(tmp_path / "conversation.log")
    log._notify_summarization = lambda: None
    resume_interactions()
    return registry, FakeQueue(), log, memory


def context(services, search=None, text="Find an invoice"):
    registry, queue, log, _ = services
    return ToolContext(registry, queue, search or FakeSearch(), log, "request", "root", "batch",
                       registry.generation, text, references=explicit_references(text))


def test_prompt_does_not_include_roster():
    text = prepare_message_with_history("Continue that invoice", "Previous conversation", task_links=[])[0]["content"]
    assert "<active_agents>" not in text
    assert "<task_references>" in text
    assert "Continue that invoice" in text


def test_creation_requires_archive_search_and_inspection(services):
    async def scenario():
        ctx = context(services)
        blocked = await handle_tool_call("create_agent", {"name": "Invoice", "purpose": "Find unpaid invoice"}, ctx, "create")
        assert not blocked.success
        await handle_tool_call("search_agents", {"query": "invoice", "include_archived": True}, ctx, "search")
        created = await handle_tool_call("create_agent", {"name": "Invoice", "purpose": "Find unpaid invoice"}, ctx, "create")
        agent = created.payload
        assert created.success
        denied = await handle_tool_call("send_message_to_agent",
                                        {"agent_id": agent["id"], "expected_version": agent["version"], "instructions": "Find it"}, ctx, "dispatch")
        assert not denied.success
        inspected = await handle_tool_call("inspect_agent", {"agent_id": agent["id"]}, ctx, "inspect")
        assert inspected.success
        sent = await handle_tool_call("send_message_to_agent",
                                      {"agent_id": agent["id"], "expected_version": inspected.payload["version"], "instructions": "Find it"}, ctx, "dispatch")
        assert sent.success
        assert len(services[1].calls) == 1
    asyncio.run(scenario())


def test_conflicting_identity_is_rejected_before_dispatch(services):
    registry = services[0]
    agent = registry.create_agent("Sarah invoice", "Follow up on invoice")
    event = registry.append_event("tool_result", {"invoice": "INV-1088"}, agent_id=agent["id"])
    registry.add_reference(agent["id"], "invoice_id", "INV-1088", confirmed=True, source_event_id=event)
    ctx = context(services, text="Follow up with Sarah about INV-1042")

    async def scenario():
        result = await handle_tool_call("inspect_agent", {"agent_id": agent["id"]}, ctx, "inspect")
        assert not result.success
        assert "conflict" in str(result.payload).lower()
        assert not services[1].calls
    asyncio.run(scenario())


def test_multiple_requested_invoices_can_dispatch_their_distinct_tasks(services):
    registry = services[0]
    agents = []
    for invoice in ("INV-1042", "INV-1088"):
        agent = registry.create_agent(invoice, f"Handle {invoice}")
        event = registry.append_event("tool_result", {"invoice": invoice}, agent_id=agent["id"])
        registry.add_reference(agent["id"], "invoice_id", invoice, confirmed=True, source_event_id=event)
        agents.append(registry.get(agent["id"]))
    ctx = context(services, text="Compare INV-1042 and INV-1088")

    async def scenario():
        for number, agent in enumerate(agents):
            inspected = await handle_tool_call("inspect_agent", {"agent_id": agent["id"]}, ctx, f"inspect-{number}")
            assert inspected.success
            result = await handle_tool_call("send_message_to_agent", {"agent_id": agent["id"],
                "expected_version": agent["version"], "instructions": f"Retrieve {agent['name']}"}, ctx, f"submit-{number}")
            assert result.success
        assert len(services[1].calls) == 2
    asyncio.run(scenario())


def test_stale_inspection_needs_reinspection(services):
    registry = services[0]
    agent = registry.create_agent("Invoice", "Invoice task")
    ctx = context(services)

    async def scenario():
        await handle_tool_call("inspect_agent", {"agent_id": agent["id"]}, ctx, "inspect")
        registry.update_agent(agent["id"], summary="Changed")
        result = await handle_tool_call("send_message_to_agent",
                                        {"agent_id": agent["id"], "expected_version": agent["version"], "instructions": "Continue"}, ctx, "dispatch")
        assert not result.success
        assert "changed" in str(result.payload).lower()
    asyncio.run(scenario())


def test_review_block_is_reported_immediately_instead_of_queueing_another_search(services):
    from server.services.execution.queue import ExecutionService

    registry, _, log, _ = services
    agent = registry.create_agent("General Magic assessment", "Find assessment details")
    queue = ExecutionService(registry)

    async def scenario():
        first = queue.create_batch("original-search")
        original = await queue.submit(agent["id"], "Find assessment details", batch_id=first["id"], request_id="original-search")
        with registry.connect(write=True) as conn:
            conn.execute("UPDATE execution_jobs SET status='interrupted',error='External change needs review' WHERE id=?", (original["id"],))
        registry.update_agent(agent["id"], state="needs_review")
        followup = queue.create_batch("explainer-search")
        ctx = ToolContext(registry, queue, FakeSearch(), log, "explainer-search", "explainer-search",
                          followup["id"], registry.generation, "Find the explainer instead")
        inspected = await handle_tool_call("inspect_agent", {"agent_id": agent["id"]}, ctx, "inspect")
        result = await handle_tool_call("send_message_to_agent", {
            "agent_id": agent["id"], "expected_version": inspected.payload["version"],
            "instructions": "Find the explainer instead",
        }, ctx, "dispatch")

        assert not result.success
        assert result.payload["status"] == "rejected"
        assert result.payload["result"]["blocked"] is True
        assert result.payload["result"]["blocking_execution_id"] == original["id"]
        assert not queue.pending_jobs()

    asyncio.run(scenario())


def test_paused_background_result_cannot_enqueue_or_create_duplicate_task(services):
    registry, queue, _, _ = services
    agent = registry.create_agent("General Magic assessment", "Find assessment details")
    ctx = context(services, text="The search expired in the queue")
    ctx.is_user_turn = False
    ctx.allow_followup = False
    ctx.archive_searched = True

    async def scenario():
        inspected = await handle_tool_call("inspect_agent", {"agent_id": agent["id"]}, ctx, "inspect")
        denied = await handle_tool_call("send_message_to_agent", {
            "agent_id": agent["id"], "expected_version": inspected.payload["version"],
            "instructions": "Try the search again",
        }, ctx, "dispatch")
        duplicate = await handle_tool_call("create_agent", {
            "name": "General Magic explainer retry", "purpose": "Try the search again",
        }, ctx, "duplicate")

        assert not denied.success
        assert not duplicate.success
        assert denied.payload["retryable"] is duplicate.payload["retryable"] is False
        assert not queue.calls
        with registry.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1

        # A later human instruction starts a new request budget; the background
        # result's exhausted retry allowance must not permanently disable work.
        fresh = context(services, text="Try the General Magic search again")
        inspected = await handle_tool_call("inspect_agent", {"agent_id": agent["id"]}, fresh, "fresh-inspect")
        accepted = await handle_tool_call("send_message_to_agent", {
            "agent_id": agent["id"], "expected_version": inspected.payload["version"],
            "instructions": "Try the search again",
        }, fresh, "fresh-dispatch")
        assert accepted.success
        assert len(queue.calls) == 1

    asyncio.run(scenario())


def test_pages_and_repeated_searches_share_candidate_budget(services):
    rows = [{"id": str(i), "name": "Invoice " + str(i), "summary": "invoice detail " * 500, "version": 1}
            for i in range(30)]
    ctx = context(services, FakeSearch(rows))

    async def scenario():
        result = await handle_tool_call("search_agents", {"query": "invoice"}, ctx, "s1")
        assert result.success
        seen = list(result.payload["candidates"])
        while result.payload["next_cursor"]:
            result = await handle_tool_call("search_agents", {"query": "invoice", "cursor": result.payload["next_cursor"]}, ctx, "s2")
            assert result.success
            seen.extend(result.payload["candidates"])
        assert len(seen) == 30
        assert len(ctx.exposed) == 30
        assert ctx.candidate_tokens <= 6000
        another = await handle_tool_call("search_agents", {"query": "invoice"}, ctx, "s3")
        assert another.success
        assert ctx.candidate_tokens <= 6000
        assert all(estimate_tokens(json.dumps(row)) <= 200 for row in seen)
    asyncio.run(scenario())


def test_degraded_discovery_is_not_a_no_match_signal(services):
    ctx = context(services, FakeSearch(degraded=True), text="Continue a previous task")

    async def scenario():
        await handle_tool_call("search_agents", {"query": "previous task", "include_archived": True}, ctx, "search")
        created = await handle_tool_call("create_agent", {"name": "Another", "purpose": "Continue previous task"}, ctx, "create")
        assert not created.success
        assert "degraded" in str(created.payload)
    asyncio.run(scenario())


def test_observed_model_reference_is_not_a_hard_filter(services):
    search = FakeSearch()
    ctx = context(services, search, text="Find Sarah's invoice")

    async def scenario():
        await handle_tool_call("search_agents", {"query": "invoice", "references": [{"kind": "invoice_id", "value": "INV-9999"}]}, ctx, "search")
        assert search.calls[0][1]["references"][0]["trusted"] is False
    asyncio.run(scenario())


def test_reply_projection_recovers_without_duplicate(services):
    registry, _, log, _ = services
    event = registry.append_event("conversation", {"tag": "poke_reply", "content": "Draft prepared",
                  "timestamp": "2026-11-12 10:00:00", "agent_ids": []}, event_key="reply", generation=registry.generation)
    log.project_pending(registry, registry.generation)
    log.project_pending(registry, registry.generation)
    assert len(log.to_chat_messages()) == 1
    assert f'event_id="{event}"' in log._path.read_text()


def test_committed_plan_resumes_after_cancel_without_repeating_job(services):
    registry, queue, log, memory = services
    agent = registry.create_agent("Invoice", "Follow up invoice")
    # A two-step plan inspects first, then submits. Simulate process death while
    # waiting for the final model response after submission has committed.
    calls = []

    def tool(name, args, call_id):
        return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

    async def first_llm(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [
                tool("inspect_agent", {"agent_id": agent["id"]}, "inspect"),
                tool("send_message_to_agent", {"agent_id": agent["id"], "expected_version": agent["version"], "instructions": "Continue invoice"}, "submit")
            ]}}]}
        raise asyncio.CancelledError()

    async def resumed_llm(**kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "I am continuing the invoice."}}]}

    async def scenario():
        first = InteractionAgentRuntime(registry=registry, queue=queue, search=FakeSearch(), conversation_log=log,
                                        working_memory_log=memory, llm=first_llm)
        with pytest.raises(asyncio.CancelledError):
            await first.execute("Continue invoice", request_id="same-request")
        assert len(queue.calls) == 1
        assert not queue.sealed
        second = InteractionAgentRuntime(registry=registry, queue=queue, search=FakeSearch(), conversation_log=log,
                                         working_memory_log=memory, llm=resumed_llm)
        result = await second.execute("Continue invoice", request_id="same-request")
        assert result.success
        assert len(queue.calls) == 1
        assert len([m for m in log.to_chat_messages() if m.role == "user"]) == 1
        assert len([m for m in log.to_chat_messages() if m.role == "assistant"]) == 1
        duplicate = await second.execute("Continue invoice", request_id="same-request")
        assert duplicate.success
        assert len(log.to_chat_messages()) == 2
    asyncio.run(scenario())


def test_explicit_user_instruction_required_for_interrupted_resolution(services):
    ctx = context(services, text="What happened with the invoice?")

    async def scenario():
        denied = await handle_tool_call("resolve_interrupted_execution", {"execution_id": "job", "resolution": "retry"}, ctx, "resolve")
        assert not denied.success
        ctx.latest_text = "Retry that interrupted invoice task"
        accepted = await handle_tool_call("resolve_interrupted_execution", {"execution_id": "job", "resolution": "retry"}, ctx, "resolve2")
        assert accepted.success
    asyncio.run(scenario())


def test_explicit_agent_id_is_extracted():
    assert explicit_references("Continue 75bc9f14-2c92-4d0c-bd9a-295715bd1941")[0]["kind"] == "agent_id"


def test_ambiguous_exact_matches_require_user_clarification(services):
    registry = services[0]
    agent = registry.create_agent("Invoice", "Invoice task")
    ctx = context(services)
    ctx.requires_clarification = True

    async def scenario():
        await handle_tool_call("inspect_agent", {"agent_id": agent["id"]}, ctx, "inspect")
        result = await handle_tool_call("send_message_to_agent",
            {"agent_id": agent["id"], "expected_version": agent["version"], "instructions": "Continue"}, ctx, "submit")
        assert not result.success
        assert "which task" in str(result.payload)
        assert not services[1].calls
    asyncio.run(scenario())


def test_partial_log_projection_is_recovered(services):
    registry, _, log, _ = services
    event = registry.append_event("conversation", {"tag": "poke_reply", "content": "Recovered reply",
                  "timestamp": "2026-11-12 10:00:00", "agent_ids": []}, event_key="partial", generation=registry.generation)
    log._path.write_text(f'<poke_reply event_id="{event}">Recovered', encoding="utf-8")
    log.project_pending(registry, registry.generation)
    assert [message.content for message in log.to_chat_messages()] == ["Recovered reply"]


def test_old_generation_cannot_project_a_reply(services):
    registry, _, log, _ = services
    old_generation = registry.generation
    with registry.connect(write=True) as conn:
        conn.execute("UPDATE registry_meta SET value=? WHERE key='generation'", (str(old_generation + 1),))
    with pytest.raises(RuntimeError, match="generation"):
        log.append_once("poke_reply", "Stale response", event_key="stale", generation=old_generation, registry=registry)
    assert not log.to_chat_messages()


def test_idempotency_key_rejects_different_message(services):
    registry, queue, log, memory = services
    runtime = InteractionAgentRuntime(registry=registry, queue=queue, conversation_log=log,
                                     working_memory_log=memory, llm=lambda **kwargs: None)
    runtime.register_turn("First request", "same-key")
    with pytest.raises(ValueError, match="different message"):
        runtime.register_turn("Different request", "same-key")


def test_http_idempotency_preserves_202_and_rejects_changed_body(monkeypatch):
    from server.models import ChatMessage, ChatRequest
    from server.services.conversation import chat_handler
    turns, executions = {}, []

    class Runtime:
        def register_turn(self, text, key):
            if key in turns and turns[key]["text"] != text:
                raise ValueError("Idempotency key was already used for different message content")
            return turns.setdefault(key, {"text": text, "status": "queued"})

        async def execute(self, user_message, request_id):
            executions.append(request_id)
            turns[request_id]["status"] = "completed"

    monkeypatch.setattr(chat_handler, "InteractionAgentRuntime", Runtime)

    async def scenario():
        request = ChatRequest(messages=[ChatMessage(role="user", content="Find invoice")])
        first = await chat_handler.handle_chat_request(request, idempotency_key="stable-key")
        await asyncio.sleep(0)
        second = await chat_handler.handle_chat_request(request, idempotency_key="stable-key")
        changed = await chat_handler.handle_chat_request(
            ChatRequest(messages=[ChatMessage(role="user", content="Different work")]), idempotency_key="stable-key")
        assert first.status_code == second.status_code == 202
        assert first.body == second.body == b""
        assert first.headers["X-Request-ID"] == second.headers["X-Request-ID"]
        assert len(executions) == 1
        assert changed.status_code == 409
    asyncio.run(scenario())


def test_quiesced_interactions_reject_new_acceptance(services):
    from server.agents.interaction_agent.runtime import quiesce_interactions
    registry, queue, log, memory = services
    runtime = InteractionAgentRuntime(registry=registry, queue=queue, conversation_log=log,
                                     working_memory_log=memory, llm=lambda **kwargs: None)

    async def scenario():
        await quiesce_interactions()
        try:
            with pytest.raises(ValueError, match="admission is paused"):
                runtime.register_turn("New work", "paused")
        finally:
            resume_interactions()
    asyncio.run(scenario())
