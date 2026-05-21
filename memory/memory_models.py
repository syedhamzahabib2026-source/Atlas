"""
Structured operational memory models (Phase 7).

Engineering intelligence — not chat logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any
import uuid


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class MemoryCategory(str, Enum):
    PROJECT = "project"
    RECOVERY = "recovery"
    REPOSITORY = "repository"
    TASK = "task"
    SYSTEM = "system"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class OperationalMemoryRecord:
    """Distilled operational fact stored in SQLite."""

    id: str
    project_id: str
    category: str
    subcategory: str
    title: str
    summary: str
    tags: list[str] = field(default_factory=list)
    signal_score: int = 50
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["updated_at"] = self.updated_at.isoformat() if self.updated_at else None
        return d


@dataclass
class ProjectMemory:
    """Architecture and stack knowledge."""

    project_id: str
    architecture_notes: list[str] = field(default_factory=list)
    stack_info: dict[str, str] = field(default_factory=dict)
    services: list[str] = field(default_factory=list)
    auth_flow_notes: str = ""
    database_notes: str = ""
    deployment_notes: str = ""


@dataclass
class RecoveryMemory:
    """What worked and what failed in recovery."""

    project_id: str
    successful_strategies: list[str] = field(default_factory=list)
    failed_strategies: list[str] = field(default_factory=list)
    recurring_signatures: list[str] = field(default_factory=list)
    rollback_frequency: int = 0


@dataclass
class RepositoryMemory:
    """Repo-specific risks."""

    project_id: str
    fragile_files: list[str] = field(default_factory=list)
    dangerous_directories: list[str] = field(default_factory=list)
    dependency_risks: list[str] = field(default_factory=list)
    high_regression_zones: list[str] = field(default_factory=list)
    critical_workflows: list[str] = field(default_factory=list)


@dataclass
class TaskMemory:
    """Task-linked operational history."""

    task_id: str
    project_id: str
    status: str
    strategy: str = ""
    failure_category: str = ""
    verification_status: str = ""
    health_score: float = 0.0
    regression: bool = False
    summary: str = ""


@dataclass
class StrategyStats:
    """Aggregated strategy performance."""

    strategy: str
    category: str
    project_id: str | None
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    rollbacks: int = 0

    @property
    def success_rate(self) -> float:
        if self.attempts == 0:
            return 0.0
        return self.successes / self.attempts


@dataclass
class RiskZone:
    """File/folder/service risk mapping."""

    project_id: str
    path: str
    zone_type: str  # file | folder | service | workflow
    risk_score: float
    failure_count: int = 0
    rollback_count: int = 0
    regression_count: int = 0

    @property
    def risk_level(self) -> RiskLevel:
        if self.risk_score >= 75:
            return RiskLevel.CRITICAL
        if self.risk_score >= 50:
            return RiskLevel.HIGH
        if self.risk_score >= 25:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW


@dataclass
class FailurePattern:
    """Recurring failure signature."""

    project_id: str
    signature: str
    category: str
    occurrence_count: int = 1
    last_seen: datetime = field(default_factory=_utc_now)


@dataclass
class OperationalContext:
    """Concise context injected before task execution."""

    project_id: str
    project_name: str
    relevant_history: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    suggested_strategies: list[str] = field(default_factory=list)
    high_risk_areas: list[str] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        lines = [
            "# Atlas operational context",
            f"Project: {self.project_name}",
            "",
            "## Relevant history",
        ]
        if self.relevant_history:
            lines.extend(f"- {h}" for h in self.relevant_history[:8])
        else:
            lines.append("- (no prior operational memory for this scope)")
        if self.high_risk_areas:
            lines.extend(["", "## High-risk areas"])
            lines.extend(f"- {r}" for r in self.high_risk_areas[:6])
        if self.cautions:
            lines.extend(["", "## Suggested caution"])
            lines.extend(f"- {c}" for c in self.cautions[:5])
        if self.suggested_strategies:
            lines.extend(["", "## Strategies that worked before"])
            lines.extend(f"- {s}" for s in self.suggested_strategies[:4])
        lines.append("")
        return "\n".join(lines)
