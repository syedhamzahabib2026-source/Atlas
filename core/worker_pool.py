"""
Worker pool operational tracking (Phase 13).

WorkerPool tracks availability and metrics — not routing (see worker_router.py).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from core.logger import get_logger
from core.pool_config import (
    AuthMode,
    CostTier,
    PoolConfig,
    PoolType,
    WorkerCapability,
)

if TYPE_CHECKING:
    from core.task_manager import Task

logger = get_logger("worker_pool")


class PoolHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    COOLDOWN = "cooldown"
    DISABLED = "disabled"


@dataclass
class PoolMetrics:
    """Per-pool operational counters."""

    tasks_completed: int = 0
    tasks_failed: int = 0
    tasks_recovered: int = 0
    total_runtime_sec: float = 0.0
    total_cost_usd: float = 0.0
    last_task_at: float | None = None

    @property
    def failure_rate(self) -> float:
        total = self.tasks_completed + self.tasks_failed
        if total == 0:
            return 0.0
        return self.tasks_failed / total

    @property
    def avg_runtime_sec(self) -> float:
        if self.tasks_completed == 0:
            return 0.0
        return self.total_runtime_sec / self.tasks_completed


@dataclass
class PoolStateSnapshot:
    """Serializable pool state for dashboard / runtime."""

    pool_id: str
    pool_type: str
    auth_mode: str
    cost_tier: str
    health: str
    active_workers: int
    busy_workers: int
    idle_slots: int
    max_workers: int
    available: bool
    cooldown_until: float | None
    cooldown_reason: str
    queued_hint: int = 0
    metrics: dict = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)


class WorkerPool:
    """
    Tracks workers (concurrent task slots), availability, and metrics for one pool.

    A "worker" is a logical slot — typically one active Claude tmux task on this pool.
    """

    def __init__(self, config: PoolConfig) -> None:
        self.config = config
        self.metrics = PoolMetrics()
        self._active_task_ids: set[str] = set()
        self._failed_worker_count = 0
        self._cooldown_until: float | None = None
        self._cooldown_reason: str = ""
        self._health = PoolHealth.HEALTHY if config.enabled else PoolHealth.DISABLED

    @property
    def pool_id(self) -> str:
        return self.config.pool_id

    @property
    def pool_type(self) -> PoolType:
        return self.config.pool_type

    @property
    def auth_mode(self) -> AuthMode:
        return self.config.auth_mode

    @property
    def cost_tier(self) -> CostTier:
        return self.config.cost_tier

    @property
    def session_prefix(self) -> str:
        return self.config.session_prefix

    @property
    def capabilities(self) -> list[WorkerCapability]:
        return list(self.config.capabilities)

    @property
    def is_on_cooldown(self) -> bool:
        if self._cooldown_until is None:
            return False
        return time.time() < self._cooldown_until

    def mark_cooldown(self, reason: str, *, duration_sec: float | None = None) -> None:
        dur = duration_sec if duration_sec is not None else self.config.cooldown_sec
        self._cooldown_until = time.time() + dur
        self._cooldown_reason = reason
        self._health = PoolHealth.COOLDOWN
        logger.warning(
            "Pool %s on cooldown for %.0fs: %s",
            self.pool_id,
            dur,
            reason,
        )

    def cooldown_state(self) -> tuple[float | None, str]:
        """(cooldown_until epoch, reason) — for persistence across restarts."""
        return self._cooldown_until, self._cooldown_reason

    def restore_cooldown(self, until: float, reason: str) -> None:
        """Reapply a persisted cooldown; expired timestamps are ignored."""
        if until <= time.time():
            return
        self._cooldown_until = until
        self._cooldown_reason = reason
        self._health = PoolHealth.COOLDOWN
        logger.info(
            "Pool %s cooldown restored (%.0fs remaining): %s",
            self.pool_id,
            until - time.time(),
            reason,
        )

    def clear_cooldown(self) -> None:
        if self._cooldown_until is not None:
            logger.info("Pool %s cooldown cleared", self.pool_id)
        self._cooldown_until = None
        self._cooldown_reason = ""
        if self.config.enabled:
            self._health = PoolHealth.HEALTHY
        else:
            self._health = PoolHealth.DISABLED

    def tick_cooldown(self) -> bool:
        """Return True if cooldown just expired."""
        if not self.is_on_cooldown and self._health == PoolHealth.COOLDOWN:
            self.clear_cooldown()
            return True
        if self.is_on_cooldown:
            return False
        if self._cooldown_until is not None:
            self.clear_cooldown()
            return True
        return False

    def is_available(self) -> bool:
        if not self.config.enabled:
            return False
        if self.is_on_cooldown:
            return False
        return self.idle_slots() > 0

    def busy_count(self) -> int:
        return len(self._active_task_ids)

    def idle_slots(self) -> int:
        return max(0, self.config.max_workers - self.busy_count())

    def acquire(self, task_id: str) -> bool:
        if not self.is_available():
            return False
        self._active_task_ids.add(task_id)
        return True

    def release(self, task_id: str) -> None:
        self._active_task_ids.discard(task_id)

    def record_success(
        self, task: Task, *, duration_sec: float = 0.0, cost_usd: float = 0.0
    ) -> None:
        self.metrics.tasks_completed += 1
        self.metrics.total_runtime_sec += duration_sec
        self.metrics.total_cost_usd += cost_usd
        self.metrics.last_task_at = time.time()
        self.release(task.id)
        if self._health == PoolHealth.DEGRADED and self.metrics.failure_rate < 0.5:
            self._health = PoolHealth.HEALTHY

    def record_failure(
        self, task: Task, *, duration_sec: float = 0.0, cost_usd: float = 0.0
    ) -> None:
        self.metrics.tasks_failed += 1
        self.metrics.total_cost_usd += cost_usd
        self.metrics.last_task_at = time.time()
        self._failed_worker_count += 1
        self.release(task.id)
        if self.metrics.failure_rate > 0.4:
            self._health = PoolHealth.DEGRADED

    def record_recovery(self) -> None:
        self.metrics.tasks_recovered += 1

    def snapshot(self, *, queued_hint: int = 0) -> PoolStateSnapshot:
        self.tick_cooldown()
        busy = self.busy_count()
        max_w = self.config.max_workers
        return PoolStateSnapshot(
            pool_id=self.pool_id,
            pool_type=self.pool_type.value,
            auth_mode=self.auth_mode.value,
            cost_tier=self.cost_tier.value,
            health=self._health.value,
            active_workers=busy,
            busy_workers=busy,
            idle_slots=self.idle_slots(),
            max_workers=max_w,
            available=self.is_available(),
            cooldown_until=self._cooldown_until,
            cooldown_reason=self._cooldown_reason,
            queued_hint=queued_hint,
            metrics={
                "tasks_completed": self.metrics.tasks_completed,
                "tasks_failed": self.metrics.tasks_failed,
                "failure_rate": round(self.metrics.failure_rate, 3),
                "avg_runtime_sec": round(self.metrics.avg_runtime_sec, 1),
                "total_cost_usd": round(self.metrics.total_cost_usd, 4),
            },
            capabilities=[c.value for c in self.capabilities],
        )


class SubscriptionPool(WorkerPool):
    """Claude subscription auth — preferred low-cost pool."""

    def __init__(self, config: PoolConfig | None = None) -> None:
        from core.pool_config import WorkerPoolsConfig

        cfg = config or WorkerPoolsConfig().subscription
        super().__init__(cfg)


class ApiPool(WorkerPool):
    """API-key Claude pool — metered fallback / overflow."""

    def __init__(self, config: PoolConfig | None = None) -> None:
        from core.pool_config import WorkerPoolsConfig

        cfg = config or WorkerPoolsConfig().api
        super().__init__(cfg)


class LocalPool(WorkerPool):
    """
    Placeholder for future Ollama/local workers.

    TODO: OpenAI workers
    TODO: Gemini workers
    TODO: Ollama/local models
    TODO: distributed workers
    TODO: cloud runners
    TODO: dynamic cost optimization
    """

    def __init__(self, config: PoolConfig | None = None) -> None:
        from core.pool_config import WorkerPoolsConfig

        cfg = config or WorkerPoolsConfig().local
        super().__init__(cfg)

    def is_available(self) -> bool:
        return False
