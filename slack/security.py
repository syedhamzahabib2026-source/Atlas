"""
Slack command authorization — restrict to approved users and channels.
"""

from __future__ import annotations

from core.config import SlackConfig
from core.logger import get_logger

logger = get_logger("slack.security")


def is_authorized(
    config: SlackConfig,
    *,
    user_id: str | None,
    channel_id: str | None,
) -> tuple[bool, str]:
    """
    Return (allowed, reason_if_denied).
    Empty allow-lists mean no restriction for that dimension.
    """
    if config.allowed_user_ids and user_id not in config.allowed_user_ids:
        logger.warning("Denied Slack user: %s", user_id)
        return False, "You are not authorized to control Atlas."

    if not config.allowed_channel_ids:
        return True, ""

    if channel_id in config.allowed_channel_ids:
        return True, ""

    # Direct messages (channel id D…) are not in the workspace channel allow-list
    # but authorized users should still control Atlas from the app DM.
    if channel_id and channel_id.startswith("D"):
        return True, ""

    logger.warning("Denied Slack channel: %s", channel_id)
    return False, "This channel is not authorized for Atlas commands."
