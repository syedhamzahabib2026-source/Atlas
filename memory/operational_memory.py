"""
Operational memory store — structured engineering intelligence (Phase 7).

SQLite-backed symbolic memory. No raw chat logs.

TODO: semantic embeddings / vector retrieval
TODO: architecture graph memory
TODO: AI-generated repo maps
TODO: cross-project learning
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.logger import get_logger
from memory.memory_models import (
    FailurePattern,
    MemoryCategory,
    OperationalMemoryRecord,
    RiskZone,
    StrategyStats,
    TaskMemory,
    new_id,
)

logger = get_logger("memory.operational")

SCHEMA = """
CREATE TABLE IF NOT EXISTS operational_memories (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    category TEXT NOT NULL,
    subcategory TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    tags TEXT DEFAULT '[]',
    signal_score INTEGER DEFAULT 50,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT,
    access_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_op_mem_project ON operational_memories(project_id);
CREATE INDEX IF NOT EXISTS idx_op_mem_category ON operational_memories(category, subcategory);
CREATE INDEX IF NOT EXISTS idx_op_mem_tags ON operational_memories(project_id, category);

CREATE TABLE IF NOT EXISTS strategy_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT,
    strategy TEXT NOT NULL,
    category TEXT NOT NULL,
    attempts INTEGER DEFAULT 0,
    successes INTEGER DEFAULT 0,
    failures INTEGER DEFAULT 0,
    rollbacks INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, strategy, category)
);

CREATE TABLE IF NOT EXISTS risk_zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    path TEXT NOT NULL,
    zone_type TEXT NOT NULL,
    risk_score REAL DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    rollback_count INTEGER DEFAULT 0,
    regression_count INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, path, zone_type)
);

CREATE TABLE IF NOT EXISTS failure_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    signature TEXT NOT NULL,
    category TEXT NOT NULL,
    occurrence_count INTEGER DEFAULT 1,
    last_seen TEXT NOT NULL,
    UNIQUE(project_id, signature)
);

CREATE TABLE IF NOT EXISTS task_outcomes (
    task_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL,
    strategy TEXT,
    failure_category TEXT,
    verification_status TEXT,
    health_score REAL,
    regression INTEGER DEFAULT 0,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_outcomes_project ON task_outcomes(project_id);
"""


class OperationalMemoryStore:
    """Persistent structured operational memory."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    async def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        logger.info("Operational memory schema ready at %s", self.db_path)

    def _conn_req(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("OperationalMemoryStore not initialized")
        return self._conn

    async def upsert_memory(self, record: OperationalMemoryRecord) -> None:
        conn = self._conn_req()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO operational_memories
            (id, project_id, category, subcategory, title, summary, tags,
             signal_score, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                summary=excluded.summary,
                signal_score=excluded.signal_score,
                metadata=excluded.metadata,
                updated_at=excluded.updated_at
            """,
            (
                record.id,
                record.project_id,
                record.category,
                record.subcategory,
                record.title,
                record.summary,
                json.dumps(record.tags),
                record.signal_score,
                json.dumps(record.metadata),
                record.created_at.isoformat(),
                now,
            ),
        )
        conn.commit()

    async def list_memories(
        self,
        project_id: str,
        *,
        category: str | None = None,
        subcategory: str | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        conn = self._conn_req()
        q = "SELECT * FROM operational_memories WHERE project_id = ?"
        params: list[Any] = [project_id]
        if category:
            q += " AND category = ?"
            params.append(category)
        if subcategory:
            q += " AND subcategory = ?"
            params.append(subcategory)
        q += " ORDER BY signal_score DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        cur = conn.execute(q, params)
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            d["tags"] = json.loads(d.get("tags") or "[]")
            d["metadata"] = json.loads(d.get("metadata") or "{}")
            rows.append(d)
        return rows

    async def record_task_outcome(self, outcome: TaskMemory) -> None:
        conn = self._conn_req()
        conn.execute(
            """
            INSERT OR REPLACE INTO task_outcomes
            (task_id, project_id, status, strategy, failure_category,
             verification_status, health_score, regression, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outcome.task_id,
                outcome.project_id,
                outcome.status,
                outcome.strategy,
                outcome.failure_category,
                outcome.verification_status,
                outcome.health_score,
                1 if outcome.regression else 0,
                outcome.summary[:1000],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

    async def update_strategy_stats(
        self,
        project_id: str | None,
        strategy: str,
        category: str,
        *,
        success: bool = False,
        failure: bool = False,
        rollback: bool = False,
    ) -> None:
        conn = self._conn_req()
        now = datetime.now(timezone.utc).isoformat()
        pid = project_id or "__global__"
        conn.execute(
            """
            INSERT INTO strategy_stats
            (project_id, strategy, category, attempts, successes, failures, rollbacks, updated_at)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(project_id, strategy, category) DO UPDATE SET
                attempts = attempts + 1,
                successes = successes + excluded.successes,
                failures = failures + excluded.failures,
                rollbacks = rollbacks + excluded.rollbacks,
                updated_at = excluded.updated_at
            """,
            (
                pid,
                strategy,
                category,
                1 if success else 0,
                1 if failure else 0,
                1 if rollback else 0,
                now,
            ),
        )
        conn.commit()

    async def get_strategy_stats(
        self,
        project_id: str | None = None,
        category: str | None = None,
        min_attempts: int = 2,
    ) -> list[StrategyStats]:
        conn = self._conn_req()
        pid = project_id or "__global__"
        q = "SELECT * FROM strategy_stats WHERE project_id = ? AND attempts >= ?"
        params: list[Any] = [pid, min_attempts]
        if category:
            q += " AND category = ?"
            params.append(category)
        q += " ORDER BY CAST(successes AS REAL) / attempts DESC"
        cur = conn.execute(q, params)
        return [
            StrategyStats(
                strategy=row["strategy"],
                category=row["category"],
                project_id=row["project_id"] if row["project_id"] != "__global__" else None,
                attempts=row["attempts"],
                successes=row["successes"],
                failures=row["failures"],
                rollbacks=row["rollbacks"],
            )
            for row in cur.fetchall()
        ]

    async def upsert_global_lesson(
        self,
        content: str,
        category: str,
        signal_score: float = 0.8,
        tags: list[str] | None = None,
    ) -> None:
        """Write a cross-project lesson into operational_memories under __global__."""
        import hashlib
        lesson_id = "global_" + hashlib.sha256(content.encode()).hexdigest()[:12]
        await self.upsert_memory(
            OperationalMemoryRecord(
                id=lesson_id,
                project_id="__global__",
                category=category,
                subcategory="lesson",
                title=content[:80],
                summary=content,
                tags=tags or [],
                signal_score=int(signal_score * 100),
            )
        )

    async def get_global_lessons(
        self,
        topics: list[str] | None = None,
        limit: int = 5,
    ) -> list[str]:
        """Fetch cross-project lessons, optionally filtered by tag overlap."""
        rows = await self.list_memories("__global__", limit=50)
        if topics:
            def overlaps(row: dict) -> bool:
                row_tags = row.get("tags") or []
                return any(t in row_tags for t in topics)
            filtered = [r for r in rows if overlaps(r)]
            rows = filtered or rows
        rows.sort(key=lambda r: r.get("signal_score", 0), reverse=True)
        return [r["summary"] for r in rows[:limit]]

    async def bump_risk_zone(
        self,
        project_id: str,
        path: str,
        zone_type: str,
        *,
        failure: bool = False,
        rollback: bool = False,
        regression: bool = False,
    ) -> RiskZone:
        conn = self._conn_req()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO risk_zones
            (project_id, path, zone_type, risk_score, failure_count,
             rollback_count, regression_count, updated_at)
            VALUES (?, ?, ?, 10, ?, ?, ?, ?)
            ON CONFLICT(project_id, path, zone_type) DO UPDATE SET
                failure_count = failure_count + excluded.failure_count,
                rollback_count = rollback_count + excluded.rollback_count,
                regression_count = regression_count + excluded.regression_count,
                updated_at = excluded.updated_at
            """,
            (
                project_id,
                path,
                zone_type,
                1 if failure else 0,
                1 if rollback else 0,
                1 if regression else 0,
                now,
            ),
        )
        conn.commit()
        cur = conn.execute(
            "SELECT * FROM risk_zones WHERE project_id=? AND path=? AND zone_type=?",
            (project_id, path, zone_type),
        )
        row = dict(cur.fetchone())
        score = min(
            100.0,
            row["failure_count"] * 12
            + row["rollback_count"] * 18
            + row["regression_count"] * 15,
        )
        conn.execute(
            "UPDATE risk_zones SET risk_score=? WHERE project_id=? AND path=? AND zone_type=?",
            (score, project_id, path, zone_type),
        )
        conn.commit()
        row["risk_score"] = score
        return RiskZone(
            project_id=project_id,
            path=path,
            zone_type=zone_type,
            risk_score=score,
            failure_count=row["failure_count"],
            rollback_count=row["rollback_count"],
            regression_count=row["regression_count"],
        )

    async def get_high_risk_zones(
        self, project_id: str, min_score: float = 40.0, limit: int = 15
    ) -> list[RiskZone]:
        conn = self._conn_req()
        cur = conn.execute(
            """
            SELECT * FROM risk_zones
            WHERE project_id = ? AND risk_score >= ?
            ORDER BY risk_score DESC LIMIT ?
            """,
            (project_id, min_score, limit),
        )
        return [
            RiskZone(
                project_id=r["project_id"],
                path=r["path"],
                zone_type=r["zone_type"],
                risk_score=r["risk_score"],
                failure_count=r["failure_count"],
                rollback_count=r["rollback_count"],
                regression_count=r["regression_count"],
            )
            for r in cur.fetchall()
        ]

    async def bump_failure_pattern(
        self, project_id: str, signature: str, category: str
    ) -> FailurePattern:
        conn = self._conn_req()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO failure_patterns (project_id, signature, category, occurrence_count, last_seen)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(project_id, signature) DO UPDATE SET
                occurrence_count = occurrence_count + 1,
                last_seen = excluded.last_seen,
                category = excluded.category
            """,
            (project_id, signature[:300], category, now),
        )
        conn.commit()
        cur = conn.execute(
            "SELECT * FROM failure_patterns WHERE project_id=? AND signature=?",
            (project_id, signature[:300]),
        )
        row = dict(cur.fetchone())
        return FailurePattern(
            project_id=project_id,
            signature=row["signature"],
            category=row["category"],
            occurrence_count=row["occurrence_count"],
        )

    async def get_recurring_patterns(
        self, project_id: str, min_count: int = 2, limit: int = 10
    ) -> list[FailurePattern]:
        conn = self._conn_req()
        cur = conn.execute(
            """
            SELECT * FROM failure_patterns
            WHERE project_id = ? AND occurrence_count >= ?
            ORDER BY occurrence_count DESC LIMIT ?
            """,
            (project_id, min_count, limit),
        )
        return [
            FailurePattern(
                project_id=r["project_id"],
                signature=r["signature"],
                category=r["category"],
                occurrence_count=r["occurrence_count"],
            )
            for r in cur.fetchall()
        ]

    async def prune_stale(
        self,
        *,
        max_age_days: int = 90,
        min_signal: int = 20,
        max_low_signal: int = 200,
    ) -> int:
        """Remove stale low-signal memories; consolidate duplicates."""
        conn = self._conn_req()
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
        # SQLite compare ISO strings works for UTC isoformat
        cur = conn.execute(
            """
            DELETE FROM operational_memories
            WHERE signal_score < ? AND created_at < datetime('now', ?)
            """,
            (min_signal, f"-{max_age_days} days"),
        )
        deleted = cur.rowcount
        conn.commit()
        logger.info("Pruned %d stale operational memories", deleted)
        return deleted

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
