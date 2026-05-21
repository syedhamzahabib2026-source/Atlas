"""
Adaptive Recovery Engine (Phase 5).

Analyzes failures, diversifies strategies, detects contradictions,
triggers investigation mode, and plans retries — NOT dumb loops.

TODO: screenshot understanding / visual diff
TODO: architecture reasoning via LLM
TODO: semantic code comparison between attempts
TODO: automatic root-cause inference
TODO: multi-agent debate/review
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TYPE_CHECKING

from core.attempt_history import AttemptHistory, AttemptRecord
from core.failure_classifier import FailureCategory, classify_failure, error_signature
from core.logger import get_logger
from core.recovery_prompt import build_retry_prompt
from core.recovery_strategies import (
    STRATEGY_BY_CATEGORY,
    RecoveryStrategy,
)

from core.recovery_config import RecoveryConfig

if TYPE_CHECKING:
    from core.task_manager import Task
    from core.task_result import TaskResult

logger = get_logger("recovery")


class RecoveryAction(str, Enum):
    NONE = "none"
    INVESTIGATE = "investigate"
    RETRY = "retry"
    ROLLBACK = "rollback"
    ESCALATE = "escalate"
    FAIL = "fail"


@dataclass
class ContradictionReport:
    detected: bool
    reasons: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class RecoveryPlan:
    """Decision from recovery engine — orchestrator executes this."""

    action: RecoveryAction
    strategy: str | None = None
    prompt: str | None = None
    failure_category: str = "unknown"
    attempt_number: int = 0
    contradiction: ContradictionReport = field(default_factory=ContradictionReport)
    investigation_report: str | None = None
    escalation_reason: str | None = None
    message: str = ""
    root_cause_hypothesis: str = ""


class RecoveryEngine:
    """
    Failure analysis and recovery planning.

    Agents execute work; this engine decides what to try next.
    """

    def __init__(self, config: RecoveryConfig | None = None, log_dir: Path | None = None):
        self.config = config or RecoveryConfig()
        self.log_dir = log_dir or Path("logs")

    def record_attempt(
        self,
        task: Task,
        result: TaskResult,
        *,
        strategy: str,
        phase: str,
        outcome: str = "failed",
    ) -> AttemptRecord:
        browser = (result.metadata or {}).get("browser", {})
        category = classify_failure(task, result)
        record = AttemptRecord(
            attempt_number=len(AttemptHistory.load(task)) + 1,
            strategy=strategy,
            failure_category=category.value,
            outcome=outcome,
            error_summary=result.summary[:500],
            root_cause_hypothesis=self._hypothesis(category, result),
            phase=phase,
            browser_failures=browser.get("errors", []) if browser else [],
            console_errors=[
                l for l in browser.get("console_logs", []) if "[error]" in l.lower()
            ][:10]
            if browser
            else [],
            network_failures=browser.get("network_failures", [])[:10] if browser else [],
            verification_outcome="failed" if phase == "verification" else None,
            error_signature=error_signature(result),
            metadata={"agent": task.metadata.get("agent")},
        )
        AttemptHistory.append(task, record)
        logger.info(
            "Recorded attempt #%s strategy=%s category=%s",
            record.attempt_number,
            strategy,
            category.value,
        )
        return record

    def _hypothesis(self, category: FailureCategory, result: TaskResult) -> str:
        browser = (result.metadata or {}).get("browser", {})
        if category == FailureCategory.AUTH:
            return "Authentication or token handling may be broken server-side."
        if category == FailureCategory.NETWORK and browser.get("network_failures"):
            return f"Network/API failure: {browser['network_failures'][0][:120]}"
        if category == FailureCategory.FRONTEND:
            return "UI layer issue — verify whether API/backend is healthy first."
        if category == FailureCategory.BACKEND:
            return "Server or API path likely failing before UI can succeed."
        return f"Failure category {category.value}; needs investigation before patching."

    def detect_contradictions(self, task: Task) -> ContradictionReport:
        """
        Same symptom despite different strategies → assumptions may be wrong.
        """
        records = AttemptHistory.load(task)
        reasons: list[str] = []

        if len(records) < 2:
            return ContradictionReport(detected=False)

        sigs = [r.error_signature for r in records if r.error_signature]
        if len(sigs) >= 2 and len(set(sigs)) == 1:
            reasons.append("Identical error signature across multiple attempts.")

        strategies = [r.strategy for r in records if r.outcome == "failed"]
        if len(strategies) >= 3 and len(set(strategies)) >= 2:
            # Different strategies, same signature
            if len(set(sigs)) <= 1 and sigs:
                reasons.append(
                    "Multiple strategies tried but symptom unchanged — diagnosis may be wrong."
                )

        frontend_fails = sum(
            1
            for r in records
            if r.strategy in (
                RecoveryStrategy.MINIMAL_PATCH.value,
                RecoveryStrategy.FRONTEND_TRACE.value,
            )
            and r.outcome == "failed"
        )
        if frontend_fails >= 2:
            network_any = any(r.network_failures for r in records)
            if network_any:
                reasons.append(
                    "Repeated frontend fixes while network/API errors present — likely backend issue."
                )

        browser_recs = [r for r in records if r.browser_failures or r.network_failures]
        if len(browser_recs) >= 2:
            reasons.append("Browser verification failed repeatedly with similar diagnostics.")

        if not reasons:
            return ContradictionReport(detected=False)

        summary = "; ".join(reasons)
        return ContradictionReport(detected=True, reasons=reasons, summary=summary)

    def select_strategy(
        self,
        task: Task,
        category: FailureCategory,
        *,
        after_investigation: bool = False,
    ) -> RecoveryStrategy | None:
        """
        Pick a strategy not exhausted by prior failures.
        Never pick minimal_patch twice in a row after double failure.
        """
        candidates = list(STRATEGY_BY_CATEGORY.get(category, STRATEGY_BY_CATEGORY[FailureCategory.UNKNOWN]))

        if after_investigation:
            # Prefer non-patch strategies post-investigation
            candidates = [s for s in candidates if s != RecoveryStrategy.MINIMAL_PATCH]
            if RecoveryStrategy.INSPECT_LOGS_ONLY in candidates:
                candidates.remove(RecoveryStrategy.INSPECT_LOGS_ONLY)

        for strategy in candidates:
            fails = AttemptHistory.strategy_fail_count(task, strategy.value)
            if fails >= self.config.max_same_strategy_failures:
                logger.info("Skipping exhausted strategy %s (%s fails)", strategy.value, fails)
                continue
            # Avoid repeating last failed strategy
            used = AttemptHistory.strategies_used(task, failed_only=True)
            if used and used[-1] == strategy.value and fails >= 1:
                continue
            return strategy

        return None

    def should_escalate(
        self,
        task: Task,
        contradictions: ContradictionReport,
    ) -> str | None:
        """Return escalation reason or None."""
        count = int(task.metadata.get("recovery_attempt_count", 0))
        if count >= self.config.escalate_after_attempts:
            return f"Recovery attempt limit reached ({count})."

        if contradictions.detected and count >= self.config.investigate_after_failures:
            return f"Contradictions persist: {contradictions.summary}"

        text = (task.description + str(task.metadata)).lower()
        if "architecture conflict" in text or task.metadata.get("architecture_conflict"):
            return "Architecture conflict flagged on task."

        if task.metadata.get("requirements_inconsistent"):
            return "Task requirements appear inconsistent."

        return None

    def build_investigation_report(self, task: Task, result: TaskResult) -> str:
        """
        Gather diagnostics WITHOUT patching — investigation mode.
        """
        lines = [
            "# Investigation report",
            f"Task: {task.id}",
            f"Title: {task.title}",
            "",
            "## Failure summary",
            result.summary,
            "",
            "## Errors",
            "\n".join(f"- {e}" for e in result.errors) or "- none",
            "",
            "## Attempt history",
        ]
        for rec in AttemptHistory.load(task):
            lines.append(
                f"- #{rec.attempt_number} [{rec.strategy}] {rec.outcome}: {rec.error_summary[:150]}"
            )

        browser = (result.metadata or {}).get("browser", {})
        if browser:
            lines.extend([
                "",
                "## Browser diagnostics",
                f"- URL: {browser.get('final_url')}",
                f"- DOM: {browser.get('dom_summary')}",
            ])
            for nf in browser.get("network_failures", [])[:8]:
                lines.append(f"- Network: {nf}")
            for ce in browser.get("console_logs", [])[-12:]:
                lines.append(f"- Console: {ce}")
            for shot in browser.get("screenshots", [])[:3]:
                lines.append(f"- Screenshot: {shot}")

        log_path = self.log_dir / "atlas.log"
        if log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
            lines.extend(["", "## Atlas log tail", "```", *tail, "```"])

        lines.extend([
            "",
            "## Preliminary hypothesis",
            self._hypothesis(classify_failure(task, result), result),
            "",
            "## Recommendation",
            "Do not apply another UI patch until API/auth/network logs are understood.",
            "",
            "# TODO: vision model analyze screenshots",
            "# TODO: visual diff against baseline",
        ])
        return "\n".join(lines)

    def plan_recovery(
        self,
        task: Task,
        result: TaskResult,
        *,
        phase: str = "execution",
        investigation_report: str | None = None,
        health_report: Any | None = None,
    ) -> RecoveryPlan:
        """
        Main entry: analyze failure and return next action.
        """
        category = classify_failure(task, result)
        contradictions = self.detect_contradictions(task)
        attempt_num = len(AttemptHistory.load(task))

        escalation = self.should_escalate(task, contradictions)
        if escalation:
            return RecoveryPlan(
                action=RecoveryAction.ESCALATE,
                failure_category=category.value,
                attempt_number=attempt_num,
                contradiction=contradictions,
                escalation_reason=escalation,
                message=escalation,
            )

        recovery_count = int(task.metadata.get("recovery_attempt_count", 0))
        if recovery_count >= self.config.max_recovery_attempts:
            return RecoveryPlan(
                action=RecoveryAction.ESCALATE,
                failure_category=category.value,
                attempt_number=attempt_num,
                contradiction=contradictions,
                escalation_reason="Max recovery attempts exceeded.",
                message="Atlas will not retry further without human guidance.",
            )

        # Prefer rollback when health regressed and checkpoints exist
        if health_report and getattr(health_report, "regression_detected", False):
            checkpoints = task.metadata.get("git", {}).get("checkpoints", [])
            if checkpoints and AttemptHistory.strategy_fail_count(
                task, RecoveryStrategy.ROLLBACK_CHECKPOINT.value
            ) < self.config.max_same_strategy_failures:
                return RecoveryPlan(
                    action=RecoveryAction.ROLLBACK,
                    strategy=RecoveryStrategy.ROLLBACK_CHECKPOINT.value,
                    failure_category=category.value,
                    attempt_number=attempt_num,
                    contradiction=contradictions,
                    message="Regression detected — rollback before new strategy",
                    root_cause_hypothesis=self._hypothesis(category, result),
                )

        # Investigation mode: stop patching, gather evidence first
        needs_investigate = (
            not investigation_report
            and recovery_count >= self.config.investigate_after_failures
            and (contradictions.detected or recovery_count >= 2)
        )
        if needs_investigate and not task.metadata.get("investigation_complete"):
            report = self.build_investigation_report(task, result)
            return RecoveryPlan(
                action=RecoveryAction.INVESTIGATE,
                failure_category=category.value,
                attempt_number=attempt_num,
                contradiction=contradictions,
                investigation_report=report,
                message="Entering investigation mode — no patch until root cause is clearer.",
                root_cause_hypothesis=self._hypothesis(category, result),
            )

        strategy = self.select_strategy(
            task,
            category,
            after_investigation=bool(investigation_report or task.metadata.get("investigation_complete")),
        )
        if strategy is None:
            return RecoveryPlan(
                action=RecoveryAction.ESCALATE,
                failure_category=category.value,
                attempt_number=attempt_num,
                contradiction=contradictions,
                escalation_reason="No viable recovery strategies remain.",
                message="All strategies exhausted for this failure category.",
            )

        prompt = build_retry_prompt(
            task,
            result,
            strategy=strategy,
            category=category,
            contradiction_summary=contradictions.summary if contradictions.detected else None,
            investigation_report=investigation_report or task.metadata.get("investigation_report"),
        )

        return RecoveryPlan(
            action=RecoveryAction.RETRY,
            strategy=strategy.value,
            prompt=prompt,
            failure_category=category.value,
            attempt_number=attempt_num,
            contradiction=contradictions,
            message=f"Retry with strategy: {strategy.value}",
            root_cause_hypothesis=self._hypothesis(category, result),
        )

    def plan_after_investigation(self, task: Task, result: TaskResult) -> RecoveryPlan:
        """Select retry strategy using investigation findings."""
        task.metadata["investigation_complete"] = True
        report = task.metadata.get("investigation_report", "")
        health = task.metadata.get("health")
        return self.plan_recovery(
            task,
            result,
            phase="recovery",
            investigation_report=report,
            health_report=health,
        )

    def apply_retry_to_task(self, task: Task, plan: RecoveryPlan) -> None:
        """Mutate task for orchestrator to re-run with new prompt."""
        if not task.metadata.get("original_prompt"):
            task.metadata["original_prompt"] = (
                task.metadata.get("prompt") or task.description
            )
        task.metadata["prompt"] = plan.prompt
        task.metadata["recovery_strategy"] = plan.strategy
        task.metadata["failure_category"] = plan.failure_category
        task.metadata["agent"] = task.metadata.get("agent") or "claude_code"
        task.metadata["recovery_chain"] = task.metadata.get("recovery_chain", []) + [
            {
                "attempt": plan.attempt_number,
                "strategy": plan.strategy,
                "category": plan.failure_category,
            }
        ]
        task.description = f"[Recovery #{plan.attempt_number}: {plan.strategy}] {task.title}"
        task.error = None
