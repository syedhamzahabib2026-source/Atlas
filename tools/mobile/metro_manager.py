"""
Metro bundler control for AssignMint React Native iOS (macOS).

Operational lifecycle only — start/stop/restart, port detection, log capture,
and deterministic failure signature matching for future RecoveryEngine hooks.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from core.logger import get_logger

logger = get_logger("mobile.metro")

# AssignMint default on macOS (overridable via MetroManagerConfig.project_root)
ASSIGNMINT_PROJECT_ROOT = Path("/Users/hamza/Desktop/Assignmint3")

_METRO_FAILURE_PATTERNS: list[tuple[str, str, str]] = [
    # category, severity, regex
    ("port_conflict", "error", r"EADDRINUSE|address already in use|Port \d+ already"),
    ("module_resolution", "error", r"Unable to resolve module|Cannot find module"),
    ("transform", "error", r"TransformError|transform\[stdin\]|Failed to construct transformer"),
    ("syntax", "error", r"SyntaxError|Unexpected token"),
    ("bundler_crash", "error", r"Metro has encountered an error|Bundler process exited"),
    ("watchman", "warning", r"watchman|Watchman"),
    ("file_limits", "error", r"EMFILE|too many open files|ENOSPC"),
    ("cache", "warning", r"reset-cache|cache stores"),
    ("process_exit", "error", r"Process terminated|Killed|SIGKILL|exited with code"),
    ("network", "warning", r"ECONNREFUSED|ETIMEDOUT|network"),
]


class MetroRunState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    UNHEALTHY = "unhealthy"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass
class MetroFailureSignature:
    """Detected bundler/runtime issue from log lines."""

    category: str
    severity: str
    pattern: str
    line: str
    line_number: int | None = None

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity,
            "pattern": self.pattern,
            "line": self.line[:500],
            "line_number": self.line_number,
        }


@dataclass
class MetroRuntimeState:
    """Point-in-time Metro runtime snapshot for monitoring / recovery metadata."""

    state: MetroRunState
    port: int
    host: str
    project_root: str
    running: bool
    pid: int | None = None
    managed_by_atlas: bool = False
    status_body: str | None = None
    failures: list[MetroFailureSignature] = field(default_factory=list)
    log_line_count: int = 0
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "port": self.port,
            "host": self.host,
            "project_root": self.project_root,
            "running": self.running,
            "pid": self.pid,
            "managed_by_atlas": self.managed_by_atlas,
            "status_body": self.status_body,
            "failure_count": len(self.failures),
            "failures": [f.to_dict() for f in self.failures],
            "log_line_count": self.log_line_count,
            "checked_at": self.checked_at,
        }


@dataclass
class MetroOperationResult:
    """Base Metro operation result."""

    success: bool
    operation: str
    message: str = ""
    error: str | None = None
    runtime: MetroRuntimeState | None = None
    details: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "operation": self.operation,
            "message": self.message,
            "error": self.error,
            "runtime": self.runtime.to_dict() if self.runtime else None,
            "details": self.details,
        }


@dataclass
class MetroLogResult(MetroOperationResult):
    """Log tail / stream snapshot."""

    lines: list[str] = field(default_factory=list)
    log_file: Path | None = None

    def to_dict(self) -> dict:
        base = super().to_dict()
        base["line_count"] = len(self.lines)
        base["log_file"] = str(self.log_file) if self.log_file else None
        base["lines"] = self.lines[-50:]
        return base


class MetroLogBuffer:
    """In-memory ring buffer of Metro stdout/stderr lines."""

    def __init__(self, max_lines: int = 500) -> None:
        self._max_lines = max_lines
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        line = line.rstrip("\n")
        if not line:
            return
        with self._lock:
            self._lines.append(line)

    def extend(self, lines: list[str]) -> None:
        for line in lines:
            self.append(line)

    def tail(self, n: int = 40) -> list[str]:
        with self._lock:
            if n >= len(self._lines):
                return list(self._lines)
            return list(self._lines)[-n:]

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    def __len__(self) -> int:
        with self._lock:
            return len(self._lines)


@dataclass
class MetroManagerConfig:
    project_root: Path = field(default_factory=lambda: ASSIGNMINT_PROJECT_ROOT)
    port: int = 8081
    host: str = "127.0.0.1"
    npm_command: str = "npm"
    start_script: str = "start"
    command_timeout_sec: float = 30.0
    start_ready_timeout_sec: float = 90.0
    stop_timeout_sec: float = 15.0
    max_log_lines: int = 500
    logs_dir: Path = Path("logs/mobile/metro")
    poll_interval_sec: float = 1.0


class MetroManager:
    """
    Metro bundler process lifecycle for a single RN project root.

    Future: attach MetroRuntimeState + failures to RecoveryEngine evidence.
    """

    def __init__(self, config: MetroManagerConfig | None = None) -> None:
        self.config = config or MetroManagerConfig()
        self.config.project_root = self.config.project_root.resolve()
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)
        self._log_buffer = MetroLogBuffer(max_lines=self.config.max_log_lines)
        self._process: subprocess.Popen[str] | None = None
        self._log_thread: threading.Thread | None = None
        self._log_file: Path | None = None
        self._log_fp = None
        self._run_state = MetroRunState.STOPPED
        self._stop_reader = threading.Event()

    @staticmethod
    def is_available(project_root: Path | None = None) -> bool:
        root = (project_root or ASSIGNMINT_PROJECT_ROOT).resolve()
        return (root / "package.json").is_file() and shutil_which("npm") is not None

    def get_runtime_state(self, *, scan_logs: bool = True) -> MetroRuntimeState:
        port = self.config.port
        host = self.config.host
        pid = self._resolve_metro_pid()
        status_body = self._fetch_metro_status()
        running = status_body is not None and "running" in status_body.lower()
        managed = self._process is not None and self._process.poll() is None

        if managed:
            state = MetroRunState.RUNNING if running else MetroRunState.UNHEALTHY
        elif running:
            state = MetroRunState.RUNNING
        elif pid:
            state = MetroRunState.UNHEALTHY
        else:
            state = MetroRunState.STOPPED

        failures: list[MetroFailureSignature] = []
        if scan_logs:
            lines = self._log_buffer.snapshot() or self._read_log_file_lines()
            failures = self.detect_failures(lines)

        return MetroRuntimeState(
            state=state,
            port=port,
            host=host,
            project_root=str(self.config.project_root),
            running=running,
            pid=pid,
            managed_by_atlas=managed,
            status_body=status_body,
            failures=failures,
            log_line_count=len(self._log_buffer),
        )

    def is_running(self) -> bool:
        return self.get_runtime_state(scan_logs=False).running

    def detect_running(self) -> MetroOperationResult:
        runtime = self.get_runtime_state(scan_logs=True)
        return MetroOperationResult(
            success=True,
            operation="detect_running",
            message="running" if runtime.running else "stopped",
            runtime=runtime,
            details={"pid": str(runtime.pid or "")},
        )

    def start(
        self,
        *,
        reset_cache: bool = False,
        wait_until_ready: bool = True,
    ) -> MetroOperationResult:
        if not self.config.project_root.is_dir():
            return self._fail(
                "start",
                "project_root_missing",
                f"Project root not found: {self.config.project_root}",
            )
        if not (self.config.project_root / "package.json").is_file():
            return self._fail("start", "no_package_json", "package.json missing")

        existing = self.get_runtime_state(scan_logs=False)
        if existing.running and self._process is None:
            logger.info(
                "Metro already running on :%s (pid=%s, external)",
                self.config.port,
                existing.pid,
            )
            return MetroOperationResult(
                success=True,
                operation="start",
                message="already_running_external",
                runtime=existing,
            )

        if self._process and self._process.poll() is None:
            runtime = self.get_runtime_state()
            return MetroOperationResult(
                success=True,
                operation="start",
                message="already_running_managed",
                runtime=runtime,
            )

        self._run_state = MetroRunState.STARTING
        cmd = [self.config.npm_command, "run", self.config.start_script]
        if reset_cache:
            cmd.extend(["--", "--reset-cache"])

        log_name = f"metro-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.log"
        self._log_file = self.config.logs_dir / log_name
        self._log_buffer = MetroLogBuffer(max_lines=self.config.max_log_lines)
        self._stop_reader.clear()

        env = os.environ.copy()
        env["RCT_METRO_PORT"] = str(self.config.port)
        env["PORT"] = str(self.config.port)

        try:
            self._log_fp = self._log_file.open("w", encoding="utf-8")
            self._process = subprocess.Popen(
                cmd,
                cwd=str(self.config.project_root),
                stdout=self._log_fp,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            self._run_state = MetroRunState.FAILED
            return self._fail("start", "spawn_failed", str(exc))

        logger.info(
            "Started Metro (pid=%s) in %s on port %s",
            self._process.pid,
            self.config.project_root,
            self.config.port,
        )

        if wait_until_ready:
            ready, err = self._wait_until_ready()
            if not ready:
                self._run_state = MetroRunState.FAILED
                runtime = self.get_runtime_state()
                return MetroOperationResult(
                    success=False,
                    operation="start",
                    message="start_timeout",
                    error=err,
                    runtime=runtime,
                )

        self._run_state = MetroRunState.RUNNING
        runtime = self.get_runtime_state()
        return MetroOperationResult(
            success=True,
            operation="start",
            message="started",
            runtime=runtime,
            details={
                "pid": str(self._process.pid if self._process else ""),
                "log_file": str(self._log_file),
                "reset_cache": str(reset_cache),
            },
        )

    def stop(self, *, force: bool = False) -> MetroOperationResult:
        self._run_state = MetroRunState.STOPPING
        stopped_any = False

        if self._process and self._process.poll() is None:
            self._terminate_process(self._process, force=force)
            stopped_any = True
            try:
                self._process.wait(timeout=self.config.stop_timeout_sec)
            except subprocess.TimeoutExpired:
                if force:
                    self._process.kill()
                else:
                    self._process.kill()
                    self._process.wait(timeout=5)
            self._process = None

        self._stop_reader.set()
        if self._log_thread and self._log_thread.is_alive():
            self._log_thread.join(timeout=3)
        self._close_log_file()

        port_pids = self._pids_listening_on_port()
        for pid in port_pids:
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
                stopped_any = True
                logger.info("Stopped process on port %s: pid=%s", self.config.port, pid)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                return self._fail("stop", "permission_denied", str(exc))

        self._wait_port_free(timeout=self.config.stop_timeout_sec)
        self._run_state = MetroRunState.STOPPED
        runtime = self.get_runtime_state(scan_logs=True)

        return MetroOperationResult(
            success=True,
            operation="stop",
            message="stopped" if stopped_any else "already_stopped",
            runtime=runtime,
        )

    def restart(
        self,
        *,
        reset_cache: bool = False,
        wait_until_ready: bool = True,
    ) -> MetroOperationResult:
        stop_res = self.stop(force=True)
        if not stop_res.success:
            return MetroOperationResult(
                success=False,
                operation="restart",
                message="stop_failed",
                error=stop_res.error,
                runtime=stop_res.runtime,
            )
        time.sleep(0.5)
        start_res = self.start(reset_cache=reset_cache, wait_until_ready=wait_until_ready)
        return MetroOperationResult(
            success=start_res.success,
            operation="restart",
            message="restarted" if start_res.success else "start_failed",
            error=start_res.error,
            runtime=start_res.runtime,
            details={"reset_cache": str(reset_cache)},
        )

    def get_logs(self, *, tail_lines: int = 40) -> MetroLogResult:
        runtime = self.get_runtime_state(scan_logs=True)
        lines = self._log_buffer.tail(tail_lines)
        if self._log_file and self._log_file.is_file() and not lines:
            try:
                text = self._log_file.read_text(encoding="utf-8", errors="replace")
                lines = text.splitlines()[-tail_lines:]
            except OSError:
                pass
        return MetroLogResult(
            success=True,
            operation="get_logs",
            message="ok",
            runtime=runtime,
            lines=lines,
            log_file=self._log_file,
        )

    def detect_failures(self, lines: list[str] | None = None) -> list[MetroFailureSignature]:
        source = lines if lines is not None else self._log_buffer.snapshot()
        found: list[MetroFailureSignature] = []
        seen: set[tuple[str, str]] = set()
        for i, line in enumerate(source):
            for category, severity, pattern in _METRO_FAILURE_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    key = (category, line[:120])
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(
                        MetroFailureSignature(
                            category=category,
                            severity=severity,
                            pattern=pattern,
                            line=line,
                            line_number=i + 1,
                        )
                    )
        return found

    def _read_process_output(self) -> None:
        proc = self._process
        if not proc or not proc.stdout:
            return
        try:
            for line in proc.stdout:
                if self._stop_reader.is_set():
                    break
                self._log_buffer.append(line)
                if self._log_fp:
                    self._log_fp.write(line if line.endswith("\n") else line + "\n")
                    self._log_fp.flush()
                logger.debug("metro | %s", line.rstrip()[:200])
        except Exception as exc:
            logger.warning("Metro log reader ended: %s", exc)
        finally:
            self._close_log_file()

    def _read_log_file_lines(self) -> list[str]:
        if not self._log_file or not self._log_file.is_file():
            return []
        try:
            return self._log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []

    def _wait_until_ready(self) -> tuple[bool, str | None]:
        deadline = time.monotonic() + self.config.start_ready_timeout_sec
        while time.monotonic() < deadline:
            if self._process and self._process.poll() is not None:
                code = self._process.returncode
                failures = self.detect_failures(self._read_log_file_lines())
                detail = failures[0].line if failures else f"exit code {code}"
                return False, f"Metro exited early: {detail}"
            status = self._fetch_metro_status()
            if status and "running" in status.lower():
                return True, None
            failures = self.detect_failures(self._read_log_file_lines())
            if any(f.severity == "error" for f in failures):
                return False, failures[0].line
            time.sleep(self.config.poll_interval_sec)
        return False, f"Metro not ready within {self.config.start_ready_timeout_sec}s"

    def _fetch_metro_status(self) -> str | None:
        url = f"http://{self.config.host}:{self.config.port}/status"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.read().decode("utf-8", errors="replace").strip()
        except (urllib.error.URLError, TimeoutError, OSError):
            return None

    def _resolve_metro_pid(self) -> int | None:
        if self._process and self._process.poll() is None:
            return self._process.pid
        pids = self._pids_listening_on_port()
        return pids[0] if pids else None

    def _pids_listening_on_port(self) -> list[int]:
        code, out, _ = self._run(
            ["lsof", "-ti", f":{self.config.port}", "-sTCP:LISTEN"],
            timeout=self.config.command_timeout_sec,
        )
        if code != 0 or not out.strip():
            return []
        pids: list[int] = []
        for part in out.strip().split():
            try:
                pids.append(int(part))
            except ValueError:
                continue
        return pids

    def _wait_port_free(self, *, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._pids_listening_on_port():
                return True
            time.sleep(0.3)
        return False

    def _terminate_process(self, proc: subprocess.Popen[str], *, force: bool) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass
        except AttributeError:
            proc.terminate()
            if force:
                proc.kill()

    def _close_log_file(self) -> None:
        if self._log_fp:
            try:
                self._log_fp.close()
            except OSError:
                pass
            self._log_fp = None

    def _run(
        self,
        cmd: list[str],
        *,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        timeout = timeout or self.config.command_timeout_sec
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        except subprocess.TimeoutExpired:
            return 124, "", f"timeout after {timeout}s"
        except OSError as exc:
            return 1, "", str(exc)

    def _fail(self, operation: str, message: str, error: str) -> MetroOperationResult:
        logger.error("Metro %s failed: %s", operation, error)
        return MetroOperationResult(
            success=False,
            operation=operation,
            message=message,
            error=error,
            runtime=self.get_runtime_state(),
        )


def shutil_which(cmd: str) -> str | None:
    from shutil import which

    return which(cmd)
