"""
Deployment state tracking (Phase 15).

Persists on task.metadata[\"deployment\"] and mirrors task.status for delivery phases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from core.task_manager import TaskStatus


class DeploymentPhase(str, Enum):
    """Delivery lifecycle phases — aligned with TaskStatus deployment values."""

    CI_PENDING = "ci_pending"
    CI_RUNNING = "ci_running"
    CI_FAILED = "ci_failed"
    STAGING_DEPLOYING = "staging_deploying"
    STAGING_VERIFYING = "staging_verifying"
    STAGING_FAILED = "staging_failed"
    READY_FOR_PRODUCTION = "ready_for_production"
    PRODUCTION_PENDING_APPROVAL = "production_pending_approval"
    PRODUCTION_DEPLOYING = "production_deploying"
    PRODUCTION_VERIFYING = "production_verifying"
    DEPLOYED = "deployed"
    DEPLOYMENT_FAILED = "deployment_failed"
    ROLLED_BACK = "rolled_back"


# Map deployment phase → task status
PHASE_TO_STATUS: dict[DeploymentPhase, TaskStatus] = {
    DeploymentPhase.CI_PENDING: TaskStatus.CI_PENDING,
    DeploymentPhase.CI_RUNNING: TaskStatus.CI_RUNNING,
    DeploymentPhase.CI_FAILED: TaskStatus.CI_FAILED,
    DeploymentPhase.STAGING_DEPLOYING: TaskStatus.STAGING_DEPLOYING,
    DeploymentPhase.STAGING_VERIFYING: TaskStatus.STAGING_VERIFYING,
    DeploymentPhase.STAGING_FAILED: TaskStatus.STAGING_FAILED,
    DeploymentPhase.READY_FOR_PRODUCTION: TaskStatus.READY_FOR_PRODUCTION,
    DeploymentPhase.PRODUCTION_PENDING_APPROVAL: TaskStatus.PRODUCTION_PENDING_APPROVAL,
    DeploymentPhase.PRODUCTION_DEPLOYING: TaskStatus.PRODUCTION_DEPLOYING,
    DeploymentPhase.PRODUCTION_VERIFYING: TaskStatus.PRODUCTION_VERIFYING,
    DeploymentPhase.DEPLOYED: TaskStatus.DEPLOYED,
    DeploymentPhase.DEPLOYMENT_FAILED: TaskStatus.DEPLOYMENT_FAILED,
    DeploymentPhase.ROLLED_BACK: TaskStatus.ROLLED_BACK,
}

STATUS_TO_PHASE: dict[str, DeploymentPhase] = {
    s.value: p for p, s in PHASE_TO_STATUS.items()
}

DELIVERY_STATUSES = frozenset(PHASE_TO_STATUS.values())

TERMINAL_DELIVERY = frozenset({
    TaskStatus.CI_FAILED,
    TaskStatus.STAGING_FAILED,
    TaskStatus.DEPLOYED,
    TaskStatus.DEPLOYMENT_FAILED,
    TaskStatus.ROLLED_BACK,
})

IN_FLIGHT_DELIVERY = DELIVERY_STATUSES - TERMINAL_DELIVERY - frozenset({
    TaskStatus.READY_FOR_PRODUCTION,
    TaskStatus.PRODUCTION_PENDING_APPROVAL,
})


@dataclass
class CICheckSnapshot:
    build: str = "pending"
    test: str = "pending"
    lint: str = "pending"
    workflow: str = "pending"

    def to_dict(self) -> dict[str, str]:
        return {
            "build": self.build,
            "test": self.test,
            "lint": self.lint,
            "workflow": self.workflow,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CICheckSnapshot:
        if not data:
            return cls()
        return cls(
            build=data.get("build", "pending"),
            test=data.get("test", "pending"),
            lint=data.get("lint", "pending"),
            workflow=data.get("workflow", "pending"),
        )


@dataclass
class DeploymentState:
    """Durable deployment tracking for a task."""

    release_id: str
    task_id: str
    phase: str = DeploymentPhase.CI_PENDING.value
    commit_sha: str | None = None
    branch: str | None = None
    staging_url: str | None = None
    production_url: str | None = None
    deployment_risk: str = "medium"
    ci: CICheckSnapshot = field(default_factory=CICheckSnapshot)
    verification_report: dict[str, Any] = field(default_factory=dict)
    deployment_summary: str = ""
    health_status: str = "unknown"
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str | None = None
    diagnostics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "task_id": self.task_id,
            "phase": self.phase,
            "commit_sha": self.commit_sha,
            "branch": self.branch,
            "staging_url": self.staging_url,
            "production_url": self.production_url,
            "deployment_risk": self.deployment_risk,
            "ci": self.ci.to_dict(),
            "verification_report": self.verification_report,
            "deployment_summary": self.deployment_summary,
            "health_status": self.health_status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "diagnostics": self.diagnostics,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> DeploymentState | None:
        if not data:
            return None
        return cls(
            release_id=data.get("release_id", str(uuid4())),
            task_id=data.get("task_id", ""),
            phase=data.get("phase", DeploymentPhase.CI_PENDING.value),
            commit_sha=data.get("commit_sha"),
            branch=data.get("branch"),
            staging_url=data.get("staging_url"),
            production_url=data.get("production_url"),
            deployment_risk=data.get("deployment_risk", "medium"),
            ci=CICheckSnapshot.from_dict(data.get("ci")),
            verification_report=dict(data.get("verification_report", {})),
            deployment_summary=data.get("deployment_summary", ""),
            health_status=data.get("health_status", "unknown"),
            started_at=data.get("started_at", datetime.now(timezone.utc).isoformat()),
            updated_at=data.get("updated_at"),
            diagnostics=list(data.get("diagnostics", [])),
        )

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()

    @property
    def deployment_phase(self) -> DeploymentPhase:
        try:
            return DeploymentPhase(self.phase)
        except ValueError:
            return DeploymentPhase.CI_PENDING


def load_deployment_state(task) -> DeploymentState | None:
    return DeploymentState.from_dict(task.metadata.get("deployment"))


def save_deployment_state(task, state: DeploymentState) -> None:
    state.touch()
    task.metadata["deployment"] = state.to_dict()
    phase = state.deployment_phase
    if phase in PHASE_TO_STATUS:
        task.status = PHASE_TO_STATUS[phase]
