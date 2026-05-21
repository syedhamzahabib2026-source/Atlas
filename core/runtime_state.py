"""
Runtime state snapshot — survives restarts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RuntimeState:
    """Last known Atlas operational snapshot."""

    atlas_pid: int | None = None
    started_at: str = field(default_factory=_utc_now)
    last_shutdown_at: str | None = None
    last_shutdown_graceful: bool = False
    orchestrator_running: bool = False
    active_task_ids: list[str] = field(default_factory=list)
    project_queues: dict[str, list[str]] = field(default_factory=dict)
    tmux_sessions_mapped: dict[str, str] = field(default_factory=dict)  # session -> task_id
    recovery_chains_active: list[str] = field(default_factory=list)
    resource_usage: dict[str, int] = field(default_factory=dict)
    integrity_issues: list[str] = field(default_factory=list)
    boot_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuntimeState:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


class RuntimeStateStore:
    """Persist runtime state to JSON file (simple, human-inspectable)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> RuntimeState | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return RuntimeState.from_dict(data)
        except Exception:
            return None

    def save(self, state: RuntimeState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state.metadata["saved_at"] = _utc_now()
        self.path.write_text(
            json.dumps(state.to_dict(), indent=2),
            encoding="utf-8",
        )

    def mark_shutdown(self, state: RuntimeState, *, graceful: bool) -> None:
        state.last_shutdown_at = _utc_now()
        state.last_shutdown_graceful = graceful
        state.orchestrator_running = False
        self.save(state)
