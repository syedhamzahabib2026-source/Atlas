"""
GitHub API operations for Atlas PR lifecycle.

Reads GITHUB_TOKEN, GITHUB_REPO_OWNER, GITHUB_REPO_NAME from the environment
(populated by .env via load_config before this module is used).

Uses stdlib urllib only — no new dependencies.
Follows the same pattern as git_manager.py:
  - GitHubConfig dataclass holds all credentials
  - PRManager takes a config object
  - All async methods return structured dataclasses with success + error fields
  - Network I/O runs in a thread via asyncio.to_thread (urllib is sync)
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.logger import get_logger

logger = get_logger("git.pr")

_API_BASE = "https://api.github.com"


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class GitHubConfig:
    """GitHub credentials and target repo. Populate from env after load_config()."""

    token: str
    owner: str
    repo: str

    @classmethod
    def from_env(cls) -> GitHubConfig | None:
        """Return config if all three env vars are set, else None."""
        token = os.environ.get("GITHUB_TOKEN", "")
        owner = os.environ.get("GITHUB_REPO_OWNER", "")
        repo = os.environ.get("GITHUB_REPO_NAME", "")
        if not token or not owner or not repo:
            return None
        return cls(token=token, owner=owner, repo=repo)


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class PRResult:
    """Returned by create_pr()."""

    success: bool
    pr_number: int | None = None
    pr_url: str | None = None
    error: str | None = None


@dataclass
class MergeResult:
    """Returned by merge_pr()."""

    success: bool
    merged: bool = False
    message: str = ""
    error: str | None = None


@dataclass
class PushResult:
    """Returned by push_branch()."""

    success: bool
    error: str | None = None


@dataclass
class PRStatusResult:
    """Returned by get_pr_status()."""

    success: bool
    state: str | None = None   # "open" | "merged" | "closed"
    pr_number: int | None = None
    merged: bool = False
    error: str | None = None


# ── Internal exception ────────────────────────────────────────────────────────

class _GitHubAPIError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ── Manager ───────────────────────────────────────────────────────────────────

class PRManager:
    """
    GitHub API client for PR lifecycle operations.

    Usage:
        cfg = GitHubConfig.from_env()
        if cfg is None:
            # GITHUB_TOKEN / OWNER / REPO not set — skip PR operations
            ...
        pr = PRManager(cfg)
        result = await pr.create_pr(task_id, branch, title, body)
    """

    def __init__(self, config: GitHubConfig) -> None:
        self.config = config

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Synchronous GitHub API call — always run via asyncio.to_thread."""
        url = f"{_API_BASE}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, headers=self._headers(), method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                msg = json.loads(raw).get("message", str(exc))
            except Exception:
                msg = str(exc)
            raise _GitHubAPIError(exc.code, msg) from exc

    async def _run(self, fn, *args, **kwargs) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    # ── Public API ────────────────────────────────────────────────────────────

    async def push_branch(self, branch_name: str, repo_path: Path | str) -> PushResult:
        """
        Push branch_name to origin from repo_path via subprocess git.

        Runs git push origin <branch_name> synchronously in a thread,
        matching the asyncio.to_thread pattern used by git_manager.py.
        """
        def _push() -> None:
            result = subprocess.run(
                ["git", "push", "origin", branch_name],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "git push failed")

        try:
            await self._run(_push)
            logger.info("Pushed branch %s to origin", branch_name)
            return PushResult(success=True)
        except subprocess.TimeoutExpired:
            msg = f"git push timed out for branch {branch_name}"
            logger.error(msg)
            return PushResult(success=False, error=msg)
        except Exception as exc:
            logger.error("push_branch failed for %s: %s", branch_name, exc)
            return PushResult(success=False, error=str(exc))

    async def create_pr(
        self,
        task_id: str,
        branch_name: str,
        title: str,
        body: str,
        *,
        base: str = "main",
        draft: bool = False,
    ) -> PRResult:
        """
        Open a pull request from branch_name into base.

        Returns PRResult with pr_number and pr_url on success.
        """
        def _call() -> dict[str, Any]:
            return self._request(
                "POST",
                f"/repos/{self.config.owner}/{self.config.repo}/pulls",
                {
                    "title": title,
                    "body": body,
                    "head": branch_name,
                    "base": base,
                    "draft": draft,
                },
            )

        try:
            data = await self._run(_call)
            pr_number = data["number"]
            pr_url = data["html_url"]
            logger.info("PR #%s created for task %s: %s", pr_number, task_id[:8], pr_url)
            return PRResult(success=True, pr_number=pr_number, pr_url=pr_url)
        except _GitHubAPIError as exc:
            logger.error(
                "create_pr failed for task %s (HTTP %s): %s",
                task_id[:8], exc.status, exc.message,
            )
            return PRResult(success=False, error=f"HTTP {exc.status}: {exc.message}")
        except Exception as exc:
            logger.exception("create_pr unexpected error for task %s", task_id[:8])
            return PRResult(success=False, error=str(exc))

    async def merge_pr(
        self,
        pr_number: int,
        *,
        merge_method: str = "squash",
        commit_title: str | None = None,
        commit_message: str | None = None,
    ) -> MergeResult:
        """
        Merge a PR via the GitHub API.

        merge_method: "merge" | "squash" | "rebase"
        Returns MergeResult with merged=True on success.
        """
        def _call() -> dict[str, Any]:
            payload: dict[str, Any] = {"merge_method": merge_method}
            if commit_title:
                payload["commit_title"] = commit_title
            if commit_message:
                payload["commit_message"] = commit_message
            return self._request(
                "PUT",
                f"/repos/{self.config.owner}/{self.config.repo}/pulls/{pr_number}/merge",
                payload,
            )

        try:
            data = await self._run(_call)
            merged = bool(data.get("merged", False))
            message = data.get("message", "")
            logger.info("PR #%s merged=%s via %s", pr_number, merged, merge_method)
            return MergeResult(success=True, merged=merged, message=message)
        except _GitHubAPIError as exc:
            logger.error(
                "merge_pr #%s failed (HTTP %s): %s", pr_number, exc.status, exc.message
            )
            return MergeResult(success=False, error=f"HTTP {exc.status}: {exc.message}")
        except Exception as exc:
            logger.exception("merge_pr #%s unexpected error", pr_number)
            return MergeResult(success=False, error=str(exc))

    async def get_pr_status(self, pr_number: int) -> PRStatusResult:
        """
        Return the current state of a PR.

        state is "open", "merged", or "closed" (closed without merging).
        GitHub's API returns state="closed" for both merged and unmerged-closed PRs;
        we distinguish them via the merged_at field.
        """
        def _call() -> dict[str, Any]:
            return self._request(
                "GET",
                f"/repos/{self.config.owner}/{self.config.repo}/pulls/{pr_number}",
            )

        try:
            data = await self._run(_call)
            merged = bool(data.get("merged_at"))
            state = data.get("state", "unknown")
            if state == "closed" and merged:
                state = "merged"
            logger.debug("PR #%s status: %s", pr_number, state)
            return PRStatusResult(
                success=True,
                state=state,
                pr_number=pr_number,
                merged=merged,
            )
        except _GitHubAPIError as exc:
            logger.error(
                "get_pr_status #%s failed (HTTP %s): %s",
                pr_number, exc.status, exc.message,
            )
            return PRStatusResult(success=False, error=f"HTTP {exc.status}: {exc.message}")
        except Exception as exc:
            logger.exception("get_pr_status #%s unexpected error", pr_number)
            return PRStatusResult(success=False, error=str(exc))


# ── PR preparation lifecycle (Phase 14) ───────────────────────────────────────
# GitHub API client above; preparation flow below. Does NOT merge automatically.


@dataclass
class PRCandidate:
    """PR-ready work package — safe preparation without auto-merge."""

    task_id: str
    branch_name: str
    repo_path: str | None
    title: str
    body: str
    verification_status: str = "unknown"
    approval_status: str = "pending"
    risk_level: str = "low"
    checkpoint_refs: list[str] = field(default_factory=list)
    pr_number: int | None = None
    pr_url: str | None = None
    prepared_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "branch_name": self.branch_name,
            "repo_path": self.repo_path,
            "title": self.title,
            "body": self.body,
            "verification_status": self.verification_status,
            "approval_status": self.approval_status,
            "risk_level": self.risk_level,
            "checkpoint_refs": self.checkpoint_refs,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "prepared_at": self.prepared_at,
        }


@dataclass
class PRPreparationResult:
    success: bool
    candidate: PRCandidate | None = None
    pushed: bool = False
    pr_created: bool = False
    error: str | None = None
    skip_reason: str | None = None


class PRPreparationManager:
    """
    PR preparation lifecycle only: branch → checkpoint → modify → verify → summarize → approval → PR prep.

    Does NOT auto-merge. Delegates GitHub HTTP to PRManager (GitHub client).
    """

    def __init__(self, github: PRManager | None) -> None:
        self.github = github

    def build_candidate(
        self,
        task_id: str,
        branch: str,
        *,
        repo_path: str | None,
        title: str,
        body: str,
        verification_status: str = "unknown",
        risk_level: str = "low",
        git_meta: dict[str, Any] | None = None,
    ) -> PRCandidate:
        from datetime import datetime, timezone

        checkpoints = (git_meta or {}).get("checkpoints", [])
        refs = [c.get("commit_hash", "")[:12] for c in checkpoints if c.get("commit_hash")]
        return PRCandidate(
            task_id=task_id,
            branch_name=branch,
            repo_path=repo_path,
            title=title[:72],
            body=body,
            verification_status=verification_status,
            approval_status="pending",
            risk_level=risk_level,
            checkpoint_refs=refs,
            prepared_at=datetime.now(timezone.utc).isoformat(),
        )

    def enrich_pr_body(self, candidate: PRCandidate, summaries: dict[str, str]) -> str:
        """Append engineering summaries to PR description."""
        parts = [candidate.body, "", "---", "*Atlas change summary*"]
        if summaries.get("change_summary"):
            parts.append(summaries["change_summary"][:2000])
        if summaries.get("verification_summary"):
            parts.append(f"\n*Verification:* {summaries['verification_summary']}")
        if summaries.get("recovery_summary"):
            parts.append(f"\n*Recovery:* {summaries['recovery_summary']}")
        if candidate.checkpoint_refs:
            parts.append(f"\n*Rollback checkpoints:* {', '.join(candidate.checkpoint_refs)}")
        return "\n".join(parts)

    async def prepare_pr(
        self,
        candidate: PRCandidate,
        *,
        push: bool = True,
        create: bool = True,
        draft: bool = False,
    ) -> PRPreparationResult:
        """
        Push branch and open PR if configured. Never merges.

        Flow: branch → (already modified) → verify → summarize → approval → PR prep
        """
        if not self.github:
            return PRPreparationResult(
                success=False,
                error="GitHub PR client not configured",
            )

        if push and candidate.repo_path:
            push_result = await self.github.push_branch(
                candidate.branch_name, candidate.repo_path
            )
            if not push_result.success:
                return PRPreparationResult(
                    success=False,
                    candidate=candidate,
                    error=push_result.error,
                )

        if not create:
            candidate.approval_status = "ready_no_pr"
            return PRPreparationResult(success=True, candidate=candidate, pushed=push)

        pr_result = await self.github.create_pr(
            task_id=candidate.task_id,
            branch_name=candidate.branch_name,
            title=candidate.title,
            body=candidate.body,
            draft=draft,
        )
        if not pr_result.success:
            return PRPreparationResult(
                success=False,
                candidate=candidate,
                pushed=push,
                error=pr_result.error,
            )

        candidate.pr_number = pr_result.pr_number
        candidate.pr_url = pr_result.pr_url
        candidate.approval_status = "pr_prepared"
        return PRPreparationResult(
            success=True,
            candidate=candidate,
            pushed=push,
            pr_created=True,
        )

    async def has_commits_ahead(self, repo_path: str, base: str = "main") -> bool:
        """True if branch has commits not on origin/base."""
        import asyncio

        def _check() -> bool:
            result = subprocess.run(
                ["git", "log", f"origin/{base}..HEAD", "--oneline"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return bool(result.stdout.strip())

        try:
            return await asyncio.to_thread(_check)
        except Exception:
            return True
