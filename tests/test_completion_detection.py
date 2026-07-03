"""Tier 1.4 — completion detection must be sentinel-driven, not word-matching."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agents.claude_code import (
    SENTINEL_COMPLETE,
    SENTINEL_FAILED,
    ClaudeCodeAgent,
    ClaudeCodeConfig,
)
from core.task_manager import Task
from sessions.tmux_manager import TmuxManager


@pytest.fixture
def agent(tmp_path: Path) -> ClaudeCodeAgent:
    return ClaudeCodeAgent(
        tmux=TmuxManager(session_prefix="atlas-test"),
        projects_dir=tmp_path,
        config=ClaudeCodeConfig(idle_stable_polls=3),
    )


def detect(agent: ClaudeCodeAgent, output: str, *, previous: str = "", stable: int = 0):
    return asyncio.run(
        agent.detect_completion(output, previous_output=previous, stable_count=stable)
    )


class TestSentinels:
    def test_complete_sentinel_wins(self, agent):
        out = "working...\nall changes committed\n" + SENTINEL_COMPLETE
        complete, reason = detect(agent, out)
        assert complete
        assert reason.startswith("sentinel_complete")

    def test_failed_sentinel_wins(self, agent):
        out = f"tried everything\n{SENTINEL_FAILED}: missing credentials"
        complete, reason = detect(agent, out)
        assert complete
        assert reason.startswith("sentinel_failed")

    def test_failed_sentinel_beats_complete(self, agent):
        out = f"{SENTINEL_COMPLETE}\n{SENTINEL_FAILED}: regression found"
        complete, reason = detect(agent, out)
        assert complete
        assert reason.startswith("sentinel_failed")

    def test_sentinel_outside_tail_ignored(self, agent):
        # Sentinel buried 100 lines back (e.g. prompt echo scrolled) is stale
        out = SENTINEL_COMPLETE + "\n" + "\n".join(f"line {i}" for i in range(100))
        complete, reason = detect(agent, out)
        assert not complete


class TestNoFalsePositives:
    def test_conversational_done_does_not_complete(self, agent):
        out = "I'll get this done shortly. First, let me check the tests."
        complete, _ = detect(agent, out)
        assert not complete

    def test_checkmark_does_not_complete(self, agent):
        out = "\u2713 Installed dependencies\nnow editing files..."
        complete, _ = detect(agent, out)
        assert not complete

    def test_midrun_error_does_not_fail(self, agent):
        # Claude printing an error it is about to fix must not kill the run
        out = "error: test_foo failed\nLet me fix that assertion..."
        complete, _ = detect(agent, out)
        assert not complete


class TestFatalMarkers:
    def test_command_not_found_fails_fast(self, agent):
        complete, reason = detect(agent, "bash: claude: command not found")
        assert complete
        assert reason.startswith("error_detected")

    def test_rate_limit_fails_fast(self, agent):
        complete, reason = detect(agent, "API Error: rate limit exceeded")
        assert complete
        assert reason.startswith("error_detected")


class TestIdleDetection:
    def test_idle_at_prompt_completes_when_stable(self, agent):
        out = "made the changes\n\u203a "
        complete, reason = detect(agent, out, previous=out, stable=3)
        assert complete
        assert reason == "idle_at_prompt"

    def test_idle_at_prompt_needs_stability(self, agent):
        out = "made the changes\n\u203a "
        complete, _ = detect(agent, out, previous="different", stable=0)
        assert not complete

    def test_idle_with_traceback_in_tail_fails(self, agent):
        out = "Traceback (most recent call last):\n  boom\n\u203a "
        complete, reason = detect(agent, out, previous=out, stable=3)
        assert complete
        assert reason.startswith("error_detected")

    def test_stable_output_completes(self, agent):
        out = "some output without a prompt"
        complete, reason = detect(agent, out, previous=out, stable=3)
        assert complete
        assert reason == "output_stable"


class TestSessionNaming:
    def test_sessions_unique_per_task(self, agent):
        a = Task(title="t1", project_id="assignmint")
        b = Task(title="t2", project_id="assignmint")
        assert agent._resolve_session_name(a) != agent._resolve_session_name(b)

    def test_retry_gets_fresh_session(self, agent):
        t = Task(title="t", project_id="assignmint")
        first = agent._resolve_session_name(t)
        t.metadata["recovery_attempt_count"] = 1
        retry = agent._resolve_session_name(t)
        assert first != retry

    def test_recorded_session_name_not_reused(self, agent):
        t = Task(title="t", project_id="assignmint")
        t.metadata["session_name"] = "atlas-stale-session"
        assert agent._resolve_session_name(t) != "atlas-stale-session"

    def test_prompt_contains_completion_protocol(self, agent):
        t = Task(title="t", description="add a button")
        prompt = agent._build_prompt(t)
        # Instructions must describe the sentinel without containing the
        # literal token (the echo would trigger detection instantly)
        assert SENTINEL_COMPLETE not in prompt
        assert SENTINEL_FAILED not in prompt
        assert "ATLAS_TASK_" in prompt
        assert "git add -A" in prompt
