"""Check execution approvals, tool effects, source evidence, and uncertain writes."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

import server.agents.execution_agent.runtime as execution_module
import server.services.execution.registry as registry_module
from server.agents.execution_agent.runtime import ExecutionAgentRuntime
from server.services.execution.registry import AgentRegistry, StaleGenerationError
from server.services.execution.context import execution_context
from server.services.execution.queue import ExecutionService
from server.services.triggers.store import TriggerStore
from server.services.trigger_scheduler import TriggerScheduler
from server.services.execution.effects import tool_effect


def tool(name, arguments, identifier="call1"):
    import json
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def answer(content="", calls=None):
    return {"choices": [{"message": {"content": content, "tool_calls": calls or []}}]}


@pytest.fixture
def setup_runtime(tmp_path, monkeypatch):
    registry = AgentRegistry(tmp_path / "registry.db")
    monkeypatch.setattr(execution_module, "get_tool_registry", lambda **kwargs: {})
    monkeypatch.setattr(execution_module, "get_tool_schemas", lambda: [])
    import server.services.gmail as gmail
    import server.services.triggers as triggers
    monkeypatch.setattr(gmail, "get_active_gmail_user_id", lambda: "account-A")
    monkeypatch.setattr(triggers, "get_trigger_service", lambda: SimpleNamespace(list_triggers=lambda **kwargs: []))
    return registry


def test_created_draft_preserves_sources_and_cannot_be_completed_before_approval(setup_runtime, monkeypatch):
    async def scenario():
        registry = setup_runtime
        agent = registry.create_agent("Acme invoice", "Follow up INV-1042")
        body = "exact long draft content " * 5000
        steps = [answer(calls=[tool("gmail_create_draft", {"recipient_email": "sarah@acme.example", "thread_id": "thread-789", "body": body})]),
                 answer(calls=[tool("report_task_state", {"state": "completed", "summary": "Draft prepared", "pending_work": False})]),
                 answer("Draft ready for review")]
        async def llm(**kwargs):
            return steps.pop(0)
        monkeypatch.setattr(execution_module, "request_chat_completion", llm)
        runtime = ExecutionAgentRuntime(agent_id=agent["id"], request_id="draft-run", registry=registry)
        runtime.tool_registry["gmail_create_draft"] = lambda **kwargs: {"data": {"id": "draft-123"}, "successful": True}
        result = await runtime.execute("Prepare a draft")
        assert result.success and result.task_state == "waiting_for_user" and result.pending_work
        saved = registry.get(agent["id"])
        assert saved["state"] == "waiting_for_user"
        refs = {(r["kind"], r["value"], bool(r["confirmed"])) for r in saved["references"]}
        assert ("thread_id", "thread-789", True) in refs
        assert ("draft_id", "draft-123", True) in refs
        with registry.connect() as conn:
            event_id = conn.execute("SELECT id FROM events WHERE kind='tool_result' AND request_id='draft-run' ORDER BY id LIMIT 1").fetchone()[0]
        assert runtime.history._payload(registry.get_event(event_id))["arguments"]["body"] == body
    asyncio.run(scenario())


def test_successful_invocation_does_not_imply_completed_goal(setup_runtime, monkeypatch):
    async def scenario():
        registry = setup_runtime
        agent = registry.create_agent("Task", "Continue later")
        async def llm(**kwargs):
            with registry.connect() as conn:
                event_id = conn.execute("SELECT id FROM events WHERE kind='execution_effect_tracking'").fetchone()[0]
            assert registry.get_event(event_id)["payload"] == {"enabled": True}
            return answer("I found the information")
        monkeypatch.setattr(execution_module, "request_chat_completion", llm)
        result = await ExecutionAgentRuntime(agent_id=agent["id"], registry=registry).execute("Find information")
        assert result.success and registry.get(agent["id"])["state"] == "open"
    asyncio.run(scenario())


def test_multiple_draft_approvals_survive_reminders_and_only_target_is_cleared(setup_runtime, monkeypatch):
    registry = setup_runtime
    agent = registry.create_agent("Two drafts", "Prepare two drafts and a reminder")
    runtime = ExecutionAgentRuntime(agent_id=agent["id"], registry=registry, request_id="prepare")
    for draft_id in ("draft-one", "draft-two"):
        event = runtime.history.record_tool(agent["id"], "prepare", "gmail_create_draft", {}, {"data": {"id": draft_id}}, True, draft_id, registry.generation)
        runtime._capture_references("gmail_create_draft", {}, {"data": {"id": draft_id}}, event)
    import server.services.triggers as triggers
    monkeypatch.setattr(triggers, "get_trigger_service", lambda: SimpleNamespace(list_triggers=lambda **kwargs: [SimpleNamespace(status="active", next_trigger="2030-01-01")]))
    runtime._report_state("completed", "Drafts prepared", False)
    runtime._persist_task_state()
    saved = registry.get(agent["id"])
    assert saved["state"] == "waiting_for_user" and saved["approval_pending"]
    resumed = ExecutionAgentRuntime(agent_id=agent["id"], registry=registry, request_id="send-first")
    event = resumed.history.record_tool(agent["id"], "send-first", "gmail_execute_draft", {"draft_id": "draft-one"}, {"successful": True}, True, "send-first", registry.generation)
    resumed._capture_references("gmail_execute_draft", {"draft_id": "draft-one"}, {"successful": True}, event)
    resumed._report_state("completed", "First draft sent", False)
    resumed._persist_task_state()
    saved = registry.get(agent["id"])
    assert saved["approval_pending"] and saved["state"] == "waiting_for_user"
    assert [d["draft_id"] for d in saved["pending_drafts"]] == ["draft-two"]


def test_uncertain_gmail_write_stops_model_loop_and_holds_followups(setup_runtime, monkeypatch):
    async def scenario():
        registry = setup_runtime
        agent = registry.create_agent("Send approved draft", "Send the approved message")
        calls = []
        async def llm(**kwargs):
            calls.append("model")
            return answer(calls=[tool("gmail_execute_draft", {"draft_id": "draft-123"})])
        def uncertain(**kwargs):
            calls.append("send")
            raise TimeoutError("Connection lost after remote operation")
        monkeypatch.setattr(execution_module, "request_chat_completion", llm)
        async def runner(job):
            runtime = ExecutionAgentRuntime(agent_id=job["agent_id"], registry=registry, request_id=job["request_id"], generation=job["generation"])
            runtime.tool_registry["gmail_execute_draft"] = uncertain
            return await runtime.execute(job["instructions"])
        async def deliver(*args):
            return {"success": True}
        queue = ExecutionService(registry, runner=runner, delivery=deliver)
        batch = queue.create_batch("root-request")
        job = await queue.submit(agent["id"], "Send approved draft", batch_id=batch["id"], request_id="send-once")
        following = await queue.submit(agent["id"], "Follow up after sending", batch_id=batch["id"], request_id="followup")
        await queue.seal_batch(batch["id"])
        await queue.start()
        try:
            result = await queue.wait_for_job(job["id"], timeout=3)
            assert result["status"] == "interrupted"
            assert queue.job_status(following["id"])["status"] == "queued"
            assert registry.get(agent["id"])["state"] == "needs_review"
            assert calls == ["model", "send"]
            with registry.connect() as conn:
                assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='tool_intent'").fetchone()[0] == 1
        finally:
            await queue.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("raise_read_error", [False, True])
def test_handled_read_outage_cannot_interrupt_task_or_hold_followup(setup_runtime, monkeypatch, raise_read_error):
    """Replay the live 9/10 failure: the model mislabeled a handled outage."""
    async def scenario():
        registry = setup_runtime
        agent = registry.create_agent("Invoice INV-2200", "Find the invoice due date")
        steps = [answer(calls=[tool("task_email_search", {"query": "INV-2200"})]),
                 answer(calls=[tool("report_task_state", {"state": "needs_review",
                     "summary": "Email service unavailable; due date could not be verified", "pending_work": False})]),
                 answer("The due date could not be verified because the email service was unavailable."),
                 answer(calls=[tool("report_task_state", {"state": "completed",
                     "summary": "Follow-up completed", "pending_work": False})]),
                 answer("Follow-up completed")]
        async def llm(**kwargs):
            return steps.pop(0)
        monkeypatch.setattr(execution_module, "request_chat_completion", llm)
        def unavailable(**kwargs):
            if raise_read_error:
                raise ConnectionError("Email service unavailable")
            return {"error": "Email service unavailable"}
        async def runner(job):
            runtime = ExecutionAgentRuntime(agent_id=job["agent_id"], registry=registry,
                request_id=job["request_id"], generation=job["generation"])
            runtime.tool_registry["task_email_search"] = unavailable
            return await runtime.execute(job["instructions"])
        async def deliver(*args):
            return {"success": True}
        queue = ExecutionService(registry, runner=runner, delivery=deliver)
        batch = queue.create_batch("outage-request")
        outage = await queue.submit(agent["id"], "Read the due date; report an outage without guessing",
                                   batch_id=batch["id"], request_id="read-outage")
        followup = await queue.submit(agent["id"], "Continue the task",
                                     batch_id=batch["id"], request_id="followup")
        await queue.seal_batch(batch["id"])
        await queue.start()
        try:
            result = await queue.wait_for_job(outage["id"], timeout=3)
            assert result["status"] == "succeeded"
            assert not result["result"]["requires_review"]
            assert "could not be verified" in result["result"]["response"]
            assert (await queue.wait_for_job(followup["id"], timeout=3))["status"] == "succeeded"
            assert registry.get(agent["id"])["state"] == "completed"
            with registry.connect() as conn:
                events = conn.execute("SELECT id FROM events WHERE request_id='read-outage' AND kind='tool_result' ORDER BY id").fetchall()
            history = execution_module.HistoryService(registry)
            payloads = [history._payload(registry.get_event(row[0])) for row in events]
            assert [p["tool"] for p in payloads] == ["task_email_search", "report_task_state"]
            assert all(p["success"] is False for p in payloads)
            assert payloads[1]["arguments"]["state"] == "needs_review"
            assert "reserved for runtime-detected" in payloads[1]["result"]["error"]
        finally:
            await queue.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("tool_name,expected", [
    ("task_email_search", "read"),
    ("gmail_get_contacts", "read"),
    ("gmail_get_people", "read"),
    ("gmail_list_drafts", "read"),
    ("gmail_search_people", "read"),
    ("gmail_fetch_emails", "read"),
    ("gmail_get_message", "read"),
    ("listTriggers", "read"),
    ("read_agent_history", "read"),
    ("report_task_state", "read"),
    ("confirm_task_reference", "read"),
    ("gmail_create_draft", "write"),
    ("gmail_execute_draft", "write"),
    ("gmail_delete_draft", "write"),
    ("gmail_forward_email", "write"),
    ("gmail_reply_to_thread", "write"),
    ("createTrigger", "write"),
    ("updateTrigger", "write"),
    ("gmail_future_action", "unknown"),
    ("task_future_search", "unknown"),
])
def test_tool_effects_require_explicit_classification(tool_name, expected):
    assert tool_effect(tool_name) == expected


def test_all_exposed_execution_tools_have_an_audited_effect():
    from server.agents.execution_agent.tools.registry import get_tool_registry
    names = get_tool_registry("classification-only").keys()
    assert names
    assert all(tool_effect(name) != "unknown" for name in names)


@pytest.mark.parametrize("tool_name", ["task_email_search", "gmail_execute_draft", "createTrigger", "future_tool"])
def test_tool_effect_intent_commits_before_dispatch(setup_runtime, monkeypatch, tool_name):
    async def scenario():
        registry = setup_runtime
        agent = registry.create_agent("Find information", "Search or continue the saved task")
        steps = [answer(calls=[tool(tool_name, {})]), answer("Done")]
        async def llm(**kwargs):
            return steps.pop(0)
        monkeypatch.setattr(execution_module, "request_chat_completion", llm)
        runtime = ExecutionAgentRuntime(agent_id=agent["id"], request_id="effect-run", registry=registry)
        async def action(**kwargs):
            with registry.connect() as conn:
                row = conn.execute("SELECT id FROM events WHERE request_id='effect-run' AND kind='tool_intent'").fetchone()
            assert row is not None
            intent = registry.get_event(row[0])["payload"]
            assert intent["tool"] == tool_name
            assert intent["effect"] == tool_effect(tool_name)
            return {"successful": True}
        runtime.tool_registry[tool_name] = action
        result = await runtime.execute("Continue the task")
        assert result.success
    asyncio.run(scenario())


@pytest.mark.parametrize("tool_name", ["task_email_search", "gmail_execute_draft", "createTrigger", "future_tool"])
def test_later_failure_preserves_prior_action_risk(setup_runtime, monkeypatch, tool_name):
    async def scenario():
        registry = setup_runtime
        agent = registry.create_agent("Continue task", "Find information or perform the authorized action")
        calls = []
        async def llm(**kwargs):
            calls.append("model")
            if len(calls) == 1:
                return answer(calls=[tool(tool_name, {})])
            raise RuntimeError("Model connection lost after tool completion")
        monkeypatch.setattr(execution_module, "request_chat_completion", llm)
        runtime = ExecutionAgentRuntime(agent_id=agent["id"], registry=registry)
        runtime.tool_registry[tool_name] = lambda **kwargs: {"successful": True}
        result = await runtime.execute("Continue the authorized task")
        assert not result.success
        assert result.requires_review == (tool_effect(tool_name) != "read")
        assert registry.get(agent["id"])["state"] == ("open" if tool_effect(tool_name) == "read" else "needs_review")
        with registry.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='tool_result'").fetchone()[0] == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("tool_name", ["gmail_execute_draft", "createTrigger", "updateTrigger", "future_tool"])
def test_error_response_after_mutation_stops_execution(setup_runtime, monkeypatch, tool_name):
    async def scenario():
        registry = setup_runtime
        agent = registry.create_agent("Continue task", "Perform an authorized action")
        calls = []
        async def llm(**kwargs):
            calls.append("model")
            return answer(calls=[tool(tool_name, {})])
        monkeypatch.setattr(execution_module, "request_chat_completion", llm)
        runtime = ExecutionAgentRuntime(agent_id=agent["id"], registry=registry)
        runtime.tool_registry[tool_name] = lambda **kwargs: {"error": "Remote result is unknown"}
        result = await runtime.execute("Perform the action once")
        assert not result.success and result.requires_review
        assert calls == ["model"]
        assert registry.get(agent["id"])["state"] == "needs_review"
        with registry.connect() as conn:
            event_id = conn.execute("SELECT id FROM events WHERE kind='tool_result'").fetchone()[0]
        payload = runtime.history._payload(registry.get_event(event_id))
        assert payload["tool"] == tool_name and payload["success"] is False
        assert payload["result"] == {"error": "Remote result is unknown"}
    asyncio.run(scenario())


def test_confirmed_identity_requires_successful_original_source_not_echo(setup_runtime):
    registry = setup_runtime
    agent = registry.create_agent("Invoice", "Find invoice")
    runtime = ExecutionAgentRuntime(agent_id=agent["id"], registry=registry)
    for name, success, result in [
        ("read_agent_history", True, {"clean_text": "INV-1042"}),
        ("task_email_search", False, {"clean_text": "INV-1042"}),
        ("task_email_search", True, {"query": "INV-1042", "emails": [{"subject": "Unrelated", "clean_text": "No invoice here"}]})]:
        event = registry.append_event("tool_result", {"tool": name, "success": success, "result": result}, agent_id=agent["id"])
        with pytest.raises(ValueError):
            runtime._confirm_reference("invoice_id", "INV-1042", event)
    event = registry.append_event("tool_result", {"tool": "task_email_search", "success": True,
        "result": {"emails": [{"subject": "Payment", "clean_text": "Invoice INV-1042 is unpaid"}]}}, agent_id=agent["id"])
    assert runtime._confirm_reference("invoice_id", "INV-1042", event)["confirmed"]


def test_trigger_completion_cannot_overwrite_a_newer_draft(setup_runtime):
    registry = setup_runtime
    agent = registry.create_agent("Reminder", "One-time reminder")
    ExecutionService(registry)
    registry.append_event("task_state", {"reported_state": "completed", "reported_pending_work": False,
        "approval_pending": False}, agent["id"], "old-trigger")
    registry.update_agent(agent["id"], state="waiting_for_user", pending_work=True)
    registry.append_event("task_state", {"state": "waiting_for_user", "approval_pending": True}, agent["id"], "new-draft")
    scheduler = TriggerScheduler()
    scheduler._finalize_goal_after_trigger({"agent_id": agent["id"], "request_id": "old-trigger", "generation": registry.generation}, registry)
    assert registry.get(agent["id"])["state"] == "waiting_for_user"


def test_trigger_goal_finalizes_after_schedule_already_projected(setup_runtime, monkeypatch):
    async def scenario():
        registry = setup_runtime
        agent = registry.create_agent("Reminder", "One-time reminder")
        ExecutionService(registry)
        registry.update_agent(agent["id"], state="waiting_for_trigger", pending_work=True)
        registry.append_event("task_state", {"reported_state": "completed", "reported_pending_work": False,
            "approval_pending": False}, agent["id"], "trigger-run")
        job = {"id": "job-one", "agent_id": agent["id"], "request_id": "trigger-run", "generation": registry.generation,
               "status": "succeeded", "trigger": {"id": 1, "agent_name": agent["id"], "scheduled_for": "2026-01-01T00:00:00Z"}}
        projected = []
        queue = SimpleNamespace(trigger_outcomes=lambda: [job], mark_trigger_projected=projected.append)
        import server.services.execution.queue as queue_module
        monkeypatch.setattr(queue_module, "get_execution_service", lambda: queue)
        monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
        scheduler = TriggerScheduler()
        scheduler._service = SimpleNamespace(_store=SimpleNamespace(fetch_one=lambda *args: SimpleNamespace(status="completed", next_trigger=None)))
        await scheduler._reconcile_outcomes()
        assert registry.get(agent["id"])["state"] == "completed"
        assert projected == ["job-one"]
    asyncio.run(scenario())


def test_reset_serializes_with_trigger_write_inside_generation_guard(tmp_path, monkeypatch):
    registry = AgentRegistry(tmp_path / "registry.db")
    agent = registry.create_agent("Reminder", "Wait")
    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    store = TriggerStore(tmp_path / "triggers.db")
    entered, release, reset_done = threading.Event(), threading.Event(), threading.Event()
    original_connect = store._connect
    paused = [False]
    errors = []
    def connect():
        if threading.current_thread().name == "old-tool" and not paused[0]:
            paused[0] = True
            entered.set()
            assert release.wait(3)
        return original_connect()
    monkeypatch.setattr(store, "_connect", connect)
    generation = registry.generation
    def old_tool():
        token = execution_context.set((registry, generation))
        try:
            store.insert({"agent_name": agent["id"], "payload": "old", "created_at": "2026-01-01", "updated_at": "2026-01-01"})
        except StaleGenerationError:
            pass
        except Exception as exc:
            errors.append(exc)
        finally:
            execution_context.reset(token)
    def reset():
        registry.reset()
        store.clear_all()
        reset_done.set()
    writer = threading.Thread(target=old_tool, name="old-tool")
    writer.start()
    assert entered.wait(3)
    resetter = threading.Thread(target=reset)
    resetter.start()
    assert not reset_done.wait(0.1)
    release.set()
    writer.join(5)
    resetter.join(5)
    assert not writer.is_alive() and not resetter.is_alive()
    assert not errors and reset_done.is_set()
    assert store.list_all() == []
