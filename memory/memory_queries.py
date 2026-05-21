"""
Structured retrieval for operational memory.
"""

from __future__ import annotations

from typing import Any

from memory.memory_models import OperationalContext, StrategyStats
from memory.operational_memory import OperationalMemoryStore

# Keyword → failure category hints for retrieval
_TOPIC_KEYWORDS: dict[str, list[str]] = {
    "auth": ["auth", "login", "token", "session", "credential"],
    "frontend": ["ui", "button", "css", "dom", "click", "render", "notification", "badge"],
    "backend": ["api", "server", "route", "middleware", "database", "500"],
    "dependency": ["npm", "package", "dependency", "build"],
    "network": ["network", "fetch", "cors", "timeout"],
}


class MemoryQueries:
    """Query operational memory for task-relevant intelligence."""

    def __init__(self, store: OperationalMemoryStore) -> None:
        self.store = store

    def infer_topics(self, task) -> list[str]:
        text = " ".join(
            [
                task.title,
                task.description,
                str(task.metadata.get("prompt", "")),
                str(task.metadata.get("failure_category", "")),
            ]
        ).lower()
        topics = []
        for topic, keys in _TOPIC_KEYWORDS.items():
            if any(k in text for k in keys):
                topics.append(topic)
        return topics or ["general"]

    async def get_similar_failures(
        self, project_id: str, topics: list[str], limit: int = 5
    ) -> list[str]:
        patterns = await self.store.get_recurring_patterns(project_id, min_count=2)
        lines = []
        for p in patterns:
            if any(t in p.category or t in p.signature.lower() for t in topics):
                lines.append(
                    f"Recurring {p.category} failure ({p.occurrence_count}x): {p.signature[:100]}"
                )
        memories = await self.store.list_memories(
            project_id, category="recovery", subcategory="failed_strategy", limit=limit
        )
        for m in memories:
            if any(t in " ".join(m.get("tags", [])).lower() for t in topics):
                lines.append(m["summary"][:150])
        return lines[:limit]

    async def get_successful_strategies(
        self, project_id: str, topics: list[str], limit: int = 4
    ) -> list[str]:
        stats = await self.store.get_strategy_stats(project_id)
        lines = []
        for s in stats:
            if s.success_rate >= 0.5 and s.attempts >= 2:
                if any(t in s.category for t in topics) or "general" in topics:
                    lines.append(
                        f"{s.strategy}: {s.success_rate:.0%} success "
                        f"({s.successes}/{s.attempts}) for {s.category}"
                    )
        memories = await self.store.list_memories(
            project_id, category="recovery", subcategory="success", limit=limit
        )
        for m in memories:
            lines.append(m["summary"][:120])
        return lines[:limit]

    async def get_fragile_areas(self, project_id: str, limit: int = 6) -> list[str]:
        zones = await self.store.get_high_risk_zones(project_id, min_score=35)
        lines = []
        for z in zones:
            level = z.risk_level.value.upper()
            lines.append(
                f"{z.path} ({z.zone_type}) — {level} risk "
                f"(failures={z.failure_count}, rollbacks={z.rollback_count})"
            )
        repo_mem = await self.store.list_memories(
            project_id, category="repository", limit=limit
        )
        for m in repo_mem:
            lines.append(f"{m['title']}: {m['summary'][:100]}")
        return lines[:limit]

    async def get_project_notes(self, project_id: str) -> list[str]:
        notes = await self.store.list_memories(
            project_id, category="project", limit=10
        )
        return [f"{n['title']}: {n['summary'][:120]}" for n in notes]

    async def get_cautions(
        self, project_id: str, topics: list[str], stats: list[StrategyStats]
    ) -> list[str]:
        cautions = []
        for s in stats:
            if s.attempts >= 3 and s.success_rate < 0.35:
                cautions.append(
                    f"Avoid relying on `{s.strategy}` for {s.category} "
                    f"(low success {s.success_rate:.0%})"
                )
        zones = await self.store.get_high_risk_zones(project_id, min_score=50)
        for z in zones[:3]:
            cautions.append(f"High regression risk: {z.path}")
        if "auth" in topics:
            cautions.append("Auth changes often affect notification/UI flows — verify both")
        return cautions

    async def build_context(self, task, project_name: str) -> OperationalContext:
        project_id = task.project_id or "default"
        topics = self.infer_topics(task)

        ctx = OperationalContext(project_id=project_id, project_name=project_name)
        ctx.relevant_history = (
            await self.get_project_notes(project_id)
            + await self.get_similar_failures(project_id, topics)
        )
        ctx.suggested_strategies = await self.get_successful_strategies(
            project_id, topics
        )
        ctx.high_risk_areas = await self.get_fragile_areas(project_id)
        stats = await self.store.get_strategy_stats(project_id)
        ctx.cautions = await self.get_cautions(project_id, topics, stats)
        return ctx
