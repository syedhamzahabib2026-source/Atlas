"""
Cross-project lesson extraction — promotes reusable patterns to __global__ (Phase 10).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from memory.operational_memory import OperationalMemoryStore

if TYPE_CHECKING:
    from core.task_manager import Task
    from core.task_result import TaskResult


class LessonExtractor:
    """Evaluate completed tasks and promote reusable patterns to global memory."""

    def __init__(self, store: OperationalMemoryStore) -> None:
        self.store = store

    async def maybe_promote(
        self, task: Task, result: TaskResult, project_id: str
    ) -> list[str]:
        """
        Promote reusable patterns to __global__ memory.
        Returns list of promoted lesson strings (appended to Slack insights).
        """
        promoted: list[str] = []

        # Rule 1: Strategy succeeded after ≥2 prior failures on same category
        strategy = task.metadata.get("recovery_strategy") or task.metadata.get("strategy")
        if strategy and result.success:
            all_stats = await self.store.get_strategy_stats(project_id, min_attempts=1)
            matching = [s for s in all_stats if s.strategy == strategy]
            if matching and matching[0].failures >= 2:
                stat = matching[0]
                lesson = (
                    f"Strategy '{strategy}' resolved '{stat.category}' failures "
                    f"after ≥2 attempts — apply early on similar tasks."
                )
                await self.store.upsert_global_lesson(
                    content=lesson,
                    category=stat.category,
                    signal_score=0.85,
                    tags=[strategy, stat.category],
                )
                promoted.append(f"[global] {lesson}")

        # Rule 2: Failure pattern seen ≥3 times in this project → global warning
        if not result.success:
            error_text = result.errors[0] if result.errors else ""
            sig = self._signature(error_text)
            if sig:
                patterns = await self.store.get_recurring_patterns(
                    project_id, min_count=3
                )
                for p in patterns:
                    if p.signature.startswith(sig[:40]):
                        lesson = (
                            f"Recurring failure '{sig[:60]}' — "
                            f"seen {p.occurrence_count}x. Check before starting similar tasks."
                        )
                        await self.store.upsert_global_lesson(
                            content=lesson,
                            category="failure_pattern",
                            signal_score=0.75,
                            tags=["failure", sig[:20]],
                        )
                        promoted.append(f"[global] {lesson}")

        return promoted

    def _signature(self, error: str) -> str:
        """Stable short signature for an error string."""
        cleaned = re.sub(r"(line \d+|0x[0-9a-f]+|\d{4,})", "", error.lower())
        return cleaned.strip()[:80]
