"""
Claude Code terminal agent — tmux-driven execution (Phase 2).

Atlas does not call the Anthropic API here; it controls the `claude` CLI in a tmux pane.
"""

from __future__ import annotations

import asyncio
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


# Structured completion sentinels — instructed in every prompt (Tier 1.4).
# These are the primary, high-confidence completion signal.
SENTINEL_COMPLETE = "ATLAS_TASK_COMPLETE"
SENTINEL_FAILED = "ATLAS_TASK_FAILED"

# Fatal markers that justify failing immediately even mid-run: they indicate
# the CLI itself cannot proceed (launch/auth problems), not code-level errors
# Claude may be in the middle of fixing.
_FATAL_MARKERS = (
    "command not found",
    "enoent",
    "api error",
    "rate limit",
    "usage limit",
    "quota exceeded",
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
        self._session_name: str | None = None
        self._last_output: str = ""
        self._active_tmux: TmuxManager | None = None

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
        """Pool-isolated Claude launch — subscription vs API key env."""
        import os

        base = self.config.command
        auth_mode = task.metadata.get("pool_auth_mode", "subscription")
        if auth_mode == "api_key":
            key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            if key:
                return f"ANTHROPIC_API_KEY={key} {base}"
        return base

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

    def _build_prompt(self, task: Task) -> str:
        parts: list[str] = []
        if ctx := task.metadata.get("operational_context"):
            parts.append(str(ctx))
        if custom := task.metadata.get("prompt"):
            parts.append(str(custom))
        else:
            parts.append(task.title)
            if task.description:
                parts.append(task.description)
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

    async def start_session(self, task: Task) -> str:
        """Create or reuse tmux session and launch Claude Code."""
        tmux = self._tmux_for(task)
        self._active_tmux = tmux
        if not tmux.is_available():
            raise RuntimeError("tmux unavailable — cannot start Claude Code session")

        if not shutil.which(self.config.command) and self.tmux.backend.value != "wsl":
            # CLI may exist only inside WSL; still attempt launch there
            logger.warning(
                "%r not found on host PATH; will try inside tmux session",
                self.config.command,
            )

        self._session_name = self._resolve_session_name(task)
        task.metadata["session_name"] = self._session_name
        working_dir = self._resolve_working_dir(task)

        logger.info(
            "start_session: name=%s cwd=%s task=%s",
            self._session_name,
            working_dir,
            task.id[:8],
        )

        # Never reuse a leftover session (Tier 1.5 / bug H2): stale Claude
        # CLI state caused instant error loops. Kill and start clean.
        if await tmux.session_exists(self._session_name):
            logger.warning(
                "Killing leftover tmux session before fresh start: %s",
                self._session_name,
            )
            await tmux.kill_session(self._session_name)

        created = await tmux.create_session(
            self._session_name,
            working_dir=working_dir,
        )
        if not created:
            raise RuntimeError(f"Failed to create tmux session: {self._session_name}")

        launch_cmd = self._launch_command(task)
        logger.info("Launching %s in %s", launch_cmd, self._session_name)
        ok = await tmux.send_keys(self._session_name, launch_cmd)
        if not ok:
            raise RuntimeError(f"Failed to launch {self.config.command}")
        await asyncio.sleep(self.config.launch_wait_sec)

        return self._session_name

    async def send_prompt(self, task: Task, prompt: str | None = None) -> None:
        """Inject the task prompt into the Claude Code session."""
        if not self._session_name:
            raise RuntimeError("start_session() must be called first")

        text = prompt or self._build_prompt(task)
        logger.info(
            "send_prompt: session=%s chars=%d",
            self._session_name,
            len(text),
        )

        tmux = self._active_tmux or self.tmux
        ok = await tmux.send_keys(self._session_name, text)
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

    async def monitor_execution(self, task: Task) -> TaskResult:
        """Poll tmux output until completion, timeout, or failure."""
        if not self._session_name:
            raise RuntimeError("start_session() must be called first")

        session = self._session_name
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
                result.raw_output = self._last_output
                logger.info("Execution cancelled: %s", session)
                break

            tmux = self._active_tmux or self.tmux
            if not await tmux.session_exists(session):
                result.status = ResultStatus.FAILED
                result.errors.append("tmux session disappeared")
                result.summary = "Session lost during execution"
                logger.error("Session lost: %s", session)
                break

            output = await tmux.capture_output(
                session,
                lines=self.config.capture_lines,
            )
            self._last_output = output

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
            result.raw_output = self._last_output
            result.errors.append("timeout")
            logger.error("Timeout: %s after %ss", session, max_sec)

        result.finish()
        return result

    async def run(self, task: Task) -> TaskResult:
        """Full execution pipeline for a task."""
        logger.info("run: task=%s title=%r", task.id[:8], task.title)
        session = ""
        try:
            session = await self.start_session(task)
            await self.send_prompt(task)
            result = await self.monitor_execution(task)
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
                tmux = self._active_tmux or self.tmux
                await tmux.kill_session(session)
                # Clear stale session pointer so retries build a fresh session
                task.metadata.pop("session_name", None)

            return result

        except Exception as exc:
            logger.exception("Claude Code run failed: %s", exc)
            result = TaskResult(
                status=ResultStatus.FAILED,
                summary=str(exc),
                session_name=session or self._session_name or "unknown",
                raw_output=self._last_output,
                errors=[str(exc)],
                metadata={"task_id": task.id, "agent": self.name},
            )
            result.finish()
            return result

    async def on_complete(self, task: Task, result: TaskResult) -> None:
        pass

    async def on_error(self, task: Task, result: TaskResult) -> None:
        pass
