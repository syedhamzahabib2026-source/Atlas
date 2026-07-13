"""Executor abstraction — headless Claude + Ollama (LocalPool foundation)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core.executor import (
    ExecutionRequest,
    ExecutionResult,
    HeadlessClaudeExecutor,
    OllamaExecutor,
    _to_wsl_path,
)
from core.task_manager import Task
from core.task_result import ResultStatus


SUCCESS_JSON = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Added the button and committed.",
        "session_id": "sess-123",
        "total_cost_usd": 0.0421,
        "num_turns": 7,
        "duration_ms": 61000,
    }
)


class TestParseOutput:
    def test_clean_json(self):
        data = HeadlessClaudeExecutor.parse_output(SUCCESS_JSON)
        assert data["result"] == "Added the button and committed."
        assert data["total_cost_usd"] == 0.0421

    def test_json_with_banner_noise(self):
        noisy = "some banner line\nwarning: foo\n" + SUCCESS_JSON
        data = HeadlessClaudeExecutor.parse_output(noisy)
        assert data.get("session_id") == "sess-123"

    def test_garbage_returns_empty(self):
        assert HeadlessClaudeExecutor.parse_output("not json at all") == {}
        assert HeadlessClaudeExecutor.parse_output("") == {}


class TestArgvBuilding:
    def test_native_argv(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/claude")
        ex = HeadlessClaudeExecutor()
        argv = ex.build_argv(
            ExecutionRequest(prompt="p", working_dir=Path("/tmp"))
        )
        assert argv[0] == "/usr/bin/claude"
        assert "-p" in argv
        assert "--output-format" in argv and "json" in argv
        assert "--dangerously-skip-permissions" in argv

    def test_model_flag(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/claude")
        ex = HeadlessClaudeExecutor()
        argv = ex.build_argv(
            ExecutionRequest(prompt="p", working_dir=Path("/tmp"), model="opus")
        )
        assert "--model" in argv and "opus" in argv

    def test_skip_permissions_can_be_disabled(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/claude")
        ex = HeadlessClaudeExecutor(skip_permissions=False)
        argv = ex.build_argv(
            ExecutionRequest(prompt="p", working_dir=Path("/tmp"))
        )
        assert "--dangerously-skip-permissions" not in argv

    def test_wsl_path_translation(self):
        assert _to_wsl_path(Path("C:/Users/foo/proj")) == "/mnt/c/Users/foo/proj"


class _FakeExecutor:
    """Scripted executor for agent-mapping tests."""

    def __init__(self, result: ExecutionResult) -> None:
        self.result = result
        self.last_request: ExecutionRequest | None = None

    def is_available(self) -> bool:
        return True

    async def execute(self, request, cancel=None):
        self.last_request = request
        return self.result


def make_agent(tmp_path: Path, mode: str = "headless"):
    from agents.claude_code import ClaudeCodeAgent, ClaudeCodeConfig
    from sessions.tmux_manager import TmuxManager

    return ClaudeCodeAgent(
        tmux=TmuxManager(session_prefix="atlas-test"),
        projects_dir=tmp_path,
        config=ClaudeCodeConfig(execution_mode=mode),
    )


class TestExecutionModeResolution:
    def test_agent_default(self, tmp_path):
        agent = make_agent(tmp_path, mode="tmux")
        assert agent._resolve_execution_mode(Task(title="t")) == "tmux"

    def test_pool_overrides_agent(self, tmp_path):
        agent = make_agent(tmp_path, mode="tmux")
        t = Task(title="t")
        t.metadata["pool_execution_mode"] = "headless"
        assert agent._resolve_execution_mode(t) == "headless"

    def test_task_overrides_pool(self, tmp_path):
        agent = make_agent(tmp_path, mode="tmux")
        t = Task(title="t")
        t.metadata["pool_execution_mode"] = "headless"
        t.metadata["execution_mode"] = "tmux"
        assert agent._resolve_execution_mode(t) == "tmux"


class TestHeadlessRunMapping:
    def _run(self, tmp_path: Path, exec_result: ExecutionResult, task: Task):
        agent = make_agent(tmp_path)
        fake = _FakeExecutor(exec_result)
        agent.headless_executor = fake
        result = asyncio.run(agent.run(task))
        return agent, fake, result

    def test_success_maps_to_completed_with_cost(self, tmp_path):
        task = Task(title="t", metadata={"working_dir": str(tmp_path)})
        _, fake, result = self._run(
            tmp_path,
            ExecutionResult(
                exit_code=0,
                output="done and committed",
                cost_usd=0.05,
                session_id="s1",
            ),
            task,
        )
        assert result.status == ResultStatus.COMPLETED
        assert result.metadata["cost_usd"] == 0.05
        assert task.metadata["cost_usd"] == 0.05

    def test_failure_maps_to_failed(self, tmp_path):
        task = Task(title="t", metadata={"working_dir": str(tmp_path)})
        _, _, result = self._run(
            tmp_path,
            ExecutionResult(exit_code=1, error="max turns exceeded"),
            task,
        )
        assert result.status == ResultStatus.FAILED
        assert "max turns exceeded" in result.errors

    def test_timeout_maps_to_timeout(self, tmp_path):
        task = Task(title="t", metadata={"working_dir": str(tmp_path)})
        _, _, result = self._run(
            tmp_path,
            ExecutionResult(exit_code=-1, timed_out=True, error="timed out after 5s"),
            task,
        )
        assert result.status == ResultStatus.TIMEOUT

    def test_cancel_maps_to_cancelled(self, tmp_path):
        task = Task(title="t", metadata={"working_dir": str(tmp_path)})
        _, _, result = self._run(
            tmp_path,
            ExecutionResult(exit_code=-1, cancelled=True, error="cancelled"),
            task,
        )
        assert result.status == ResultStatus.CANCELLED

    def test_api_pool_injects_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
        task = Task(
            title="t",
            metadata={"working_dir": str(tmp_path), "pool_auth_mode": "api_key"},
        )
        _, fake, _ = self._run(tmp_path, ExecutionResult(exit_code=0, output="ok"), task)
        assert fake.last_request.env.get("ANTHROPIC_API_KEY") == "sk-test-123"

    def test_subscription_pool_no_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
        task = Task(
            title="t",
            metadata={"working_dir": str(tmp_path), "pool_auth_mode": "subscription"},
        )
        _, fake, _ = self._run(tmp_path, ExecutionResult(exit_code=0, output="ok"), task)
        assert "ANTHROPIC_API_KEY" not in fake.last_request.env

    def test_headless_prompt_has_no_sentinel_protocol(self, tmp_path):
        agent = make_agent(tmp_path)
        t = Task(title="t", description="add a button")
        prompt = agent._build_prompt(t, headless=True)
        assert "ATLAS_TASK_" not in prompt
        assert "git add -A" in prompt


class TestOllamaExecutor:
    def test_parse_response(self):
        assert OllamaExecutor.parse_response({"response": "hi"}) == "hi"
        assert OllamaExecutor.parse_response({}) == ""

    def test_no_model_fails_fast(self):
        ex = OllamaExecutor()
        result = asyncio.run(
            ex.execute(ExecutionRequest(prompt="p", working_dir=Path(".")))
        )
        assert result.exit_code == -1
        assert "no model" in result.error

    def test_request_failure_is_structured(self, monkeypatch):
        ex = OllamaExecutor(model="qwen3-coder", base_url="http://127.0.0.1:9")

        def boom(payload, timeout_sec):
            raise OSError("connection refused")

        monkeypatch.setattr(ex, "_post", boom)
        result = asyncio.run(
            ex.execute(ExecutionRequest(prompt="p", working_dir=Path(".")))
        )
        assert result.exit_code == -1
        assert "ollama request failed" in result.error

    def test_success_is_free(self, monkeypatch):
        ex = OllamaExecutor(model="qwen3-coder")
        monkeypatch.setattr(ex, "_post", lambda p, t: {"response": "summary text"})
        result = asyncio.run(
            ex.execute(ExecutionRequest(prompt="p", working_dir=Path(".")))
        )
        assert result.success
        assert result.output == "summary text"
        assert result.cost_usd == 0.0


class TestPoolExecutionModeStamping:
    def test_router_stamps_mode_and_model(self):
        from core.pool_config import WorkerPoolsConfig
        from core.worker_pool_manager import WorkerPoolManager
        from core.worker_router import RoutingDecision

        cfg = WorkerPoolsConfig(enabled=True)
        cfg.subscription.execution_mode = "headless"
        cfg.subscription.model = "opus"
        mgr = WorkerPoolManager(cfg)
        task = Task(title="t")
        pool = mgr.bind_task(
            task, RoutingDecision(pool_id="subscription", reason="test")
        )
        assert pool is not None
        assert task.metadata["pool_execution_mode"] == "headless"
        assert task.metadata["model"] == "opus"
