"""
Approval policy — maps risk levels to governance requirements (Phase 14).

Governance only; no risk analysis or execution logic here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.risk_classifier import RiskLevel


class ApprovalMode(str, Enum):
    """How a task may proceed after risk assessment."""

    AUTO = "auto"
    SLACK_CONFIRM = "slack_confirm"
    EXPLICIT = "explicit"
    MANUAL_ONLY = "manual_only"


@dataclass
class ApprovalRequirement:
    """What human oversight is required for a given risk level."""

    risk_level: RiskLevel
    mode: ApprovalMode
    allow_auto_execution: bool
    allow_auto_pr: bool
    requires_slack: bool
    requires_explicit_approval: bool
    block_automatic_execution: bool
    description: str = ""


from core.approval_config import ApprovalConfig  # noqa: F401 — re-export

# Default policy table — risk → requirement
_DEFAULT_REQUIREMENTS: dict[RiskLevel, ApprovalRequirement] = {
    RiskLevel.LOW: ApprovalRequirement(
        risk_level=RiskLevel.LOW,
        mode=ApprovalMode.AUTO,
        allow_auto_execution=True,
        allow_auto_pr=True,
        requires_slack=False,
        requires_explicit_approval=False,
        block_automatic_execution=False,
        description="Auto-approved — low-risk changes",
    ),
    RiskLevel.MEDIUM: ApprovalRequirement(
        risk_level=RiskLevel.MEDIUM,
        mode=ApprovalMode.SLACK_CONFIRM,
        allow_auto_execution=True,
        allow_auto_pr=False,
        requires_slack=True,
        requires_explicit_approval=False,
        block_automatic_execution=False,
        description="Slack confirmation required before PR",
    ),
    RiskLevel.HIGH: ApprovalRequirement(
        risk_level=RiskLevel.HIGH,
        mode=ApprovalMode.EXPLICIT,
        allow_auto_execution=False,
        allow_auto_pr=False,
        requires_slack=True,
        requires_explicit_approval=True,
        block_automatic_execution=False,
        description="Explicit human approval required",
    ),
    RiskLevel.CRITICAL: ApprovalRequirement(
        risk_level=RiskLevel.CRITICAL,
        mode=ApprovalMode.MANUAL_ONLY,
        allow_auto_execution=False,
        allow_auto_pr=False,
        requires_slack=True,
        requires_explicit_approval=True,
        block_automatic_execution=True,
        description="Manual review mandatory — no automatic execution",
    ),
}


class ApprovalPolicy:
    """
    Maps classified risk to approval requirements.

    Config flags can tighten policy (e.g. force approval for dependencies).
    """

    def __init__(self, config: ApprovalConfig | None = None) -> None:
        self.config = config or ApprovalConfig()

    def requirement_for(self, risk_level: RiskLevel) -> ApprovalRequirement:
        """Return base requirement for a risk level."""
        base = _DEFAULT_REQUIREMENTS[risk_level]
        if not self.config.enabled:
            return ApprovalRequirement(
                risk_level=risk_level,
                mode=ApprovalMode.AUTO,
                allow_auto_execution=True,
                allow_auto_pr=True,
                requires_slack=False,
                requires_explicit_approval=False,
                block_automatic_execution=False,
                description="Approvals disabled — auto proceed",
            )
        if risk_level == RiskLevel.LOW and not self.config.auto_approve_low_risk:
            return _DEFAULT_REQUIREMENTS[RiskLevel.MEDIUM]
        return base

    def effective_requirement(
        self,
        risk_level: RiskLevel,
        *,
        dependency_change: bool = False,
        deployment_change: bool = False,
        recovery_escalation: bool = False,
    ) -> ApprovalRequirement:
        """
        Apply config overrides (dependencies, deployments, recovery escalation).
        """
        req = self.requirement_for(risk_level)
        if not self.config.enabled:
            return req

        level = risk_level
        if recovery_escalation and level.score_weight < RiskLevel.HIGH.score_weight:
            level = RiskLevel.HIGH
        if self.config.require_approval_for_dependencies and dependency_change:
            if level.score_weight < RiskLevel.HIGH.score_weight:
                level = RiskLevel.HIGH
        if self.config.require_approval_for_deployments and deployment_change:
            level = RiskLevel.CRITICAL

        req = self.requirement_for(level)
        if self.config.require_approval_high_risk and level in (
            RiskLevel.HIGH,
            RiskLevel.CRITICAL,
        ):
            return req
        return req

    def should_pause_before_execution(self, requirement: ApprovalRequirement) -> bool:
        return requirement.block_automatic_execution

    def should_pause_before_pr(self, requirement: ApprovalRequirement) -> bool:
        return not requirement.allow_auto_pr or requirement.requires_explicit_approval

    def can_auto_proceed(self, requirement: ApprovalRequirement) -> bool:
        return requirement.mode == ApprovalMode.AUTO and requirement.allow_auto_execution
