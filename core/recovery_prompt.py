"""
Intelligent retry prompt builder — root-cause oriented, not blind patching.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.attempt_history import AttemptHistory
from core.failure_classifier import FailureCategory
from core.recovery_strategies import STRATEGY_GUIDANCE, RecoveryStrategy

if TYPE_CHECKING:
    from core.task_manager import Task
    from core.task_result import TaskResult


def build_retry_prompt(
    task: Task,
    result: TaskResult,
    *,
    strategy: RecoveryStrategy,
    category: FailureCategory,
    contradiction_summary: str | None = None,
    investigation_report: str | None = None,
) -> str:
    """
    Build a Claude Code prompt that forces reasoning about past failures.
    """
    original = task.metadata.get("original_prompt") or task.metadata.get("prompt") or task.description
    attempts = AttemptHistory.load(task)

    op_ctx = task.metadata.get("operational_context", "")
    lines = [
        "# Atlas adaptive recovery",
        "",
    ]
    if op_ctx:
        lines.extend([op_ctx, ""])
    lines.extend([
        "You are continuing work on a task that FAILED verification or execution.",
        "Do NOT repeat the same fix that already failed. Analyze root cause first.",
        "",
        "## Original goal",
        original,
        "",
        "## Latest failure",
        f"- Summary: {result.summary}",
        f"- Errors: {', '.join(result.errors) or 'none'}",
        f"- Category (heuristic): {category.value}",
        "",
        "## Previous attempts (do not repeat blindly)",
    ])

    if not attempts:
        lines.append("- (no prior attempts recorded)")
    else:
        for rec in attempts[-6:]:
            lines.append(
                f"- #{rec.attempt_number} strategy={rec.strategy} outcome={rec.outcome} "
                f"hypothesis={rec.root_cause_hypothesis or 'n/a'}: {rec.error_summary[:200]}"
            )

    lines.extend([
        "",
        "## New recovery strategy (required approach)",
        f"**{strategy.value}**",
        STRATEGY_GUIDANCE.get(strategy, ""),
        "",
    ])

    if contradiction_summary:
        lines.extend([
            "## Contradiction detected",
            contradiction_summary,
            "Current assumptions may be WRONG. Question prior diagnoses.",
            "",
        ])

    if investigation_report:
        lines.extend([
            "## Investigation findings (read before coding)",
            investigation_report,
            "",
        ])

    browser = (result.metadata or {}).get("browser", {})
    if browser:
        lines.extend([
            "## Browser verification diagnostics",
            f"- URL: {browser.get('final_url', 'n/a')}",
            f"- DOM: {browser.get('dom_summary', 'n/a')}",
        ])
        if browser.get("network_failures"):
            lines.append(f"- Network: {browser['network_failures'][:5]}")
        if browser.get("console_logs"):
            lines.append("- Console (tail):")
            for log in browser["console_logs"][-8:]:
                lines.append(f"  - {log}")
        lines.append("")

    lines.extend([
        "## Instructions",
        "1. State your root-cause hypothesis in 2-3 sentences.",
        "2. Explain why previous strategies likely failed.",
        "3. Apply a fundamentally different fix aligned with the strategy above.",
        "4. List files you will modify and why.",
        "5. Do not apply cosmetic patches if diagnostics point to backend/auth.",
        "",
        f"Task ID: {task.id}",
    ])

    return "\n".join(lines)
