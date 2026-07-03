"""
Slack remote control for Atlas (Phase 3).

Socket Mode + Bolt async app. Slash command /atlas and thread replies for BLOCKED tasks.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

from core.config import SlackConfig
from core.logger import get_logger
from slack.commands import parse_atlas_command
from slack.security import is_authorized

if TYPE_CHECKING:
    from core.approval_engine import ApprovalContext
    from core.atlas_controller import AtlasController
    from core.task_manager import Task

logger = get_logger("slack")


class SlackBot:
    """Slack control plane: inbound commands and outbound task notifications."""

    def __init__(self, config: SlackConfig) -> None:
        self.config = config
        self._app: AsyncApp | None = None
        self._client: AsyncWebClient | None = None
        self._handler: AsyncSocketModeHandler | None = None
        self._handler_task: asyncio.Task | None = None
        self._controller: AtlasController | None = None

    def bind(self, controller: AtlasController) -> None:
        self._controller = controller

    async def connect(self) -> None:
        if not self.config.bot_token or not self.config.app_token:
            raise ValueError("SLACK_BOT_TOKEN and SLACK_APP_TOKEN required")

        self._client = AsyncWebClient(token=self.config.bot_token)
        self._app = AsyncApp(token=self.config.bot_token, client=self._client)
        self._register_handlers()

        self._handler = AsyncSocketModeHandler(self._app, self.config.app_token)
        self._handler_task = asyncio.create_task(self._handler.start_async())
        logger.info("Slack Socket Mode connected")

    def _register_handlers(self) -> None:
        assert self._app is not None

        @self._app.command("/atlas")
        async def atlas_slash(ack, command, respond, logger):  # noqa: ARG001
            await ack()
            await self._dispatch_command(
                text=command.get("text", ""),
                user_id=command.get("user_id"),
                channel_id=command.get("channel_id"),
                respond=respond,
            )

        @self._app.event("message")
        async def on_message(event, say, logger):  # noqa: ARG001
            if event.get("subtype"):
                return

            channel_id = event.get("channel")
            user_id = event.get("user")
            text = (event.get("text") or "").strip()

            # Thread replies to unblock tasks
            if event.get("thread_ts"):
                if text.startswith("/atlas"):
                    return

                allowed, msg = is_authorized(
                    self.config, user_id=user_id, channel_id=channel_id
                )
                if not allowed:
                    return

                if not self._controller:
                    return

                handled = await self._controller.handle_thread_reply(
                    channel_id=channel_id,
                    thread_ts=event.get("thread_ts"),
                    text=text,
                    user_id=user_id,
                )
                if handled:
                    logger.info("Thread reply applied for blocked task")
                return

            # DM to the bot (requires Messages Tab enabled in Slack app settings)
            channel_type = event.get("channel_type")
            is_dm = channel_type == "im" or (
                channel_id and str(channel_id).startswith("D")
            )
            if not is_dm or not text or text.startswith("/"):
                return

            allowed, deny = is_authorized(
                self.config, user_id=user_id, channel_id=channel_id
            )
            if not allowed:
                await say(deny)
                return
            if not self._controller:
                await say("Atlas controller not ready.")
                return

            reply = await self._controller.handle_command(
                text,
                user_id=user_id or "",
                channel_id=channel_id or "",
            )
            await say(reply)
            logger.info("Handled Atlas DM command from %s", user_id)

        @self._app.event("app_mention")
        async def on_mention(event, say, logger):  # noqa: ARG001
            text = event.get("text", "")
            # Strip bot mention
            parts = text.split(maxsplit=1)
            body = parts[1] if len(parts) > 1 else ""
            parsed = parse_atlas_command(body)
            if parsed.subcommand == "help" and not body:
                body = "help"
            channel_id = event.get("channel")
            user_id = event.get("user")
            thread_ts = event.get("thread_ts") or event.get("ts")

            allowed, deny = is_authorized(
                self.config, user_id=user_id, channel_id=channel_id
            )
            if not allowed:
                await say(deny)
                return
            if not self._controller:
                await say("Atlas controller not ready.")
                return

            reply = await self._controller.handle_command(
                body or "help",
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )
            await say(reply, thread_ts=thread_ts)

    async def _dispatch_command(
        self,
        *,
        text: str,
        user_id: str | None,
        channel_id: str | None,
        respond,
    ) -> None:
        allowed, deny = is_authorized(
            self.config, user_id=user_id, channel_id=channel_id
        )
        if not allowed:
            await respond(deny)
            return

        if not self._controller:
            await respond("Atlas controller not ready.")
            return

        try:
            reply = await self._controller.handle_command(
                text,
                user_id=user_id or "",
                channel_id=channel_id or "",
            )
            await respond(reply)
        except Exception as exc:
            logger.exception("Slack command failed")
            await respond(f"Atlas error: {exc}")

    async def disconnect(self) -> None:
        if self._handler:
            await self._handler.close_async()
        if self._handler_task:
            self._handler_task.cancel()
            try:
                await self._handler_task
            except asyncio.CancelledError:
                pass
        self._handler = None
        self._handler_task = None
        logger.info("Slack disconnected")

    async def notify(
        self,
        text: str,
        channel_id: str | None = None,
        thread_ts: str | None = None,
    ) -> str | None:
        """Post a message; returns ts of posted message."""
        channel = channel_id or self.config.channel_id
        if not channel or not self._client:
            logger.warning("Slack notify skipped (no channel/client)")
            return None

        kwargs: dict = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        resp = await self._client.chat_postMessage(**kwargs)
        ts = resp.get("ts")
        logger.info("Slack notify -> %s", channel)
        return ts

    async def ask_question(self, task: Task, text: str) -> str | None:
        """
        Post blocked-state question; returns thread_ts for tracking replies.

        TODO: screenshots attach to blocked messages
        """
        channel = task.metadata.get("slack_channel_id") or self.config.channel_id
        thread = task.metadata.get("slack_thread_ts")
        body = f"{text}\n\n_Task ID: `{task.id}` — reply in this thread to continue._"
        ts = await self.notify(body, channel_id=channel, thread_ts=thread)
        return thread or ts

    async def notify_task_started(self, task: Task) -> None:
        await self.notify(
            f"*Task started*\n"
            f"• id: `{task.id}`\n"
            f"• title: {task.title}\n"
            f"• session: `{task.session_name or 'starting...'}`",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_task_completed(self, task: Task, result) -> None:
        summary = result.summary if hasattr(result, "summary") else str(result)
        dur = getattr(result, "duration_sec", None) or "?"
        await self.notify(
            f"*Task completed*\n"
            f"• id: `{task.id[:8]}`\n"
            f"• summary: {summary}\n"
            f"• duration: {dur}s",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_task_failed(self, task: Task, result) -> None:
        errors = getattr(result, "errors", []) or [task.error]
        await self.notify(
            f"*Task failed*\n"
            f"• id: `{task.id[:8]}`\n"
            f"• summary: {getattr(result, 'summary', task.error)}\n"
            f"• errors: {', '.join(errors) or 'unknown'}",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_task_timeout(self, task: Task, result) -> None:
        await self.notify(
            f"*Task timed out*\n"
            f"• id: `{task.id[:8]}`\n"
            f"• {getattr(result, 'summary', 'timeout')}",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_task_blocked(self, task: Task, reason: str) -> None:
        await self.notify(
            f"*Atlas blocked*\n"
            f"• id: `{task.id[:8]}`\n"
            f"• reason: {reason}\n"
            f"_Reply in thread to continue._",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_task_stopped(self, task: Task) -> None:
        await self.notify(
            f"*Task stopped*\n• id: `{task.id[:8]}`",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def _upload_screenshot(
        self,
        path: str,
        *,
        channel_id: str | None,
        thread_ts: str | None,
        title: str,
    ) -> None:
        if not self._client:
            return
        channel = channel_id or self.config.channel_id
        if not channel:
            return
        from pathlib import Path as P

        file_path = P(path)
        if not file_path.is_file():
            logger.warning("Screenshot not found for upload: %s", path)
            return
        try:
            await self._client.files_upload_v2(
                channel=channel,
                file=str(file_path),
                title=title,
                thread_ts=thread_ts,
            )
            logger.info("Uploaded screenshot to Slack: %s", file_path.name)
        except Exception:
            logger.exception("Slack screenshot upload failed")

    async def notify_browser_failure(self, task: Task, result) -> None:
        """Failure summary + screenshot paths uploaded to Slack."""
        meta = getattr(result, "metadata", None) or {}
        browser = meta.get("browser", {})

        summary = browser.get("summary") or getattr(result, "summary", "Browser task failed")
        errors = browser.get("errors") or getattr(result, "errors", [])
        screenshots = browser.get("screenshots", [])
        network = browser.get("network_failures", [])

        text = (
            f"*Browser verification failed*\n"
            f"• id: `{task.id[:8]}`\n"
            f"• summary: {summary}\n"
            f"• errors: {', '.join(errors) or 'none'}\n"
            f"• network failures: {len(network)}\n"
            f"• screenshots: {len(screenshots)}"
        )
        channel = task.metadata.get("slack_channel_id")
        thread = task.metadata.get("slack_thread_ts")
        await self.notify(text, channel_id=channel, thread_ts=thread)

        for i, shot in enumerate(screenshots[:3]):
            await self._upload_screenshot(
                shot,
                channel_id=channel,
                thread_ts=thread,
                title=f"browser-fail-{task.id[:8]}-{i}",
            )

        # TODO: GPT/Claude vision — analyze screenshots before upload
        # TODO: visual diff vs baseline images

    async def notify_browser_success(self, task: Task, result) -> None:
        browser = result.metadata.get("browser", {})
        shots = browser.get("screenshots", [])
        text = (
            f"*Browser verification passed*\n"
            f"• id: `{task.id[:8]}`\n"
            f"• summary: {browser.get('summary', result.summary)}\n"
            f"• url: {browser.get('final_url', 'n/a')}\n"
            f"• duration: {browser.get('duration_sec', result.duration_sec)}s"
        )
        channel = task.metadata.get("slack_channel_id")
        thread = task.metadata.get("slack_thread_ts")
        await self.notify(text, channel_id=channel, thread_ts=thread)

        if shots and task.metadata.get("slack_upload_success_screenshot", True):
            await self._upload_screenshot(
                shots[-1],
                channel_id=channel,
                thread_ts=thread,
                title=f"browser-ok-{task.id[:8]}",
            )

    async def notify_recovery_retry(self, task: Task, plan) -> None:
        await self.notify(
            f"*Recovery retry #{plan.attempt_number}*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• strategy: `{plan.strategy}`\n"
            f"• category: `{plan.failure_category}`\n"
            f"• hypothesis: {plan.root_cause_hypothesis[:200]}",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_investigation_mode(self, task: Task, report: str) -> None:
        excerpt = (report or "")[:1200]
        await self.notify(
            f"*Investigation mode*\n"
            f"• task: `{task.id[:8]}`\n"
            f"Atlas paused patching to analyze root cause.\n"
            f"```\n{excerpt}\n```",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_contradiction_detected(self, task: Task, summary: str) -> None:
        await self.notify(
            f"*Contradiction detected*\n"
            f"• task: `{task.id[:8]}`\n"
            f"{summary}\n"
            f"_Assumptions may be wrong — switching strategy._",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_recovery_success(self, task: Task, message: str) -> None:
        await self.notify(
            f"*Recovery succeeded*\n• task: `{task.id[:8]}`\n{message}",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_checkpoint_created(self, task: Task, checkpoint: dict) -> None:
        await self.notify(
            f"*Checkpoint created*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• commit: `{checkpoint.get('commit_hash', '')[:8]}`\n"
            f"• strategy: `{checkpoint.get('strategy')}`\n"
            f"• attempt: `{checkpoint.get('attempt')}`",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_rollback(self, task: Task, summary: str) -> None:
        await self.notify(
            f"*Rollback triggered*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• {summary}",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_regression_detected(self, task: Task, health) -> None:
        reasons = getattr(health, "regression_reasons", []) or []
        await self.notify(
            f"*Regression detected*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• health: `{getattr(health.current, 'score', '?')}` "
            f"(delta {getattr(health, 'score_delta', 0):.0f})\n"
            f"• {reasons[0] if reasons else 'health worsened'}",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_workspace_isolated(self, task: Task, branch: str) -> None:
        await self.notify(
            f"*Workspace isolated*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• branch: `{branch}`\n"
            f"_Autonomous work will not run on main/master._",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_memory_insights(self, task: Task, insights: list[str]) -> None:
        if not insights:
            return
        body = "\n".join(f"• {i}" for i in insights[:6])
        await self.notify(
            f"*Atlas operational memory*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• project: `{task.project_id or 'default'}`\n"
            f"{body}",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_runtime_startup(self, state, report) -> None:
        """Atlas restarted — tasks restored, sessions reconnected."""
        issues = report.integrity_issues or state.integrity_issues
        text = (
            f"*Atlas restarted* (boot #{state.boot_count})\n"
            f"• tasks restored: `{report.tasks_restored}`\n"
            f"• tasks normalized: `{report.tasks_normalized}`\n"
            f"• tmux reconnected: `{len(report.sessions_reconnected)}`\n"
            f"• orphan sessions cleaned: `{len(report.sessions_cleaned)}`"
        )
        if report.sessions_orphaned:
            text += f"\n• orphan sessions found: `{len(report.sessions_orphaned)}`"
        if issues:
            text += f"\n:warning: *Integrity issues:* {', '.join(issues[:3])}"
        await self.notify(text)

    async def notify_runtime_shutdown(self, state, *, graceful: bool) -> None:
        await self.notify(
            f"*Atlas shutting down*\n"
            f"• graceful: `{graceful}`\n"
            f"• active tasks preserved: `{len(state.active_task_ids)}`"
        )

    async def notify_runtime_recovery_complete(self, report) -> None:
        await self.notify(
            f"*Runtime recovery complete*\n"
            f"• restored: `{report.tasks_restored}`\n"
            f"• normalized: `{report.tasks_normalized}`"
        )

    async def notify_integrity_failure(self, issues: list[str]) -> None:
        await self.notify(
            f":warning: *Atlas integrity check failed*\n"
            + "\n".join(f"• {i}" for i in issues[:8])
        )

    async def notify_pool_exhausted(self, message: str) -> None:
        await self.notify(f":warning: *{message}*")

    async def notify_pool_restored(self, message: str) -> None:
        await self.notify(f":white_check_mark: *{message}*")

    async def notify_pool_overloaded(self, message: str) -> None:
        await self.notify(f":warning: *{message}*")

    async def notify_pool_routing(
        self, task: Task, pool_id: str, message: str
    ) -> None:
        await self.notify(
            f"*{message}*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• pool: `{pool_id}`",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_approval_required(self, task: Task, context: ApprovalContext) -> str | None:
        """Rich approval prompt with risk, summaries, and commands."""
        engine = None
        if self._controller and self._controller.approval_engine:
            engine = self._controller.approval_engine
        text = (
            engine.format_slack_message(task, context)
            if engine
            else f"*Approval required* for task `{task.id[:8]}`"
        )
        return await self.notify(
            text,
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_approval_waiting(self, task: Task, phase: str) -> None:
        await self.notify(
            f"*Task waiting for approval*\n"
            f"• id: `{task.id[:8]}`\n"
            f"• phase: `{phase}`\n"
            f"• risk: `{(task.metadata.get('risk') or {}).get('risk_level', 'unknown')}`",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_high_risk_detected(self, task: Task, risk_level: str) -> None:
        await self.notify(
            f":warning: *High-risk change detected*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• risk: `{risk_level}`\n"
            f"_Manual review required before proceeding._",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_pr_candidate_prepared(self, task: Task, candidate) -> None:
        if not candidate:
            return
        pr_url = getattr(candidate, "pr_url", None) or task.metadata.get("pr_url")
        branch = getattr(candidate, "branch_name", None) or task.metadata.get("git", {}).get("branch")
        await self.notify(
            f"*PR candidate prepared*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• branch: `{branch}`\n"
            f"• PR: {pr_url or 'not created yet'}\n"
            f"_Awaiting approval — not auto-merged._",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_approval_rejected(self, task: Task, reason: str) -> None:
        await self.notify(
            f"*Approval rejected*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• reason: {reason[:500]}",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_task_resumed_after_approval(self, task: Task) -> None:
        await self.notify(
            f"*Task resumed after approval*\n"
            f"• id: `{task.id[:8]}`\n"
            f"• title: {task.title[:60]}",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_ci_started(self, task: Task) -> None:
        dep = task.metadata.get("deployment", {})
        await self.notify(
            f"*CI started*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• release: `{dep.get('release_id', '?')[:8]}`\n"
            f"• branch: `{dep.get('branch', 'n/a')}`",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_ci_failed(self, task: Task, reason: str) -> None:
        await self.notify(
            f"*CI failed*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• {reason[:500]}",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_staging_deployed(self, task: Task, url: str) -> None:
        await self.notify(
            f"*Staging deployed*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• url: {url}",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_staging_verification_failed(self, task: Task, reason: str) -> None:
        await self.notify(
            f"*Staging verification failed*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• {reason[:500]}",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_production_approval_required(self, task: Task) -> None:
        dep = task.metadata.get("deployment", {})
        await self.notify(
            f"*Production approval required*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• risk: `{dep.get('deployment_risk', '?')}`\n"
            f"• staging: `{dep.get('staging_url', 'n/a')}`\n"
            f"Reply: `/atlas approve_deploy {task.id[:8]}`",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_deployment_succeeded(self, task: Task) -> None:
        dep = task.metadata.get("deployment", {})
        await self.notify(
            f"*Deployment succeeded*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• production: `{dep.get('production_url', 'n/a')}`\n"
            f"• health: `{dep.get('health_status', '?')}`",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_deployment_failed(self, task: Task, reason: str) -> None:
        await self.notify(
            f"*Deployment failed*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• {reason[:500]}",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_deployment_rollback(self, task: Task, reason: str) -> None:
        await self.notify(
            f"*Rollback triggered*\n"
            f"• task: `{task.id[:8]}`\n"
            f"• {reason[:500]}",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )

    async def notify_escalation(self, task: Task, summary: str) -> None:
        await self.notify(
            f"*Atlas escalated — human input required*\n"
            f"• task: `{task.id[:8]}`\n\n{summary}",
            channel_id=task.metadata.get("slack_channel_id"),
            thread_ts=task.metadata.get("slack_thread_ts"),
        )
        browser = (task.metadata.get("result") or {}).get("browser", {})
        if not browser:
            browser = (task.metadata.get("verification") or {}).get("result", {})
            if isinstance(browser, dict):
                browser = browser.get("browser", browser)
        shots = browser.get("screenshots", []) if isinstance(browser, dict) else []
        for i, shot in enumerate(shots[:2]):
            await self._upload_screenshot(
                shot,
                channel_id=task.metadata.get("slack_channel_id"),
                thread_ts=task.metadata.get("slack_thread_ts"),
                title=f"escalation-{task.id[:8]}-{i}",
            )
