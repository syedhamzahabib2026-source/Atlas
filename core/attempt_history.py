"""
Attempt history tracking for adaptive recovery (Phase 5).

Atlas must know what was already tried before choosing a new strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AttemptRecord:
    """One execution or recovery attempt on a task."""

    attempt_number: int
    strategy: str
    failure_category: str
    outcome: str  # failed | success | investigating | cancelled
    error_summary: str = ""
    root_cause_hypothesis: str = ""
    timestamp: str = field(default_factory=lambda: _utc_now().isoformat())
    phase: str = "execution"  # execution | verification | recovery | investigation
    files_modified: list[str] = field(default_factory=list)
    browser_failures: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    network_failures: list[str] = field(default_factory=list)
    verification_outcome: str | None = None
    error_signature: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AttemptHistory:
    """Append-only attempt log stored on task.metadata['attempt_history']."""

    @staticmethod
    def load(task) -> list[AttemptRecord]:
        raw = task.metadata.get("attempt_history", [])
        records = []
        fields = AttemptRecord.__dataclass_fields__
        for item in raw:
            if isinstance(item, dict):
                records.append(AttemptRecord(**{k: v for k, v in item.items() if k in fields}))
        return records

    @staticmethod
    def save(task, records: list[AttemptRecord]) -> None:
        task.metadata["attempt_history"] = [r.to_dict() for r in records]

    @staticmethod
    def append(task, record: AttemptRecord) -> None:
        records = AttemptHistory.load(task)
        records.append(record)
        AttemptHistory.save(task, records)
        task.metadata["recovery_attempt_count"] = len(
            [r for r in records if r.outcome in ("failed", "investigating")]
        )

    @staticmethod
    def strategies_used(task, *, failed_only: bool = False) -> list[str]:
        records = AttemptHistory.load(task)
        if failed_only:
            records = [r for r in records if r.outcome == "failed"]
        return [r.strategy for r in records]

    @staticmethod
    def strategy_fail_count(task, strategy: str) -> int:
        return sum(
            1
            for r in AttemptHistory.load(task)
            if r.strategy == strategy and r.outcome == "failed"
        )

    @staticmethod
    def recent_error_signatures(task, n: int = 5) -> list[str]:
        records = AttemptHistory.load(task)[-n:]
        return [r.error_signature for r in records if r.error_signature]
