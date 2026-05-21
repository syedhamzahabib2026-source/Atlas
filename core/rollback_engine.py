"""
Rollback engine — when to restore checkpoints vs patch forward.

Works with GitManager; decisions informed by HealthTracker.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from core.logger import get_logger

if TYPE_CHECKING:
    from core.git_manager import GitManager
    from core.health_tracker import HealthReport
    from core.task_manager import Task
    from core.task_result import TaskResult

logger = get_logger("rollback")


class RollbackMethod(str, Enum):
    CHECKPOINT = "checkpoint"
    BRANCH = "branch"
    REVERT = "revert"


@dataclass
class RollbackDecision:
    should_rollback: bool
    reason: str = ""
    method: RollbackMethod = RollbackMethod.CHECKPOINT
    target_ref: str | None = None  # commit hash or branch name

    @property
    def summary(self) -> str:
        if not self.should_rollback:
            return "No rollback"
        return f"{self.method.value} → {self.target_ref or 'n/a'}: {self.reason}"


class RollbackEngine:
    """Evaluate and execute safe rollbacks."""

    def __init__(self, config) -> None:
        self.config = config

    def evaluate(
        self,
        task: Task,
        result: TaskResult,
        health: HealthReport,
    ) -> RollbackDecision:
        """
        Decide if Atlas should restore a checkpoint before retrying.
        """
        git_meta = task.metadata.get("git", {})
        checkpoints = git_meta.get("checkpoints", [])
        if not checkpoints:
            return RollbackDecision(should_rollback=False, reason="No checkpoints")

        consecutive = int(task.metadata.get("consecutive_rollbacks", 0))
        if consecutive >= self.config.max_consecutive_rollbacks:
            return RollbackDecision(
                should_rollback=False,
                reason="Max consecutive rollbacks reached",
            )

        text = (result.summary + " ".join(result.errors)).lower()
        catastrophic = any(
            m in text
            for m in ("build failed", "compilation error", "cannot find module", "fatal")
        )

        if catastrophic:
            target = checkpoints[-1]["commit_hash"]
            return RollbackDecision(
                should_rollback=True,
                reason="Catastrophic build/dependency failure",
                method=RollbackMethod.CHECKPOINT,
                target_ref=target,
            )

        if health.regression_detected:
            # Prefer earlier stable checkpoint if health worsening progressively
            idx = -2 if len(checkpoints) >= 2 else -1
            target = checkpoints[idx]["commit_hash"]
            return RollbackDecision(
                should_rollback=True,
                reason="; ".join(health.regression_reasons[:2]),
                method=RollbackMethod.CHECKPOINT,
                target_ref=target,
            )

        from core.health_tracker import HealthTracker

        if HealthTracker().progressive_worsening(
            task, self.config.progressive_worsening_attempts
        ):
            target = checkpoints[0]["commit_hash"]
            return RollbackDecision(
                should_rollback=True,
                reason="Progressive health decline across attempts",
                method=RollbackMethod.CHECKPOINT,
                target_ref=target,
            )

        return RollbackDecision(should_rollback=False)

    async def execute(
        self,
        git: GitManager,
        task: Task,
        decision: RollbackDecision,
    ) -> bool:
        if not decision.should_rollback or not decision.target_ref:
            return False

        from pathlib import Path

        repo_path = task.metadata.get("git", {}).get("repo_path", "")
        repo = Path(repo_path) if repo_path else None
        if not repo or not repo.exists():
            logger.warning("Rollback skipped — no repo path")
            return False

        ok = False
        if decision.method == RollbackMethod.CHECKPOINT:
            ok = await git.rollback_to_checkpoint(repo, decision.target_ref)
        elif decision.method == RollbackMethod.BRANCH:
            ok = await git.rollback_to_branch(repo, decision.target_ref)
        elif decision.method == RollbackMethod.REVERT:
            ok = await git.revert_commit(repo, decision.target_ref)

        if ok:
            git.record_rollback(
                task,
                to_hash=decision.target_ref,
                reason=decision.reason,
                method=decision.method.value,
            )
            task.metadata["consecutive_rollbacks"] = int(
                task.metadata.get("consecutive_rollbacks", 0)
            ) + 1
        return ok
