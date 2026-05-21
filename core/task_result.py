"""
Structured results from agent / terminal execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ResultStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TaskResult:
    """Outcome of a single agent run (e.g. Claude Code in tmux)."""

    status: ResultStatus
    summary: str
    session_name: str
    started_at: datetime = field(default_factory=_utc_now)
    finished_at: datetime | None = None
    raw_output: str = ""
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def finish(self) -> None:
        self.finished_at = _utc_now()

    @property
    def success(self) -> bool:
        return self.status == ResultStatus.COMPLETED

    @property
    def duration_sec(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "summary": self.summary,
            "session_name": self.session_name,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "raw_output": self.raw_output,
            "errors": self.errors,
            "duration_sec": self.duration_sec,
            "metadata": self.metadata,
        }
