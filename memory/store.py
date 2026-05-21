"""
SQLite-backed memory store (Phase 1 skeleton).

TODO: Migrations, full CRUD, vector search for RAG (later)
TODO: Sync task history from TaskManager
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from core.logger import get_logger
from memory.models import MemoryEntry, ProjectRecord

logger = get_logger("memory")


class MemoryStore:
    """Persistent project memory and plans."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        path TEXT NOT NULL,
        created_at TEXT NOT NULL,
        metadata TEXT DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS memory_entries (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        metadata TEXT DEFAULT '{}',
        FOREIGN KEY (project_id) REFERENCES projects(id)
    );
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    async def initialize(self) -> None:
        """Create DB file and schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()
        logger.info("Memory DB ready at %s", self.db_path)

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("MemoryStore not initialized — call initialize() first")
        return self._conn

    async def save_project(self, project: ProjectRecord) -> None:
        conn = self._require_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO projects (id, name, path, created_at, metadata)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                project.id,
                project.name,
                project.path,
                project.created_at.isoformat(),
                json.dumps(project.metadata),
            ),
        )
        conn.commit()

    async def add_memory(self, entry: MemoryEntry) -> None:
        conn = self._require_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO memory_entries
            (id, project_id, kind, content, created_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                entry.id,
                entry.project_id,
                entry.kind,
                entry.content,
                entry.created_at.isoformat(),
                json.dumps(entry.metadata),
            ),
        )
        conn.commit()

    async def get_memories(
        self, project_id: str, kind: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Fetch memory entries for a project (stub query)."""
        conn = self._require_conn()
        if kind:
            cur = conn.execute(
                """
                SELECT * FROM memory_entries
                WHERE project_id = ? AND kind = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (project_id, kind, limit),
            )
        else:
            cur = conn.execute(
                """
                SELECT * FROM memory_entries
                WHERE project_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (project_id, limit),
            )
        return [dict(row) for row in cur.fetchall()]

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
