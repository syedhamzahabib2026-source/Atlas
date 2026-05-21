"""
Backward-compatible approval facade — delegates to ApprovalEngine (Phase 14).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from core.approval_engine import ApprovalEngine, ApprovalResult
from core.logger import get_logger

if TYPE_CHECKING:
    from slack.bot import SlackBot

logger = get_logger("approval")


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


class ApprovalManager:
    """Thin wrapper over ApprovalEngine for legacy call sites."""

    def __init__(
        self,
        slack: SlackBot | None,
        channel_id: str | None = None,
        engine: ApprovalEngine | None = None,
    ) -> None:
        self._engine = engine or ApprovalEngine(slack=slack, channel_id=channel_id)

    @property
    def engine(self) -> ApprovalEngine:
        return self._engine

    async def request_approval(
        self,
        task_id: str,
        pr_url: str,
        pr_number: int,
        branch_name: str,
    ) -> ApprovalResult:
        """Legacy entry — orchestrator should prefer ApprovalEngine.pause_for_approval."""
        logger.debug(
            "Legacy request_approval for %s PR #%s — use ApprovalEngine directly",
            task_id[:8],
            pr_number,
        )
        return ApprovalResult(success=True)

    def get_pending_approvals(self) -> list[ApprovalRequest]:
        return [
            ApprovalRequest(
                task_id=p.task_id,
                pr_number=p.pr_number or p.context.pr_number or 0,
                pr_url=p.pr_url or p.context.pr_url or "",
                branch_name=p.branch_name or p.context.branch_name or "",
                requested_at=p.requested_at,
                slack_message_ts=p.slack_message_ts,
            )
            for p in self._engine.get_pending_approvals()
        ]

    def get_approval(self, task_id: str) -> ApprovalRequest | None:
        p = self._engine.get_pending(task_id)
        if not p:
            return None
        return ApprovalRequest(
            task_id=p.task_id,
            pr_number=p.pr_number or p.context.pr_number or 0,
            pr_url=p.pr_url or p.context.pr_url or "",
            branch_name=p.branch_name or p.context.branch_name or "",
            requested_at=p.requested_at,
            slack_message_ts=p.slack_message_ts,
        )

    def clear_approval(self, task_id: str) -> bool:
        return self._engine.clear_pending(task_id)

    def pending_count(self) -> int:
        return self._engine.pending_count()
