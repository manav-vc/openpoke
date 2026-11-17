"""Lossless journals with bounded, source-addressable model views."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from ...config import get_settings
from ...logging_config import logger
from .budgets import estimate_tokens, truncate_text
from .registry import AgentRegistry, get_registry, json_dump, utc_now


class HistoryService:
    def __init__(self, registry: AgentRegistry | None = None):
        self.registry = registry or get_registry()
        self.settings = get_settings()

    def record_tool(self, agent_id: str, request_id: str, tool_name: str, arguments: Any, result: Any,
                    success: bool, event_key: str, generation: int) -> int:
        payload = dict(tool=tool_name, arguments=arguments, result=result, success=success)
        if len(json_dump(payload).encode("utf-8")) > 65536:
            artifact_id = self.registry.store_artifact(payload, agent_id, request_id, generation)
            payload = {"tool": tool_name, "artifact_id": artifact_id, "success": success,
                       "excerpt": truncate_text(json_dump(payload), self.settings.agent_excerpt_tokens)}
        return self.registry.append_event("tool_result", payload, agent_id, request_id, event_key, generation)

    def _payload(self, event: dict) -> Any:
        payload = event["payload"]
        if isinstance(payload, dict) and payload.get("artifact_id"):
            return self.registry.get_artifact(payload["artifact_id"], event["agent_id"])
        return payload

    def read(self, agent_id: str, event_ids: list[int] | None = None, query: str | None = None,
             cursor: int | str | None = None, max_tokens: int | None = None) -> dict:
        record = self.registry.get(agent_id)
        budget = min(max_tokens or self.settings.agent_excerpt_tokens, self.settings.agent_history_tokens)
        source_offset = 0
        if isinstance(cursor, str) and cursor.startswith("source:"):
            _, source_id, position = cursor.split(":", 2)
            event_ids, source_offset, cursor = [int(source_id)], max(0, int(position)), None
        clauses, args = ["agent_id=?", "generation=?"], [agent_id, self.registry.generation]
        if event_ids:
            if len(event_ids) > 50:
                raise ValueError("Read at most 50 source events at a time")
            clauses.append("id IN (" + ",".join("?" for _ in event_ids) + ")")
            args.extend(event_ids)
        if cursor is not None:
            clauses.append("id<?")
            args.append(int(cursor))
        if query:
            clauses.append("(payload LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM artifacts a "
                           "WHERE a.id=json_extract(events.payload,'$.artifact_id') AND a.agent_id=events.agent_id AND a.payload LIKE ? ESCAPE '\\'))")
            pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args.extend([pattern, pattern])
        with self.registry.connect() as conn:
            rows = conn.execute("SELECT id FROM events WHERE " + " AND ".join(clauses) + " ORDER BY id DESC LIMIT 51", args).fetchall()
        output, consumed = [], 0
        for row in rows[:50]:
            event = self.registry.get_event(row[0])
            content = json_dump(self._payload(event))
            offset = source_offset
            if query:
                position = content.casefold().find(query.casefold())
                if position >= 0:
                    offset = max(0, position - 200)
            remaining = budget - consumed - 70
            if remaining < 50:
                break
            excerpt = truncate_text(content[offset:], min(self.settings.agent_excerpt_tokens, remaining))
            marker = "\n[excerpt truncated; retrieve source for details]"
            item = {"event_id": event["id"], "kind": event["kind"], "request_id": event["request_id"],
                    "content": excerpt, "source_offset": offset, "truncated": excerpt != content[offset:]}
            if item["truncated"]:
                item["next_source_cursor"] = f"source:{event['id']}:{offset + len(excerpt.removesuffix(marker))}"
            while estimate_tokens(json_dump(item)) > budget - consumed and excerpt:
                excerpt = truncate_text(excerpt, max(1, estimate_tokens(excerpt) - 20))
                item["content"] = excerpt
                item["truncated"] = True
                item["next_source_cursor"] = f"source:{event['id']}:{offset + len(excerpt.removesuffix(marker))}"
            consumed += estimate_tokens(json_dump(item))
            output.append(item)
        legacy = None
        if not output and record.get("legacy_history"):
            path = Path(record["legacy_history"])
            if path.is_file():
                legacy = truncate_text(path.read_text(encoding="utf-8"), budget)
        return {"agent_id": agent_id, "events": output,
                "next_cursor": output[-1]["event_id"] if len(output) < len(rows) and output else None,
                "legacy_excerpt": legacy, "legacy_coverage": "truncated_or_unknown" if record.get("legacy_history") else None,
                "estimated_tokens": consumed}

    def build_context(self, agent_id: str, current_request_id: str | None = None) -> str:
        agent = self.registry.get(agent_id)
        identity = {k: agent[k] for k in ("id", "name", "purpose", "state", "pending_work", "approval_pending", "pending_drafts", "references")}
        with self.registry.connect() as conn:
            summary = conn.execute("SELECT * FROM agent_summaries WHERE agent_id=?", (agent_id,)).fetchone()
            requests = [r[0] for r in conn.execute("SELECT request_id FROM events WHERE agent_id=? AND kind='agent_response' AND request_id IS NOT NULL GROUP BY request_id ORDER BY MAX(id) DESC LIMIT 2", (agent_id,))]
            if current_request_id and current_request_id not in requests:
                requests.insert(0, current_request_id)
            rows = []
            for request_id in reversed(requests):
                rows.extend(conn.execute("SELECT * FROM events WHERE agent_id=? AND request_id=? AND kind IN ('agent_request','tool_result','agent_response','task_state') ORDER BY id", (agent_id, request_id)).fetchall())
        parts = ["# Saved task identity and unresolved state\n" + json_dump(identity)]
        if summary:
            parts.append("# Earlier work summary; retrieve cited sources for exact details\n" + truncate_text(summary["summary"], self.settings.agent_summary_tokens))
        available = self.settings.agent_history_tokens - sum(estimate_tokens(p) for p in parts)
        groups: dict[str, list[str]] = {}
        for row in rows:
            excerpt = truncate_text(row["payload"], self.settings.agent_excerpt_tokens)
            groups.setdefault(row["request_id"], []).append(f"[source_event={row['id']} kind={row['kind']}] {excerpt}")
        selected = []
        for request_id, group in reversed(list(groups.items())):
            text = "\n".join(group)
            if estimate_tokens(text) > available:
                selected.append(f"Request {request_id} omitted to honor the context budget; retrieve source events as needed.")
                continue
            selected.append(text)
            available -= estimate_tokens(text)
        parts.extend(reversed(selected))
        if not rows and agent.get("legacy_history"):
            legacy = self.read(agent_id, max_tokens=max(100, available))
            parts.append("# Legacy history; omitted original details may be unrecoverable\n" + (legacy["legacy_excerpt"] or "No readable legacy history"))
        return truncate_text("\n\n".join(parts), self.settings.agent_history_tokens)

    async def compact(self, agent_id: str, summarizer: Callable[[str], Awaitable[str]] | None = None,
                      generation: int | None = None) -> bool:
        generation = self.registry.generation if generation is None else generation
        with self.registry.connect(generation=generation) as conn:
            previous = conn.execute("SELECT * FROM agent_summaries WHERE agent_id=?", (agent_id,)).fetchone()
            cursor = previous["last_event_id"] if previous else 0
            ends = conn.execute("SELECT id,request_id FROM events WHERE agent_id=? AND kind='agent_response' AND id>? ORDER BY id", (agent_id, cursor)).fetchall()
            if len(ends) < 3:
                return False
            source_parts, cutoff = [], cursor
            remaining = self.settings.agent_history_tokens - self.settings.agent_summary_tokens - 300
            for end in ends[:-2]:
                rows = conn.execute("SELECT id,kind,payload FROM events WHERE agent_id=? AND request_id=? AND id<=? ORDER BY id",
                                    (agent_id, end["request_id"], end["id"])).fetchall()
                segment = "\n".join(f"source_event={r['id']} {r['kind']}: {truncate_text(r['payload'], self.settings.agent_excerpt_tokens)}" for r in rows)
                if estimate_tokens(segment) > remaining:
                    if source_parts:
                        break
                    # Represent every source reference instead of silently cutting
                    # a request in half and claiming its unshown tail was summarized.
                    segment = (f"Completed request {end['request_id']} exceeds the summary input budget. "
                               "Do not infer its omitted contents; retrieve these source events: " + ",".join(str(r["id"]) for r in rows))
                if estimate_tokens(segment) > remaining:
                    return False
                source_parts.append(segment)
                remaining -= estimate_tokens(segment)
                cutoff = end["id"]
        if not source_parts:
            return False
        source = "\n\n".join(source_parts)
        prompt = ("Summarize completed task work in at most " + str(self.settings.agent_summary_tokens) + " tokens. "
                  "The saved summary has a strict size limit: prioritize current corrections and superseded values, unresolved instructions, "
                  "pending approvals, exact task identifiers and the source_event IDs supporting these facts. "
                  "Preserve distinctions between original and corrected amounts and never treat a correction as approval. "
                  "Group routine operational updates briefly instead of enumerating every entry. "
                  "Do not infer that approvals or unresolved work are absent just because they are not mentioned in this batch. "
                  "Do not mark a task completed or invent missing information.\nPrevious summary:\n" + (previous["summary"] if previous else "") + "\nEvents:\n" + source)
        if summarizer is None:
            from ...openrouter_client import request_chat_completion
            async def summarizer(text: str) -> str:
                response = await request_chat_completion(model=self.settings.summarizer_model,
                    messages=[{"role": "user", "content": text}], api_key=self.settings.openrouter_api_key)
                return response["choices"][0]["message"]["content"]
        try:
            result = await summarizer(prompt)
            if not isinstance(result, str) or not result.strip():
                return False
            result = truncate_text(result, self.settings.agent_summary_tokens)
            with self.registry.connect(write=True, generation=generation) as conn:
                current = conn.execute("SELECT last_event_id FROM agent_summaries WHERE agent_id=?", (agent_id,)).fetchone()
                if current and current[0] != cursor:
                    return False
                conn.execute("INSERT INTO agent_summaries(agent_id,last_event_id,summary,version,updated_at) VALUES(?,?,?,1,?) "
                             "ON CONFLICT(agent_id) DO UPDATE SET last_event_id=excluded.last_event_id,summary=excluded.summary,version=version+1,updated_at=excluded.updated_at",
                             (agent_id, cutoff, result, utc_now()))
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Agent history compaction failed", extra={"agent_id": agent_id})
            return False
