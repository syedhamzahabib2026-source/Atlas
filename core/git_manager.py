"""
Git safety layer for Atlas (Phase 6).

Checkpoints, isolated branches, rollback — NOT embedded in agents.

TODO: semantic diff analysis
TODO: architecture drift detection
TODO: autonomous PR generation
TODO: multi-branch experimentation / merge intelligence
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.logger import get_logger

logger = get_logger("git")

try:
    import git
    from git import Repo

    HAS_GITPYTHON = True
except ImportError:
    HAS_GITPYTHON = False
    Repo = None  # type: ignore


@dataclass
class CheckpointRecord:
    commit_hash: str
    timestamp: str
    strategy: str
    attempt: int
    task_id: str
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GitWorkspaceMeta:
    """Tracked on task.metadata['git']."""

    repo_path: str
    branch: str
    base_branch: str
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    rollback_history: list[dict[str, Any]] = field(default_factory=list)
    dependency_modify_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GitManager:
    """Repository safety operations via GitPython."""

    def __init__(self, config) -> None:
        self.config = config

    @staticmethod
    def is_available() -> bool:
        return HAS_GITPYTHON

    def _repo(self, path: Path) -> Repo:
        if not HAS_GITPYTHON:
            raise RuntimeError("GitPython not installed. pip install GitPython")
        return Repo(str(path))

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def find_repo(self, project_path: Path) -> Path | None:
        """Return git root if project_path is inside a repository."""

        def _find():
            p = project_path.resolve()
            if (p / ".git").exists():
                return p
            try:
                if HAS_GITPYTHON:
                    repo = Repo(str(p), search_parent_directories=True)
                    return Path(repo.working_dir)
            except Exception:
                pass
            return None

        return await self._run(_find)

    async def is_dirty(self, repo_path: Path) -> bool:
        def _check():
            repo = self._repo(repo_path)
            return repo.is_dirty(untracked_files=True)

        return await self._run(_check)

    async def current_branch(self, repo_path: Path) -> str:
        def _branch():
            return self._repo(repo_path).active_branch.name

        return await self._run(_branch)

    async def is_protected_branch(self, branch: str) -> bool:
        return branch in self.config.protected_branches

    def task_branch_name(self, task_id: str) -> str:
        short = task_id.replace("-", "")[:12]
        return f"{self.config.branch_prefix}-{short}"

    async def ensure_task_branch(
        self,
        repo_path: Path,
        task_id: str,
        base_branch: str | None = None,
    ) -> GitWorkspaceMeta:
        """
        Create and checkout isolated branch atlas/task-<id>.
        Never leave autonomous work on main/master when block_work_on_protected.
        """
        branch = self.task_branch_name(task_id)

        def _ensure():
            repo = self._repo(repo_path)
            base = base_branch or self._detect_base(repo)
            current = repo.active_branch.name

            if self.config.block_work_on_protected and current in self.config.protected_branches:
                logger.info("Leaving protected branch %s for isolated branch", current)

            existing = [h.name for h in repo.heads]
            if branch in existing:
                repo.heads[branch].checkout()
                logger.info("Checked out existing branch %s", branch)
            else:
                start = base if base in repo.heads else repo.active_branch.name
                try:
                    repo.git.checkout("-b", branch, start)
                except Exception:
                    repo.git.checkout("-b", branch)
                logger.info("Created branch %s from %s", branch, start)

            return GitWorkspaceMeta(
                repo_path=str(repo_path),
                branch=branch,
                base_branch=base,
            )

        meta = await self._run(_ensure)
        return meta

    def _detect_base(self, repo: Repo) -> str:
        for name in ("main", "master", "develop"):
            if name in repo.heads:
                return name
        return repo.active_branch.name

    async def create_checkpoint(
        self,
        repo_path: Path,
        task_id: str,
        strategy: str,
        attempt: int,
        *,
        allow_empty: bool = False,
    ) -> CheckpointRecord | None:
        """Commit current workspace state as atlas-checkpoint."""

        def _commit():
            repo = self._repo(repo_path)
            if not allow_empty and not repo.is_dirty(untracked_files=True):
                logger.debug("No changes to checkpoint for %s", task_id[:8])
                return None

            msg = (
                f"atlas-checkpoint: task={task_id} "
                f"strategy={strategy} attempt={attempt}"
            )
            repo.git.add("--all")
            try:
                commit = repo.index.commit(msg)
            except Exception as exc:
                if "nothing to commit" in str(exc).lower():
                    return None
                raise
            logger.info("Checkpoint %s on %s", commit.hexsha[:8], task_id[:8])
            return CheckpointRecord(
                commit_hash=commit.hexsha,
                timestamp=datetime.now(timezone.utc).isoformat(),
                strategy=strategy,
                attempt=attempt,
                task_id=task_id,
                message=msg,
            )

        return await self._run(_commit)

    async def rollback_to_checkpoint(
        self,
        repo_path: Path,
        commit_hash: str,
        *,
        hard: bool = True,
    ) -> bool:
        """Restore workspace to checkpoint commit."""

        def _rollback():
            repo = self._repo(repo_path)
            if hard:
                repo.git.reset("--hard", commit_hash)
            else:
                repo.git.checkout(commit_hash)
            logger.info("Rolled back to checkpoint %s", commit_hash[:8])
            return True

        return await self._run(_rollback)

    async def rollback_to_branch(
        self,
        repo_path: Path,
        branch: str,
    ) -> bool:
        def _checkout():
            repo = self._repo(repo_path)
            repo.heads[branch].checkout()
            logger.info("Restored branch %s", branch)
            return True

        return await self._run(_checkout)

    async def revert_commit(self, repo_path: Path, commit_hash: str) -> bool:
        def _revert():
            repo = self._repo(repo_path)
            repo.git.revert(commit_hash, no_commit=False)
            logger.info("Reverted commit %s", commit_hash[:8])
            return True

        try:
            return await self._run(_revert)
        except Exception:
            logger.exception("Revert failed for %s", commit_hash[:8])
            return False

    async def diff_stat(self, repo_path: Path, ref_a: str = "HEAD~1", ref_b: str = "HEAD") -> str:
        def _diff():
            repo = self._repo(repo_path)
            return repo.git.diff("--stat", ref_a, ref_b)

        try:
            return await self._run(_diff)
        except Exception:
            return ""

    def attach_checkpoint_to_task(self, task, record: CheckpointRecord) -> None:
        git_meta = task.metadata.setdefault("git", {})
        cps = git_meta.setdefault("checkpoints", [])
        cps.append(record.to_dict())

    def record_rollback(self, task, *, to_hash: str, reason: str, method: str) -> None:
        git_meta = task.metadata.setdefault("git", {})
        history = git_meta.setdefault("rollback_history", [])
        history.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "to_hash": to_hash,
                "reason": reason,
                "method": method,
            }
        )
