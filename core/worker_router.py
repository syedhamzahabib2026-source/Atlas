"""
Worker router — decides which pool executes a task (Phase 13).

Routing policy (default):
  1. subscription pool
  2. API pool
  3. local pool (placeholder, not implemented)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.logger import get_logger
from core.pool_config import PoolType, WorkerCapability

if TYPE_CHECKING:
    from core.auth_monitor import AuthMonitor
    from core.task_manager import Task
    from core.worker_pool import WorkerPool
    from core.worker_registry import WorkerRegistry

logger = get_logger("worker_router")


@dataclass
class RoutingDecision:
    pool_id: str
    reason: str
    overflow: bool = False
    preferred_pool: str | None = None


class WorkerRouter:
    """Select execution pool based on capabilities, availability, and priority."""

    def __init__(
        self,
        registry: WorkerRegistry,
        auth_monitor: AuthMonitor | None = None,
    ) -> None:
        self.registry = registry
        self.auth_monitor = auth_monitor

    def _required_capabilities(self, task: Task) -> list[WorkerCapability]:
        raw = task.metadata.get("capabilities") or task.metadata.get("required_capabilities")
        if not raw:
            return []
        out: list[WorkerCapability] = []
        for item in raw:
            if isinstance(item, WorkerCapability):
                out.append(item)
            else:
                try:
                    out.append(WorkerCapability(str(item)))
                except ValueError:
                    pass
        return out

    def _task_priority(self, task: Task) -> int:
        return int(task.metadata.get("priority", 50))

    def _explicit_pool(self, task: Task) -> str | None:
        forced = task.metadata.get("worker_pool") or task.metadata.get("pool")
        if forced:
            return str(forced)
        return None

    def route(self, task: Task, *, active_tasks: list[Task] | None = None) -> RoutingDecision | None:
        """
        Choose a pool for the task. Returns None if no pool can accept work.

        Browser tasks skip pool routing (handled by browser agent).
        """
        agent = task.metadata.get("agent")
        if agent == "browser":
            return None

        if not self.registry.enabled:
            return None

        if active_tasks:
            self.registry.sync_active_workers(active_tasks)

        if self.auth_monitor:
            restored = self.auth_monitor.tick()
            for pid in restored:
                logger.info("Pool %s restored after cooldown", pid)

        explicit = self._explicit_pool(task)
        if explicit:
            pool = self.registry.get(explicit)
            if pool and pool.is_available():
                return RoutingDecision(
                    pool_id=explicit,
                    reason="explicit pool request",
                    preferred_pool=explicit,
                )

        required = self._required_capabilities(task)
        candidates = self.registry.pools_for_capabilities(required)
        priority = self._task_priority(task)

        for pool in candidates:
            if pool.pool_type == PoolType.LOCAL:
                continue
            if not pool.is_available():
                continue
            if pool.pool_type == PoolType.SUBSCRIPTION and self.auth_monitor:
                if self.auth_monitor.subscription_on_cooldown:
                    continue

            reason = self._reason_for_pool(pool, priority)
            overflow = pool.pool_id == "api" and (
                self.auth_monitor is not None
                and self.auth_monitor.subscription_on_cooldown
            )
            return RoutingDecision(
                pool_id=pool.pool_id,
                reason=reason,
                overflow=overflow,
                preferred_pool="subscription",
            )

        return None

    def _reason_for_pool(self, pool: WorkerPool, task_priority: int) -> str:
        if pool.pool_id == "subscription":
            return "default subscription-first policy"
        if pool.pool_id == "api":
            if task_priority >= 80:
                return "priority overflow to API pool"
            return "subscription unavailable — API fallback"
        return f"routed to {pool.pool_id}"

    def apply_routing(self, task: Task, decision: RoutingDecision) -> WorkerPool | None:
        """Bind pool metadata on task and acquire a worker slot."""
        pool = self.registry.get(decision.pool_id)
        if not pool:
            return None
        if not pool.acquire(task.id):
            return None

        task.metadata["worker_pool"] = decision.pool_id
        task.metadata["session_prefix"] = pool.session_prefix
        task.metadata["pool_auth_mode"] = pool.auth_mode.value
        task.metadata["pool_cost_tier"] = pool.cost_tier.value
        task.metadata["pool_launch_command"] = pool.config.launch_command
        task.metadata["pool_execution_mode"] = pool.config.execution_mode
        if pool.config.model:
            task.metadata.setdefault("model", pool.config.model)
        task.metadata["routing_reason"] = decision.reason
        if decision.overflow:
            task.metadata["routing_overflow"] = True

        logger.info(
            "Routed task %s -> pool %s (%s)",
            task.id[:8],
            decision.pool_id,
            decision.reason,
        )
        return pool
