"""
Worker pool coordinator — wires registry, router, and auth monitor (Phase 13).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
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

    def record_result(self, task: Task, result: TaskResult):
        """Record outcome on the task's pool.

        Returns the AuthClassification for failures (None for successes or
        unpooled tasks) so callers never reach into auth monitor internals —
        a shared "last classification" slot misattributes results when two
        tasks finish concurrently.
        """
        pool_id = task.metadata.get("worker_pool")
        if not pool_id:
            return None
        pool = self.registry.get(pool_id)
        if not pool:
            return None
        dur = getattr(result, "duration_sec", 0.0) or 0.0
        cost = float(result.metadata.get("cost_usd") or 0.0)
        if result.success:
            self.auth_monitor.report_success(pool)
            pool.record_success(task, duration_sec=dur, cost_usd=cost)
            return None
        classification = self.auth_monitor.report_failure(pool, result, task)
        pool.record_failure(task, duration_sec=dur, cost_usd=cost)
        return classification

    def sync_from_tasks(self, task_manager: TaskManager) -> None:
        self.registry.sync_active_workers(task_manager.list_all())

    async def cleanup_orphan_sessions(self, task_manager: TaskManager) -> list[str]:
        """
        Kill pool-namespace tmux sessions no task claims.

        RuntimeRecovery deliberately skips atlas-sub-*/atlas-api-* (it only
        owns the root prefix), so without this pass stale pool sessions from
        previous runs accumulate forever. Sessions recorded on any known task
        are kept — failed sessions stay alive for post-mortem.
        """
        known = {
            t.session_name for t in task_manager.list_all() if t.session_name
        }
        cleaned: list[str] = []
        for pool_id, tmux in self.pool_tmux.items():
            if not tmux.is_available():
                continue
            prefix = tmux.session_prefix
            for name in await tmux.list_sessions():
                if not name.startswith(f"{prefix}-"):
                    continue
                if name in known:
                    continue
                if await tmux.kill_session(name):
                    cleaned.append(name)
                    logger.info(
                        "Cleaned orphan pool session %s (pool=%s)", name, pool_id
                    )
        return cleaned

    def snapshots(self, task_manager: TaskManager | None = None) -> list:
        tasks = task_manager.list_all() if task_manager else None
        return self.registry.snapshot_all(tasks)

    def save_state(self, path: Path) -> None:
        """
        Persist active cooldowns (wall-clock epochs, restart-safe).

        Without this, a restart during subscription exhaustion forgets the
        cooldown and routes straight back to the exhausted pool.
        """
        data = {}
        for pool in self.registry.all_pools():
            until, reason = pool.cooldown_state()
            if until and until > time.time():
                data[pool.pool_id] = {"cooldown_until": until, "reason": reason}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            logger.warning("Could not persist pool state to %s", path)

    def load_state(self, path: Path) -> None:
        """Restore persisted cooldowns; expired entries are ignored."""
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Could not read pool state from %s", path)
            return
        for pool_id, entry in (data or {}).items():
            pool = self.registry.get(pool_id)
            if pool:
                pool.restore_cooldown(
                    float(entry.get("cooldown_until", 0.0)),
                    str(entry.get("reason", "restored from previous run")),
                )
