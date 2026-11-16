"""Draft previews must come from pending successful execution-tool evidence."""
import json
from types import SimpleNamespace

import pytest

from server.agents.execution_agent import runtime as execution
from server.agents.interaction_agent.tools import ToolContext, explicit_references, get_tool_schemas
from server.services.conversation.log import ConversationLog
from server.services.conversation.summarization.working_memory_log import WorkingMemoryLog
from server.services.execution.drafts import load_pending_draft, pending_draft_references
from server.services.execution.registry import AgentRegistry


@pytest.fixture
def state(tmp_path, monkeypatch):
    registry = AgentRegistry(tmp_path / "registry.db")
    memory = WorkingMemoryLog(tmp_path / "memory.log")
    monkeypatch.setattr("server.services.conversation.log._resolve_working_memory_log", lambda: memory)
    monkeypatch.setattr("server.services.gmail.get_active_gmail_user_id", lambda: "account-A")
    monkeypatch.setattr(execution, "get_tool_registry", lambda **kwargs: {})
    monkeypatch.setattr(execution, "get_tool_schemas", lambda: [])
    log = ConversationLog(tmp_path / "conversation.log")
    log._notify_summarization = lambda: None
    agent = registry.create_agent("Acme invoice", "Follow up on invoice INV-1042")
    context = ToolContext(registry, SimpleNamespace(), SimpleNamespace(), log, "user-turn",
                          "user-turn", "batch", registry.generation, "Draft a payment reminder for INV-1042",
                          references=explicit_references("INV-1042"), account_scope="account-A")
    return SimpleNamespace(registry=registry, agent=agent, context=context, log=log)


def save_draft(state, *, draft_id="draft-123", agent_id=None, request_id="execution-request", body="Invoice INV-1042: CAD 420"):
    agent_id = agent_id or state.agent["id"]
    runtime = execution.ExecutionAgentRuntime(agent_id=agent_id, registry=state.registry, request_id=request_id)
    arguments = dict(recipient_email="sarah@acme.example", subject="Payment reminder", body=body,
                     thread_id="thread-acme-1042")
    result = {"data": {"id": draft_id}, "successful": True}
    source = runtime.history.record_tool(agent_id, request_id, "gmail_create_draft", arguments,
                                        result, True, "create:" + draft_id, state.registry.generation)
    runtime._capture_references("gmail_create_draft", arguments, result, source)
    return dict(agent_id=agent_id, draft_id=draft_id), source, runtime


def test_send_draft_accepts_only_saved_references():
    schema = next(s["function"] for s in get_tool_schemas() if s["function"]["name"] == "send_draft")
    assert set(schema["parameters"]["properties"]) == {"agent_id", "draft_id"}
    assert set(schema["parameters"]["required"]) == {"agent_id", "draft_id"}


def test_ia_cannot_record_the_live_run_freeform_bypass(state):
    # The live failure read invoice history then called send_draft with freshly
    # generated text, never dispatching an EA or creating an email draft.
    arguments = dict(agent_id=state.agent["id"], to="sarah@acme.example",
                     subject="Payment reminder", body="Invoice INV-1042: CAD 420")
    with pytest.raises(ValueError, match="Submit drafting instructions"):
        state.context.present_draft(arguments, "forged-preview")
    assert not state.log.to_chat_messages()
    with pytest.raises(ValueError, match="No pending Execution Agent draft"):
        state.context.present_draft(dict(agent_id=state.agent["id"], draft_id="invented"), "invented")


def test_draft_tool_hidden_until_inspected_or_linked_task_has_a_pending_draft(state):
    schemas = get_tool_schemas()
    state.context.inspected[state.agent["id"]] = state.agent["version"]
    assert "send_draft" not in {s["function"]["name"] for s in state.context.available_tools(schemas)}
    save_draft(state)
    assert "send_draft" in {s["function"]["name"] for s in state.context.available_tools(schemas)}


def test_presents_exact_native_execution_source_once_and_keeps_approval_pending(state):
    arguments, source, _ = save_draft(state)
    first = state.context.present_draft(arguments, "present-once")
    second = state.context.present_draft(arguments, "present-once")
    assert first.success and second.success
    assert first.payload["source_event_id"] == source
    replies = [message.content for message in state.log.to_chat_messages()]
    assert replies == ["To: sarah@acme.example\nSubject: Payment reminder\n\nInvoice INV-1042: CAD 420"]
    assert state.registry.get(state.agent["id"])["approval_pending"]


def test_model_cannot_replace_body_or_recipient_of_a_saved_draft(state):
    arguments, _, _ = save_draft(state)
    for extra in ({"body": "Different amount: 980"}, {"to": "someone-else@example.test"}):
        with pytest.raises(ValueError, match="only presents saved"):
            state.context.present_draft(arguments | extra, "altered")
    assert not state.log.to_chat_messages()


def test_draft_source_cannot_cross_agent_identity_or_account(state):
    arguments, _, _ = save_draft(state)
    other = state.registry.create_agent("Other invoice", "Unrelated work")
    with pytest.raises(ValueError, match="No pending"):
        load_pending_draft(state.registry, other["id"], arguments["draft_id"])
    state.context.account_scope = "account-B"
    with pytest.raises(ValueError, match="different connected account"):
        state.context.present_draft(arguments, "other-account")
    assert not state.log.to_chat_messages()


def test_draft_presentation_still_rejects_confirmed_invoice_conflicts(state):
    arguments, source, _ = save_draft(state)
    state.registry.add_reference(state.agent["id"], "invoice_id", "INV-9999",
                                 confirmed=True, source_event_id=source)
    with pytest.raises(ValueError, match="identifiers conflict"):
        state.context.present_draft(arguments, "conflicting-invoice")


@pytest.mark.parametrize("tool_name", ["gmail_execute_draft", "gmail_delete_draft"])
def test_sent_or_deleted_draft_cannot_be_presented_as_pending(state, tool_name):
    arguments, _, runtime = save_draft(state)
    source = runtime.history.record_tool(state.agent["id"], "resolve", tool_name,
                                        {"draft_id": arguments["draft_id"]}, {"successful": True},
                                        True, "resolve", state.registry.generation)
    runtime._capture_references(tool_name, {"draft_id": arguments["draft_id"]}, {"successful": True}, source)
    with pytest.raises(ValueError, match="No pending"):
        state.context.present_draft(arguments, "stale")
    state.context.linked_agent_ids = [state.agent["id"]]
    assert "send_draft" not in {s["function"]["name"] for s in state.context.available_tools(get_tool_schemas())}


def test_large_draft_uses_full_artifact_not_truncated_history(state):
    body = 'Exact "quoted" draft, with unicode ₹ and original details.\n' * 3000
    arguments, source, _ = save_draft(state, body=body)
    assert "artifact_id" in state.registry.get_event(source)["payload"]
    state.context.present_draft(arguments, "large")
    assert state.log.to_chat_messages()[0].content.endswith(body)
    assert load_pending_draft(state.registry, **arguments)["body"] == body


@pytest.mark.parametrize("change", ["failed", "wrong_kind", "wrong_generation", "different_draft_id"])
def test_pending_pointer_is_not_sufficient_without_valid_execution_evidence(state, change):
    arguments, source, _ = save_draft(state)
    event = state.registry.get_event(source)
    if change == "failed":
        event["payload"]["result"]["successful"] = False
    elif change == "different_draft_id":
        event["payload"]["result"]["data"]["id"] = "other-draft"
    with state.registry.connect(write=True) as conn:
        conn.execute("UPDATE events SET payload=?,kind=?,generation=? WHERE id=?",
                     (json.dumps(event["payload"]), "agent_response" if change == "wrong_kind" else event["kind"],
                      event["generation"] - 1 if change == "wrong_generation" else event["generation"], source))
    with pytest.raises(ValueError):
        state.context.present_draft(arguments, "invalid-evidence")
    assert not state.log.to_chat_messages()


def test_queue_draft_references_are_scoped_to_the_execution_request(state):
    first, source, _ = save_draft(state, request_id="first")
    save_draft(state, draft_id="draft-second", request_id="second")
    refs = pending_draft_references(state.registry, [state.agent["id"]], request_id="first")
    assert refs == [first | {"source_event_id": source}]
    assert not pending_draft_references(state.registry, [state.agent["id"]], request_id="unrelated")
