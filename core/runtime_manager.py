"""
Persistent runtime manager — operational continuity (Phase 8).

TODO: deployment pipelines
TODO: autonomous PR lifecycle integration
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from core.logger import get_logger
from core.project_scheduler import ProjectScheduler
from core.resource_manager import ResourceManager
from core.runtime_recovery import RuntimeRecovery
from core.runtime_state import RuntimeState, RuntimeStateStore
from core.task_manager import TaskStatus

if TYPE_CHECKING:
    from core.config import AtlasConfig
    from core.task_store import TaskStore
    from core.task_manager import TaskManager
    from sessions.tmux_manager import TmuxManager
    from slack.bot import SlackBot

logger = get_logger("runtime")


class RuntimeManager:
    """
    Coordinates durable runtime: startup recovery, shutdown, snapshots.

    Does NOT execute agents or choose recovery strategies.
    """

    def __init__(self, config: AtlasConfig) -> None:
        self.config = config
        self.state_store = RuntimeStateStore(
            config.root_dir / config.runtime.state_path
        )
        self.recovery = RuntimeRecovery(config)
        self.scheduler = ProjectScheduler(config.runtime.per_project_max_concurrent)
        self.resources = ResourceManager(
            max_total=config.max_concurrent_tasks,
            max_claude=config.runtime.max_claude_concurrent,
            max_browser=config.runtime.max_browser_concurrent,
        )
        self._tick_count = 0
        self._state: RuntimeState | None = None

    async def startup(
        self,
        task_manager: TaskManager,
        task_store: TaskStore | None,
        tmux: TmuxManager | None,
        slack: SlackBot | None = None,
    ) -> RuntimeState:
        """Integrity checks, restore tasks, reconnect sessions."""
        prev = self.state_store.load()
        self._state = RuntimeState(
            atlas_pid=os.getpid(),
            boot_count=(prev.boot_count + 1) if prev else 1,
        )
        if prev and not prev.last_shutdown_graceful:
            self._state.integrity_issues.append(
                "Previous shutdown was not graceful"
            )

        report = await self.recovery.recover_on_startup(
            task_manager, task_store, tmux
        )
        self._state.integrity_issues.extend(report.integrity_issues)
        self._state.orchestrator_running = True
        self._state.metadata["recovery_report"] = {
            "tasks_restored": report.tasks_restored,
            "tasks_normalized": report.tasks_normalized,
            "sessions_reconnected": len(report.sessions_reconnected),
            "sessions_orphaned": len(report.sessions_orphaned),
        }

        self.state_store.save(self._state)

        if slack and self.config.slack_ready:
            await slack.notify_runtime_startup(self._state, report)
            await slack.notify_runtime_recovery_complete(report)
            if report.integrity_issues:
                await slack.notify_integrity_failure(report.integrity_issues)

        logger.info(
            "Runtime startup: restored=%d normalized=%d tmux_reconnected=%d",
            report.tasks_restored,
            report.tasks_normalized,
            len(report.sessions_reconnected),
        )
        return self._state

    async def shutdown(
        self,
        task_manager: TaskManager,
        tmux: TmuxManager | None,
        *,
        graceful: bool = True,
        slack: SlackBot | None = None,
    ) -> None:
        """Persist all tasks and runtime snapshot; preserve tmux sessions."""
        if self._state is None:
            self._state = RuntimeState(atlas_pid=os.getpid())

        self._state.active_task_ids = [
            t.id
            for t in task_manager.list_all()
            if t.status in task_manager.ACTIVE_STATUSES
        ]
        self._state.project_queues = {
            pid: [
                t.id
                for t in tasks
                if t.status in (TaskStatus.PENDING, TaskStatus.RETRYING)
            ]
            for pid, tasks in self._group_by_project(task_manager).items()
        }
        self._state.resource_usage = self.resources.usage_snapshot(
            task_manager.list_all()
        )
        self._state.recovery_chains_active = [
            t.id
            for t in task_manager.list_all()
            if t.metadata.get("recovery_chain") or t.metadata.get("attempt_history")
        ]

        await task_manager.flush_all()

        self.state_store.mark_shutdown(self._state, graceful=graceful)

        if slack and self.config.slack_ready:
            await slack.notify_runtime_shutdown(self._state, graceful=graceful)

        logger.info("Runtime shutdown (graceful=%s)", graceful)

    def _group_by_project(self, task_manager: TaskManager) -> dict:
        groups: dict[str, list] = {}
        for t in task_manager.list_all():
            pid = t.project_id or "default"
            groups.setdefault(pid, []).append(t)
        return groups

    async def on_tick(self, task_manager: TaskManager) -> None:
        """Periodic snapshot during orchestrator loop."""
        self._tick_count += 1
        if self._tick_count % self.config.runtime.snapshot_interval_ticks != 0:
            return
        if self._state is None:
            self._state = RuntimeState(atlas_pid=os.getpid())
        self._state.active_task_ids = [
            t.id
            for t in task_manager.list_all()
            if t.status in task_manager.ACTIVE_STATUSES
        ]
        self._state.resource_usage = self.resources.usage_snapshot(
            task_manager.list_all()
        )
        self.state_store.save(self._state)
        await task_manager.flush_all()

    def next_scheduled_task(self, task_manager: TaskManager):
        """Project-aware + resource-aware task selection."""
        from core.task_manager import TaskStatus

        tasks = task_manager.list_all()
        task = self.scheduler.next_task(tasks)
        if task is None:
            return None
        ok, reason = self.resources.can_start(task, tasks)
        if not ok:
            logger.debug("Resource gate: %s", reason)
            return None
        if not self.scheduler.can_schedule(task, tasks):
            return None
        if not task_manager.can_start_more():
            return None
        return task
