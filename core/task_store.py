"""
SQLite persistence for Atlas tasks (Phase 8).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.logger import get_logger
from core.task_manager import Task, TaskStatus

logger = get_logger("task.store")

# Keys that must never be persisted (runtime-only objects)
_NON_PERSISTENT_KEYS = frozenset({"_cancel_event"})


def _sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Strip non-JSON-serializable runtime fields before SQLite write."""

    def _clean(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: _clean(v)
                for k, v in obj.items()
                if k not in _NON_PERSISTENT_KEYS
            }
        if isinstance(obj, list):
            return [_clean(x) for x in obj]
        if isinstance(obj, (str, int, float, bool)) or obj is None:
            return obj
        return str(obj)

    return _clean(metadata)


SCHEMA = """
CREATE TABLE IF NOT EXISTS persisted_tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    project_id TEXT,
    status TEXT NOT NULL,
    priority INTEGER DEFAULT 50,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT,
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON persisted_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON persisted_tasks(project_id, status);
"""


class TaskStore:
    """Durable task storage backed by SQLite."""

    UNFINISHED = (
        TaskStatus.PENDING,
        TaskStatus.RUNNING,
        TaskStatus.VERIFYING,
        TaskStatus.RETRYING,
        TaskStatus.INVESTIGATING,
        TaskStatus.BLOCKED,
    )

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    async def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        logger.info("Task store ready at %s", self.db_path)

    def _conn_req(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("TaskStore not initialized")
        return self._conn

    def _task_to_row(self, task: Task) -> tuple:
        return (
            task.id,
            task.title,
            task.description,
            task.project_id,
            task.status.value if isinstance(task.status, TaskStatus) else str(task.status),
            int(task.metadata.get("priority", 50)),
            task.created_at.isoformat(),
            task.started_at.isoformat() if task.started_at else None,
            task.updated_at.isoformat() if task.updated_at else None,
            task.error,
            json.dumps(_sanitize_metadata(task.metadata)),
        )

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        meta = json.loads(row["metadata_json"] or "{}")
        return Task(
            id=row["id"],
            title=row["title"],
            description=row["description"] or "",
            project_id=row["project_id"],
            status=TaskStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=(
                datetime.fromisoformat(row["started_at"])
                if row["started_at"]
                else None
            ),
            updated_at=(
                datetime.fromisoformat(row["updated_at"])
                if row["updated_at"]
                else None
            ),
            metadata=meta,
            error=row["error"],
        )

    async def upsert(self, task: Task) -> None:
        conn = self._conn_req()
        conn.execute(
            """
            INSERT INTO persisted_tasks
            (id, title, description, project_id, status, priority,
             created_at, started_at, updated_at, error, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                project_id=excluded.project_id,
                status=excluded.status,
                priority=excluded.priority,
                started_at=excluded.started_at,
                updated_at=excluded.updated_at,
                error=excluded.error,
                metadata_json=excluded.metadata_json
            """,
            self._task_to_row(task),
        )
        conn.commit()

    async def get(self, task_id: str) -> Task | None:
        conn = self._conn_req()
        cur = conn.execute(
            "SELECT * FROM persisted_tasks WHERE id = ?", (task_id,)
        )
        row = cur.fetchone()
        return self._row_to_task(row) if row else None

    async def load_all(self) -> list[Task]:
        conn = self._conn_req()
        cur = conn.execute("SELECT * FROM persisted_tasks ORDER BY created_at")
        return [self._row_to_task(r) for r in cur.fetchall()]

    async def load_unfinished(self) -> list[Task]:
        conn = self._conn_req()
        placeholders = ",".join("?" * len(self.UNFINISHED))
        statuses = [s.value for s in self.UNFINISHED]
        cur = conn.execute(
            f"""
            SELECT * FROM persisted_tasks
            WHERE status IN ({placeholders})
            ORDER BY priority DESC, created_at
            """,
            statuses,
        )
        return [self._row_to_task(r) for r in cur.fetchall()]

    async def delete(self, task_id: str) -> None:
        conn = self._conn_req()
        conn.execute("DELETE FROM persisted_tasks WHERE id = ?", (task_id,))
        conn.commit()

    async def integrity_check(self) -> list[str]:
        issues = []
        conn = self._conn_req()
        try:
            conn.execute("PRAGMA integrity_check")
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if row and row[0] != "ok":
                issues.append(f"SQLite integrity: {row[0]}")
        except Exception as exc:
            issues.append(f"DB check failed: {exc}")
        return issues

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
