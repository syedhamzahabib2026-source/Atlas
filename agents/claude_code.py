"""
Claude Code terminal agent — tmux-driven execution (Phase 2).

Atlas does not call the Anthropic API here; it controls the `claude` CLI in a tmux pane.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agents.base import BaseAgent
from core.logger import get_logger
from core.task_result import ResultStatus, TaskResult
from sessions.tmux_manager import TmuxManager

if TYPE_CHECKING:
    from core.task_manager import Task

logger = get_logger("agents.claude_code")


@dataclass
class ClaudeCodeConfig:
    """Tunable behavior for terminal control (non-secret)."""

    command: str = "claude"
    max_execution_sec: float = 3600.0
    poll_interval_sec: float = 2.0
    launch_wait_sec: float = 15.0
    idle_stable_polls: int = 3
    capture_lines: int = 200
    kill_session_on_finish: bool = False
    kill_session_on_success: bool = True
    # "tmux" (interactive pane, marker heuristics) or "headless"
    # (claude -p --output-format json: exit code + cost, no scraping).
    execution_mode: str = "tmux"


# Structured completion sentinels — instructed in every prompt (Tier 1.4).
# These are the primary, high-confidence completion signal.
SENTINEL_COMPLETE = "ATLAS_TASK_COMPLETE"
SENTINEL_FAILED = "ATLAS_TASK_FAILED"

# Fatal markers that justify failing immediately even mid-run: they indicate
# the CLI itself cannot proceed (launch/auth problems), not code-level errors
# Claude may be in the middle of fixing.
#
# Use specific phrases only — bare "usage limit" / "rate limit" false-positive
# on Claude Code welcome banners (e.g. Fable 5 promo: "weekly usage limit on").
_FATAL_MARKERS = (
    "command not found",
    "enoent",
    "api error",
    "rate limit exceeded",
    "usage limit reached",
    "usage limit exceeded",
    "hit your usage limit",
    "quota exceeded",
    "out of credits",
)

# Soft error markers — only consulted when the session has gone idle, to
# classify how the run ended. Never fail mid-run on these: Claude routinely
# prints and then fixes errors during normal work.
_IDLE_ERROR_MARKERS = (
    "traceback (most recent call last)",
    "permission denied",
)

# How many trailing lines to scan for markers (avoids matching scrollback
# from earlier in the session or echoes of the injected prompt).
_MARKER_TAIL_LINES = 25

# Claude Code often shows an input prompt when idle (varies by version)
_IDLE_PROMPT_PATTERNS = (
    re.compile(r"›\s*$", re.MULTILINE),
    re.compile(r">\s*$", re.MULTILINE),
    re.compile(r"\?\s+for shortcuts", re.IGNORECASE),
)


@dataclass
class _RunState:
    """
    Per-run mutable state, threaded through the execution pipeline.

    Must NOT live on the agent instance: the orchestrator shares one agent
    across tasks, so instance attributes get clobbered the moment two tasks
    run concurrently (task A's monitor loop watching task B's session).
    """

    tmux: TmuxManager
    session_name: str = ""
    last_output: str = ""


class ClaudeCodeAgent(BaseAgent):
    """Runs tasks via Claude Code in a managed tmux session."""

    name = "claude_code"

    def __init__(
        self,
        tmux: TmuxManager,
        projects_dir: Path,
        config: ClaudeCodeConfig | None = None,
        pool_tmux: dict[str, TmuxManager] | None = None,
    ) -> None:
        self.tmux = tmux
        self.pool_tmux = pool_tmux or {}
        self.projects_dir = projects_dir
        self.config = config or ClaudeCodeConfig()
        # Injectable for tests; lazily defaults to HeadlessClaudeExecutor.
        self.headless_executor = None

    def can_handle(self, task: Task) -> bool:
        agent = task.metadata.get("agent")
        if agent == "browser":
            return False
        return agent is None or agent == self.name

    def _tmux_for(self, task: Task) -> TmuxManager:
        pool_id = task.metadata.get("worker_pool")
        if pool_id and pool_id in self.pool_tmux:
            return self.pool_tmux[pool_id]
        prefix = task.metadata.get("session_prefix")
        if prefix:
            return TmuxManager(session_prefix=str(prefix), socket_path=self.tmux.socket_path)
        return self.tmux

    def _resolve_session_name(self, task: Task) -> str:
        # Explicit operator override only. task.metadata["session_name"] is a
        # *record* of the last session, deliberately NOT honored here: reusing
        # a failed attempt's session re-enters broken CLI state (bug H2).
        custom = task.metadata.get("custom_session_name")
        if custom:
            return str(custom)
        # Session-per-task (Tier 1.2): include task id so concurrent tasks on
        # the same project never share a Claude session. Retries get a fresh
        # suffix so they never inherit a failed session's broken CLI state.
        if task.project_id:
            key = f"{task.project_id[:24]}-{task.id[:8]}"
        else:
            key = task.id[:12]
        attempt = int(task.metadata.get("recovery_attempt_count", 0))
        if attempt > 0:
            key = f"{key}-r{attempt}"
        return self._tmux_for(task).session_name(key)

    def _launch_command(self, task: Task) -> str:
        """
        CLI to launch, owned by the routed pool (env is injected separately
        for the API pool). Falls back to the agent default for unpooled tasks.
        """
        pool_cmd = task.metadata.get("pool_launch_command")
        if pool_cmd:
            return str(pool_cmd)
        return self.config.command

    async def _launch_claude_cli(
        self,
        tmux: TmuxManager,
        session_name: str,
        task: Task,
    ) -> bool:
        """Start Claude in the tmux pane (subscription or API-key pool)."""
        auth_mode = task.metadata.get("pool_auth_mode", "subscription")
        if auth_mode == "api_key":
            key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            if key and not await tmux.set_session_environment(
                session_name, "ANTHROPIC_API_KEY", key
            ):
                logger.warning("Failed to inject ANTHROPIC_API_KEY into tmux session")
        command = self._launch_command(task)
        logger.info("Launching %s in %s (auth=%s)", command, session_name, auth_mode)
        return await tmux.send_keys(session_name, command)

    def _resolve_working_dir(self, task: Task) -> Path:
        if path := task.metadata.get("working_dir"):
            return Path(path).resolve()
        if task.project_id:
            project_path = self.projects_dir / task.project_id
            project_path.mkdir(parents=True, exist_ok=True)
            return project_path.resolve()
        fallback = self.projects_dir / "_default"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback.resolve()

    def _build_prompt(self, task: Task, *, headless: bool = False) -> str:
        parts: list[str] = [
            "You are in autonomous execution mode. Do not explain your plan. "
            "Do not ask questions. Do not summarize what you are about to do. "
            "Immediately open the files and make the required changes. "
            "Start editing now. When done, run git add -A && git commit -m 'atlas: <description>'. "
            "Do not stop until the files are edited and committed."
        ]
        if ctx := task.metadata.get("operational_context"):
            parts.append(str(ctx))
        if custom := task.metadata.get("prompt"):
            parts.append(str(custom))
        else:
            parts.append(task.title)
            if task.description:
                parts.append(task.description)

        if headless:
            # Headless runs have a real exit code and structured result —
            # no sentinel protocol needed, only the commit requirement.
            parts.append(
                "\n\nIMPORTANT: After completing all changes, you MUST run:\n"
                "git add -A && git commit -m 'atlas: <describe what you did>'\n"
                "Do not finish without committing."
            )
            return "\n\n".join(parts)

        # NOTE: the sentinel is described in two pieces so the literal marker
        # never appears in the prompt echo inside the tmux pane — otherwise
        # detection would fire on our own instructions.
        parts.append(
            "\n\nIMPORTANT completion protocol:\n"
            "1. After completing all changes, you MUST run:\n"
            "   git add -A && git commit -m 'atlas: <describe what you did>'\n"
            "   Do not finish without committing.\n"
            "2. When everything is finished and committed, print one final line\n"
            "   consisting of the word ATLAS_TASK_ immediately followed by the\n"
            "   word COMPLETE (joined together as a single token, no space).\n"
            "3. If you cannot complete the task, print one final line consisting\n"
            "   of ATLAS_TASK_ immediately followed by FAILED (single token),\n"
            "   then a colon and a one-line reason.\n"
            "Print that marker line alone as your very last output."
        )
        return "\n\n".join(parts)

    async def start_session(self, task: Task, state: _RunState) -> str:
        """Create or reuse tmux session and launch Claude Code."""
        tmux = state.tmux
        if not tmux.is_available():
            raise RuntimeError("tmux unavailable — cannot start Claude Code session")

        if not shutil.which(self.config.command) and self.tmux.backend.value != "wsl":
            # CLI may exist only inside WSL; still attempt launch there
            logger.warning(
                "%r not found on host PATH; will try inside tmux session",
                self.config.command,
            )

        state.session_name = self._resolve_session_name(task)
        task.metadata["session_name"] = state.session_name
        working_dir = self._resolve_working_dir(task)

        logger.info(
            "start_session: name=%s cwd=%s task=%s",
            state.session_name,
            working_dir,
            task.id[:8],
        )

        # Never reuse a leftover session (Tier 1.5 / bug H2): stale Claude
        # CLI state caused instant error loops. Kill and start clean.
        if await tmux.session_exists(state.session_name):
            logger.warning(
                "Killing leftover tmux session before fresh start: %s",
                state.session_name,
            )
            await tmux.kill_session(state.session_name)

        launch_cmd = self._launch_command(task)
        logger.info("Preparing Claude launch in %s", state.session_name)
        # Shell session + send_keys is more reliable on WSL than
        # new-session ... claude (start_command can kill the tmux server).
        created = await tmux.create_session(
            state.session_name,
            working_dir=working_dir,
        )
        if not created:
            raise RuntimeError(f"Failed to create tmux session: {state.session_name}")

        launched = await self._launch_claude_cli(tmux, state.session_name, task)
        if not launched:
            raise RuntimeError(
                f"Failed to launch {launch_cmd} in tmux session: {state.session_name}"
            )

        await asyncio.sleep(self.config.launch_wait_sec)

        if not await tmux.session_exists(state.session_name):
            logger.warning(
                "tmux session lost during Claude launch — recreating %s",
                state.session_name,
            )
            created = await tmux.create_session(
                state.session_name,
                working_dir=working_dir,
            )
            if not created:
                raise RuntimeError(
                    f"tmux session lost during Claude launch: {state.session_name}"
                )
            launched = await self._launch_claude_cli(tmux, state.session_name, task)
            if not launched:
                raise RuntimeError(
                    f"Failed to relaunch {launch_cmd} in tmux session: {state.session_name}"
                )
            await asyncio.sleep(self.config.launch_wait_sec)

        if not await tmux.session_exists(state.session_name):
            raise RuntimeError(
                f"tmux session unavailable after Claude launch: {state.session_name}"
            )

        return state.session_name

    async def _ensure_session_ready(self, task: Task, state: _RunState) -> None:
        """Recreate session + Claude if the server or session vanished."""
        tmux = state.tmux
        if await tmux.session_exists(state.session_name):
            return

        working_dir = self._resolve_working_dir(task)
        created = await tmux.create_session(
            state.session_name,
            working_dir=working_dir,
        )
        if not created:
            raise RuntimeError(f"Failed to recreate tmux session: {state.session_name}")
        launched = await self._launch_claude_cli(tmux, state.session_name, task)
        if not launched:
            raise RuntimeError(
                f"Failed to relaunch claude in tmux session: {state.session_name}"
            )
        await asyncio.sleep(min(self.config.launch_wait_sec, 25))
        if not await tmux.session_exists(state.session_name):
            raise RuntimeError(f"tmux session still missing: {state.session_name}")

    async def send_prompt(
        self, task: Task, state: _RunState, prompt: str | None = None
    ) -> None:
        """Inject the task prompt into the Claude Code session."""
        if not state.session_name:
            raise RuntimeError("start_session() must be called first")

        await self._ensure_session_ready(task, state)

        # Wipe the pane's scrollback before each task so stale output from a
        # previously reused session can never trigger false-positive detection.
        cleared = await state.tmux.clear_history(state.session_name)
        if not cleared:
            logger.warning(
                "send_prompt: clear_history failed for %s — stale scrollback may remain",
                state.session_name,
            )

        text = prompt or self._build_prompt(task)

        logger.info(
            "send_prompt: session=%s chars=%d",
            state.session_name,
            len(text),
        )

        ok = await state.tmux.send_keys(state.session_name, text)
        if not ok:
            await self._ensure_session_ready(task, state)
            ok = await state.tmux.send_keys(state.session_name, text)
        if not ok:
            raise RuntimeError("Failed to send prompt to tmux session")

    @staticmethod
    def _tail(output: str, lines: int = _MARKER_TAIL_LINES) -> str:
        return "\n".join(output.splitlines()[-lines:])

    async def detect_completion(
        self,
        output: str,
        *,
        previous_output: str,
        stable_count: int,
    ) -> tuple[bool, str]:
        """
        Completion detection, in priority order (Tier 1.4):

        1. Structured sentinels printed by Claude (high confidence)
        2. Fatal CLI markers in the tail (launch/auth failures)
        3. Idle-at-prompt with stable output — classified by tail errors
        4. Output stable fallback

        Weak word markers ("done", "completed", checkmarks) were removed:
        they matched Claude's conversational output and caused false success.

        Returns (is_complete, reason).
        """
        tail = self._tail(output)
        tail_lower = tail.lower()

        # 1. Structured sentinels — primary signal
        if SENTINEL_FAILED in tail:
            return True, f"sentinel_failed:{SENTINEL_FAILED}"
        if SENTINEL_COMPLETE in tail:
            return True, f"sentinel_complete:{SENTINEL_COMPLETE}"

        # 2. Fatal markers — CLI cannot proceed regardless of Claude's state
        for marker in _FATAL_MARKERS:
            if marker in tail_lower:
                return True, f"error_detected:{marker}"

        is_stable = (
            output == previous_output
            and stable_count >= self.config.idle_stable_polls
        )

        # 3. Idle prompt visible + output stopped changing
        for pattern in _IDLE_PROMPT_PATTERNS:
            if pattern.search(output):
                if is_stable:
                    for marker in _IDLE_ERROR_MARKERS:
                        if marker in tail_lower:
                            return True, f"error_detected:{marker}"
                    return True, "idle_at_prompt"
                break

        # 4. Output frozen without a recognizable prompt
        if is_stable:
            for marker in _IDLE_ERROR_MARKERS:
                if marker in tail_lower:
                    return True, f"error_detected:{marker}"
            return True, "output_stable"

        return False, "running"

    async def monitor_execution(self, task: Task, state: _RunState) -> TaskResult:
        """Poll tmux output until completion, timeout, or failure."""
        if not state.session_name:
            raise RuntimeError("start_session() must be called first")

        session = state.session_name
        result = TaskResult(
            status=ResultStatus.COMPLETED,
            summary="",
            session_name=session,
        )

        previous = ""
        stable_count = 0
        elapsed = 0.0
        poll = self.config.poll_interval_sec
        max_sec = float(
            task.metadata.get("max_execution_sec", self.config.max_execution_sec)
        )

        logger.info(
            "monitor_execution: session=%s max_sec=%s poll=%ss",
            session,
            max_sec,
            poll,
        )

        cancel: asyncio.Event | None = task.metadata.get("_cancel_event")

        while elapsed < max_sec:
            if cancel and cancel.is_set():
                result.status = ResultStatus.CANCELLED
                result.summary = "Cancelled by operator"
                result.raw_output = state.last_output
                logger.info("Execution cancelled: %s", session)
                break

            if not await state.tmux.session_exists(session):
                result.status = ResultStatus.FAILED
                result.errors.append("tmux session disappeared")
                result.summary = "Session lost during execution"
                logger.error("Session lost: %s", session)
                break

            output = await state.tmux.capture_output(
                session,
                lines=self.config.capture_lines,
            )
            state.last_output = output

            # Stream recent tail to logs
            tail = "\n".join(output.splitlines()[-8:])
            if tail.strip():
                logger.debug("output tail (%s):\n%s", session, tail)

            complete, reason = await self.detect_completion(
                output,
                previous_output=previous,
                stable_count=stable_count,
            )

            if output == previous:
                stable_count += 1
            else:
                stable_count = 0
                previous = output

            if complete:
                failed = reason.startswith(("error_detected", "sentinel_failed"))
                result.raw_output = output
                if failed:
                    result.status = ResultStatus.FAILED
                    result.summary = f"Execution failed ({reason})"
                    result.errors.append(reason)
                    logger.error("Failure detected: %s (%s)", session, reason)
                else:
                    result.status = ResultStatus.COMPLETED
                    result.summary = f"Detected completion ({reason})"
                    logger.info("Completion detected: %s (%s)", session, reason)
                break

            await asyncio.sleep(poll)
            elapsed += poll
        else:
            result.status = ResultStatus.TIMEOUT
            result.summary = f"Timed out after {max_sec}s"
            result.raw_output = state.last_output
            result.errors.append("timeout")
            logger.error("Timeout: %s after %ss", session, max_sec)

        result.finish()
        return result

    def _resolve_execution_mode(self, task: Task) -> str:
        """Task override > pool setting > agent default."""
        return str(
            task.metadata.get("execution_mode")
            or task.metadata.get("pool_execution_mode")
            or self.config.execution_mode
        )

    def _get_headless_executor(self):
        if self.headless_executor is None:
            from core.executor import HeadlessClaudeExecutor

            self.headless_executor = HeadlessClaudeExecutor(
                command=self.config.command
            )
        return self.headless_executor

    async def _run_headless(self, task: Task) -> TaskResult:
        """
        Structured execution: claude -p in the project dir, JSON result out.

        No sessions, no completion heuristics — the exit code and result
        object are the signal. Cost and session id land in result.metadata.
        """
        from core.executor import ExecutionRequest

        executor = self._get_headless_executor()
        run_id = f"headless-{task.id[:8]}"
        env: dict[str, str] = {}
        if task.metadata.get("pool_auth_mode") == "api_key":
            key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            if key:
                env["ANTHROPIC_API_KEY"] = key

        request = ExecutionRequest(
            prompt=self._build_prompt(task, headless=True),
            working_dir=self._resolve_working_dir(task),
            env=env,
            timeout_sec=float(
                task.metadata.get("max_execution_sec", self.config.max_execution_sec)
            ),
            model=str(task.metadata.get("model", "")),
        )
        cancel: asyncio.Event | None = task.metadata.get("_cancel_event")
        exec_result = await executor.execute(request, cancel=cancel)

        if exec_result.cancelled:
            status = ResultStatus.CANCELLED
            summary = "Cancelled by operator"
        elif exec_result.timed_out:
            status = ResultStatus.TIMEOUT
            summary = exec_result.error
        elif exec_result.success:
            status = ResultStatus.COMPLETED
            summary = (exec_result.output or "Completed")[:500]
        else:
            status = ResultStatus.FAILED
            summary = f"Headless execution failed: {exec_result.error}"

        result = TaskResult(
            status=status,
            summary=summary,
            session_name=run_id,
            raw_output=exec_result.raw_output,
            metadata={
                "task_id": task.id,
                "agent": self.name,
                "execution_mode": "headless",
                "exit_code": exec_result.exit_code,
                "cost_usd": exec_result.cost_usd,
                "claude_session_id": exec_result.session_id,
                "num_turns": exec_result.num_turns,
            },
        )
        if not exec_result.success and exec_result.error:
            result.errors.append(exec_result.error)
        if exec_result.cost_usd is not None:
            task.metadata["cost_usd"] = round(
                float(task.metadata.get("cost_usd", 0.0)) + exec_result.cost_usd, 6
            )
        result.finish()
        logger.info(
            "headless run finished: task=%s exit=%s cost=%s duration=%.1fs",
            task.id[:8],
            exec_result.exit_code,
            exec_result.cost_usd,
            exec_result.duration_sec,
        )
        return result

    async def run(self, task: Task) -> TaskResult:
        """Full execution pipeline for a task."""
        logger.info("run: task=%s title=%r", task.id[:8], task.title)
        if self._resolve_execution_mode(task) == "headless":
            return await self._run_headless(task)
        state = _RunState(tmux=self._tmux_for(task))
        try:
            session = await self.start_session(task, state)
            await self.send_prompt(task, state)
            result = await self.monitor_execution(task, state)
            result.metadata["task_id"] = task.id
            result.metadata["agent"] = self.name

            # Session lifecycle (Tier 1.5): kill on success so retries and new
            # tasks always start clean; keep failed sessions for post-mortem
            # unless kill_session_on_finish forces cleanup for all outcomes.
            should_kill = self.config.kill_session_on_finish or (
                result.status == ResultStatus.COMPLETED
                and self.config.kill_session_on_success
            )
            if should_kill:
                await state.tmux.kill_session(session)
                # Clear stale session pointer so retries build a fresh session
                task.metadata.pop("session_name", None)

            return result

        except Exception as exc:
            logger.exception("Claude Code run failed: %s", exc)
            result = TaskResult(
                status=ResultStatus.FAILED,
                summary=str(exc),
                session_name=state.session_name or "unknown",
                raw_output=state.last_output,
                errors=[str(exc)],
                metadata={"task_id": task.id, "agent": self.name},
            )
            result.finish()
            return result

    async def on_complete(self, task: Task, result: TaskResult) -> None:
        pass

    async def on_error(self, task: Task, result: TaskResult) -> None:
        pass
