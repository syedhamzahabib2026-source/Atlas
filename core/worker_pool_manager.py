"""
Worker pool coordinator — wires registry, router, and auth monitor (Phase 13).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.auth_monitor import AuthMonitor
from core.logger import get_logger
from core.pool_config import WorkerPoolsConfig
from core.worker_registry import WorkerRegistry
from core.worker_router import WorkerRouter, RoutingDecision
from sessions.tmux_manager import TmuxManager

if TYPE_CHECKING:
    from core.task_manager import Task, TaskManager
    from core.task_result import TaskResult
    from core.worker_pool import WorkerPool

logger = get_logger("worker_pool_manager")


class WorkerPoolManager:
    """
    Facade for pool infrastructure.

    Orchestrator uses this — not individual pools directly for routing.
    """

    def __init__(
        self,
        pools_config: WorkerPoolsConfig,
        *,
        tmux_socket: str | None = None,
    ) -> None:
        self.config = pools_config
        self.registry = WorkerRegistry(pools_config)
        self.auth_monitor = AuthMonitor(self.registry)
        self.router = WorkerRouter(self.registry, self.auth_monitor)
        self.pool_tmux: dict[str, TmuxManager] = {}
        for pool in self.registry.all_pools():
            self.pool_tmux[pool.pool_id] = TmuxManager(
                session_prefix=pool.session_prefix,
                socket_path=tmux_socket,
            )

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def tmux_for_task(self, task: Task) -> TmuxManager | None:
        pool_id = task.metadata.get("worker_pool") or "subscription"
        return self.pool_tmux.get(pool_id) or self.pool_tmux.get("subscription")

    def assign_task(self, task: Task, tasks: list[Task]) -> RoutingDecision | None:
        if not self.enabled:
            return None
        agent = task.metadata.get("agent")
        if agent == "browser":
            return None
        return self.router.route(task, active_tasks=tasks)

    def bind_task(self, task: Task, decision: RoutingDecision) -> WorkerPool | None:
        return self.router.apply_routing(task, decision)

    def release_task(self, task: Task) -> None:
        pool_id = task.metadata.get("worker_pool")
        if pool_id:
            pool = self.registry.get(pool_id)
            if pool:
                pool.release(task.id)

    def record_result(self, task: Task, result: TaskResult) -> None:
        pool_id = task.metadata.get("worker_pool")
        if not pool_id:
            return
        pool = self.registry.get(pool_id)
        if not pool:
            return
        dur = getattr(result, "duration_sec", 0.0) or 0.0
        if result.success:
            self.auth_monitor.report_success(pool)
            pool.record_success(task, duration_sec=dur)
        else:
            self.auth_monitor.report_failure(pool, result, task)
            pool.record_failure(task, duration_sec=dur)

    def sync_from_tasks(self, task_manager: TaskManager) -> None:
        self.registry.sync_active_workers(task_manager.list_all())

    def snapshots(self, task_manager: TaskManager | None = None) -> list:
        tasks = task_manager.list_all() if task_manager else None
        return self.registry.snapshot_all(tasks)
