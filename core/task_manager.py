"""
Task queue and lifecycle management.

Phase 5: adaptive recovery states and attempt history.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from core.task_result import TaskResult

if TYPE_CHECKING:
    from core.task_store import TaskStore

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    VERIFYING = "verifying"
    RETRYING = "retrying"
    INVESTIGATING = "investigating"
    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    MERGED = "merged"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Populated after class body
def _active_statuses() -> frozenset[TaskStatus]:
    return frozenset({
        TaskStatus.RUNNING,
        TaskStatus.VERIFYING,
        TaskStatus.RETRYING,
        TaskStatus.INVESTIGATING,
    })


@dataclass
class Task:
    """A unit of work for the orchestrator to execute."""

    title: str
    description: str = ""
    project_id: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    started_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)

    @property
    def session_name(self) -> str | None:
        return self.metadata.get("session_name") or (
            (self.metadata.get("result") or {}).get("session_name")
        )

    def mark_started(self) -> None:
        self.started_at = datetime.now(timezone.utc)
        self.touch()

    @property
    def recovery_enabled(self) -> bool:
        return self.metadata.get("recovery_enabled", True)


class TaskManager:
    """
    Task registry with optional SQLite persistence (Phase 8).
    """

    ACTIVE_STATUSES = _active_statuses()

    def __init__(
        self,
        max_concurrent: int = 3,
        store: TaskStore | None = None,
    ) -> None:
        self._tasks: dict[str, Task] = {}
        self.max_concurrent = max_concurrent
        self._store = store

    def bind_store(self, store: TaskStore) -> None:
        self._store = store

    async def persist(self, task: Task | None = None) -> None:
        if not self._store:
            return
        if task is not None:
            await self._store.upsert(task)
            return
        for t in self._tasks.values():
            await self._store.upsert(t)

    async def flush_all(self) -> None:
        await self.persist()

    def create(self, title: str, description: str = "", **kwargs: Any) -> Task:
        task = Task(title=title, description=description, **kwargs)
        if "recovery_enabled" not in task.metadata:
            task.metadata["recovery_enabled"] = True
        self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def list_all(self) -> list[Task]:
        return list(self._tasks.values())

    def list_by_status(self, status: TaskStatus) -> list[Task]:
        return [t for t in self._tasks.values() if t.status == status]

    def active_count(self) -> int:
        return len([t for t in self._tasks.values() if t.status in self.ACTIVE_STATUSES])

    def running_count(self) -> int:
        return self.active_count()

    def can_start_more(self) -> bool:
        return self.active_count() < self.max_concurrent

    def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        error: str | None = None,
    ) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        task.status = status
        task.error = error
        if status == TaskStatus.RUNNING and task.started_at is None:
            task.mark_started()
        else:
            task.touch()
        self._schedule_persist(task)
        return task

    def _schedule_persist(self, task: Task | None) -> None:
        if not self._store or task is None:
            return
        conn = getattr(self._store, "_conn", None)
        if conn is None:
            return
        import asyncio

        try:
            asyncio.get_running_loop().create_task(self.persist(task))
        except RuntimeError:
            pass

    def set_session_name(self, task_id: str, session_name: str) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        task.metadata["session_name"] = session_name
        task.touch()
        self._schedule_persist(task)
        return task

    def next_pending(self) -> Task | None:
        """Return oldest pending or retrying task."""
        candidates = [
            t
            for t in self._tasks.values()
            if t.status in (TaskStatus.PENDING, TaskStatus.RETRYING)
        ]
        candidates.sort(key=lambda t: t.created_at)
        return candidates[0] if candidates else None

    def find_blocked_by_thread(
        self,
        channel_id: str,
        thread_ts: str,
    ) -> Task | None:
        for task in self.list_by_status(TaskStatus.BLOCKED):
            if (
                task.metadata.get("slack_channel_id") == channel_id
                and task.metadata.get("slack_thread_ts") == thread_ts
            ):
                return task
        return None

    def attach_result(self, task_id: str, result: TaskResult) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        task.metadata["result"] = result.to_dict()
        if result.session_name:
            task.metadata["session_name"] = result.session_name
        task.touch()
        self._schedule_persist(task)
        return task
