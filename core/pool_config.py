"""
Worker pool configuration — types, capabilities, and isolation settings (Phase 13).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PoolType(str, Enum):
    SUBSCRIPTION = "subscription"
    API = "api"
    LOCAL = "local"


class AuthMode(str, Enum):
    """How Claude Code authenticates in this pool."""

    SUBSCRIPTION = "subscription"
    API_KEY = "api_key"
    LOCAL = "local"


class CostTier(str, Enum):
    FREE = "free"
    METERED = "metered"
    CHEAP = "cheap"


class WorkerCapability(str, Enum):
    """Capabilities exposed by workers for routing."""

    DEEP_REASONING = "deep_reasoning"
    BROWSER_VERIFICATION = "browser_verification"
    CHEAP_SUMMARIZATION = "cheap_summarization"
    RECOVERY_ANALYSIS = "recovery_analysis"
    REPO_ANALYSIS = "repo_analysis"


# Default capabilities for Claude Code pools
CLAUDE_POOL_CAPABILITIES = [
    WorkerCapability.DEEP_REASONING,
    WorkerCapability.RECOVERY_ANALYSIS,
    WorkerCapability.REPO_ANALYSIS,
]


@dataclass
class PoolConfig:
    """Static configuration for one execution pool."""

    pool_id: str
    pool_type: PoolType
    session_prefix: str
    auth_mode: AuthMode
    cost_tier: CostTier
    max_workers: int = 2
    enabled: bool = True
    priority: int = 100
    capabilities: list[WorkerCapability] = field(default_factory=list)
    api_key_env_var: str = "ANTHROPIC_API_KEY"
    cooldown_sec: float = 600.0
    description: str = ""
    # CLI the agent launches in this pool's sessions. Pools own their runtime
    # (e.g. LocalPool -> an ollama-backed CLI); agents must stay provider-blind.
    launch_command: str = "claude"
    # "tmux" or "headless" — how this pool's tasks execute (agents obey).
    execution_mode: str = "tmux"
    # Model hint for executors (e.g. an Ollama model tag for LocalPool).
    model: str = ""

    def __post_init__(self) -> None:
        if not self.capabilities:
            if self.pool_type == PoolType.LOCAL:
                self.capabilities = [WorkerCapability.CHEAP_SUMMARIZATION]
            else:
                self.capabilities = list(CLAUDE_POOL_CAPABILITIES)


@dataclass
class WorkerPoolsConfig:
    """Top-level worker pool settings."""

    enabled: bool = True
    subscription: PoolConfig = field(
        default_factory=lambda: PoolConfig(
            pool_id="subscription",
            pool_type=PoolType.SUBSCRIPTION,
            session_prefix="atlas-sub",
            auth_mode=AuthMode.SUBSCRIPTION,
            cost_tier=CostTier.FREE,
            priority=10,
            description="Claude subscription login (no API key)",
        )
    )
    api: PoolConfig = field(
        default_factory=lambda: PoolConfig(
            pool_id="api",
            pool_type=PoolType.API,
            session_prefix="atlas-api",
            auth_mode=AuthMode.API_KEY,
            cost_tier=CostTier.METERED,
            priority=20,
            description="Claude API key (token-metered fallback)",
        )
    )
    local: PoolConfig = field(
        default_factory=lambda: PoolConfig(
            pool_id="local",
            pool_type=PoolType.LOCAL,
            session_prefix="atlas-local",
            auth_mode=AuthMode.LOCAL,
            cost_tier=CostTier.CHEAP,
            priority=30,
            enabled=False,
            max_workers=0,
            description="Future local/cheap workers (placeholder)",
        )
    )

    def all_pools(self) -> list[PoolConfig]:
        return [self.subscription, self.api, self.local]

    def by_id(self, pool_id: str) -> PoolConfig | None:
        for p in self.all_pools():
            if p.pool_id == pool_id:
                return p
        return None


def pools_config_from_yaml(data: dict[str, Any] | None) -> WorkerPoolsConfig:
    """Build WorkerPoolsConfig from YAML worker_pools section."""
    if not data:
        return WorkerPoolsConfig()

    def _pool(key: str, defaults: PoolConfig) -> PoolConfig:
        raw = data.get(key) or {}
        caps_raw = raw.get("capabilities") or []
        caps = [
            WorkerCapability(c) if isinstance(c, str) else c
            for c in caps_raw
        ]
        return PoolConfig(
            pool_id=raw.get("pool_id", defaults.pool_id),
            pool_type=PoolType(raw.get("pool_type", defaults.pool_type.value)),
            session_prefix=raw.get("session_prefix", defaults.session_prefix),
            auth_mode=AuthMode(raw.get("auth_mode", defaults.auth_mode.value)),
            cost_tier=CostTier(raw.get("cost_tier", defaults.cost_tier.value)),
            max_workers=int(raw.get("max_workers", defaults.max_workers)),
            enabled=bool(raw.get("enabled", defaults.enabled)),
            priority=int(raw.get("priority", defaults.priority)),
            capabilities=caps or defaults.capabilities,
            api_key_env_var=raw.get("api_key_env_var", defaults.api_key_env_var),
            cooldown_sec=float(raw.get("cooldown_sec", defaults.cooldown_sec)),
            description=raw.get("description", defaults.description),
            launch_command=raw.get("launch_command", defaults.launch_command),
            execution_mode=raw.get("execution_mode", defaults.execution_mode),
            model=raw.get("model", defaults.model),
        )

    base = WorkerPoolsConfig()
    return WorkerPoolsConfig(
        enabled=bool(data.get("enabled", True)),
        subscription=_pool("subscription", base.subscription),
        api=_pool("api", base.api),
        local=_pool("local", base.local),
    )
