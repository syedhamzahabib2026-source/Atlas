"""
iOS Simulator control for Atlas mobile runtime (AssignMint React Native).

Uses xcrun simctl and the Simulator.app. Operational infrastructure only —
designed for future RecoveryEngine / verification attachment via structured results.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from core.logger import get_logger

logger = get_logger("mobile.simulator")

_UDID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


def _runtime_version_key(runtime: str) -> tuple[int, ...]:
    """Prefer newest iOS runtime when device names collide."""
    m = re.search(r"iOS-(\d+)(?:-(\d+))?", runtime, re.IGNORECASE)
    if not m:
        return (0,)
    parts = [int(m.group(1))]
    if m.group(2):
        parts.append(int(m.group(2)))
    return tuple(parts)


@dataclass(frozen=True)
class SimulatorDevice:
    """One simulator from simctl."""

    udid: str
    name: str
    state: str
    runtime: str
    is_available: bool = True

    @property
    def is_booted(self) -> bool:
        return self.state.lower() == "booted"

    @property
    def is_ios(self) -> bool:
        return "ios" in self.runtime.lower()


@dataclass
class SimulatorOperationResult:
    """Base result for simulator ops — attach to task/recovery metadata later."""

    success: bool
    operation: str
    message: str = ""
    error: str | None = None
    device: SimulatorDevice | None = None
    devices: list[SimulatorDevice] = field(default_factory=list)
    details: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """JSON-friendly summary for verification / recovery pipelines."""
        return {
            "success": self.success,
            "operation": self.operation,
            "message": self.message,
            "error": self.error,
            "device_udid": self.device.udid if self.device else None,
            "device_name": self.device.name if self.device else None,
            "device_count": len(self.devices),
            "details": self.details,
        }


@dataclass
class ScreenshotResult(SimulatorOperationResult):
    """Screenshot capture outcome."""

    path: Path | None = None

    def to_dict(self) -> dict:
        base = super().to_dict()
        base["screenshot_path"] = str(self.path) if self.path else None
        return base


@dataclass
class SimulatorManagerConfig:
    command_timeout_sec: float = 60.0
    boot_timeout_sec: float = 120.0
    screenshots_dir: Path = Path("logs/screenshots/mobile")
    ios_only: bool = True


class SimulatorManager:
    """
    macOS iOS Simulator operations via simctl.

    Future: wire into mobile verification agents and RecoveryEngine evidence.
    """

    def __init__(self, config: SimulatorManagerConfig | None = None) -> None:
        self.config = config or SimulatorManagerConfig()
        self.config.screenshots_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def is_available() -> bool:
        try:
            proc = subprocess.run(
                ["xcrun", "simctl", "help"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            return proc.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    def list_devices(
        self,
        *,
        available_only: bool = True,
        ios_only: bool | None = None,
    ) -> SimulatorOperationResult:
        flag = "available" if available_only else None
        return self._list_devices_json(
            operation="list_devices",
            simctl_args=["list", "devices"] + ([flag] if flag else []) + ["-j"],
            ios_only=self.config.ios_only if ios_only is None else ios_only,
        )

    def list_booted(self, *, ios_only: bool | None = None) -> SimulatorOperationResult:
        return self._list_devices_json(
            operation="list_booted",
            simctl_args=["list", "devices", "booted", "-j"],
            ios_only=self.config.ios_only if ios_only is None else ios_only,
        )

    def resolve_device(
        self,
        device: str,
        *,
        prefer_booted: bool = True,
    ) -> SimulatorDevice | None:
        """Resolve by UDID or exact device name (newest runtime if ambiguous)."""
        target = device.strip()
        listed = self.list_devices()
        if not listed.success:
            return None
        candidates = listed.devices
        if _UDID_RE.match(target):
            for d in candidates:
                if d.udid.lower() == target.lower():
                    return d
            return None
        named = [d for d in candidates if d.name == target]
        if not named:
            return None
        if prefer_booted:
            booted = [d for d in named if d.is_booted]
            if booted:
                return booted[0]
        named.sort(key=lambda d: _runtime_version_key(d.runtime), reverse=True)
        return named[0]

    def boot(
        self,
        device: str,
        *,
        open_app: bool = False,
    ) -> SimulatorOperationResult:
        resolved = self.resolve_device(device)
        if not resolved:
            return SimulatorOperationResult(
                success=False,
                operation="boot",
                message=f"Device not found: {device}",
                error="not_found",
            )
        if resolved.is_booted:
            logger.info("Simulator already booted: %s (%s)", resolved.name, resolved.udid)
            if open_app:
                self.open_simulator_app(resolved.udid)
            return SimulatorOperationResult(
                success=True,
                operation="boot",
                message="already_booted",
                device=resolved,
            )

        code, out, err = self._run(
            ["xcrun", "simctl", "boot", resolved.udid],
            timeout=self.config.boot_timeout_sec,
        )
        if code != 0:
            msg = (err or out).strip()
            logger.error("Boot failed for %s: %s", resolved.udid, msg)
            return SimulatorOperationResult(
                success=False,
                operation="boot",
                message="boot_failed",
                error=msg,
                device=resolved,
            )

        logger.info("Booted simulator %s (%s)", resolved.name, resolved.udid)
        refreshed = self.resolve_device(resolved.udid) or resolved
        if open_app:
            self.open_simulator_app(refreshed.udid)
        return SimulatorOperationResult(
            success=True,
            operation="boot",
            message="booted",
            device=refreshed,
        )

    def open_simulator_app(self, udid: str | None = None) -> SimulatorOperationResult:
        cmd = ["open", "-a", "Simulator"]
        if udid:
            cmd.extend(["--args", "-CurrentDeviceUDID", udid])
        code, out, err = self._run(cmd, timeout=self.config.command_timeout_sec)
        if code != 0:
            msg = (err or out).strip()
            logger.error("Failed to open Simulator.app: %s", msg)
            return SimulatorOperationResult(
                success=False,
                operation="open_simulator",
                message="open_failed",
                error=msg,
            )
        logger.info("Opened Simulator.app%s", f" (udid={udid})" if udid else "")
        return SimulatorOperationResult(
            success=True,
            operation="open_simulator",
            message="opened",
            details={"udid": udid or ""},
        )

    def capture_screenshot(
        self,
        device: str | None = None,
        *,
        output_path: Path | None = None,
        label: str = "capture",
    ) -> ScreenshotResult:
        """
        Capture PNG from booted simulator.

        device: UDID, device name, or None/'booted' for simctl's booted alias.
        """
        resolved: SimulatorDevice | None = None
        target = "booted"
        if device and device.strip().lower() not in ("booted", ""):
            resolved = self.resolve_device(device, prefer_booted=True)
            if not resolved:
                return ScreenshotResult(
                    success=False,
                    operation="screenshot",
                    message=f"Device not found: {device}",
                    error="not_found",
                )
            if not resolved.is_booted:
                boot_res = self.boot(resolved.udid, open_app=True)
                if not boot_res.success:
                    return ScreenshotResult(
                        success=False,
                        operation="screenshot",
                        message="boot_before_screenshot_failed",
                        error=boot_res.error,
                        device=resolved,
                    )
                resolved = boot_res.device or resolved
            target = resolved.udid
        else:
            booted = self.list_booted()
            if booted.devices:
                resolved = booted.devices[0]
                target = resolved.udid

        path = output_path or (
            self.config.screenshots_dir / f"{label}-{target[:8]}.png"
        )
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)

        code, out, err = self._run(
            ["xcrun", "simctl", "io", target, "screenshot", str(path)],
            timeout=self.config.command_timeout_sec,
        )
        if code != 0 or not path.is_file():
            msg = (err or out).strip() or "screenshot file missing"
            logger.error("Screenshot failed (%s): %s", target, msg)
            return ScreenshotResult(
                success=False,
                operation="screenshot",
                message="capture_failed",
                error=msg,
                device=resolved,
                path=None,
            )

        logger.info("Screenshot saved: %s", path)
        return ScreenshotResult(
            success=True,
            operation="screenshot",
            message="captured",
            device=resolved,
            path=path,
            details={"target": target},
        )

    def shutdown(
        self,
        device: str | None = None,
        *,
        all_devices: bool = False,
    ) -> SimulatorOperationResult:
        if all_devices:
            code, out, err = self._run(
                ["xcrun", "simctl", "shutdown", "all"],
                timeout=self.config.command_timeout_sec,
            )
            if code != 0:
                msg = (err or out).strip()
                return SimulatorOperationResult(
                    success=False,
                    operation="shutdown",
                    message="shutdown_all_failed",
                    error=msg,
                )
            logger.info("Shutdown all simulators")
            return SimulatorOperationResult(
                success=True,
                operation="shutdown",
                message="shutdown_all",
            )

        if not device:
            booted = self.list_booted()
            if not booted.devices:
                return SimulatorOperationResult(
                    success=True,
                    operation="shutdown",
                    message="no_booted_devices",
                )
            shutdown_errors: list[str] = []
            for d in booted.devices:
                res = self.shutdown(d.udid)
                if not res.success:
                    shutdown_errors.append(res.error or d.udid)
            if shutdown_errors:
                return SimulatorOperationResult(
                    success=False,
                    operation="shutdown",
                    message="partial_shutdown",
                    error="; ".join(shutdown_errors),
                    devices=booted.devices,
                )
            return SimulatorOperationResult(
                success=True,
                operation="shutdown",
                message="shutdown_booted",
                devices=booted.devices,
            )

        resolved = self.resolve_device(device, prefer_booted=True)
        udid = resolved.udid if resolved else device
        code, out, err = self._run(
            ["xcrun", "simctl", "shutdown", udid],
            timeout=self.config.command_timeout_sec,
        )
        if code != 0:
            msg = (err or out).strip()
            if "current state: Shutdown" in msg or "Unable to shutdown" in msg:
                logger.info("Simulator already shut down: %s", udid)
                return SimulatorOperationResult(
                    success=True,
                    operation="shutdown",
                    message="already_shutdown",
                    device=resolved,
                )
            return SimulatorOperationResult(
                success=False,
                operation="shutdown",
                message="shutdown_failed",
                error=msg,
                device=resolved,
            )
        logger.info("Shutdown simulator %s", udid)
        return SimulatorOperationResult(
            success=True,
            operation="shutdown",
            message="shutdown",
            device=resolved,
        )

    def _list_devices_json(
        self,
        *,
        operation: str,
        simctl_args: list[str],
        ios_only: bool,
    ) -> SimulatorOperationResult:
        code, out, err = self._run(
            ["xcrun", "simctl", *simctl_args],
            timeout=self.config.command_timeout_sec,
        )
        if code != 0:
            msg = (err or out).strip()
            logger.error("simctl list failed: %s", msg)
            return SimulatorOperationResult(
                success=False,
                operation=operation,
                message="list_failed",
                error=msg,
            )
        try:
            payload = json.loads(out)
        except json.JSONDecodeError as exc:
            return SimulatorOperationResult(
                success=False,
                operation=operation,
                message="parse_failed",
                error=str(exc),
            )

        devices: list[SimulatorDevice] = []
        for runtime, entries in (payload.get("devices") or {}).items():
            if ios_only and "ios" not in runtime.lower():
                continue
            for entry in entries:
                if not entry.get("isAvailable", True):
                    continue
                devices.append(
                    SimulatorDevice(
                        udid=entry["udid"],
                        name=entry.get("name", ""),
                        state=entry.get("state", "Unknown"),
                        runtime=runtime,
                        is_available=True,
                    )
                )

        logger.info("%s: %d device(s)", operation, len(devices))
        return SimulatorOperationResult(
            success=True,
            operation=operation,
            message="ok",
            devices=devices,
        )

    def _run(
        self,
        cmd: list[str],
        *,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        timeout = timeout or self.config.command_timeout_sec
        logger.debug("Running: %s", " ".join(cmd))
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
            logger.error("Command timed out after %ss: %s", timeout, cmd[0])
            return 124, "", f"timeout after {timeout}s"
        except OSError as exc:
            logger.error("Command failed: %s", exc)
            return 1, "", str(exc)
