from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import DB_FILE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id          TEXT PRIMARY KEY,
    prompt_file      TEXT NOT NULL,
    project          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'PENDING',
    attempt          INTEGER NOT NULL DEFAULT 0,
    worker_id        TEXT,
    lease_id         TEXT,
    started_at       TEXT,
    heartbeat_at     TEXT,
    checkpoint       TEXT,
    failure_type     TEXT,
    failure_message  TEXT,
    diagnosis        TEXT,
    commit_sha       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    task_id          TEXT NOT NULL,
    attempt          INTEGER NOT NULL,
    status           TEXT,
    failure_type     TEXT,
    failure_message  TEXT,
    diagnosis        TEXT,
    started_at       TEXT,
    finished_at      TEXT,
    PRIMARY KEY (task_id, attempt)
);
"""


class TaskStore:
    """SQLite-backed task state store — the single source of truth for task
    lifecycle, lease/heartbeat, checkpoint, and attempt history.

    Directory state (prompts/ processing/ …) is the *physical* location of the
    prompt file; this store holds the *logical* state.
    """

    def __init__(self) -> None:
        self._conn = _connect()
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the first release (idempotent)."""
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(attempts)")
        }
        if "response" not in columns:
            self._conn.execute("ALTER TABLE attempts ADD COLUMN response TEXT")

    # ------------------------------------------------------------------ basic

    def create_task(self, task_id: str, prompt_file: str, project: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO tasks
                   (task_id, prompt_file, project, status, attempt,
                    created_at, updated_at)
                   VALUES (?, ?, ?, 'RUNNING', 0, ?, ?)""",
                (task_id, prompt_file, project, _now(), _now()),
            )
            self._conn.commit()

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_status(self, task_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (status, _now(), task_id),
            )
            self._conn.commit()

    def update_checkpoint(self, task_id: str, checkpoint: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET checkpoint = ?, updated_at = ? WHERE task_id = ?",
                (checkpoint, _now(), task_id),
            )
            self._conn.commit()

    # -------------------------------------------------------------- lease/hb

    def claim_lease(self, task_id: str, worker_id: str) -> str:
        """Assign a new lease to *task_id*.  Returns the lease_id (fencing token)."""
        lease_id = uuid.uuid4().hex[:8]
        now = _now()
        with self._lock:
            self._conn.execute(
                """UPDATE tasks
                   SET worker_id = ?, lease_id = ?, started_at = ?,
                       heartbeat_at = ?, updated_at = ?
                   WHERE task_id = ?""",
                (worker_id, lease_id, now, now, now, task_id),
            )
            self._conn.commit()
        return lease_id

    def update_heartbeat(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET heartbeat_at = ?, updated_at = ? WHERE task_id = ?",
                (_now(), _now(), task_id),
            )
            self._conn.commit()

    def clear_lease(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE tasks
                   SET worker_id = NULL, lease_id = NULL, updated_at = ?
                   WHERE task_id = ?""",
                (_now(), task_id),
            )
            self._conn.commit()

    def check_lease(self, task_id: str, lease_id: str) -> bool:
        """Fencing: returns True if *lease_id* is still the current lease."""
        with self._lock:
            row = self._conn.execute(
                "SELECT lease_id FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return False
        return row["lease_id"] == lease_id

    def get_stale_tasks(self, lease_timeout: int) -> list[dict]:
        """Return RUNNING tasks whose heartbeat is older than *lease_timeout*."""
        cutoff = datetime.now(timezone.utc).timestamp() - lease_timeout
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM tasks
                   WHERE status = 'RUNNING'
                     AND (heartbeat_at IS NULL OR heartbeat_at < ?)""",
                (cutoff_iso,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- attempt log

    def increment_attempt(self, task_id: str) -> int:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET attempt = attempt + 1, updated_at = ? WHERE task_id = ?",
                (_now(), task_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT attempt FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return row["attempt"] if row else 0

    def get_attempt(self, task_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT attempt FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return row["attempt"] if row else 0

    def record_attempt(
        self,
        task_id: str,
        attempt: int,
        status: str,
        failure_type: str = "",
        failure_message: str = "",
        diagnosis: str = "",
        response: str = "",
    ) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO attempts
                   (task_id, attempt, status, failure_type, failure_message,
                    diagnosis, started_at, finished_at, response)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, attempt, status, failure_type, failure_message,
                 diagnosis, now, now, response),
            )
            self._conn.execute(
                """UPDATE tasks
                   SET failure_type = ?, failure_message = ?, diagnosis = ?,
                       updated_at = ?
                   WHERE task_id = ?""",
                (failure_type, failure_message, diagnosis, now, task_id),
            )
            self._conn.commit()

    def set_commit(self, task_id: str, commit: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET commit_sha = ?, updated_at = ? WHERE task_id = ?",
                (commit, _now(), task_id),
            )
            self._conn.commit()

    def get_all_running(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status = 'RUNNING'"
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
