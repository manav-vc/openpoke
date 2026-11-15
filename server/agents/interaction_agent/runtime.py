"""Durable interaction plans, bounded discovery, and idempotent tool effects."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
import weakref
from dataclasses import asdict, dataclass
from typing import Any, Optional

from .agent import build_system_prompt, prepare_message_with_history
from .tools import ToolContext, ToolResult, explicit_references, get_tool_schemas, handle_tool_call
from ...config import get_settings
from ...logging_config import logger
from ...openrouter_client import request_chat_completion
from ...services.conversation import get_conversation_log, get_working_memory_log
from ...services.execution.budgets import bounded_json, estimate_tokens, truncate_text
from ...services.execution.registry import get_registry, utc_now

_locks = weakref.WeakKeyDictionary()
_tasks: set[asyncio.Task] = set()
_accepting = True


def _interaction_lock():
    loop = asyncio.get_running_loop()
    if loop not in _locks:
        _locks[loop] = asyncio.Lock()
    return _locks[loop]


@dataclass
class InteractionResult:
    success: bool
    response: str
    error: Optional[str] = None
    execution_agents_used: int = 0


class InteractionAgentRuntime:
    """One global conversation, with recoverable request-specific tool plans."""

    MAX_TOOL_ITERATIONS = 12

    def __init__(self, *, registry=None, queue=None, search=None, conversation_log=None,
                 working_memory_log=None, llm=None):
        self.settings = get_settings()
        self.api_key = self.settings.openrouter_api_key
        self.model = self.settings.interaction_agent_model
        self.registry = registry or get_registry()
        self._queue = queue
        self._search = search
        self.conversation_log = conversation_log or get_conversation_log()
        self.working_memory_log = working_memory_log or get_working_memory_log()
        self.llm = llm or request_chat_completion
        self.tool_schemas = get_tool_schemas()
        if not self.api_key and llm is None:
            raise ValueError("OpenRouter API key not configured. Set OPENROUTER_API_KEY.")
        self._ensure_schema()

    def _ensure_schema(self):
        with self.registry.connect(write=True) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS interaction_turns(
                    request_id TEXT PRIMARY KEY,generation INTEGER NOT NULL,
                    user_text TEXT NOT NULL,message_type TEXT NOT NULL,context TEXT NOT NULL,
                    transcript TEXT,status TEXT NOT NULL DEFAULT 'queued',response TEXT,
                    error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS interaction_steps(
                    request_id TEXT NOT NULL,step INTEGER NOT NULL,message TEXT NOT NULL,
                    PRIMARY KEY(request_id,step)
                );
                CREATE TABLE IF NOT EXISTS interaction_operations(
                    operation_key TEXT PRIMARY KEY,request_id TEXT NOT NULL,tool_name TEXT NOT NULL,
                    result TEXT NOT NULL,context TEXT NOT NULL
                );
            """)

    @property
    def queue(self):
        if self._queue is None:
            from ...services.execution.queue import get_execution_service
            self._queue = get_execution_service()
        return self._queue

    @property
    def search(self):
        if self._search is None:
            from ...services.execution.retrieval import SearchService
            self._search = SearchService(self.registry)
        return self._search

    def register_turn(self, text, request_id, message_type="user", context=None):
        if not _accepting:
            raise ValueError("Interaction admission is paused")
        generation = self.registry.generation
        with self.registry.connect(write=True, generation=generation) as conn:
            existing = conn.execute("SELECT * FROM interaction_turns WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["generation"] != generation or existing["user_text"] != text or existing["message_type"] != message_type:
                    raise ValueError("Idempotency key was already used for different message content")
                return dict(existing)
            now = utc_now()
            conn.execute("INSERT INTO interaction_turns(request_id,generation,user_text,message_type,context,status,created_at,updated_at) "
                         "VALUES(?,?,?,?,?,'queued',?,?)",
                         (request_id, generation, text, message_type, json.dumps(context or {}, default=str), now, now))
            return dict(conn.execute("SELECT * FROM interaction_turns WHERE request_id=?", (request_id,)).fetchone())

    async def execute(self, user_message: str, request_id: Optional[str] = None):
        return await self._process(user_message, request_id or "user:" + uuid.uuid4().hex, "user", {})

    async def handle_agent_message(self, agent_message: str, batch_context=None):
        context = batch_context or {}
        request_id = context.get("request_id") or "notification:" + uuid.uuid4().hex
        return await self._process(agent_message, request_id, "agent", context)

    async def _process(self, text, request_id, message_type, context):
        if not _accepting:
            return InteractionResult(False, "", "Interaction admission is paused")
        if context.get("generation") is not None and context["generation"] != self.registry.generation:
            return InteractionResult(False, "", "Interaction belongs to a cleared generation")
        turn = self.register_turn(text, request_id, message_type, context)
        generation = turn["generation"]
        task = asyncio.current_task()
        if task:
            _tasks.add(task)
        batch = None
        seal = False
        try:
            async with _interaction_lock():
                if not _accepting or generation != self.registry.generation:
                    raise RuntimeError("Interaction generation was cleared")
                with self.registry.connect(generation=generation) as conn:
                    turn = dict(conn.execute("SELECT * FROM interaction_turns WHERE request_id=?", (request_id,)).fetchone())
                if turn["status"] == "completed":
                    return InteractionResult(True, turn["response"] or "")
                context = json.loads(turn["context"])
                if "task_links" not in context:
                    context["task_links"] = self.registry.recent_links(limit=10)
                    with self.registry.connect(write=True, generation=generation) as conn:
                        conn.execute("UPDATE interaction_turns SET context=? WHERE request_id=?",
                                     (json.dumps(context, default=str), request_id))
                root_id = context.get("root_request_id") or request_id
                stage = int(context.get("stage", -1)) + 1 if message_type == "agent" and context.get("batch_id") else 0
                allow_followup = context.get("allow_followup", True) and stage <= self.settings.agent_max_followup_stages
                source_context = context.get("source_context") or {}
                root_user_text = (text if message_type == "user" else
                                  source_context.get("root_user_text", source_context.get("user_text", "")))
                batch = self.queue.create_batch(request_id, root_request_id=root_id, stage=stage,
                                                context={"generation": generation, "source_request_id": request_id,
                                                         "task_links": context["task_links"], "user_text": text,
                                                         "root_user_text": root_user_text})
                batch_id = batch.get("id") or batch["batch_id"]
                self.conversation_log.project_pending(self.registry, generation)
                if turn["transcript"] is None:
                    transcript = self._load_conversation_transcript()
                    with self.registry.connect(write=True, generation=generation) as conn:
                        conn.execute("UPDATE interaction_turns SET transcript=?,status='running',updated_at=? WHERE request_id=?",
                                     (transcript, utc_now(), request_id))
                else:
                    transcript = turn["transcript"]
                self.conversation_log.append_once(
                    "user_message" if message_type == "user" else "agent_message", text,
                    event_key=request_id + ":input", generation=generation, registry=self.registry,
                    agent_ids=context.get("agent_ids", []), request_id=request_id, batch_id=batch_id)
                refs = explicit_references(root_user_text)
                # Persist a provenance event for explicit user identifiers. Create
                # only passes them as hints; confirmed agent ownership is observed
                # at the execution tool boundary.
                user_event_id = self.registry.append_event(
                    "user_task_references", {"text": root_user_text, "references": refs}, request_id=request_id,
                    event_key=request_id + ":references", generation=generation)
                for ref in refs:
                    ref["source_event_id"] = user_event_id
                tool_context = ToolContext(
                    registry=self.registry, queue=self.queue, search=self.search, log=self.conversation_log,
                    request_id=request_id, root_request_id=root_id, batch_id=batch_id, generation=generation,
                    latest_text=text, is_user_turn=message_type == "user", stage=stage,
                    allow_followup=allow_followup, references=refs,
                    linked_agent_ids=list(context.get("agent_ids", [])))
                from ...services.gmail.client import get_active_gmail_user_id
                tool_context.account_scope = get_active_gmail_user_id()
                links = context["task_links"]
                messages = prepare_message_with_history(text, transcript, message_type, task_links=links)
                final_text, emitted_user, used = await self._run_loop(messages, tool_context)
                if final_text and not emitted_user:
                    self.conversation_log.append_once(
                        "poke_reply", final_text, event_key=request_id + ":final", generation=generation,
                        registry=self.registry, agent_ids=tool_context.linked_agent_ids,
                        request_id=request_id, batch_id=batch_id)
                with self.registry.connect(write=True, generation=generation) as conn:
                    conn.execute("UPDATE interaction_turns SET status='completed',response=?,error=NULL,updated_at=? WHERE request_id=?",
                                 (final_text, utc_now(), request_id))
                seal = True
                return InteractionResult(True, final_text, execution_agents_used=len(used))
        except asyncio.CancelledError:
            # Leave committed plans and queued envelope available for restart.
            raise
        except Exception as exc:
            logger.exception("Interaction failed", extra={"request_id": request_id})
            notice = "I couldn't finish processing this request. Your saved work is preserved; please try again."
            if self.registry.generation == generation:
                with self.registry.connect(write=True, generation=generation) as conn:
                    conn.execute("UPDATE interaction_turns SET status='failed',error=?,updated_at=? WHERE request_id=?",
                                 (str(exc), utc_now(), request_id))
                self.conversation_log.append_once(
                    "poke_reply", notice,
                    event_key=request_id + ":error", generation=generation, registry=self.registry,
                    request_id=request_id)
                seal = True
            # A terminal notification failure has been visibly delivered. Do not
            # let its parent batch retry a sealed child turn forever. Process
            # interruption/cancellation follows the separate resumable path.
            return InteractionResult(message_type == "agent" and seal, notice if message_type == "agent" and seal else "", str(exc))
        finally:
            if seal and batch and self.registry.generation == generation:
                await self.queue.seal_batch(batch.get("id") or batch["batch_id"])
            if task:
                _tasks.discard(task)

    def _load_conversation_transcript(self):
        text = self.working_memory_log.render_transcript() if self.settings.summarization_enabled else ""
        memory_path = getattr(self.working_memory_log, "_path", None)
        conversation_path = getattr(self.conversation_log, "_path", None)
        if text and memory_path and conversation_path and memory_path.exists() and conversation_path.exists():
            if conversation_path.stat().st_mtime_ns > memory_path.stat().st_mtime_ns:
                text = ""
        if not text.strip():
            text = self.conversation_log.load_transcript()
        budget = self.settings.agent_interaction_history_tokens
        if estimate_tokens(text) <= budget:
            return text
        # Preserve a compact summary plus recent history, not only the oldest prefix.
        head = ""
        end = text.find("</conversation_summary>")
        if end >= 0:
            head = truncate_text(text[:end + len("</conversation_summary>")], budget // 4)
        tail_budget = max(0, budget - estimate_tokens(head))
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if estimate_tokens(text[-middle:]) <= tail_budget:
                low = middle
            else:
                high = middle - 1
        return head + "\n" + (text[-low:] if low else "")

    async def _run_loop(self, messages, context):
        final_text = ""
        emitted_user = False
        used = set()
        for step in range(self.settings.agent_interaction_iterations):
            with self.registry.connect(generation=context.generation) as conn:
                saved = conn.execute("SELECT message FROM interaction_steps WHERE request_id=? AND step=?",
                                     (context.request_id, step)).fetchone()
            if saved:
                message = json.loads(saved[0])
            else:
                before = time.monotonic()
                response = await self.llm(model=self.model, messages=messages, system=build_system_prompt(),
                                          api_key=self.api_key, tools=self.tool_schemas)
                context.check_generation()
                message = (response.get("choices") or [{}])[0].get("message")
                if not isinstance(message, dict):
                    raise RuntimeError("LLM response has no assistant message")
                logger.info("interaction_model_usage", extra={"request_id": context.request_id, "usage": response.get("usage", {}),
                            "elapsed_ms": (time.monotonic() - before) * 1000})
                # The exact plan must commit before any tool side effects.
                with self.registry.connect(write=True, generation=context.generation) as conn:
                    conn.execute("INSERT INTO interaction_steps(request_id,step,message) VALUES(?,?,?)",
                                 (context.request_id, step, json.dumps(message, default=str)))
            final_text = str(message.get("content") or "").strip() or final_text
            messages.append({"role": "assistant", **message})
            calls = message.get("tool_calls") or []
            if not calls:
                return final_text, emitted_user, used
            for index, raw in enumerate(calls):
                function = raw.get("function") or {}
                name = function.get("name", "")
                call_id = str(raw.get("id") or f"call-{index}")
                operation_key = f"{context.request_id}:{step}:{index}:{call_id}"
                with self.registry.connect(generation=context.generation) as conn:
                    operation = conn.execute("SELECT result,context FROM interaction_operations WHERE operation_key=?", (operation_key,)).fetchone()
                if operation:
                    result = ToolResult(**json.loads(operation["result"]))
                    context.restore(json.loads(operation["context"]))
                else:
                    result = await handle_tool_call(name, function.get("arguments") or {}, context, operation_key)
                    context.check_generation()
                    with self.registry.connect(write=True, generation=context.generation) as conn:
                        conn.execute("INSERT INTO interaction_operations(operation_key,request_id,tool_name,result,context) VALUES(?,?,?,?,?)",
                                     (operation_key, context.request_id, name, json.dumps(asdict(result), default=str),
                                      json.dumps(context.snapshot(), default=str)))
                if result.user_message:
                    emitted_user = True
                    final_text = result.user_message
                if name == "send_message_to_agent" and result.success:
                    used.add((result.payload or {}).get("agent_id", operation_key))
                limit = (self.settings.agent_inspection_tokens if name == "inspect_agent" else
                         self.settings.agent_discovery_tokens if name == "search_agents" else
                         self.settings.agent_excerpt_tokens)
                content = bounded_json({"tool": name, "success": result.success, "result": result.payload}, limit)
                messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
        raise RuntimeError("Interaction step limit reached; further work needs user direction")


def track_interaction(task):
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def quiesce_interactions():
    global _accepting
    _accepting = False
    current = asyncio.current_task()
    pending = [task for task in list(_tasks) if task is not current and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def resume_interactions():
    global _accepting
    _accepting = True


async def recover_pending_interactions():
    """Resume accepted user turns and notifications from their committed plans."""
    resume_interactions()
    registry = get_registry()
    runtime = InteractionAgentRuntime(registry=registry)
    with registry.connect() as conn:
        turns = [dict(row) for row in conn.execute(
            "SELECT * FROM interaction_turns WHERE generation=? AND status IN ('queued','running') ORDER BY created_at",
            (registry.generation,))]
    for turn in turns:
        track_interaction(asyncio.create_task(runtime._process(
            turn["user_text"], turn["request_id"], turn["message_type"], json.loads(turn["context"]))))
