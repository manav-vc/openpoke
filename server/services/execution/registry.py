"""Durable task identities, evidence, and journals shared by agent services.

SQLite is the source of truth. Display names and legacy log filenames are never
used as the identity of a new task. Every asynchronous writer carries a reset
generation, preventing a completed old request from repopulating cleared data.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


STATES = {"open", "waiting_for_user", "waiting_for_trigger", "completed", "needs_review"}
IDENTITY_KINDS = {"invoice_id", "case_id", "task_id"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class StaleGenerationError(RuntimeError):
    pass


class StaleAgentError(RuntimeError):
    pass


class DuplicateAgentError(ValueError):
    def __init__(self, candidate_ids: list[str]):
        self.candidate_ids = candidate_ids
        super().__init__("Existing tasks share identifying references; inspect them before creating a separate task")


class AgentRegistry:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migration_lock = threading.RLock()
        self._initialize()

    @contextmanager
    def connect(self, write: bool = False, generation: int | None = None) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            if generation is not None:
                self.check_generation(conn, generation)
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS registry_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT OR IGNORE INTO registry_meta VALUES('generation','1');
                CREATE TABLE IF NOT EXISTS agents(
                    id TEXT PRIMARY KEY,name TEXT NOT NULL,purpose TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',state TEXT NOT NULL DEFAULT 'open',
                    last_activity TEXT NOT NULL,completed_at TEXT,archived INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,search_version INTEGER NOT NULL DEFAULT 1,
                    legacy_alias TEXT UNIQUE,legacy_history TEXT,history_ambiguous INTEGER NOT NULL DEFAULT 0,
                    pending_work INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,
                    creation_key TEXT UNIQUE
                );
                CREATE TABLE IF NOT EXISTS agent_references(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,value TEXT NOT NULL,scope TEXT NOT NULL DEFAULT '',
                    confirmed INTEGER NOT NULL DEFAULT 0,source_event_id INTEGER,source_text TEXT,
                    UNIQUE(agent_id,kind,value,scope)
                );
                CREATE INDEX IF NOT EXISTS reference_lookup ON agent_references(kind,value,scope,confirmed);
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT UNIQUE,generation INTEGER NOT NULL,
                    agent_id TEXT,request_id TEXT,kind TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS agent_events ON events(agent_id,id);
                CREATE INDEX IF NOT EXISTS event_requests ON events(request_id,id);
                CREATE TABLE IF NOT EXISTS artifacts(
                    id TEXT PRIMARY KEY,generation INTEGER NOT NULL,agent_id TEXT,request_id TEXT,
                    payload TEXT NOT NULL,created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_summaries(
                    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
                    last_event_id INTEGER NOT NULL DEFAULT 0,summary TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_approvals(
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    draft_id TEXT NOT NULL,source_event_id INTEGER NOT NULL,
                    PRIMARY KEY(agent_id,draft_id)
                );
                CREATE TABLE IF NOT EXISTS embeddings(
                    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
                    model TEXT NOT NULL,dimensions INTEGER NOT NULL,search_version INTEGER NOT NULL,
                    vector BLOB NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS index_outbox(
                    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
                    search_version INTEGER NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,available_at TEXT
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS agent_fts USING fts5(
                    agent_id UNINDEXED,name,purpose,summary,reference_text
                );
            """)
            if "approval_pending" not in {r[1] for r in conn.execute("PRAGMA table_info(agents)")}:
                conn.execute("ALTER TABLE agents ADD COLUMN approval_pending INTEGER NOT NULL DEFAULT 0")
                conn.execute("UPDATE agents SET approval_pending=COALESCE((SELECT json_extract(e.payload,'$.approval_pending') "
                             "FROM events e WHERE e.agent_id=agents.id AND e.kind='task_state' ORDER BY e.id DESC LIMIT 1),0)")

    @property
    def generation(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT value FROM registry_meta WHERE key='generation'").fetchone()[0])

    @staticmethod
    def check_generation(conn: sqlite3.Connection, generation: int) -> None:
        actual = int(conn.execute("SELECT value FROM registry_meta WHERE key='generation'").fetchone()[0])
        if actual != generation:
            raise StaleGenerationError("This request belongs to a cleared history generation")

    def _record(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        record = dict(row)
        record["archived"] = bool(record["archived"])
        record["history_ambiguous"] = bool(record["history_ambiguous"])
        record["pending_work"] = bool(record["pending_work"])
        record["approval_pending"] = bool(record.get("approval_pending"))
        record["pending_drafts"] = [dict(r) for r in conn.execute("SELECT draft_id,source_event_id FROM task_approvals WHERE agent_id=?", (row["id"],))]
        record["references"] = [dict(r) for r in conn.execute(
            "SELECT * FROM agent_references WHERE agent_id=? ORDER BY id", (row["id"],)
        )]
        return record

    def get(self, agent_id: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown saved agent: {agent_id}")
            return self._record(conn, row)

    def resolve_alias(self, name: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM agents WHERE id=? OR legacy_alias=?", (name, name)).fetchone()
            return row[0] if row else None

    def eligible_agents(self, include_archived: bool = False, scope: str | None = None) -> list[dict]:
        clauses, args = [], []
        if not include_archived:
            clauses.append("archived=0")
        if scope:
            # A task with known integration ownership may not leak into another account.
            clauses.append("(NOT EXISTS(SELECT 1 FROM agent_references r WHERE r.agent_id=agents.id AND r.scope!='') "
                           "OR EXISTS(SELECT 1 FROM agent_references r WHERE r.agent_id=agents.id AND r.scope=?))")
            args.append(scope)
        sql = "SELECT * FROM agents" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        with self.connect() as conn:
            return [self._record(conn, row) for row in conn.execute(sql, args)]

    @staticmethod
    def references_match(agent: dict, references: list[dict]) -> tuple[list[dict], list[dict]]:
        matches, conflicts = [], []
        for wanted in references:
            kind, value = wanted.get("kind"), str(wanted.get("value", ""))
            known = [r for r in agent.get("references", []) if r["kind"] == kind and r["confirmed"]
                     and (not wanted.get("scope") or r.get("scope") == wanted["scope"])]
            matching = [r for r in known if str(r["value"]).casefold() == value.casefold()]
            if matching:
                matches.extend(matching)
            elif known and kind in IDENTITY_KINDS:
                conflicts.append(wanted)
        return matches, conflicts

    def _index(self, conn: sqlite3.Connection, agent_id: str) -> None:
        row = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        text = " ".join(r[0] for r in conn.execute("SELECT value FROM agent_references WHERE agent_id=?", (agent_id,)))
        conn.execute("DELETE FROM agent_fts WHERE agent_id=?", (agent_id,))
        conn.execute("INSERT INTO agent_fts(agent_id,name,purpose,summary,reference_text) VALUES(?,?,?,?,?)",
                     (agent_id, row["name"], row["purpose"], row["summary"], text))
        conn.execute("INSERT INTO index_outbox(agent_id,search_version) VALUES(?,?) "
                     "ON CONFLICT(agent_id) DO UPDATE SET search_version=excluded.search_version,attempts=0,last_error=NULL,available_at=NULL",
                     (agent_id, row["search_version"]))

    def create_agent(self, name: str, purpose: str, references: list[dict] | None = None,
                     operation_key: str | None = None, generation: int | None = None,
                     allow_distinct: bool = False) -> dict:
        if not name.strip() or not purpose.strip():
            raise ValueError("A name and explicit task purpose are required")
        if len(name) > 256 or len(purpose) > 4000:
            raise ValueError("Agent name or purpose exceeds the registry limit")
        refs = references or []
        generation = self.generation if generation is None else generation
        with self.connect(write=True, generation=generation) as conn:
            if operation_key:
                existing = conn.execute("SELECT id FROM agents WHERE creation_key=?", (operation_key,)).fetchone()
                if existing:
                    return self._record(conn, conn.execute("SELECT * FROM agents WHERE id=?", (existing[0],)).fetchone())
            candidates: set[str] = set()
            for ref in refs:
                if ref.get("kind") not in IDENTITY_KINDS:
                    continue
                rows = conn.execute("SELECT DISTINCT agent_id FROM agent_references WHERE kind=? AND value=? AND scope=? AND confirmed=1",
                                    (ref["kind"], str(ref["value"]), ref.get("scope", "")))
                candidates.update(r[0] for r in rows)
            if candidates and not allow_distinct:
                raise DuplicateAgentError(sorted(candidates))
            agent_id, now = str(uuid.uuid4()), utc_now()
            conn.execute("INSERT INTO agents(id,name,purpose,last_activity,created_at,creation_key) VALUES(?,?,?,?,?,?)",
                         (agent_id, name.strip(), purpose.strip(), now, now, operation_key))
            for ref in refs:
                # Model-supplied creation metadata is searchable but not trusted evidence.
                self._add_reference(conn, agent_id, ref, trust=False)
            self._index(conn, agent_id)
        return self.get(agent_id)

    def update_agent(self, agent_id: str, expected_version: int | None = None,
                     generation: int | None = None, **fields: Any) -> dict:
        allowed = {"name", "purpose", "summary", "state", "last_activity", "completed_at", "archived", "pending_work",
                   "legacy_alias", "legacy_history", "history_ambiguous", "approval_pending"}
        if not fields or set(fields) - allowed:
            raise ValueError("Invalid agent update")
        if "state" in fields and fields["state"] not in STATES:
            raise ValueError("Invalid task state")
        with self.connect(write=True, generation=generation) as conn:
            row = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if row is None:
                raise KeyError(agent_id)
            if expected_version is not None and row["version"] != expected_version:
                raise StaleAgentError("Task state changed; inspect the current record before dispatch")
            search_changed = any(k in fields and fields[k] != row[k] for k in ("name", "purpose", "summary"))
            assignments = ",".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE agents SET {assignments},version=version+1,search_version=search_version+? WHERE id=?",
                         (*fields.values(), int(search_changed), agent_id))
            if search_changed:
                self._index(conn, agent_id)
        return self.get(agent_id)

    @staticmethod
    def _add_reference(conn: sqlite3.Connection, agent_id: str, ref: dict, trust: bool) -> None:
        kind, value = str(ref.get("kind", "")), str(ref.get("value", "")).strip()
        if not kind or not value or len(value) > 1024:
            raise ValueError("A reference requires a kind and bounded value")
        if kind == "email":
            value = value.casefold()
        source = ref.get("source_event_id")
        confirmed = bool(trust and ref.get("confirmed") and source)
        if confirmed:
            event = conn.execute("SELECT agent_id FROM events WHERE id=?", (source,)).fetchone()
            if not event or event["agent_id"] != agent_id:
                raise ValueError("Confirmed references require an event from this task")
        conn.execute("INSERT INTO agent_references(agent_id,kind,value,scope,confirmed,source_event_id,source_text) VALUES(?,?,?,?,?,?,?) "
                     "ON CONFLICT(agent_id,kind,value,scope) DO UPDATE SET confirmed=MAX(confirmed,excluded.confirmed),"
                     "source_event_id=COALESCE(excluded.source_event_id,source_event_id),source_text=COALESCE(excluded.source_text,source_text)",
                     (agent_id, kind, value, ref.get("scope", ""), int(confirmed), source, ref.get("source_text")))

    def add_reference(self, agent_id: str, kind: str, value: str, scope: str = "", confirmed: bool = False,
                      source_event_id: int | None = None, source_text: str | None = None,
                      generation: int | None = None) -> None:
        self.add_references(agent_id, [dict(kind=kind, value=value, scope=scope, confirmed=confirmed,
                                           source_event_id=source_event_id, source_text=source_text)], generation)

    def add_references(self, agent_id: str, references: list[dict], generation: int | None = None) -> None:
        if not references:
            return
        changed = False
        with self.connect(write=True, generation=generation) as conn:
            for ref in references:
                value = str(ref["value"]).casefold() if ref["kind"] == "email" else str(ref["value"])
                old = conn.execute("SELECT confirmed FROM agent_references WHERE agent_id=? AND kind=? AND value=? AND scope=?",
                                   (agent_id, ref["kind"], value, ref.get("scope", ""))).fetchone()
                self._add_reference(conn, agent_id, ref, trust=True)
                changed |= old is None or (not old["confirmed"] and bool(ref.get("confirmed") and ref.get("source_event_id")))
            if changed:
                conn.execute("UPDATE agents SET version=version+1,search_version=search_version+1 WHERE id=?", (agent_id,))
                self._index(conn, agent_id)

    def append_event(self, kind: str, payload: Any, agent_id: str | None = None, request_id: str | None = None,
                     event_key: str | None = None, generation: int | None = None) -> int:
        generation = self.generation if generation is None else generation
        with self.connect(write=True, generation=generation) as conn:
            conn.execute("INSERT OR IGNORE INTO events(event_key,generation,agent_id,request_id,kind,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                         (event_key, generation, agent_id, request_id, kind, json_dump(payload), utc_now()))
            if event_key:
                return int(conn.execute("SELECT id FROM events WHERE event_key=?", (event_key,)).fetchone()[0])
            return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def get_event(self, event_id: int) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if not row:
                raise KeyError(event_id)
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            return result

    def recent_links(self, limit: int = 10) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT e.id AS event_id,e.agent_id,e.request_id,e.kind,e.created_at,a.name,a.state,a.version "
                                "FROM events e JOIN agents a ON a.id=e.agent_id "
                                "WHERE e.kind IN ('agent_response','conversation_link','draft','task_state') ORDER BY e.id DESC LIMIT ?", (limit,))
            links = [dict(r) for r in rows]
            seen = {(r["event_id"], r["agent_id"]) for r in links}
            conversations = conn.execute("SELECT id,request_id,payload,created_at FROM events WHERE kind='conversation' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            for event in conversations:
                payload = json.loads(event["payload"])
                for agent_id in payload.get("agent_ids", []):
                    if (event["id"], agent_id) in seen:
                        continue
                    row = conn.execute("SELECT id,name,state,version FROM agents WHERE id=?", (agent_id,)).fetchone()
                    if row:
                        links.append({"event_id": event["id"], "agent_id": agent_id, "request_id": event["request_id"],
                                      "kind": payload.get("tag"), "created_at": event["created_at"],
                                      "name": row["name"], "state": row["state"], "version": row["version"]})
            return sorted(links, key=lambda item: item["event_id"], reverse=True)[:limit]

    def store_artifact(self, payload: Any, agent_id: str | None = None, request_id: str | None = None,
                       generation: int | None = None) -> str:
        data = json_dump(payload)
        generation = self.generation if generation is None else generation
        artifact_id = hashlib.sha256(f"{generation}:{agent_id}:".encode() + data.encode("utf-8")).hexdigest()
        with self.connect(write=True, generation=generation) as conn:
            conn.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?)",
                         (artifact_id, generation, agent_id, request_id, data, utc_now()))
        return artifact_id

    def get_artifact(self, artifact_id: str, agent_id: str) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM artifacts WHERE id=? AND agent_id=?", (artifact_id, agent_id)).fetchone()
            if not row:
                raise KeyError(artifact_id)
            return json.loads(row[0])

    def _archive_eligible(self, conn: sqlite3.Connection, agent_id: str) -> bool:
        row = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not row or row["state"] != "completed" or row["pending_work"] or row["approval_pending"] or row["history_ambiguous"]:
            return False
        if conn.execute("SELECT 1 FROM task_approvals WHERE agent_id=? LIMIT 1", (agent_id,)).fetchone():
            return False
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_jobs'").fetchone():
            if conn.execute("SELECT 1 FROM execution_jobs WHERE agent_id=? AND (status IN ('queued','running') OR (status='interrupted' AND review_resolution IS NULL)) LIMIT 1", (agent_id,)).fetchone():
                return False
        return not conn.execute("SELECT 1 FROM agent_references WHERE agent_id=? AND kind='enabled_trigger' LIMIT 1", (agent_id,)).fetchone()

    def archive_agent(self, agent_id: str, generation: int | None = None) -> dict:
        with self.connect(write=True, generation=generation) as conn:
            if not self._archive_eligible(conn, agent_id):
                raise ValueError("Only completed tasks without pending work, reminders, or interruptions can be archived")
            conn.execute("UPDATE agents SET archived=1,version=version+1 WHERE id=?", (agent_id,))
        return self.get(agent_id)

    def restore_agent(self, agent_id: str, generation: int | None = None) -> dict:
        return self.update_agent(agent_id, generation=generation, archived=False, state="open", completed_at=None, last_activity=utc_now())

    def archive_completed(self, days: int = 30, now: datetime | None = None, generation: int | None = None) -> int:
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()
        count = 0
        with self.connect(write=True, generation=generation) as conn:
            rows = conn.execute("SELECT id FROM agents WHERE archived=0 AND state='completed' AND completed_at<=? AND last_activity<=?", (cutoff, cutoff)).fetchall()
            for row in rows:
                if self._archive_eligible(conn, row[0]):
                    conn.execute("UPDATE agents SET archived=1,version=version+1 WHERE id=?", (row[0],))
                    count += 1
        return count

    def migrate_legacy(self, roster_path: Path, history_dir: Path, trigger_db: Path | None = None) -> None:
        """Import names once without changing the original files or guessing evidence."""
        from .log_store import _slugify
        with self._migration_lock:
            names: list[str] = []
            if roster_path.is_file():
                try:
                    data = json.loads(roster_path.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        names.extend(str(v) for v in data)
                except (ValueError, OSError):
                    # Corrupt input is visible, not silently treated as empty migration.
                    raise ValueError(f"Cannot migrate legacy roster: {roster_path}")
            trigger_names: list[str] = []
            if trigger_db and trigger_db.is_file():
                with sqlite3.connect(trigger_db) as conn:
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='triggers'").fetchone():
                        trigger_names = [r[0] for r in conn.execute("SELECT DISTINCT agent_name FROM triggers")]
                        names.extend(trigger_names)
            slugs: dict[str, list[str]] = {}
            for name in dict.fromkeys(names):
                slugs.setdefault(_slugify(name), []).append(name)
            with self.connect(write=True) as conn:
                for slug, aliases in slugs.items():
                    history = history_dir / f"{slug}.log"
                    if len(aliases) > 1:
                        conn.execute("UPDATE agents SET history_ambiguous=1,version=version+1 WHERE legacy_history=? AND history_ambiguous=0", (str(history),))
                    for name in aliases:
                        if conn.execute("SELECT 1 FROM agents WHERE id=? OR legacy_alias=?", (name, name)).fetchone():
                            continue
                        now, agent_id = utc_now(), str(uuid.uuid4())
                        conn.execute("INSERT INTO agents(id,name,purpose,last_activity,created_at,legacy_alias,legacy_history,history_ambiguous) VALUES(?,?,?,?,?,?,?,?)",
                                     (agent_id, name, f"Legacy task: {name}", now, now, name, str(history) if history.is_file() else None, int(len(aliases) > 1)))
                        self._index(conn, agent_id)
                # Keep unmapped log files visible as explicitly uncertain recovered records.
                for history in history_dir.glob("*.log"):
                    if history.stem in slugs or conn.execute("SELECT 1 FROM agents WHERE legacy_history=?", (str(history),)).fetchone():
                        continue
                    now, agent_id = utc_now(), str(uuid.uuid4())
                    conn.execute("INSERT INTO agents(id,name,purpose,last_activity,created_at,legacy_history,history_ambiguous) VALUES(?,?,?,?,?,?,1)",
                                 (agent_id, f"Recovered history: {history.stem}", "Legacy history with unknown task identity", now, now, str(history)))
                    self._index(conn, agent_id)

    def reset(self) -> int:
        """Caller must quiesce workers before advancing the generation and clearing."""
        with self.connect(write=True) as conn:
            generation = int(conn.execute("SELECT value FROM registry_meta WHERE key='generation'").fetchone()[0]) + 1
            conn.execute("UPDATE registry_meta SET value=? WHERE key='generation'", (str(generation),))
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            # Delete referencing tables first; keep schemas, sequence IDs, and reset epoch.
            conn.execute("PRAGMA defer_foreign_keys=ON")
            for table in tables:
                if table == "registry_meta" or table.startswith("sqlite_") or table.startswith("agent_fts_"):
                    continue
                conn.execute('DELETE FROM "' + table.replace('"', '""') + '"')
            return generation


_registry: AgentRegistry | None = None
_factory_lock = threading.Lock()


def get_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        with _factory_lock:
            if _registry is None:
                from ...config import get_settings
                configured = getattr(get_settings(), "agent_registry_path", None)
                path = Path(configured) if configured else Path(__file__).resolve().parents[2] / "data" / "agents" / "registry.db"
                _registry = AgentRegistry(path)
    return _registry
