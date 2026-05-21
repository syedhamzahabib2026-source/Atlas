"""
Failure classification for recovery strategy selection.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.task_manager import Task
    from core.task_result import TaskResult


class FailureCategory(str, Enum):
    FRONTEND = "frontend"
    BACKEND = "backend"
    AUTH = "auth"
    API = "api"
    STATE = "state"
    DEPENDENCY = "dependency"
    ENVIRONMENT = "environment"
    RACE_CONDITION = "race_condition"
    BUILD = "build"
    NETWORK = "network"
    UNKNOWN = "unknown"


def classify_failure(task: Task, result: TaskResult) -> FailureCategory:
    """Heuristic classification from errors, output, and browser metadata."""
    text = " ".join(
        [
            result.summary or "",
            " ".join(result.errors),
            result.raw_output or "",
            str(task.error or ""),
        ]
    ).lower()

    browser = (result.metadata or {}).get("browser", {})
    if browser:
        if browser.get("network_failures"):
            return FailureCategory.NETWORK
        console = " ".join(browser.get("console_logs", [])).lower()
        if "auth" in console or "token" in console or "login" in console:
            return FailureCategory.AUTH
        if browser.get("errors"):
            return FailureCategory.FRONTEND

    if task.metadata.get("agent") == "browser":
        if "network" in text or "timeout" in text:
            return FailureCategory.NETWORK
        return FailureCategory.FRONTEND

    rules: list[tuple[tuple[str, ...], FailureCategory]] = [
        (("api key", "unauthorized", "401", "403", "auth", "token", "login"), FailureCategory.AUTH),
        (("npm err", "pip install", "dependency", "module not found", "cannot find module"), FailureCategory.DEPENDENCY),
        (("build failed", "compilation error", "typescript error", "syntaxerror"), FailureCategory.BUILD),
        (("race", "deadlock", "timing", "flaky"), FailureCategory.RACE_CONDITION),
        (("connection refused", "econnrefused", "network", "dns", "fetch failed"), FailureCategory.NETWORK),
        (("500", "502", "503", "backend", "server error", "database"), FailureCategory.BACKEND),
        (("api", "endpoint", "rest", "graphql"), FailureCategory.API),
        (("state", "hydration", "redux", "stale"), FailureCategory.STATE),
        (("env", "environment variable", ".env"), FailureCategory.ENVIRONMENT),
        (("selector", "element not visible", "click", "ui", "css", "dom", "frontend"), FailureCategory.FRONTEND),
    ]

    for markers, category in rules:
        if any(m in text for m in markers):
            return category

    return FailureCategory.UNKNOWN


def error_signature(result: TaskResult) -> str:
    """Stable fingerprint for contradiction detection."""
    parts = [
        result.summary[:120] if result.summary else "",
        "|".join(sorted(result.errors[:5])),
    ]
    browser = (result.metadata or {}).get("browser", {})
    if browser:
        parts.append(browser.get("summary", "")[:80])
        parts.extend(browser.get("network_failures", [])[:2])
    sig = " :: ".join(p for p in parts if p)
    return sig[:300]
