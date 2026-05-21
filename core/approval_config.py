"""Approval configuration dataclass (Phase 14) — no policy/risk imports."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ApprovalConfig:
    """YAML/env-driven approval governance settings."""

    enabled: bool = True
    auto_approve_low_risk: bool = True
    require_approval_high_risk: bool = True
    require_approval_for_dependencies: bool = True
    require_approval_for_deployments: bool = True
    escalate_on_high_rollback_count: int = 3
    escalate_on_excessive_retries: int = 6
