"""
Risk classification for engineering changes (Phase 14).

Risk analysis only — no approval decisions or execution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.task_manager import Task
    from core.task_result import TaskResult


def _logger():
    from core.logger import get_logger
    return get_logger("risk")


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def score_weight(self) -> int:
        return {"low": 10, "medium": 35, "high": 65, "critical": 90}[self.value]


# Pattern buckets for classification
_LOW_PATTERNS = (
    r"\b(css|style|styling|typo|text|copy|label|tooltip|readme|comment)\b",
    r"\b(ui tweak|color|font|spacing|padding|margin)\b",
)
_MEDIUM_PATTERNS = (
    r"\b(refactor|component|route|endpoint|api|handler|middleware)\b",
    r"\b(page|view|layout|form|table|list)\b",
)
_HIGH_PATTERNS = (
    r"\b(auth|login|session|jwt|oauth|permission|rbac)\b",
    r"\b(dependency|dependencies|package\.json|requirements\.txt|pip install|npm install)\b",
    r"\b(state management|redux|zustand|context provider|store rewrite)\b",
    r"\b(infrastructure|terraform|kubernetes|docker|nginx)\b",
)
_CRITICAL_PATTERNS = (
    r"\b(deploy|deployment|production|release|migrate|migration|schema)\b",
    r"\b(secret|\.env|api[_-]?key|credential|password|token rotation)\b",
    r"\b(delete|remove all|drop table|truncate)\b",
)

_ARCHITECTURE_PATHS = (
    "orchestrator",
    "recovery_engine",
    "approval_engine",
    "git_manager",
    "pr_manager",
    "database",
    "schema",
    "migration",
    "auth",
    "security",
)

_DEPENDENCY_FILES = (
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "requirements.txt",
    "pyproject.toml",
    "poetry.lock",
    "Cargo.toml",
    "go.mod",
)


@dataclass
class RiskAssessment:
    """Structured risk output stored on task metadata."""

    risk_score: int
    risk_level: RiskLevel
    risk_reasons: list[str] = field(default_factory=list)
    dependency_change: bool = False
    deployment_change: bool = False
    recovery_escalation: bool = False
    architecture_sensitive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_score": self.risk_score,
            "risk_level": self.risk_level.value,
            "risk_reasons": self.risk_reasons,
            "dependency_change": self.dependency_change,
            "deployment_change": self.deployment_change,
            "recovery_escalation": self.recovery_escalation,
            "architecture_sensitive": self.architecture_sensitive,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RiskAssessment | None:
        if not data:
            return None
        try:
            level = RiskLevel(data.get("risk_level", "low"))
        except ValueError:
            level = RiskLevel.LOW
        return cls(
            risk_score=int(data.get("risk_score", 10)),
            risk_level=level,
            risk_reasons=list(data.get("risk_reasons", [])),
            dependency_change=bool(data.get("dependency_change")),
            deployment_change=bool(data.get("deployment_change")),
            recovery_escalation=bool(data.get("recovery_escalation")),
            architecture_sensitive=bool(data.get("architecture_sensitive")),
        )


class RiskClassifier:
    """
    Classifies engineering risk from task intent, git metadata, and recovery history.
    """

    def classify_task(
        self,
        task: Task,
        *,
        result: TaskResult | None = None,
        phase: str = "pre_execution",
    ) -> RiskAssessment:
        """Classify risk for a task at a given lifecycle phase."""
        text = self._task_text(task)
        reasons: list[str] = []
        level = RiskLevel.LOW
        score = 10

        def bump(new_level: RiskLevel, reason: str, points: int) -> None:
            nonlocal level, score
            reasons.append(reason)
            if new_level.score_weight > level.score_weight:
                level = new_level
            score = max(score, new_level.score_weight + min(points, 9))

        for pat in _CRITICAL_PATTERNS:
            if re.search(pat, text, re.I):
                bump(RiskLevel.CRITICAL, f"Critical pattern: {pat}", 5)
        for pat in _HIGH_PATTERNS:
            if re.search(pat, text, re.I):
                bump(RiskLevel.HIGH, f"High-risk pattern: {pat}", 4)
        for pat in _MEDIUM_PATTERNS:
            if re.search(pat, text, re.I):
                bump(RiskLevel.MEDIUM, f"Medium-risk pattern: {pat}", 3)
        for pat in _LOW_PATTERNS:
            if re.search(pat, text, re.I):
                if level == RiskLevel.LOW:
                    reasons.append(f"Low-risk signal: {pat}")

        git_meta = task.metadata.get("git", {})
        dep_change = self._detect_dependency_change(task, git_meta)
        if dep_change:
            bump(RiskLevel.HIGH, "Dependency or lockfile modification detected", 6)

        deploy = bool(task.metadata.get("deployment") or task.metadata.get("deploy"))
        if deploy or re.search(r"\b(deploy|production)\b", text, re.I):
            bump(RiskLevel.CRITICAL, "Deployment-related change", 8)
            deploy = True

        arch = self._architecture_sensitive(git_meta, task)
        if arch:
            bump(RiskLevel.HIGH, "Architecture-sensitive files changed", 5)

        recovery_esc = self._recovery_escalation_signals(task)
        if recovery_esc:
            bump(
                RiskLevel.HIGH,
                "Recovery escalation: excessive retries, rollbacks, or contradictions",
                7,
            )

        if phase == "post_execution" and result:
            if not result.success:
                bump(RiskLevel.MEDIUM, "Task did not complete cleanly before approval", 2)

        if not reasons:
            reasons.append("Default low-risk classification")

        assessment = RiskAssessment(
            risk_score=min(100, score),
            risk_level=level,
            risk_reasons=reasons[:12],
            dependency_change=dep_change,
            deployment_change=deploy,
            recovery_escalation=recovery_esc,
            architecture_sensitive=arch,
        )
        _logger().debug(
            "Risk classified task %s: %s (score=%s)",
            task.id[:8],
            level.value,
            assessment.risk_score,
        )
        return assessment

    def classify_deployment_risk(self, task: Task) -> str:
        """
        Deployment-specific risk for delivery gates (Phase 15).

        Returns DeploymentRiskLevel value string — used by DeploymentPolicy.
        """
        from core.deployment_policy import DeploymentRiskLevel

        eng = (task.metadata.get("risk") or {}).get("risk_level", "medium")
        text = self._task_text(task)

        if any(
            k in text
            for k in ("migration", "schema", "payment", "billing", "production config")
        ):
            return DeploymentRiskLevel.CRITICAL.value
        if eng == "critical" or ("deploy" in text and "production" in text):
            return DeploymentRiskLevel.CRITICAL.value
        if eng == "high" or any(
            k in text for k in ("auth", "dependency", "infrastructure", "kubernetes")
        ):
            return DeploymentRiskLevel.HIGH.value
        if eng == "low" or any(k in text for k in ("css", "text", "typo", "style")):
            return DeploymentRiskLevel.LOW.value
        return DeploymentRiskLevel.MEDIUM.value

    def _task_text(self, task: Task) -> str:
        parts = [
            task.title,
            task.description,
            str(task.metadata.get("prompt", "")),
            str(task.metadata.get("change_summary", "")),
        ]
        return " ".join(p for p in parts if p).lower()

    def _detect_dependency_change(self, task: Task, git_meta: dict) -> bool:
        if task.metadata.get("dependency_modifications"):
            return True
        changed = git_meta.get("files_changed") or git_meta.get("changed_files") or []
        for path in changed:
            name = str(path).replace("\\", "/").split("/")[-1].lower()
            if name in _DEPENDENCY_FILES:
                return True
        dep_count = git_meta.get("dependency_modification_count", 0)
        return int(dep_count) > 0

    def _architecture_sensitive(self, git_meta: dict, task: Task) -> bool:
        changed = git_meta.get("files_changed") or git_meta.get("changed_files") or []
        for path in changed:
            low = str(path).replace("\\", "/").lower()
            if any(seg in low for seg in _ARCHITECTURE_PATHS):
                return True
        if task.metadata.get("architecture_sensitive"):
            return True
        return False

    def _recovery_escalation_signals(self, task: Task) -> bool:
        attempts = task.metadata.get("attempt_history") or []
        if len(attempts) >= 6:
            return True
        git_meta = task.metadata.get("git", {})
        rollbacks = git_meta.get("rollback_history") or []
        if len(rollbacks) >= 3:
            return True
        if task.metadata.get("contradiction_detected"):
            return True
        esc = task.metadata.get("escalation_summary")
        if esc:
            return True
        return False
