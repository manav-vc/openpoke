from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...logging_config import logger
from .models import TriggerRecord
from .utils import to_storage_timestamp, utc_now


class TriggerStore:
    """Low-level persistence for triggers backed by SQLite."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._ensure_directory()
        self._ensure_schema()

    def _ensure_directory(self) -> None:
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "trigger directory creation failed",
                extra={"error": str(exc)},
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        schema_sql = """
        CREATE TABLE IF NOT EXISTS triggers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            payload TEXT NOT NULL,
            start_time TEXT,
            next_trigger TEXT,
            recurrence_rule TEXT,
            timezone TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
        index_sql = """
        CREATE INDEX IF NOT EXISTS idx_triggers_agent_next
        ON triggers (agent_name, next_trigger);
        """
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(schema_sql)
            conn.execute(index_sql)
            if "agent_id" not in {r[1] for r in conn.execute("PRAGMA table_info(triggers)")}:
                conn.execute("ALTER TABLE triggers ADD COLUMN agent_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_triggers_agent_id ON triggers(agent_id)")

    def insert(self, payload: Dict[str, Any]) -> int:
        from ..execution.context import generation_write_guard
        with generation_write_guard(), self._lock, self._connect() as conn:
            columns = ", ".join(payload.keys())
            placeholders = ", ".join([":" + key for key in payload.keys()])
            sql = f"INSERT INTO triggers ({columns}) VALUES ({placeholders})"
            conn.execute(sql, payload)
            trigger_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.sync_owner(payload["agent_name"])
        return int(trigger_id)

    def fetch_one(self, trigger_id: int, agent_name: str) -> Optional[TriggerRecord]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM triggers WHERE id = ? AND agent_name = ?",
                (trigger_id, agent_name),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def update(self, trigger_id: int, agent_name: str, fields: Dict[str, Any]) -> bool:
        from ..execution.context import generation_write_guard
        if not fields:
            return False
        assignments = ", ".join(f"{key} = :{key}" for key in fields.keys())
        sql = (
            f"UPDATE triggers SET {assignments}, updated_at = :updated_at"
            " WHERE id = :trigger_id AND agent_name = :agent_name"
        )
        payload = {
            **fields,
            "updated_at": to_storage_timestamp(utc_now()),
            "trigger_id": trigger_id,
            "agent_name": agent_name,
        }
        with generation_write_guard(), self._lock, self._connect() as conn:
            cursor = conn.execute(sql, payload)
            changed = cursor.rowcount > 0
        self.sync_owner(agent_name)
        return changed

    def list_all(self) -> List[TriggerRecord]:
        with self._lock, self._connect() as conn:
            return [self._row_to_record(r) for r in conn.execute("SELECT * FROM triggers ORDER BY id")]

    def sync_owner(self, agent_name: str) -> None:
        from ..execution.registry import get_registry
        from ..execution.context import execution_context, check_execution_generation
        check_execution_generation()
        current = execution_context.get()
        registry = current[0] if current else get_registry()
        generation = current[1] if current else registry.generation
        agent_id = registry.resolve_alias(agent_name)
        if not agent_id:
            return
        with registry.connect(write=True, generation=generation) as conn:
            with self._lock, self._connect() as trigger_conn:
                trigger_conn.execute("UPDATE triggers SET agent_id=? WHERE agent_name=?", (agent_id, agent_name))
                enabled = {str(r[0]) for r in trigger_conn.execute("SELECT id FROM triggers WHERE agent_id=? AND status='active' AND next_trigger IS NOT NULL", (agent_id,))}
            old = {r[0] for r in conn.execute("SELECT value FROM agent_references WHERE agent_id=? AND kind='enabled_trigger'", (agent_id,))}
            if old == enabled:
                return
            conn.execute("DELETE FROM agent_references WHERE agent_id=? AND kind='enabled_trigger'", (agent_id,))
            for value in enabled:
                conn.execute("INSERT INTO agent_references(agent_id,kind,value,scope,confirmed) VALUES(?,'enabled_trigger',?,'',0)", (agent_id, value))
            conn.execute("UPDATE agents SET version=version+1 WHERE id=?", (agent_id,))

    def list_for_agent(self, agent_name: str) -> List[TriggerRecord]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM triggers WHERE agent_name = ? ORDER BY next_trigger IS NULL, next_trigger",
                (agent_name,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def fetch_due(
        self, agent_name: Optional[str], before_iso: str
    ) -> List[TriggerRecord]:
        sql = (
            "SELECT * FROM triggers WHERE status = 'active' AND next_trigger IS NOT NULL"
            " AND next_trigger <= ?"
        )
        params: List[Any] = [before_iso]
        if agent_name:
            sql += " AND agent_name = ?"
            params.append(agent_name)
        sql += " ORDER BY next_trigger, id"
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def clear_all(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM triggers")

    def _row_to_record(self, row: sqlite3.Row) -> TriggerRecord:
        data = dict(row)
        return TriggerRecord.model_validate(data)


__all__ = ["TriggerStore"]
