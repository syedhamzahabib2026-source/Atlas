"""
Multi-project task scheduling (Phase 8).
"""

from __future__ import annotations

from core.logger import get_logger
from core.task_manager import Task, TaskManager, TaskStatus

logger = get_logger("scheduler")


class ProjectScheduler:
    """
    Per-project queues with priority and concurrency caps.

    TODO: distributed workers per project
    TODO: cloud execution nodes
    """

    def __init__(self, per_project_max: int = 2) -> None:
        self.per_project_max = per_project_max

    def _pid(self, task: Task) -> str:
        return task.project_id or "default"

    def active_by_project(self, tasks: list[Task]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in tasks:
            if t.status in TaskManager.ACTIVE_STATUSES:
                pid = self._pid(t)
                counts[pid] = counts.get(pid, 0) + 1
        return counts

    def can_schedule(self, task: Task, tasks: list[Task]) -> bool:
        pid = self._pid(task)
        active = self.active_by_project(tasks).get(pid, 0)
        return active < self.per_project_max

    def next_task(self, tasks: list[Task]) -> Task | None:
        """Pick highest-priority pending/retrying task respecting per-project limits."""
        candidates = [
            t
            for t in tasks
            if t.status in (TaskStatus.PENDING, TaskStatus.RETRYING)
        ]
        candidates.sort(
            key=lambda t: (
                -int(t.metadata.get("priority", 50)),
                t.created_at,
            )
        )
        for task in candidates:
            if self.can_schedule(task, tasks):
                return task
        return None

    def project_summary(self, tasks: list[Task]) -> dict[str, dict]:
        summary: dict[str, dict] = {}
        for t in tasks:
            pid = self._pid(t)
            s = summary.setdefault(
                pid,
                {"pending": 0, "active": 0, "blocked": 0, "done": 0},
            )
            if t.status in (TaskStatus.PENDING, TaskStatus.RETRYING):
                s["pending"] += 1
            elif t.status in TaskManager.ACTIVE_STATUSES:
                s["active"] += 1
            elif t.status == TaskStatus.BLOCKED:
                s["blocked"] += 1
            else:
                s["done"] += 1
        return summary
