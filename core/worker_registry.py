"""
Worker registry — registers pools and worker capabilities (Phase 13).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.logger import get_logger
from core.pool_config import PoolConfig, PoolType, WorkerCapability, WorkerPoolsConfig
from core.worker_pool import ApiPool, LocalPool, SubscriptionPool, WorkerPool

if TYPE_CHECKING:
    from core.task_manager import Task

logger = get_logger("worker_registry")


class WorkerRegistry:
    """Central registry of execution pools and their capabilities."""

    def __init__(self, pools_config: WorkerPoolsConfig | None = None) -> None:
        self.pools_config = pools_config or WorkerPoolsConfig()
        self._pools: dict[str, WorkerPool] = {}
        self._build_pools()

    def _build_pools(self) -> None:
        cfg = self.pools_config
        if cfg.subscription.enabled:
            self._pools["subscription"] = SubscriptionPool(cfg.subscription)
        if cfg.api.enabled:
            self._pools["api"] = ApiPool(cfg.api)
        if cfg.local.enabled:
            self._pools["local"] = LocalPool(cfg.local)
        else:
            self._pools["local"] = LocalPool(cfg.local)

        logger.info(
            "Worker pools registered: %s",
            [f"{p.pool_id}({p.config.max_workers})" for p in self._pools.values()],
        )

    @property
    def enabled(self) -> bool:
        return self.pools_config.enabled

    def get(self, pool_id: str) -> WorkerPool | None:
        return self._pools.get(pool_id)

    def all_pools(self) -> list[WorkerPool]:
        return list(self._pools.values())

    def routing_order(self) -> list[WorkerPool]:
        """Pools sorted by routing priority (lower priority value = tried first)."""
        return sorted(
            self._pools.values(),
            key=lambda p: (p.config.priority, p.pool_id),
        )

    def pools_for_capabilities(
        self, required: list[WorkerCapability]
    ) -> list[WorkerPool]:
        if not required:
            return self.routing_order()
        out: list[WorkerPool] = []
        for pool in self.routing_order():
            caps = set(pool.capabilities)
            if all(r in caps for r in required):
                out.append(pool)
        return out

    def count_active_by_pool(self, tasks: list[Task]) -> dict[str, int]:
        from core.task_manager import TaskManager

        counts: dict[str, int] = {p.pool_id: 0 for p in self._pools.values()}
        for t in tasks:
            if t.status not in TaskManager.ACTIVE_STATUSES:
                continue
            pid = t.metadata.get("worker_pool")
            if pid and pid in counts:
                counts[pid] += 1
        return counts

    def sync_active_workers(self, tasks: list[Task]) -> None:
        """Reconcile pool busy counts from active tasks (e.g. after restart)."""
        counts = self.count_active_by_pool(tasks)
        for pool in self._pools.values():
            pool._active_task_ids.clear()
        from core.task_manager import TaskManager

        for t in tasks:
            if t.status not in TaskManager.ACTIVE_STATUSES:
                continue
            pid = t.metadata.get("worker_pool")
            pool = self._pools.get(pid or "")
            if pool:
                pool._active_task_ids.add(t.id)

        for pool_id, count in counts.items():
            if count:
                logger.debug("Pool %s active tasks: %d", pool_id, count)

    def snapshot_all(self, tasks: list[Task] | None = None) -> list:
        queued: dict[str, int] = {}
        if tasks:
            from core.task_manager import TaskStatus

            for t in tasks:
                if t.status in (TaskStatus.PENDING, TaskStatus.RETRYING):
                    pid = t.metadata.get("worker_pool") or "unassigned"
                    queued[pid] = queued.get(pid, 0) + 1

        return [
            p.snapshot(queued_hint=queued.get(p.pool_id, 0))
            for p in self.routing_order()
        ]

    def update_config(self, pool_id: str, **kwargs) -> None:
        pool = self._pools.get(pool_id)
        if not pool:
            return
        for key, val in kwargs.items():
            if hasattr(pool.config, key):
                setattr(pool.config, key, val)
