"""Background scheduler that watches trigger definitions and executes them."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional, Set

from ..logging_config import logger
from .triggers import TriggerRecord, get_trigger_service


UTC = timezone.utc


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _isoformat(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class TriggerScheduler:
    """Polls stored triggers and launches execution agents when due."""

    def __init__(self, poll_interval_seconds: float = 10.0) -> None:
        self._poll_interval = poll_interval_seconds
        self._service = get_trigger_service()
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._in_flight: Set[int] = set()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._task and not self._task.done():
                return
            loop = asyncio.get_running_loop()
            self._running = True
            self._task = loop.create_task(self._run(), name="trigger-scheduler")
            logger.info("Trigger scheduler started", extra={"interval": self._poll_interval})

    async def stop(self) -> None:
        async with self._lock:
            self._running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                self._task = None
                logger.info("Trigger scheduler stopped")

    async def _run(self) -> None:
        try:
            while self._running:
                await self._poll_once()
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Trigger scheduler loop crashed", extra={"error": str(exc)})

    async def _poll_once(self) -> None:
        await self._reconcile_outcomes()
        now = _utc_now()
        due_triggers = self._service.get_due_triggers(before=now)
        if not due_triggers:
            return

        for trigger in due_triggers:
            if trigger.id in self._in_flight:
                continue
            await self._execute_trigger(trigger)

    async def _execute_trigger(self, trigger: TriggerRecord) -> None:
        from .execution.queue import get_execution_service
        from .execution.registry import get_registry
        try:
            fired_at = _utc_now()
            instructions = self._format_instructions(trigger, fired_at)
            logger.info(
                "Dispatching trigger",
                extra={
                    "trigger_id": trigger.id,
                    "agent": trigger.agent_name,
                    "scheduled_for": trigger.next_trigger,
                },
            )
            registry = get_registry()
            agent_id = trigger.agent_id or registry.resolve_alias(trigger.agent_name)
            if not agent_id:
                raise ValueError("Reminder owner could not be resolved to a saved task")
            queue = get_execution_service()
            scheduled = trigger.next_trigger or _isoformat(fired_at)
            occurrence = f"trigger:{registry.generation}:{trigger.id}:{scheduled}"
            batch = queue.create_batch(occurrence, context={"trigger_id": trigger.id, "generation": registry.generation})
            await queue.submit(agent_id, instructions, batch_id=batch["id"], request_id=occurrence,
                operation_key=occurrence, trigger={"id": trigger.id, "scheduled_for": scheduled,
                    "agent_name": trigger.agent_name, "recurrence_rule": trigger.recurrence_rule})
            await queue.seal_batch(batch["id"])
        except Exception as exc:  # pragma: no cover - defensive
            self._handle_failure(trigger, _utc_now(), str(exc))
            logger.exception(
                "Trigger execution failed unexpectedly",
                extra={"trigger_id": trigger.id, "agent": trigger.agent_name},
            )
        finally:
            self._in_flight.discard(trigger.id)

    async def _reconcile_outcomes(self) -> None:
        """Idempotently project committed occurrence outcomes into triggers.db."""
        from .execution.queue import get_execution_service
        from .execution.registry import get_registry
        queue = get_execution_service()
        registry = get_registry()
        for job in queue.trigger_outcomes():
            if job["generation"] != registry.generation:
                continue
            metadata = job["trigger"]
            trigger = self._service._store.fetch_one(metadata["id"], metadata["agent_name"])
            if trigger is None:
                queue.mark_trigger_projected(job["id"])
                continue
            # A paused/rescheduled trigger must not be overwritten by an old job.
            if trigger.next_trigger != metadata["scheduled_for"] or trigger.status != "active":
                if job["status"] == "succeeded":
                    self._finalize_goal_after_trigger(job, registry)
                queue.mark_trigger_projected(job["id"])
                continue
            fired_at = datetime.fromtimestamp(job["completed_at"], UTC)
            if job["status"] == "succeeded":
                self._handle_success(trigger, fired_at)
            elif job["status"] == "interrupted":
                error = "Interrupted occurrence requires review; it will not be repeated automatically"
                self._service.record_failure(trigger, error)
                if trigger.recurrence_rule:
                    self._service.schedule_next_occurrence(trigger, fired_at=fired_at)
                    self._service.record_failure(trigger, error)
                else:
                    self._service.clear_next_fire(trigger.id, agent_name=trigger.agent_name)
            else:
                self._handle_failure(trigger, fired_at, job.get("error") or job["status"])
            if job["status"] == "succeeded":
                self._finalize_goal_after_trigger(job, registry)
            queue.mark_trigger_projected(job["id"])

    def _finalize_goal_after_trigger(self, job, registry):
        import json
        from .execution.registry import utc_now
        with registry.connect(generation=job["generation"]) as conn:
            event = conn.execute("SELECT payload,request_id FROM events WHERE agent_id=? AND kind='task_state' ORDER BY id DESC LIMIT 1",
                                 (job["agent_id"],)).fetchone()
            if not event:
                return
            if event["request_id"] != job["request_id"]:
                return
            state = json.loads(event[0])
            if state.get("reported_state") != "completed" or state.get("reported_pending_work") is not False or state.get("approval_pending"):
                return
            if conn.execute("SELECT 1 FROM agent_references WHERE agent_id=? AND kind='enabled_trigger'", (job["agent_id"],)).fetchone():
                return
            if conn.execute("SELECT 1 FROM execution_jobs WHERE agent_id=? AND (status IN ('queued','running') OR (status='interrupted' AND review_resolution IS NULL))", (job["agent_id"],)).fetchone():
                return
            agent = conn.execute("SELECT state,version,approval_pending FROM agents WHERE id=?", (job["agent_id"],)).fetchone()
            if not agent or agent["state"] != "waiting_for_trigger" or agent["approval_pending"]:
                return
        from .execution.registry import StaleAgentError
        try:
            registry.update_agent(job["agent_id"], expected_version=agent["version"], generation=job["generation"],
                                  state="completed", pending_work=False, completed_at=utc_now())
        except StaleAgentError:
            pass

    def _handle_success(self, trigger: TriggerRecord, fired_at: datetime) -> None:
        logger.info(
            "Trigger completed",
            extra={"trigger_id": trigger.id, "agent": trigger.agent_name},
        )
        self._service.schedule_next_occurrence(trigger, fired_at=fired_at)

    def _handle_failure(self, trigger: TriggerRecord, fired_at: datetime, error: str) -> None:
        logger.warning(
            "Trigger execution failed",
            extra={
                "trigger_id": trigger.id,
                "agent": trigger.agent_name,
                "error": error,
            },
        )
        self._service.record_failure(trigger, error)
        if trigger.recurrence_rule:
            self._service.schedule_next_occurrence(trigger, fired_at=fired_at)
        else:
            self._service.clear_next_fire(trigger.id, agent_name=trigger.agent_name)

    def _format_instructions(self, trigger: TriggerRecord, fired_at: datetime) -> str:
        scheduled_for = trigger.next_trigger or _isoformat(fired_at)
        metadata_lines = [f"Trigger ID: {trigger.id}"]
        if trigger.recurrence_rule:
            metadata_lines.append(f"Recurrence: {trigger.recurrence_rule}")
        if trigger.timezone:
            metadata_lines.append(f"Timezone: {trigger.timezone}")
        if trigger.start_time:
            metadata_lines.append(f"Start Time (UTC): {trigger.start_time}")

        metadata = "\n".join(f"- {line}" for line in metadata_lines)
        return (
            f"Trigger fired at {_isoformat(fired_at)} (UTC).\n"
            f"Scheduled occurrence time: {scheduled_for}.\n\n"
            f"Metadata:\n{metadata}\n\n"
            f"Payload:\n{trigger.payload}"
        )


_scheduler_instance: Optional[TriggerScheduler] = None


def get_trigger_scheduler() -> TriggerScheduler:
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = TriggerScheduler()
    return _scheduler_instance


__all__ = ["TriggerScheduler", "get_trigger_scheduler"]
