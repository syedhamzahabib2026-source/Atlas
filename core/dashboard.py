"""
Terminal runtime dashboard (Phase 8).

TODO: web UI / distributed worker view
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.approval_engine import ApprovalEngine
    from core.config import AtlasConfig
    from core.deployment_manager import DeploymentManager
    from core.runtime_state import RuntimeState
    from core.task_manager import TaskManager
    from core.worker_pool_manager import WorkerPoolManager


def render_dashboard(
    config: AtlasConfig,
    task_manager: TaskManager,
    runtime_state: RuntimeState | None = None,
    *,
    tmux_sessions: list[str] | None = None,
    worker_pools: WorkerPoolManager | None = None,
    approval_engine: ApprovalEngine | None = None,
    deployment_manager: DeploymentManager | None = None,
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

    # Worker pools (Phase 13)
    if worker_pools and worker_pools.enabled:
        lines.extend(["", "  WORKER POOLS", "  " + "-" * 40])
        for snap in worker_pools.snapshots(task_manager):
            cooldown = ""
            if snap.cooldown_until:
                import time

                remaining = max(0, int(snap.cooldown_until - time.time()))
                cooldown = f" | cooldown {remaining}s"
            lines.append(
                f"    {snap.pool_id:14} {snap.health:10} "
                f"busy={snap.busy_workers}/{snap.max_workers} "
                f"avail={snap.available}{cooldown}"
            )
            if snap.queued_hint:
                lines.append(f"      queued: {snap.queued_hint}")
            if snap.cooldown_reason:
                lines.append(f"      reason: {snap.cooldown_reason[:50]}")
            if snap.pool_id == "local" and not snap.available:
                lines.append("      (placeholder — not implemented)")

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

    # Approval governance (Phase 14)
    waiting = task_manager.list_waiting_approval()
    rejected = task_manager.list_rejected()
    pending_pr = [
        t for t in task_manager.list_all()
        if t.metadata.get("pr_candidate") or t.metadata.get("pr_url")
    ]
    lines.extend(["", "  APPROVAL / PR GOVERNANCE", "  " + "-" * 40])
    lines.append(f"    {'waiting_approval':18} {len(waiting)}")
    lines.append(f"    {'rejected':18} {len(rejected)}")
    lines.append(f"    {'pr_candidates':18} {len(pending_pr)}")
    if approval_engine:
        lines.append(f"    {'approval_queue':18} {approval_engine.pending_count()}")
    for t in waiting[:5]:
        risk = (t.metadata.get("risk") or {}).get("risk_level", "?")
        phase = t.metadata.get("approval_phase", "?")
        lines.append(f"    {t.id[:8]} | risk={risk} | phase={phase} | {t.title[:24]}")
    for t in rejected[:3]:
        lines.append(f"    {t.id[:8]} | REJECTED | {t.title[:28]}")

    # Delivery / deployment (Phase 15)
    from core.deployment_state import DELIVERY_STATUSES
    from core.task_manager import TaskStatus

    delivery_tasks = [t for t in task_manager.list_all() if t.status in DELIVERY_STATUSES]
    lines.extend(["", "  DELIVERY / DEPLOYMENT", "  " + "-" * 40])
    lines.append(f"    {'in_pipeline':18} {len(delivery_tasks)}")
    ci_running = sum(1 for t in delivery_tasks if t.status.value in ("ci_running", "ci_pending"))
    staging = sum(
        1
        for t in delivery_tasks
        if t.status.value in ("staging_deploying", "staging_verifying", "staging_failed")
    )
    prod_wait = sum(1 for t in delivery_tasks if t.status == TaskStatus.PRODUCTION_PENDING_APPROVAL)
    deployed = sum(1 for t in delivery_tasks if t.status == TaskStatus.DEPLOYED)
    rolled = sum(1 for t in delivery_tasks if t.status == TaskStatus.ROLLED_BACK)
    lines.append(f"    {'ci_active':18} {ci_running}")
    lines.append(f"    {'staging':18} {staging}")
    lines.append(f"    {'prod_approval':18} {prod_wait}")
    lines.append(f"    {'deployed':18} {deployed}")
    lines.append(f"    {'rolled_back':18} {rolled}")
    for t in delivery_tasks[:6]:
        dep = t.metadata.get("deployment", {})
        ci = dep.get("ci", {})
        lines.append(
            f"    {t.id[:8]} | {t.status.value:22} | "
            f"risk={dep.get('deployment_risk', '?')} | "
            f"ci={ci.get('build', '?')}/{ci.get('test', '?')}"
        )

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
