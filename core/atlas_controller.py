"""
Atlas control API — used by Slack (and future remote interfaces).

Bridges inbound commands to task manager, tmux, and orchestrator lifecycle.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from core.blocked import block_reason_label, detect_block
from core.logger import get_logger
from core.task_manager import Task, TaskStatus
from core.task_result import ResultStatus, TaskResult
if TYPE_CHECKING:
    from core.approval_engine import ApprovalEngine, ApprovalDecision
    from core.approval_manager import ApprovalManager
    from core.deployment_manager import DeploymentManager
    from core.orchestrator import Orchestrator
    from core.pr_manager import PRManager, PRPreparationManager

logger = get_logger("controller")


class AtlasController:
    """Remote control surface for Atlas."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        approval_engine: ApprovalEngine | None = None,
        approval_manager: ApprovalManager | None = None,
        pr_manager: PRManager | None = None,
        pr_preparation: PRPreparationManager | None = None,
        deployment_manager: DeploymentManager | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.tasks = orchestrator.tasks
        self.tmux = orchestrator.tmux
        self.config = orchestrator.config
        self.slack = orchestrator.slack
        self.approval_engine = approval_engine or (
            approval_manager.engine if approval_manager else None
        )
        self.approval_manager = approval_manager
        self.pr_manager = pr_manager
        self.pr_preparation = pr_preparation
        self.deployment_manager = deployment_manager

    def _resolve_task_id(self, token: str | None) -> Task | None:
        if not token:
            running = self.tasks.list_by_status(TaskStatus.RUNNING)
            if len(running) == 1:
                return running[0]
            return None
        task = self.tasks.get(token)
        if task:
            return task
        token = token.lower()
        for t in self.tasks.list_all():
            if t.id.lower().startswith(token):
                return t
        return None

    async def handle_command(
        self,
        text: str,
        *,
        user_id: str,
        channel_id: str,
        thread_ts: str | None = None,
    ) -> str:
        """Execute a parsed /atlas command and return Slack-visible text."""
        from slack.commands import parse_atlas_command

        parsed = parse_atlas_command(text)
        sub = parsed.subcommand

        if sub == "help":
            return self._help_text()

        if sub == "start":
            logger.info("START args: %s", parsed.args)
            args = list(parsed.args)
            project_name: str | None = None
            if "--project" in args:
                idx = args.index("--project")
                if idx + 1 < len(args):
                    project_name = args[idx + 1]
                    args = args[:idx] + args[idx + 2:]
                else:
                    return "Usage: `/atlas start --project <name> <prompt>`"

            prompt = " ".join(args).strip()
            if not prompt:
                return "Usage: `/atlas start [--project <name>] <prompt>`"

            task = await self.start_task(
                prompt,
                project_name=project_name,
                slack_user_id=user_id,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts,
            )
            reply = (
                f"Task queued\n"
                f"• id: `{task.id}`\n"
                f"• status: `{task.status.value}`\n"
                f"• project: `{task.project_id}`"
            )
            if project_name:
                wd = task.metadata.get("working_dir", "not found in config")
                reply += f"\n• working_dir: `{wd}`"
            return reply

        if sub == "stop":
            kill = "--kill" in parsed.args
            tid = next((a for a in parsed.args if not a.startswith("--")), None)
            return await self.stop_task(tid, kill_session=kill)

        if sub == "status":
            return self.format_status()

        if sub == "sessions":
            return await self.format_sessions()

        if sub == "logs":
            tid = parsed.args[0] if parsed.args else None
            return self.format_logs(tid)

        if sub == "approve":
            tid = parsed.args[0] if parsed.args else None
            if not tid:
                return "Usage: `/atlas approve <task_id>`"
            return await self._handle_approve(tid, user_id=user_id)

        if sub == "reject":
            if not parsed.args:
                return "Usage: `/atlas reject <task_id> <reason>`"
            tid = parsed.args[0]
            reason = " ".join(parsed.args[1:]).strip()
            if not reason:
                return "Usage: `/atlas reject <task_id> <reason>` — reason is required"
            return await self._handle_reject(tid, reason, user_id=user_id)

        if sub == "request_changes":
            if len(parsed.args) < 2:
                return "Usage: `/atlas request_changes <task_id> <feedback>`"
            tid = parsed.args[0]
            feedback = " ".join(parsed.args[1:]).strip()
            return await self._handle_request_changes(tid, feedback, user_id=user_id)

        if sub == "scan":
            project_filter = parsed.args[0] if parsed.args else None
            return await self._handle_scan(project_filter)

        if sub in ("approve_deploy", "approve-deploy"):
            tid = parsed.args[0] if parsed.args else None
            if not tid:
                return "Usage: `/atlas approve_deploy <task_id>`"
            return await self._handle_approve_deploy(tid, user_id=user_id)

        if sub in ("rollback_release", "rollback-release"):
            if len(parsed.args) < 2:
                return "Usage: `/atlas rollback_release <task_id> <reason>`"
            tid = parsed.args[0]
            reason = " ".join(parsed.args[1:]).strip()
            return await self._handle_rollback_release(tid, reason, user_id=user_id)

        if sub in ("deployment_status", "deployment-status", "deploy_status"):
            tid = parsed.args[0] if parsed.args else None
            return self._handle_deployment_status(tid)

        return f"Unknown subcommand `{sub}`. Try `/atlas help`."

    def _help_text(self) -> str:
        return (
            "*Atlas commands*\n"
            "• `/atlas start [--project <name>] <prompt>` — queue a Claude Code task\n"
            "• `/atlas stop [task_id] [--kill]` — cancel running task\n"
            "• `/atlas approve <task_id>` — approve changes (merge PR if prepared)\n"
            "• `/atlas reject <task_id> <reason>` — reject and stop\n"
            "• `/atlas request_changes <task_id> <feedback>` — send back for rework\n"
            "• `/atlas scan [project]` — proactive memory scan; suggest tasks to run\n"
            "• `/atlas status` — tasks overview\n"
            "• `/atlas sessions` — tmux sessions + tasks\n"
            "• `/atlas logs [task_id]` — recent Atlas log tail\n"
            "• `/atlas approve_deploy <task_id>` — approve production deployment\n"
            "• `/atlas rollback_release <task_id> <reason>` — rollback release\n"
            "• `/atlas deployment_status [task_id]` — delivery / CI status\n"
            "\n_Reply in a blocked task thread to unblock Atlas._"
        )

    async def _handle_scan(self, project_filter: str | None = None) -> str:
        """Run an on-demand memory scan and return formatted suggestions."""
        if not self.orchestrator.memory_coordinator:
            return "Memory not active — enable `memory.enabled` in config."
        return await self.orchestrator.run_scanner(project_filter)

    async def _handle_approve(self, task_id: str, *, user_id: str | None = None) -> str:
        from core.approval_engine import ApprovalDecision

        engine = self.approval_engine
        if not engine:
            return "Approval engine not configured."

        pending = engine.get_pending(task_id)
        task = self._resolve_task_id(pending.task_id if pending else task_id)
        if not task:
            return f"No task found for `{task_id}`."

        phase = task.metadata.get("approval_phase", "pr_creation")
        engine.apply_decision_to_task(
            task, ApprovalDecision.APPROVE, user_id=user_id
        )
        engine.clear_pending(task.id)
        if self.approval_manager:
            self.approval_manager.clear_approval(task.id)

        pr_number = task.metadata.get("pr_number") or (pending.pr_number if pending else None)

        if phase == "pre_execution":
            task.metadata["approval_granted"] = True
            self.tasks.update_status(task.id, TaskStatus.PENDING, error=None)
            msg = f"✅ Task `{task.id[:8]}` approved — resuming execution."
            if self.slack:
                await self.slack.notify_task_resumed_after_approval(task)
            logger.info("Task %s approved for pre-execution resume", task.id[:8])
            return msg

        if pr_number and self.pr_manager:
            merge = await self.pr_manager.merge_pr(pr_number)
            if not merge.success:
                msg = f"PR merge failed for `{task.id[:8]}`: {merge.error}"
                if self.slack:
                    await self.slack.notify(msg)
                return msg
            self.tasks.update_status(task.id, TaskStatus.MERGED)
            msg = f"✅ PR #{pr_number} merged — task `{task.id[:8]}` complete."
            if self.orchestrator.deployment_manager and self.config.deployment.enabled:
                started = await self.orchestrator.start_delivery_for_task(task)
                if started:
                    msg += "\n🚀 Delivery pipeline started (CI pending)."
        else:
            self.tasks.update_status(task.id, TaskStatus.COMPLETED)
            msg = f"✅ Task `{task.id[:8]}` approved — marked complete."

        if self.slack:
            await self.slack.notify(msg)
        logger.info("Task %s approved (phase=%s)", task.id[:8], phase)
        return msg

    async def _handle_approve_deploy(self, task_id: str, *, user_id: str | None = None) -> str:
        dm = self.deployment_manager
        if not dm:
            return "Deployment manager not configured."
        task = self._resolve_task_id(task_id)
        if not task:
            return f"No task found for `{task_id}`."
        step = await dm.approve_production_deploy(task, user_id=user_id)
        await self.tasks.persist(task)
        await self.orchestrator._handle_deployment_step(task, step)
        return f"✅ Production deploy approved for `{task.id[:8]}` — {step.message}"

    async def _handle_rollback_release(
        self, task_id: str, reason: str, *, user_id: str | None = None
    ) -> str:
        dm = self.deployment_manager
        if not dm:
            return "Deployment manager not configured."
        task = self._resolve_task_id(task_id)
        if not task:
            return f"No task found for `{task_id}`."
        step = await dm.rollback_release(task, reason)
        await self.tasks.persist(task)
        await self.orchestrator._handle_deployment_step(task, step)
        return f"↩️ Rollback for `{task.id[:8]}`: {step.message}"

    def _handle_deployment_status(self, task_id: str | None) -> str:
        dm = self.deployment_manager
        if not dm:
            return "Deployment manager not configured."
        if task_id:
            task = self._resolve_task_id(task_id)
            if not task:
                return f"No task found for `{task_id}`."
            return f"*Deployment status — `{task.id[:8]}`*\n{dm.format_deployment_status(task)}"
        lines = ["*Deployment queue*"]
        for t in self.tasks.list_in_delivery()[:10]:
            dep = t.metadata.get("deployment", {})
            lines.append(
                f"• `{t.id[:8]}` | {t.status.value} | risk={dep.get('deployment_risk', '?')}"
            )
        if len(lines) == 1:
            return "No tasks in delivery pipeline."
        return "\n".join(lines)

    async def _handle_reject(self, task_id: str, reason: str, *, user_id: str | None = None) -> str:
        from core.approval_engine import ApprovalDecision

        engine = self.approval_engine
        if not engine:
            return "Approval engine not configured."

        pending = engine.get_pending(task_id)
        task = self._resolve_task_id(pending.task_id if pending else task_id)
        if not task:
            return f"No task found for `{task_id}`."

        engine.apply_decision_to_task(
            task, ApprovalDecision.REJECT, user_id=user_id, notes=reason
        )
        engine.clear_pending(task.id)
        if self.approval_manager:
            self.approval_manager.clear_approval(task.id)

        task.metadata["rejection_reason"] = reason
        self.tasks.update_status(task.id, TaskStatus.REJECTED, error=reason)

        msg = f"❌ Approval rejected for task `{task.id[:8]}`: {reason}"
        if self.slack:
            await self.slack.notify_approval_rejected(task, reason)
        logger.info("Task %s rejected: %s", task.id[:8], reason)
        return msg

    async def _handle_request_changes(
        self, task_id: str, feedback: str, *, user_id: str | None = None
    ) -> str:
        from core.approval_engine import ApprovalDecision

        engine = self.approval_engine
        if not engine:
            return "Approval engine not configured."

        pending = engine.get_pending(task_id)
        task = self._resolve_task_id(pending.task_id if pending else task_id)
        if not task:
            return f"No task found for `{task_id}`."

        engine.apply_decision_to_task(
            task, ApprovalDecision.REQUEST_CHANGES, user_id=user_id, notes=feedback
        )
        engine.clear_pending(task.id)
        if self.approval_manager:
            self.approval_manager.clear_approval(task.id)

        base_prompt = task.metadata.get("prompt") or task.description
        task.metadata["prompt"] = (
            f"{base_prompt}\n\n"
            f"Reviewer requested changes:\n{feedback}\n"
            f"Please address this feedback and try again."
        )
        task.metadata["change_request_feedback"] = feedback
        task.metadata["failure_count"] = 0
        task.metadata["approval_granted"] = True
        self.tasks.update_status(task.id, TaskStatus.PENDING, error=None)

        msg = f"↩️ Task `{task.id[:8]}` sent back for changes: {feedback}"
        if self.slack:
            await self.slack.notify(msg)
        logger.info("Task %s request_changes: %s", task.id[:8], feedback[:80])
        return msg

    async def start_task(
        self,
        prompt: str,
        *,
        project_id: str | None = None,
        project_name: str | None = None,
        slack_user_id: str | None = None,
        slack_channel_id: str | None = None,
        slack_thread_ts: str | None = None,
    ) -> Task:
        """Create a pending task for the orchestrator loop."""
        pid = project_id or project_name or f"slack-{slack_user_id or 'remote'}"
        title = prompt[:80] + ("..." if len(prompt) > 80 else "")

        metadata: dict = {
            "agent": "claude_code",
            "prompt": prompt,
            "source": "slack",
            "slack_user_id": slack_user_id,
            "slack_channel_id": slack_channel_id,
            "slack_thread_ts": slack_thread_ts,
        }

        if project_name:
            proj_cfg = self.config.projects.get(project_name)
            if proj_cfg:
                metadata["working_dir"] = proj_cfg.repo_path
                logger.info(
                    "Task %s → project %r at %s",
                    pid, project_name, proj_cfg.repo_path,
                )
            else:
                logger.warning(
                    "Project %r not found in config — known: %s",
                    project_name,
                    list(self.config.projects),
                )

        task = self.tasks.create(
            title=title,
            description=prompt,
            project_id=pid,
            metadata=metadata,
        )
        logger.info("Task created via Slack: %s (project=%s)", task.id[:8], pid)
        return task

    async def stop_task(
        self,
        task_id: str | None = None,
        *,
        kill_session: bool = False,
    ) -> str:
        """Gracefully stop a running task."""
        task = self._resolve_task_id(task_id)
        if not task:
            return "No matching task. Use `/atlas status` or `/atlas stop <task_id>`."

        if task.status not in (TaskStatus.RUNNING, TaskStatus.PENDING, TaskStatus.BLOCKED):
            return f"Task `{task.id[:8]}` is already `{task.status.value}`."

        self.orchestrator.request_cancel(task.id)
        session = task.session_name

        if kill_session and session and self.tmux:
            await self.tmux.kill_session(session)
            logger.info("Killed tmux session %s for stop", session)

        if task.status == TaskStatus.PENDING:
            self.tasks.update_status(task.id, TaskStatus.CANCELLED)
        else:
            self.tasks.update_status(task.id, TaskStatus.CANCELLED, error="stopped via Slack")

        if self.slack:
            await self.slack.notify_task_stopped(task)

        return (
            f"Stop requested for `{task.id[:8]}`\n"
            f"• session: `{session or 'n/a'}`\n"
            f"• kill_session: `{kill_session}`"
        )

    def format_status(self) -> str:
        lines = ["*Atlas status*"]
        by_status: dict[str, list[Task]] = {}
        for task in sorted(self.tasks.list_all(), key=lambda t: t.created_at, reverse=True):
            by_status.setdefault(task.status.value, []).append(task)

        if not by_status:
            return "No tasks yet. Use `/atlas start <prompt>`."

        from slack.commands import format_duration

        for status, tasks in by_status.items():
            lines.append(f"\n*{status}* ({len(tasks)})")
            for t in tasks[:8]:
                sid = t.session_name or "n/a"
                dur = format_duration(t.started_at)
                lines.append(
                    f"• `{t.id[:8]}` | {t.title[:40]} | session `{sid}` | running {dur}"
                )
        return "\n".join(lines)

    async def format_sessions(self) -> str:
        lines = ["*Atlas sessions*"]
        if not self.tmux or not self.tmux.is_available():
            lines.append("_tmux unavailable on this host._")
        else:
            names = await self.tmux.list_sessions()
            if not names:
                lines.append("_No active tmux sessions._")
            for name in names:
                lines.append(f"• tmux: `{name}`")

        lines.append("\n*Task ↔ session map*")
        active = [
            t
            for t in self.tasks.list_all()
            if t.status in (TaskStatus.RUNNING, TaskStatus.PENDING, TaskStatus.BLOCKED)
        ]
        if not active:
            lines.append("_No active tasks._")
        else:
            from slack.commands import format_duration

            for t in active:
                lines.append(
                    f"• `{t.id[:8]}` | `{t.status.value}` | "
                    f"session `{t.session_name or 'pending'}` | "
                    f"{format_duration(t.started_at)}"
                )
        return "\n".join(lines)

    def format_logs(self, task_id: str | None = None) -> str:
        log_path = self.config.log_dir / "atlas.log"
        if not log_path.exists():
            return "No log file yet."

        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-self.config.slack.log_tail_lines :]

        header = "*Atlas logs* (tail)"
        if task_id:
            task = self._resolve_task_id(task_id)
            if task:
                short = task.id[:8]
                filtered = [ln for ln in tail if short in ln]
                if filtered:
                    tail = filtered[-20:]
                    header = f"*Logs for task `{short}`*"
                result = task.metadata.get("result", {})
                if result.get("raw_output"):
                    preview = result["raw_output"][-600:]
                    return f"{header}\n```\n{preview}\n```"

        body = "\n".join(tail[-20:])
        if len(body) > 2800:
            body = body[-2800:]
        return f"{header}\n```\n{body}\n```"

    async def handle_thread_reply(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        text: str,
        user_id: str,
    ) -> bool:
        """
        If this thread is a blocked task, apply human response and re-queue.

        Returns True if handled.
        """
        task = self.tasks.find_blocked_by_thread(channel_id, thread_ts)
        if not task:
            return False

        await self.unblock_task(task, text.strip(), user_id=user_id)
        return True

    async def unblock_task(
        self,
        task: Task,
        response: str,
        *,
        user_id: str | None = None,
    ) -> None:
        """Resume a BLOCKED task with human input."""
        if not response:
            return

        task.metadata["human_response"] = response
        task.metadata["unblocked_by"] = user_id
        task.metadata["unblocked_at"] = datetime.now(timezone.utc).isoformat()
        task.metadata.pop("blocked_question", None)
        base = task.metadata.get("prompt") or task.description
        task.metadata["prompt"] = f"{base}\n\nHuman clarification:\n{response}"
        task.metadata["failure_count"] = 0
        self.tasks.update_status(task.id, TaskStatus.PENDING, error=None)
        logger.info("Task %s unblocked via Slack", task.id[:8])

        if self.slack:
            await self.slack.notify(
                f"Task `{task.id[:8]}` unblocked. Resuming with your reply.",
                thread_ts=task.metadata.get("slack_thread_ts"),
            )

    async def enter_blocked(
        self,
        task: Task,
        reason: str,
        question: str,
    ) -> None:
        """Mark task BLOCKED and ask via Slack."""
        self.tasks.update_status(task.id, TaskStatus.BLOCKED, error=reason)
        task.metadata["blocked_reason"] = reason
        task.metadata["blocked_question"] = question

        if not task.metadata.get("slack_channel_id"):
            task.metadata["slack_channel_id"] = self.config.slack.channel_id

        if self.slack:
            parent_ts = await self.slack.ask_question(
                task,
                f"*Atlas blocked* ({block_reason_label(reason)})\n{question}",
            )
            if parent_ts:
                task.metadata["slack_thread_ts"] = parent_ts

        logger.info("Task %s blocked: %s", task.id[:8], reason)
