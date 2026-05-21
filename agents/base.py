"""
Base agent interface for Atlas-controlled workers.

All coding agents (Claude Code, future API agents) implement this contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.task_manager import Task
    from core.task_result import TaskResult


class BaseAgent(ABC):
    """Abstract agent that can accept and execute tasks."""

    name: str = "base"

    @abstractmethod
    def can_handle(self, task: Task) -> bool:
        """Return True if this agent should execute the task."""

    @abstractmethod
    async def run(self, task: Task) -> TaskResult:
        """
        Execute the task (may be long-running).

        Returns structured TaskResult for orchestrator lifecycle handling.
        """

    async def on_complete(self, task: Task, result: TaskResult) -> None:
        """Optional hook after successful run."""
        pass

    async def on_error(self, task: Task, result: TaskResult) -> None:
        """Optional hook on failure."""
        pass
