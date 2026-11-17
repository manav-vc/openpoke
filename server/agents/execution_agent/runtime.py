"""Simplified Execution Agent Runtime."""

import inspect
import asyncio
import json
import re
import uuid
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

from .agent import ExecutionAgent
from .tools import get_tool_schemas, get_tool_registry
from ...config import get_settings
from ...openrouter_client import request_chat_completion
from ...logging_config import logger
from ...services.execution.registry import get_registry, STATES, utc_now
from ...services.execution.history import HistoryService
from ...services.execution.budgets import truncate_text, bounded_json
from ...services.execution.effects import tool_effect


class UncertainExecutionError(RuntimeError):
    """A mutating external request may have succeeded before its error."""


@dataclass
class ExecutionResult:
    """Result from an execution agent."""
    agent_name: str
    success: bool
    response: str
    error: Optional[str] = None
    tools_executed: List[str] = None
    agent_id: Optional[str] = None
    request_id: Optional[str] = None
    generation: Optional[int] = None
    task_state: str = "open"
    pending_work: bool = False
    summary: str = ""
    requires_review: bool = False


class ExecutionAgentRuntime:
    """Manages the execution of a single agent request."""

    MAX_TOOL_ITERATIONS = 8

    # Initialize execution agent runtime with settings, tools, and agent instance
    def __init__(self, agent_name: Optional[str] = None, *, agent_id: Optional[str] = None,
                 request_id: Optional[str] = None, generation: Optional[int] = None, registry=None):
        settings = get_settings()
        self.settings = settings
        self.registry = registry or get_registry()
        self.agent_id = agent_id or self.registry.resolve_alias(agent_name or "")
        if not self.agent_id:
            raise ValueError("Execution requires an existing stable agent ID")
        self.record = self.registry.get(self.agent_id)
        self.generation = self.registry.generation if generation is None else generation
        self.request_id = request_id or str(uuid.uuid4())
        self.history = HistoryService(self.registry)
        self.task_state = "open"
        self._reported_state = None
        self._reported_pending_work = None
        self.pending_work = bool(self.record["pending_work"])
        self._approval_pending = bool(self.record.get("approval_pending") or self.record.get("pending_drafts"))
        self.task_summary = self.record["summary"]
        self.agent = ExecutionAgent(self.record["name"])
        self.api_key = settings.openrouter_api_key
        self.model = settings.execution_agent_model
        self.tool_registry = get_tool_registry(agent_name=self.record["legacy_alias"] or self.agent_id)
        self.tool_registry.update(report_task_state=self._report_state, read_agent_history=self._read_history,
                                  confirm_task_reference=self._confirm_reference)
        self.tool_schemas = list(get_tool_schemas()) + self._state_schemas()

        if not self.api_key:
            raise ValueError("OpenRouter API key not configured. Set OPENROUTER_API_KEY environment variable.")

    # Main execution loop for running agent with LLM calls and tool execution
    async def execute(self, instructions: str) -> ExecutionResult:
        """Execute the agent with given instructions."""
        from ...services.execution.context import execution_context
        context_token = execution_context.set((self.registry, self.generation))
        may_have_acted = False
        try:
            self.registry.append_event("execution_effect_tracking", {"enabled": True},
                self.agent_id, self.request_id, f"execution:{self.request_id}:effects", self.generation)
            # Build system prompt with history
            system_prompt = self.agent.build_system_prompt() + "\n\n" + self.history.build_context(self.agent_id, self.request_id)

            # Start conversation with the instruction
            messages = [{"role": "user", "content": instructions}]
            tools_executed: List[str] = []
            final_response: Optional[str] = None

            for iteration in range(self.settings.agent_execution_iterations):
                with self.registry.connect(generation=self.generation):
                    pass
                logger.info(
                    f"[{self.agent.name}] Requesting plan (iteration {iteration + 1})"
                )
                response = await self._make_llm_call(system_prompt, messages, with_tools=True)
                assistant_message = response.get("choices", [{}])[0].get("message", {})

                if not assistant_message:
                    raise RuntimeError("LLM response did not include an assistant message")

                raw_tool_calls = assistant_message.get("tool_calls", []) or []
                parsed_tool_calls = self._extract_tool_calls(raw_tool_calls)

                assistant_entry: Dict[str, Any] = {
                    "role": "assistant",
                    "content": assistant_message.get("content", "") or "",
                }
                if raw_tool_calls:
                    assistant_entry["tool_calls"] = raw_tool_calls
                messages.append(assistant_entry)

                if not parsed_tool_calls:
                    final_response = assistant_entry["content"] or "No action required."
                    break

                for tool_call in parsed_tool_calls:
                    tool_name = tool_call.get("name", "")
                    tool_args = tool_call.get("arguments", {})
                    call_id = tool_call.get("id")

                    if not tool_name:
                        logger.warning("Tool call missing name: %s", tool_call)
                        failure = {"error": "Tool call missing name; unable to execute."}
                        tool_message = {
                            "role": "tool",
                            "tool_call_id": call_id or "unknown_tool",
                            "content": self._format_tool_result(
                                tool_name or "<unknown>", False, failure, tool_args
                            ),
                        }
                        messages.append(tool_message)
                        continue

                    tools_executed.append(tool_name)
                    logger.info(f"[{self.agent.name}] Executing tool: {tool_name}")

                    effect = tool_effect(tool_name)
                    self.registry.append_event("tool_intent", {"tool": tool_name, "arguments": tool_args, "effect": effect},
                        self.agent_id, self.request_id, f"execution:{self.request_id}:intent:{iteration}:{call_id or tool_name}", self.generation)
                    # Record intent before dispatch: cancelling an await on a
                    # synchronous tool cannot stop its worker thread's action.
                    may_have_acted |= effect != "read"
                    success, result = await self._execute_tool(tool_name, tool_args)

                    if success:
                        logger.info(f"[{self.agent.name}] Tool {tool_name} completed successfully")
                        record_payload = self._safe_json_dump(result)
                    else:
                        error_detail = result.get("error") if isinstance(result, dict) else str(result)
                        logger.warning(f"[{self.agent.name}] Tool {tool_name} failed: {error_detail}")
                        record_payload = error_detail

                    source_id = self.history.record_tool(self.agent_id, self.request_id, tool_name, tool_args,
                        result, success, f"execution:{self.request_id}:tool:{iteration}:{call_id or tool_name}", self.generation)
                    if not success and effect != "read":
                        # Save the complete response before stopping: the error
                        # may be the only evidence of an action's remote outcome.
                        raise UncertainExecutionError(
                            f"{tool_name} returned an uncertain error; do not repeat it automatically. "
                            f"Review tool result event {source_id}.")
                    if success:
                        self._capture_references(tool_name, tool_args, result, source_id)

                    tool_message = {
                        "role": "tool",
                        "tool_call_id": call_id or tool_name,
                        "content": bounded_json({"tool": tool_name, "success": success, "source_event_id": source_id,
                                                  "result": result}, self.settings.agent_excerpt_tokens),
                    }
                    messages.append(tool_message)

            else:
                raise RuntimeError("Reached tool iteration limit without final response")

            if final_response is None:
                raise RuntimeError("LLM did not return a final response")

            self.registry.append_event("agent_response", {"response": final_response, "task_state": self.task_state},
                self.agent_id, self.request_id, f"execution:{self.request_id}:response", self.generation)
            self._persist_task_state()

            return ExecutionResult(
                agent_name=self.agent.name,
                success=True,
                response=final_response,
                tools_executed=tools_executed, agent_id=self.agent_id, request_id=self.request_id,
                generation=self.generation, task_state=self.task_state, pending_work=self.pending_work, summary=self.task_summary,
                requires_review=self.task_state == "needs_review"
            )

        except UncertainExecutionError as exc:
            error = str(exc)
            self.registry.append_event("execution_interrupted", {"error": error}, self.agent_id, self.request_id,
                f"execution:{self.request_id}:uncertain", self.generation)
            self.registry.update_agent(self.agent_id, generation=self.generation, state="needs_review", pending_work=True)
            return ExecutionResult(agent_name=self.agent.name, success=False,
                response="An external action may have completed. Check the original source before continuing.",
                error=error, agent_id=self.agent_id, request_id=self.request_id, generation=self.generation,
                task_state="needs_review", pending_work=True, requires_review=True)
        except Exception as e:
            logger.error(f"[{self.agent.name}] Execution failed: {e}")
            error_msg = str(e)
            failure_text = f"Failed to complete task: {error_msg}"
            self.registry.append_event("execution_error", {"error": error_msg}, self.agent_id,
                self.request_id, f"execution:{self.request_id}:error", self.generation)

            # A later model/parsing failure does not undo an earlier action.
            # Replaying the entire request could repeat a successful write too.
            if may_have_acted:
                self.registry.update_agent(self.agent_id, generation=self.generation,
                                           state="needs_review", pending_work=True)

            return ExecutionResult(
                agent_name=self.agent.name,
                success=False,
                response=failure_text,
                error=error_msg, agent_id=self.agent_id, request_id=self.request_id, generation=self.generation,
                task_state="needs_review" if may_have_acted else self.task_state,
                pending_work=True if may_have_acted else self.pending_work,
                requires_review=may_have_acted,
            )
        finally:
            execution_context.reset(context_token)

    # Execute OpenRouter API call with system prompt, messages, and optional tool schemas
    async def _make_llm_call(self, system_prompt: str, messages: List[Dict], with_tools: bool) -> Dict:
        """Make an LLM call."""
        tools_to_send = self.tool_schemas if with_tools else None
        logger.info(f"[{self.agent.name}] Calling LLM with model: {self.model}, tools: {len(tools_to_send) if tools_to_send else 0}")
        return await request_chat_completion(
            model=self.model,
            messages=messages,
            system=system_prompt,
            api_key=self.api_key,
            tools=tools_to_send
        )

    # Parse and validate tool calls from LLM response into structured format
    def _extract_tool_calls(self, raw_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Extract tool calls from an assistant message."""
        tool_calls: List[Dict[str, Any]] = []

        for tool in raw_tools:
            function = tool.get("function", {})
            name = function.get("name", "")
            args = function.get("arguments", "")

            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except json.JSONDecodeError:
                    args = {}

            if name:
                tool_calls.append({
                    "id": tool.get("id"),
                    "name": name,
                    "arguments": args,
                })

        return tool_calls

    # Safely convert objects to JSON with fallback to string representation
    def _safe_json_dump(self, payload: Any) -> str:
        """Serialize payload to JSON, falling back to string representation."""
        try:
            return json.dumps(payload, default=str)
        except TypeError:
            return str(payload)

    # Format tool execution results into JSON structure for LLM consumption
    def _format_tool_result(
        self,
        tool_name: str,
        success: bool,
        result: Any,
        arguments: Dict[str, Any],
    ) -> str:
        """Build a structured string for tool responses."""
        if success:
            payload: Dict[str, Any] = {
                "tool": tool_name,
                "status": "success",
                "arguments": arguments,
                "result": result,
            }
        else:
            error_detail = result.get("error") if isinstance(result, dict) else str(result)
            payload = {
                "tool": tool_name,
                "status": "error",
                "arguments": arguments,
                "error": error_detail,
            }
        return self._safe_json_dump(payload)

    # Execute tool function from registry with error handling and async support
    async def _execute_tool(self, tool_name: str, arguments: Dict) -> Tuple[bool, Any]:
        """Execute a tool. Returns (success, result)."""
        tool_func = self.tool_registry.get(tool_name)
        if not tool_func:
            return False, {"error": f"Unknown tool: {tool_name}"}

        try:
            with self.registry.connect(generation=self.generation):
                pass
            if inspect.iscoroutinefunction(tool_func):
                result = await tool_func(**arguments)
            else:
                result = await asyncio.to_thread(tool_func, **arguments)
            if inspect.isawaitable(result):
                result = await result
            success = not (isinstance(result, dict) and (result.get("error") or result.get("status") == "error" or result.get("successful") is False))
            return success, result
        except UncertainExecutionError:
            raise
        except Exception as e:
            if tool_effect(tool_name) != "read":
                raise UncertainExecutionError(f"{tool_name} returned an uncertain error; do not repeat it automatically: {e}") from e
            return False, {"error": str(e)}

    @staticmethod
    def _state_schemas() -> List[Dict]:
        def schema(name, description, properties, required):
            return {"type": "function", "function": {"name": name, "description": description,
                    "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}}
        return [schema("report_task_state", "Explicitly report the saved task goal state. Success of an invocation alone is not completion. Pending approval/reminders mean waiting. For an unavailable read tool use open or waiting_for_user; needs_review is reserved for runtime-detected uncertain external actions.",
                       {"state": {"type": "string", "enum": sorted(STATES - {"needs_review"})}, "summary": {"type": "string"},
                        "pending_work": {"type": "boolean"}}, ["state", "summary", "pending_work"]),
                schema("read_agent_history", "Retrieve bounded original source events for this task when exact details are needed.",
                       {"event_ids": {"type": "array", "items": {"type": "integer"}}, "query": {"type": "string"}, "cursor": {"type": "string"}}, []),
                schema("confirm_task_reference", "Select a task-specific invoice/case reference observed in an actual tool result. Cite its source event; do not confirm every search hit.",
                       {"kind": {"type": "string", "enum": ["invoice_id", "case_id", "task_id"]},
                        "value": {"type": "string"}, "source_event_id": {"type": "integer"}}, ["kind", "value", "source_event_id"])]

    def _report_state(self, state: str, summary: str, pending_work: bool) -> dict:
        # A model's request for human attention is not evidence that an external
        # write may have happened. Only runtime effect tracking can establish
        # that recovery condition; otherwise a handled read failure would block
        # this task's queue as an unresolved interrupted execution.
        if state == "needs_review":
            raise ValueError("needs_review is reserved for runtime-detected uncertain external actions. "
                             "Use open or waiting_for_user for missing information or an unavailable read tool.")
        if state not in STATES:
            raise ValueError("Invalid task state")
        self._reported_state, self._reported_pending_work = state, bool(pending_work)
        self.task_state, self.task_summary, self.pending_work = state, summary, bool(pending_work)
        if self._approval_pending:
            self.task_state, self.pending_work = "waiting_for_user", True
        elif state == "completed" and pending_work:
            self.task_state = "waiting_for_user"
        return {"state": self.task_state, "completion_applied_after_execution": True}

    def _read_history(self, **kwargs) -> dict:
        return self.history.read(self.agent_id, **kwargs)

    def _confirm_reference(self, kind: str, value: str, source_event_id: int) -> dict:
        if kind not in {"invoice_id", "case_id", "task_id"}:
            raise ValueError("Unsupported task identity kind")
        event = self.registry.get_event(source_event_id)
        if event["agent_id"] != self.agent_id or event["kind"] != "tool_result":
            raise ValueError("Reference source must be a tool result for this task")
        payload = self.history._payload(event)
        if not payload.get("success") or payload.get("tool") not in {"task_email_search", "gmail_fetch_emails", "gmail_get_message"}:
            raise ValueError("Identity confirmation requires a successful original email source, not a history or model-output echo")
        parts = []
        def collect(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"subject", "clean_text", "body", "snippet"} and isinstance(child, str):
                        parts.append(child)
                    elif isinstance(child, (dict, list)):
                        collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(payload.get("result", {}))
        evidence = "\n".join(parts)
        if value.casefold() not in evidence.casefold():
            raise ValueError("Reference is not present in the cited tool result")
        from ...services.gmail import get_active_gmail_user_id
        scope = get_active_gmail_user_id() or ""
        self.registry.add_reference(self.agent_id, kind, value, scope, True, source_event_id, value, self.generation)
        return {"confirmed": True, "kind": kind, "value": value}

    def _capture_references(self, name: str, arguments: dict, result: Any, source_id: int) -> None:
        from ...services.gmail import get_active_gmail_user_id
        scope = get_active_gmail_user_id() or ""
        captured = {}
        def add(kind, value, confirmed=False):
            if isinstance(value, str) and value.strip():
                key = (kind, value.casefold() if kind == "email" else value)
                item = captured.setdefault(key, dict(kind=kind, value=value, scope=scope, confirmed=False, source_event_id=source_id))
                item["confirmed"] |= confirmed
        if name.startswith("gmail_"):
            add("thread_id", arguments.get("thread_id"), True)
            add("email", arguments.get("recipient_email"), True)
            add("draft_id", arguments.get("draft_id"), True)
        def observe(value):
            if isinstance(value, dict):
                if value.get("id") and ("subject" in value or "clean_text" in value):
                    add("message_id", value["id"])
                for key, child in value.items():
                    if key in {"thread_id", "threadId"}:
                        add("thread_id", child)
                    elif key in {"message_id", "messageId"}:
                        add("message_id", child)
                    elif key in {"draft_id", "draftId"}:
                        add("draft_id", child, name == "gmail_create_draft")
                    elif key in {"sender", "recipient", "from", "to"} and isinstance(child, str):
                        for email in re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", child):
                            add("email", email)
                    if isinstance(child, (dict, list)):
                        observe(child)
            elif isinstance(value, list):
                for child in value:
                    observe(child)
        observe(result)
        if name == "gmail_create_draft":
            if isinstance(result, dict):
                data = result.get("data", result)
                if isinstance(data, dict):
                    add("draft_id", data.get("id"), True)
            draft_ids = [item["value"] for item in captured.values() if item["kind"] == "draft_id" and item["confirmed"]]
            with self.registry.connect(write=True, generation=self.generation) as conn:
                for draft_id in draft_ids or [f"unknown-source:{source_id}"]:
                    conn.execute("INSERT OR IGNORE INTO task_approvals(agent_id,draft_id,source_event_id) VALUES(?,?,?)",
                                 (self.agent_id, draft_id, source_id))
                conn.execute("UPDATE agents SET approval_pending=1,pending_work=1,version=version+1 WHERE id=?", (self.agent_id,))
            self._approval_pending = True
            self.task_state, self.pending_work = "waiting_for_user", True
        elif name in {"gmail_execute_draft", "gmail_delete_draft"}:
            with self.registry.connect(write=True, generation=self.generation) as conn:
                conn.execute("DELETE FROM task_approvals WHERE agent_id=? AND draft_id=?", (self.agent_id, arguments.get("draft_id", "")))
                rows = conn.execute("SELECT draft_id FROM task_approvals WHERE agent_id=?", (self.agent_id,)).fetchall()
                if len(rows) == 1 and rows[0][0].startswith("unknown-source:"):
                    conn.execute("DELETE FROM task_approvals WHERE agent_id=?", (self.agent_id,))
                    rows = []
                self._approval_pending = bool(rows)
                self.pending_work = self._approval_pending
                conn.execute("UPDATE agents SET approval_pending=?,version=version+1 WHERE id=?", (int(self._approval_pending), self.agent_id))
        self.registry.add_references(self.agent_id, list(captured.values()), self.generation)

    def _persist_task_state(self) -> None:
        from ...services.triggers import get_trigger_service
        alias = self.record["legacy_alias"] or self.agent_id
        reminders = [t for t in get_trigger_service().list_triggers(agent_name=alias) if t.status == "active" and t.next_trigger]
        if self._approval_pending:
            self.task_state, self.pending_work = "waiting_for_user", True
        elif reminders:
            self.task_state, self.pending_work = "waiting_for_trigger", True
        elif self.pending_work and self.task_state == "completed":
            self.task_state = "waiting_for_user"
        self.registry.append_event("task_state", {"state": self.task_state, "summary": self.task_summary,
            "pending_work": self.pending_work, "reported_state": self._reported_state,
            "reported_pending_work": self._reported_pending_work, "approval_pending": self._approval_pending}, self.agent_id, self.request_id,
            f"execution:{self.request_id}:state", self.generation)
        self.registry.update_agent(self.agent_id, generation=self.generation, state=self.task_state,
            summary=self.task_summary, pending_work=self.pending_work, last_activity=utc_now(),
            approval_pending=self._approval_pending, completed_at=utc_now() if self.task_state == "completed" else None)
