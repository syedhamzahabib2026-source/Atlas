"""Tier 1.3 / 2.4 — git evidence gates against real temp repos."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("git")

from core.config import GitSafetyConfig
from core.git_manager import GitManager
from core.pr_manager import PRPreparationManager


def make_git_manager() -> GitManager:
    return GitManager(GitSafetyConfig())


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@atlas.local")
    _git(path, "config", "user.name", "Atlas Test")
    (path / "README.md").write_text("hello\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "initial")
    return path


class TestCountCommitsAhead:
    def test_zero_on_fresh_branch(self, repo: Path):
        _git(repo, "checkout", "-b", "atlas/task-x")
        gm = make_git_manager()
        assert asyncio.run(gm.count_commits_ahead(repo, "main")) == 0

    def test_counts_new_commits(self, repo: Path):
        _git(repo, "checkout", "-b", "atlas/task-x")
        (repo / "feature.py").write_text("x = 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "feature")
        gm = make_git_manager()
        assert asyncio.run(gm.count_commits_ahead(repo, "main")) == 1

    def test_unknown_base_returns_none(self, repo: Path):
        gm = make_git_manager()
        assert asyncio.run(gm.count_commits_ahead(repo, "does-not-exist")) is None


class TestCommitAll:
    def test_commits_dirty_tree(self, repo: Path):
        (repo / "wip.txt").write_text("uncommitted\n")
        gm = make_git_manager()
        sha = asyncio.run(gm.commit_all(repo, "atlas: rescue"))
        assert sha
        assert not asyncio.run(gm.is_dirty(repo))

    def test_clean_tree_returns_none(self, repo: Path):
        gm = make_git_manager()
        assert asyncio.run(gm.commit_all(repo, "atlas: rescue")) is None


class TestHasCommitsAhead:
    def test_local_base_fallback(self, repo: Path):
        # No origin remote — must fall back to the local base ref
        prep = PRPreparationManager(github=None)
        _git(repo, "checkout", "-b", "atlas/task-x")
        assert not asyncio.run(prep.has_commits_ahead(str(repo), base="main"))

        (repo / "f.txt").write_text("new\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "change")
        assert asyncio.run(prep.has_commits_ahead(str(repo), base="main"))

    def test_git_error_fails_open_to_pr(self, tmp_path: Path):
        # Not a git repo — surface the problem via PR attempt, not silence
        prep = PRPreparationManager(github=None)
        assert asyncio.run(prep.has_commits_ahead(str(tmp_path), base="main"))
