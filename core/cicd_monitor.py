"""
CI/CD monitoring (Phase 15) — CI awareness only.

Provider adapters are mock/integration-friendly; no full cloud integrations yet.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from core.deployment_state import CICheckSnapshot


class CIWorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass
class CIWorkflowResult:
    status: CIWorkflowStatus
    build: str = "pending"
    test: str = "pending"
    lint: str = "pending"
    workflow: str = "pending"
    run_id: str | None = None
    url: str | None = None
    error: str | None = None
    completed_at: str | None = None

    def to_snapshot(self) -> CICheckSnapshot:
        return CICheckSnapshot(
            build=self.build,
            test=self.test,
            lint=self.lint,
            workflow=self.workflow,
        )

    @property
    def all_passed(self) -> bool:
        return self.status == CIWorkflowStatus.SUCCESS


class CIProviderAdapter(ABC):
    """Abstraction for CI systems (GitHub Actions, GitLab CI, etc.)."""

    @abstractmethod
    async def trigger_workflow(self, ref: str, *, branch: str | None = None) -> str:
        """Start CI; return run id."""
        ...

    @abstractmethod
    async def get_workflow_status(self, run_id: str) -> CIWorkflowResult:
        ...

    @abstractmethod
    async def list_checks(self, ref: str) -> CIWorkflowResult:
        ...


class DeploymentProviderAdapter(ABC):
    """Abstraction for deployment targets (Vercel, K8s, etc.)."""

    @abstractmethod
    async def deploy_staging(
        self, *, release_id: str, commit_sha: str | None, branch: str | None
    ) -> dict[str, Any]:
        """Deploy to staging; return url + metadata."""
        ...

    @abstractmethod
    async def deploy_production(
        self, *, release_id: str, commit_sha: str | None
    ) -> dict[str, Any]:
        """Deploy to production; return url + metadata."""
        ...

    @abstractmethod
    async def rollback(
        self, *, release_id: str, target: str = "staging"
    ) -> dict[str, Any]:
        """Rollback deployment."""
        ...


@dataclass
class MockGitHubActionsAdapter(CIProviderAdapter):
    """
    Mock GitHub Actions — simulates build/test/lint for integration tests.

    TODO: real GitHub Actions API
    """

    simulate_failure: bool = False
    run_delay_sec: float = 0.5
    _runs: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def trigger_workflow(self, ref: str, *, branch: str | None = None) -> str:
        run_id = f"gha-mock-{ref[:8]}-{len(self._runs)}"
        self._runs[run_id] = {
            "ref": ref,
            "branch": branch,
            "started": datetime.now(timezone.utc).isoformat(),
            "status": CIWorkflowStatus.RUNNING.value,
        }
        await asyncio.sleep(self.run_delay_sec)
        return run_id

    async def get_workflow_status(self, run_id: str) -> CIWorkflowResult:
        run = self._runs.get(run_id)
        if not run:
            return CIWorkflowResult(
                status=CIWorkflowStatus.FAILURE,
                error=f"Unknown run {run_id}",
            )
        if run.get("status") == CIWorkflowStatus.RUNNING.value:
            await asyncio.sleep(self.run_delay_sec)
            if self.simulate_failure:
                run["status"] = CIWorkflowStatus.FAILURE.value
                return CIWorkflowResult(
                    status=CIWorkflowStatus.FAILURE,
                    build="failure",
                    test="failure",
                    lint="skipped",
                    workflow="failure",
                    run_id=run_id,
                    error="Mock CI failure",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
            run["status"] = CIWorkflowStatus.SUCCESS.value
        return CIWorkflowResult(
            status=CIWorkflowStatus.SUCCESS,
            build="success",
            test="success",
            lint="success",
            workflow="success",
            run_id=run_id,
            url=f"https://github.com/mock/actions/runs/{run_id}",
            completed_at=datetime.now(timezone.utc).isoformat(),
        )

    async def list_checks(self, ref: str) -> CIWorkflowResult:
        run_id = await self.trigger_workflow(ref)
        return await self.get_workflow_status(run_id)


@dataclass
class MockDeploymentProviderAdapter(DeploymentProviderAdapter):
    """
    Mock deploy provider — returns configurable staging/production URLs.

    TODO: Vercel / Railway / Render / K8s providers
    """

    staging_url: str = "https://staging.example.com"
    production_url: str = "https://app.example.com"
    simulate_failure: bool = False

    async def deploy_staging(
        self, *, release_id: str, commit_sha: str | None, branch: str | None
    ) -> dict[str, Any]:
        await asyncio.sleep(0.2)
        if self.simulate_failure:
            return {"success": False, "error": "Mock staging deploy failed"}
        return {
            "success": True,
            "url": self.staging_url,
            "release_id": release_id,
            "environment": "staging",
            "commit_sha": commit_sha,
            "branch": branch,
        }

    async def deploy_production(
        self, *, release_id: str, commit_sha: str | None
    ) -> dict[str, Any]:
        await asyncio.sleep(0.2)
        if self.simulate_failure:
            return {"success": False, "error": "Mock production deploy failed"}
        return {
            "success": True,
            "url": self.production_url,
            "release_id": release_id,
            "environment": "production",
            "commit_sha": commit_sha,
        }

    async def rollback(
        self, *, release_id: str, target: str = "staging"
    ) -> dict[str, Any]:
        await asyncio.sleep(0.1)
        return {
            "success": True,
            "release_id": release_id,
            "target": target,
            "rolled_back_at": datetime.now(timezone.utc).isoformat(),
        }


class CICDMonitor:
    """
    Monitors CI/CD status via provider adapters.

    Does not coordinate full deployment lifecycle — DeploymentManager does.
    """

    def __init__(
        self,
        ci_adapter: CIProviderAdapter | None = None,
        deploy_adapter: DeploymentProviderAdapter | None = None,
    ) -> None:
        self.ci = ci_adapter or MockGitHubActionsAdapter()
        self.deploy = deploy_adapter or MockDeploymentProviderAdapter()

    async def start_ci(self, ref: str, *, branch: str | None = None) -> tuple[str, CIWorkflowResult]:
        run_id = await self.ci.trigger_workflow(ref, branch=branch)
        result = await self.ci.get_workflow_status(run_id)
        return run_id, result

    async def poll_ci(self, run_id: str) -> CIWorkflowResult:
        return await self.ci.get_workflow_status(run_id)

    async def deploy_to_staging(
        self, *, release_id: str, commit_sha: str | None, branch: str | None
    ) -> dict[str, Any]:
        return await self.deploy.deploy_staging(
            release_id=release_id, commit_sha=commit_sha, branch=branch
        )

    async def deploy_to_production(
        self, *, release_id: str, commit_sha: str | None
    ) -> dict[str, Any]:
        return await self.deploy.deploy_production(
            release_id=release_id, commit_sha=commit_sha
        )

    async def rollback_deployment(
        self, *, release_id: str, target: str = "staging"
    ) -> dict[str, Any]:
        return await self.deploy.rollback(release_id=release_id, target=target)
