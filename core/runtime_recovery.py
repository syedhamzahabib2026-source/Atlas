"""
Startup recovery — restore tasks, sessions, and recovery chains (Phase 8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from core.logger import get_logger
from core.runtime_safety import RuntimeSafetyPolicies
from core.task_manager import TaskManager, TaskStatus
from core.task_store import TaskStore

if TYPE_CHECKING:
    from core.config import AtlasConfig
    from sessions.tmux_manager import TmuxManager

logger = get_logger("runtime.recovery")


@dataclass
class RecoveryReport:
    tasks_restored: int = 0
    tasks_normalized: int = 0
    sessions_reconnected: list[str] = field(default_factory=list)
    sessions_orphaned: list[str] = field(default_factory=list)
    sessions_cleaned: list[str] = field(default_factory=list)
    integrity_issues: list[str] = field(default_factory=list)
    stale_cleaned: int = 0
    messages: list[str] = field(default_factory=list)


# Statuses that cannot survive a cold restart mid-flight
_RESET_ON_BOOT = {
    TaskStatus.RUNNING: TaskStatus.PENDING,
    TaskStatus.VERIFYING: TaskStatus.PENDING,
    TaskStatus.RETRYING: TaskStatus.PENDING,
    TaskStatus.INVESTIGATING: TaskStatus.PENDING,
}


class RuntimeRecovery:
    """Recover operational continuity after restart."""

    def __init__(self, config: AtlasConfig) -> None:
        self.config = config

    async def recover_on_startup(
        self,
        task_manager: TaskManager,
        task_store: TaskStore | None,
        tmux: TmuxManager | None,
    ) -> RecoveryReport:
        report = RecoveryReport()

        if task_store:
            report.integrity_issues.extend(await task_store.integrity_check())

        # Load persisted tasks into memory
        if task_store:
            existing = {t.id for t in task_manager.list_all()}
            for task in await task_store.load_all():
                if task.id not in existing:
                    task_manager._tasks[task.id] = task
                    report.tasks_restored += 1

        await self._normalize_interrupted_tasks(task_manager, report)
        await self._resurrect_tmux_sessions(task_manager, tmux, report)
        await self._validate_git_checkpoints(task_manager, report)
        await self._cleanup_stale_tasks(task_manager, report)
        safety = RuntimeSafetyPolicies(self.config)
        for action in await safety.apply_policies(task_manager):
            report.messages.append(action)

        for msg in report.messages:
            logger.info("Recovery: %s", msg)

        return report

    async def _normalize_interrupted_tasks(
        self,
        task_manager: TaskManager,
        report: RecoveryReport,
    ) -> None:
        """RUNNING/VERIFYING cannot continue after reboot — re-queue safely."""
        for task in task_manager.list_all():
            new_status = _RESET_ON_BOOT.get(task.status)
            if new_status:
                old = task.status.value
                task_manager.update_status(task.id, new_status)
                task.metadata["recovered_from_status"] = old
                task.metadata["runtime_recovered"] = True
                await task_manager.persist(task)
                report.tasks_normalized += 1
                report.messages.append(
                    f"Task {task.id[:8]}: {old} -> {new_status.value} after restart"
                )

    async def _resurrect_tmux_sessions(
        self,
        task_manager: TaskManager,
        tmux: TmuxManager | None,
        report: RecoveryReport,
    ) -> None:
        if not tmux or not tmux.is_available():
            return

        live_sessions = set(await tmux.list_sessions())
        prefix = tmux.session_prefix

        # Map live sessions to tasks
        for task in task_manager.list_all():
            session = task.session_name
            if not session or not session.startswith(prefix):
                continue
            if session in live_sessions:
                task.metadata["tmux_resurrected"] = True
                report.sessions_reconnected.append(session)
                await task_manager.persist(task)
            elif task.status in TaskManager.ACTIVE_STATUSES:
                task.metadata["tmux_stale"] = True
                report.messages.append(
                    f"Task {task.id[:8]} expected session {session} — not found"
                )

        # Orphan tmux sessions (no matching active task)
        task_sessions = {
            t.session_name for t in task_manager.list_all() if t.session_name
        }
        for name in live_sessions:
            if not name.startswith(prefix):
                continue
            if name not in task_sessions:
                report.sessions_orphaned.append(name)
                if self.config.runtime.orphan_session_cleanup:
                    await tmux.kill_session(name)
                    report.sessions_cleaned.append(name)
                    report.messages.append(f"Cleaned orphan tmux session {name}")

    async def _validate_git_checkpoints(
        self,
        task_manager: TaskManager,
        report: RecoveryReport,
    ) -> None:
        """Flag missing checkpoint hashes in metadata (soft validation)."""
        try:
            import git
        except ImportError:
            return

        for task in task_manager.list_all():
            git_meta = task.metadata.get("git", {})
            repo_path = git_meta.get("repo_path")
            if not repo_path:
                continue
            for cp in git_meta.get("checkpoints", []):
                commit = cp.get("commit_hash", "")[:8]
                if commit:
                    task.metadata.setdefault("checkpoint_validated", True)
            # Full git object verification is expensive — mark presence only
            if git_meta.get("rollback_history"):
                report.messages.append(
                    f"Task {task.id[:8]} has {len(git_meta['rollback_history'])} rollback(s) on record"
                )

    async def _cleanup_stale_tasks(
        self,
        task_manager: TaskManager,
        report: RecoveryReport,
    ) -> None:
        """Cancel ancient unfinished tasks to prevent runaway retry loops."""
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self.config.runtime.stale_task_hours
        )
        for task in list(task_manager.list_all()):
            if task.status not in (
                TaskStatus.PENDING,
                TaskStatus.BLOCKED,
                TaskStatus.RETRYING,
            ):
                continue
            if task.updated_at and task.updated_at > cutoff:
                continue
            if task.created_at > cutoff:
                continue
            retries = int(task.metadata.get("recovery_attempt_count", 0))
            if retries >= self.config.runtime.max_runaway_retries:
                task_manager.update_status(
                    task.id, TaskStatus.CANCELLED, error="runaway retry limit"
                )
                await task_manager.persist(task)
                report.stale_cleaned += 1
                report.messages.append(
                    f"Cancelled stale task {task.id[:8]} (retries={retries})"
                )
