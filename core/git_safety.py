"""
Git safety coordinator — orchestrator-facing facade (Phase 6).

Keeps git logic out of agents and recovery engine execution paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from core.git_manager import GitManager
from core.health_tracker import HealthTracker, HealthReport
from core.logger import get_logger
from core.rollback_engine import RollbackDecision, RollbackEngine

if TYPE_CHECKING:
    from core.git_config import GitSafetyConfig
    from core.task_manager import Task
    from core.task_result import TaskResult

logger = get_logger("git.safety")


class GitSafetyCoordinator:
    """Checkpoint, isolate, rollback, and health analysis for a task."""

    def __init__(
        self,
        config: GitSafetyConfig,
        git: GitManager | None = None,
        rollback: RollbackEngine | None = None,
        health: HealthTracker | None = None,
    ) -> None:
        self.config = config
        self.git = git or GitManager(config)
        self.rollback = rollback or RollbackEngine(config)
        self.health = health or HealthTracker(config.health_regression_threshold)

    def resolve_project_path(self, projects_dir: Path, task: Task) -> Path:
        if task.metadata.get("project_path"):
            return Path(task.metadata["project_path"]).resolve()
        if task.metadata.get("working_dir"):
            return Path(task.metadata["working_dir"]).resolve()
        if task.project_id:
            return (projects_dir / task.project_id).resolve()
        return projects_dir.resolve()

    async def prepare_workspace(
        self,
        task: Task,
        projects_dir: Path,
    ) -> bool:
        """
        Isolate task on atlas/task-<id> branch. Returns True if git safety active.
        """
        if not self.config.enabled or not GitManager.is_available():
            return False

        project_path = self.resolve_project_path(projects_dir, task)
        project_path.mkdir(parents=True, exist_ok=True)
        repo_path = await self.git.find_repo(project_path)
        if not repo_path:
            logger.debug("No git repo for task %s at %s", task.id[:8], project_path)
            return False

        if self.config.auto_isolate_branch:
            meta = await self.git.ensure_task_branch(repo_path, task.id)
            task.metadata["git"] = meta.to_dict()
            task.metadata["project_path"] = str(project_path)
            logger.info(
                "Workspace isolated: %s on branch %s",
                task.id[:8],
                meta.branch,
            )
            return True
        return False

    def should_checkpoint(self, task: Task, strategy: str) -> bool:
        if not self.config.enabled:
            return False
        if not task.metadata.get("git"):
            return False
        if self.config.auto_checkpoint_before_retry:
            return True
        return strategy in self.config.checkpoint_risky_strategies

    async def create_checkpoint(
        self,
        task: Task,
        strategy: str,
        attempt: int,
    ) -> bool:
        if not self.should_checkpoint(task, strategy):
            return False
        git_meta = task.metadata.get("git", {})
        repo_path = git_meta.get("repo_path")
        if not repo_path:
            return False

        record = await self.git.create_checkpoint(
            Path(repo_path),
            task.id,
            strategy,
            attempt,
        )
        if record:
            self.git.attach_checkpoint_to_task(task, record)
            return True
        return False

    def track_dependency_change(self, task: Task) -> None:
        if task.metadata.get("recovery_strategy") == "dependency_repair":
            git_meta = task.metadata.setdefault("git", {})
            git_meta["dependency_modify_count"] = int(
                git_meta.get("dependency_modify_count", 0)
            ) + 1

    def safety_limits_exceeded(self, task: Task) -> str | None:
        git_meta = task.metadata.get("git", {})
        if int(task.metadata.get("consecutive_rollbacks", 0)) >= self.config.max_consecutive_rollbacks:
            return "Max consecutive rollbacks exceeded"
        dep_count = int(git_meta.get("dependency_modify_count", 0))
        if dep_count >= self.config.max_dependency_modifications:
            return f"Max dependency modifications ({dep_count}) exceeded"
        return None

    async def analyze_and_maybe_rollback(
        self,
        task: Task,
        result: TaskResult,
    ) -> tuple[HealthReport, RollbackDecision, bool]:
        """Record health, evaluate rollback, execute if needed."""
        report = self.health.analyze(task, result)
        self.health.record(task, report)

        decision = RollbackDecision(should_rollback=False)
        rolled_back = False

        if not self.config.enabled or not task.metadata.get("git"):
            return report, decision, rolled_back

        decision = self.rollback.evaluate(task, result, report)
        if decision.should_rollback:
            rolled_back = await self.rollback.execute(self.git, task, decision)

        return report, decision, rolled_back
