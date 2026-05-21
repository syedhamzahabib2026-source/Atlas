"""
Runtime safety policies — stale tasks, runaway retries, orphan cleanup (Phase 8).
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from core.logger import get_logger
from core.task_manager import TaskStatus

if TYPE_CHECKING:
    from core.config import AtlasConfig
    from core.task_manager import TaskManager

logger = get_logger("runtime.safety")


class RuntimeSafetyPolicies:
    """Operational guardrails independent of recovery strategy logic."""

    def __init__(self, config: AtlasConfig) -> None:
        self.config = config

    def is_stale(self, task, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=self.config.runtime.stale_task_hours)
        updated = task.updated_at or task.created_at
        return updated < cutoff

    def runaway_retries(self, task) -> bool:
        count = int(task.metadata.get("recovery_attempt_count", 0))
        return count >= self.config.runtime.max_runaway_retries

    def excessive_rollbacks(self, task) -> bool:
        rollbacks = len(task.metadata.get("git", {}).get("rollback_history", []))
        return rollbacks >= self.config.git.max_consecutive_rollbacks

    async def apply_policies(self, task_manager: TaskManager) -> list[str]:
        """Return list of actions taken."""
        actions: list[str] = []
        for task in list(task_manager.list_all()):
            if task.status not in (
                TaskStatus.PENDING,
                TaskStatus.RETRYING,
                TaskStatus.BLOCKED,
            ):
                continue
            if self.runaway_retries(task) and self.is_stale(task):
                task_manager.update_status(
                    task.id, TaskStatus.CANCELLED, error="runaway retry policy"
                )
                await task_manager.persist(task)
                actions.append(f"cancelled runaway {task.id[:8]}")
            elif self.excessive_rollbacks(task):
                task.metadata["safety_flag"] = "excessive_rollbacks"
                await task_manager.persist(task)
                actions.append(f"flagged rollbacks {task.id[:8]}")
        return actions
