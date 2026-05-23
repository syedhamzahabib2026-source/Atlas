"""
Approval engine — governance coordination for Atlas (Phase 14).

Coordinates approvals, pauses risky tasks, resumes approved work.
Does NOT execute agents, classify risk (RiskClassifier), or prepare PRs (PRManager).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from core.approval_policy import ApprovalConfig, ApprovalMode, ApprovalPolicy
from core.logger import get_logger
from core.risk_classifier import RiskAssessment, RiskClassifier, RiskLevel
from core.task_manager import TaskStatus

if TYPE_CHECKING:
    from core.pr_manager import PRPreparationManager
    from core.task_manager import Task
    from core.task_result import TaskResult
    from slack.bot import SlackBot

logger = get_logger("approval.engine")


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"
    PENDING = "pending"


class ApprovalPhase(str, Enum):
    PRE_EXECUTION = "pre_execution"
    POST_EXECUTION = "post_execution"
    PR_CREATION = "pr_creation"


@dataclass
class ApprovalContext:
    """Rich context for human reviewers."""

    task_id: str
    phase: ApprovalPhase
    risk: RiskAssessment
    change_summary: str = ""
    verification_summary: str = ""
    recovery_summary: str = ""
    files_modified: list[str] = field(default_factory=list)
    rollback_available: bool = False
    suggested_action: str = ""
    branch_name: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "phase": self.phase.value,
            "risk": self.risk.to_dict(),
            "change_summary": self.change_summary,
            "verification_summary": self.verification_summary,
            "recovery_summary": self.recovery_summary,
            "files_modified": self.files_modified,
            "rollback_available": self.rollback_available,
            "suggested_action": self.suggested_action,
            "branch_name": self.branch_name,
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
        }


@dataclass
class ApprovalRecord:
    """Single approval gate event — stored in task metadata approval history."""

    requested_at: str
    phase: str
    risk_level: str
    decision: str = ApprovalDecision.PENDING.value
    decided_at: str | None = None
    decided_by: str | None = None
    notes: str | None = None
    slack_message_ts: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_at": self.requested_at,
            "phase": self.phase,
            "risk_level": self.risk_level,
            "decision": self.decision,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "notes": self.notes,
            "slack_message_ts": self.slack_message_ts,
        }


@dataclass
class GateResult:
    """Whether orchestrator should proceed, wait, or stop."""

    proceed: bool
    wait_for_approval: bool = False
    blocked: bool = False
    reason: str = ""
    requirement_mode: ApprovalMode = ApprovalMode.AUTO


@dataclass
class ApprovalResult:
    success: bool
    error: str | None = None


@dataclass
class ChangeSummaries:
    change_summary: str
    verification_summary: str
    recovery_summary: str

    def apply_to_task(self, task: Task) -> None:
        task.metadata["change_summary"] = self.change_summary
        task.metadata["verification_summary"] = self.verification_summary
        task.metadata["recovery_summary"] = self.recovery_summary


# In-memory pending gates (survives task state in SQLite; re-hydrated on restart)
@dataclass
class PendingApproval:
    task_id: str
    context: ApprovalContext
    requested_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    slack_message_ts: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    branch_name: str | None = None


class ApprovalEngine:
    """
    Governance coordinator: risk gates, pause/resume, Slack approval workflow.
    """

    WAIT_STATUSES = frozenset({
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.AWAITING_APPROVAL,
    })

    def __init__(
        self,
        policy: ApprovalPolicy | None = None,
        risk_classifier: RiskClassifier | None = None,
        slack: SlackBot | None = None,
        channel_id: str | None = None,
        pr_preparation: PRPreparationManager | None = None,
    ) -> None:
        self.policy = policy or ApprovalPolicy()
        self.risk = risk_classifier or RiskClassifier()
        self._slack = slack
        self._channel_id = channel_id
        self._pr_prep = pr_preparation
        self._pending: dict[str, PendingApproval] = {}

    def bind_slack(self, slack: SlackBot | None, channel_id: str | None = None) -> None:
        self._slack = slack
        if channel_id:
            self._channel_id = channel_id

    # ── Risk + gates ─────────────────────────────────────────────────────────

    def classify(self, task: Task, *, result: TaskResult | None = None, phase: str = "pre_execution") -> RiskAssessment:
        assessment = self.risk.classify_task(task, result=result, phase=phase)
        task.metadata["risk"] = assessment.to_dict()
        return assessment

    def evaluate_gate(
        self,
        task: Task,
        assessment: RiskAssessment,
        phase: ApprovalPhase,
    ) -> GateResult:
        """Determine if task may proceed without pausing."""
        req = self.policy.effective_requirement(
            assessment.risk_level,
            dependency_change=assessment.dependency_change,
            deployment_change=assessment.deployment_change,
            recovery_escalation=assessment.recovery_escalation,
        )

        if self.policy.can_auto_proceed(req) and phase == ApprovalPhase.PRE_EXECUTION:
            return GateResult(proceed=True, requirement_mode=req.mode)

        if req.mode == ApprovalMode.AUTO:
            if phase == ApprovalPhase.PR_CREATION and self.policy.should_pause_before_pr(req):
                return GateResult(
                    proceed=False,
                    wait_for_approval=True,
                    reason=req.description,
                    requirement_mode=req.mode,
                )
            return GateResult(proceed=True, requirement_mode=req.mode)

        if req.block_automatic_execution and phase == ApprovalPhase.PRE_EXECUTION:
            return GateResult(
                proceed=False,
                wait_for_approval=True,
                blocked=True,
                reason=req.description,
                requirement_mode=req.mode,
            )

        if phase in (ApprovalPhase.POST_EXECUTION, ApprovalPhase.PR_CREATION):
            if self.policy.should_pause_before_pr(req) or req.requires_explicit_approval:
                return GateResult(
                    proceed=False,
                    wait_for_approval=True,
                    reason=req.description,
                    requirement_mode=req.mode,
                )

        if req.mode == ApprovalMode.SLACK_CONFIRM and phase == ApprovalPhase.PRE_EXECUTION:
            return GateResult(proceed=True, requirement_mode=req.mode)

        return GateResult(proceed=True, requirement_mode=req.mode)

    # ── Summaries ────────────────────────────────────────────────────────────

    def build_change_summaries(self, task: Task, result: TaskResult | None = None) -> ChangeSummaries:
        """Engineering change report for reviewers."""
        prompt = (task.metadata.get("prompt") or task.description)[:400]
        strategies = [
            r.get("strategy", "?")
            for r in task.metadata.get("attempt_history", [])[-5:]
        ]
        strategies_text = ", ".join(strategies) if strategies else "initial"

        git_meta = task.metadata.get("git", {})
        checkpoints = git_meta.get("checkpoints", [])
        rollbacks = git_meta.get("rollback_history", [])

        change = (
            f"*What changed:* Task `{task.id[:8]}` — {task.title[:80]}\n"
            f"*Why:* {prompt}\n"
            f"*Strategies attempted:* {strategies_text}"
        )
        if result:
            change += f"\n*Outcome:* {result.summary[:300]}"

        verification = task.metadata.get("verification", {})
        if verification:
            verification_summary = (
                f"Verification: {verification.get('status', 'unknown')}"
            )
        elif result and result.success:
            verification_summary = "No browser verification configured; agent reported success."
        else:
            verification_summary = "Verification not run."

        recovery_parts = []
        if len(task.metadata.get("attempt_history", [])) > 1:
            recovery_parts.append(
                f"{len(task.metadata['attempt_history'])} recovery attempts"
            )
        if rollbacks:
            recovery_parts.append(f"{len(rollbacks)} rollbacks")
        if checkpoints:
            recovery_parts.append(f"{len(checkpoints)} checkpoints available")
        recovery_summary = (
            "; ".join(recovery_parts) if recovery_parts else "Clean execution path"
        )

        summaries = ChangeSummaries(
            change_summary=change,
            verification_summary=verification_summary,
            recovery_summary=recovery_summary,
        )
        summaries.apply_to_task(task)
        return summaries

    def build_approval_context(
        self,
        task: Task,
        assessment: RiskAssessment,
        phase: ApprovalPhase,
        *,
        result: TaskResult | None = None,
    ) -> ApprovalContext:
        summaries = self.build_change_summaries(task, result)
        git_meta = task.metadata.get("git", {})
        files = list(git_meta.get("files_changed") or git_meta.get("changed_files") or [])
        checkpoints = git_meta.get("checkpoints", [])

        risk = assessment
        known = "; ".join(risk.risk_reasons[:4])
        suggested = {
            RiskLevel.LOW: "Auto-merge path available after PR review.",
            RiskLevel.MEDIUM: "Confirm in Slack, then approve PR when ready.",
            RiskLevel.HIGH: "Review diff and recovery history before approving.",
            RiskLevel.CRITICAL: "Manual review only — do not auto-execute or auto-merge.",
        }[risk.risk_level]

        return ApprovalContext(
            task_id=task.id,
            phase=phase,
            risk=assessment,
            change_summary=summaries.change_summary,
            verification_summary=summaries.verification_summary,
            recovery_summary=summaries.recovery_summary,
            files_modified=files[:20],
            rollback_available=bool(checkpoints),
            suggested_action=f"{suggested} Known risks: {known}",
            branch_name=git_meta.get("branch"),
            pr_url=task.metadata.get("pr_url"),
            pr_number=task.metadata.get("pr_number"),
        )

    # ── Pause / resume ───────────────────────────────────────────────────────

    async def pause_for_approval(
        self,
        task: Task,
        context: ApprovalContext,
        *,
        update_status: bool = True,
    ) -> ApprovalResult:
        """Pause task at approval boundary and notify Slack."""
        now = datetime.now(timezone.utc).isoformat()
        record = ApprovalRecord(
            requested_at=now,
            phase=context.phase.value,
            risk_level=context.risk.risk_level.value,
        )
        history = task.metadata.setdefault("approval_history", [])
        history.append(record.to_dict())
        task.metadata["approval_context"] = context.to_dict()
        task.metadata["approval_phase"] = context.phase.value

        pending = PendingApproval(
            task_id=task.id,
            context=context,
            branch_name=context.branch_name,
            pr_url=context.pr_url,
            pr_number=context.pr_number,
        )

        if self._slack:
            try:
                ts = await self._slack.notify_approval_required(task, context)  # type: ignore[attr-defined]
                pending.slack_message_ts = ts
                record.slack_message_ts = ts
                history[-1] = record.to_dict()
            except Exception as exc:
                logger.error("Approval Slack notify failed for %s: %s", task.id[:8], exc)
                return ApprovalResult(success=False, error=str(exc))
        else:
            logger.warning(
                "Slack not connected — approval for task %s not posted", task.id[:8]
            )

        self._pending[task.id] = pending
        if update_status:
            task.status = TaskStatus.WAITING_APPROVAL

        logger.info(
            "Task %s waiting for approval (%s, risk=%s)",
            task.id[:8],
            context.phase.value,
            context.risk.risk_level.value,
        )
        return ApprovalResult(success=True)

    def is_waiting(self, task: Task) -> bool:
        return task.status in self.WAIT_STATUSES

    def can_resume(self, task: Task) -> bool:
        return task.status == TaskStatus.APPROVED or task.metadata.get("approval_decision") == ApprovalDecision.APPROVE.value

    def record_decision(
        self,
        task_id: str,
        decision: ApprovalDecision,
        *,
        user_id: str | None = None,
        notes: str | None = None,
    ) -> PendingApproval | None:
        pending = self.get_pending(task_id)
        if not pending:
            return None

        now = datetime.now(timezone.utc).isoformat()
        history = pending.context  # context only; update task via caller
        _ = history

        if task_id in self._pending:
            self._pending[task_id]  # keep until cleared by controller

        logger.info(
            "Approval decision %s for task %s by %s",
            decision.value,
            task_id[:8],
            user_id or "unknown",
        )
        return pending

    def apply_decision_to_task(
        self,
        task: Task,
        decision: ApprovalDecision,
        *,
        user_id: str | None = None,
        notes: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        task.metadata["approval_decision"] = decision.value
        task.metadata["approval_decided_at"] = now
        task.metadata["approval_decided_by"] = user_id
        if notes:
            task.metadata["approval_notes"] = notes

        history = task.metadata.get("approval_history", [])
        if history:
            history[-1]["decision"] = decision.value
            history[-1]["decided_at"] = now
            history[-1]["decided_by"] = user_id
            history[-1]["notes"] = notes

    def clear_pending(self, task_id: str) -> bool:
        if task_id in self._pending:
            self._pending.pop(task_id)
            return True
        tid = task_id.lower()
        for key in list(self._pending):
            if key.lower().startswith(tid):
                self._pending.pop(key)
                return True
        return False

    def get_pending(self, task_id: str) -> PendingApproval | None:
        if task_id in self._pending:
            return self._pending[task_id]
        tid = task_id.lower()
        for key, req in self._pending.items():
            if key.lower().startswith(tid):
                return req
        return None

    def get_pending_approvals(self) -> list[PendingApproval]:
        return sorted(self._pending.values(), key=lambda p: p.requested_at)

    def pending_count(self) -> int:
        return len(self._pending)

    def rehydrate_from_tasks(self, tasks: list[Task]) -> None:
        """Restore in-memory pending map from durable task state after restart."""
        for task in tasks:
            if task.status not in self.WAIT_STATUSES:
                continue
            ctx_data = task.metadata.get("approval_context")
            if not ctx_data:
                logger.warning(
                    "rehydrate: task %s is waiting_approval but has no approval_context — skipping",
                    task.id[:8],
                )
                continue
            risk = RiskAssessment.from_dict(ctx_data.get("risk"))
            if not risk:
                logger.warning(
                    "rehydrate: task %s approval_context has no risk data — skipping",
                    task.id[:8],
                )
                continue
            try:
                phase = ApprovalPhase(ctx_data.get("phase", "post_execution"))
            except ValueError:
                phase = ApprovalPhase.POST_EXECUTION
            context = ApprovalContext(
                task_id=task.id,
                phase=phase,
                risk=risk,
                change_summary=ctx_data.get("change_summary", ""),
                verification_summary=ctx_data.get("verification_summary", ""),
                recovery_summary=ctx_data.get("recovery_summary", ""),
                files_modified=list(ctx_data.get("files_modified", [])),
                rollback_available=bool(ctx_data.get("rollback_available")),
                suggested_action=ctx_data.get("suggested_action", ""),
                branch_name=ctx_data.get("branch_name"),
                pr_url=ctx_data.get("pr_url"),
                pr_number=ctx_data.get("pr_number"),
            )
            self._pending[task.id] = PendingApproval(
                task_id=task.id,
                context=context,
                branch_name=context.branch_name,
                pr_url=context.pr_url,
                pr_number=context.pr_number,
            )
            logger.info(
                "rehydrate: restored pending approval for task %s (phase=%s)",
                task.id[:8],
                context.phase.value,
            )

    def format_slack_message(self, task: Task, context: ApprovalContext) -> str:
        short = task.id[:8]
        files = ", ".join(f"`{f}`" for f in context.files_modified[:8]) or "_none tracked_"
        rollback = "yes" if context.rollback_available else "no"
        pr_line = ""
        if context.pr_url:
            pr_line = f"• PR: {context.pr_url}\n"

        return (
            f"*Atlas — approval required*\n"
            f"Approve Atlas changes for task `{short}`?\n\n"
            f"*Summary*\n{context.change_summary[:1200]}\n\n"
            f"*Risk:* `{context.risk.risk_level.value}` (score {context.risk.risk_score})\n"
            f"*Reasons:* {', '.join(context.risk.risk_reasons[:4])}\n"
            f"*Files modified:* {files}\n"
            f"*Verification:* {context.verification_summary}\n"
            f"*Recovery:* {context.recovery_summary}\n"
            f"*Rollback available:* {rollback}\n"
            f"{pr_line}"
            f"*Suggested action:* {context.suggested_action}\n\n"
            f"Reply with:\n"
            f"  `/atlas approve {short}` — approve and continue\n"
            f"  `/atlas reject {short} <reason>` — reject\n"
            f"  `/atlas request_changes {short} <feedback>` — request changes"
        )
