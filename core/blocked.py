"""
Blocked-state detection — when Atlas must ask a human before continuing.

TODO: Playwright failures routing to blocked
TODO: memory persistence of block history
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.task_result import ResultStatus, TaskResult

if TYPE_CHECKING:
    from core.task_manager import Task

# Output snippets that imply human input is required
_CREDENTIAL_MARKERS = (
    "api key",
    "api_key",
    "missing credentials",
    "authentication required",
    "not authenticated",
    "invalid token",
    "login required",
)

_UNCLEAR_MARKERS = (
    "unclear",
    "ambiguous",
    "need more information",
    "please clarify",
    "which option",
)

_BLOCK_REASONS = {
    "missing_credentials": "Missing API key or credentials",
    "repeated_failures": "Task failed multiple times",
    "unclear_requirements": "Requirements need clarification",
    "timeout": "Execution timed out",
}


def detect_block(
    result: TaskResult,
    task: Task,
    *,
    max_failures_before_block: int = 3,
) -> tuple[str, str] | None:
    """
    Return (reason_code, question_for_human) if task should enter BLOCKED.

    Otherwise return None.
    """
    if task.metadata.get("force_block"):
        reason = task.metadata.get("block_reason", "unclear_requirements")
        question = task.metadata.get(
            "block_question",
            "Atlas needs your input to continue. Reply in this thread.",
        )
        return reason, question

    output = (result.raw_output or "").lower()
    errors = " ".join(result.errors).lower()

    for marker in _CREDENTIAL_MARKERS:
        if marker in output or marker in errors:
            return (
                "missing_credentials",
                "Atlas hit a credentials/auth error. "
                "Reply with where credentials live or paste the required key name.",
            )

    fail_count = int(task.metadata.get("failure_count", 0))
    if result.status in (ResultStatus.FAILED, ResultStatus.TIMEOUT):
        fail_count += 1
    if fail_count >= max_failures_before_block:
        return (
            "repeated_failures",
            f"Task `{task.id[:8]}` failed {fail_count} times. "
            "What should Atlas do next? (retry / change approach / stop)",
        )

    if result.status == ResultStatus.TIMEOUT:
        return (
            "timeout",
            f"Task `{task.id[:8]}` timed out. "
            "Reply with: continue with more time / narrower scope / stop",
        )

    for marker in _UNCLEAR_MARKERS:
        if marker in output:
            return (
                "unclear_requirements",
                "Claude output suggests unclear requirements. "
                "Reply with the specific goal or constraints.",
            )

    if task.metadata.get("unclear_requirements"):
        return (
            "unclear_requirements",
            "Please clarify what Atlas should build or change.",
        )

    return None


def block_reason_label(code: str) -> str:
    return _BLOCK_REASONS.get(code, code)
