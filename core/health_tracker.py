"""
System health tracking and regression detection (Phase 6).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.task_manager import Task
    from core.task_result import TaskResult


@dataclass
class HealthSnapshot:
    score: float = 50.0
    verification_passed: bool = False
    console_error_count: int = 0
    network_failure_count: int = 0
    browser_failed: bool = False
    build_failed: bool = False
    error_count: int = 0
    screenshot_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HealthReport:
    current: HealthSnapshot
    previous: HealthSnapshot | None = None
    regression_detected: bool = False
    regression_reasons: list[str] = field(default_factory=list)
    trend: str = "stable"  # improving | stable | worsening
    score_delta: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current.to_dict(),
            "previous": self.previous.to_dict() if self.previous else None,
            "regression_detected": self.regression_detected,
            "regression_reasons": self.regression_reasons,
            "trend": self.trend,
            "score_delta": self.score_delta,
        }


class HealthTracker:
    """Compare verification outcomes across attempts."""

    def __init__(self, regression_threshold: float = 15.0) -> None:
        self.regression_threshold = regression_threshold

    def build_snapshot(self, task: Task, result: TaskResult) -> HealthSnapshot:
        snap = HealthSnapshot()
        browser = (result.metadata or {}).get("browser", {})
        verify = task.metadata.get("verification", {})

        if result.success:
            snap.score += 25
        if verify.get("status") == "passed":
            snap.verification_passed = True
            snap.score += 25
        elif verify.get("status") == "failed":
            snap.browser_failed = True
            snap.score -= 20

        if browser:
            if not browser.get("success", True):
                snap.browser_failed = True
                snap.score -= 15
            snap.console_error_count = len(
                [l for l in browser.get("console_logs", []) if "[error]" in l.lower()]
            )
            snap.network_failure_count = len(browser.get("network_failures", []))
            snap.screenshot_count = len(browser.get("screenshots", []))
            snap.score -= min(20, snap.console_error_count * 5)
            snap.score -= min(15, snap.network_failure_count * 5)

        snap.error_count = len(result.errors)
        snap.score -= min(25, snap.error_count * 8)

        text = (result.summary + " ".join(result.errors)).lower()
        if any(m in text for m in ("build failed", "compilation", "syntaxerror", "npm err")):
            snap.build_failed = True
            snap.score -= 30

        snap.score = max(0.0, min(100.0, snap.score))
        return snap

    def analyze(self, task: Task, result: TaskResult) -> HealthReport:
        current = self.build_snapshot(task, result)
        history: list[dict] = task.metadata.get("health_history", [])
        previous = None
        if history:
            previous = HealthSnapshot(**history[-1].get("current", history[-1]))

        report = HealthReport(current=current, previous=previous)
        reasons: list[str] = []

        if previous:
            report.score_delta = current.score - previous.score
            if report.score_delta <= -self.regression_threshold:
                report.regression_detected = True
                reasons.append(
                    f"Health score dropped {abs(report.score_delta):.0f} points "
                    f"({previous.score:.0f} → {current.score:.0f})"
                )
            if current.console_error_count > previous.console_error_count:
                report.regression_detected = True
                reasons.append(
                    f"Console errors increased ({previous.console_error_count} → "
                    f"{current.console_error_count})"
                )
            if current.network_failure_count > previous.network_failure_count:
                report.regression_detected = True
                reasons.append("Network failures increased")
            if previous.verification_passed and not current.verification_passed:
                report.regression_detected = True
                reasons.append("Verification regressed from pass to fail")
            if not previous.browser_failed and current.browser_failed:
                report.regression_detected = True
                reasons.append("Browser verification newly failing")

            if report.score_delta > 5:
                report.trend = "improving"
            elif report.score_delta < -5:
                report.trend = "worsening"
            else:
                report.trend = "stable"
        else:
            if current.score < 40 or current.build_failed:
                report.regression_detected = True
                reasons.append("Initial health critically low")

        report.regression_reasons = reasons
        return report

    def record(self, task: Task, report: HealthReport) -> None:
        history = task.metadata.setdefault("health_history", [])
        history.append(report.to_dict())
        task.metadata["health"] = report.to_dict()
        task.metadata["health_score"] = report.current.score

    def progressive_worsening(self, task: Task, n: int = 3) -> bool:
        """True if last N snapshots show declining scores."""
        history = task.metadata.get("health_history", [])
        if len(history) < n:
            return False
        scores = [h.get("current", h).get("score", 50) for h in history[-n:]]
        return all(scores[i] > scores[i + 1] for i in range(len(scores) - 1))
