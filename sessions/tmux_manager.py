"""
tmux session manager for Atlas-controlled terminals.

Phase 2: create, control, capture, and tear down sessions for Claude Code.
Uses subprocess with timeouts; on Windows falls back to WSL tmux when available.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from core.logger import get_logger

logger = get_logger("sessions.tmux")

# Stable socket shared by every TmuxManager (main + worker pools). Avoids WSL
# default-socket races where the server vanishes between subprocess calls.
DEFAULT_TMUX_SOCKET = "/tmp/atlas-tmux.sock"

# Subprocess timeout for individual tmux commands (seconds)
_CMD_TIMEOUT = 30

_SERVER_ERRORS = (
    "error connecting",
    "no server running",
    "connection refused",
)


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
        socket_path: str | None = DEFAULT_TMUX_SOCKET,
    ) -> None:
        self.session_prefix = session_prefix
        self.socket_path = socket_path or DEFAULT_TMUX_SOCKET
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

    def _tmux_argv(self, args: list[str]) -> list[str]:
        """Build tmux argv including socket path."""
        argv: list[str] = ["tmux"]
        if self.socket_path:
            argv.extend(["-S", self.socket_path])
        argv.extend(args)
        return argv

    def _exec_cmd(self, args: list[str]) -> list[str]:
        """
        Build the subprocess argv for a tmux invocation.

        On WSL, run tmux inside `bash -lc` so every command shares one stable
        server context. Bare `wsl tmux` from Windows can leave the socket in
        a dead-server state between asyncio subprocess calls.
        """
        if self._backend == TmuxBackend.WSL:
            script = " ".join(shlex.quote(part) for part in self._tmux_argv(args))
            return ["wsl", "bash", "-lc", script]
        return self._tmux_argv(args)

    @staticmethod
    def _is_server_error(result: TmuxRunResult) -> bool:
        err = result.stderr.lower()
        return any(marker in err for marker in _SERVER_ERRORS)

    async def ensure_server(self) -> None:
        """Start the shared tmux server if it is not already running."""
        if not self.is_available():
            return
        result = await self._run(["start-server"], log_level=logging.DEBUG)
        if result.returncode == 0:
            logger.debug("tmux server ready (socket=%s)", self.socket_path)

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
        cmd = self._exec_cmd(args)
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
        result = TmuxRunResult(proc.returncode, stdout, stderr)
        if proc.returncode != 0 and self._is_server_error(result):
            await self._run(["start-server"], log_level=logging.DEBUG)
            proc2 = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b2, stderr_b2 = await asyncio.wait_for(
                proc2.communicate(),
                timeout=timeout,
            )
            result = TmuxRunResult(
                proc2.returncode,
                stdout_b2.decode(errors="replace"),
                stderr_b2.decode(errors="replace"),
            )
        if result.returncode != 0:
            logger.warning(
                "tmux command failed (rc=%s): %s | stderr=%s",
                result.returncode,
                " ".join(args[:4]),
                result.stderr.strip()[:500],
            )
        return result

    async def list_sessions(self) -> list[str]:
        if not self.is_available():
            return []
        # Do NOT use list-sessions -F "#{session_name}" on WSL: the # is stripped
        # by wsl.exe argument parsing, leaving -F with no value (rc=1, no server).
        result = await self._run(["list-sessions"])
        if result.returncode != 0:
            return []
        names: list[str] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            # Default format: "session-name: N windows (created ...)"
            name = line.split(":", 1)[0].strip()
            if name:
                names.append(name)
        return names

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

        await self.ensure_server()

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

        # WSL tmux can report rc=0 from new-session while the server is still
        # settling (or already dead if start_command exited). Verify before
        # returning success.
        await asyncio.sleep(0.25)
        if not await self.session_exists(session_name):
            logger.warning(
                "Session %s missing after create; restarting tmux server",
                session_name,
            )
            await self.ensure_server()
            if not await self.session_exists(session_name):
                logger.error("Session %s not found after create", session_name)
                return False

        # Best-effort: keep pane visible if Claude crashes so Atlas can inspect.
        await self._run(
            ["set-option", "-t", session_name, "remain-on-exit", "on"],
            log_level=logging.DEBUG,
        )

        logger.info("Created tmux session: %s", session_name)
        return True

    async def set_session_environment(
        self,
        session_name: str,
        name: str,
        value: str,
    ) -> bool:
        """Set a tmux session environment variable (visible to shell children)."""
        await self.ensure_server()
        result = await self._run(
            ["set-environment", "-t", session_name, name, value],
            log_level=logging.INFO,
        )
        if result.returncode != 0:
            logger.warning(
                "set-environment %s on %s failed: %s",
                name,
                session_name,
                result.stderr.strip()[:200],
            )
            return False
        return True

    async def _send_via_buffer(self, session_name: str, text: str, *, enter: bool) -> bool:
        """
        Paste multi-line / long text through a tmux buffer.

        Avoids WSL argument-parsing bugs with embedded newlines and quotes.
        Buffer and temp-file names are unique per call: a shared name lets two
        concurrent sends paste task A's prompt into task B's session.
        """
        import uuid

        token = uuid.uuid4().hex[:12]
        buf_name = f"atlasq-{token}"
        tmp_file = f"/tmp/atlas-send-{token}.txt"
        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        paste_argv = self._tmux_argv(
            ["load-buffer", "-b", buf_name, tmp_file],
        )
        paste_buf = self._tmux_argv(
            ["paste-buffer", "-d", "-b", buf_name, "-t", session_name]
        )
        enter_argv = self._tmux_argv(["send-keys", "-t", session_name, "C-m"])
        paste_cmd = " ".join(shlex.quote(p) for p in paste_argv)
        paste_buf_cmd = " ".join(shlex.quote(p) for p in paste_buf)
        enter_cmd = " ".join(shlex.quote(p) for p in enter_argv) if enter else "true"
        script = (
            f"printf '%s' '{payload}' | base64 -d > {tmp_file} && "
            f"{paste_cmd} && {paste_buf_cmd} && {enter_cmd}; "
            f"rc=$?; rm -f {tmp_file}; exit $rc"
        )
        if self._backend == TmuxBackend.WSL:
            cmd = ["wsl", "bash", "-lc", script]
        else:
            cmd = ["bash", "-lc", script]
        logger.log(logging.INFO, "tmux paste-buffer -> %s (%d chars)", session_name, len(text))
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=_CMD_TIMEOUT,
            )
            _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=_CMD_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error("tmux paste-buffer timed out for %s", session_name)
            return False
        if proc.returncode != 0:
            logger.warning(
                "tmux paste-buffer failed for %s: %s",
                session_name,
                stderr_b.decode(errors="replace").strip()[:500],
            )
            return False
        return True

    async def send_keys(
        self,
        session_name: str,
        command: str,
        *,
        enter: bool = True,
        literal: bool = True,
        require_session: bool = True,
    ) -> bool:
        """
        Send text to the session's active pane.

        Uses literal mode by default so prompts are not interpreted as key names.
        """
        await self.ensure_server()
        if require_session and not await self.session_exists(session_name):
            logger.error("send_keys: session does not exist: %s", session_name)
            return False

        preview = command[:120] + ("..." if len(command) > 120 else "")
        logger.info("send_keys -> %s: %r", session_name, preview)

        if literal and (
            "\n" in command
            or len(command) > 400
            or (self._backend == TmuxBackend.WSL and len(command) > 200)
        ):
            return await self._send_via_buffer(session_name, command, enter=enter)

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
