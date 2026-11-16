"""Check registry, queue, interaction, trigger, and reset integration with fake models."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from server.agents.interaction_agent import runtime as interaction
from server.services.conversation.log import ConversationLog
from server.services.conversation.summarization.working_memory_log import WorkingMemoryLog
from server.services.execution.registry import AgentRegistry
from server.services.execution.queue import ExecutionService
from server.services.execution.retrieval import SearchService
from server.services.triggers.service import TriggerService
from server.services.triggers.store import TriggerStore
from server.services.trigger_scheduler import TriggerScheduler


async def eventually(predicate, timeout=3):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("Timed out waiting for integration outcome")
        await asyncio.sleep(0.01)


@pytest.fixture
def state(tmp_path, monkeypatch):
    from server.config import get_settings
    from server.services.conversation import log as log_module
    from server.services.conversation.summarization import working_memory_log as memory_module
    from server.services.execution import registry as registry_module

    registry = AgentRegistry(tmp_path / "registry.db")
    memory = WorkingMemoryLog(tmp_path / "memory.log")
    monkeypatch.setattr(log_module, "_resolve_working_memory_log", lambda: memory)
    log = ConversationLog(tmp_path / "conversation.log")
    log._notify_summarization = lambda: None
    monkeypatch.setattr(log_module, "_conversation_log", log)
    monkeypatch.setattr(memory_module, "_working_memory_log", memory)
    monkeypatch.setattr(registry_module, "_registry", registry)
    settings = get_settings().model_copy(update={"agent_hybrid_search_enabled": False,
        "agent_compaction_enabled": False, "agent_auto_archive_enabled": False,
        "agent_shutdown_seconds": 0})
    interaction.resume_interactions()
    return SimpleNamespace(registry=registry, memory=memory, log=log, settings=settings, tmp=tmp_path)


def response(content="", calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [{"id": str(index), "type": "function", "function": {
            "name": name, "arguments": json.dumps(args)}} for index, (name, args) in enumerate(calls)]
    return {"choices": [{"message": message}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def test_real_registry_queue_discovers_and_reuses_same_agent(state):
    registry, log, memory = state.registry, state.log, state.memory
    search = SearchService(registry, settings=state.settings)
    executions = []
    notifications = []

    async def runner(job):
        executions.append(job)
        event = registry.append_event("agent_response", {"response": "Found invoice INV-1042"},
            agent_id=job["agent_id"], request_id=job["request_id"], generation=job["generation"])
        registry.add_reference(job["agent_id"], "invoice_id", "INV-1042", confirmed=True, source_event_id=event)
        return {"success": True, "response": "Invoice INV-1042 is ready for review"}

    async def llm(**kwargs):
        messages = kwargs["messages"]
        is_notification = "<new_agent_message>" in messages[0]["content"]
        tools = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        if is_notification:
            if not tools:
                return response(calls=[("send_message_to_user", {"message": "Invoice INV-1042 is ready for review."})])
            return response()
        if not tools:
            return response(calls=[("search_agents", {"query": "Sarah invoice INV-1042", "include_archived": True})])
        last = tools[-1]
        assert last["success"], last
        result = last["result"]
        if last["tool"] == "search_agents":
            if result["candidates"]:
                return response(calls=[("inspect_agent", {"agent_id": result["candidates"][0]["id"]})])
            return response(calls=[("create_agent", {"name": "Sarah invoice INV-1042", "purpose": "Find Sarah's invoice INV-1042"})])
        if last["tool"] == "create_agent":
            return response(calls=[("inspect_agent", {"agent_id": result["id"]})])
        if last["tool"] == "inspect_agent":
            return response(calls=[("send_message_to_agent", {"agent_id": result["id"],
                "expected_version": result["version"], "instructions": "Continue invoice INV-1042"})])
        if last["tool"] == "send_message_to_agent":
            return response(calls=[("send_message_to_user", {"message": "I am checking invoice INV-1042."})])
        return response()

    queue = None

    async def delivery(payload, context):
        notifications.append(context)
        return await interaction.InteractionAgentRuntime(registry=registry, queue=queue, search=search,
            conversation_log=log, working_memory_log=memory, llm=llm).handle_agent_message(payload, batch_context=context)

    async def scenario():
        nonlocal queue
        queue = ExecutionService(registry, runner=runner, delivery=delivery, settings=state.settings)
        runtime = interaction.InteractionAgentRuntime(registry=registry, queue=queue, search=search,
            conversation_log=log, working_memory_log=memory, llm=llm)
        first = await runtime.execute("Find Sarah's invoice INV-1042", request_id="first")
        assert first.success
        await queue.start()
        try:
            await eventually(lambda: len(notifications) == 1 and not queue._deliveries)
            first_id = executions[0]["agent_id"]
            second = await runtime.execute("Follow up on Sarah's invoice INV-1042", request_id="second")
            assert second.success
            await eventually(lambda: len(notifications) == 2 and not queue._deliveries)
            assert [job["agent_id"] for job in executions] == [first_id, first_id]
            with registry.connect() as conn:
                assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1
                assert conn.execute("SELECT COUNT(*) FROM execution_jobs WHERE status='succeeded'").fetchone()[0] == 2
            assert len([m for m in log.to_chat_messages() if m.role == "user"]) == 2
            assert len([m for m in log.to_chat_messages() if m.role == "assistant"]) == 4
            assert notifications[0]["batch_id"] != notifications[1]["batch_id"]
        finally:
            await queue.stop()
    asyncio.run(scenario())


class Worker:
    def __init__(self):
        self.starts = 0
        self.stops = 0

    async def start(self):
        self.starts += 1

    async def stop(self):
        self.stops += 1


def lifecycle_dependencies(state, monkeypatch, queue):
    from server.services.execution import lifecycle, queue as queue_module
    from server.services.execution import roster, log_store
    from server.services import trigger_scheduler, gmail, triggers
    from server.services.conversation.summarization import scheduler as summary_scheduler

    trigger_service = TriggerService(TriggerStore(state.tmp / "triggers.db"))
    watcher, scheduler, maintenance = Worker(), Worker(), Worker()
    monkeypatch.setattr(queue_module, "get_execution_service", lambda: queue)
    monkeypatch.setattr(triggers, "get_trigger_service", lambda: trigger_service)
    monkeypatch.setattr(trigger_scheduler, "get_trigger_scheduler", lambda: scheduler)
    monkeypatch.setattr(gmail, "get_important_email_watcher", lambda: watcher)
    monkeypatch.setattr(lifecycle, "_maintenance", maintenance)
    monkeypatch.setattr(lifecycle, "_reset_lock", None)
    monkeypatch.setattr(state.registry, "migrate_legacy", lambda *args: None)
    monkeypatch.setattr(roster, "get_agent_roster", lambda: roster.AgentRoster(state.tmp / "roster.json", state.registry))
    monkeypatch.setattr(log_store, "get_execution_agent_logs", lambda: log_store.ExecutionAgentLogStore(state.tmp / "legacy"))
    monkeypatch.setattr(summary_scheduler, "resume_summarization", lambda: None)

    async def stop_summary():
        pass
    monkeypatch.setattr(summary_scheduler, "stop_summarization", stop_summary)
    return lifecycle, trigger_service, watcher, scheduler, maintenance


def test_startup_recovers_accepted_user_turn(state, monkeypatch):
    async def llm(**kwargs):
        return response("Your saved request resumed.")
    monkeypatch.setattr(interaction, "request_chat_completion", llm)

    async def scenario():
        queue = ExecutionService(state.registry, runner=lambda job: {"success": True}, settings=state.settings)
        lifecycle, _, watcher, scheduler, maintenance = lifecycle_dependencies(state, monkeypatch, queue)
        runtime = interaction.InteractionAgentRuntime(registry=state.registry, queue=queue,
            search=SearchService(state.registry, settings=state.settings), conversation_log=state.log,
            working_memory_log=state.memory, llm=llm)
        runtime.register_turn("Resume my request", "accepted-before-restart")
        await lifecycle.start_all()
        try:
            def complete():
                with state.registry.connect() as conn:
                    return conn.execute("SELECT status FROM interaction_turns WHERE request_id='accepted-before-restart'").fetchone()[0] == "completed"
            await eventually(complete)
            assert [m.content for m in state.log.to_chat_messages()] == ["Resume my request", "Your saved request resumed."]
            assert watcher.starts == scheduler.starts == maintenance.starts == 1
        finally:
            await lifecycle.stop_all()
    asyncio.run(scenario())


def test_coordinated_reset_cancels_owned_work_and_fences_old_callbacks(state, monkeypatch):
    async def scenario():
        gate = asyncio.Event()

        async def runner(job):
            await gate.wait()
            return {"success": True, "response": "Old result"}

        queue = ExecutionService(state.registry, runner=runner, settings=state.settings)
        lifecycle, trigger_service, watcher, scheduler, maintenance = lifecycle_dependencies(state, monkeypatch, queue)
        agent = state.registry.create_agent("Old work", "Must not survive reset")
        batch = queue.create_batch("before-reset")
        job = await queue.submit(agent["id"], "Wait", batch_id=batch["id"], request_id="old-job")
        await queue.seal_batch(batch["id"])
        await queue.start()
        await eventually(lambda: queue.job_status(job["id"])["status"] == "running")
        old_generation = state.registry.generation
        state.log.append_once("poke_reply", "Old reply", event_key="old-reply", generation=old_generation,
                              registry=state.registry, agent_ids=[agent["id"]])
        await lifecycle.reset_all()
        try:
            assert state.registry.generation == old_generation + 1
            assert not state.log.to_chat_messages()
            with state.registry.connect() as conn:
                assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0
                assert conn.execute("SELECT COUNT(*) FROM execution_jobs").fetchone()[0] == 0
            with pytest.raises(RuntimeError, match="generation"):
                state.registry.append_event("agent_response", "Late result", generation=old_generation)
            assert watcher.stops == scheduler.stops == maintenance.stops == 1
            assert watcher.starts == scheduler.starts == maintenance.starts == 1
        finally:
            await lifecycle.stop_all()
    asyncio.run(scenario())


def test_trigger_occurrence_uses_shared_queue_and_reconciles_once(state, monkeypatch):
    from server.services import trigger_scheduler
    from server.services.execution import queue as queue_module

    async def scenario():
        runs = []

        async def runner(job):
            runs.append(job)
            return {"success": True, "response": "Reminder completed"}

        async def delivery(payload, context):
            return {"success": True}

        queue = ExecutionService(state.registry, runner=runner, delivery=delivery, settings=state.settings)
        monkeypatch.setattr(queue_module, "get_execution_service", lambda: queue)
        service = TriggerService(TriggerStore(state.tmp / "triggers.db"))
        monkeypatch.setattr(trigger_scheduler, "get_trigger_service", lambda: service)
        scheduler = TriggerScheduler()
        agent = state.registry.create_agent("Reminder", "Scheduled reminder")
        now = datetime.now(timezone.utc)
        scheduled = (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        timestamp = now.isoformat().replace("+00:00", "Z")
        trigger_id = service._store.insert({"agent_name": agent["id"], "payload": "Check invoice",
            "start_time": scheduled, "next_trigger": scheduled, "recurrence_rule": None,
            "timezone": "UTC", "status": "active", "last_error": None,
            "created_at": timestamp, "updated_at": timestamp})
        trigger = service._store.fetch_one(trigger_id, agent["id"])
        await scheduler._execute_trigger(trigger)
        await scheduler._execute_trigger(trigger)
        with state.registry.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM execution_jobs").fetchone()[0] == 1
        await queue.start()
        try:
            await eventually(lambda: len(runs) == 1 and queue.job_status(runs[0]["id"])["status"] == "succeeded")
            await scheduler._reconcile_outcomes()
            await scheduler._reconcile_outcomes()
            assert service._store.fetch_one(trigger_id, agent["id"]).next_trigger is None
            assert queue.trigger_outcomes() == []
            assert len(runs) == 1
            assert not any(ref["kind"] == "enabled_trigger" for ref in state.registry.get(agent["id"])["references"])
        finally:
            await queue.stop()
    asyncio.run(scenario())


def test_draft_runs_through_execution_queue_before_ia_presents_saved_content(state, monkeypatch):
    from server.agents.execution_agent import runtime as execution
    from server.services import gmail, triggers

    monkeypatch.setattr(execution, "get_tool_registry", lambda **kwargs: {})
    monkeypatch.setattr(execution, "get_tool_schemas", lambda: [])
    monkeypatch.setattr(gmail, "get_active_gmail_user_id", lambda: None)
    monkeypatch.setattr(triggers, "get_trigger_service", lambda: SimpleNamespace(list_triggers=lambda **kwargs: []))
    registry = state.registry
    agent = registry.create_agent("Acme invoice INV-1042", "Draft the invoice reminder for Sarah")
    body = "Hi Sarah, invoice INV-1042 for CAD 420 is due March 15, 2027."
    created, notifications = [], []
    execution_steps = [
        response(calls=[("gmail_create_draft", dict(recipient_email="sarah@acme.example",
                                                   subject="Invoice reminder", body=body))]),
        response(calls=[("report_task_state", dict(state="waiting_for_user", summary="Draft prepared", pending_work=True))]),
        # Deliberately omit the ID here: delivery must supply the durable reference.
        response("The saved draft is ready for review."),
    ]

    async def execution_llm(**kwargs):
        return execution_steps.pop(0)
    monkeypatch.setattr(execution, "request_chat_completion", execution_llm)

    async def interaction_llm(**kwargs):
        messages = kwargs["messages"]
        notification = "<new_agent_message>" in messages[0]["content"]
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        schemas = {s["function"]["name"] for s in kwargs["tools"]}
        if notification:
            assert "draft-123" in messages[0]["content"]
            assert "send_draft" in schemas
            if not results:
                return response(calls=[("send_draft", dict(agent_id=agent["id"], draft_id="draft-123")),
                                       ("send_message_to_user", dict(message="Your saved draft is ready for approval."))])
            return response()
        if not results:
            assert "send_draft" not in schemas
            return response(calls=[("inspect_agent", dict(agent_id=agent["id"]))])
        if results[-1]["tool"] == "inspect_agent":
            return response(calls=[
                ("send_message_to_user", dict(message="I'll prepare the draft for review.")),
                ("send_message_to_agent", dict(agent_id=agent["id"],
                                               expected_version=results[-1]["result"]["version"],
                                               instructions="Draft Sarah a reminder for INV-1042.")),
            ])
        return response()

    async def runner(job):
        runtime = execution.ExecutionAgentRuntime(agent_id=job["agent_id"], request_id=job["request_id"],
                                                  generation=job["generation"], registry=registry)

        def draft(**arguments):
            created.append(arguments)
            return {"data": {"id": "draft-123"}, "successful": True}

        runtime.tool_registry["gmail_create_draft"] = draft
        return await runtime.execute(job["instructions"])

    async def scenario():
        queue = None

        async def delivery(payload, context):
            notifications.append(payload)
            return await interaction.InteractionAgentRuntime(registry=registry, queue=queue,
                search=SearchService(registry, settings=state.settings), conversation_log=state.log,
                working_memory_log=state.memory, llm=interaction_llm).handle_agent_message(payload, batch_context=context)

        queue = ExecutionService(registry, runner=runner, delivery=delivery, settings=state.settings)
        runtime = interaction.InteractionAgentRuntime(registry=registry, queue=queue,
            search=SearchService(registry, settings=state.settings), conversation_log=state.log,
            working_memory_log=state.memory, llm=interaction_llm)
        await queue.start()
        try:
            assert (await runtime.execute("Draft a reminder for INV-1042.", request_id="draft-request")).success
            await eventually(lambda: len(notifications) == 1 and not queue._deliveries)
            assert len(created) == 1 and created[0]["body"] == body
            assert not execution_steps
            with registry.connect() as conn:
                jobs = conn.execute("SELECT agent_id,status FROM execution_jobs").fetchall()
            assert [(j["agent_id"], j["status"]) for j in jobs] == [(agent["id"], "succeeded")]
            replies = [m.content for m in state.log.to_chat_messages() if m.role == "assistant"]
            assert f"To: sarah@acme.example\nSubject: Invoice reminder\n\n{body}" in replies
            assert registry.get(agent["id"])["approval_pending"]
        finally:
            await queue.stop()
    asyncio.run(scenario())
