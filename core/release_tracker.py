"""
Release history and rollback metadata (Phase 15).

Tracks staging/production releases, approvals, and health — stored on task metadata
and optional project-level release log in metadata[\"release_history\"].
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from core.deployment_state import DeploymentPhase


@dataclass
class ReleaseRecord:
    release_id: str
    task_id: str
    environment: str
    commit_sha: str | None = None
    branch: str | None = None
    phase: str = DeploymentPhase.CI_PENDING.value
    deployment_summary: str = ""
    verification_report: dict[str, Any] = field(default_factory=dict)
    health_status: str = "unknown"
    approval_decision: str | None = None
    approved_by: str | None = None
    rolled_back: bool = False
    rollback_reason: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "task_id": self.task_id,
            "environment": self.environment,
            "commit_sha": self.commit_sha,
            "branch": self.branch,
            "phase": self.phase,
            "deployment_summary": self.deployment_summary,
            "verification_report": self.verification_report,
            "health_status": self.health_status,
            "approval_decision": self.approval_decision,
            "approved_by": self.approved_by,
            "rolled_back": self.rolled_back,
            "rollback_reason": self.rollback_reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReleaseRecord:
        return cls(
            release_id=data.get("release_id", str(uuid4())),
            task_id=data.get("task_id", ""),
            environment=data.get("environment", "staging"),
            commit_sha=data.get("commit_sha"),
            branch=data.get("branch"),
            phase=data.get("phase", DeploymentPhase.CI_PENDING.value),
            deployment_summary=data.get("deployment_summary", ""),
            verification_report=dict(data.get("verification_report", {})),
            health_status=data.get("health_status", "unknown"),
            approval_decision=data.get("approval_decision"),
            approved_by=data.get("approved_by"),
            rolled_back=bool(data.get("rolled_back")),
            rollback_reason=data.get("rollback_reason"),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
        )


class ReleaseTracker:
    """
    Release history + rollback metadata.

    Does not execute deployments — records outcomes for governance and diagnostics.
    """

    def __init__(self) -> None:
        pass

    def create_release(
        self,
        task,
        *,
        commit_sha: str | None = None,
        branch: str | None = None,
        environment: str = "pipeline",
    ) -> ReleaseRecord:
        release_id = str(uuid4())
        record = ReleaseRecord(
            release_id=release_id,
            task_id=task.id,
            environment=environment,
            commit_sha=commit_sha,
            branch=branch,
        )
        history = task.metadata.setdefault("release_history", [])
        history.append(record.to_dict())
        task.metadata["current_release_id"] = release_id
        return record

    def update_release(self, task, record: ReleaseRecord) -> None:
        history = task.metadata.get("release_history", [])
        for i, entry in enumerate(history):
            if entry.get("release_id") == record.release_id:
                history[i] = record.to_dict()
                break
        else:
            history.append(record.to_dict())
        task.metadata["release_history"] = history

    def record_staging_release(
        self,
        task,
        *,
        url: str,
        summary: str,
        verification: dict[str, Any] | None = None,
    ) -> ReleaseRecord:
        record = ReleaseRecord(
            release_id=task.metadata.get("current_release_id", str(uuid4())),
            task_id=task.id,
            environment="staging",
            commit_sha=(task.metadata.get("deployment") or {}).get("commit_sha"),
            branch=(task.metadata.get("deployment") or {}).get("branch"),
            phase=DeploymentPhase.STAGING_VERIFYING.value,
            deployment_summary=summary,
            verification_report=verification or {},
            health_status="healthy" if (verification or {}).get("status") == "passed" else "checking",
        )
        staging = task.metadata.setdefault("staging_releases", [])
        staging.append(record.to_dict())
        self.update_release(task, record)
        return record

    def record_production_release(
        self,
        task,
        *,
        url: str,
        summary: str,
        approved_by: str | None = None,
    ) -> ReleaseRecord:
        record = ReleaseRecord(
            release_id=task.metadata.get("current_release_id", str(uuid4())),
            task_id=task.id,
            environment="production",
            commit_sha=(task.metadata.get("deployment") or {}).get("commit_sha"),
            phase=DeploymentPhase.DEPLOYED.value,
            deployment_summary=summary,
            health_status="healthy",
            approval_decision="approved",
            approved_by=approved_by,
        )
        prod = task.metadata.setdefault("production_releases", [])
        prod.append(record.to_dict())
        self.update_release(task, record)
        return record

    def record_rollback(
        self,
        task,
        *,
        reason: str,
        target: str = "staging",
        diagnostics: list[str] | None = None,
    ) -> ReleaseRecord:
        release_id = task.metadata.get("current_release_id", str(uuid4()))
        record = ReleaseRecord(
            release_id=release_id,
            task_id=task.id,
            environment=target,
            phase=DeploymentPhase.ROLLED_BACK.value,
            deployment_summary=f"Rolled back: {reason}",
            health_status="unhealthy",
            rolled_back=True,
            rollback_reason=reason,
        )
        rollbacks = task.metadata.setdefault("deployment_rollback_history", [])
        entry = record.to_dict()
        if diagnostics:
            entry["diagnostics"] = diagnostics
        rollbacks.append(entry)
        self.update_release(task, record)
        return record

    def get_release_history(self, task) -> list[ReleaseRecord]:
        return [ReleaseRecord.from_dict(r) for r in task.metadata.get("release_history", [])]

    def get_rollback_history(self, task) -> list[dict[str, Any]]:
        return list(task.metadata.get("deployment_rollback_history", []))

    def format_release_summary(self, task) -> str:
        dep = task.metadata.get("deployment", {})
        lines = [
            f"Release `{dep.get('release_id', '?')[:8]}`",
            f"Phase: `{dep.get('phase', '?')}`",
            f"Risk: `{dep.get('deployment_risk', '?')}`",
            f"Staging: `{dep.get('staging_url', 'n/a')}`",
            f"Health: `{dep.get('health_status', '?')}`",
        ]
        ci = dep.get("ci", {})
        if ci:
            lines.append(
                f"CI: build={ci.get('build')} test={ci.get('test')} lint={ci.get('lint')}"
            )
        return "\n".join(lines)
