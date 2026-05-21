"""
Approval state manager for Atlas PR lifecycle.

Sits between PR creation (pr_manager) and merge (git_manager + pr_manager).
When a task completes and a PR is opened, the orchestrator calls
request_approval() to pause in AWAITING_APPROVAL and post a Slack prompt.
A human replies via /atlas approve or /atlas reject to resume.

No new DB tables — pending approvals live in memory only. Atlas restart
clears them; tasks remain in AWAITING_APPROVAL in SQLite and will need
manual /atlas approve after a restart (acceptable for now).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from core.logger import get_logger

if TYPE_CHECKING:
    from slack.bot import SlackBot

logger = get_logger("approval")


# ── Data ──────────────────────────────────────────────────────────────────────

@dataclass
class ApprovalRequest:
    task_id: str
    pr_number: int
    pr_url: str
    branch_name: str
    requested_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    slack_message_ts: str | None = None


@dataclass
class ApprovalResult:
    success: bool
    error: str | None = None


# ── Manager ───────────────────────────────────────────────────────────────────

class ApprovalManager:
    """
    Tracks tasks awaiting human PR approval via Slack.

    Instantiate once and share across the orchestrator lifetime.
    The SlackBot instance must already be connected before request_approval()
    is called.
    """

    def __init__(self, slack: SlackBot | None, channel_id: str | None = None) -> None:
        self._slack = slack
        self._channel_id = channel_id
        self._pending: dict[str, ApprovalRequest] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    async def request_approval(
        self,
        task_id: str,
        pr_url: str,
        pr_number: int,
        branch_name: str,
    ) -> ApprovalResult:
        """
        Post a Slack approval prompt and store the pending request.

        The task should already be in AWAITING_APPROVAL status before
        this is called.
        """
        req = ApprovalRequest(
            task_id=task_id,
            pr_number=pr_number,
            pr_url=pr_url,
            branch_name=branch_name,
        )

        short_id = task_id[:8]
        text = (
            f"*Atlas — PR ready for review*\n"
            f"• Task: `{short_id}`\n"
            f"• Branch: `{branch_name}`\n"
            f"• PR: {pr_url}\n\n"
            f"Reply with one of:\n"
            f"  `/atlas approve {short_id}` — merge the PR\n"
            f"  `/atlas reject {short_id} <reason>` — send back for rework"
        )

        ts: str | None = None
        if self._slack:
            try:
                ts = await self._slack.notify(text, channel_id=self._channel_id)
                logger.info(
                    "Approval request posted for task %s PR #%s", short_id, pr_number
                )
            except Exception as exc:
                logger.error("Failed to post approval request for task %s: %s", short_id, exc)
                return ApprovalResult(success=False, error=str(exc))
        else:
            logger.warning(
                "Slack not connected — approval request for task %s not posted", short_id
            )

        req.slack_message_ts = ts
        self._pending[task_id] = req
        return ApprovalResult(success=True)

    def get_pending_approvals(self) -> list[ApprovalRequest]:
        """Return all tasks currently awaiting approval, oldest first."""
        return sorted(self._pending.values(), key=lambda r: r.requested_at)

    def get_approval(self, task_id: str) -> ApprovalRequest | None:
        """Look up a specific pending approval by full or prefix task ID."""
        if task_id in self._pending:
            return self._pending[task_id]
        task_id_lower = task_id.lower()
        for tid, req in self._pending.items():
            if tid.lower().startswith(task_id_lower):
                return req
        return None

    def clear_approval(self, task_id: str) -> bool:
        """
        Remove a pending approval after approve or reject.

        Accepts a full task ID or a short prefix (same as _resolve_task_id
        in atlas_controller). Returns True if something was removed.
        """
        if task_id in self._pending:
            self._pending.pop(task_id)
            logger.info("Approval cleared for task %s", task_id[:8])
            return True
        task_id_lower = task_id.lower()
        for tid in list(self._pending):
            if tid.lower().startswith(task_id_lower):
                self._pending.pop(tid)
                logger.info("Approval cleared for task %s (prefix match)", tid[:8])
                return True
        return False

    def pending_count(self) -> int:
        return len(self._pending)
