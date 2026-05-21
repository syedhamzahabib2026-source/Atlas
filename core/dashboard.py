"""
Terminal runtime dashboard (Phase 8).

TODO: web UI / distributed worker view
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.config import AtlasConfig
    from core.runtime_state import RuntimeState
    from core.task_manager import TaskManager


def render_dashboard(
    config: AtlasConfig,
    task_manager: TaskManager,
    runtime_state: RuntimeState | None = None,
    *,
    tmux_sessions: list[str] | None = None,
) -> str:
    """Return formatted operational dashboard text."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "",
        "=" * 60,
        f"  ATLAS RUNTIME DASHBOARD  |  {now}",
        "=" * 60,
        "",
    ]

    if runtime_state:
        lines.extend([
            f"  Boot #{runtime_state.boot_count}  |  "
            f"Graceful last shutdown: {runtime_state.last_shutdown_graceful}",
            "",
        ])

    # Tasks by status
    by_status: dict[str, int] = {}
    for t in task_manager.list_all():
        by_status[t.status.value] = by_status.get(t.status.value, 0) + 1

    lines.append("  TASKS")
    lines.append("  " + "-" * 40)
    for status, count in sorted(by_status.items()):
        lines.append(f"    {status:16} {count}")
    lines.append(
        f"    {'active':16} {sum(1 for t in task_manager.list_all() if t.status in task_manager.ACTIVE_STATUSES)}"
    )

    # Active task detail
    active = [
        t for t in task_manager.list_all()
        if t.status in task_manager.ACTIVE_STATUSES
    ]
    if active:
        lines.extend(["", "  ACTIVE TASKS", "  " + "-" * 40])
        for t in active[:8]:
            health = t.metadata.get("health_score", "n/a")
            strat = t.metadata.get("recovery_strategy", "-")
            lines.append(
                f"    {t.id[:8]} | {t.status.value:12} | "
                f"h={health} | {strat} | {t.title[:28]}"
            )

    # Projects
    from core.project_scheduler import ProjectScheduler

    sched = ProjectScheduler(config.runtime.per_project_max_concurrent)
    summary = sched.project_summary(task_manager.list_all())
    if summary:
        lines.extend(["", "  PROJECT QUEUES", "  " + "-" * 40])
        for pid, counts in sorted(summary.items()):
            lines.append(
                f"    {pid:20} pending={counts['pending']} "
                f"active={counts['active']} blocked={counts['blocked']}"
            )

    # tmux
    if tmux_sessions is not None:
        lines.extend(["", "  TMUX SESSIONS", "  " + "-" * 40])
        if tmux_sessions:
            for s in tmux_sessions[:10]:
                lines.append(f"    {s}")
        else:
            lines.append("    (none)")

    # Recovery / git hints on active tasks
    lines.extend(["", "  RECOVERY / SAFETY", "  " + "-" * 40])
    for t in task_manager.list_all():
        if not t.metadata.get("attempt_history") and not t.metadata.get("git"):
            continue
        attempts = len(t.metadata.get("attempt_history", []))
        rollbacks = len(t.metadata.get("git", {}).get("rollback_history", []))
        if attempts or rollbacks:
            lines.append(
                f"    {t.id[:8]} attempts={attempts} rollbacks={rollbacks}"
            )

    lines.extend(["", "=" * 60, ""])
    return "\n".join(lines)


def print_dashboard(*args, **kwargs) -> None:
    print(render_dashboard(*args, **kwargs))
