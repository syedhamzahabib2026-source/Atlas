"""
Operational memory coordinator — orchestrator-facing API (Phase 7).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from core.logger import get_logger
from memory.context_builder import ContextBuilder
from memory.memory_summarizer import MemorySummarizer
from memory.operational_memory import OperationalMemoryStore

if TYPE_CHECKING:
    from core.task_manager import Task
    from core.task_result import TaskResult

logger = get_logger("memory.coordinator")


class MemoryCoordinator:
    """
    Unified operational memory lifecycle.

    Memory layer only — does not execute tasks or choose strategies.
    """

    def __init__(self, db_path: Path, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.store = OperationalMemoryStore(db_path)
        self.summarizer = MemorySummarizer(self.store)
        self.context_builder = ContextBuilder(self.store)
        self._initialized = False

    async def initialize(self) -> None:
        if not self.enabled:
            return
        await self.store.initialize()
        self._initialized = True
        logger.info("Memory coordinator ready")

    async def prepare_task_context(self, task: Task) -> str | None:
        """Retrieve and inject operational context before execution."""
        if not self.enabled or not self._initialized:
            return None
        if task.metadata.get("agent") == "browser" and not task.metadata.get(
            "use_operational_context"
        ):
            return None

        ctx = await self.context_builder.build_for_task(task)
        self.context_builder.apply_to_task(task, ctx)
        return ctx.to_prompt_block()

    async def extract_after_task(
        self,
        task: Task,
        result: TaskResult,
    ) -> list[str]:
        """Distill operational memory after task completes."""
        if not self.enabled or not self._initialized:
            return []

        insights = await self.summarizer.extract_from_task(task, result)
        consolidated = await self.summarizer.consolidate_project(
            self.summarizer._project_id(task)
        )
        insights.extend(consolidated)
        return list(dict.fromkeys(insights))  # dedupe preserve order

    async def maintenance(self, *, max_age_days: int = 90) -> int:
        """Prune stale low-signal memories."""
        if not self.enabled or not self._initialized:
            return 0
        return await self.store.prune_stale(max_age_days=max_age_days)

    async def close(self) -> None:
        await self.store.close()
