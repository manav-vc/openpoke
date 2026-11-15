from __future__ import annotations

import asyncio

from ....logging_config import logger
from .summarizer import summarize_conversation

_pending = False
_running = False
_paused = False
_tasks: set[asyncio.Task] = set()


def schedule_summarization() -> None:
    """Schedule a background summarization pass if not already queued."""
    global _pending
    if _paused:
        return
    _pending = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("summarization skipped (no running event loop)")
        return

    if not _running:
        task = loop.create_task(_run_worker())
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


async def _run_worker() -> None:
    global _pending, _running
    if _running:
        return

    _running = True
    try:
        while _pending:
            _pending = False
            try:
                await summarize_conversation()
            except Exception as exc:  # pragma: no cover - defensive
                logger.error(
                    "summarization worker failed",
                    extra={"error": str(exc)},
                )
    finally:
        _running = False


__all__ = ["schedule_summarization"]


async def stop_summarization():
    global _paused, _pending
    _paused, _pending = True, False
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def resume_summarization():
    global _paused
    _paused = False
