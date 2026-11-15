"""Compatibility facade over the shared durable execution service.

New callers explicitly create and seal request groups. Legacy calls receive a
one-job group, never an implicit batch shared with unrelated requests.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .runtime import ExecutionResult
from ...services.execution.queue import get_execution_service
from ...services.execution.registry import get_registry


@dataclass
class PendingExecution:
    """Legacy import compatibility; durable jobs are exposed as dictionaries."""
    request_id: str
    agent_name: str
    instructions: str
    batch_id: str
    created_at: datetime = field(default_factory=datetime.now)


class ExecutionBatchManager:
    def __init__(self, timeout_seconds: int = 90) -> None:
        self.timeout_seconds = timeout_seconds

    async def execute_agent(self, agent_name: str, instructions: str,
                            request_id: Optional[str] = None) -> ExecutionResult:
        registry = get_registry()
        try:
            agent = registry.get(agent_name)
        except KeyError:
            agent_id = registry.resolve_alias(agent_name)
            if not agent_id:
                raise KeyError("Unknown saved agent; create agents explicitly before dispatch")
            agent = registry.get(agent_id)
        request_id = request_id or str(uuid.uuid4())
        service = get_execution_service()
        await service.start()
        batch = service.create_batch("legacy:" + request_id)
        job = await service.submit(agent["id"], instructions, batch_id=batch["id"], request_id=request_id,
                                   expected_version=agent["version"])
        await service.seal_batch(batch["id"])
        wait_seconds = (service.settings.agent_queue_wait_seconds
                        + service.settings.agent_execution_timeout_seconds + 10)
        done = await service.wait_for_job(job["id"], timeout=wait_seconds)
        value = done.get("result") or {}
        return ExecutionResult(agent_name=agent["name"], success=done["status"] == "succeeded",
            response=value.get("response") or done.get("error") or "Task finished", error=done.get("error"))

    def get_pending_executions(self) -> list[dict]:
        return get_execution_service().pending_jobs()

    async def shutdown(self) -> None:
        await get_execution_service().stop()
