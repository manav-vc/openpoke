"""Owned maintenance workers and coordinated application/reset lifecycle."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from ...config import get_settings
from ...logging_config import logger
from .history import HistoryService
from .registry import get_registry


class AgentMaintenance:
    def __init__(self, registry=None, search=None):
        self.registry = registry or get_registry()
        self.search = search
        self.tasks: list[asyncio.Task] = []
        self.settings = get_settings()

    async def start(self):
        if self.tasks:
            return
        generation = self.registry.generation
        if self.settings.agent_auto_archive_enabled:
            self.registry.archive_completed(self.settings.agent_archive_days, generation=generation)
        self.tasks = [asyncio.create_task(self._archive_loop(generation), name="agent-archive")]
        if self.settings.openrouter_api_key:
            self.tasks.append(asyncio.create_task(self._index_loop(generation), name="agent-index"))
            self.tasks.append(asyncio.create_task(self._history_loop(generation), name="agent-compaction"))

    async def stop(self):
        tasks, self.tasks = self.tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _archive_loop(self, generation):
        while generation == self.registry.generation:
            await asyncio.sleep(self.settings.agent_maintenance_interval_seconds)
            try:
                sync_trigger_ownership()
                archived = self.registry.archive_completed(self.settings.agent_archive_days, generation=generation) if self.settings.agent_auto_archive_enabled else 0
                logger.info("agent maintenance", extra={"archived_count": archived, **registry_metrics(self.registry)})
            except Exception:
                logger.exception("Agent archival maintenance failed")

    async def _index_loop(self, generation):
        from .retrieval import get_search_service
        search = self.search or get_search_service()
        while generation == self.registry.generation:
            try:
                saved = await search.index_pending(generation=generation)
                await asyncio.sleep(0 if saved == 64 else 2)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Agent indexing worker failed")
                await asyncio.sleep(5)

    async def _history_loop(self, generation):
        history = HistoryService(self.registry)
        while generation == self.registry.generation:
            if self.settings.agent_compaction_enabled:
                try:
                    with self.registry.connect(generation=generation) as conn:
                        rows = conn.execute("SELECT e.agent_id FROM events e LEFT JOIN agent_summaries s ON s.agent_id=e.agent_id "
                            "WHERE e.kind='agent_response' AND e.id>COALESCE(s.last_event_id,0) "
                            "GROUP BY e.agent_id HAVING COUNT(*)>=3 LIMIT 5").fetchall()
                    for row in rows:
                        await history.compact(row[0], generation=generation)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Agent history maintenance failed")
            await asyncio.sleep(10)


def sync_trigger_ownership():
    from ..triggers import get_trigger_service
    store = get_trigger_service()._store
    for owner in {r.agent_name for r in store.list_all()}:
        store.sync_owner(owner)


def registry_metrics(registry=None) -> dict:
    registry = registry or get_registry()
    with registry.connect() as conn:
        metrics = {"saved_agents": conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0],
                   "archived_agents": conn.execute("SELECT COUNT(*) FROM agents WHERE archived=1").fetchone()[0],
                   "index_pending": conn.execute("SELECT COUNT(*) FROM index_outbox").fetchone()[0]}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='execution_jobs'").fetchone():
            metrics["jobs"] = {r[0]: r[1] for r in conn.execute("SELECT status,COUNT(*) FROM execution_jobs GROUP BY status")}
            metrics["average_queue_wait_seconds"] = conn.execute("SELECT AVG(started_at-created_at) FROM execution_jobs WHERE started_at IS NOT NULL").fetchone()[0]
        return metrics


_maintenance: AgentMaintenance | None = None
_reset_lock: asyncio.Lock | None = None


async def start_all():
    global _maintenance
    from .queue import get_execution_service
    from ...agents.interaction_agent.runtime import recover_pending_interactions, resume_interactions
    from ..conversation import get_conversation_log
    from ..conversation.summarization.scheduler import resume_summarization
    from ..trigger_scheduler import get_trigger_scheduler
    from ..gmail import get_important_email_watcher
    registry = get_registry()
    data = Path(__file__).resolve().parents[2] / "data"
    registry.migrate_legacy(data / "execution_agents" / "roster.json", data / "execution_agents", data / "triggers.db")
    sync_trigger_ownership()
    get_conversation_log().project_pending(registry, registry.generation)
    resume_interactions()
    resume_summarization()
    await get_execution_service().start()
    if _maintenance is None:
        _maintenance = AgentMaintenance(registry)
    await _maintenance.start()
    if get_settings().openrouter_api_key:
        await recover_pending_interactions()
    await get_trigger_scheduler().start()
    await get_important_email_watcher().start()


async def stop_all():
    from .queue import get_execution_service
    from ...agents.interaction_agent.runtime import quiesce_interactions
    from ..conversation.summarization.scheduler import stop_summarization
    from ..trigger_scheduler import get_trigger_scheduler
    from ..gmail import get_important_email_watcher
    await get_trigger_scheduler().stop()
    await get_important_email_watcher().stop()
    await quiesce_interactions()
    if _maintenance:
        await _maintenance.stop()
    await stop_summarization()
    await get_execution_service().stop()


async def reset_all():
    global _reset_lock
    if _reset_lock is None:
        _reset_lock = asyncio.Lock()
    async with _reset_lock:
        await stop_all()
        from ..conversation import get_conversation_log
        from .roster import get_agent_roster
        from .log_store import get_execution_agent_logs
        from ..triggers import get_trigger_service
        registry = get_registry()
        registry.reset()
        get_conversation_log().clear()
        get_execution_agent_logs().clear_all()
        get_agent_roster().clear()
        get_trigger_service().clear_all()
        await start_all()
