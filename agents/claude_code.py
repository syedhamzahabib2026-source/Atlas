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


# Heuristic signals — practical, not perfect
_COMPLETION_MARKERS = (
    "done",
    "completed",
    "finished",
    "task complete",
    "all done",
    "✓",
    "✔",
)

_ERROR_MARKERS = (
    "error:",
    "traceback (most recent call last)",
    "command not found",
    "enoent",
    "permission denied",
    "api error",
    "rate limit",
)

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
    ) -> None:
        self.tmux = tmux
        self.projects_dir = projects_dir
        self.config = config or ClaudeCodeConfig()
        self._session_name: str | None = None
        self._last_output: str = ""

    def can_handle(self, task: Task) -> bool:
        agent = task.metadata.get("agent")
        if agent == "browser":
            return False
        return agent is None or agent == self.name

    def _resolve_session_name(self, task: Task) -> str:
        custom = task.metadata.get("session_name")
        if custom:
            return str(custom)
        key = task.project_id or task.id[:12]
        return self.tmux.session_name(key)

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
        parts.append(
            "\n\nIMPORTANT: After completing all changes, you MUST run:\n"
            "git add -A && git commit -m 'atlas: <describe what you did>'\n"
            "Do not finish without committing."
        )
        return "\n\n".join(parts)

    async def start_session(self, task: Task) -> str:
        """Create or reuse tmux session and launch Claude Code."""
        if not self.tmux.is_available():
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

        created = await self.tmux.create_session(
            self._session_name,
            working_dir=working_dir,
        )
        if not created:
            raise RuntimeError(f"Failed to create tmux session: {self._session_name}")

        # Launch Claude Code if pane does not already show it
        snapshot = await self.tmux.capture_output(
            self._session_name,
            lines=30,
        )
        if self.config.command.lower() not in snapshot.lower():
            logger.info("Launching %s in %s", self.config.command, self._session_name)
            ok = await self.tmux.send_keys(self._session_name, self.config.command)
            if not ok:
                raise RuntimeError(f"Failed to launch {self.config.command}")
            await asyncio.sleep(self.config.launch_wait_sec)
        else:
            logger.info("Claude Code already running in %s", self._session_name)

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

        ok = await self.tmux.send_keys(self._session_name, text)
        if not ok:
            raise RuntimeError("Failed to send prompt to tmux session")

    async def detect_completion(
        self,
        output: str,
        *,
        previous_output: str,
        stable_count: int,
    ) -> tuple[bool, str]:
        """
        Simple completion heuristics.

        Returns (is_complete, reason).
        """
        lower = output.lower()

        for marker in _ERROR_MARKERS:
            if marker in lower:
                return False, f"error_detected:{marker}"

        for marker in _COMPLETION_MARKERS:
            if marker in lower:
                return True, f"marker:{marker}"

        for pattern in _IDLE_PROMPT_PATTERNS:
            if pattern.search(output):
                # Prompt visible and output stopped changing
                if output == previous_output and stable_count >= self.config.idle_stable_polls:
                    return True, "idle_at_prompt"

        if output == previous_output and stable_count >= self.config.idle_stable_polls:
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

            if not await self.tmux.session_exists(session):
                result.status = ResultStatus.FAILED
                result.errors.append("tmux session disappeared")
                result.summary = "Session lost during execution"
                logger.error("Session lost: %s", session)
                break

            output = await self.tmux.capture_output(
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

            if complete and "error_detected" not in reason:
                result.status = ResultStatus.COMPLETED
                result.summary = f"Detected completion ({reason})"
                result.raw_output = output
                logger.info("Completion detected: %s (%s)", session, reason)
                break

            if "error_detected" in reason:
                result.status = ResultStatus.FAILED
                result.summary = f"Execution failed ({reason})"
                result.raw_output = output
                result.errors.append(reason)
                logger.error("Failure detected: %s (%s)", session, reason)
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

            # TODO: Slack — notify on completion / failure
            # TODO: memory.store — persist result.raw_output + summary
            # TODO: retries — if result.status == TIMEOUT, re-queue with metadata

            if self.config.kill_session_on_finish:
                await self.tmux.kill_session(session)

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
