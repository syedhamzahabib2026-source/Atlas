"""
Execution backends — structured, non-interactive agent runs.

The tmux path drives an interactive TUI and infers completion from screen
scraping; every marker heuristic in that stack exists to compensate. An
Executor runs a prompt to completion and returns structured facts instead:
exit code, result text, cost, session id. No sentinels, no idle detection.

Implementations:
  HeadlessClaudeExecutor — `claude -p --output-format json` subprocess
  OllamaExecutor         — local model via Ollama HTTP API (LocalPool)

Executors are stateless per call — safe to share across concurrent tasks.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from core.logger import get_logger

logger = get_logger("executor")


@dataclass
class ExecutionRequest:
    prompt: str
    working_dir: Path
    env: dict[str, str] = field(default_factory=dict)
    timeout_sec: float = 3600.0
    model: str = ""


@dataclass
class ExecutionResult:
    exit_code: int
    output: str = ""           # final result text (model's answer)
    raw_output: str = ""       # full stdout/stderr for diagnostics
    cost_usd: float | None = None
    session_id: str | None = None
    num_turns: int | None = None
    duration_sec: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    error: str = ""

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled


class Executor:
    """Interface every execution backend implements."""

    name: str = "base"

    def is_available(self) -> bool:
        raise NotImplementedError

    async def execute(
        self,
        request: ExecutionRequest,
        cancel: asyncio.Event | None = None,
    ) -> ExecutionResult:
        raise NotImplementedError


def _to_wsl_path(path: Path) -> str:
    """C:\\Users\\foo -> /mnt/c/Users/foo (same rule as TmuxManager)."""
    resolved = Path(path).resolve()
    drive = resolved.drive.rstrip(":").lower()
    if drive:
        rest = resolved.as_posix().split(":", 1)[-1]
        return f"/mnt/{drive}{rest}"
    return resolved.as_posix()


class HeadlessClaudeExecutor(Executor):
    """
    Run Claude Code non-interactively: prompt on stdin, JSON result on stdout.

    On Windows without a native `claude`, runs inside WSL (matching the tmux
    setup, where the CLI lives in the Linux environment).
    """

    name = "claude_headless"

    def __init__(
        self,
        command: str = "claude",
        *,
        skip_permissions: bool = True,
        extra_args: list[str] | None = None,
    ) -> None:
        self.command = command
        self.skip_permissions = skip_permissions
        self.extra_args = list(extra_args or [])

    def _native_launcher(self) -> list[str] | None:
        """
        Resolved argv prefix for a native launch, or None if not on PATH.

        On Windows, npm installs `claude` as a .cmd/.ps1 shim; CreateProcess
        cannot exec those directly, so batch shims are wrapped in `cmd /c`.
        """
        path = shutil.which(self.command)
        if not path:
            return None
        if sys.platform == "win32" and path.lower().endswith((".cmd", ".bat")):
            return ["cmd", "/c", path]
        return [path]

    def _use_wsl(self) -> bool:
        if self._native_launcher():
            return False
        return sys.platform == "win32" and shutil.which("wsl") is not None

    def is_available(self) -> bool:
        return self._native_launcher() is not None or self._use_wsl()

    def _claude_args(self, request: ExecutionRequest) -> list[str]:
        args = ["-p", "--output-format", "json"]
        if self.skip_permissions:
            args.append("--dangerously-skip-permissions")
        if request.model:
            args.extend(["--model", request.model])
        args.extend(self.extra_args)
        return args

    def build_argv(self, request: ExecutionRequest) -> list[str]:
        """Full argv — native (resolved path) or WSL-wrapped."""
        claude_args = self._claude_args(request)
        launcher = self._native_launcher()
        if launcher:
            return launcher + claude_args
        wsl_cwd = _to_wsl_path(request.working_dir)
        argv = ["wsl", "--cd", wsl_cwd, "--"]
        if request.env:
            argv.append("env")
            argv.extend(f"{k}={v}" for k, v in request.env.items())
        argv.append(self.command)
        argv.extend(claude_args)
        return argv

    @staticmethod
    async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        """
        Kill the process AND its children.

        On Windows the launcher is `cmd /c <shim>` — proc.kill() alone leaves
        the node child running (and holding the working directory open).
        """
        if proc.returncode is not None:
            return
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            import signal as _signal

            try:
                os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    @staticmethod
    def parse_output(stdout: str) -> dict:
        """
        Extract the result JSON object from stdout.

        Tolerates banner noise around the JSON: tries the whole payload, then
        each line from the end (the result object is printed last).
        """
        stdout = stdout.strip()
        if not stdout:
            return {}
        try:
            data = json.loads(stdout)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
        return {}

    async def execute(
        self,
        request: ExecutionRequest,
        cancel: asyncio.Event | None = None,
    ) -> ExecutionResult:
        argv = self.build_argv(request)
        use_wsl = self._use_wsl()
        env = None
        cwd = None
        if not use_wsl:
            env = {**os.environ, **request.env}
            cwd = str(request.working_dir)

        logger.info(
            "headless execute: cwd=%s wsl=%s prompt_chars=%d",
            request.working_dir,
            use_wsl,
            len(request.prompt),
        )
        started = time.monotonic()
        result = ExecutionResult(exit_code=-1)

        kwargs: dict = {}
        if sys.platform != "win32":
            # New session so a kill can take the whole process group with it.
            kwargs["start_new_session"] = True
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                **kwargs,
            )
        except OSError as exc:
            result.error = f"failed to launch {argv[0]}: {exc}"
            return result

        comm = asyncio.ensure_future(
            proc.communicate(input=request.prompt.encode("utf-8"))
        )
        waiters: list[asyncio.Future] = [comm]
        cancel_waiter: asyncio.Task | None = None
        if cancel is not None:
            cancel_waiter = asyncio.ensure_future(cancel.wait())
            waiters.append(cancel_waiter)

        done, _ = await asyncio.wait(
            waiters,
            timeout=request.timeout_sec,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if comm not in done:
            # Timeout or operator cancel — either way the process must die.
            result.cancelled = cancel_waiter in done if cancel_waiter else False
            result.timed_out = not result.cancelled
            await self._kill_tree(proc)
            try:
                await asyncio.wait_for(comm, timeout=10)
            except (asyncio.TimeoutError, Exception):
                pass
            result.error = "cancelled by operator" if result.cancelled else (
                f"timed out after {request.timeout_sec}s"
            )
            result.duration_sec = time.monotonic() - started
            if cancel_waiter and not cancel_waiter.done():
                cancel_waiter.cancel()
            return result

        if cancel_waiter and not cancel_waiter.done():
            cancel_waiter.cancel()

        stdout_b, stderr_b = comm.result()
        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        result.exit_code = proc.returncode if proc.returncode is not None else -1
        result.raw_output = stdout + (f"\n[stderr]\n{stderr}" if stderr.strip() else "")
        result.duration_sec = time.monotonic() - started

        data = self.parse_output(stdout)
        result.output = str(data.get("result", "")) or stdout.strip()
        result.session_id = data.get("session_id")
        if data.get("total_cost_usd") is not None:
            try:
                result.cost_usd = float(data["total_cost_usd"])
            except (TypeError, ValueError):
                pass
        if data.get("num_turns") is not None:
            try:
                result.num_turns = int(data["num_turns"])
            except (TypeError, ValueError):
                pass
        if data.get("is_error") and result.exit_code == 0:
            # CLI exited 0 but reported an error result (e.g. max turns)
            result.exit_code = 1
            result.error = str(data.get("subtype") or "error result")
        if result.exit_code != 0 and not result.error:
            result.error = (stderr.strip() or result.output or "non-zero exit")[:500]
        return result


class OllamaExecutor(Executor):
    """
    Single-shot generation via a local Ollama server (LocalPool backbone).

    Deliberately non-agentic: local models get bounded transform tasks
    (summaries, commit messages, lint fixes), not multi-step repo work.
    Uses stdlib urllib in a thread — consistent with pr_manager's no-new-deps
    pattern.
    """

    name = "ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "",
        *,
        connect_timeout_sec: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.connect_timeout_sec = connect_timeout_sec

    def is_available(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=self.connect_timeout_sec):
                return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _post(self, payload: dict, timeout_sec: float) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def parse_response(data: dict) -> str:
        return str(data.get("response", ""))

    async def execute(
        self,
        request: ExecutionRequest,
        cancel: asyncio.Event | None = None,
    ) -> ExecutionResult:
        model = request.model or self.model
        if not model:
            return ExecutionResult(exit_code=-1, error="no model configured")

        payload = {"model": model, "prompt": request.prompt, "stream": False}
        started = time.monotonic()
        try:
            data = await asyncio.to_thread(
                self._post, payload, request.timeout_sec
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return ExecutionResult(
                exit_code=-1,
                error=f"ollama request failed: {exc}",
                duration_sec=time.monotonic() - started,
            )

        text = self.parse_response(data)
        return ExecutionResult(
            exit_code=0 if text else 1,
            output=text,
            raw_output=text,
            cost_usd=0.0,
            duration_sec=time.monotonic() - started,
            error="" if text else "empty response from model",
        )
