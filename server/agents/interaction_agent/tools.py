"""Explicit task discovery and dispatch scoped to a durable interaction turn."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ...config import get_settings
from ...services.execution.budgets import bounded_json, estimate_tokens, truncate_text


@dataclass
class ToolResult:
    success: bool
    payload: Any = None
    user_message: Optional[str] = None
    recorded_reply: bool = False


def _schema(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}}


_STR = {"type": "string"}
_REFERENCES = {"type": "array", "items": {"type": "object", "properties": {
    "kind": _STR, "value": _STR, "scope": _STR, "source_event_id": {"type": "integer"}},
    "required": ["kind", "value"], "additionalProperties": False}}
TOOL_SCHEMAS = [
    _schema("search_agents", "Discover saved tasks. Search archives before creation. Continue pages with cursor.",
            {"query": _STR, "references": _REFERENCES, "include_archived": {"type": "boolean"}, "cursor": _STR}, ["query"]),
    _schema("inspect_agent", "Verify current identity, state and version before dispatch.",
            {"agent_id": _STR}, ["agent_id"]),
    _schema("create_agent", "Explicitly create after archive-inclusive discovery; never invent identifiers.",
            {"name": _STR, "purpose": _STR, "references": _REFERENCES}, ["name", "purpose"]),
    _schema("send_message_to_agent", "Submit to an inspected saved task; never implicitly creates one.",
            {"agent_id": _STR, "expected_version": {"type": "integer"}, "instructions": _STR},
            ["agent_id", "expected_version", "instructions"]),
    _schema("read_agent_history", "Read bounded original evidence for a saved task.",
            {"agent_id": _STR, "event_ids": {"type": "array", "items": {"type": "integer"}}, "query": _STR, "cursor": _STR}, ["agent_id"]),
    _schema("archive_agent", "Archive an eligible completed task.", {"agent_id": _STR}, ["agent_id"]),
    _schema("restore_agent", "Restore an archived task.", {"agent_id": _STR}, ["agent_id"]),
    _schema("resolve_interrupted_execution", "Only following explicit user direction, resolve uncertain work.",
            {"execution_id": _STR, "resolution": {"type": "string", "enum": ["completed", "abandon", "retry"]}}, ["execution_id", "resolution"]),
    _schema("send_message_to_user", "Record a response or clarification for the user.", {"message": _STR}, ["message"]),
    _schema("send_draft", "Record an exact draft for review; then ask the user how to proceed.",
            {"to": _STR, "subject": _STR, "body": _STR, "agent_id": _STR}, ["to", "subject", "body"]),
    _schema("wait", "Avoid a duplicate response already in the history.", {"reason": _STR}, ["reason"]),
]


def get_tool_schemas():
    return TOOL_SCHEMAS


def explicit_references(text):
    refs = []
    for value in re.findall(r"\bINV[-_ ]?\d[\w-]*\b", text, re.IGNORECASE):
        refs.append({"kind": "invoice_id", "value": value, "confirmed": True})
    for value in re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text):
        refs.append({"kind": "email", "value": value.lower(), "confirmed": True})
    for value in re.findall(r"\bagent_[A-Za-z0-9_-]+\b", text):
        refs.append({"kind": "agent_id", "value": value, "confirmed": True})
    for value in re.findall(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", text):
        refs.append({"kind": "agent_id", "value": value.lower(), "confirmed": True})
    return refs


@dataclass
class ToolContext:
    registry: Any
    queue: Any
    search: Any
    log: Any
    request_id: str
    root_request_id: str
    batch_id: str
    generation: int
    latest_text: str
    is_user_turn: bool = True
    stage: int = 0
    allow_followup: bool = True
    account_scope: Optional[str] = None
    references: list = field(default_factory=list)
    inspected: dict = field(default_factory=dict)
    searches: dict = field(default_factory=dict)
    exposed: list = field(default_factory=list)
    discovery_calls: int = 0
    inspections: int = 0
    candidate_tokens: int = 0
    archive_searched: bool = False
    search_degraded: bool = False
    requires_clarification: bool = False
    linked_agent_ids: list = field(default_factory=list)

    def snapshot(self):
        return {name: getattr(self, name) for name in (
            "references", "inspected", "searches", "exposed", "discovery_calls", "inspections",
            "candidate_tokens", "archive_searched", "search_degraded", "requires_clarification", "linked_agent_ids")}

    def restore(self, state):
        for name, value in state.items():
            if name in self.snapshot():
                setattr(self, name, value)

    def check_generation(self):
        if self.registry.generation != self.generation:
            raise RuntimeError("Interaction belongs to a cleared generation")

    def _discovery(self):
        self.discovery_calls += 1
        if self.discovery_calls > get_settings().agent_discovery_calls:
            raise ValueError("Discovery budget reached; ask for a distinguishing task reference")

    def _trusted_references(self, supplied):
        refs = list(self.references)
        for ref in supplied or []:
            item = {k: v for k, v in ref.items() if k in {"kind", "value", "scope", "source_event_id"}}
            item["confirmed"] = any(r.get("kind") == item.get("kind") and
                                    str(r.get("value", "")).casefold() == str(item.get("value", "")).casefold()
                                    for r in self.references)
            item["trusted"] = item["confirmed"]
            if item not in refs:
                refs.append(item)
        return refs

    def _identity_conflicts(self, agent):
        conflicts = []
        groups = {}
        for reference in self.references:
            if reference.get("kind") in {"agent_id", "invoice_id", "case_id", "task_id"}:
                groups.setdefault((reference["kind"], reference.get("scope", "")), []).append(reference)
        for (kind, scope), required in groups.items():
            allowed = {str(ref["value"]).casefold() for ref in required}
            if kind == "agent_id":
                if agent["id"].casefold() not in allowed:
                    conflicts.extend(required)
                continue
            known = [r for r in agent.get("references", []) if r.get("confirmed") and
                     r.get("kind") == kind and (not scope or r.get("scope") == scope)]
            if known and not any(str(r["value"]).casefold() in allowed for r in known):
                conflicts.extend(required)
        return conflicts

    def _page(self, search_id, offset):
        settings = get_settings()
        result = self.searches[search_id]
        candidates = result.get("candidates", [])
        rows = []
        for item in candidates[offset:offset + settings.agent_candidate_page_size]:
            agent_id = item.get("id") or item.get("agent_id")
            if agent_id not in self.exposed and len(self.exposed) >= settings.agent_candidate_pool:
                break
            row = dict(item)
            row["summary"] = truncate_text(str(row.get("summary", "")), settings.agent_candidate_tokens // 2)
            row["purpose"] = truncate_text(str(row.get("purpose", "")), settings.agent_candidate_tokens // 4)
            if estimate_tokens(json.dumps(row, default=str)) > settings.agent_candidate_tokens:
                row = {k: row[k] for k in ("id", "agent_id", "name", "version", "state", "archived", "identifier_matches") if k in row} | {
                    "summary": truncate_text(str(row.get("summary", "")), settings.agent_candidate_tokens // 3)}
            if estimate_tokens(json.dumps(row, default=str)) > settings.agent_candidate_tokens:
                row = {k: row[k] for k in ("id", "version", "state", "archived") if k in row} | {
                    "name": truncate_text(str(row.get("name", "")), 30), "evidence_available_on_inspection": True}
            cost = estimate_tokens(json.dumps(row, default=str))
            if self.candidate_tokens + cost > settings.agent_discovery_tokens:
                break
            self.candidate_tokens += cost
            rows.append(row)
            if agent_id not in self.exposed:
                self.exposed.append(agent_id)
        next_offset = offset + len(rows)
        return {k: v for k, v in result.items() if k != "candidates"} | {
            "search_id": search_id, "candidates": rows,
            "next_cursor": f"{search_id}:{next_offset}" if rows and next_offset < len(candidates) else None,
            "estimated_candidate_tokens": self.candidate_tokens}

    async def execute(self, name, args, operation_key):
        self.check_generation()
        settings = get_settings()
        if name in {"create_agent", "send_message_to_agent"} and not self.allow_followup:
            return ToolResult(False, {
                "status": "blocked", "retryable": False,
                "error": "Automatic follow-up is paused for this result; report the outcome and ask the user for further direction",
            })
        if name == "search_agents":
            self._discovery()
            if args.get("cursor"):
                search_id, position = args["cursor"].rsplit(":", 1)
                if search_id not in self.searches:
                    raise ValueError("Unknown search cursor")
                return ToolResult(True, self._page(search_id, int(position)))
            result = await self.search.search(args["query"], references=self._trusted_references(args.get("references")),
                                             include_archived=bool(args.get("include_archived", False)), scope=self.account_scope)
            self.check_generation()
            search_id = uuid.uuid4().hex
            self.searches[search_id] = result
            self.archive_searched |= bool(args.get("include_archived", False))
            self.search_degraded |= bool(result.get("degraded"))
            self.requires_clarification = bool(result.get("requires_clarification"))
            return ToolResult(True, self._page(search_id, 0))
        if name == "inspect_agent":
            self._discovery()
            self.inspections += 1
            if self.inspections > settings.agent_max_inspections:
                raise ValueError("Inspection budget reached; clarify remaining task ambiguity")
            agent = self.registry.get(args["agent_id"])
            scopes = {r.get("scope") for r in agent.get("references", []) if r.get("scope")}
            if self.account_scope and scopes and self.account_scope not in scopes:
                raise ValueError("Task belongs to a different connected account")
            if self._identity_conflicts(agent):
                return ToolResult(False, {"error": "Task identifiers conflict", "conflicts": self._identity_conflicts(agent)})
            self.inspected[agent["id"]] = agent["version"]
            visible = {k: agent[k] for k in ("id", "name", "purpose", "summary", "version", "state",
                                             "archived", "history_ambiguous", "pending_work", "approval_pending", "pending_drafts") if k in agent}
            visible["summary"] = truncate_text(visible.get("summary", ""), settings.agent_inspection_tokens // 3)
            visible["purpose"] = truncate_text(visible.get("purpose", ""), settings.agent_inspection_tokens // 5)
            visible["references"] = [{k: r[k] for k in ("kind", "value", "scope", "confirmed", "source_event_id") if k in r}
                                     for r in agent.get("references", [])[:20]]
            visible["reference_count"] = len(agent.get("references", []))
            jobs = self.queue.agent_jobs(agent["id"], limit=5) if hasattr(self.queue, "agent_jobs") else []
            visible["executions"] = [{k: job.get(k) for k in ("id", "request_id", "status", "review_resolution")} |
                                     {"error": truncate_text(job.get("error") or "", 100)} for job in jobs]
            visible["execution_count"] = len(jobs)
            while visible["references"] and estimate_tokens(json.dumps(visible)) > settings.agent_inspection_tokens:
                visible["references"].pop()
            if estimate_tokens(json.dumps(visible)) > settings.agent_inspection_tokens:
                visible["summary"] = ""
                visible["purpose"] = ""
            while len(visible["executions"]) > 1 and estimate_tokens(json.dumps(visible)) > settings.agent_inspection_tokens:
                visible["executions"].pop()
            return ToolResult(True, visible)
        if name == "create_agent":
            if self.requires_clarification:
                raise ValueError("Clarify the matching task identities before creating another agent")
            if not self.archive_searched:
                raise ValueError("Search active and archived tasks before creating an agent")
            if self.search_degraded and not self.references:
                raise ValueError("Discovery is degraded; clarify the task or obtain an identifying reference before creation")
            agent = self.registry.create_agent(args["name"], args["purpose"], references=self._trusted_references(args.get("references")),
                                               operation_key=operation_key, generation=self.generation)
            return ToolResult(True, {k: agent[k] for k in ("id", "name", "version", "state", "archived")})
        if name == "send_message_to_agent":
            if self.requires_clarification and not any(r.get("kind") == "agent_id" for r in self.references):
                raise ValueError("Several tasks match the identifying details; ask the user which task to continue")
            agent_id, expected = args["agent_id"], args["expected_version"]
            if self.inspected.get(agent_id) != expected:
                raise ValueError("Inspect this agent's current record before dispatch")
            agent = self.registry.get(agent_id)
            if agent["version"] != expected:
                raise ValueError("Agent changed after inspection; inspect it again")
            if self._identity_conflicts(agent):
                raise ValueError("Confirmed task identifiers conflict")
            if agent.get("history_ambiguous") and not self.is_user_turn:
                raise ValueError("Ambiguous legacy history needs user review")
            job = await self.queue.submit(agent_id, args["instructions"], batch_id=self.batch_id,
                                          request_id=operation_key, expected_version=expected, operation_key=operation_key)
            if agent_id not in self.linked_agent_ids:
                self.linked_agent_ids.append(agent_id)
            return ToolResult(job.get("status") != "rejected", job)
        if name == "read_agent_history":
            self._discovery()
            from ...services.execution.history import HistoryService
            return ToolResult(True, HistoryService(self.registry).read(
                args["agent_id"], event_ids=args.get("event_ids"), query=args.get("query"),
                cursor=args.get("cursor"), max_tokens=settings.agent_excerpt_tokens))
        if name in {"archive_agent", "restore_agent"}:
            return ToolResult(True, getattr(self.registry, name)(args["agent_id"], generation=self.generation))
        if name == "resolve_interrupted_execution":
            cues = {"retry": r"\b(retry|try again|resume|repeat)\b", "completed": r"\b(completed|already done|finished|succeeded)\b",
                    "abandon": r"\b(abandon|cancel|stop|discard)\b"}
            resolution = args["resolution"]
            if not self.is_user_turn or resolution not in cues or not re.search(cues[resolution], self.latest_text, re.I):
                raise ValueError("Ask the user how to resolve the interrupted execution")
            return ToolResult(True, await self.queue.resolve_interrupted(args["execution_id"], resolution, explicit_user=True))
        if name in {"send_message_to_user", "send_draft", "wait"}:
            if name == "send_message_to_user":
                text, tag = args["message"], "poke_reply"
            elif name == "send_draft":
                text, tag = f"To: {args['to']}\nSubject: {args['subject']}\n\n{args['body']}", "poke_reply"
                if args.get("agent_id") and args["agent_id"] not in self.linked_agent_ids:
                    self.linked_agent_ids.append(args["agent_id"])
            else:
                text, tag = args["reason"], "wait"
            self.log.append_once(tag, text, event_key=operation_key, generation=self.generation,
                                 registry=self.registry, agent_ids=self.linked_agent_ids,
                                 request_id=self.request_id, batch_id=self.batch_id)
            return ToolResult(True, {"status": "recorded"}, text if name == "send_message_to_user" else None, True)
        raise ValueError(f"Unknown tool: {name}")


async def handle_tool_call(name, arguments, context=None, operation_key=None):
    if context is None:
        return ToolResult(False, {"error": "A durable interaction context is required"})
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be an object")
        return await context.execute(name, args, operation_key or uuid.uuid4().hex)
    except Exception as exc:
        payload = {"error": str(exc)}
        if hasattr(exc, "candidate_ids"):
            payload["candidate_ids"] = exc.candidate_ids
        return ToolResult(False, payload)
