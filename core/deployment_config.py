"""Deployment configuration (Phase 15) — no heavy imports."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DeploymentConfig:
    """YAML/env-driven delivery orchestration settings."""

    enabled: bool = True
    auto_start_after_merge: bool = True
    mock_ci: bool = True
    mock_deploy: bool = True
    staging_url_template: str = "https://staging.example.com"
    production_url_template: str = "https://app.example.com"
    staging_verify_enabled: bool = True
    production_requires_approval: bool = True
    never_auto_deploy_critical: bool = True
    ci_poll_interval_sec: float = 5.0
    health_check_paths: list[str] = field(default_factory=lambda: ["/", "/api/health"])
