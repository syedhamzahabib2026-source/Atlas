"""
Build concise operational context before task execution (Phase 7).
"""

from __future__ import annotations

from core.logger import get_logger
from memory.memory_models import OperationalContext
from memory.memory_queries import MemoryQueries
from memory.operational_memory import OperationalMemoryStore

logger = get_logger("memory.context")


class ContextBuilder:
    """Prepare task context from operational memory."""

    def __init__(self, store: OperationalMemoryStore) -> None:
        self.queries = MemoryQueries(store)

    async def build_for_task(self, task, project_name: str | None = None) -> OperationalContext:
        name = project_name or task.project_id or "default"
        ctx = await self.queries.build_context(task, name)
        logger.info(
            "Built operational context for %s: %d history, %d cautions",
            task.id[:8],
            len(ctx.relevant_history),
            len(ctx.cautions),
        )
        return ctx

    def apply_to_task(self, task, ctx: OperationalContext) -> None:
        """Inject context into task metadata for agents (not buried in agents)."""
        task.metadata["operational_context"] = ctx.to_prompt_block()
        task.metadata["operational_context_struct"] = {
            "project_id": ctx.project_id,
            "high_risk": ctx.high_risk_areas,
            "cautions": ctx.cautions,
            "strategies": ctx.suggested_strategies,
        }
