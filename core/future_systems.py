"""
Future distributed execution hooks (Phase 8 placeholders).

NOT implemented — architectural extension points only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class DistributedWorkerSlot:
    """TODO: distributed workers — remote agent execution nodes."""

    worker_id: str = ""
    endpoint: str = ""
    capabilities: list[str] | None = None


@dataclass
class CloudExecutionNode:
    """TODO: cloud execution nodes — VM/container fleet."""

    region: str = ""
    pool_size: int = 0


@dataclass
class RemoteBrowserRunner:
    """TODO: remote browser runners — Playwright offloaded to dedicated host."""

    runner_url: str = ""


@dataclass
class ContainerizedExecution:
    """TODO: containerized execution — Docker/K8s task sandboxes."""

    image: str = ""
    namespace: str = ""


@dataclass
class DeploymentPipelineHook:
    """TODO: deployment pipelines — CI/CD integration after task success."""

    pipeline_id: str = ""


@dataclass
class AutonomousPRLifecycle:
    """TODO: autonomous PR lifecycle — open/review/merge without human."""

    repo: str = ""
    auto_merge: bool = False


@dataclass
class GitHubPRAPIHook:
    """TODO: GitHub PR APIs — extended review, checks, draft promotion."""

    repo: str = ""


@dataclass
class GitLabIntegrationHook:
    """TODO: GitLab merge request integration."""

    project_id: str = ""


@dataclass
class AutomatedReviewerHook:
    """TODO: automated reviewers — CODEOWNERS, bot review assignment."""

    enabled: bool = False


@dataclass
class CICDPipelineHook:
    """TODO: CI/CD pipelines — required checks before merge approval."""

    pipeline_id: str = ""


@dataclass
class DeploymentApprovalHook:
    """TODO: deployment approvals — staged promote with human gate."""

    environment: str = ""


@dataclass
class SemanticDiffHook:
    """TODO: semantic diff analysis — intent-aware change review."""

    enabled: bool = False


@dataclass
class ArchitectureDriftHook:
    """TODO: architecture drift detection — boundary violations."""

    enabled: bool = False


@dataclass
class KubernetesDeployHook:
    """TODO: Kubernetes deployments — helm/kubectl rollout."""

    cluster: str = ""


@dataclass
class DockerDeployHook:
    """TODO: Docker image build and registry push."""

    registry: str = ""


@dataclass
class VercelDeployHook:
    """TODO: Vercel preview/production promote."""

    project_id: str = ""


@dataclass
class RailwayDeployHook:
    """TODO: Railway environment deploy."""

    service_id: str = ""


@dataclass
class RenderDeployHook:
    """TODO: Render blueprint / service deploy."""

    service_id: str = ""


@dataclass
class CloudProviderDeployHook:
    """TODO: AWS/GCP deployment orchestration."""

    region: str = ""


@dataclass
class CanaryDeployHook:
    """TODO: canary deployments — gradual traffic shift."""

    enabled: bool = False


@dataclass
class BlueGreenDeployHook:
    """TODO: blue-green deployments — swap after verification."""

    enabled: bool = False


@dataclass
class TrafficShiftingHook:
    """TODO: traffic shifting — weighted routing during rollout."""

    enabled: bool = False


@dataclass
class ObservabilityHook:
    """TODO: real observability — Datadog, Prometheus, Sentry release health."""

    provider: str = ""


class FutureSystemsRegistry:
    """
    Registry of unimplemented future capabilities.

    RuntimeManager and Orchestrator should NOT depend on these yet.
    """

    distributed_workers: list[DistributedWorkerSlot] | None = None
    cloud_nodes: list[CloudExecutionNode] | None = None
    remote_browsers: list[RemoteBrowserRunner] | None = None
    containers: list[ContainerizedExecution] | None = None
    deployment_pipelines: list[DeploymentPipelineHook] | None = None
    pr_lifecycle: AutonomousPRLifecycle | None = None
    github_pr_api: GitHubPRAPIHook | None = None
    gitlab_integration: GitLabIntegrationHook | None = None
    automated_reviewers: AutomatedReviewerHook | None = None
    cicd_pipelines: CICDPipelineHook | None = None
    deployment_approvals: DeploymentApprovalHook | None = None
    semantic_diff: SemanticDiffHook | None = None
    architecture_drift: ArchitectureDriftHook | None = None
    kubernetes: KubernetesDeployHook | None = None
    docker_deploy: DockerDeployHook | None = None
    vercel: VercelDeployHook | None = None
    railway: RailwayDeployHook | None = None
    render: RenderDeployHook | None = None
    cloud_providers: CloudProviderDeployHook | None = None
    canary_deployments: CanaryDeployHook | None = None
    blue_green_deployments: BlueGreenDeployHook | None = None
    traffic_shifting: TrafficShiftingHook | None = None
    observability: ObservabilityHook | None = None

    @classmethod
    def placeholder_status(cls) -> dict[str, Any]:
        return {
            "distributed_workers": "not_implemented",
            "cloud_execution_nodes": "not_implemented",
            "remote_browser_runners": "not_implemented",
            "containerized_execution": "not_implemented",
            "deployment_pipelines": "not_implemented",
            "autonomous_pr_lifecycle": "not_implemented",
            "github_pr_api": "not_implemented",
            "gitlab_integration": "not_implemented",
            "automated_reviewers": "not_implemented",
            "cicd_pipelines": "not_implemented",
            "deployment_approvals": "not_implemented",
            "semantic_diff_analysis": "not_implemented",
            "architecture_drift_detection": "not_implemented",
            "kubernetes": "not_implemented",
            "docker": "not_implemented",
            "vercel": "not_implemented",
            "railway": "not_implemented",
            "render": "not_implemented",
            "aws_gcp_deploy": "not_implemented",
            "canary_deployments": "not_implemented",
            "blue_green_deployments": "not_implemented",
            "traffic_shifting": "not_implemented",
            "observability_systems": "not_implemented",
            # Phase 13+ provider hooks (see core/worker_pool.py LocalPool)
            "openai_workers": "not_implemented",
            "gemini_workers": "not_implemented",
            "ollama_local_models": "not_implemented",
            "distributed_worker_fleet": "not_implemented",
            "cloud_runners": "not_implemented",
            "dynamic_cost_optimization": "not_implemented",
        }
