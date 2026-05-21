"""
Deterministic mobile verification workflows for AssignMint RN iOS.

Composes SimulatorManager + MetroManager + XcodeManager.
No AI, no UI automation — log markers, health checks, screenshots.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core.logger import get_logger
from tools.mobile.metro_manager import (
    ASSIGNMINT_PROJECT_ROOT,
    MetroManager,
    MetroManagerConfig,
)
from tools.mobile.simulator_manager import (
    SimulatorDevice,
    SimulatorManager,
    SimulatorManagerConfig,
)
from tools.mobile.xcode_manager import (
    ASSIGNMINT_BUNDLE_ID,
    XcodeManager,
    XcodeManagerConfig,
)

logger = get_logger("mobile.workflows")

# Metro / JS console markers (AssignMint RootNavigator + RN runtime)
_LOGIN_LOG_MARKERS = [
    r"shouldShowAuth",
    r"Navigation Decision",
    r"AuthNavigator|Auth Stack|\"Login\"",
    r"Sign In|Create Account|Forgot Password",
]
_DASHBOARD_LOG_MARKERS = [
    r"shouldShowMain",
    r"MainTabs|HomeScreen|AppTabs",
]
_RUNTIME_ERROR_MARKERS = [
    r"RedBox|Invariant Violation|Unhandled JS Exception",
    r"Unable to load script|Could not connect to development server",
    r"RCTFatal|FATAL ERROR",
]

# Native iOS crash signatures scraped from simulator device log
_NATIVE_CRASH_PATTERNS: list[tuple[str, str, str]] = [
    ("rct_fatal",             "error", r"RCTFatal"),
    ("no_bundle_url",         "error", r"No bundle URL present"),
    ("redbox",                "error", r"RedBox"),
    ("invariant_violation",   "error", r"Invariant Violation"),
    ("unhandled_js_exception","error", r"Unhandled JS Exception"),
    ("js_thread_exception",   "error", r"com\.facebook\.react\.JavaScript.*Exception"),
]


@dataclass
class ScreenshotEvidence:
    label: str
    path: str
    captured_at: str
    device_name: str
    udid: str
    file_size_bytes: int = 0

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "path": self.path,
            "captured_at": self.captured_at,
            "device_name": self.device_name,
            "udid": self.udid,
            "file_size_bytes": self.file_size_bytes,
        }


@dataclass
class MobileWorkflowConfig:
    project_root: Path = field(default_factory=lambda: ASSIGNMINT_PROJECT_ROOT)
    simulator_name: str = "iPhone 16 Pro"
    bundle_id: str = ASSIGNMINT_BUNDLE_ID
    scheme: str = "Assignmint"
    metro_port: int = 8081
    screenshots_dir: Path = Path("logs/screenshots/mobile/verify")
    metro_logs_dir: Path = Path("logs/mobile/metro")
    xcode_logs_dir: Path = Path("logs/mobile/xcode")
    stability_wait_sec: float = 5.0
    log_tail_lines: int = 80
    ios_build_timeout_sec: float = 900.0
    skip_build_if_app_installed: bool = True


@dataclass
class MobileVerificationResult:
    """Structured verification outcome for RecoveryEngine / task metadata."""

    workflow: str
    success: bool
    message: str = ""
    screenshots: list[ScreenshotEvidence] = field(default_factory=list)
    runtime_health: dict = field(default_factory=dict)
    metro_health: dict = field(default_factory=dict)
    simulator_info: dict = field(default_factory=dict)
    detected_failures: list[dict] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    finished_at: str | None = None
    details: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "workflow": self.workflow,
            "success": self.success,
            "message": self.message,
            "screenshots": [s.to_dict() for s in self.screenshots],
            "runtime_health": self.runtime_health,
            "metro_health": self.metro_health,
            "simulator_info": self.simulator_info,
            "detected_failures": self.detected_failures,
            "diagnostics": self.diagnostics,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "details": self.details,
        }


class MobileWorkflows:
    """AssignMint iOS verification workflows."""

    def __init__(
        self,
        config: MobileWorkflowConfig | None = None,
        *,
        simulator: SimulatorManager | None = None,
        metro: MetroManager | None = None,
        xcode: XcodeManager | None = None,
    ) -> None:
        self.config = config or MobileWorkflowConfig()
        self.config.project_root = self.config.project_root.resolve()
        self.config.screenshots_dir.mkdir(parents=True, exist_ok=True)

        self._sim = simulator or SimulatorManager(
            SimulatorManagerConfig(screenshots_dir=self.config.screenshots_dir)
        )
        self._metro = metro or MetroManager(
            MetroManagerConfig(
                project_root=self.config.project_root,
                port=self.config.metro_port,
                logs_dir=self.config.metro_logs_dir,
            )
        )
        self._xcode = xcode or XcodeManager(
            XcodeManagerConfig(
                project_root=self.config.project_root,
                simulator_name=self.config.simulator_name,
                scheme=self.config.scheme,
                bundle_id=self.config.bundle_id,
                metro_port=self.config.metro_port,
                use_packager=False,
                logs_dir=self.config.xcode_logs_dir,
                build_timeout_sec=self.config.ios_build_timeout_sec,
            )
        )
        self._device: SimulatorDevice | None = None

    @classmethod
    def from_atlas_dirs(
        cls,
        log_dir: Path,
        mobile_cfg: dict,
        project_root: Path,
    ) -> MobileWorkflows:
        cfg = MobileWorkflowConfig(
            project_root=project_root,
            simulator_name=mobile_cfg.get("primary_simulator", "iPhone 16 Pro"),
            metro_port=int(mobile_cfg.get("metro_port", 8081)),
            scheme=mobile_cfg.get("xcode_scheme", "Assignmint"),
            bundle_id=mobile_cfg.get("bundle_id", ASSIGNMINT_BUNDLE_ID),
            screenshots_dir=log_dir / "screenshots" / "mobile" / "verify",
            metro_logs_dir=log_dir / "mobile" / "metro",
            xcode_logs_dir=log_dir / "mobile" / "xcode",
            stability_wait_sec=float(mobile_cfg.get("stability_wait_sec", 5)),
            ios_build_timeout_sec=float(mobile_cfg.get("ios_build_timeout_sec", 900)),
        )
        return cls(cfg)

    def verify_ios_launch(self, *, force_build: bool = False) -> MobileVerificationResult:
        result = self._base_result("verify_ios_launch")
        try:
            device = self._ensure_simulator_booted(result)
            if not device:
                return self._finalize(result, False, "simulator_boot_failed")

            if not self._ensure_metro_running(result):
                return self._finalize(result, False, "metro_start_failed")

            launch_ok = self._ensure_app_launched(
                device, result, force_build=force_build
            )
            if not launch_ok:
                return self._finalize(result, False, "app_launch_failed")

            stable = self._verify_runtime_stability(result)
            shot = self._capture_screenshot(device, "ios-launch", result)
            if not shot:
                return self._finalize(result, False, "screenshot_failed")

            failures = self._collect_failures()
            result.detected_failures = failures
            if any(f.get("severity") == "error" for f in failures):
                return self._finalize(result, False, "runtime_errors_detected")

            if not stable:
                return self._finalize(result, False, "runtime_unstable")

            return self._finalize(result, True, "ios_launch_verified")
        except Exception as exc:
            logger.exception("verify_ios_launch failed")
            result.diagnostics.append(str(exc))
            return self._finalize(result, False, "exception")

    def capture_home_screen(self) -> MobileVerificationResult:
        result = self._base_result("capture_home_screen")
        device = self._ensure_simulator_booted(result)
        if not device:
            return self._finalize(result, False, "simulator_not_ready")

        self._ensure_metro_running(result)
        self._ensure_app_foreground(device)
        self._wait_stability()
        self._refresh_health(result)

        shot = self._capture_screenshot(device, "home-screen", result)
        if not shot:
            return self._finalize(result, False, "screenshot_failed")

        return self._finalize(result, True, "home_screen_captured")

    def verify_login_screen(self) -> MobileVerificationResult:
        result = self._base_result("verify_login_screen")
        device = self._ensure_simulator_booted(result)
        if not device:
            return self._finalize(result, False, "simulator_not_ready")

        if not self._ensure_metro_running(result):
            return self._finalize(result, False, "metro_not_ready")

        self._terminate_app(device.udid)
        self._launch_app(device.udid)
        self._wait_stability()

        shot = self._capture_screenshot(device, "login-screen", result)
        if not shot:
            return self._finalize(result, False, "screenshot_failed")

        logs = self._metro.get_logs(tail_lines=self.config.log_tail_lines).lines
        result.detected_failures = self._collect_failures()
        if self._has_runtime_errors(logs):
            return self._finalize(result, False, "runtime_errors_on_login")

        if not self._logs_match(logs, _LOGIN_LOG_MARKERS):
            result.diagnostics.append(
                "login_log_markers_missing — app may be on loading or main tabs (guest/auth)"
            )
            return self._finalize(result, False, "login_screen_not_confirmed")

        self._refresh_health(result)
        return self._finalize(result, True, "login_screen_verified")

    def verify_dashboard_layout(self) -> MobileVerificationResult:
        result = self._base_result("verify_dashboard_layout")
        device = self._ensure_simulator_booted(result)
        if not device:
            return self._finalize(result, False, "simulator_not_ready")

        if not self._ensure_metro_running(result):
            return self._finalize(result, False, "metro_not_ready")

        self._ensure_app_foreground(device)
        self._wait_stability()

        logs = self._metro.get_logs(tail_lines=self.config.log_tail_lines).lines
        result.detected_failures = self._collect_failures()

        if self._has_runtime_errors(logs):
            return self._finalize(result, False, "runtime_errors_on_dashboard")

        if not self._logs_match(logs, _DASHBOARD_LOG_MARKERS):
            result.diagnostics.append(
                "dashboard_log_markers_missing — requires authenticated or guest session"
            )
            result.details["session_required"] = "guest_or_auth"
            return self._finalize(result, False, "dashboard_not_confirmed")

        shot = self._capture_screenshot(device, "dashboard-layout", result)
        if not shot:
            return self._finalize(result, False, "screenshot_failed")

        self._refresh_health(result)
        return self._finalize(result, True, "dashboard_layout_verified")

    def run_verify_demo(self) -> list[MobileVerificationResult]:
        """Run standard verification sequence; returns all workflow results."""
        results: list[MobileVerificationResult] = []
        for fn in (
            self.verify_ios_launch,
            self.capture_home_screen,
            self.verify_login_screen,
            self.verify_dashboard_layout,
        ):
            logger.info("Running workflow: %s", fn.__name__)
            results.append(fn())
        return results

    def _ensure_simulator_booted(
        self, result: MobileVerificationResult
    ) -> SimulatorDevice | None:
        boot = self._sim.boot(self.config.simulator_name, open_app=True)
        if not boot.success or not boot.device:
            result.diagnostics.append(boot.error or boot.message)
            return None
        self._device = boot.device
        result.simulator_info = {
            "name": boot.device.name,
            "udid": boot.device.udid,
            "state": boot.device.state,
            "runtime": boot.device.runtime,
        }
        return boot.device

    def _ensure_metro_running(self, result: MobileVerificationResult) -> bool:
        if self._metro.is_running():
            self._refresh_health(result)
            return True
        start = self._metro.start(wait_until_ready=True)
        self._refresh_health(result)
        if not start.success:
            result.diagnostics.append(start.error or "metro start failed")
            return False
        return True

    def _ensure_app_launched(
        self,
        device: SimulatorDevice,
        result: MobileVerificationResult,
        *,
        force_build: bool,
    ) -> bool:
        installed = self._xcode._verify_app_container(device.udid)
        if (
            self.config.skip_build_if_app_installed
            and installed
            and not force_build
        ):
            logger.info("App installed — launching via simctl (skip build)")
            self._launch_app(device.udid)
            self._wait_stability()
            result.runtime_health = {
                "launch_mode": "simctl",
                "app_installed": True,
                "app_running": self._is_app_running(device.udid),
            }
            return result.runtime_health.get("app_running", False)

        launch = self._xcode.launch_ios(
            simulator_name=device.name,
            simulator_udid=device.udid,
        )
        if launch.summary:
            result.runtime_health = launch.summary.to_dict()
        if not launch.success:
            result.diagnostics.append(launch.error or launch.message)
            return False
        self._wait_stability()
        return True

    def _verify_runtime_stability(self, result: MobileVerificationResult) -> bool:
        self._wait_stability()
        self._refresh_health(result)
        metro = result.metro_health
        if not metro.get("running"):
            result.diagnostics.append("metro not running after stability wait")
            return False
        if metro.get("failure_count", 0) > 0:
            result.diagnostics.append("metro failures after stability wait")
            return False
        return True

    def _capture_screenshot(
        self,
        device: SimulatorDevice,
        label: str,
        result: MobileVerificationResult,
    ) -> ScreenshotEvidence | None:
        shot = self._sim.capture_screenshot(
            device.udid,
            label=label,
        )
        if not shot.success or not shot.path:
            result.diagnostics.append(shot.error or "screenshot failed")
            return None
        size = shot.path.stat().st_size if shot.path.is_file() else 0
        evidence = ScreenshotEvidence(
            label=label,
            path=str(shot.path),
            captured_at=datetime.now(timezone.utc).isoformat(),
            device_name=device.name,
            udid=device.udid,
            file_size_bytes=size,
        )
        result.screenshots.append(evidence)
        logger.info("Screenshot [%s]: %s (%d bytes)", label, evidence.path, size)
        return evidence

    def _collect_failures(self) -> list[dict]:
        combined: list[dict] = []
        metro_state = self._metro.get_runtime_state()
        for f in metro_state.failures:
            combined.append({**f.to_dict(), "source": "metro"})
        for f in self._xcode.detect_failures():
            combined.append({**f.to_dict(), "source": "xcode"})
        if self._device:
            combined.extend(self._collect_device_log_failures(self._device.udid))
        return combined

    def _collect_device_log_failures(self, udid: str) -> list[dict]:
        """Scan simulator device log for native RN crash markers (last 60s)."""
        code, out, _ = self._sim._run(
            [
                "xcrun", "simctl", "spawn", udid, "log", "show",
                "--last", "60s", "--style", "compact",
                "--predicate", 'process == "Assignmint"',
            ],
            timeout=30,
        )
        if code != 0 or not out.strip():
            return []
        failures: list[dict] = []
        seen: set[str] = set()
        for line in out.splitlines():
            for category, severity, pattern in _NATIVE_CRASH_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    key = f"{category}:{line[:80]}"
                    if key in seen:
                        continue
                    seen.add(key)
                    failures.append({
                        "category": category,
                        "severity": severity,
                        "pattern": pattern,
                        "line": line[:500],
                        "source": "device_log",
                    })
        return failures

    def _refresh_health(self, result: MobileVerificationResult) -> None:
        result.metro_health = self._metro.get_runtime_state().to_dict()
        if self._device:
            result.simulator_info = {
                "name": self._device.name,
                "udid": self._device.udid,
                "state": self._device.state,
                "booted": self._device.is_booted,
                "app_running": self._is_app_running(self._device.udid),
            }

    def _wait_stability(self) -> None:
        time.sleep(self.config.stability_wait_sec)

    def _is_app_running(self, udid: str) -> bool:
        code, out, _ = self._sim._run(
            ["xcrun", "simctl", "spawn", udid, "launchctl", "list"],
            timeout=15,
        )
        if code != 0:
            return self._xcode._verify_app_container(udid)
        return self.config.bundle_id in out or "Assignmint" in out

    def _terminate_app(self, udid: str) -> None:
        self._sim._run(
            ["xcrun", "simctl", "terminate", udid, self.config.bundle_id],
            timeout=15,
        )

    def _launch_app(self, udid: str) -> None:
        code, out, err = self._sim._run(
            ["xcrun", "simctl", "launch", udid, self.config.bundle_id],
            timeout=30,
        )
        if code != 0:
            logger.warning("simctl launch: %s %s", out, err)

    def _ensure_app_foreground(self, device: SimulatorDevice) -> None:
        if not self._is_app_running(device.udid):
            self._launch_app(device.udid)
            self._wait_stability()

    @staticmethod
    def _logs_match(lines: list[str], patterns: list[str]) -> bool:
        text = "\n".join(lines)
        return any(re.search(p, text, re.IGNORECASE) for p in patterns)

    @staticmethod
    def _has_runtime_errors(lines: list[str]) -> bool:
        return MobileWorkflows._logs_match(lines, _RUNTIME_ERROR_MARKERS)

    def _base_result(self, workflow: str) -> MobileVerificationResult:
        return MobileVerificationResult(workflow=workflow, success=False)

    def _finalize(
        self,
        result: MobileVerificationResult,
        success: bool,
        message: str,
    ) -> MobileVerificationResult:
        result.success = success
        result.message = message
        result.finished_at = datetime.now(timezone.utc).isoformat()
        if not result.metro_health:
            self._refresh_health(result)
        if not result.detected_failures:
            result.detected_failures = self._collect_failures()
        logger.info(
            "Workflow %s: success=%s message=%s screenshots=%d failures=%d",
            result.workflow,
            success,
            message,
            len(result.screenshots),
            len(result.detected_failures),
        )
        return result
