"""
Centralized auth / subscription exhaustion detection (Phase 13).

AuthMonitor owns auth state only — not routing or execution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from core.logger import get_logger

if TYPE_CHECKING:
    from core.task_manager import Task
    from core.task_result import TaskResult
    from core.worker_pool import WorkerPool
    from core.worker_registry import WorkerRegistry

logger = get_logger("auth_monitor")


class AuthFailureKind(str, Enum):
    NONE = "none"
    SUBSCRIPTION_EXHAUSTED = "subscription_exhausted"
    RATE_LIMIT = "rate_limit"
    INVALID_API_KEY = "invalid_api_key"
    MISSING_CREDENTIALS = "missing_credentials"
    AUTH_GENERIC = "auth_generic"


# Centralized markers — single source for exhaustion heuristics
_EXHAUSTION_MARKERS = (
    "rate limit",
    "rate_limit",
    "usage limit",
    "quota exceeded",
    "out of credits",
    "billing",
    "subscription",
    "plan limit",
    "too many requests",
    "429",
    "overloaded",
)

_SUBSCRIPTION_MARKERS = (
    "subscription",
    "login required",
    "not authenticated",
    "sign in",
)

_API_KEY_MARKERS = (
    "api key",
    "api_key",
    "invalid api key",
    "authentication error",
    "401",
    "403",
    "unauthorized",
)


@dataclass
class AuthClassification:
    kind: AuthFailureKind
    confidence: float
    summary: str
    should_cooldown_subscription: bool = False


class AuthMonitor:
    """
    Detect likely subscription exhaustion and classify auth failures.

    Marks subscription pool temporarily unavailable; router uses API pool.
    """

    def __init__(
        self,
        registry: WorkerRegistry,
        *,
        exhaustion_threshold: int = 2,
    ) -> None:
        self.registry = registry
        self.exhaustion_threshold = exhaustion_threshold
        self._subscription_failure_streak = 0
        self._last_classification: AuthClassification | None = None

    def classify(self, result: TaskResult, task: Task | None = None) -> AuthClassification:
        text = " ".join(
            [
                (result.summary or "").lower(),
                " ".join(result.errors).lower(),
                (result.raw_output or "")[-4000:].lower(),
            ]
        )

        for marker in _EXHAUSTION_MARKERS:
            if marker in text:
                return AuthClassification(
                    kind=AuthFailureKind.SUBSCRIPTION_EXHAUSTED,
                    confidence=0.85,
                    summary=f"Likely subscription/quota exhaustion ({marker})",
                    should_cooldown_subscription=True,
                )

        for marker in _API_KEY_MARKERS:
            if marker in text:
                pool = (task.metadata.get("worker_pool") if task else None) or ""
                if pool == "api":
                    return AuthClassification(
                        kind=AuthFailureKind.INVALID_API_KEY,
                        confidence=0.8,
                        summary=f"API key auth issue ({marker})",
                    )
                return AuthClassification(
                    kind=AuthFailureKind.MISSING_CREDENTIALS,
                    confidence=0.7,
                    summary=f"Credentials issue ({marker})",
                )

        for marker in _SUBSCRIPTION_MARKERS:
            if marker in text:
                return AuthClassification(
                    kind=AuthFailureKind.AUTH_GENERIC,
                    confidence=0.6,
                    summary=f"Auth/subscription signal ({marker})",
                    should_cooldown_subscription=marker in ("login required", "not authenticated"),
                )

        if "rate limit" in text or "429" in text:
            return AuthClassification(
                kind=AuthFailureKind.RATE_LIMIT,
                confidence=0.75,
                summary="Rate limit detected",
                should_cooldown_subscription=True,
            )

        return AuthClassification(
            kind=AuthFailureKind.NONE,
            confidence=0.0,
            summary="",
        )

    def report_failure(
        self,
        pool: WorkerPool,
        result: TaskResult,
        task: Task | None = None,
    ) -> AuthClassification:
        """Record failure and apply pool cooldown if warranted."""
        classification = self.classify(result, task)
        self._last_classification = classification

        if pool.pool_id == "subscription":
            if classification.kind != AuthFailureKind.NONE:
                self._subscription_failure_streak += 1
            else:
                self._subscription_failure_streak = max(
                    0, self._subscription_failure_streak - 1
                )

            if (
                classification.should_cooldown_subscription
                or self._subscription_failure_streak >= self.exhaustion_threshold
            ):
                pool.mark_cooldown(
                    classification.summary or "subscription exhaustion suspected",
                )
                return classification

        if pool.pool_id == "api" and classification.kind == AuthFailureKind.INVALID_API_KEY:
            pool.mark_cooldown("API key authentication failure")

        return classification

    def report_success(self, pool: WorkerPool) -> None:
        if pool.pool_id == "subscription":
            self._subscription_failure_streak = 0
            if pool.is_on_cooldown:
                pool.clear_cooldown()

    def tick(self) -> list[str]:
        """Check cooldown expiry; return pool ids that became available."""
        restored: list[str] = []
        for pool in self.registry.all_pools():
            if pool.tick_cooldown():
                restored.append(pool.pool_id)
        return restored

    @property
    def subscription_on_cooldown(self) -> bool:
        sub = self.registry.get("subscription")
        return bool(sub and sub.is_on_cooldown)
