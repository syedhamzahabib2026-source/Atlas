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

    @classmethod
    def placeholder_status(cls) -> dict[str, Any]:
        return {
            "distributed_workers": "not_implemented",
            "cloud_execution_nodes": "not_implemented",
            "remote_browser_runners": "not_implemented",
            "containerized_execution": "not_implemented",
            "deployment_pipelines": "not_implemented",
            "autonomous_pr_lifecycle": "not_implemented",
        }
