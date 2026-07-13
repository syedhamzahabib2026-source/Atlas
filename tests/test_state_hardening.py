"""State-hardening fixes: crash-window requeue, audit events, pool state."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from core.pool_config import WorkerPoolsConfig
from core.runtime_recovery import RecoveryReport, RuntimeRecovery
from core.task_manager import Task, TaskManager, TaskStatus
from core.task_store import TaskStore
from core.worker_pool_manager import WorkerPoolManager
from core.worker_router import RoutingDecision


class TestRecoveryPendingRequeue:
    """FAILED persisted before the retry decision must survive a crash."""

    def _recover(self, task: Task) -> Task:
        tm = TaskManager()
        tm._tasks[task.id] = task
        recovery = RuntimeRecovery(config=SimpleNamespace())
        asyncio.run(
            recovery._normalize_interrupted_tasks(tm, RecoveryReport())
        )
        return task

    def test_failed_with_recovery_pending_requeues(self):
        task = Task(title="t", status=TaskStatus.FAILED)
        task.metadata["recovery_pending"] = True
        self._recover(task)
        assert task.status == TaskStatus.PENDING
        assert "recovery_pending" not in task.metadata

    def test_failed_without_flag_stays_failed(self):
        task = Task(title="t", status=TaskStatus.FAILED)
        self._recover(task)
        assert task.status == TaskStatus.FAILED

    def test_running_still_resets(self):
        task = Task(title="t", status=TaskStatus.RUNNING)
        self._recover(task)
        assert task.status == TaskStatus.PENDING


class TestTaskEvents:
    """Status transitions must land in the task_events audit table."""

    def test_transitions_recorded(self, tmp_path: Path):
        async def scenario() -> list[dict]:
            store = TaskStore(tmp_path / "tasks.db")
            await store.initialize()
            task = Task(title="t")
            await store.upsert(task)          # (new) -> pending
            task.status = TaskStatus.RUNNING
            await store.upsert(task)          # pending -> running
            await store.upsert(task)          # no transition — no event
            task.status = TaskStatus.FAILED
            task.error = "boom"
            await store.upsert(task)          # running -> failed
            await store.record_event(task.id, "pr_opened", "PR #7")
            events = await store.events_since("")
            await store.close()
            return events

        events = asyncio.run(scenario())
        kinds = [e["event"] for e in events]
        assert kinds.count("status_change") == 3
        assert "pr_opened" in kinds
        failed = [e for e in events if "failed" in e["detail"]]
        assert failed and "boom" in failed[0]["detail"]


class TestPoolStatePersistence:
    def test_cooldown_survives_save_load(self, tmp_path: Path):
        path = tmp_path / "pool_state.json"
        mgr = WorkerPoolManager(WorkerPoolsConfig(enabled=True))
        sub = mgr.registry.get("subscription")
        sub.mark_cooldown("exhausted", duration_sec=600)
        mgr.save_state(path)

        fresh = WorkerPoolManager(WorkerPoolsConfig(enabled=True))
        fresh.load_state(path)
        restored = fresh.registry.get("subscription")
        assert restored.is_on_cooldown
        assert not restored.is_available()

    def test_expired_cooldown_not_restored(self, tmp_path: Path):
        path = tmp_path / "pool_state.json"
        path.write_text(
            '{"subscription": {"cooldown_until": %f, "reason": "old"}}'
            % (time.time() - 5)
        )
        mgr = WorkerPoolManager(WorkerPoolsConfig(enabled=True))
        mgr.load_state(path)
        assert not mgr.registry.get("subscription").is_on_cooldown


class TestPoolLaunchCommand:
    def test_routing_stamps_launch_command(self):
        mgr = WorkerPoolManager(WorkerPoolsConfig(enabled=True))
        task = Task(title="t")
        decision = RoutingDecision(pool_id="subscription", reason="test")
        pool = mgr.bind_task(task, decision)
        assert pool is not None
        assert task.metadata["pool_launch_command"] == "claude"

    def test_agent_uses_pool_launch_command(self, tmp_path: Path):
        from agents.claude_code import ClaudeCodeAgent
        from sessions.tmux_manager import TmuxManager

        agent = ClaudeCodeAgent(
            tmux=TmuxManager(session_prefix="atlas-test"),
            projects_dir=tmp_path,
        )
        task = Task(title="t")
        assert agent._launch_command(task) == "claude"
        task.metadata["pool_launch_command"] = "ollama-cli"
        assert agent._launch_command(task) == "ollama-cli"
