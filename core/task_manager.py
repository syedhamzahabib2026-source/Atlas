"""
Task queue and lifecycle management.

Phase 5: adaptive recovery states and attempt history.
"""

from __future__ import annotations

import re
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
    WAITING_APPROVAL = "waiting_approval"
    AWAITING_APPROVAL = "waiting_approval"  # legacy alias (Phase 14)
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"
    MERGED = "merged"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # Phase 15 — delivery / deployment lifecycle
    CI_PENDING = "ci_pending"
    CI_RUNNING = "ci_running"
    CI_FAILED = "ci_failed"
    STAGING_DEPLOYING = "staging_deploying"
    STAGING_VERIFYING = "staging_verifying"
    STAGING_FAILED = "staging_failed"
    READY_FOR_PRODUCTION = "ready_for_production"
    PRODUCTION_PENDING_APPROVAL = "production_pending_approval"
    PRODUCTION_DEPLOYING = "production_deploying"
    PRODUCTION_VERIFYING = "production_verifying"
    DEPLOYED = "deployed"
    DEPLOYMENT_FAILED = "deployment_failed"
    ROLLED_BACK = "rolled_back"


# Durable wait states — not picked up by next_pending()
APPROVAL_WAIT_STATUSES = frozenset({
    TaskStatus.WAITING_APPROVAL,
})

DELIVERY_WAIT_STATUSES = frozenset({
    TaskStatus.PRODUCTION_PENDING_APPROVAL,
    TaskStatus.READY_FOR_PRODUCTION,
})


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

    def list_waiting_approval(self) -> list[Task]:
        return [
            t for t in self._tasks.values()
            if t.status in APPROVAL_WAIT_STATUSES
        ]

    def list_rejected(self) -> list[Task]:
        return [t for t in self._tasks.values() if t.status == TaskStatus.REJECTED]

    def list_in_delivery(self) -> list[Task]:
        from core.deployment_state import DELIVERY_STATUSES

        return [t for t in self._tasks.values() if t.status in DELIVERY_STATUSES]

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
        thread_ts: str | None,
    ) -> Task | None:
        """
        Match a Slack reply to a blocked task.

        Accepts explicit thread match OR a single blocked task in the channel
        (DM users often reply without using Slack's thread UI).
        """
        blocked = self.list_by_status(TaskStatus.BLOCKED)
        channel_matches = [
            t
            for t in blocked
            if t.metadata.get("slack_channel_id") == channel_id
        ]
        if not channel_matches:
            return None

        if thread_ts:
            for task in channel_matches:
                anchors = {
                    task.metadata.get("slack_thread_ts"),
                    task.metadata.get("blocked_slack_thread_ts"),
                }
                if thread_ts in anchors:
                    return task

        if len(channel_matches) == 1:
            return channel_matches[0]
        return None

    # Matches any run of 8+ hex chars (with optional UUID dashes).
    _HEX_TASK_ID_RE = re.compile(r'\b([0-9a-f]{8}[0-9a-f\-]*)\b', re.IGNORECASE)

    def find_blocked_by_id_in_text(self, text: str) -> Task | None:
        """
        Scan free-form text for a hex token that is a prefix (≥8 chars) of any
        blocked task's ID.  Lets users unblock tasks by mentioning the short ID
        in a top-level channel message rather than having to reply in-thread.
        """
        bare_tokens = {
            m.group(1).lower().replace("-", "")
            for m in self._HEX_TASK_ID_RE.finditer(text)
            if len(m.group(1).replace("-", "")) >= 8
        }
        if not bare_tokens:
            return None
        for task in self.list_by_status(TaskStatus.BLOCKED):
            task_bare = task.id.lower().replace("-", "")
            for token in bare_tokens:
                if task_bare.startswith(token):
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
