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
import urllib.error
import urllib.request
from dataclasses import dataclass
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
