"""
Structured results from browser verification tasks (Phase 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BrowserTaskResult:
    """Outcome of a Playwright browser verification run."""

    success: bool
    summary: str
    started_at: datetime = field(default_factory=_utc_now)
    finished_at: datetime | None = None
    screenshots: list[str] = field(default_factory=list)
    console_logs: list[str] = field(default_factory=list)
    network_failures: list[str] = field(default_factory=list)
    dom_summary: str = ""
    errors: list[str] = field(default_factory=list)
    steps_run: int = 0
    final_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def finish(self) -> None:
        self.finished_at = _utc_now()

    @property
    def duration_sec(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "summary": self.summary,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_sec": self.duration_sec,
            "screenshots": self.screenshots,
            "console_logs": self.console_logs[-50:],
            "network_failures": self.network_failures,
            "dom_summary": self.dom_summary,
            "errors": self.errors,
            "steps_run": self.steps_run,
            "final_url": self.final_url,
            "metadata": self.metadata,
        }
