"""
Xcode / React Native iOS launch orchestration for AssignMint (macOS).

Bridges Metro + Simulator into `npx react-native run-ios` workflows.
Operational only — structured logs, lifecycle flags, failure signatures.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from core.logger import get_logger

logger = get_logger("mobile.xcode")

ASSIGNMINT_PROJECT_ROOT = Path("/Users/hamza/Desktop/Assignmint3")
ASSIGNMINT_BUNDLE_ID = "com.assignmint.app"

# category, severity, regex
_XCODE_FAILURE_PATTERNS: list[tuple[str, str, str]] = [
    ("metro_connection", "error", r"Could not connect to development server|Metro|packager|8081|RCT_METRO"),
    ("pod_issues", "error", r"pod install|CocoaPods|The sandbox is not in sync|Pods/"),
    ("derived_data", "warning", r"DerivedData|clean build folder|ModuleCache"),
    ("signing", "error", r"code signing|Provisioning profile|Signing for|requires a development team|signing certificate"),
    ("simulator_unavailable", "error", r"Unable to find a destination|simulator not found|no devices|not available"),
    ("duplicate_symbols", "error", r"duplicate symbol"),
    ("missing_dependency", "error", r"No such module|ld: library not found|module not found"),
    ("js_bundle", "error", r"bundle JS|Bundler|Unable to resolve|JS bundle"),
    ("build_failed", "error", r"\*\* BUILD FAILED \*\*|xcodebuild.*error"),
    ("compile_error", "error", r"error:.*\.(m|mm|swift|h|cpp)"),
]

_LIFECYCLE_MARKERS: list[tuple[str, str]] = [
    ("build_started", r"xcodebuild|Building workspace|CompileC|Building target"),
    ("build_succeeded", r"\*\* BUILD SUCCEEDED \*\*"),
    ("build_failed", r"\*\* BUILD FAILED \*\*"),
    ("install_completed", r"Installing|Install (Succeeded|Success)|installed on"),
    ("simulator_connected", r"simulator|Booted|Opening .* on iPhone|Using simulator"),
    ("app_launch_detected", r"Launching|Successfully launched|successfully installed"),
]


class BuildLifecycle(str, Enum):
    PENDING = "pending"
    BUILDING = "building"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INSTALLING = "installing"
    LAUNCHED = "launched"


@dataclass
class XcodeFailureSignature:
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
class IOSLaunchSummary:
    """Structured iOS launch outcome for monitoring / RecoveryEngine."""

    success: bool
    build_success: bool = False
    install_success: bool = False
    simulator_connected: bool = False
    app_launch_detected: bool = False
    launch_duration_sec: float = 0.0
    lifecycle: BuildLifecycle = BuildLifecycle.PENDING
    simulator_name: str = ""
    simulator_udid: str | None = None
    bundle_id: str = ASSIGNMINT_BUNDLE_ID
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    failures: list[XcodeFailureSignature] = field(default_factory=list)
    log_line_count: int = 0
    log_file: str | None = None
    exit_code: int | None = None
    finished_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "build_success": self.build_success,
            "install_success": self.install_success,
            "simulator_connected": self.simulator_connected,
            "app_launch_detected": self.app_launch_detected,
            "launch_duration_sec": round(self.launch_duration_sec, 2),
            "lifecycle": self.lifecycle.value,
            "simulator_name": self.simulator_name,
            "simulator_udid": self.simulator_udid,
            "bundle_id": self.bundle_id,
            "warnings": self.warnings[:20],
            "errors": self.errors[:20],
            "failure_count": len(self.failures),
            "failures": [f.to_dict() for f in self.failures],
            "log_line_count": self.log_line_count,
            "log_file": self.log_file,
            "exit_code": self.exit_code,
            "finished_at": self.finished_at,
        }


@dataclass
class XcodeOperationResult:
    success: bool
    operation: str
    message: str = ""
    error: str | None = None
    summary: IOSLaunchSummary | None = None
    details: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "operation": self.operation,
            "message": self.message,
            "error": self.error,
            "summary": self.summary.to_dict() if self.summary else None,
            "details": self.details,
        }


class XcodeLogBuffer:
    def __init__(self, max_lines: int = 1000) -> None:
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        line = line.rstrip("\n")
        if not line:
            return
        with self._lock:
            self._lines.append(line)

    def tail(self, n: int = 50) -> list[str]:
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
class XcodeManagerConfig:
    project_root: Path = field(default_factory=lambda: ASSIGNMINT_PROJECT_ROOT)
    simulator_name: str = "iPhone 16 Pro"
    scheme: str = "Assignmint"
    bundle_id: str = ASSIGNMINT_BUNDLE_ID
    metro_port: int = 8081
    use_packager: bool = False
    npx_command: str = "npx"
    logs_dir: Path = Path("logs/mobile/xcode")
    build_timeout_sec: float = 900.0
    command_timeout_sec: float = 30.0
    max_log_lines: int = 1000


class XcodeManager:
    """
    Launch and monitor `react-native run-ios` for AssignMint.

    Future: direct xcodebuild invocation via same log/lifecycle parsers.
    """

    def __init__(self, config: XcodeManagerConfig | None = None) -> None:
        self.config = config or XcodeManagerConfig()
        self.config.project_root = self.config.project_root.resolve()
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)
        self._log_buffer = XcodeLogBuffer(max_lines=self.config.max_log_lines)
        self._process: subprocess.Popen[str] | None = None
        self._log_thread: threading.Thread | None = None
        self._log_file: Path | None = None
        self._log_fp = None
        self._stop_reader = threading.Event()
        self._lifecycle_flags: dict[str, bool] = {k: False for k, _ in _LIFECYCLE_MARKERS}

    @staticmethod
    def is_available(project_root: Path | None = None) -> bool:
        root = (project_root or ASSIGNMINT_PROJECT_ROOT).resolve()
        ios_dir = root / "ios"
        if not (root / "package.json").is_file() or not ios_dir.is_dir():
            return False
        from shutil import which

        return which("npx") is not None and which("xcodebuild") is not None

    def launch_ios(
        self,
        *,
        simulator_name: str | None = None,
        simulator_udid: str | None = None,
        scheme: str | None = None,
        wait_for_completion: bool = True,
    ) -> XcodeOperationResult:
        if not self.is_available(self.config.project_root):
            return self._fail("launch_ios", "unavailable", "npx or xcodebuild not found")

        sim = simulator_name or self.config.simulator_name
        self._reset_run_state(sim, simulator_udid)

        cmd = [
            self.config.npx_command,
            "react-native",
            "run-ios",
            "--simulator",
            sim,
            "--port",
            str(self.config.metro_port),
        ]
        if scheme or self.config.scheme:
            cmd.extend(["--scheme", scheme or self.config.scheme])
        if simulator_udid:
            cmd.extend(["--udid", simulator_udid])

        log_name = f"run-ios-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.log"
        self._log_file = self.config.logs_dir / log_name
        self._stop_reader.clear()
        started = time.monotonic()

        try:
            self._log_fp = self._log_file.open("w", encoding="utf-8")
            self._process = subprocess.Popen(
                cmd,
                cwd=str(self.config.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=os.environ.copy(),
                start_new_session=True,
            )
        except OSError as exc:
            return self._fail("launch_ios", "spawn_failed", str(exc))

        self._lifecycle_flags["build_started"] = True
        self._log_thread = threading.Thread(
            target=self._read_process_output,
            name="atlas-xcode-log-reader",
            daemon=True,
        )
        self._log_thread.start()
        logger.info(
            "Started run-ios (pid=%s) simulator=%s scheme=%s",
            self._process.pid,
            sim,
            scheme or self.config.scheme,
        )

        exit_code: int | None = None
        if wait_for_completion and self._process:
            try:
                exit_code = self._process.wait(timeout=self.config.build_timeout_sec)
            except subprocess.TimeoutExpired:
                self._terminate_process(self._process, force=True)
                exit_code = -1
                self._lifecycle_flags["build_failed"] = True
            self._process = None

        self._stop_reader.set()
        if self._log_thread and self._log_thread.is_alive():
            self._log_thread.join(timeout=5)
        self._close_log_file()

        duration = time.monotonic() - started
        summary = self._build_summary(
            simulator_name=sim,
            simulator_udid=simulator_udid,
            launch_duration_sec=duration,
            exit_code=exit_code,
        )

        if exit_code == 0 and not self._lifecycle_flags.get("build_failed"):
            summary.build_success = True
            if not summary.app_launch_detected:
                summary.app_launch_detected = summary.install_success or True
            summary.success = True
        elif exit_code == -1:
            summary.success = False
            summary.errors.append(
                f"Build timed out after {self.config.build_timeout_sec}s"
            )
        elif exit_code is not None and exit_code != 0:
            summary.success = False
            if not summary.errors:
                summary.errors.append(f"run-ios exited with code {exit_code}")

        return XcodeOperationResult(
            success=summary.success,
            operation="launch_ios",
            message="launched" if summary.success else "launch_incomplete",
            error=None if summary.success else (summary.errors[0] if summary.errors else "launch failed"),
            summary=summary,
            details={
                "log_file": str(self._log_file),
                "command": " ".join(cmd),
            },
        )

    def get_logs(self, *, tail_lines: int = 50) -> list[str]:
        lines = self._log_buffer.tail(tail_lines)
        if not lines and self._log_file and self._log_file.is_file():
            try:
                text = self._log_file.read_text(encoding="utf-8", errors="replace")
                lines = text.splitlines()[-tail_lines:]
            except OSError:
                pass
        return lines

    def detect_failures(self, lines: list[str] | None = None) -> list[XcodeFailureSignature]:
        source = lines if lines is not None else self._log_buffer.snapshot()
        found: list[XcodeFailureSignature] = []
        seen: set[tuple[str, str]] = set()
        for i, line in enumerate(source):
            for category, severity, pattern in _XCODE_FAILURE_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    key = (category, line[:120])
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(
                        XcodeFailureSignature(
                            category=category,
                            severity=severity,
                            pattern=pattern,
                            line=line,
                            line_number=i + 1,
                        )
                    )
        return found

    def _build_summary(
        self,
        *,
        simulator_name: str,
        simulator_udid: str | None,
        launch_duration_sec: float,
        exit_code: int | None,
    ) -> IOSLaunchSummary:
        lines = self._log_buffer.snapshot()
        failures = self.detect_failures(lines)
        errors = [f.line for f in failures if f.severity == "error"][:10]
        warnings = [f.line for f in failures if f.severity == "warning"][:10]

        build_success = self._lifecycle_flags.get("build_succeeded", False)
        build_failed = self._lifecycle_flags.get("build_failed", False)
        if build_failed:
            build_success = False
        if exit_code == 0 and not build_failed:
            build_success = build_success or self._lifecycle_flags.get("install_completed", False)

        install_success = self._lifecycle_flags.get("install_completed", False)
        simulator_connected = self._lifecycle_flags.get("simulator_connected", False)
        app_launch_detected = self._lifecycle_flags.get("app_launch_detected", False)

        if simulator_udid and self._verify_app_container(simulator_udid):
            install_success = True
            if not app_launch_detected:
                app_launch_detected = True

        lifecycle = BuildLifecycle.PENDING
        if app_launch_detected and build_success:
            lifecycle = BuildLifecycle.LAUNCHED
        elif install_success:
            lifecycle = BuildLifecycle.INSTALLING
        elif build_success:
            lifecycle = BuildLifecycle.SUCCEEDED
        elif build_failed:
            lifecycle = BuildLifecycle.FAILED
        elif self._lifecycle_flags.get("build_started"):
            lifecycle = BuildLifecycle.BUILDING

        success = build_success and (app_launch_detected or install_success) and not build_failed

        return IOSLaunchSummary(
            success=bool(success),
            build_success=build_success,
            install_success=install_success,
            simulator_connected=simulator_connected or bool(simulator_udid),
            app_launch_detected=app_launch_detected,
            launch_duration_sec=launch_duration_sec,
            lifecycle=lifecycle,
            simulator_name=simulator_name,
            simulator_udid=simulator_udid,
            bundle_id=self.config.bundle_id,
            warnings=warnings,
            errors=errors,
            failures=failures,
            log_line_count=len(lines),
            log_file=str(self._log_file) if self._log_file else None,
            exit_code=exit_code,
        )

    def _scan_line(self, line: str) -> None:
        for key, pattern in _LIFECYCLE_MARKERS:
            if re.search(pattern, line, re.IGNORECASE):
                self._lifecycle_flags[key] = True

    def _read_process_output(self) -> None:
        proc = self._process
        if not proc or not proc.stdout:
            return
        try:
            for line in proc.stdout:
                if self._stop_reader.is_set():
                    break
                self._log_buffer.append(line)
                self._scan_line(line)
                if self._log_fp:
                    self._log_fp.write(line if line.endswith("\n") else line + "\n")
                    self._log_fp.flush()
                logger.debug("xcode | %s", line.rstrip()[:200])
        except Exception as exc:
            logger.warning("Xcode log reader ended: %s", exc)
        finally:
            self._close_log_file()

    def _verify_app_container(self, udid: str) -> bool:
        code, _, _ = self._run(
            [
                "xcrun",
                "simctl",
                "get_app_container",
                udid,
                self.config.bundle_id,
            ],
            timeout=self.config.command_timeout_sec,
        )
        return code == 0

    def _reset_run_state(self, simulator_name: str, simulator_udid: str | None) -> None:
        self._log_buffer = XcodeLogBuffer(max_lines=self.config.max_log_lines)
        self._lifecycle_flags = {k: False for k, _ in _LIFECYCLE_MARKERS}
        self.config.simulator_name = simulator_name
        if simulator_udid:
            self._lifecycle_flags["simulator_connected"] = True

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

    def _fail(self, operation: str, message: str, error: str) -> XcodeOperationResult:
        logger.error("Xcode %s failed: %s", operation, error)
        return XcodeOperationResult(
            success=False,
            operation=operation,
            message=message,
            error=error,
        )
