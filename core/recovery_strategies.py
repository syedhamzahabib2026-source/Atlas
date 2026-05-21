"""
Recovery strategy catalog — diversified responses, not repeated patches.
"""

from __future__ import annotations

from enum import Enum

from core.failure_classifier import FailureCategory


class RecoveryStrategy(str, Enum):
    MINIMAL_PATCH = "minimal_patch"
    FRONTEND_TRACE = "frontend_trace"
    BACKEND_TRACE = "backend_trace"
    ARCHITECTURAL_FIX = "architectural_fix"
    DEPENDENCY_REPAIR = "dependency_repair"
    STATE_RESET = "state_reset"
    REBUILD_COMPONENT = "rebuild_component"
    ROLLBACK_AND_REIMPLEMENT = "rollback_and_reimplement"
    ISOLATE_FAILURE = "isolate_failure"
    INSPECT_LOGS_ONLY = "inspect_logs_only"
    ROLLBACK_CHECKPOINT = "rollback_checkpoint"


STRATEGY_BY_CATEGORY: dict[FailureCategory, list[RecoveryStrategy]] = {
    FailureCategory.FRONTEND: [
        RecoveryStrategy.FRONTEND_TRACE,
        RecoveryStrategy.ISOLATE_FAILURE,
        RecoveryStrategy.MINIMAL_PATCH,
        RecoveryStrategy.ARCHITECTURAL_FIX,
        RecoveryStrategy.REBUILD_COMPONENT,
    ],
    FailureCategory.BACKEND: [
        RecoveryStrategy.BACKEND_TRACE,
        RecoveryStrategy.INSPECT_LOGS_ONLY,
        RecoveryStrategy.DEPENDENCY_REPAIR,
        RecoveryStrategy.ARCHITECTURAL_FIX,
    ],
    FailureCategory.AUTH: [
        RecoveryStrategy.BACKEND_TRACE,
        RecoveryStrategy.INSPECT_LOGS_ONLY,
        RecoveryStrategy.STATE_RESET,
        RecoveryStrategy.ISOLATE_FAILURE,
    ],
    FailureCategory.API: [
        RecoveryStrategy.BACKEND_TRACE,
        RecoveryStrategy.INSPECT_LOGS_ONLY,
        RecoveryStrategy.ISOLATE_FAILURE,
    ],
    FailureCategory.NETWORK: [
        RecoveryStrategy.INSPECT_LOGS_ONLY,
        RecoveryStrategy.BACKEND_TRACE,
        RecoveryStrategy.DEPENDENCY_REPAIR,
    ],
    FailureCategory.DEPENDENCY: [
        RecoveryStrategy.ROLLBACK_CHECKPOINT,
        RecoveryStrategy.DEPENDENCY_REPAIR,
        RecoveryStrategy.INSPECT_LOGS_ONLY,
        RecoveryStrategy.ROLLBACK_AND_REIMPLEMENT,
    ],
    FailureCategory.BUILD: [
        RecoveryStrategy.DEPENDENCY_REPAIR,
        RecoveryStrategy.INSPECT_LOGS_ONLY,
        RecoveryStrategy.ROLLBACK_AND_REIMPLEMENT,
    ],
    FailureCategory.STATE: [
        RecoveryStrategy.STATE_RESET,
        RecoveryStrategy.ISOLATE_FAILURE,
        RecoveryStrategy.ARCHITECTURAL_FIX,
    ],
    FailureCategory.RACE_CONDITION: [
        RecoveryStrategy.ISOLATE_FAILURE,
        RecoveryStrategy.INSPECT_LOGS_ONLY,
        RecoveryStrategy.ARCHITECTURAL_FIX,
    ],
    FailureCategory.ENVIRONMENT: [
        RecoveryStrategy.INSPECT_LOGS_ONLY,
        RecoveryStrategy.DEPENDENCY_REPAIR,
        RecoveryStrategy.STATE_RESET,
    ],
    FailureCategory.UNKNOWN: [
        RecoveryStrategy.INSPECT_LOGS_ONLY,
        RecoveryStrategy.ISOLATE_FAILURE,
        RecoveryStrategy.BACKEND_TRACE,
        RecoveryStrategy.FRONTEND_TRACE,
        RecoveryStrategy.ARCHITECTURAL_FIX,
    ],
}


STRATEGY_GUIDANCE: dict[RecoveryStrategy, str] = {
    RecoveryStrategy.MINIMAL_PATCH: "Apply the smallest change that could fix the symptom — only if root cause is localized.",
    RecoveryStrategy.FRONTEND_TRACE: "Trace UI event flow, rendering, and selectors. Do not stack CSS patches without understanding why clicks fail.",
    RecoveryStrategy.BACKEND_TRACE: "Trace server routes, auth middleware, and data flow. Prefer logs and request paths over UI tweaks.",
    RecoveryStrategy.ARCHITECTURAL_FIX: "Reconsider component boundaries or service design — previous local fixes did not hold.",
    RecoveryStrategy.DEPENDENCY_REPAIR: "Fix package versions, lockfiles, or install errors before changing application logic.",
    RecoveryStrategy.STATE_RESET: "Inspect stale client/server state, caches, sessions. Reset or resync state deliberately.",
    RecoveryStrategy.REBUILD_COMPONENT: "Rewrite the failing component cleanly instead of patching accumulated layers.",
    RecoveryStrategy.ROLLBACK_AND_REIMPLEMENT: "Revert recent changes in the affected area and reimplement with a clearer approach.",
    RecoveryStrategy.ISOLATE_FAILURE: "Create a minimal reproduction or test that isolates the failure without fixing unrelated code.",
    RecoveryStrategy.INSPECT_LOGS_ONLY: "Do not patch yet. Gather logs, network traces, and console output; form a root-cause hypothesis first.",
    RecoveryStrategy.ROLLBACK_CHECKPOINT: "Restore last atlas-checkpoint, then apply a different fix from the stable tree. Do not stack patches on a worsening branch.",
}
