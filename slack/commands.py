"""
Parse and format /atlas Slack commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class ParsedCommand:
    subcommand: str
    args: list[str]
    raw: str


def parse_atlas_command(text: str) -> ParsedCommand:
    """
    Parse '/atlas start fix bug' or 'start fix bug' (slash command text body).
    """
    raw = (text or "").strip()
    # Normalize Slack's smart-dash substitutions (em-dash → --, en-dash → -)
    raw = raw.replace("—", "--").replace("–", "-")
    parts = raw.split()
    if not parts:
        return ParsedCommand(subcommand="help", args=[], raw=raw)

    # Allow optional leading 'atlas'
    if parts[0].lower() == "atlas":
        parts = parts[1:]

    if not parts:
        return ParsedCommand(subcommand="help", args=[], raw=raw)

    sub = parts[0].lower()
    args = parts[1:]
    return ParsedCommand(subcommand=sub, args=args, raw=raw)


def format_duration(started: datetime | None) -> str:
    if not started:
        return "n/a"
    delta = datetime.now(timezone.utc) - started
    sec = int(delta.total_seconds())
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h {(sec % 3600) // 60}m"
