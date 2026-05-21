"""
Proactive task suggestions from operational memory (Phase 12).

Scans risk zones, failure patterns, and global warnings to surface
actionable suggestions before problems become blocked tasks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.logger import get_logger
from memory.operational_memory import OperationalMemoryStore

logger = get_logger("core.scanner")


@dataclass
class Suggestion:
    project_id: str
    priority: str  # "high" | "medium"
    reason: str
    prompt: str    # everything after /atlas start — e.g. "--project foo fix the thing"


class TaskScanner:
    """Scan operational memory for signals that warrant proactive action."""

    def __init__(self, store: OperationalMemoryStore) -> None:
        self.store = store

    async def scan_project(self, project_id: str, project_name: str) -> list[Suggestion]:
        """Return up to 5 prioritised suggestions for a single project."""
        suggestions: list[Suggestion] = []

        # Signal 1: high-risk zones (score >= 60)
        zones = await self.store.get_high_risk_zones(project_id, min_score=60, limit=5)
        for zone in zones:
            priority = "high" if zone.risk_score >= 75 else "medium"
            suggestions.append(Suggestion(
                project_id=project_id,
                priority=priority,
                reason=(
                    f"`{zone.path}` ({zone.zone_type}) {zone.risk_level.value.upper()} risk "
                    f"— score {zone.risk_score:.0f}, {zone.failure_count} failure(s), "
                    f"{zone.rollback_count} rollback(s)"
                ),
                prompt=(
                    f"--project {project_name} audit and stabilise {zone.path} "
                    f"— {zone.failure_count} recorded failure(s) and "
                    f"{zone.rollback_count} rollback(s)"
                ),
            ))

        # Signal 2: recurring failure patterns (count >= 3)
        patterns = await self.store.get_recurring_patterns(project_id, min_count=3, limit=5)
        for p in patterns:
            suggestions.append(Suggestion(
                project_id=project_id,
                priority="medium",
                reason=(
                    f"Recurring `{p.category}` failure ({p.occurrence_count}×): "
                    f"{p.signature[:80]}"
                ),
                prompt=(
                    f"--project {project_name} investigate and fix recurring "
                    f"{p.category} failure: {p.signature[:60]}"
                ),
            ))

        # Signal 3: global warnings tagged "failure" (from LessonExtractor Rule 2)
        global_warnings = await self.store.get_global_lessons(
            topics=["failure"], limit=3
        )
        for warning in global_warnings:
            suggestions.append(Suggestion(
                project_id=project_id,
                priority="medium",
                reason=f"Global warning: {warning[:120]}",
                prompt=(
                    f"--project {project_name} apply global learning: {warning[:80]}"
                ),
            ))

        # High priority first, then cap at 5
        suggestions.sort(key=lambda s: 0 if s.priority == "high" else 1)
        return suggestions[:5]


def format_suggestions(
    suggestions: list[Suggestion],
    *,
    scanned: list[str],
) -> str:
    """Format a suggestion list as a Slack message block."""
    project_label = ", ".join(f"`{p}`" for p in scanned)
    if not suggestions:
        return (
            f"*Atlas proactive scan* — {project_label}\n"
            f"✅ No issues detected. Memory looks healthy."
        )

    lines = [f"*Atlas proactive scan* — {project_label}"]
    for s in suggestions[:5]:
        icon = "\U0001f534" if s.priority == "high" else "\U0001f7e1"
        label = "HIGH" if s.priority == "high" else "MEDIUM"
        lines.append(f"\n{icon} *{label}* — `{s.project_id}`")
        lines.append(f"  *Reason:* {s.reason}")
        lines.append(f"  *Run:* `/atlas start {s.prompt}`")

    total = len(suggestions)
    lines.append(
        f"\n_{total} suggestion{'s' if total != 1 else ''} found. "
        f"Copy any command above to act on it._"
    )
    return "\n".join(lines)
