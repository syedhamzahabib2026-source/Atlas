"""Recovery engine configuration (avoids circular imports)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RecoveryConfig:
    max_recovery_attempts: int = 5
    max_same_strategy_failures: int = 2
    investigate_after_failures: int = 2
    escalate_after_attempts: int = 6
    low_confidence_escalate: bool = True
