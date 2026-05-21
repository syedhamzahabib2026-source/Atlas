"""Git safety configuration (avoids circular imports)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GitSafetyConfig:
    enabled: bool = True
    branch_prefix: str = "atlas/task"
    protected_branches: list[str] = field(
        default_factory=lambda: ["main", "master"]
    )
    block_work_on_protected: bool = True
    auto_checkpoint_before_retry: bool = True
    auto_isolate_branch: bool = True
    max_consecutive_rollbacks: int = 3
    max_dependency_modifications: int = 5
    health_regression_threshold: float = 15.0
    progressive_worsening_attempts: int = 3
    checkpoint_risky_strategies: list[str] = field(
        default_factory=lambda: [
            "minimal_patch",
            "architectural_fix",
            "dependency_repair",
            "rebuild_component",
            "rollback_and_reimplement",
            "initial",
        ]
    )
