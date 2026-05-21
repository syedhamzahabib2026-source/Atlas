"""
Data models for Atlas memory.

Lightweight dataclasses; SQLite rows map to these in memory.store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ProjectRecord:
    """A project Atlas is working on."""

    id: str
    name: str
    path: str
    created_at: datetime = field(default_factory=_utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryEntry:
    """A single memory fact, plan chunk, or observation."""

    id: str
    project_id: str
    kind: str  # e.g. "plan", "fact", "observation", "decision"
    content: str
    created_at: datetime = field(default_factory=_utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)
