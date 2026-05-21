"""
Configuration loader for Atlas.

Merges YAML defaults with environment variable overrides.
Secrets must come from the environment, never from YAML.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from core.git_config import GitSafetyConfig
from core.memory_config import MemoryConfig
from core.recovery_config import RecoveryConfig
from core.runtime_config import RuntimeConfig


def _env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return int(raw)


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


@dataclass
class BrowserConfig:
    """Playwright browser verification settings."""

    headless: bool = True
    timeout_ms: int = 30_000
    max_runtime_sec: float = 120.0
    capture_success_screenshot: bool = True
    capture_failure_screenshot: bool = True
    viewport_width: int = 1280
    viewport_height: int = 720


@dataclass
class ClaudeCodeConfig:
    """Claude Code tmux execution settings."""

    command: str = "claude"
    max_execution_sec: float = 3600.0
    poll_interval_sec: float = 2.0
    launch_wait_sec: float = 15.0
    idle_stable_polls: int = 3
    capture_lines: int = 200
    kill_session_on_finish: bool = False


@dataclass
class ProjectConfig:
    """A known project repo that Atlas can target via --project <name>."""

    repo_path: str
    description: str = ""


def _env_list(key: str, default: list[str] | None = None) -> list[str]:
    raw = os.environ.get(key)
    if not raw:
        return default or []
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class SlackConfig:
    """Slack integration settings (tokens from env only)."""

    enabled: bool = False
    bot_token: str | None = None
    app_token: str | None = None
    channel_id: str | None = None
    allowed_user_ids: list[str] = field(default_factory=list)
    allowed_channel_ids: list[str] = field(default_factory=list)
    log_tail_lines: int = 40
    stop_kill_session_default: bool = False


@dataclass
class AtlasConfig:
    """Runtime configuration for the Atlas orchestrator."""

    root_dir: Path
    config_path: Path
    log_level: str = "INFO"
    log_dir: Path = field(default_factory=lambda: Path("logs"))
    projects_dir: Path = field(default_factory=lambda: Path("projects"))
    poll_interval_sec: float = 5.0
    max_concurrent_tasks: int = 3
    memory_db_path: Path = field(default_factory=lambda: Path("memory/atlas.db"))
    tmux_session_prefix: str = "atlas"
    tmux_socket: str | None = None
    claude_code: ClaudeCodeConfig = field(default_factory=ClaudeCodeConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    screenshots_dir: Path = field(default_factory=lambda: Path("logs/screenshots"))
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    git: GitSafetyConfig = field(default_factory=GitSafetyConfig)
    memory_config: MemoryConfig = field(default_factory=MemoryConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    scanner_interval_hours: float = 6.0
    scanner_enabled: bool = True
    _yaml: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def slack_ready(self) -> bool:
        """True when Slack tokens are present and integration is enabled."""
        s = self.slack
        return bool(
            s.enabled
            and s.bot_token
            and s.app_token
            and s.channel_id
        )


def load_config(
    root_dir: Path | None = None,
    config_path: Path | None = None,
    dotenv_path: Path | None = None,
) -> AtlasConfig:
    """
    Load configuration: .env → YAML → environment overrides.

    Call once at process startup (e.g. from main.py).
    """
    root = Path(root_dir or Path.cwd()).resolve()
    load_dotenv(dotenv_path or root / ".env")

    cfg_file = Path(
        _env("ATLAS_CONFIG_PATH", str(root / "configs" / "default.yaml"))
    )
    if config_path is not None:
        cfg_file = Path(config_path)

    yaml_data: dict[str, Any] = {}
    if cfg_file.exists():
        with cfg_file.open(encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}

    orch = yaml_data.get("orchestrator", {})
    logging_cfg = yaml_data.get("logging", {})
    memory_cfg = yaml_data.get("memory", {})
    sessions_cfg = yaml_data.get("sessions", {})
    slack_cfg = yaml_data.get("slack", {})
    claude_cfg = yaml_data.get("claude_code", {})
    browser_cfg = yaml_data.get("browser", {})
    recovery_cfg = yaml_data.get("recovery", {})
    git_cfg = yaml_data.get("git", {})
    runtime_cfg = yaml_data.get("runtime", {})
    scanner_cfg = yaml_data.get("scanner", {})
    projects_cfg = yaml_data.get("projects", {})

    projects = {
        name: ProjectConfig(
            repo_path=p.get("repo_path", ""),
            description=p.get("description", ""),
        )
        for name, p in (projects_cfg or {}).items()
        if isinstance(p, dict) and p.get("repo_path")
    }

    slack_enabled = _env_bool("ATLAS_SLACK_ENABLED", slack_cfg.get("enabled", False))

    return AtlasConfig(
        root_dir=root,
        config_path=cfg_file.resolve(),
        log_level=_env("ATLAS_LOG_LEVEL", logging_cfg.get("level", "INFO")),
        log_dir=root / _env("ATLAS_LOG_DIR", "logs"),
        projects_dir=root / _env("ATLAS_PROJECTS_DIR", "projects"),
        poll_interval_sec=float(
            _env("ATLAS_POLL_INTERVAL_SEC", str(orch.get("poll_interval_sec", 5)))
        ),
        max_concurrent_tasks=_env_int(
            "ATLAS_MAX_CONCURRENT_TASKS", orch.get("max_concurrent_tasks", 3)
        ),
        memory_db_path=root / memory_cfg.get("db_path", "memory/atlas.db"),
        memory_config=MemoryConfig(
            enabled=_env_bool("ATLAS_MEMORY_ENABLED", memory_cfg.get("enabled", True)),
            prune_max_age_days=_env_int(
                "ATLAS_MEMORY_PRUNE_DAYS", memory_cfg.get("prune_max_age_days", 90)
            ),
            inject_context_into_prompts=_env_bool(
                "ATLAS_MEMORY_INJECT_CONTEXT",
                memory_cfg.get("inject_context_into_prompts", True),
            ),
        ),
        tmux_session_prefix=sessions_cfg.get("session_prefix", "atlas"),
        tmux_socket=sessions_cfg.get("tmux_socket"),
        screenshots_dir=root / browser_cfg.get("screenshots_dir", "logs/screenshots"),
        browser=BrowserConfig(
            headless=_env_bool("ATLAS_BROWSER_HEADLESS", browser_cfg.get("headless", True)),
            timeout_ms=_env_int("ATLAS_BROWSER_TIMEOUT_MS", browser_cfg.get("timeout_ms", 30000)),
            max_runtime_sec=float(
                _env("ATLAS_BROWSER_MAX_SEC", str(browser_cfg.get("max_runtime_sec", 120)))
            ),
            capture_success_screenshot=_env_bool(
                "ATLAS_BROWSER_CAPTURE_SUCCESS",
                browser_cfg.get("capture_success_screenshot", True),
            ),
            capture_failure_screenshot=_env_bool(
                "ATLAS_BROWSER_CAPTURE_FAILURE",
                browser_cfg.get("capture_failure_screenshot", True),
            ),
            viewport_width=_env_int(
                "ATLAS_BROWSER_WIDTH", browser_cfg.get("viewport_width", 1280)
            ),
            viewport_height=_env_int(
                "ATLAS_BROWSER_HEIGHT", browser_cfg.get("viewport_height", 720)
            ),
        ),
        git=GitSafetyConfig(
            enabled=_env_bool("ATLAS_GIT_ENABLED", git_cfg.get("enabled", True)),
            branch_prefix=_env("ATLAS_GIT_BRANCH_PREFIX", git_cfg.get("branch_prefix", "atlas/task"))
            or "atlas/task",
            protected_branches=git_cfg.get("protected_branches", ["main", "master"]),
            block_work_on_protected=_env_bool(
                "ATLAS_GIT_BLOCK_MAIN", git_cfg.get("block_work_on_protected", True)
            ),
            auto_checkpoint_before_retry=_env_bool(
                "ATLAS_GIT_AUTO_CHECKPOINT",
                git_cfg.get("auto_checkpoint_before_retry", True),
            ),
            auto_isolate_branch=_env_bool(
                "ATLAS_GIT_AUTO_ISOLATE", git_cfg.get("auto_isolate_branch", True)
            ),
            max_consecutive_rollbacks=_env_int(
                "ATLAS_GIT_MAX_ROLLBACKS", git_cfg.get("max_consecutive_rollbacks", 3)
            ),
            max_dependency_modifications=_env_int(
                "ATLAS_GIT_MAX_DEP_CHANGES",
                git_cfg.get("max_dependency_modifications", 5),
            ),
            health_regression_threshold=float(
                _env(
                    "ATLAS_HEALTH_REGRESSION_THRESHOLD",
                    str(git_cfg.get("health_regression_threshold", 15)),
                )
            ),
            progressive_worsening_attempts=_env_int(
                "ATLAS_PROGRESSIVE_WORSENING",
                git_cfg.get("progressive_worsening_attempts", 3),
            ),
        ),
        recovery=RecoveryConfig(
            max_recovery_attempts=_env_int(
                "ATLAS_MAX_RECOVERY_ATTEMPTS",
                recovery_cfg.get("max_recovery_attempts", 5),
            ),
            max_same_strategy_failures=_env_int(
                "ATLAS_MAX_SAME_STRATEGY_FAILURES",
                recovery_cfg.get("max_same_strategy_failures", 2),
            ),
            investigate_after_failures=_env_int(
                "ATLAS_INVESTIGATE_AFTER_FAILURES",
                recovery_cfg.get("investigate_after_failures", 2),
            ),
            escalate_after_attempts=_env_int(
                "ATLAS_ESCALATE_AFTER_ATTEMPTS",
                recovery_cfg.get("escalate_after_attempts", 6),
            ),
        ),
        claude_code=ClaudeCodeConfig(
            command=_env("ATLAS_CLAUDE_COMMAND", claude_cfg.get("command", "claude")),
            max_execution_sec=float(
                _env(
                    "ATLAS_CLAUDE_MAX_SEC",
                    str(claude_cfg.get("max_execution_sec", 3600)),
                )
            ),
            poll_interval_sec=float(
                _env(
                    "ATLAS_CLAUDE_POLL_SEC",
                    str(claude_cfg.get("poll_interval_sec", 2)),
                )
            ),
            launch_wait_sec=float(
                _env(
                    "ATLAS_CLAUDE_LAUNCH_WAIT_SEC",
                    str(claude_cfg.get("launch_wait_sec", 15)),
                )
            ),
            idle_stable_polls=_env_int(
                "ATLAS_CLAUDE_IDLE_POLLS",
                claude_cfg.get("idle_stable_polls", 3),
            ),
            capture_lines=_env_int(
                "ATLAS_CLAUDE_CAPTURE_LINES",
                claude_cfg.get("capture_lines", 200),
            ),
            kill_session_on_finish=_env_bool(
                "ATLAS_CLAUDE_KILL_SESSION",
                claude_cfg.get("kill_session_on_finish", False),
            ),
        ),
        runtime=RuntimeConfig(
            enabled=_env_bool("ATLAS_RUNTIME_ENABLED", runtime_cfg.get("enabled", True)),
            state_path=runtime_cfg.get("state_path", "logs/runtime_state.json"),
            stale_task_hours=_env_int(
                "ATLAS_STALE_TASK_HOURS", runtime_cfg.get("stale_task_hours", 72)
            ),
            max_runaway_retries=_env_int(
                "ATLAS_MAX_RUNAWAY_RETRIES", runtime_cfg.get("max_runaway_retries", 15)
            ),
            max_browser_concurrent=_env_int(
                "ATLAS_MAX_BROWSER", runtime_cfg.get("max_browser_concurrent", 2)
            ),
            max_claude_concurrent=_env_int(
                "ATLAS_MAX_CLAUDE", runtime_cfg.get("max_claude_concurrent", 2)
            ),
            per_project_max_concurrent=_env_int(
                "ATLAS_PER_PROJECT_MAX",
                runtime_cfg.get("per_project_max_concurrent", 2),
            ),
            snapshot_interval_ticks=_env_int(
                "ATLAS_SNAPSHOT_TICKS", runtime_cfg.get("snapshot_interval_ticks", 12)
            ),
            orphan_session_cleanup=_env_bool(
                "ATLAS_ORPHAN_CLEANUP", runtime_cfg.get("orphan_session_cleanup", True)
            ),
        ),
        slack=SlackConfig(
            enabled=slack_enabled,
            bot_token=_env("SLACK_BOT_TOKEN"),
            app_token=_env("SLACK_APP_TOKEN"),
            channel_id=_env("SLACK_CHANNEL_ID"),
            allowed_user_ids=_env_list("SLACK_ALLOWED_USER_IDS")
            or slack_cfg.get("allowed_user_ids", []),
            allowed_channel_ids=_env_list("SLACK_ALLOWED_CHANNEL_IDS")
            or slack_cfg.get("allowed_channel_ids", [])
            or ([_env("SLACK_CHANNEL_ID")] if _env("SLACK_CHANNEL_ID") else []),
            log_tail_lines=_env_int(
                "SLACK_LOG_TAIL_LINES", slack_cfg.get("log_tail_lines", 40)
            ),
            stop_kill_session_default=_env_bool(
                "SLACK_STOP_KILL_SESSION",
                slack_cfg.get("stop_kill_session_default", False),
            ),
        ),
        projects=projects,
        scanner_interval_hours=float(
            _env("ATLAS_SCANNER_INTERVAL_HOURS", str(scanner_cfg.get("interval_hours", 6.0)))
        ),
        scanner_enabled=_env_bool(
            "ATLAS_SCANNER_ENABLED", scanner_cfg.get("enabled", True)
        ),
        _yaml=yaml_data,
    )
