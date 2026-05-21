"""
Distill task outcomes into operational memory (Phase 7).

Extracts high-signal summaries — not raw logs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from core.attempt_history import AttemptHistory
from core.failure_classifier import classify_failure, error_signature
from core.logger import get_logger
from memory.memory_models import (
    MemoryCategory,
    OperationalMemoryRecord,
    TaskMemory,
    new_id,
)
from memory.lesson_extractor import LessonExtractor
from memory.operational_memory import OperationalMemoryStore

if TYPE_CHECKING:
    from core.task_manager import Task
    from core.task_result import TaskResult

logger = get_logger("memory.summarizer")


class MemorySummarizer:
    """Extract and store distilled operational knowledge from tasks."""

    def __init__(self, store: OperationalMemoryStore) -> None:
        self.store = store
        self.lesson_extractor = LessonExtractor(self.store)

    def _project_id(self, task) -> str:
        return task.project_id or "default"

    async def extract_from_task(
        self,
        task: Task,
        result: TaskResult,
    ) -> list[str]:
        """
        Distill task completion into memory records.
        Returns list of insight strings for Slack reporting.
        """
        insights: list[str] = []
        pid = self._project_id(task)
        category = classify_failure(task, result).value
        strategy = task.metadata.get("recovery_strategy", "initial")
        verify = task.metadata.get("verification", {})
        health = task.metadata.get("health", {})
        health_score = float(
            health.get("current", {}).get("score", task.metadata.get("health_score", 0))
        )
        regression = bool(health.get("regression_detected", False))

        outcome = TaskMemory(
            task_id=task.id,
            project_id=pid,
            status=task.status.value if hasattr(task.status, "value") else str(task.status),
            strategy=strategy,
            failure_category=category,
            verification_status=verify.get("status", ""),
            health_score=health_score,
            regression=regression,
            summary=self._distill_summary(task, result),
        )
        await self.store.record_task_outcome(outcome)

        success = result.success
        await self.store.update_strategy_stats(
            pid, strategy, category,
            success=success, failure=not success,
            rollback=bool(task.metadata.get("git", {}).get("rollback_history")),
        )

        sig = error_signature(result)
        if not success and sig:
            pattern = await self.store.bump_failure_pattern(pid, sig, category)
            if pattern.occurrence_count >= 2:
                insights.append(
                    f"Recurring {category} failure pattern ({pattern.occurrence_count}x)"
                )

        if success:
            await self._record_success(task, result, pid, strategy, category)
            if strategy not in ("initial", "none"):
                insights.append(f"Strategy `{strategy}` succeeded for {category}")
        else:
            await self._record_failure(task, result, pid, strategy, category)
            if regression:
                insights.append(f"Regression detected on {category} work")

        await self._update_risk_zones(task, result, pid, regression)
        await self._extract_architecture_hints(task, result, pid)

        # TODO: semantic diff — compare checkpoints for file-level insights
        # TODO: AI-generated architecture observations

        promoted = await self.lesson_extractor.maybe_promote(task, result, pid)
        insights.extend(promoted)

        # Phase 11: score lessons that were injected into this task
        injected_ids = task.metadata.get("injected_lesson_ids", [])
        if injected_ids:
            for lesson_id in injected_ids:
                await self.store.update_lesson_signal(lesson_id, result.success)

        return insights

    def _distill_summary(self, task: Task, result: TaskResult) -> str:
        parts = [result.summary[:200] if result.summary else task.title]
        if result.errors:
            parts.append("Errors: " + "; ".join(result.errors[:3])[:150])
        attempts = AttemptHistory.load(task)
        if attempts:
            parts.append(f"Attempts: {len(attempts)}")
        return " | ".join(parts)[:500]

    async def _record_success(
        self, task, result, pid: str, strategy: str, category: str
    ) -> None:
        summary = (
            f"Task `{task.id[:8]}` completed with `{strategy}` on {category}. "
            f"{result.summary[:120]}"
        )
        await self.store.upsert_memory(
            OperationalMemoryRecord(
                id=new_id(),
                project_id=pid,
                category=MemoryCategory.RECOVERY.value,
                subcategory="success",
                title=f"Success: {strategy}",
                summary=summary,
                tags=[category, strategy, "success"],
                signal_score=65,
                metadata={"task_id": task.id},
            )
        )

    async def _record_failure(
        self, task, result, pid: str, strategy: str, category: str
    ) -> None:
        summary = (
            f"Task `{task.id[:8]}` failed ({category}) after `{strategy}`. "
            f"{result.summary[:120]}"
        )
        await self.store.upsert_memory(
            OperationalMemoryRecord(
                id=new_id(),
                project_id=pid,
                category=MemoryCategory.RECOVERY.value,
                subcategory="failed_strategy",
                title=f"Failed: {strategy}",
                summary=summary,
                tags=[category, strategy, "failure"],
                signal_score=55,
                metadata={"task_id": task.id},
            )
        )

    async def _update_risk_zones(
        self, task, result, pid: str, regression: bool
    ) -> None:
        paths = self._infer_risk_paths(task, result)
        for path, zone_type in paths:
            zone = await self.store.bump_risk_zone(
                pid,
                path,
                zone_type,
                failure=not result.success,
                rollback=bool(
                    task.metadata.get("git", {}).get("rollback_history")
                ),
                regression=regression,
            )
            if zone.risk_score >= 50 and zone.failure_count >= 2:
                await self.store.upsert_memory(
                    OperationalMemoryRecord(
                        id=new_id(),
                        project_id=pid,
                        category=MemoryCategory.REPOSITORY.value,
                        subcategory="fragile_zone",
                        title=f"Risk: {path}",
                        summary=(
                            f"{path} marked {zone.risk_level.value.upper()} risk "
                            f"(score={zone.risk_score:.0f}, failures={zone.failure_count})"
                        ),
                        tags=["risk", zone_type, path],
                        signal_score=int(min(90, zone.risk_score)),
                        metadata={"zone_type": zone_type},
                    )
                )

    def _infer_risk_paths(self, task, result) -> list[tuple[str, str]]:
        """Infer file/area paths from task metadata and errors."""
        paths: list[tuple[str, str]] = []
        text = " ".join(
            [
                task.title,
                task.description,
                result.summary,
                " ".join(result.errors),
            ]
        ).lower()

        hints = [
            ("auth", "service", "auth"),
            ("middleware", "file", "auth/middleware"),
            ("notification", "service", "notification"),
            ("login", "workflow", "auth/login"),
            ("api", "service", "api"),
            ("package.json", "file", "package.json"),
        ]
        for keyword, zone_type, path in hints:
            if keyword in text:
                paths.append((path, zone_type))

        fc = task.metadata.get("failure_category", "")
        if fc == "auth":
            paths.append(("auth", "service"))
        if fc == "frontend":
            paths.append(("ui", "workflow"))

        return paths or [("general", "workflow")]

    async def _extract_architecture_hints(
        self, task, result, pid: str
    ) -> None:
        """Store stable project observations when present in metadata."""
        if notes := task.metadata.get("architecture_note"):
            await self.store.upsert_memory(
                OperationalMemoryRecord(
                    id=new_id(),
                    project_id=pid,
                    category=MemoryCategory.PROJECT.value,
                    subcategory="architecture",
                    title="Architecture note",
                    summary=str(notes)[:300],
                    tags=["architecture"],
                    signal_score=70,
                )
            )

    async def consolidate_project(self, project_id: str) -> list[str]:
        """
        Compress noisy histories into stable operational knowledge.
        Returns consolidation insights.
        """
        insights = []
        stats = await self.store.get_strategy_stats(project_id, min_attempts=3)
        for s in stats:
            if s.success_rate >= 0.6:
                await self.store.upsert_memory(
                    OperationalMemoryRecord(
                        id=new_id(),
                        project_id=project_id,
                        category=MemoryCategory.SYSTEM.value,
                        subcategory="strategy_performance",
                        title=f"Reliable: {s.strategy}",
                        summary=(
                            f"{s.strategy} has {s.success_rate:.0%} success rate "
                            f"for {s.category} ({s.attempts} attempts)"
                        ),
                        tags=[s.strategy, s.category, "stable"],
                        signal_score=75,
                    )
                )
                insights.append(
                    f"`{s.strategy}` has highest success rate for {s.category} failures"
                )
            elif s.attempts >= 4 and s.success_rate < 0.3:
                insights.append(
                    f"`{s.strategy}` has high regression risk in {s.category} context"
                )

        zones = await self.store.get_high_risk_zones(project_id, min_score=50)
        for z in zones[:3]:
            insights.append(f"{z.path} marked {z.risk_level.value.upper()} RISK")

        return insights
