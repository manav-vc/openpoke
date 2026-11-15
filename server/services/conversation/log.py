from __future__ import annotations

import re
import threading
from html import escape, unescape
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Protocol, Tuple

from ...config import get_settings
from ...logging_config import logger
from ...models import ChatMessage
from ...utils.timezones import now_in_user_timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - used for type checkers only
    from .summarization import WorkingMemoryLog


_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_CONVERSATION_LOG_PATH = _DATA_DIR / "conversation" / "poke_conversation.log"


class TranscriptFormatter(Protocol):
    def __call__(self, tag: str, timestamp: str, payload: str) -> str:  # pragma: no cover - typing protocol
        ...


def _encode_payload(payload: str) -> str:
    normalized = payload.replace("\r\n", "\n").replace("\r", "\n")
    collapsed = normalized.replace("\n", "\\n")
    return escape(collapsed, quote=False)


def _decode_payload(payload: str) -> str:
    return unescape(payload).replace("\\n", "\n")


def _default_formatter(tag: str, timestamp: str, payload: str) -> str:
    encoded = _encode_payload(payload)
    return f"<{tag} timestamp=\"{timestamp}\">{encoded}</{tag}>\n"


def _resolve_working_memory_log() -> "WorkingMemoryLog":
    from .summarization import get_working_memory_log

    return get_working_memory_log()


_ATTR_PATTERN = re.compile(r"(\w+)\s*=\s*\"([^\"]*)\"")


class ConversationLog:
    """Append-only conversation log persisted to disk for the interaction agent."""

    def __init__(self, path: Path, formatter: TranscriptFormatter = _default_formatter):
        self._path = path
        self._formatter = formatter
        self._lock = threading.Lock()
        self._ensure_directory()
        self._working_memory_log = _resolve_working_memory_log()
        self._projection_ids = set()
        self._projection_signature = None

    def _load_projection_ids_locked(self):
        stat = self._path.stat() if self._path.exists() else None
        signature = (stat.st_mtime_ns, stat.st_size) if stat else None
        if signature != self._projection_signature:
            existing = self._path.read_text(encoding="utf-8") if stat else ""
            self._projection_ids = set()
            for line in existing.splitlines():
                if self._parse_line(line) is None:
                    continue
                match = re.search(r'event_id="(\d+)"', line.split(">", 1)[0])
                if match:
                    self._projection_ids.add(int(match.group(1)))
            self._projection_signature = signature
        return self._projection_ids

    def append_once(self, tag: str, content: str, *, event_key: str, generation: int,
                    registry, agent_ids: Optional[list[str]] = None,
                    request_id: Optional[str] = None, batch_id: Optional[str] = None) -> int:
        """Persist first, then project once into the existing UI-compatible log.

        The SQLite journal is the recovery authority. The event_id attribute is
        deliberately ignored by legacy parsers and survives a crash between the
        file append and completion of an interaction operation.
        """
        timestamp = now_in_user_timezone("%Y-%m-%d %H:%M:%S")
        ids = list(dict.fromkeys(agent_ids or []))
        event_id = registry.append_event(
            "conversation", {"tag": tag, "content": str(content), "timestamp": timestamp,
                             "agent_ids": ids, "batch_id": batch_id},
            agent_id=ids[0] if len(ids) == 1 else None, request_id=request_id,
            event_key=event_key, generation=generation)
        self._project_event(registry, event_id, generation)
        return event_id

    def _project_event(self, registry, event_id: int, generation: int) -> None:
        event = registry.get_event(event_id)
        payload = event["payload"]
        appended = False
        # Hold the generation transaction through file projection. Reset either
        # runs before this check or clears the projection after this append.
        with registry.connect(write=True, generation=generation):
            with self._lock:
                if event_id in self._load_projection_ids_locked():
                    return
                needs_newline = False
                if self._path.exists() and self._path.stat().st_size:
                    with self._path.open("rb") as handle:
                        handle.seek(-1, 2)
                        needs_newline = handle.read(1) != b"\n"
                line = self._formatter(payload["tag"], payload["timestamp"], payload["content"])
                line = line.replace(">", f' event_id="{event_id}">', 1)
                self._ensure_directory()
                with self._path.open("a", encoding="utf-8") as handle:
                    if needs_newline:
                        handle.write("\n")
                    handle.write(line)
                    handle.flush()
                stat = self._path.stat()
                self._projection_ids.add(event_id)
                self._projection_signature = (stat.st_mtime_ns, stat.st_size)
                appended = True
        if appended:
            self._working_memory_log.append_entry(payload["tag"], payload["content"], payload["timestamp"])
            self._notify_summarization()

    def project_pending(self, registry, generation: int) -> None:
        with registry.connect(generation=generation) as conn:
            ids = [row[0] for row in conn.execute(
                "SELECT id FROM events WHERE kind='conversation' AND generation=? ORDER BY id", (generation,))]
        with self._lock:
            projected = self._load_projection_ids_locked()
            ids = [event_id for event_id in ids if event_id not in projected]
        for event_id in ids:
            self._project_event(registry, event_id, generation)

    def _ensure_directory(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("conversation log directory creation failed", extra={"error": str(exc)})

    def _append(self, tag: str, payload: str) -> str:
        timestamp = now_in_user_timezone("%Y-%m-%d %H:%M:%S")
        entry = self._formatter(tag, timestamp, str(payload))
        with self._lock:
            try:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(entry)
            except Exception as exc:  # pragma: no cover - defensive
                logger.error(
                    "conversation log append failed",
                    extra={"error": str(exc), "tag": tag, "path": str(self._path)},
                )
                raise
        self._notify_summarization()
        return timestamp

    def _parse_line(self, line: str) -> Optional[Tuple[str, str, str]]:
        stripped = line.strip()
        if not stripped.startswith("<") or "</" not in stripped:
            return None
        open_end = stripped.find(">")
        if open_end == -1:
            return None
        open_tag_content = stripped[1:open_end]
        if " " in open_tag_content:
            tag, attr_string = open_tag_content.split(" ", 1)
        else:
            tag, attr_string = open_tag_content, ""
        close_start = stripped.rfind("</")
        close_end = stripped.rfind(">")
        if close_start == -1 or close_end == -1:
            return None
        closing_tag = stripped[close_start + 2 : close_end]
        if closing_tag != tag:
            return None
        payload = stripped[open_end + 1 : close_start]
        attributes: Dict[str, str] = {
            match.group(1): match.group(2) for match in _ATTR_PATTERN.finditer(attr_string)
        }
        timestamp = attributes.get("timestamp", "")
        return tag, timestamp, _decode_payload(payload)

    def iter_entries(self) -> Iterator[Tuple[str, str, str]]:
        with self._lock:
            try:
                lines = self._path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
            except Exception as exc:  # pragma: no cover - defensive
                logger.error(
                    "conversation log read failed", extra={"error": str(exc), "path": str(self._path)}
                )
                raise
        for line in lines:
            item = self._parse_line(line)
            if item is not None:
                yield item

    def load_transcript(self) -> str:
        parts: List[str] = []
        for tag, timestamp, payload in self.iter_entries():
            safe_payload = escape(payload, quote=False)
            if timestamp:
                parts.append(f"<{tag} timestamp=\"{timestamp}\">{safe_payload}</{tag}>")
            else:
                parts.append(f"<{tag}>{safe_payload}</{tag}>")
        return "\n".join(parts)

    def record_user_message(self, content: str) -> None:
        timestamp = self._append("user_message", content)
        self._working_memory_log.append_entry("user_message", content, timestamp)

    def record_agent_message(self, content: str) -> None:
        timestamp = self._append("agent_message", content)
        self._working_memory_log.append_entry("agent_message", content, timestamp)

    def record_reply(self, content: str) -> None:
        timestamp = self._append("poke_reply", content)
        self._working_memory_log.append_entry("poke_reply", content, timestamp)

    def record_wait(self, reason: str) -> None:
        """Record a wait marker that should not reach the user-facing chat history."""
        timestamp = self._append("wait", reason)
        self._working_memory_log.append_entry("wait", reason, timestamp)

    def _notify_summarization(self) -> None:
        settings = get_settings()
        if not settings.summarization_enabled:
            return

        try:
            from .summarization import schedule_summarization  # type: ignore import-not-found
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "summarization scheduler unavailable",
                extra={"error": str(exc)},
            )
            return

        try:
            schedule_summarization()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "failed to schedule summarization",
                extra={"error": str(exc)},
            )

    def to_chat_messages(self) -> List[ChatMessage]:
        messages: List[ChatMessage] = []
        for tag, timestamp, payload in self.iter_entries():
            normalized_timestamp = timestamp or None
            if tag == "user_message":
                messages.append(
                    ChatMessage(role="user", content=payload, timestamp=normalized_timestamp)
                )
            elif tag == "poke_reply":
                messages.append(
                    ChatMessage(
                        role="assistant", content=payload, timestamp=normalized_timestamp
                    )
                )
            elif tag == "wait":
                # Wait markers are orchestration metadata and must not surface to the user
                continue
        return messages

    def clear(self) -> None:
        with self._lock:
            self._projection_ids.clear()
            self._projection_signature = None
            try:
                if self._path.exists():
                    self._path.unlink()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "conversation log clear failed", extra={"error": str(exc), "path": str(self._path)}
                )
            finally:
                self._ensure_directory()
        try:
            self._working_memory_log.clear()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "working memory clear skipped",
                extra={"error": str(exc)},
            )


_conversation_log = ConversationLog(_CONVERSATION_LOG_PATH)


def get_conversation_log() -> ConversationLog:
    return _conversation_log


__all__ = ["ConversationLog", "get_conversation_log"]
