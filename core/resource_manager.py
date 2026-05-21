"""
Resource limits for long-running Atlas operations (Phase 8).
"""

from __future__ import annotations

from core.logger import get_logger
from core.task_manager import Task, TaskManager

logger = get_logger("resources")


class ResourceManager:
    """
    Soft limits on concurrent agent workloads.

    TODO: containerized execution quotas
    TODO: remote browser runners
    """

    def __init__(
        self,
        max_total: int = 3,
        max_claude: int = 2,
        max_browser: int = 2,
    ) -> None:
        self.max_total = max_total
        self.max_claude = max_claude
        self.max_browser = max_browser
        self._claude_active = 0
        self._browser_active = 0

    def _count_agents(self, tasks: list[Task]) -> tuple[int, int, int]:
        claude = browser = total = 0
        for t in tasks:
            if t.status not in TaskManager.ACTIVE_STATUSES:
                continue
            total += 1
            agent = t.metadata.get("agent", "claude_code")
            if agent == "browser":
                browser += 1
            else:
                claude += 1
        return total, claude, browser

    def can_start(self, task: Task, tasks: list[Task]) -> tuple[bool, str]:
        total, claude, browser = self._count_agents(tasks)
        if total >= self.max_total:
            return False, "max concurrent tasks reached"
        agent = task.metadata.get("agent", "claude_code")
        if agent == "browser" and browser >= self.max_browser:
            return False, "max browser sessions reached"
        if agent != "browser" and claude >= self.max_claude:
            return False, "max Claude workers reached"
        return True, ""

    def usage_snapshot(self, tasks: list[Task]) -> dict[str, int]:
        total, claude, browser = self._count_agents(tasks)
        return {
            "total_active": total,
            "claude_active": claude,
            "browser_active": browser,
            "max_total": self.max_total,
            "max_claude": self.max_claude,
            "max_browser": self.max_browser,
        }
