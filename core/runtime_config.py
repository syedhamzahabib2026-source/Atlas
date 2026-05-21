"""Persistent runtime configuration (Phase 8)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RuntimeConfig:
    enabled: bool = True
    state_path: str = "logs/runtime_state.json"
    stale_task_hours: int = 72
    max_runaway_retries: int = 15
    max_browser_concurrent: int = 2
    max_claude_concurrent: int = 2
    per_project_max_concurrent: int = 2
    snapshot_interval_ticks: int = 12
    orphan_session_cleanup: bool = True
