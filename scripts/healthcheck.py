#!/usr/bin/env python3
"""
Atlas pre-flight health check.

Validates config, Slack API, DB, tmux, and the claude CLI
without starting the orchestrator or any agents.

Usage:
    python scripts/healthcheck.py
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"


def _icon(ok: bool, warn: bool = False) -> str:
    if ok:
        return f"{_GREEN}PASS{_RESET}"
    if warn:
        return f"{_YELLOW}WARN{_RESET}"
    return f"{_RED}FAIL{_RESET}"


def check(label: str, ok: bool, detail: str = "", warn_only: bool = False) -> bool:
    icon = _icon(ok, warn=warn_only and not ok)
    suffix = f"  {detail}" if detail else ""
    print(f"  [{icon}] {label}{suffix}")
    return ok


# ── 1. Config ──────────────────────────────────────────────────────────────

def run_config_check() -> tuple[bool, object | None]:
    print("\n1. Config")
    try:
        from core.config import load_config
        cfg = load_config(root_dir=ROOT)
        check("load_config()", True, str(cfg.config_path))
    except Exception as exc:
        check("load_config()", False, str(exc))
        return False, None

    slack_ready = cfg.slack_ready
    if slack_ready:
        check("slack_ready", True, "enabled + bot_token + app_token + channel_id all set")
    else:
        s = cfg.slack
        missing = [
            k for k, v in {
                "ATLAS_SLACK_ENABLED": s.enabled,
                "SLACK_BOT_TOKEN": s.bot_token,
                "SLACK_APP_TOKEN": s.app_token,
                "SLACK_CHANNEL_ID": s.channel_id,
            }.items() if not v
        ]
        check("slack_ready", False, f"missing: {', '.join(missing)}")

    return slack_ready, cfg


# ── 2. Slack API ────────────────────────────────────────────────────────────

async def run_slack_check(bot_token: str | None) -> bool:
    print("\n2. Slack API")
    if not bot_token:
        check("auth.test", False, "SLACK_BOT_TOKEN not set")
        return False
    try:
        from slack_sdk.web.async_client import AsyncWebClient
        client = AsyncWebClient(token=bot_token)
        resp = await client.auth_test()
        team = resp.get("team", "?")
        bot_user = resp.get("user", "?")
        check("auth.test", True, f"team={team!r}  bot={bot_user!r}")
        return True
    except Exception as exc:
        check("auth.test", False, str(exc))
        return False


# ── 3. Database ─────────────────────────────────────────────────────────────

def run_db_check(db_path: Path) -> bool:
    print("\n3. Database")
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        check("db dir writable", True, str(db_path.parent))
    except Exception as exc:
        check("db dir writable", False, str(exc))
        return False

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS _hc (ts TEXT)")
        conn.execute("INSERT INTO _hc VALUES (datetime('now'))")
        conn.commit()
        conn.execute("DROP TABLE _hc")
        conn.commit()
        conn.close()
        check("db read/write", True, str(db_path))
        return True
    except Exception as exc:
        check("db read/write", False, str(exc))
        return False


# ── 4. tmux ─────────────────────────────────────────────────────────────────

def run_tmux_check(session_prefix: str) -> bool:
    print("\n4. tmux")
    from sessions.tmux_manager import TmuxManager, TmuxBackend
    tmux = TmuxManager(session_prefix=session_prefix)
    available = tmux.is_available()
    backend = tmux.backend.value
    check("tmux available", available, backend, warn_only=True)
    if not available:
        print(f"         {_YELLOW}tmux is required for Claude Code tasks.")
        print(f"         On Windows: install WSL and run 'sudo apt install tmux' inside it.{_RESET}")
    return available


# ── 5. Claude CLI ────────────────────────────────────────────────────────────

def run_claude_check(command: str) -> bool:
    print("\n5. Claude Code CLI")
    path = shutil.which(command)
    ok = bool(path)
    check(f"'{command}' on PATH", ok, path if ok else "not found — run: npm install -g @anthropic-ai/claude-code")
    return ok


# ── Main ─────────────────────────────────────────────────────────────────────

async def main() -> int:
    print(f"\n{'=' * 44}")
    print("  Atlas pre-flight health check")
    print(f"{'=' * 44}")

    failures: list[str] = []
    warnings: list[str] = []

    slack_ready, cfg = run_config_check()
    if cfg is None:
        print("\nCannot continue — config failed to load.\n")
        return 1
    if not slack_ready:
        failures.append("slack_ready")

    if not await run_slack_check(cfg.slack.bot_token):
        failures.append("Slack API")

    if not run_db_check(cfg.memory_db_path):
        failures.append("database")

    tmux_ok = run_tmux_check(cfg.tmux_session_prefix)
    if not tmux_ok:
        warnings.append("tmux (Claude Code tasks will not work)")

    if not run_claude_check(cfg.claude_code.command):
        warnings.append("claude CLI (Claude Code tasks will not work)")

    print(f"\n{'=' * 44}")
    if not failures and not warnings:
        print(f"  {_GREEN}All checks passed.{_RESET}\n")
        return 0

    if failures:
        print(f"  {_RED}{len(failures)} failure(s):{_RESET} {', '.join(failures)}")
    if warnings:
        print(f"  {_YELLOW}{len(warnings)} warning(s):{_RESET} {', '.join(warnings)}")
    print()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
