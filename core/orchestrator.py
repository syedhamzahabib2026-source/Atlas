"""
Atlas orchestrator — persistent execution loop.

Phase 8: durable runtime, task persistence, recovery on restart.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from core.blocked import detect_block
from core.config import AtlasConfig
from core.git_safety import GitSafetyCoordinator
from core.logger import get_logger
from core.recovery_engine import RecoveryAction, RecoveryEngine
from core.recovery_strategies import RecoveryStrategy
from core.task_manager import TaskManager, TaskStatus
from core.task_result import ResultStatus, TaskResult

if TYPE_CHECKING:
    from agents.base import BaseAgent
    from core.approval_manager import ApprovalManager
    from core.pr_manager import PRManager
    from core.runtime_manager import RuntimeManager
    from core.task_store import TaskStore
    from memory.memory_coordinator import MemoryCoordinator
    from memory.store import MemoryStore
    from sessions.tmux_manager import TmuxManager
    from slack.bot import SlackBot

logger = get_logger("orchestrator")


class Orchestrator:
    """
    Main autonomous loop for Atlas.

    RecoveryEngine: strategy decisions
    GitSafetyCoordinator: repository safety
    Agents: execute work only
    """

    def __init__(
        self,
        config: AtlasConfig,
        task_manager: TaskManager,
        memory: MemoryStore | None = None,
        tmux: TmuxManager | None = None,
        slack: SlackBot | None = None,
        agents: list[BaseAgent] | None = None,
        recovery: RecoveryEngine | None = None,
        git_safety: GitSafetyCoordinator | None = None,
        memory_coordinator: MemoryCoordinator | None = None,
        runtime: RuntimeManager | None = None,
        task_store: TaskStore | None = None,
    ) -> None:
        self.config = config
        self.tasks = task_manager
        self.task_store = task_store
        self.runtime = runtime
        self.memory = memory
        self.memory_coordinator = memory_coordinator
        self.tmux = tmux
        self.slack = slack
        self.agents = agents or []
        self.recovery = recovery or RecoveryEngine(
            config.recovery,
            log_dir=config.log_dir,
        )
        self.git_safety = git_safety or GitSafetyCoordinator(config.git)
        self._running = False
        self._cancel_events: dict[str, asyncio.Event] = {}
        self.controller = None
        self.approval_manager: ApprovalManager | None = None
        self.pr_manager: PRManager | None = None

    def request_cancel(self, task_id: str) -> None:
        event = self._cancel_events.get(task_id)
        if event:
            event.set()
            logger.info("Cancel requested for task %s", task_id[:8])

    def _get_cancel_event(self, task_id: str) -> asyncio.Event:
        ev = self._cancel_events.get(task_id)
        if ev is None:
            ev = asyncio.Event()
            self._cancel_events[task_id] = ev
        return ev

    async def start(self) -> None:
        logger.info("Atlas orchestrator starting (Phase 8)")
        await self._bootstrap()
        self._running = True
        if self.config.scanner_enabled and self.memory_coordinator:
            asyncio.create_task(self._scanner_loop())
        await self.run_loop()

    async def stop(self, *, graceful: bool = True) -> None:
        logger.info("Atlas orchestrator stopping")
        self._running = False
        if self.runtime and self.config.runtime.enabled:
            await self.runtime.shutdown(
                self.tasks, self.tmux, graceful=graceful, slack=self.slack
            )
        if self.task_store:
            await self.task_store.close()
        if self.slack:
            await self.slack.disconnect()

    async def _bootstrap(self) -> None:
        if self.task_store:
            await self.task_store.initialize()
            self.tasks.bind_store(self.task_store)
            logger.info("Task persistence enabled")

        if self.memory:
            await self.memory.initialize()
            logger.info("Legacy memory store initialized")
        if self.memory_coordinator:
            await self.memory_coordinator.initialize()
            logger.info("Operational memory layer active")

        if self.runtime and self.config.runtime.enabled:
            await self.runtime.startup(
                self.tasks, self.task_store, self.tmux, slack=self.slack
            )

        from core.atlas_controller import AtlasController
        from core.approval_manager import ApprovalManager
        from core.pr_manager import GitHubConfig, PRManager

        github_cfg = GitHubConfig.from_env()
        if github_cfg:
            self.pr_manager = PRManager(github_cfg)
            logger.info(
                "GitHub PR manager ready (%s/%s)",
                github_cfg.owner,
                github_cfg.repo,
            )
        else:
            logger.warning(
                "GITHUB_TOKEN / GITHUB_REPO_OWNER / GITHUB_REPO_NAME not set "
                "— PR operations disabled"
            )

        self.approval_manager = ApprovalManager(
            slack=self.slack,
            channel_id=self.config.slack.channel_id,
        )

        self.controller = AtlasController(
            self,
            approval_manager=self.approval_manager,
            pr_manager=self.pr_manager,
        )

        if self.slack and self.config.slack_ready:
            self.slack.bind(self.controller)
            await self.slack.connect()
            logger.info("Slack remote control active")

        if self.tmux and self.tmux.is_available():
            logger.info("Tmux ready (backend=%s)", self.tmux.backend.value)

        if self.agents:
            logger.info("Agents registered: %s", [a.name for a in self.agents])

        if self.config.git.enabled and self.git_safety.git.is_available():
            logger.info("Git safety layer active")
        elif self.config.git.enabled:
            logger.warning("Git safety enabled but GitPython not installed")

    async def run_loop(self) -> None:
        interval = self.config.poll_interval_sec
        logger.info("Entering orchestrator loop (poll_interval=%ss)", interval)

        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("Orchestrator tick failed")
            await asyncio.sleep(interval)

    async def _tick(self) -> None:
        if self.runtime and self.config.runtime.enabled:
            await self.runtime.on_tick(self.tasks)

        while True:
            if self.runtime and self.config.runtime.enabled:
                task = self.runtime.next_scheduled_task(self.tasks)
            else:
                task = self.tasks.next_pending()
            if task is None:
                break
            if not self.tasks.can_start_more():
                break
            if task.status == TaskStatus.RETRYING:
                self.tasks.update_status(task.id, TaskStatus.PENDING)
                await self.tasks.persist(task)
            await self._run_task(task)

    async def _run_task(self, task) -> None:
        cancel_ev = self._get_cancel_event(task.id)
        cancel_ev.clear()
        task.metadata["_cancel_event"] = cancel_ev

        await self._prepare_git_workspace(task)
        await self._prepare_operational_context(task)

        self.tasks.update_status(task.id, TaskStatus.RUNNING)
        await self.tasks.persist(task)
        logger.info("Task running: %s - %s", task.id[:8], task.title)

        if self.slack and self.config.slack_ready:
            await self.slack.notify_task_started(task)

        try:
            if cancel_ev.is_set():
                result = self._cancelled_result(task)
            else:
                result = await self._execute_task(task)
        except Exception as exc:
            logger.exception("Task execution crashed: %s", task.id[:8])
            result = TaskResult(
                status=ResultStatus.FAILED,
                summary=f"Orchestrator error: {exc}",
                session_name=task.session_name or "",
                errors=[str(exc)],
                metadata={"task_id": task.id},
            )
            result.finish()
        finally:
            self._cancel_events.pop(task.id, None)
            task.metadata.pop("_cancel_event", None)

        if cancel_ev.is_set() and result.status != ResultStatus.CANCELLED:
            result = self._cancelled_result(task, previous=result)

        self.tasks.attach_result(task.id, result)

        if result.status == ResultStatus.CANCELLED:
            self.tasks.update_status(task.id, TaskStatus.CANCELLED, error="cancelled")
            await self.tasks.persist(task)
            return

        if result.success:
            verify = await self._maybe_verify(task, result)
            if verify is not None:
                return
            await self._finalize_success(task, result)
            return

        if result.status == ResultStatus.TIMEOUT:
            await self._handle_failure_with_recovery(task, result, phase="timeout")
        else:
            await self._handle_failure_with_recovery(task, result, phase="execution")

    async def _prepare_git_workspace(self, task) -> None:
        if task.metadata.get("agent") not in ("claude_code", None):
            return
        if not task.metadata.get("git_safety", True):
            return
        isolated = await self.git_safety.prepare_workspace(task, self.config.projects_dir)
        if isolated:
            await self.git_safety.create_checkpoint(task, "initial", 0)
            if self.slack and self.config.slack_ready:
                git_meta = task.metadata.get("git", {})
                await self.slack.notify_workspace_isolated(
                    task, git_meta.get("branch", "unknown")
                )
                cps = git_meta.get("checkpoints", [])
                if cps:
                    await self.slack.notify_checkpoint_created(task, cps[-1])

    async def _maybe_verify(self, task, code_result: TaskResult) -> bool | None:
        verify_cfg = task.metadata.get("verify") or task.metadata.get("verification")
        if not verify_cfg:
            return None

        self.tasks.update_status(task.id, TaskStatus.VERIFYING)
        await self.tasks.persist(task)
        logger.info("Verification phase for task %s", task.id[:8])

        browser_agent = self._find_agent_by_name("browser")
        if not browser_agent:
            return None

        verify_meta = {
            "agent": "browser",
            "recovery_enabled": False,
            **verify_cfg,
        }
        if "steps" not in verify_meta and "workflow" not in verify_meta:
            verify_meta.setdefault("url", verify_cfg.get("url", task.metadata.get("url")))

        verify_task = self.tasks.create(
            title=f"Verify: {task.title[:40]}",
            description="Post-change browser verification",
            project_id=task.project_id,
            metadata=verify_meta,
        )
        verify_task.metadata["parent_task_id"] = task.id

        verify_result = await browser_agent.run(verify_task)
        self.tasks.attach_result(verify_task.id, verify_result)

        if verify_result.success:
            task.metadata["verification"] = {"status": "passed", "child": verify_task.id}
            health = self.git_safety.health.analyze(task, verify_result)
            self.git_safety.health.record(task, health)
            if self.slack and self.config.slack_ready:
                await self.slack.notify_recovery_success(
                    task, "Browser verification passed after code change."
                )
            return None

        task.metadata["verification"] = {
            "status": "failed",
            "child": verify_task.id,
            "result": verify_result.to_dict(),
        }
        await self._handle_failure_with_recovery(
            task,
            verify_result,
            phase="verification",
        )
        return True

    async def _handle_failure_with_recovery(
        self,
        task,
        result: TaskResult,
        *,
        phase: str = "execution",
    ) -> None:
        await self._finalize_failure(task, result, notify_slack=False)

        if not task.recovery_enabled or "no_agent" in result.errors:
            await self._notify_failure_slack(task, result)
            block = detect_block(result, task)
            if block and self.controller:
                await self.controller.enter_blocked(task, block[0], block[1])
            return

        strategy = task.metadata.get("recovery_strategy", "initial")
        self.recovery.record_attempt(
            task, result, strategy=strategy, phase=phase, outcome="failed"
        )
        self.git_safety.track_dependency_change(task)

        health, rollback_dec, rolled_back = await self.git_safety.analyze_and_maybe_rollback(
            task, result
        )

        if rolled_back and self.slack and self.config.slack_ready:
            await self.slack.notify_rollback(task, rollback_dec.summary)

        if health.regression_detected and self.slack and self.config.slack_ready:
            await self.slack.notify_regression_detected(task, health)

        safety_block = self.git_safety.safety_limits_exceeded(task)
        if safety_block:
            await self._escalate_safety(task, result, safety_block)
            return

        plan = self.recovery.plan_recovery(
            task, result, phase=phase, health_report=health
        )

        if plan.contradiction.detected and self.slack and self.config.slack_ready:
            await self.slack.notify_contradiction_detected(task, plan.contradiction.summary)

        if plan.action == RecoveryAction.INVESTIGATE:
            await self._run_investigation(task, result, plan)
            return

        if plan.action == RecoveryAction.ROLLBACK:
            await self._execute_planned_rollback(task, result, plan)
            return

        if plan.action == RecoveryAction.RETRY:
            await self._schedule_retry(task, result, plan)
            return

        if plan.action == RecoveryAction.ESCALATE:
            await self._escalate(task, result, plan)
            return

        await self._notify_failure_slack(task, result)
        block = detect_block(result, task)
        if block and self.controller:
            await self.controller.enter_blocked(task, block[0], block[1])

    async def _execute_planned_rollback(self, task, result: TaskResult, plan) -> None:
        """Recovery engine requested rollback — execute then retry with new strategy."""
        from core.rollback_engine import RollbackDecision, RollbackMethod

        checkpoints = task.metadata.get("git", {}).get("checkpoints", [])
        target = checkpoints[-1]["commit_hash"] if checkpoints else None
        decision = RollbackDecision(
            should_rollback=bool(target),
            reason=plan.message,
            method=RollbackMethod.CHECKPOINT,
            target_ref=target,
        )
        if decision.should_rollback:
            await self.git_safety.rollback.execute(
                self.git_safety.git, task, decision
            )
            if self.slack and self.config.slack_ready:
                await self.slack.notify_rollback(task, decision.summary)

        follow = self.recovery.plan_recovery(
            task,
            result,
            phase="recovery",
            health_report=task.metadata.get("health"),
            investigation_report=task.metadata.get("investigation_report"),
        )
        if follow.action == RecoveryAction.RETRY:
            follow.strategy = follow.strategy or RecoveryStrategy.BACKEND_TRACE.value
            await self._schedule_retry(task, result, follow)
        elif follow.action == RecoveryAction.ESCALATE:
            await self._escalate(task, result, follow)

    async def _run_investigation(self, task, result: TaskResult, plan) -> None:
        self.tasks.update_status(task.id, TaskStatus.INVESTIGATING)
        await self.tasks.persist(task)
        task.metadata["investigation_report"] = plan.investigation_report
        logger.info("Investigation mode: %s", task.id[:8])

        if self.slack and self.config.slack_ready:
            await self.slack.notify_investigation_mode(task, plan.investigation_report or "")

        self.recovery.record_attempt(
            task,
            result,
            strategy="investigate",
            phase="investigation",
            outcome="investigating",
        )

        follow_up = self.recovery.plan_after_investigation(task, result)

        if follow_up.action == RecoveryAction.RETRY:
            await self._schedule_retry(task, result, follow_up)
        elif follow_up.action == RecoveryAction.ESCALATE:
            await self._escalate(task, result, follow_up)
        else:
            self.tasks.update_status(task.id, TaskStatus.FAILED)
            await self.tasks.persist(task)

    async def _schedule_retry(self, task, result: TaskResult, plan) -> None:
        attempt = len(task.metadata.get("attempt_history", [])) + 1
        await self.git_safety.create_checkpoint(
            task,
            task.metadata.get("recovery_strategy", "initial"),
            max(0, attempt - 1),
        )

        self.recovery.apply_retry_to_task(task, plan)
        task.metadata.setdefault("recovery_chain", []).append(
            {
                "attempt": plan.attempt_number,
                "strategy": plan.strategy,
                "phase": "retry",
            }
        )
        self.tasks.update_status(task.id, TaskStatus.RETRYING)
        await self.tasks.persist(task)
        logger.info(
            "Recovery retry #%s strategy=%s for %s",
            plan.attempt_number,
            plan.strategy,
            task.id[:8],
        )

        if self.slack and self.config.slack_ready:
            await self.slack.notify_recovery_retry(task, plan)

        cp = task.metadata.get("git", {}).get("checkpoints", [])
        if cp and self.slack and self.config.slack_ready:
            await self.slack.notify_checkpoint_created(task, cp[-1])

        self.tasks.update_status(task.id, TaskStatus.PENDING)
        await self.tasks.persist(task)

    async def _escalate(self, task, result: TaskResult, plan) -> None:
        reason = plan.escalation_reason or "Recovery exhausted"
        summary = self._escalation_summary(task, plan)
        task.metadata["escalation_summary"] = summary

        if self.slack and self.config.slack_ready:
            await self.slack.notify_escalation(task, summary)

        if self.controller:
            await self.controller.enter_blocked(
                task,
                "recovery_exhausted",
                f"{reason}\n\n{summary}\n\nReply with guidance to continue.",
            )
        else:
            self.tasks.update_status(task.id, TaskStatus.FAILED, error=reason)

    async def _escalate_safety(self, task, result: TaskResult, reason: str) -> None:
        from core.attempt_history import AttemptHistory

        summary = (
            f"*Safety escalation*\n{reason}\n"
            f"Attempts: {len(AttemptHistory.load(task))}\n"
            f"Rollbacks: {len(task.metadata.get('git', {}).get('rollback_history', []))}"
        )
        if self.slack and self.config.slack_ready:
            await self.slack.notify_escalation(task, summary)
        if self.controller:
            await self.controller.enter_blocked(
                task,
                "safety_limit",
                f"{reason}\n\nReply with guidance.",
            )

    def _escalation_summary(self, task, plan) -> str:
        from core.attempt_history import AttemptHistory

        lines = [
            "*Escalation summary*",
            f"Reason: {getattr(plan, 'escalation_reason', 'unknown')}",
            f"Attempts: {len(AttemptHistory.load(task))}",
            "",
            "*Strategies tried:*",
        ]
        for rec in AttemptHistory.load(task):
            lines.append(f"• {rec.strategy} ({rec.outcome}) — {rec.error_summary[:80]}")
        if getattr(plan, "contradiction", None) and plan.contradiction.detected:
            lines.append(f"\n*Contradictions:* {plan.contradiction.summary}")
        git_meta = task.metadata.get("git", {})
        if git_meta.get("checkpoints"):
            lines.append(f"\n*Checkpoints:* {len(git_meta['checkpoints'])}")
        if git_meta.get("rollback_history"):
            lines.append(f"*Rollbacks:* {len(git_meta['rollback_history'])}")
        if task.metadata.get("investigation_report"):
            lines.append("\n*Investigation excerpt:*")
            lines.append(task.metadata["investigation_report"][:800])
        return "\n".join(lines)

    async def _notify_failure_slack(self, task, result: TaskResult) -> None:
        if not self.slack or not self.config.slack_ready:
            return
        if task.metadata.get("agent") == "browser":
            await self.slack.notify_browser_failure(task, result)
        else:
            await self.slack.notify_task_failed(task, result)

    def _cancelled_result(self, task, previous: TaskResult | None = None) -> TaskResult:
        r = TaskResult(
            status=ResultStatus.CANCELLED,
            summary="Stopped by operator",
            session_name=(previous.session_name if previous else task.session_name) or "",
            raw_output=previous.raw_output if previous else "",
            metadata={"task_id": task.id},
        )
        r.finish()
        return r

    async def _finalize_success(self, task, result: TaskResult) -> None:
        health = self.git_safety.health.analyze(task, result)
        self.git_safety.health.record(task, health)
        logger.info("Task completed: %s | health=%.0f", task.id[:8], health.current.score)
        agent = self._find_agent_for(task)
        if agent:
            await agent.on_complete(task, result)
        await self._persist_operational_memory(task, result)

        branch = task.metadata.get("git", {}).get("branch")
        if branch and self.pr_manager and self.approval_manager:
            await self._open_pr_and_request_approval(task, branch)
            return

        self.tasks.update_status(task.id, TaskStatus.COMPLETED)
        await self.tasks.persist(task)
        if self.slack and self.config.slack_ready:
            if task.metadata.get("agent") == "browser":
                await self.slack.notify_browser_success(task, result)
            else:
                await self.slack.notify_task_completed(task, result)

    async def _open_pr_and_request_approval(self, task, branch: str) -> None:
        """Push branch, create a GitHub PR, and move task to AWAITING_APPROVAL."""
        repo_path = task.metadata.get("git", {}).get("repo_path")
        if repo_path:
            push_result = await self.pr_manager.push_branch(branch, repo_path)
            if not push_result.success:
                logger.error(
                    "Branch push failed for task %s: %s", task.id[:8], push_result.error
                )
                if self.slack and self.config.slack_ready:
                    await self.slack.notify(
                        f"⚠️ Task `{task.id[:8]}` succeeded but branch push failed: "
                        f"{push_result.error}\nBranch `{branch}` may need manual push."
                    )
                self.tasks.update_status(task.id, TaskStatus.COMPLETED)
                await self.tasks.persist(task)
                return
        else:
            logger.warning(
                "No repo_path in task %s git metadata — skipping push", task.id[:8]
            )

        if repo_path:
            diff_check = await asyncio.to_thread(
                __import__("subprocess").run,
                ["git", "log", "origin/main..HEAD", "--oneline"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if not diff_check.stdout.strip():
                msg = (
                    f"✅ Task `{task.id[:8]}` complete — no code changes to merge, "
                    f"branch is identical to main."
                )
                logger.info("Task %s has no new commits vs origin/main — skipping PR", task.id[:8])
                self.tasks.update_status(task.id, TaskStatus.COMPLETED)
                await self.tasks.persist(task)
                if self.slack and self.config.slack_ready:
                    await self.slack.notify(msg)
                return

        title = task.title[:72]
        body = (
            f"Automated PR opened by Atlas.\n\n"
            f"**Task:** `{task.id[:8]}`\n"
            f"**Prompt:** {(task.metadata.get('prompt') or task.description)[:500]}"
        )
        pr_result = await self.pr_manager.create_pr(
            task_id=task.id,
            branch_name=branch,
            title=title,
            body=body,
        )
        if not pr_result.success:
            logger.error(
                "PR creation failed for task %s: %s", task.id[:8], pr_result.error
            )
            if self.slack and self.config.slack_ready:
                await self.slack.notify(
                    f"⚠️ Task `{task.id[:8]}` succeeded but PR creation failed: "
                    f"{pr_result.error}\nBranch `{branch}` ready for manual PR."
                )
            self.tasks.update_status(task.id, TaskStatus.COMPLETED)
            await self.tasks.persist(task)
            return

        task.metadata["pr_number"] = pr_result.pr_number
        task.metadata["pr_url"] = pr_result.pr_url
        self.tasks.update_status(task.id, TaskStatus.AWAITING_APPROVAL)
        await self.tasks.persist(task)

        approval = await self.approval_manager.request_approval(
            task_id=task.id,
            pr_url=pr_result.pr_url,
            pr_number=pr_result.pr_number,
            branch_name=branch,
        )
        if not approval.success:
            logger.error(
                "Approval request failed for task %s: %s", task.id[:8], approval.error
            )
        logger.info(
            "Task %s awaiting approval — PR #%s", task.id[:8], pr_result.pr_number
        )

    async def _finalize_failure(
        self, task, result: TaskResult, *, notify_slack: bool = True
    ) -> None:
        self.tasks.update_status(
            task.id,
            TaskStatus.FAILED,
            error="; ".join(result.errors) or result.summary,
        )
        await self.tasks.persist(task)
        logger.error("Task failed: %s | %s", task.id[:8], result.summary)
        agent = self._find_agent_for(task)
        if agent:
            await agent.on_error(task, result)
        if notify_slack:
            await self._notify_failure_slack(task, result)
        await self._persist_operational_memory(task, result)

    def _find_agent_for(self, task) -> BaseAgent | None:
        for agent in self.agents:
            if agent.can_handle(task):
                return agent
        return None

    def _find_agent_by_name(self, name: str) -> BaseAgent | None:
        for agent in self.agents:
            if agent.name == name:
                return agent
        return None

    async def _execute_task(self, task) -> TaskResult:
        for agent in self.agents:
            if agent.can_handle(task):
                logger.info("Assigning task %s to agent %s", task.id[:8], agent.name)
                result = await agent.run(task)
                if task.session_name:
                    self.tasks.set_session_name(task.id, task.session_name)
                return result

        result = TaskResult(
            status=ResultStatus.FAILED,
            summary="No agent registered to handle this task",
            session_name="",
            errors=["no_agent"],
            metadata={"task_id": task.id},
        )
        result.finish()
        return result

    async def _prepare_operational_context(self, task) -> None:
        if not self.memory_coordinator or not self.config.memory_config.inject_context_into_prompts:
            return
        await self.memory_coordinator.prepare_task_context(task)

    async def _persist_operational_memory(self, task, result: TaskResult) -> None:
        if not self.memory_coordinator:
            return
        insights = await self.memory_coordinator.extract_after_task(task, result)
        if insights and self.slack and self.config.slack_ready:
            await self.slack.notify_memory_insights(task, insights)

    async def _scanner_loop(self) -> None:
        """Background proactive scan — fires every scanner_interval_hours hours."""
        interval_sec = self.config.scanner_interval_hours * 3600
        logger.info(
            "Proactive scanner loop started (interval=%.1fh)", self.config.scanner_interval_hours
        )
        while self._running:
            await asyncio.sleep(interval_sec)
            if not self._running:
                break
            try:
                msg = await self.run_scanner()
                if self.slack and self.config.slack_ready:
                    await self.slack.notify(msg)
            except Exception:
                logger.exception("Proactive scanner failed — skipping this cycle")

    async def run_scanner(self, project_filter: str | None = None) -> str:
        """Scan memory for actionable signals. Returns formatted Slack message.

        Called by the background loop and directly by /atlas scan.
        Non-fatal: caller is responsible for try/except on the background path.
        """
        from core.task_scanner import TaskScanner, format_suggestions

        if not self.memory_coordinator:
            return "Memory coordinator not active — scanner unavailable."

        scanner = TaskScanner(self.memory_coordinator.store)
        projects = dict(self.config.projects)

        if project_filter:
            if project_filter not in projects:
                known = ", ".join(f"`{k}`" for k in projects) or "none configured"
                return f"Unknown project `{project_filter}`. Known: {known}"
            projects = {project_filter: projects[project_filter]}

        all_suggestions = []
        for name in projects:
            sug = await scanner.scan_project(name, name)
            all_suggestions.extend(sug)

        all_suggestions.sort(key=lambda s: 0 if s.priority == "high" else 1)
        return format_suggestions(all_suggestions[:5], scanned=list(projects.keys()))

    async def _persist_result(self, task, result: TaskResult) -> None:
        await self._persist_operational_memory(task, result)
        if not self.memory:
            return
        logger.debug("legacy memory store: task=%s", task.id[:8])
