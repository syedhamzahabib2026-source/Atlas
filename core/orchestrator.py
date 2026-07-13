"""
Atlas orchestrator — persistent execution loop.

Phase 8: durable runtime, task persistence, recovery on restart.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from core.blocked import detect_block
from core.config import AtlasConfig
from core.git_safety import GitSafetyCoordinator
from core.logger import get_logger
from core.pr_manager import GitHubConfig, PRManager, PRPreparationManager
from core.recovery_engine import RecoveryAction, RecoveryEngine
from core.recovery_strategies import RecoveryStrategy
from core.task_manager import TaskManager, TaskStatus
from core.task_result import ResultStatus, TaskResult

if TYPE_CHECKING:
    from agents.base import BaseAgent
    from core.approval_engine import ApprovalEngine
    from core.approval_manager import ApprovalManager
    from core.deployment_manager import DeploymentManager
    from core.runtime_manager import RuntimeManager
    from core.task_store import TaskStore
    from core.worker_pool_manager import WorkerPoolManager
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
        worker_pools: WorkerPoolManager | None = None,
    ) -> None:
        self.config = config
        self.tasks = task_manager
        self.task_store = task_store
        self.runtime = runtime
        self.worker_pools = worker_pools
        self.memory = memory
        self.memory_coordinator = memory_coordinator
        self.tmux = tmux
        self.slack = slack
        self.agents = agents or []
        self.recovery = recovery or RecoveryEngine(
            config.recovery,
            log_dir=config.log_dir,
        )
        self.git_safety = git_safety or GitSafetyCoordinator(config.git, projects=config.projects)
        self._running = False
        self._cancel_events: dict[str, asyncio.Event] = {}
        self.controller = None
        self.approval_engine: ApprovalEngine | None = None
        self.approval_manager: ApprovalManager | None = None
        self.pr_manager: PRManager | None = None
        self.pr_preparation: PRPreparationManager | None = None
        self.deployment_manager: DeploymentManager | None = None
        # Per-project GitHub clients keyed by (owner, repo) — Tier 3.1
        self._pr_managers: dict[tuple[str, str], PRManager] = {}

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

        if self.tmux and self.tmux.is_available():
            await self.tmux.ensure_server()

        if self.runtime and self.config.runtime.enabled:
            await self.runtime.startup(
                self.tasks, self.task_store, self.tmux, slack=self.slack
            )

        from core.approval_engine import ApprovalEngine
        from core.approval_manager import ApprovalManager
        from core.approval_policy import ApprovalPolicy
        from core.atlas_controller import AtlasController
        from core.pr_manager import GitHubConfig, PRManager, PRPreparationManager

        github_cfg = GitHubConfig.from_env()
        if github_cfg:
            self.pr_manager = PRManager(github_cfg)
            self.pr_preparation = PRPreparationManager(self.pr_manager)
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
            self.pr_preparation = PRPreparationManager(None)

        policy = ApprovalPolicy(self.config.approval)
        self.approval_engine = ApprovalEngine(
            policy=policy,
            slack=self.slack,
            channel_id=self.config.slack.channel_id,
            pr_preparation=self.pr_preparation,
        )
        self.approval_manager = ApprovalManager(
            slack=self.slack,
            channel_id=self.config.slack.channel_id,
            engine=self.approval_engine,
        )
        self.approval_engine.rehydrate_from_tasks(self.tasks.list_all())

        from core.deployment_manager import DeploymentManager
        from core.deployment_policy import DeploymentPolicy

        self.deployment_manager = DeploymentManager(
            config=self.config.deployment,
            policy=DeploymentPolicy(self.config.deployment),
        )
        logger.info(
            "Deployment orchestration active (mock_ci=%s)",
            self.config.deployment.mock_ci,
        )

        self.controller = AtlasController(
            self,
            approval_engine=self.approval_engine,
            approval_manager=self.approval_manager,
            pr_manager=self.pr_manager,
            pr_preparation=self.pr_preparation,
            deployment_manager=self.deployment_manager,
        )

        if self.slack and self.config.slack_ready:
            self.slack.bind(self.controller)
            await self.slack.connect()
            logger.info("Slack remote control active")

        if self.tmux and self.tmux.is_available():
            logger.info("Tmux ready (backend=%s)", self.tmux.backend.value)

        if self.agents:
            logger.info("Agents registered: %s", [a.name for a in self.agents])

        if self.worker_pools and self.worker_pools.enabled:
            self.worker_pools.load_state(self._pool_state_path)
            self.worker_pools.sync_from_tasks(self.tasks)
            if self.config.runtime.orphan_session_cleanup:
                cleaned = await self.worker_pools.cleanup_orphan_sessions(self.tasks)
                if cleaned:
                    logger.info(
                        "Cleaned %d orphan pool tmux session(s): %s",
                        len(cleaned),
                        ", ".join(cleaned),
                    )
            logger.info(
                "Worker pools active: %s",
                [p.pool_id for p in self.worker_pools.registry.all_pools()],
            )

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

        if self.deployment_manager and self.config.deployment.enabled:
            await self._process_delivery_queue()

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

        if not await self._assign_worker_pool(task):
            return

        if not await self._check_pre_execution_approval(task):
            # Slot was acquired during routing — release it or the pool
            # leaks capacity every time a task pauses at the approval gate.
            self._release_worker_pool(task)
            return

        try:
            await self._prepare_git_workspace(task)
            await self._prepare_operational_context(task)
        except Exception as exc:
            logger.exception("Task preparation failed: %s", task.id[:8])
            self._release_worker_pool(task)
            prep_result = TaskResult(
                status=ResultStatus.FAILED,
                summary=f"Preparation error: {exc}",
                session_name=task.session_name or "",
                errors=[str(exc)],
                metadata={"task_id": task.id},
            )
            prep_result.finish()
            self.tasks.attach_result(task.id, prep_result)
            await self._handle_failure_with_recovery(task, prep_result, phase="preparation")
            return

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
        await self._record_pool_result(task, result)

        if result.status == ResultStatus.CANCELLED:
            self.tasks.update_status(task.id, TaskStatus.CANCELLED, error="cancelled")
            await self.tasks.persist(task)
            return

        if result.success:
            # Tier 1.3: success requires evidence (committed work), not just
            # an idle-looking tmux pane.
            if not await self._verify_success_evidence(task, result):
                await self._handle_failure_with_recovery(task, result, phase="execution")
                return
            # Tier 2.2: project quality gates (build/test) must pass.
            if not await self._run_quality_gates(task, result):
                await self._handle_failure_with_recovery(task, result, phase="verification")
                return
            verify = await self._maybe_verify(task, result)
            if verify is not None:
                return
            await self._finalize_success(task, result)
            return

        if result.status == ResultStatus.TIMEOUT:
            await self._handle_failure_with_recovery(task, result, phase="timeout")
        else:
            await self._handle_failure_with_recovery(task, result, phase="execution")

    async def _assign_worker_pool(self, task) -> bool:
        """
        Route task to a worker pool before execution.

        Returns False if no pool is available (task stays pending).
        """
        if not self.worker_pools or not self.worker_pools.enabled:
            return True

        agent = task.metadata.get("agent")
        if agent == "browser":
            return True

        decision = self.worker_pools.assign_task(task, self.tasks.list_all())
        if decision is None:
            logger.debug(
                "No worker pool available for task %s — deferring",
                task.id[:8],
            )
            api_pool = self.worker_pools.registry.get("api")
            if (
                api_pool
                and api_pool.busy_count() >= api_pool.config.max_workers
                and self.slack
                and self.config.slack_ready
            ):
                await self.slack.notify_pool_overloaded("API pool overloaded")
            return False

        pool = self.worker_pools.bind_task(task, decision)
        if not pool:
            return False

        if self.slack and self.config.slack_ready:
            if decision.overflow:
                await self.slack.notify_pool_routing(
                    task,
                    decision.pool_id,
                    "Task routed to API pool (priority overflow)",
                )
            elif decision.pool_id == "api" and self.worker_pools.auth_monitor.subscription_on_cooldown:
                await self.slack.notify_pool_exhausted(
                    "Subscription pool exhausted — routing to API pool"
                )

        return True

    def _release_worker_pool(self, task) -> None:
        """Idempotent slot release for exit paths that skip result recording."""
        if self.worker_pools and self.worker_pools.enabled:
            self.worker_pools.release_task(task)

    @property
    def _pool_state_path(self) -> Path:
        return Path(self.config.log_dir) / "pool_state.json"

    async def _record_pool_result(self, task, result: TaskResult) -> None:
        if not self.worker_pools or not self.worker_pools.enabled:
            return
        prev_cooldown = self.worker_pools.auth_monitor.subscription_on_cooldown
        classification = self.worker_pools.record_result(task, result)
        self.worker_pools.save_state(self._pool_state_path)

        if (
            classification
            and classification.should_cooldown_subscription
            and not prev_cooldown
            and self.slack
            and self.config.slack_ready
        ):
            await self.slack.notify_pool_exhausted(
                "Subscription pool exhausted — routing to API pool"
            )

        if prev_cooldown and not self.worker_pools.auth_monitor.subscription_on_cooldown:
            if self.slack and self.config.slack_ready:
                await self.slack.notify_pool_restored("Subscription pool restored")

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

    async def _verify_success_evidence(self, task, result: TaskResult) -> bool:
        """
        Success gate (Tier 1.3): a git-isolated coding task only counts as
        successful when committed work exists on the task branch.

        Mutates `result` to FAILED with a diagnostic when evidence is missing.
        Tasks may opt out with metadata require_commit: false (e.g. analysis
        prompts that intentionally change nothing).
        """
        if task.metadata.get("agent") == "browser":
            return True
        if task.metadata.get("require_commit") is False:
            return True

        git_meta = task.metadata.get("git") or {}
        repo_path = git_meta.get("repo_path")
        if not repo_path or not self.git_safety.git.is_available():
            return True  # no git isolation — nothing to check against

        repo = Path(repo_path)

        # Rescue uncommitted work first: Claude finishing without committing
        # was a recurring production bug (empty-diff PRs).
        try:
            if await self.git_safety.git.is_dirty(repo):
                sha = await self.git_safety.git.commit_all(
                    repo, f"atlas: auto-commit task work (task={task.id[:8]})"
                )
                if sha:
                    git_meta["auto_committed"] = sha
                    logger.info(
                        "Auto-committed uncommitted work for %s: %s",
                        task.id[:8],
                        sha[:8],
                    )
        except Exception:
            logger.exception("Auto-commit failed for %s", task.id[:8])

        base = git_meta.get("base_branch") or "main"
        count = await self.git_safety.git.count_commits_ahead(repo, base)
        if count is None:
            # Can't determine — don't fail a possibly-good run on a git
            # plumbing error; PR-time empty-diff check still applies.
            logger.warning(
                "Could not count commits ahead of %s for %s — gate skipped",
                base,
                task.id[:8],
            )
            return True

        git_meta["commits_ahead"] = count
        if count > 0:
            return True

        result.status = ResultStatus.FAILED
        result.errors.append("no_code_changes_detected")
        result.summary = (
            "Agent reported completion but no commits exist on the task "
            f"branch vs {base}. The requested work was not delivered."
        )
        logger.error("Success gate failed for %s: no commits on branch", task.id[:8])
        return False

    def _project_config_for(self, task):
        if not task.project_id:
            return None
        return self.config.projects.get(task.project_id)

    async def _run_quality_gates(self, task, result: TaskResult) -> bool:
        """
        Tier 2.2: run the project's build/test commands after implementation.

        Returns False (and mutates `result` to FAILED) when a gate fails so
        the standard recovery path handles it. Projects without configured
        commands pass trivially.
        """
        if task.metadata.get("agent") == "browser":
            return True
        proj = self._project_config_for(task)
        if not proj:
            return True

        commands = [
            ("build", proj.build_command),
            ("test", proj.test_command),
        ]
        commands = [(name, cmd) for name, cmd in commands if cmd.strip()]
        if not commands:
            return True

        cwd = (
            (task.metadata.get("git") or {}).get("repo_path")
            or task.metadata.get("working_dir")
            or proj.repo_path
        )
        gates: dict[str, str] = {}
        task.metadata["quality_gates"] = gates

        for name, cmd in commands:
            logger.info("Quality gate %r for %s: %s", name, task.id[:8], cmd)

            def _run_cmd(command=cmd):
                return subprocess.run(
                    command,
                    shell=True,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=proj.check_timeout_sec,
                )

            try:
                proc = await asyncio.to_thread(_run_cmd)
            except subprocess.TimeoutExpired:
                gates[name] = "timeout"
                result.status = ResultStatus.FAILED
                result.errors.append(f"{name}_gate_timeout")
                result.summary = (
                    f"Quality gate '{name}' timed out after "
                    f"{proj.check_timeout_sec}s: {cmd}"
                )
                return False
            except Exception as exc:
                gates[name] = "error"
                logger.exception("Quality gate %r crashed for %s", name, task.id[:8])
                result.status = ResultStatus.FAILED
                result.errors.append(f"{name}_gate_error")
                result.summary = f"Quality gate '{name}' could not run: {exc}"
                return False

            if proc.returncode != 0:
                tail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-3000:]
                gates[name] = "failed"
                task.metadata[f"quality_gate_{name}_output"] = tail
                result.status = ResultStatus.FAILED
                result.errors.append(f"{name}_gate_failed")
                result.summary = (
                    f"Quality gate '{name}' failed (exit {proc.returncode}): {cmd}"
                )
                result.raw_output = (result.raw_output or "") + f"\n\n[{name} gate output]\n{tail}"
                logger.error(
                    "Quality gate %r failed for %s (exit %s)",
                    name,
                    task.id[:8],
                    proc.returncode,
                )
                return False

            gates[name] = "passed"
            logger.info("Quality gate %r passed for %s", name, task.id[:8])

        return True

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
        recovery_active = task.recovery_enabled and "no_agent" not in result.errors
        if recovery_active:
            # Crash safety: _finalize_failure persists FAILED before the retry
            # decision below is made. If Atlas dies in that window, boot
            # recovery re-queues FAILED+recovery_pending instead of stranding
            # the task. Cleared on every durable outcome (retry/block/fail).
            task.metadata["recovery_pending"] = True
        await self._finalize_failure(task, result, notify_slack=False)

        if not recovery_active:
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

        task.metadata.pop("recovery_pending", None)
        await self.tasks.persist(task)
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
            task.metadata.pop("recovery_pending", None)
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
        task.metadata.pop("recovery_pending", None)
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
        task.metadata.pop("recovery_pending", None)
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

        task.metadata.pop("recovery_pending", None)
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

    async def _check_pre_execution_approval(self, task) -> bool:
        """Governance gate before agent execution. Returns False if paused."""
        if not self.approval_engine or not self.config.approval.enabled:
            return True
        if task.metadata.get("approval_granted"):
            return True

        from core.approval_engine import ApprovalPhase

        assessment = self.approval_engine.classify(task, phase="pre_execution")
        gate = self.approval_engine.evaluate_gate(
            task, assessment, ApprovalPhase.PRE_EXECUTION
        )
        if gate.proceed:
            return True

        context = self.approval_engine.build_approval_context(
            task, assessment, ApprovalPhase.PRE_EXECUTION
        )
        self.tasks.update_status(task.id, TaskStatus.WAITING_APPROVAL)
        await self.approval_engine.pause_for_approval(task, context, update_status=False)
        await self.tasks.persist(task)
        if self.slack and self.config.slack_ready:
            await self.slack.notify_approval_waiting(task, "pre_execution")
        return False

    async def _finalize_success(self, task, result: TaskResult) -> None:
        health = self.git_safety.health.analyze(task, result)
        self.git_safety.health.record(task, health)
        logger.info("Task completed: %s | health=%.0f", task.id[:8], health.current.score)
        agent = self._find_agent_for(task)
        if agent:
            await agent.on_complete(task, result)
        await self._persist_operational_memory(task, result)

        if self.approval_engine and self.config.approval.enabled:
            summaries = self.approval_engine.build_change_summaries(task, result)
            task.metadata["engineering_report"] = {
                "change_summary": summaries.change_summary,
                "verification_summary": summaries.verification_summary,
                "recovery_summary": summaries.recovery_summary,
            }

        branch = task.metadata.get("git", {}).get("branch")
        if branch and self.pr_preparation and self.approval_engine:
            await self._prepare_pr_with_approval_gate(task, branch, result)
            return

        self.tasks.update_status(task.id, TaskStatus.COMPLETED)
        await self.tasks.persist(task)
        if self.slack and self.config.slack_ready:
            if task.metadata.get("agent") == "browser":
                await self.slack.notify_browser_success(task, result)
            else:
                await self.slack.notify_task_completed(task, result)

    def pr_manager_for(self, task) -> PRManager | None:
        """
        Per-project GitHub client (Tier 3.1). Falls back to the env-configured
        default when the project has no explicit github_owner/github_repo.
        Token always comes from GITHUB_TOKEN in the environment.
        """
        proj = self._project_config_for(task)
        if not proj or not proj.github_owner or not proj.github_repo:
            return self.pr_manager

        key = (proj.github_owner, proj.github_repo)
        cached = self._pr_managers.get(key)
        if cached:
            return cached

        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            logger.warning(
                "Project %s has GitHub config but GITHUB_TOKEN is not set",
                task.project_id,
            )
            return self.pr_manager

        client = PRManager(GitHubConfig(token=token, owner=key[0], repo=key[1]))
        self._pr_managers[key] = client
        logger.info("GitHub client ready for %s/%s", key[0], key[1])
        return client

    def _pr_preparation_for(self, task) -> PRPreparationManager | None:
        github = self.pr_manager_for(task)
        if github is self.pr_manager:
            return self.pr_preparation
        return PRPreparationManager(github)

    async def _prepare_pr_with_approval_gate(self, task, branch: str, result: TaskResult) -> None:
        """branch → checkpoint → modify → verify → summarize → approval → PR prep."""
        from core.approval_engine import ApprovalPhase

        pr_preparation = self._pr_preparation_for(task) or self.pr_preparation
        git_meta = task.metadata.get("git", {})
        repo_path = git_meta.get("repo_path")
        assessment = self.approval_engine.classify(
            task, result=result, phase="post_execution"
        )
        gate = self.approval_engine.evaluate_gate(
            task, assessment, ApprovalPhase.PR_CREATION
        )

        verification_status = (task.metadata.get("verification") or {}).get(
            "status", "passed" if result.success else "unknown"
        )
        title = task.title[:72]
        body = (
            f"Automated PR prepared by Atlas (governed — not auto-merged).\n\n"
            f"**Task:** `{task.id[:8]}`\n"
            f"**Prompt:** {(task.metadata.get('prompt') or task.description)[:500]}"
        )
        candidate = pr_preparation.build_candidate(
            task.id,
            branch,
            repo_path=str(repo_path) if repo_path else None,
            title=title,
            body=body,
            verification_status=verification_status,
            risk_level=assessment.risk_level.value,
            git_meta=git_meta,
        )
        candidate.body = pr_preparation.enrich_pr_body(
            candidate,
            {
                "change_summary": task.metadata.get("change_summary", ""),
                "verification_summary": task.metadata.get("verification_summary", ""),
                "recovery_summary": task.metadata.get("recovery_summary", ""),
            },
        )
        task.metadata["pr_candidate"] = candidate.to_dict()

        if assessment.risk_level.value == "critical":
            context = self.approval_engine.build_approval_context(
                task, assessment, ApprovalPhase.PR_CREATION, result=result
            )
            self.tasks.update_status(task.id, TaskStatus.WAITING_APPROVAL)
            await self.approval_engine.pause_for_approval(task, context, update_status=False)
            await self.tasks.persist(task)
            if self.slack and self.config.slack_ready:
                await self.slack.notify_high_risk_detected(task, assessment.risk_level.value)
                await self.slack.notify_approval_waiting(task, "pr_creation")
            return

        if gate.wait_for_approval and not gate.proceed:
            context = self.approval_engine.build_approval_context(
                task, assessment, ApprovalPhase.PR_CREATION, result=result
            )
            self.tasks.update_status(task.id, TaskStatus.WAITING_APPROVAL)
            await self.approval_engine.pause_for_approval(task, context, update_status=False)
            await self.tasks.persist(task)
            if self.slack and self.config.slack_ready:
                await self.slack.notify_approval_waiting(task, "post_execution")
            return

        base_branch = git_meta.get("base_branch") or "main"
        if repo_path:
            has_commits = await pr_preparation.has_commits_ahead(
                str(repo_path), base=base_branch
            )
            if not has_commits:
                msg = (
                    f"✅ Task `{task.id[:8]}` complete — no code changes to merge, "
                    f"branch is identical to {base_branch}."
                )
                logger.info("Task %s has no new commits — skipping PR", task.id[:8])
                self.tasks.update_status(task.id, TaskStatus.COMPLETED)
                await self.tasks.persist(task)
                if self.slack and self.config.slack_ready:
                    await self.slack.notify(msg)
                return

        prep = await pr_preparation.prepare_pr(candidate, push=bool(repo_path), create=True)
        if not prep.success:
            logger.error("PR preparation failed for %s: %s", task.id[:8], prep.error)
            if self.slack and self.config.slack_ready:
                await self.slack.notify(
                    f"⚠️ Task `{task.id[:8]}` succeeded but PR preparation failed: "
                    f"{prep.error}\nBranch `{branch}` may need manual PR."
                )
            self.tasks.update_status(task.id, TaskStatus.COMPLETED)
            await self.tasks.persist(task)
            return

        if prep.candidate:
            task.metadata["pr_number"] = prep.candidate.pr_number
            task.metadata["pr_url"] = prep.candidate.pr_url
            task.metadata["pr_candidate"] = prep.candidate.to_dict()

        if await self._maybe_auto_merge(task, assessment, prep):
            return

        context = self.approval_engine.build_approval_context(
            task, assessment, ApprovalPhase.PR_CREATION, result=result
        )
        if prep.candidate:
            context.pr_url = prep.candidate.pr_url
            context.pr_number = prep.candidate.pr_number

        self.tasks.update_status(task.id, TaskStatus.WAITING_APPROVAL)
        await self.approval_engine.pause_for_approval(task, context, update_status=False)
        await self.tasks.persist(task)

        if self.slack and self.config.slack_ready:
            await self.slack.notify_pr_candidate_prepared(task, prep.candidate)
            await self.slack.notify_approval_waiting(task, "pr_merge")

        logger.info(
            "Task %s PR candidate prepared — awaiting approval (PR #%s)",
            task.id[:8],
            prep.candidate.pr_number if prep.candidate else "?",
        )

    def _has_verified_evidence(self, task) -> bool:
        """True when at least one real verification signal passed (Tier 3.2)."""
        gates = task.metadata.get("quality_gates") or {}
        if gates and all(v == "passed" for v in gates.values()):
            return True
        verification = task.metadata.get("verification") or {}
        return verification.get("status") == "passed"

    async def _maybe_auto_merge(self, task, assessment, prep) -> bool:
        """
        Auto-merge low-risk verified work (Tier 3.2).

        Requires ALL of: approvals enabled with auto_approve_low_risk, LOW
        risk with allow_auto_pr policy, a created PR, and at least one passed
        verification signal (quality gate or browser verify). Anything else
        waits for a human. Returns True when the task was merged and closed.
        """
        if not self.config.approval.enabled:
            return False
        if not self.config.approval.auto_approve_low_risk:
            return False
        if assessment.risk_level.value != "low":
            return False
        if not prep.candidate or not prep.candidate.pr_number:
            return False
        if not self._has_verified_evidence(task):
            logger.info(
                "Task %s is low-risk but has no verified evidence — "
                "keeping human approval gate",
                task.id[:8],
            )
            return False

        requirement = self.approval_engine.policy.requirement_for(
            assessment.risk_level
        )
        if not requirement.allow_auto_pr:
            return False

        pr_number = prep.candidate.pr_number
        github = self.pr_manager_for(task)
        if not github:
            return False

        merge = await github.merge_pr(pr_number)
        if not merge.success or not merge.merged:
            logger.warning(
                "Auto-merge failed for %s (PR #%s): %s — waiting for human",
                task.id[:8],
                pr_number,
                merge.error or merge.message,
            )
            return False

        task.metadata["auto_merged"] = True
        self.tasks.update_status(task.id, TaskStatus.MERGED)
        await self.tasks.persist(task)
        logger.info("Auto-merged PR #%s for low-risk task %s", pr_number, task.id[:8])

        if self.slack and self.config.slack_ready:
            await self.slack.notify(
                f"✅ PR #{pr_number} auto-merged (low risk, verified) — "
                f"task `{task.id[:8]}` complete.\n{prep.candidate.pr_url or ''}"
            )

        await self.start_delivery_for_task(task)
        return True

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

    async def _process_delivery_queue(self) -> None:
        """Advance in-flight delivery tasks (Phase 15)."""
        from core.deployment_manager import DeploymentAction
        from core.deployment_state import DELIVERY_STATUSES

        dm = self.deployment_manager
        if not dm:
            return

        for task in self.tasks.list_in_delivery():
            if task.status not in DELIVERY_STATUSES:
                continue
            if task.metadata.get("_delivery_processing"):
                continue

            task.metadata["_delivery_processing"] = True
            try:
                step = await dm.process_delivery_task(task)
                await self._handle_deployment_step(task, step)
            except Exception:
                logger.exception("Delivery step failed for %s", task.id[:8])
            finally:
                task.metadata.pop("_delivery_processing", None)
                await self.tasks.persist(task)

    async def _handle_deployment_step(self, task, step) -> None:
        from core.deployment_manager import DeploymentAction

        dm = self.deployment_manager
        if not dm:
            return

        if step.action == DeploymentAction.NEEDS_STAGING_VERIFY:
            if task.metadata.pop("_notify_staging_deployed", None):
                if self.slack and self.config.slack_ready:
                    url = task.metadata.get("staging_url", "")
                    await self.slack.notify_staging_deployed(task, url)
            if self.config.deployment.staging_verify_enabled:
                await self._run_deployment_verification(task, environment="staging")
            return

        if step.action == DeploymentAction.NEEDS_PRODUCTION_VERIFY:
            await self._run_deployment_verification(task, environment="production")
            return

        if step.action == DeploymentAction.NEEDS_PRODUCTION_APPROVAL:
            if self.slack and self.config.slack_ready:
                await self.slack.notify_production_approval_required(task)
            return

        if step.action == DeploymentAction.FAILED:
            if self.slack and self.config.slack_ready:
                if "ci" in step.message.lower():
                    await self.slack.notify_ci_failed(task, step.message)
                else:
                    await self.slack.notify_deployment_failed(task, step.message)
            return

        if step.action == DeploymentAction.ROLLED_BACK:
            if self.slack and self.config.slack_ready:
                await self.slack.notify_deployment_rollback(task, step.message)
            return

        if step.action == DeploymentAction.COMPLETE:
            if self.slack and self.config.slack_ready:
                await self.slack.notify_deployment_succeeded(task)
            return

        if step.action == DeploymentAction.ADVANCE:
            follow = await dm.process_delivery_task(task)
            await self._handle_deployment_step(task, follow)

    async def _run_deployment_verification(self, task, *, environment: str) -> None:
        """Run browser verification against staging or production URL."""
        dm = self.deployment_manager
        if not dm:
            return

        if environment == "staging":
            verify_meta = dm.build_staging_verify_metadata(task)
        else:
            verify_meta = dm.build_production_verify_metadata(task)

        browser_agent = self._find_agent_by_name("browser")
        if not browser_agent:
            report = {"status": "skipped", "reason": "browser agent unavailable"}
            if environment == "staging":
                dm.apply_staging_verification_result(task, True, report)
            else:
                dm.apply_production_verification_result(task, True, report)
            return

        verify_task = self.tasks.create(
            title=f"Deploy verify ({environment}): {task.title[:30]}",
            description=f"Deployment verification for {environment}",
            project_id=task.project_id,
            metadata=verify_meta,
        )
        verify_task.metadata["parent_task_id"] = task.id
        verify_task.metadata["deployment_parent"] = task.id

        verify_result = await browser_agent.run(verify_task)
        self.tasks.attach_result(verify_task.id, verify_result)
        report = {
            "status": "passed" if verify_result.success else "failed",
            "environment": environment,
            "child_task_id": verify_task.id,
            "summary": verify_result.summary,
            "result": verify_result.to_dict(),
        }

        if environment == "staging":
            step = dm.apply_staging_verification_result(
                task, verify_result.success, report
            )
        else:
            step = dm.apply_production_verification_result(
                task, verify_result.success, report
            )

        await self.tasks.persist(task)
        await self._handle_deployment_step(task, step)

        if not verify_result.success and self.slack and self.config.slack_ready:
            await self.slack.notify_staging_verification_failed(task, verify_result.summary)

    async def start_delivery_for_task(self, task) -> bool:
        """Begin delivery pipeline after merge — called from controller."""
        if not self.deployment_manager or not self.config.deployment.enabled:
            return False
        if not self.config.deployment.auto_start_after_merge:
            return False
        if task.metadata.get("deployment"):
            return False

        self.deployment_manager.start_delivery_pipeline(task)
        await self.tasks.persist(task)
        if self.slack and self.config.slack_ready:
            await self.slack.notify_ci_started(task)
        return True

    async def _persist_result(self, task, result: TaskResult) -> None:
        await self._persist_operational_memory(task, result)
        if not self.memory:
            return
        logger.debug("legacy memory store: task=%s", task.id[:8])
