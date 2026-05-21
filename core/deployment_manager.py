"""
Deployment manager — coordinates delivery lifecycle (Phase 15).

Deployment coordination only. CI awareness → CICDMonitor. Governance → DeploymentPolicy.
Does not execute agents directly — returns actions for the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from core.cicd_monitor import CICDMonitor, CIWorkflowStatus, MockDeploymentProviderAdapter
from core.deployment_config import DeploymentConfig
from core.deployment_policy import DeploymentPolicy, DeploymentRiskLevel
from core.deployment_state import (
    DeploymentPhase,
    DeploymentState,
    PHASE_TO_STATUS,
    load_deployment_state,
    save_deployment_state,
)
from core.release_tracker import ReleaseTracker
from core.task_manager import TaskStatus

if TYPE_CHECKING:
    from core.task_manager import Task
    from core.task_result import TaskResult

def _logger():
    from core.logger import get_logger
    return get_logger("deployment")


class DeploymentAction(str, Enum):
    """What the orchestrator should do next."""

    WAIT = "wait"
    ADVANCE = "advance"
    NEEDS_STAGING_VERIFY = "needs_staging_verify"
    NEEDS_PRODUCTION_VERIFY = "needs_production_verify"
    NEEDS_PRODUCTION_APPROVAL = "needs_production_approval"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    COMPLETE = "complete"


@dataclass
class DeploymentStepResult:
    action: DeploymentAction
    message: str = ""
    new_phase: str | None = None


class DeploymentManager:
    """
    Coordinates: branch → PR → CI → staging → verify → production approval → deploy.

    Never auto-deploys CRITICAL risk to production.
    """

    def __init__(
        self,
        config: DeploymentConfig | None = None,
        policy: DeploymentPolicy | None = None,
        cicd: CICDMonitor | None = None,
        releases: ReleaseTracker | None = None,
    ) -> None:
        self.config = config or DeploymentConfig()
        self.policy = policy or DeploymentPolicy(self.config)
        deploy_adapter = MockDeploymentProviderAdapter(
            staging_url=self.config.staging_url_template,
            production_url=self.config.production_url_template,
        )
        self.cicd = cicd or CICDMonitor(deploy_adapter=deploy_adapter)
        self.releases = releases or ReleaseTracker()
        self._ci_run_ids: dict[str, str] = {}

    def classify_deployment_risk(self, task: Task) -> DeploymentRiskLevel:
        """Deployment-specific risk — uses RiskClassifier when available."""
        from core.risk_classifier import RiskClassifier

        level = RiskClassifier().classify_deployment_risk(task)
        return DeploymentRiskLevel(level)

    def start_delivery_pipeline(self, task: Task) -> DeploymentState:
        """Initialize deployment after merge / PR approval."""
        git = task.metadata.get("git", {})
        risk = self.classify_deployment_risk(task)
        release = self.releases.create_release(
            task,
            commit_sha=git.get("last_commit") or git.get("head_sha"),
            branch=git.get("branch"),
            environment="pipeline",
        )
        state = DeploymentState(
            release_id=release.release_id,
            task_id=task.id,
            phase=DeploymentPhase.CI_PENDING.value,
            commit_sha=release.commit_sha,
            branch=release.branch,
            deployment_risk=risk.value,
            staging_url=self.config.staging_url_template,
            production_url=self.config.production_url_template,
        )
        save_deployment_state(task, state)
        task.status = TaskStatus.CI_PENDING
        _logger().info(
            "Delivery pipeline started for %s release=%s risk=%s",
            task.id[:8],
            release.release_id[:8],
            risk.value,
        )
        return state

    async def process_delivery_task(self, task: Task) -> DeploymentStepResult:
        """
        Advance one step in the delivery state machine.

        Orchestrator calls this each tick for tasks in DELIVERY_STATUSES.
        """
        if not self.config.enabled:
            return DeploymentStepResult(DeploymentAction.WAIT, "Deployments disabled")

        state = load_deployment_state(task)
        if not state:
            return DeploymentStepResult(DeploymentAction.WAIT, "No deployment state")

        risk = DeploymentRiskLevel(state.deployment_risk)
        phase = state.deployment_phase

        if phase == DeploymentPhase.CI_PENDING:
            return await self._step_ci_pending(task, state, risk)
        if phase == DeploymentPhase.CI_RUNNING:
            return await self._step_ci_running(task, state, risk)
        if phase == DeploymentPhase.STAGING_DEPLOYING:
            return await self._step_staging_deploy(task, state, risk)
        if phase == DeploymentPhase.STAGING_VERIFYING:
            return DeploymentStepResult(
                DeploymentAction.NEEDS_STAGING_VERIFY,
                "Awaiting staging browser verification",
            )
        if phase == DeploymentPhase.READY_FOR_PRODUCTION:
            return await self._step_ready_for_production(task, state, risk)
        if phase == DeploymentPhase.PRODUCTION_PENDING_APPROVAL:
            return DeploymentStepResult(
                DeploymentAction.NEEDS_PRODUCTION_APPROVAL,
                "Production approval required",
            )
        if phase == DeploymentPhase.PRODUCTION_DEPLOYING:
            return await self._step_production_deploy(task, state, risk)
        if phase == DeploymentPhase.PRODUCTION_VERIFYING:
            return DeploymentStepResult(
                DeploymentAction.NEEDS_PRODUCTION_VERIFY,
                "Awaiting production verification",
            )

        return DeploymentStepResult(DeploymentAction.WAIT, f"Phase {phase.value} idle")

    async def _step_ci_pending(
        self, task: Task, state: DeploymentState, risk: DeploymentRiskLevel
    ) -> DeploymentStepResult:
        if self.policy.is_manual_only(risk):
            state.phase = DeploymentPhase.PRODUCTION_PENDING_APPROVAL.value
            state.deployment_summary = "Critical risk — manual CI/deploy only"
            save_deployment_state(task, state)
            return DeploymentStepResult(
                DeploymentAction.NEEDS_PRODUCTION_APPROVAL,
                state.deployment_summary,
            )

        state.phase = DeploymentPhase.CI_RUNNING.value
        save_deployment_state(task, state)
        ref = state.commit_sha or state.branch or task.id
        run_id, result = await self.cicd.start_ci(ref, branch=state.branch)
        self._ci_run_ids[task.id] = run_id
        state.ci = result.to_snapshot()
        if result.status == CIWorkflowStatus.SUCCESS:
            state.phase = DeploymentPhase.STAGING_DEPLOYING.value
            save_deployment_state(task, state)
            return DeploymentStepResult(
                DeploymentAction.ADVANCE,
                "CI passed",
                new_phase=state.phase,
            )
        if result.status == CIWorkflowStatus.FAILURE:
            return await self._fail(task, state, f"CI failed: {result.error or 'checks failed'}")

        return DeploymentStepResult(DeploymentAction.WAIT, "CI running")

    async def _step_ci_running(
        self, task: Task, state: DeploymentState, risk: DeploymentRiskLevel
    ) -> DeploymentStepResult:
        run_id = self._ci_run_ids.get(task.id, "")
        result = await self.cicd.poll_ci(run_id)
        state.ci = result.to_snapshot()
        if result.status == CIWorkflowStatus.RUNNING:
            save_deployment_state(task, state)
            return DeploymentStepResult(DeploymentAction.WAIT, "CI still running")
        if result.all_passed:
            state.phase = DeploymentPhase.STAGING_DEPLOYING.value
            save_deployment_state(task, state)
            return DeploymentStepResult(DeploymentAction.ADVANCE, "CI passed")
        return await self._fail(task, state, f"CI failed: {result.error or 'checks failed'}")

    async def _step_staging_deploy(
        self, task: Task, state: DeploymentState, risk: DeploymentRiskLevel
    ) -> DeploymentStepResult:
        if not self.policy.can_auto_deploy_staging(risk):
            state.phase = DeploymentPhase.PRODUCTION_PENDING_APPROVAL.value
            save_deployment_state(task, state)
            return DeploymentStepResult(
                DeploymentAction.NEEDS_PRODUCTION_APPROVAL,
                "Staging auto-deploy blocked for risk level",
            )

        outcome = await self.cicd.deploy_to_staging(
            release_id=state.release_id,
            commit_sha=state.commit_sha,
            branch=state.branch,
        )
        if not outcome.get("success"):
            return await self._fail(
                task, state, outcome.get("error", "Staging deploy failed")
            )

        state.staging_url = outcome.get("url") or self.config.staging_url_template
        state.deployment_summary = f"Staging deployed to {state.staging_url}"
        state.phase = DeploymentPhase.STAGING_VERIFYING.value
        save_deployment_state(task, state)
        task.metadata["staging_url"] = state.staging_url
        self.releases.record_staging_release(
            task, url=state.staging_url, summary=state.deployment_summary
        )
        task.metadata["_notify_staging_deployed"] = True
        return DeploymentStepResult(
            DeploymentAction.NEEDS_STAGING_VERIFY,
            state.deployment_summary,
            new_phase=state.phase,
        )

    async def _step_ready_for_production(
        self, task: Task, state: DeploymentState, risk: DeploymentRiskLevel
    ) -> DeploymentStepResult:
        if self.policy.must_pause_for_production_approval(risk):
            state.phase = DeploymentPhase.PRODUCTION_PENDING_APPROVAL.value
            save_deployment_state(task, state)
            return DeploymentStepResult(
                DeploymentAction.NEEDS_PRODUCTION_APPROVAL,
                "Production approval required",
            )
        if self.policy.can_auto_deploy_production(risk):
            state.phase = DeploymentPhase.PRODUCTION_DEPLOYING.value
            save_deployment_state(task, state)
            return DeploymentStepResult(DeploymentAction.ADVANCE, "Auto production deploy")
        state.phase = DeploymentPhase.PRODUCTION_PENDING_APPROVAL.value
        save_deployment_state(task, state)
        return DeploymentStepResult(
            DeploymentAction.NEEDS_PRODUCTION_APPROVAL,
            "Production approval required",
        )

    async def _step_production_deploy(
        self, task: Task, state: DeploymentState, risk: DeploymentRiskLevel
    ) -> DeploymentStepResult:
        if self.policy.is_manual_only(risk) or self.config.never_auto_deploy_critical:
            if risk == DeploymentRiskLevel.CRITICAL:
                return DeploymentStepResult(
                    DeploymentAction.NEEDS_PRODUCTION_APPROVAL,
                    "Critical — manual production deploy only",
                )

        outcome = await self.cicd.deploy_to_production(
            release_id=state.release_id,
            commit_sha=state.commit_sha,
        )
        if not outcome.get("success"):
            return await self._fail(
                task, state, outcome.get("error", "Production deploy failed")
            )

        state.production_url = outcome.get("url") or self.config.production_url_template
        state.deployment_summary = f"Production deployed to {state.production_url}"
        state.phase = DeploymentPhase.PRODUCTION_VERIFYING.value
        save_deployment_state(task, state)
        return DeploymentStepResult(
            DeploymentAction.NEEDS_PRODUCTION_VERIFY,
            state.deployment_summary,
        )

    def apply_staging_verification_result(
        self, task: Task, success: bool, report: dict[str, Any]
    ) -> DeploymentStepResult:
        state = load_deployment_state(task)
        if not state:
            return DeploymentStepResult(DeploymentAction.FAILED, "No deployment state")

        state.verification_report = report
        if success:
            state.health_status = "healthy"
            state.phase = DeploymentPhase.READY_FOR_PRODUCTION.value
            state.deployment_summary = "Staging verification passed"
            save_deployment_state(task, state)
            self.releases.record_staging_release(
                task,
                url=state.staging_url or "",
                summary=state.deployment_summary,
                verification=report,
            )
            risk = DeploymentRiskLevel(state.deployment_risk)
            if self.policy.must_pause_for_production_approval(risk):
                state.phase = DeploymentPhase.PRODUCTION_PENDING_APPROVAL.value
                save_deployment_state(task, state)
                return DeploymentStepResult(
                    DeploymentAction.NEEDS_PRODUCTION_APPROVAL,
                    "Staging OK — production approval required",
                )
            return DeploymentStepResult(
                DeploymentAction.ADVANCE,
                "Staging verified — ready for production",
            )

        state.health_status = "unhealthy"
        return self._fail_sync(
            task, state, "Staging verification failed", rollback=True
        )

    def apply_production_verification_result(
        self, task: Task, success: bool, report: dict[str, Any]
    ) -> DeploymentStepResult:
        state = load_deployment_state(task)
        if not state:
            return DeploymentStepResult(DeploymentAction.FAILED, "No deployment state")

        state.verification_report = report
        if success:
            state.health_status = "healthy"
            state.phase = DeploymentPhase.DEPLOYED.value
            state.deployment_summary = "Production verification passed — release complete"
            save_deployment_state(task, state)
            self.releases.record_production_release(
                task,
                url=state.production_url or "",
                summary=state.deployment_summary,
                approved_by=task.metadata.get("deploy_approved_by"),
            )
            return DeploymentStepResult(DeploymentAction.COMPLETE, state.deployment_summary)

        return self._fail_sync(
            task, state, "Production verification failed", rollback=True
        )

    async def approve_production_deploy(
        self, task: Task, *, user_id: str | None = None
    ) -> DeploymentStepResult:
        state = load_deployment_state(task)
        if not state:
            return DeploymentStepResult(DeploymentAction.FAILED, "No deployment state")

        task.metadata["deploy_approved"] = True
        task.metadata["deploy_approved_by"] = user_id
        state.phase = DeploymentPhase.PRODUCTION_DEPLOYING.value
        save_deployment_state(task, state)
        return await self._step_production_deploy(
            task, state, DeploymentRiskLevel(state.deployment_risk)
        )

    async def rollback_release(
        self, task: Task, reason: str, *, target: str = "staging"
    ) -> DeploymentStepResult:
        state = load_deployment_state(task)
        if not state:
            return DeploymentStepResult(DeploymentAction.FAILED, "No deployment state")

        outcome = await self.cicd.rollback_deployment(
            release_id=state.release_id, target=target
        )
        diagnostics = list(state.diagnostics) + [reason]
        if not outcome.get("success"):
            diagnostics.append(outcome.get("error", "rollback failed"))

        state.phase = DeploymentPhase.ROLLED_BACK.value
        state.health_status = "rolled_back"
        state.diagnostics = diagnostics
        state.deployment_summary = f"Rolled back ({target}): {reason}"
        save_deployment_state(task, state)
        self.releases.record_rollback(
            task, reason=reason, target=target, diagnostics=diagnostics
        )
        return DeploymentStepResult(
            DeploymentAction.ROLLED_BACK,
            state.deployment_summary,
        )

    def build_staging_verify_metadata(self, task: Task) -> dict[str, Any]:
        state = load_deployment_state(task)
        url = (state.staging_url if state else None) or task.metadata.get(
            "staging_url", self.config.staging_url_template
        )
        return {
            "agent": "browser",
            "recovery_enabled": False,
            "url": url,
            "steps": [
                {"action": "goto", "url": url},
                {"action": "assert_visible", "selector": "body"},
                {"action": "screenshot", "label": "staging-verify"},
            ],
            "max_runtime_sec": 90,
            "fail_on_network_error": True,
            "deployment_verify": True,
            "environment": "staging",
        }

    def build_production_verify_metadata(self, task: Task) -> dict[str, Any]:
        state = load_deployment_state(task)
        url = (state.production_url if state else None) or self.config.production_url_template
        return {
            "agent": "browser",
            "recovery_enabled": False,
            "url": url,
            "steps": [
                {"action": "goto", "url": url},
                {"action": "assert_visible", "selector": "body"},
                {"action": "screenshot", "label": "production-verify"},
            ],
            "max_runtime_sec": 90,
            "deployment_verify": True,
            "environment": "production",
        }

    async def _fail(
        self, task: Task, state: DeploymentState, reason: str, *, rollback: bool = False
    ) -> DeploymentStepResult:
        if rollback:
            await self.cicd.rollback_deployment(
                release_id=state.release_id, target="staging"
            )
            state.phase = DeploymentPhase.ROLLED_BACK.value
            self.releases.record_rollback(task, reason=reason)
        else:
            if "staging" in reason.lower():
                state.phase = DeploymentPhase.STAGING_FAILED.value
            elif "ci" in reason.lower():
                state.phase = DeploymentPhase.CI_FAILED.value
            else:
                state.phase = DeploymentPhase.DEPLOYMENT_FAILED.value
        state.health_status = "unhealthy"
        state.diagnostics.append(reason)
        state.deployment_summary = reason
        save_deployment_state(task, state)
        return DeploymentStepResult(DeploymentAction.FAILED, reason)

    def _fail_sync(
        self, task: Task, state: DeploymentState, reason: str, *, rollback: bool = False
    ) -> DeploymentStepResult:
        if rollback:
            state.phase = DeploymentPhase.ROLLED_BACK.value
            self.releases.record_rollback(task, reason=reason)
        else:
            state.phase = DeploymentPhase.STAGING_FAILED.value
        state.health_status = "unhealthy"
        state.diagnostics.append(reason)
        state.deployment_summary = reason
        save_deployment_state(task, state)
        return DeploymentStepResult(
            DeploymentAction.ROLLED_BACK if rollback else DeploymentAction.FAILED,
            reason,
        )

    def format_deployment_status(self, task: Task) -> str:
        return self.releases.format_release_summary(task)
