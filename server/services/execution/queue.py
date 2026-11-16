"""Durable single-process execution admission and request-scoped result groups.

Database rows describe work; asyncio tasks are current leases. Lost running
leases are interrupted rather than replayed, because a tool may have acted.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ...config import get_settings
from ...logging_config import logger
from .registry import StaleGenerationError
from .effects import tool_effect
from .drafts import pending_draft_references

TERMINAL = frozenset({"succeeded", "failed", "expired", "interrupted", "rejected"})
_TERMINAL_SQL = "'succeeded','failed','expired','interrupted','rejected'"


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str)


class ExecutionService:
    def __init__(self, registry=None, *, runner=None, delivery=None, settings=None,
                 clock: Callable[[], float] = time.time):
        if registry is None:
            from .registry import get_registry
            registry = get_registry()
        self.registry = registry
        self.settings = settings or get_settings()
        self.runner = runner or self._default_runner
        self.delivery = delivery or self._default_delivery
        self.clock = clock
        self._generation = registry.generation
        self._wake = asyncio.Event()
        self._scheduler = None
        self._workers = {}
        self._deliveries = {}
        self._accepting = True
        self._running = False
        self._ensure_schema()

    def _setting(self, name, default):
        return getattr(self.settings, name, default)

    def _ensure_schema(self):
        with self.registry.connect(write=True) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS execution_batches (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    root_request_id TEXT NOT NULL, stage INTEGER NOT NULL,
                    context TEXT NOT NULL, generation INTEGER NOT NULL,
                    created_at REAL NOT NULL, sealed INTEGER NOT NULL DEFAULT 0,
                    delivery_state TEXT NOT NULL DEFAULT 'pending',
                    delivery_attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL DEFAULT 0, last_error TEXT);
                CREATE TABLE IF NOT EXISTS execution_jobs (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    operation_key TEXT UNIQUE,
                    batch_id TEXT NOT NULL REFERENCES execution_batches(id),
                    agent_id TEXT NOT NULL REFERENCES agents(id),
                    instructions TEXT NOT NULL, status TEXT NOT NULL,
                    expected_version INTEGER, result TEXT, error TEXT,
                    created_at REAL NOT NULL, started_at REAL, completed_at REAL,
                    generation INTEGER NOT NULL, trigger_data TEXT,
                    trigger_key TEXT UNIQUE, trigger_projected INTEGER NOT NULL DEFAULT 0,
                    review_resolution TEXT, retry_of_job_id TEXT);
                CREATE INDEX IF NOT EXISTS execution_jobs_ready
                    ON execution_jobs(generation,status,created_at);
                CREATE INDEX IF NOT EXISTS execution_jobs_agent
                    ON execution_jobs(agent_id,status);
                CREATE INDEX IF NOT EXISTS execution_jobs_batch
                    ON execution_jobs(batch_id,status);
            """)
            if "retry_of_job_id" not in {row[1] for row in conn.execute("PRAGMA table_info(execution_jobs)")}:
                conn.execute("ALTER TABLE execution_jobs ADD COLUMN retry_of_job_id TEXT")

    def create_batch(self, request_id, *, root_request_id=None, stage=0, context=None):
        if not request_id:
            raise ValueError("A stable request_id is required")
        if not self._accepting:
            raise RuntimeError("Execution admission is stopped")
        generation = self.registry.generation
        with self.registry.connect(write=True, generation=generation) as conn:
            old = conn.execute("SELECT * FROM execution_batches WHERE request_id=?", (request_id,)).fetchone()
            if old:
                return self._batch_dict(old)
            batch_id = str(uuid.uuid4())
            conn.execute("""INSERT INTO execution_batches
                (id,request_id,root_request_id,stage,context,generation,created_at)
                VALUES (?,?,?,?,?,?,?)""", (batch_id, request_id, root_request_id or request_id,
                stage, _json(context or {}), generation, self.clock()))
            row = conn.execute("SELECT * FROM execution_batches WHERE id=?", (batch_id,)).fetchone()
        return self._batch_dict(row)

    def _read_only_evidence(self, conn, job):
        """Classify dispatched tools, never the model's description of its task.

        Old jobs need at least one known read intent. New runtimes also journal
        that effect tracking is active before their first model call, so a
        stalled model with no tool calls can safely fail without review.
        """
        rows = conn.execute("""SELECT id,kind,payload FROM events WHERE agent_id=?
            AND request_id=? AND generation=? AND kind IN
            ('tool_intent','tool_result','execution_effect_tracking') ORDER BY id""",
            (job["agent_id"], job["request_id"], job["generation"])).fetchall()
        intents, tracking, sources = [], False, []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                return False, sources
            if not isinstance(payload, dict):
                return False, sources
            if row["kind"] in {"tool_intent", "tool_result"}:
                name = payload.get("tool")
                if not isinstance(name, str) or tool_effect(name) != "read":
                    return False, sources
            if row["kind"] == "tool_intent":
                intents.append("read")
            elif row["kind"] == "execution_effect_tracking":
                tracking |= payload.get("enabled") is True
            else:
                sources.append(row["id"])
        return bool(intents or tracking) and all(effect == "read" for effect in intents), sources

    def _interruption_outcome(self, conn, job, reason, description):
        safe, sources = self._read_only_evidence(conn, job)
        # A reviewed retry must not silently close the original uncertain
        # action merely because this attempt stalled before reaching its write.
        safe = safe and not self._reviewed_execution(conn, job)
        error = description + ("; read-only work can be retried" if safe else "; external effects require review")
        result = {"success": False, "response": error, "error": error,
                  "failure_kind": reason, "read_only": safe, "retryable": safe,
                  "requires_review": not safe, "source_event_ids": sources}
        return ("failed" if safe else "interrupted"), result, error

    def _reviewed_execution(self, conn, job):
        batch = conn.execute("SELECT context FROM execution_batches WHERE id=?", (job["batch_id"],)).fetchone()
        return json.loads(batch["context"]).get("reviewed_execution") if batch else None

    def _release_review_state(self, conn, agent_id, generation):
        conn.execute("""UPDATE agents SET state=CASE
            WHEN approval_pending=1 THEN 'waiting_for_user'
            WHEN EXISTS (SELECT 1 FROM agent_references WHERE agent_id=agents.id
                AND kind='enabled_trigger') THEN 'waiting_for_trigger' ELSE 'open' END,
            version=version+1 WHERE id=? AND state='needs_review' AND NOT EXISTS
            (SELECT 1 FROM execution_jobs WHERE agent_id=? AND generation=?
                AND status='interrupted' AND review_resolution IS NULL)""",
            (agent_id, agent_id, generation))

    def reconcile_read_only_interruption(self, job_id):
        """Correct a legacy timeout only when its journal proves safe reads."""
        generation = self.registry.generation
        with self.registry.connect(write=True, generation=generation) as conn:
            row = conn.execute("SELECT * FROM execution_jobs WHERE id=? AND generation=?", (job_id, generation)).fetchone()
            if not row:
                raise KeyError("Unknown execution")
            if row["status"] != "interrupted" or row["review_resolution"] is not None:
                raise ValueError("Only unresolved interruptions can be reconciled")
            error = row["error"] or ""
            prefixes = ("Execution timed out;", "Execution interrupted;", "Backend stopped during execution;")
            if not error.startswith(prefixes) or not self._read_only_evidence(conn, row)[0]:
                raise ValueError("Tool journal does not prove a read-only interruption")
            if self._reviewed_execution(conn, row):
                raise ValueError("Reviewed retry still requires resolution of its original execution")
            status, result, error = self._interruption_outcome(conn, row, "timeout" if error.startswith(prefixes[0]) else "interrupted",
                                                            "Recovered read-only interruption")
            result["recovered_from_review"] = True
            conn.execute("UPDATE execution_jobs SET status=?,result=?,error=? WHERE id=?",
                         (status, _json(result), error, job_id))
            self._release_review_state(conn, row["agent_id"], generation)
            # Keep the original error in the audit trail rather than erasing it.
            conn.execute("""INSERT OR IGNORE INTO events
                (event_key,generation,agent_id,request_id,kind,payload,created_at)
                VALUES (?,?,?,?,?,?,?)""", ("reconcile:" + job_id, generation, row["agent_id"], row["request_id"],
                "read_only_interruption_reconciled", _json({"job_id": job_id, "original_error": row["error"]}),
                datetime.now(timezone.utc).isoformat()))
        self._wake.set()
        return self.job_status(job_id)

    def _autonomous_followup_error(self, conn, batch, agent_id):
        if not batch["stage"]:
            return None
        previous = conn.execute("""SELECT j.* FROM execution_jobs j JOIN execution_batches b ON b.id=j.batch_id
            WHERE b.root_request_id=? AND b.generation=? AND j.agent_id=?
            ORDER BY j.created_at DESC,j.rowid DESC LIMIT 1""",
            (batch["root_request_id"], batch["generation"], agent_id)).fetchone()
        if not previous:
            # A failure notification cannot evade a retry limit by switching to
            # a new saved identity. New responsibilities follow successful work.
            failed_source = conn.execute("""SELECT 1 FROM execution_jobs j JOIN execution_batches b ON b.id=j.batch_id
                WHERE b.root_request_id=? AND b.generation=?
                AND j.status IN ('failed','expired','interrupted','rejected') LIMIT 1""",
                (batch["root_request_id"], batch["generation"])).fetchone()
            return "Continue the existing failed task; do not create a replacement execution agent" if failed_source else None
        if previous["status"] == "succeeded":
            return None
        value = json.loads(previous["result"] or "{}")
        if previous["status"] != "failed" or not value.get("retryable") or not value.get("read_only"):
            return "Previous execution cannot be retried automatically; user direction required"
        retries = conn.execute("""SELECT COUNT(*) FROM execution_jobs j JOIN execution_batches b ON b.id=j.batch_id
            WHERE b.root_request_id=? AND b.generation=? AND j.retry_of_job_id IS NOT NULL""",
            (batch["root_request_id"], batch["generation"])).fetchone()[0]
        if retries >= 1:
            return "Automatic read-only retry limit reached; user direction required"
        return None

    async def submit(self, agent_id, instructions, *, batch_id, request_id,
                     expected_version=None, operation_key=None, trigger=None):
        if not request_id or not instructions.strip():
            raise ValueError("request_id and nonempty instructions are required")
        if not self._accepting:
            raise RuntimeError("Execution admission is stopped")
        generation, now = self.registry.generation, self.clock()
        trigger_key = None
        if trigger:
            scheduled = trigger.get("scheduled_for") or trigger.get("next_trigger")
            if not scheduled or trigger.get("id") is None:
                raise ValueError("Trigger occurrence requires id and scheduled_for")
            trigger_key = f"{generation}:{trigger['id']}:{scheduled}"
        with self.registry.connect(write=True, generation=generation) as conn:
            clauses, values = ["request_id=?"], [request_id]
            if operation_key:
                clauses.append("operation_key=?")
                values.append(operation_key)
            if trigger_key:
                clauses.append("trigger_key=?")
                values.append(trigger_key)
            old = conn.execute("SELECT * FROM execution_jobs WHERE " + " OR ".join(clauses), values).fetchone()
            if old:
                if old["agent_id"] != agent_id or old["instructions"] != instructions:
                    if not trigger_key or old["trigger_key"] != trigger_key:
                        raise ValueError("Idempotency key reused with different work")
                return self._job_dict(old)
            batch = conn.execute("SELECT * FROM execution_batches WHERE id=? AND generation=?", (batch_id, generation)).fetchone()
            if not batch:
                raise KeyError("Unknown execution batch")
            if batch["sealed"]:
                raise ValueError("Cannot add work to a sealed batch")
            agent = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if not agent:
                raise KeyError("Unknown agent_id")
            if expected_version is not None and agent["version"] != expected_version:
                raise ValueError("Agent changed since inspection; inspect it again")
            status, error = "queued", None
            result = None
            blocker = conn.execute("""SELECT id FROM execution_jobs WHERE agent_id=? AND generation=?
                AND status='interrupted' AND review_resolution IS NULL ORDER BY created_at LIMIT 1""",
                (agent_id, generation)).fetchone()
            reviewed = json.loads(batch["context"]).get("reviewed_execution")
            authorized_retry = reviewed and request_id == "retry:" + reviewed and conn.execute(
                "SELECT 1 FROM execution_jobs WHERE id=? AND agent_id=? AND generation=? AND review_resolution='retry'",
                (reviewed, agent_id, generation)).fetchone()
            followup_error = self._autonomous_followup_error(conn, batch, agent_id)
            queued = conn.execute("SELECT COUNT(*) FROM execution_jobs WHERE status='queued' AND generation=?", (generation,)).fetchone()[0]
            total = conn.execute("""SELECT COUNT(*) FROM execution_jobs j JOIN execution_batches b
                ON b.id=j.batch_id WHERE b.root_request_id=? AND b.generation=?""", (batch["root_request_id"], generation)).fetchone()[0]
            if blocker or (agent["state"] == "needs_review" and not authorized_retry):
                status, error = "rejected", "Task is blocked by an unresolved execution; review it before submitting more work"
                result = {"blocked": True, "retryable": False, "blocking_execution_id": blocker["id"] if blocker else None,
                          "error": error, "response": error}
            elif followup_error:
                status, error = "rejected", followup_error
                result = {"retryable": False, "error": error, "response": error}
            elif batch["stage"] > self._setting("agent_max_followup_stages", 5):
                status, error = "rejected", "Follow-up stage limit reached; user direction required"
            elif total >= self._setting("agent_max_submissions", 20):
                status, error = "rejected", "Execution submission limit reached; user direction required"
            elif queued >= self._setting("agent_queue_capacity", 100):
                status, error = "rejected", "Execution queue is full; retry later"
            if status == "rejected" and reviewed and not (result or {}).get("blocked"):
                # The original uncertain action is still unresolved in practice
                # if its authorized retry never starts. Preserve a review ID so
                # the user can retry or abandon this attempt instead of leaving
                # the agent permanently blocked without a resolvable execution.
                status, error = "interrupted", "Reviewed retry was not started: " + error
            job_id = str(uuid.uuid4())
            retry_of = None
            if status == "queued" and batch["stage"]:
                previous = conn.execute("""SELECT j.id,j.status FROM execution_jobs j JOIN execution_batches b ON b.id=j.batch_id
                    WHERE b.root_request_id=? AND b.generation=? AND j.agent_id=?
                    ORDER BY j.created_at DESC,j.rowid DESC LIMIT 1""",
                    (batch["root_request_id"], generation, agent_id)).fetchone()
                if previous and previous["status"] == "failed":
                    retry_of = previous["id"]
            conn.execute("""INSERT INTO execution_jobs
                (id,request_id,operation_key,batch_id,agent_id,instructions,status,expected_version,
                error,created_at,completed_at,generation,trigger_data,trigger_key,result,retry_of_job_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (job_id, request_id, operation_key,
                batch_id, agent_id, instructions, status, expected_version, error, now,
                now if status in TERMINAL else None, generation,
                _json(trigger) if trigger else None, trigger_key, _json(result) if result else None, retry_of))
            if status == "queued":
                conn.execute("""UPDATE agents SET archived=0,completed_at=NULL,last_activity=?,
                    state=CASE WHEN state='completed' THEN 'open' ELSE state END,
                    version=version+1 WHERE id=?""", (datetime.now(timezone.utc).isoformat(timespec="microseconds"), agent_id))
            elif status == "interrupted":
                conn.execute("UPDATE agents SET state='needs_review',version=version+1 WHERE id=?", (agent_id,))
            row = conn.execute("SELECT * FROM execution_jobs WHERE id=?", (job_id,)).fetchone()
        self._wake.set()
        logger.info("Execution submitted", extra={"job_id": job_id, "agent_id": agent_id, "batch_id": batch_id, "status": status})
        return self._job_dict(row)

    async def seal_batch(self, batch_id):
        with self.registry.connect(write=True, generation=self.registry.generation) as conn:
            cursor = conn.execute("UPDATE execution_batches SET sealed=1 WHERE id=? AND generation=?", (batch_id, self.registry.generation))
            if not cursor.rowcount:
                raise KeyError("Unknown execution batch")
        self._wake.set()

    async def start(self):
        if self._running:
            return
        self._generation = self.registry.generation
        self._ensure_schema()
        with self.registry.connect(write=True, generation=self._generation) as conn:
            # A turn's final state commits before its finally-block seals the
            # group. Recover the small crash window without sealing a genuinely
            # in-progress turn that may still submit more work on replay.
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_turns'").fetchone():
                conn.execute("""UPDATE execution_batches SET sealed=1 WHERE sealed=0 AND generation=?
                    AND EXISTS (SELECT 1 FROM interaction_turns t
                    WHERE t.request_id=execution_batches.request_id AND t.generation=execution_batches.generation
                    AND t.status IN ('completed','failed'))""", (self._generation,))
            rows = conn.execute("SELECT * FROM execution_jobs WHERE status='running' AND generation=?", (self._generation,)).fetchall()
            for row in rows:
                status, result, error = self._interruption_outcome(conn, row, "restart", "Backend stopped during execution")
                conn.execute("UPDATE execution_jobs SET status=?,completed_at=?,result=?,error=? WHERE id=?",
                    (status, self.clock(), _json(result), error, row["id"]))
                if status == "interrupted":
                    conn.execute("UPDATE agents SET state='needs_review',version=version+1 WHERE id=?", (row["agent_id"],))
                else:
                    self._release_review_state(conn, row["agent_id"], self._generation)
            conn.execute("UPDATE execution_batches SET delivery_state='pending' WHERE delivery_state='delivering' AND generation=?", (self._generation,))
        self._accepting = True
        # A reviewed retry is durable intent. A crash between recording that
        # decision and submitting its job must not silently discard the retry.
        with self.registry.connect() as conn:
            retries = conn.execute("""SELECT j.id FROM execution_jobs j WHERE j.status='interrupted'
                AND j.review_resolution='retry' AND j.generation=? AND NOT EXISTS
                (SELECT 1 FROM execution_jobs r WHERE r.request_id='retry:' || j.id)""", (self._generation,)).fetchall()
        for row in retries:
            await self.resolve_interrupted(row["id"], "retry", explicit_user=True)
        self._running = True
        self._scheduler = asyncio.create_task(self._run(), name="execution-admission")
        self._wake.set()

    async def stop(self):
        self._accepting, self._running = False, False
        self._wake.set()
        if self._scheduler:
            self._scheduler.cancel()
            await asyncio.gather(self._scheduler, return_exceptions=True)
            self._scheduler = None
        owned = list(self._workers.values()) + list(self._deliveries.values())
        if owned:
            _, pending = await asyncio.wait(owned, timeout=self._setting("agent_shutdown_seconds", 30))
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._workers.clear()
        self._deliveries.clear()

    async def quiesce(self):
        await self.stop()

    async def reset_workers(self):
        await self.stop()

    async def _run(self):
        while self._running:
            self._wake.clear()
            try:
                await self._tick()
            except Exception:
                logger.exception("Execution admission tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass

    async def _tick(self):
        if self.registry.generation != self._generation:
            return
        self._expire_queued()
        while len(self._workers) < self._setting("agent_max_concurrency", 4):
            job = self._claim_next()
            if not job:
                break
            self._workers[job["id"]] = asyncio.create_task(self._execute(job), name="execution-" + job["id"])
        if not self._deliveries:
            batch = self._claim_delivery()
            if batch:
                self._deliveries[batch["id"]] = asyncio.create_task(self._deliver(batch), name="delivery-" + batch["id"])

    def _expire_queued(self):
        cutoff = self.clock() - self._setting("agent_queue_wait_seconds", 300)
        with self.registry.connect(write=True, generation=self._generation) as conn:
            conn.execute("""UPDATE execution_jobs SET
                status=CASE WHEN EXISTS (SELECT 1 FROM execution_batches b WHERE b.id=execution_jobs.batch_id
                    AND json_extract(b.context,'$.reviewed_execution') IS NOT NULL) THEN 'interrupted' ELSE 'expired' END,
                completed_at=?,error=CASE WHEN EXISTS (SELECT 1 FROM execution_batches b WHERE b.id=execution_jobs.batch_id
                    AND json_extract(b.context,'$.reviewed_execution') IS NOT NULL)
                    THEN 'Reviewed retry expired before starting; task still requires review' ELSE 'Queue wait limit exceeded' END
                WHERE status='queued' AND generation=? AND created_at<?""", (self.clock(), self._generation, cutoff))
            conn.execute("""UPDATE agents SET state='needs_review',version=version+1
                WHERE state!='needs_review' AND id IN (SELECT agent_id FROM execution_jobs
                WHERE generation=? AND status='interrupted' AND review_resolution IS NULL)""", (self._generation,))

    def _claim_next(self):
        with self.registry.connect(write=True, generation=self._generation) as conn:
            running = conn.execute("SELECT COUNT(*) FROM execution_jobs WHERE status='running' AND generation=?", (self._generation,)).fetchone()[0]
            if running >= self._setting("agent_max_concurrency", 4):
                return None
            row = conn.execute("""SELECT j.* FROM execution_jobs j JOIN agents a ON a.id=j.agent_id
                WHERE j.status='queued' AND j.generation=?
                AND (a.state!='needs_review' OR EXISTS (SELECT 1 FROM execution_jobs reviewed
                    WHERE j.request_id='retry:' || reviewed.id AND reviewed.agent_id=j.agent_id
                    AND reviewed.review_resolution='retry'))
                AND NOT EXISTS (SELECT 1 FROM execution_jobs r WHERE r.agent_id=j.agent_id
                    AND r.status='running' AND r.generation=j.generation)
                AND NOT EXISTS (SELECT 1 FROM execution_jobs r WHERE r.agent_id=j.agent_id
                    AND r.status='interrupted' AND r.review_resolution IS NULL AND r.generation=j.generation)
                ORDER BY j.created_at,j.rowid LIMIT 1""", (self._generation,)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE execution_jobs SET status='running',started_at=? WHERE id=? AND status='queued'", (self.clock(), row["id"]))
            row = conn.execute("SELECT * FROM execution_jobs WHERE id=?", (row["id"],)).fetchone()
        return self._job_dict(row)

    async def _execute(self, job):
        status, result, error = "failed", None, None
        try:
            self.registry.append_event("agent_request", {"instructions": job["instructions"], "batch_id": job["batch_id"]},
                agent_id=job["agent_id"], request_id=job["request_id"], event_key="job:" + job["id"] + ":request", generation=job["generation"])
            value = self.runner(job)
            if inspect.isawaitable(value):
                value = await asyncio.wait_for(value, timeout=self._setting("agent_execution_timeout_seconds", 300))
            if is_dataclass(value):
                value = asdict(value)
            result = value if isinstance(value, dict) else {"success": True, "response": str(value)}
            if result.get("requires_review"):
                status = "interrupted"
            else:
                status = "succeeded" if result.get("success", False) else "failed"
            error = result.get("error")
            if status == "failed":
                with self.registry.connect(generation=job["generation"]) as conn:
                    if self._reviewed_execution(conn, job):
                        status = "interrupted"
                        result.update(requires_review=True, retryable=False)
                        error = error or "Reviewed retry failed; original external effects still require review"
        except asyncio.TimeoutError:
            with self.registry.connect(generation=job["generation"]) as conn:
                status, result, error = self._interruption_outcome(conn, job, "timeout", "Execution timed out")
        except asyncio.CancelledError:
            with self.registry.connect(generation=job["generation"]) as conn:
                status, result, error = self._interruption_outcome(conn, job, "cancelled", "Execution interrupted")
        except Exception as exc:
            error = str(exc)
            logger.exception("Execution failed", extra={"job_id": job["id"]})
        finally:
            try:
                if self.registry.generation == job["generation"]:
                    with self.registry.connect(write=True, generation=job["generation"]) as conn:
                        conn.execute("UPDATE execution_jobs SET status=?,result=?,error=?,completed_at=? WHERE id=? AND status='running'", (status, _json(result) if result else None, error, self.clock(), job["id"]))
                        if status == "interrupted":
                            conn.execute("UPDATE agents SET state='needs_review',version=version+1 WHERE id=?", (job["agent_id"],))
                        elif job["request_id"].startswith("retry:"):
                            # A reviewed retry runs before held follow-ups; only
                            # its completed lease releases those follow-ups.
                            self._release_review_state(conn, job["agent_id"], job["generation"])
            except StaleGenerationError:
                pass  # A coordinated reset superseded this completion.
            finally:
                self._workers.pop(job["id"], None)
                self._wake.set()

    def _claim_delivery(self):
        with self.registry.connect(write=True, generation=self._generation) as conn:
            conn.execute("""UPDATE execution_batches SET delivery_state='delivered'
                WHERE sealed=1 AND generation=? AND delivery_state='pending'
                AND NOT EXISTS (SELECT 1 FROM execution_jobs WHERE batch_id=execution_batches.id)""", (self._generation,))
            row = conn.execute(f"""SELECT b.* FROM execution_batches b WHERE b.sealed=1 AND b.generation=?
                AND b.delivery_state='pending' AND b.available_at<=?
                AND EXISTS (SELECT 1 FROM execution_jobs WHERE batch_id=b.id)
                AND NOT EXISTS (SELECT 1 FROM execution_jobs WHERE batch_id=b.id AND status NOT IN ({_TERMINAL_SQL}))
                ORDER BY b.created_at,b.rowid LIMIT 1""", (self._generation, self.clock())).fetchone()
            if not row:
                return None
            conn.execute("UPDATE execution_batches SET delivery_state='delivering',delivery_attempts=delivery_attempts+1 WHERE id=?", (row["id"],))
        return self._batch_dict(row)

    async def _deliver(self, batch):
        completed, error = False, None
        try:
            results = self.batch_jobs(batch["id"])
            count = self.root_submission_count(batch["root_request_id"])
            context = {"batch_id": batch["id"], "request_id": "delivery:" + batch["id"],
                "root_request_id": batch["root_request_id"], "stage": batch["stage"],
                "context": batch["context"], "source_context": batch["context"],
                "generation": batch["generation"], "results": results,
                "agent_ids": list(dict.fromkeys(j["agent_id"] for j in results)),
                "allow_followup": batch["stage"] < self._setting("agent_max_followup_stages", 5)
                    and count < self._setting("agent_max_submissions", 20)
                    and any(self._can_followup(batch, job) for job in results)}
            lines = []
            for job in results:
                value = job.get("result") or {}
                retry_allowed = value.get("retryable") and self._can_followup(batch, job)
                lines.append(f"[{job['status'].upper()}] agent_id={job['agent_id']} request_id={job['request_id']}: "
                    + str(value.get("response") or job.get("error") or "Task finished")
                    + (" [read-only; one automatic retry permitted]" if retry_allowed and context["allow_followup"] else
                       " [automatic retry unavailable; user direction required]" if job["status"] != "succeeded" else ""))
                drafts = pending_draft_references(self.registry, [job["agent_id"]],
                                                  request_id=job["request_id"], generation=batch["generation"])
                if drafts:
                    lines.append("Saved pending drafts for review (use send_draft with agent_id and draft_id): " + _json(drafts))
            response = self.delivery("\n".join(lines), context)
            if inspect.isawaitable(response):
                response = await response
            if isinstance(response, dict):
                completed, error = response.get("success", True), response.get("error")
            else:
                completed, error = getattr(response, "success", True), getattr(response, "error", None)
        except asyncio.CancelledError:
            error = "Delivery interrupted; resume committed interaction plan"
        except Exception as exc:
            error = str(exc)
            logger.exception("Batch result delivery failed", extra={"batch_id": batch["id"]})
        finally:
            try:
                if self.registry.generation == batch["generation"]:
                    with self.registry.connect(write=True, generation=batch["generation"]) as conn:
                        conn.execute("""UPDATE execution_batches SET delivery_state=?,last_error=?,available_at=? WHERE id=?""",
                            ("delivered" if completed else "pending", error, self.clock() + min(60, 2 ** min(6, batch["delivery_attempts"] + 1)), batch["id"]))
            except StaleGenerationError:
                pass
            finally:
                self._deliveries.pop(batch["id"], None)
                self._wake.set()

    def _can_followup(self, batch, job):
        if job["status"] == "succeeded":
            return True
        with self.registry.connect() as conn:
            candidate = dict(batch, stage=batch["stage"] + 1)
            return self._autonomous_followup_error(conn, candidate, job["agent_id"]) is None

    async def resolve_interrupted(self, job_id, resolution, *, explicit_user=False):
        if not explicit_user:
            raise ValueError("Interrupted execution requires explicit user direction")
        if resolution not in {"completed", "abandon", "retry"}:
            raise ValueError("resolution must be completed, abandon, or retry")
        generation = self.registry.generation
        with self.registry.connect(write=True, generation=generation) as conn:
            row = conn.execute("SELECT * FROM execution_jobs WHERE (id=? OR request_id=?) AND generation=?", (job_id, job_id, generation)).fetchone()
            if not row:
                raise KeyError("Unknown execution")
            if row["status"] != "interrupted":
                raise ValueError("Only interrupted executions require resolution")
            if row["review_resolution"] and row["review_resolution"] != "retry":
                return self._job_dict(row)
            if row["review_resolution"] == "retry" and resolution != "retry":
                raise ValueError("A retry was already requested for this execution")
            conn.execute("UPDATE execution_jobs SET review_resolution=? WHERE id=?", (resolution, row["id"]))
            unresolved = conn.execute("SELECT COUNT(*) FROM execution_jobs WHERE agent_id=? AND status='interrupted' AND review_resolution IS NULL AND generation=?", (row["agent_id"], generation)).fetchone()[0]
            if not unresolved and resolution != "retry":
                conn.execute("UPDATE agents SET state='open',version=version+1 WHERE id=? AND state='needs_review'", (row["agent_id"],))
            old = self._job_dict(row)
        retry_job = None
        if resolution == "retry":
            batch = self.create_batch("retry:" + old["id"], context={"reviewed_execution": old["id"]})
            retry_job = await self.submit(old["agent_id"], old["instructions"], batch_id=batch["id"], request_id="retry:" + old["id"], operation_key="retry:" + old["id"])
            await self.seal_batch(batch["id"])
        self._wake.set()
        result = self.job_status(old["id"])
        if retry_job:
            result["retry_job"] = retry_job
        return result

    def job_status(self, job_id):
        with self.registry.connect() as conn:
            row = conn.execute("SELECT * FROM execution_jobs WHERE id=? OR request_id=?", (job_id, job_id)).fetchone()
        if not row:
            raise KeyError("Unknown execution")
        return self._job_dict(row)

    def batch_jobs(self, batch_id):
        with self.registry.connect() as conn:
            rows = conn.execute("SELECT * FROM execution_jobs WHERE batch_id=? ORDER BY created_at,rowid", (batch_id,)).fetchall()
        return [self._job_dict(row) for row in rows]

    def batch_status(self, batch_id):
        with self.registry.connect() as conn:
            row = conn.execute("SELECT * FROM execution_batches WHERE id=? OR request_id=?", (batch_id, batch_id)).fetchone()
        if not row:
            raise KeyError("Unknown batch")
        result = self._batch_dict(row)
        result["jobs"] = self.batch_jobs(result["id"])
        return result

    def root_submission_count(self, root_request_id):
        with self.registry.connect() as conn:
            return conn.execute("""SELECT COUNT(*) FROM execution_jobs j JOIN execution_batches b
                ON b.id=j.batch_id WHERE b.root_request_id=? AND b.generation=?""", (root_request_id, self.registry.generation)).fetchone()[0]

    def pending_jobs(self):
        with self.registry.connect() as conn:
            rows = conn.execute("SELECT * FROM execution_jobs WHERE status IN ('queued','running') ORDER BY created_at,rowid").fetchall()
        return [self._job_dict(row) for row in rows]

    def agent_jobs(self, agent_id, limit=10):
        """Expose review IDs without loading another agent's execution history."""
        limit = min(100, max(1, int(limit)))
        with self.registry.connect() as conn:
            rows = conn.execute("""SELECT * FROM execution_jobs WHERE agent_id=? AND generation=?
                ORDER BY CASE WHEN status='interrupted' AND review_resolution IS NULL THEN 0 ELSE 1 END,
                created_at DESC,rowid DESC LIMIT ?""", (agent_id, self.registry.generation, limit)).fetchall()
        return [self._job_dict(row) for row in rows]

    def trigger_outcomes(self):
        with self.registry.connect() as conn:
            rows = conn.execute(f"""SELECT * FROM execution_jobs WHERE trigger_data IS NOT NULL AND trigger_projected=0
                AND generation=? AND status IN ({_TERMINAL_SQL}) ORDER BY completed_at,rowid""", (self.registry.generation,)).fetchall()
        return [self._job_dict(row) for row in rows]

    def mark_trigger_projected(self, job_id):
        with self.registry.connect(write=True, generation=self.registry.generation) as conn:
            conn.execute("UPDATE execution_jobs SET trigger_projected=1 WHERE id=? AND generation=?", (job_id, self.registry.generation))

    async def wait_for_job(self, job_id, *, timeout=120):
        async def wait():
            while True:
                job = self.job_status(job_id)
                if job["status"] in TERMINAL:
                    return job
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(wait(), timeout=timeout)

    async def wait_for_idle(self, *, timeout=120):
        async def wait():
            while self.pending_jobs() or self._workers or self._deliveries:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout=timeout)

    @staticmethod
    def _job_dict(row):
        result = dict(row)
        for key in ("result", "trigger_data"):
            if result.get(key):
                result[key] = json.loads(result[key])
        result["job_id"] = result["id"]
        result["trigger"] = result.get("trigger_data")
        return result

    @staticmethod
    def _batch_dict(row):
        result = dict(row)
        result["context"] = json.loads(result["context"])
        result["batch_id"] = result["id"]
        return result

    async def _default_runner(self, job):
        from ...agents.execution_agent.runtime import ExecutionAgentRuntime
        runtime = ExecutionAgentRuntime(agent_id=job["agent_id"], request_id=job["request_id"],
            generation=job["generation"], registry=self.registry)
        return await runtime.execute(job["instructions"])

    async def _default_delivery(self, payload, context):
        from ...agents.interaction_agent.runtime import InteractionAgentRuntime
        return await InteractionAgentRuntime(registry=self.registry, queue=self).handle_agent_message(payload, batch_context=context)


_execution_service: Optional[ExecutionService] = None


def get_execution_service():
    global _execution_service
    if _execution_service is None:
        _execution_service = ExecutionService()
    return _execution_service
