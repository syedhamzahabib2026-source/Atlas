"""Slack communication layer for Atlas."""

from slack.bot import SlackBot
from slack.commands import parse_atlas_command

__all__ = ["SlackBot", "parse_atlas_command"]
