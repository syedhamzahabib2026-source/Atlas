"""
Deployment approval policy (Phase 15).

Maps deployment risk → governance requirements. Does not execute deployments.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.deployment_config import DeploymentConfig


class DeploymentRiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DeploymentGateMode(str, Enum):
    STAGING_AUTO_VERIFY = "staging_auto_verify"
    PRODUCTION_APPROVAL = "production_approval"
    EXPLICIT_DEPLOY_APPROVAL = "explicit_deploy_approval"
    MANUAL_ONLY = "manual_only"


@dataclass
class DeploymentGateRequirement:
    risk_level: DeploymentRiskLevel
    mode: DeploymentGateMode
    auto_staging_verify: bool
    allow_auto_production: bool
    requires_production_approval: bool
    block_auto_deploy: bool
    description: str = ""


_DEFAULT_GATES: dict[DeploymentRiskLevel, DeploymentGateRequirement] = {
    DeploymentRiskLevel.LOW: DeploymentGateRequirement(
        risk_level=DeploymentRiskLevel.LOW,
        mode=DeploymentGateMode.STAGING_AUTO_VERIFY,
        auto_staging_verify=True,
        allow_auto_production=False,
        requires_production_approval=True,
        block_auto_deploy=False,
        description="Staging auto-verify; production approval required",
    ),
    DeploymentRiskLevel.MEDIUM: DeploymentGateRequirement(
        risk_level=DeploymentRiskLevel.MEDIUM,
        mode=DeploymentGateMode.PRODUCTION_APPROVAL,
        auto_staging_verify=True,
        allow_auto_production=False,
        requires_production_approval=True,
        block_auto_deploy=False,
        description="Production approval required after staging",
    ),
    DeploymentRiskLevel.HIGH: DeploymentGateRequirement(
        risk_level=DeploymentRiskLevel.HIGH,
        mode=DeploymentGateMode.EXPLICIT_DEPLOY_APPROVAL,
        auto_staging_verify=True,
        allow_auto_production=False,
        requires_production_approval=True,
        block_auto_deploy=True,
        description="Explicit deployment approval — no auto production",
    ),
    DeploymentRiskLevel.CRITICAL: DeploymentGateRequirement(
        risk_level=DeploymentRiskLevel.CRITICAL,
        mode=DeploymentGateMode.MANUAL_ONLY,
        auto_staging_verify=False,
        allow_auto_production=False,
        requires_production_approval=True,
        block_auto_deploy=True,
        description="Manual deployment only — never auto-deploy",
    ),
}


class DeploymentPolicy:
    """Deployment governance rules."""

    def __init__(self, config: DeploymentConfig | None = None) -> None:
        self.config = config or DeploymentConfig()

    def requirement_for(self, risk: DeploymentRiskLevel) -> DeploymentGateRequirement:
        if not self.config.enabled:
            return DeploymentGateRequirement(
                risk_level=risk,
                mode=DeploymentGateMode.MANUAL_ONLY,
                auto_staging_verify=False,
                allow_auto_production=False,
                requires_production_approval=True,
                block_auto_deploy=True,
                description="Deployments disabled",
            )
        req = _DEFAULT_GATES[risk]
        if self.config.never_auto_deploy_critical and risk == DeploymentRiskLevel.CRITICAL:
            return req
        if not self.config.production_requires_approval and risk == DeploymentRiskLevel.LOW:
            return DeploymentGateRequirement(
                risk_level=risk,
                mode=DeploymentGateMode.STAGING_AUTO_VERIFY,
                auto_staging_verify=True,
                allow_auto_production=True,
                requires_production_approval=False,
                block_auto_deploy=False,
                description="Low risk — production may proceed after staging verify",
            )
        return req

    def can_auto_deploy_staging(self, risk: DeploymentRiskLevel) -> bool:
        req = self.requirement_for(risk)
        return req.auto_staging_verify and not req.block_auto_deploy

    def can_auto_deploy_production(self, risk: DeploymentRiskLevel) -> bool:
        if not self.config.enabled:
            return False
        req = self.requirement_for(risk)
        return req.allow_auto_production and not req.requires_production_approval

    def must_pause_for_production_approval(self, risk: DeploymentRiskLevel) -> bool:
        req = self.requirement_for(risk)
        return req.requires_production_approval or req.block_auto_deploy

    def is_manual_only(self, risk: DeploymentRiskLevel) -> bool:
        return self.requirement_for(risk).mode == DeploymentGateMode.MANUAL_ONLY
