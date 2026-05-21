"""
tmux session manager for Atlas-controlled terminals.

Phase 2: create, control, capture, and tear down sessions for Claude Code.
Uses subprocess with timeouts; on Windows falls back to WSL tmux when available.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from core.logger import get_logger

logger = get_logger("sessions.tmux")

# Subprocess timeout for individual tmux commands (seconds)
_CMD_TIMEOUT = 30


class TmuxBackend(str, Enum):
    NATIVE = "native"
    WSL = "wsl"
    UNAVAILABLE = "unavailable"


@dataclass
class TmuxRunResult:
    returncode: int
    stdout: str
    stderr: str


class TmuxManager:
    """Create and control tmux sessions for agent workloads."""

    def __init__(
        self,
        session_prefix: str = "atlas",
        socket_path: str | None = None,
    ) -> None:
        self.session_prefix = session_prefix
        self.socket_path = socket_path
        self._backend = self._detect_backend()
        logger.info("Tmux backend: %s", self._backend.value)

    @property
    def backend(self) -> TmuxBackend:
        return self._backend

    @staticmethod
    def _detect_backend() -> TmuxBackend:
        if shutil.which("tmux"):
            return TmuxBackend.NATIVE
        if sys.platform == "win32" and shutil.which("wsl"):
            # tmux inside WSL is the typical Windows setup
            try:
                proc = subprocess.run(
                    ["wsl", "which", "tmux"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    return TmuxBackend.WSL
            except (subprocess.TimeoutExpired, OSError):
                pass
        return TmuxBackend.UNAVAILABLE

    def is_available(self) -> bool:
        return self._backend != TmuxBackend.UNAVAILABLE

    def _base_cmd(self) -> list[str]:
        if self._backend == TmuxBackend.WSL:
            cmd = ["wsl", "tmux"]
        else:
            cmd = ["tmux"]
        if self.socket_path:
            cmd.extend(["-S", self.socket_path])
        return cmd

    @staticmethod
    def _normalize_working_dir(working_dir: str | Path | None) -> str | None:
        if working_dir is None:
            return None
        path = Path(working_dir).resolve()
        if sys.platform == "win32" and TmuxManager._detect_backend() == TmuxBackend.WSL:
            # Convert C:\Users\foo -> /mnt/c/Users/foo for WSL tmux -c
            drive = path.drive.rstrip(":").lower()
            if drive:
                rest = path.as_posix().split(":", 1)[-1]
                return f"/mnt/{drive}{rest}"
        return str(path)

    def session_name(self, project_id: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in project_id)
        return f"{self.session_prefix}-{safe[:48]}"

    async def _run(
        self,
        args: list[str],
        *,
        timeout: float = _CMD_TIMEOUT,
        log_level: int = logging.DEBUG,
    ) -> TmuxRunResult:
        cmd = [*self._base_cmd(), *args]
        logger.log(log_level, "tmux exec: %s", " ".join(cmd))

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=timeout,
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.error("tmux command timed out: %s", " ".join(args[:3]))
            return TmuxRunResult(returncode=-1, stdout="", stderr="timeout")

        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        if proc.returncode != 0:
            logger.warning(
                "tmux command failed (rc=%s): %s | stderr=%s",
                proc.returncode,
                " ".join(args[:4]),
                stderr.strip()[:500],
            )
        return TmuxRunResult(proc.returncode, stdout, stderr)

    async def list_sessions(self) -> list[str]:
        if not self.is_available():
            return []
        result = await self._run(["list-sessions", "-F", "#{session_name}"])
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    async def session_exists(self, session_name: str) -> bool:
        """Return True if a tmux session with this name exists."""
        if not self.is_available():
            logger.warning("session_exists called but tmux unavailable")
            return False
        result = await self._run(["has-session", "-t", session_name])
        exists = result.returncode == 0
        logger.debug("session_exists(%s) -> %s", session_name, exists)
        return exists

    async def create_session(
        self,
        session_name: str,
        working_dir: str | Path | None = None,
        start_command: str | None = None,
    ) -> bool:
        """
        Create a detached tmux session.

        If the session already exists, logs and returns True (reuse).
        """
        if not self.is_available():
            raise RuntimeError(
                "tmux is not available. Install tmux (or use WSL on Windows)."
            )

        if await self.session_exists(session_name):
            logger.info("Reusing existing tmux session: %s", session_name)
            return True

        args: list[str] = ["new-session", "-d", "-s", session_name]
        wd = self._normalize_working_dir(working_dir)
        if wd:
            args.extend(["-c", wd])
            logger.info("Creating session %s (cwd=%s)", session_name, wd)
        else:
            logger.info("Creating session %s", session_name)

        if start_command:
            args.append(start_command)

        result = await self._run(args, log_level=logging.INFO)
        if result.returncode != 0:
            logger.error(
                "Failed to create session %s: %s",
                session_name,
                result.stderr.strip(),
            )
            return False

        logger.info("Created tmux session: %s", session_name)
        return True

    async def send_keys(
        self,
        session_name: str,
        command: str,
        *,
        enter: bool = True,
        literal: bool = True,
    ) -> bool:
        """
        Send text to the session's active pane.

        Uses literal mode by default so prompts are not interpreted as key names.
        """
        if not await self.session_exists(session_name):
            logger.error("send_keys: session does not exist: %s", session_name)
            return False

        # Log a short preview, not the full prompt
        preview = command[:120] + ("..." if len(command) > 120 else "")
        logger.info("send_keys -> %s: %r", session_name, preview)

        args: list[str] = ["send-keys", "-t", session_name]
        if literal:
            args.append("-l")
        args.append(command)

        result = await self._run(args)
        if result.returncode != 0:
            return False

        if enter:
            enter_result = await self._run(
                ["send-keys", "-t", session_name, "C-m"],
            )
            return enter_result.returncode == 0

        return True

    async def capture_output(
        self,
        session_name: str,
        lines: int = 200,
    ) -> str:
        """Capture recent pane output for monitoring."""
        if not await self.session_exists(session_name):
            logger.warning("capture_output: missing session %s", session_name)
            return ""

        result = await self._run(
            [
                "capture-pane",
                "-t",
                session_name,
                "-p",
                "-S",
                f"-{lines}",
            ],
        )
        text = result.stdout if result.returncode == 0 else ""
        logger.debug(
            "capture_output(%s): %d chars, %d lines",
            session_name,
            len(text),
            text.count("\n"),
        )
        return text

    async def kill_session(self, session_name: str) -> bool:
        """Kill a tmux session."""
        if not await self.session_exists(session_name):
            logger.debug("kill_session: %s not found (noop)", session_name)
            return True

        result = await self._run(["kill-session", "-t", session_name], log_level=logging.INFO)
        if result.returncode == 0:
            logger.info("Killed tmux session: %s", session_name)
            return True
        logger.error("Failed to kill session %s: %s", session_name, result.stderr.strip())
        return False

    # Back-compat alias
    async def capture_pane(self, session: str, lines: int = 100) -> str:
        return await self.capture_output(session, lines=lines)
