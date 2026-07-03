"""Tier 1.1 — pool slots must be released on every exit path."""

from __future__ import annotations

from core.pool_config import AuthMode, CostTier, PoolConfig, PoolType
from core.task_manager import Task
from core.worker_pool import WorkerPool


def make_pool(max_workers: int = 2) -> WorkerPool:
    return WorkerPool(
        PoolConfig(
            pool_id="subscription",
            pool_type=PoolType.SUBSCRIPTION,
            session_prefix="atlas-sub",
            auth_mode=AuthMode.SUBSCRIPTION,
            cost_tier=CostTier.FREE,
            max_workers=max_workers,
        )
    )


class TestSlotAccounting:
    def test_acquire_consumes_slot(self):
        pool = make_pool(max_workers=1)
        assert pool.acquire("t1")
        assert not pool.is_available()
        assert not pool.acquire("t2")

    def test_release_frees_slot(self):
        pool = make_pool(max_workers=1)
        pool.acquire("t1")
        pool.release("t1")
        assert pool.is_available()
        assert pool.acquire("t2")

    def test_release_is_idempotent(self):
        pool = make_pool(max_workers=1)
        pool.acquire("t1")
        pool.release("t1")
        pool.release("t1")  # double release must not raise or corrupt state
        assert pool.idle_slots() == 1

    def test_record_success_releases(self):
        pool = make_pool(max_workers=1)
        t = Task(title="x")
        pool.acquire(t.id)
        pool.record_success(t, duration_sec=1.0)
        assert pool.is_available()

    def test_record_failure_releases(self):
        pool = make_pool(max_workers=1)
        t = Task(title="x")
        pool.acquire(t.id)
        pool.record_failure(t, duration_sec=1.0)
        assert pool.is_available()


class TestManagerReleaseTask:
    def test_release_task_by_metadata(self):
        from core.pool_config import WorkerPoolsConfig
        from core.worker_pool_manager import WorkerPoolManager

        cfg = WorkerPoolsConfig(enabled=True)
        mgr = WorkerPoolManager(cfg)
        pool = mgr.registry.get("subscription")
        assert pool is not None

        t = Task(title="x")
        t.metadata["worker_pool"] = "subscription"
        pool.acquire(t.id)
        assert pool.busy_count() == 1

        mgr.release_task(t)
        assert pool.busy_count() == 0

        # Releasing again (e.g. record_result later) must be harmless
        mgr.release_task(t)
        assert pool.busy_count() == 0
