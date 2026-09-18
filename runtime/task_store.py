from __future__ import annotations

import sqlite3
import threading
import uuid
import re
from datetime import datetime, timezone
from pathlib import Path

from config import DB_FILE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(value: str) -> str:
    """Remove common secret forms before durable evidence is stored."""
    text = str(value or "")
    patterns = (
        (r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+", r"\1[REDACTED]"),
        (r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]"),
        (r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{12,}\b", "[REDACTED]"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


def _connect() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
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

CREATE TABLE IF NOT EXISTS requirement_runs (
    run_id           TEXT PRIMARY KEY,
    requirement_file TEXT NOT NULL,
    requirement_hash TEXT,
    project          TEXT NOT NULL,
    base_revision    TEXT,
    final_revision   TEXT,
    status           TEXT NOT NULL DEFAULT 'PLANNED',
    review_status    TEXT,
    coverage_score   REAL,
    risk_level       TEXT,
    planner_mode     TEXT,
    round            INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requirement_reviews (
    review_id          TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL,
    round              INTEGER NOT NULL DEFAULT 1,
    base_revision      TEXT,
    final_revision     TEXT,
    status             TEXT NOT NULL,
    report_path        TEXT,
    json_path          TEXT,
    coverage_score     REAL,
    risk_level         TEXT,
    followup_generated INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS architecture_snapshots (
    id                  TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    diagram_type        TEXT NOT NULL DEFAULT 'architecture',
    repository_path     TEXT NOT NULL,
    repository_revision TEXT NOT NULL,
    branch              TEXT,
    status              TEXT NOT NULL,
    json_path           TEXT,
    html_path           TEXT,
    svg_path            TEXT,
    metadata_path       TEXT,
    archify_version     TEXT,
    source_task_id      TEXT,
    error               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (project_id, id)
);

CREATE INDEX IF NOT EXISTS idx_architecture_project
ON architecture_snapshots(project_id);

CREATE INDEX IF NOT EXISTS idx_architecture_status
ON architecture_snapshots(status);

CREATE INDEX IF NOT EXISTS idx_architecture_revision
ON architecture_snapshots(repository_revision);

CREATE TABLE IF NOT EXISTS events (
    event_id           TEXT PRIMARY KEY,
    event_type         TEXT NOT NULL,
    occurred_at        TEXT NOT NULL,
    run_id             TEXT,
    plan_id            TEXT,
    task_id            TEXT,
    attempt_id         TEXT,
    operation_id       TEXT,
    worker_id          TEXT,
    lease_id           TEXT,
    causation_id       TEXT,
    status             TEXT,
    payload            TEXT NOT NULL DEFAULT '{}',
    schema_version     INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_attempt ON events(attempt_id, occurred_at);

CREATE TABLE IF NOT EXISTS operations (
    operation_id       TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL,
    attempt_id         TEXT,
    kind                TEXT NOT NULL,
    status              TEXT NOT NULL,
    result              TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS validations (
    validation_id      TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL,
    attempt_id         TEXT,
    phase               TEXT NOT NULL,
    status              TEXT NOT NULL,
    command             TEXT,
    exit_code           INTEGER,
    duration_ms         INTEGER,
    output              TEXT,
    changed_files       TEXT NOT NULL DEFAULT '[]',
    violations          TEXT NOT NULL DEFAULT '[]',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_projects (
    run_id              TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    project_ref         TEXT,
    repository_path     TEXT NOT NULL,
    branch              TEXT,
    base_revision       TEXT,
    final_revision      TEXT,
    PRIMARY KEY (run_id, project_id)
);

CREATE TABLE IF NOT EXISTS document_runs (
    run_id              TEXT PRIMARY KEY,
    mode                TEXT NOT NULL,
    status              TEXT NOT NULL,
    report_path         TEXT,
    stats               TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_items (
    run_id              TEXT NOT NULL,
    item_id             TEXT NOT NULL,
    source_name         TEXT NOT NULL,
    source_path         TEXT NOT NULL,
    sha256              TEXT,
    project_id          TEXT,
    category            TEXT,
    confidence          REAL,
    action              TEXT,
    target_path         TEXT,
    status              TEXT NOT NULL,
    detail              TEXT,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (run_id,item_id)
);

CREATE TABLE IF NOT EXISTS document_import_runs (
    import_run_id       TEXT PRIMARY KEY,
    status              TEXT NOT NULL,
    mode                TEXT NOT NULL,
    plan_path           TEXT,
    plan_hash           TEXT NOT NULL,
    effective_config    TEXT NOT NULL DEFAULT '{}',
    stats               TEXT NOT NULL DEFAULT '{}',
    error               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_source_roots (
    source_root_id      TEXT PRIMARY KEY,
    import_run_id       TEXT NOT NULL,
    input_path          TEXT NOT NULL,
    resolved_path       TEXT NOT NULL,
    source_is_file      INTEGER NOT NULL DEFAULT 0,
    source_type         TEXT NOT NULL,
    profile             TEXT,
    project_refs        TEXT NOT NULL DEFAULT '[]',
    actions             TEXT NOT NULL DEFAULT '{}',
    recursive           INTEGER NOT NULL DEFAULT 1,
    scan_error          TEXT,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_source_files (
    source_file_id      TEXT PRIMARY KEY,
    import_run_id       TEXT NOT NULL,
    source_root_id      TEXT NOT NULL,
    original_path       TEXT NOT NULL,
    relative_path       TEXT NOT NULL,
    sha256              TEXT NOT NULL,
    normalized_hash     TEXT,
    size_bytes          INTEGER NOT NULL,
    modified_at         TEXT,
    project_id          TEXT,
    category            TEXT,
    topic               TEXT,
    target_path         TEXT,
    status              TEXT NOT NULL,
    detail              TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE(import_run_id, source_root_id, relative_path)
);

CREATE TABLE IF NOT EXISTS document_claims (
    claim_id            TEXT PRIMARY KEY,
    import_run_id       TEXT NOT NULL,
    source_file_id      TEXT NOT NULL,
    ordinal             INTEGER NOT NULL,
    claim_type          TEXT NOT NULL,
    text                TEXT NOT NULL,
    source_location     TEXT,
    project_ids         TEXT NOT NULL DEFAULT '[]',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_evidence (
    evidence_id         TEXT PRIMARY KEY,
    import_run_id       TEXT NOT NULL,
    claim_id            TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    repository_path     TEXT NOT NULL,
    revision            TEXT NOT NULL,
    file_path           TEXT NOT NULL,
    line_number         INTEGER,
    symbol              TEXT,
    excerpt             TEXT,
    excerpt_hash        TEXT,
    method              TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_validations_p13 (
    validation_id       TEXT PRIMARY KEY,
    import_run_id       TEXT NOT NULL,
    claim_id            TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    revision            TEXT NOT NULL,
    status              TEXT NOT NULL,
    confidence          REAL NOT NULL,
    detail              TEXT,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_repairs (
    repair_id           TEXT PRIMARY KEY,
    import_run_id       TEXT NOT NULL,
    source_file_id      TEXT NOT NULL,
    mode                TEXT NOT NULL,
    draft_path          TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    validation_revision TEXT,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_relations (
    relation_id         TEXT PRIMARY KEY,
    import_run_id       TEXT NOT NULL,
    source_document_id  TEXT NOT NULL,
    target_document_id  TEXT NOT NULL,
    relation_type       TEXT NOT NULL,
    confidence          REAL NOT NULL,
    detail              TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE(import_run_id, source_document_id, target_document_id, relation_type)
);

CREATE TABLE IF NOT EXISTS document_clusters (
    cluster_id          TEXT PRIMARY KEY,
    import_run_id       TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    topic               TEXT NOT NULL,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_cluster_members (
    cluster_id          TEXT NOT NULL,
    source_file_id      TEXT NOT NULL,
    relation            TEXT NOT NULL DEFAULT 'member',
    PRIMARY KEY(cluster_id, source_file_id)
);

CREATE TABLE IF NOT EXISTS document_summaries (
    summary_id          TEXT PRIMARY KEY,
    import_run_id       TEXT NOT NULL,
    cluster_id          TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    topic               TEXT NOT NULL,
    path                TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    status              TEXT NOT NULL,
    source_document_ids TEXT NOT NULL DEFAULT '[]',
    validation_revisions TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_doc_import_files
ON document_source_files(import_run_id, project_id, topic);
CREATE INDEX IF NOT EXISTS idx_doc_claims_source
ON document_claims(source_file_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_doc_validations_claim
ON document_validations_p13(claim_id, project_id);
CREATE INDEX IF NOT EXISTS idx_doc_relations_source
ON document_relations(source_document_id, relation_type);
CREATE INDEX IF NOT EXISTS idx_doc_summaries_project
ON document_summaries(project_id, topic, updated_at);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version             TEXT PRIMARY KEY,
    applied_at          TEXT NOT NULL,
    details             TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS requirements (
    requirement_id     TEXT PRIMARY KEY,
    content_hash       TEXT NOT NULL UNIQUE,
    source_path        TEXT NOT NULL,
    snapshot_path      TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id            TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL,
    round              INTEGER NOT NULL,
    status             TEXT NOT NULL,
    json_path           TEXT NOT NULL,
    content_hash       TEXT,
    created_at         TEXT NOT NULL,
    published_at       TEXT,
    UNIQUE(run_id, round),
    FOREIGN KEY(run_id) REFERENCES requirement_runs(run_id)
);

CREATE TABLE IF NOT EXISTS plan_tasks (
    plan_id            TEXT NOT NULL,
    task_id            TEXT NOT NULL,
    ordinal            INTEGER NOT NULL,
    source_review_id   TEXT,
    requirement_items  TEXT NOT NULL DEFAULT '[]',
    reason             TEXT,
    PRIMARY KEY(plan_id, task_id),
    FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id            TEXT NOT NULL,
    depends_on_task_id TEXT NOT NULL,
    PRIMARY KEY(task_id, depends_on_task_id)
);

CREATE TABLE IF NOT EXISTS changesets (
    changeset_id       TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL,
    attempt_id         TEXT,
    changed_files      TEXT NOT NULL DEFAULT '[]',
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commits (
    commit_id          TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL,
    attempt_id         TEXT,
    kind               TEXT NOT NULL,
    commit_sha         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    UNIQUE(task_id, attempt_id, kind, commit_sha)
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id        TEXT PRIMARY KEY,
    run_id             TEXT,
    task_id            TEXT,
    attempt_id         TEXT,
    kind               TEXT NOT NULL,
    path               TEXT NOT NULL,
    content_hash       TEXT,
    status             TEXT NOT NULL DEFAULT 'AVAILABLE',
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_items (
    review_id          TEXT NOT NULL,
    requirement_item_id TEXT NOT NULL,
    status             TEXT NOT NULL,
    evidence_grade     TEXT,
    evidence           TEXT NOT NULL DEFAULT '[]',
    risks              TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(review_id, requirement_item_id),
    FOREIGN KEY(review_id) REFERENCES requirement_reviews(review_id)
);

CREATE INDEX IF NOT EXISTS idx_plans_run ON plans(run_id, round);
CREATE INDEX IF NOT EXISTS idx_plan_tasks_task ON plan_tasks(task_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, created_at);
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
        task_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(tasks)")
        }
        if "run_id" not in task_columns:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN run_id TEXT")
        additions = {
            "version": "INTEGER NOT NULL DEFAULT 0",
            "block_reason": "TEXT",
            "current_attempt_id": "TEXT",
            "merge_commit_sha": "TEXT",
            "completed_at": "TEXT",
        }
        for name, ddl in additions.items():
            if name not in task_columns:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
        attempt_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(attempts)")
        }
        attempt_additions = {
            "attempt_id": "TEXT",
            "worker_id": "TEXT",
            "lease_id": "TEXT",
            "session_id": "TEXT",
            "duration_ms": "INTEGER",
            "config_hash": "TEXT",
            "model": "TEXT",
            "prompt_hash": "TEXT",
            "input_tokens": "INTEGER",
            "output_tokens": "INTEGER",
            "cost": "REAL",
        }
        for name, ddl in attempt_additions.items():
            if name not in attempt_columns:
                self._conn.execute(f"ALTER TABLE attempts ADD COLUMN {name} {ddl}")
        run_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(requirement_runs)")
        }
        if "config_hash" not in run_columns:
            self._conn.execute("ALTER TABLE requirement_runs ADD COLUMN config_hash TEXT")
        if "requirement_id" not in run_columns:
            self._conn.execute("ALTER TABLE requirement_runs ADD COLUMN requirement_id TEXT")
        source_root_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(document_source_roots)")
        }
        if "source_is_file" not in source_root_columns:
            self._conn.execute(
                "ALTER TABLE document_source_roots ADD COLUMN source_is_file INTEGER NOT NULL DEFAULT 0"
            )
        evidence_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(document_evidence)")
        }
        if "excerpt" not in evidence_columns:
            self._conn.execute("ALTER TABLE document_evidence ADD COLUMN excerpt TEXT")
        self._conn.execute(
            """UPDATE events SET run_id=(
                   SELECT t.run_id FROM tasks t WHERE t.task_id=events.task_id
               ) WHERE run_id IS NULL AND task_id IS NOT NULL"""
        )
        self._migrate_p12_history()
        self._record_p12_v2_migration()
        self._record_p13_migration()

    def _record_p13_migration(self) -> None:
        import json
        details = {
            "version": "p13-v1",
            "added_entities": [
                "document_import_runs", "document_source_roots",
                "document_source_files", "document_claims", "document_evidence",
                "document_validations_p13", "document_repairs",
                "document_relations", "document_clusters",
                "document_cluster_members", "document_summaries",
            ],
            "safety": "source roots are read-only; apply is copy-only and hash-fenced",
            "rollback": "restore a pre-migration SQLite backup; imported source files remain intact",
        }
        now = _now()
        self._conn.execute(
            """INSERT OR REPLACE INTO schema_migrations(version,applied_at,details)
               VALUES ('p13-v1',COALESCE((SELECT applied_at FROM schema_migrations
               WHERE version='p13-v1'),?),?)""",
            (now, json.dumps(details, ensure_ascii=False)),
        )
        (DB_FILE.parent / "migration-p13-v1.json").write_text(
            json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _record_p12_v2_migration(self) -> None:
        import json
        existing = self._conn.execute(
            "SELECT details FROM schema_migrations WHERE version='p12-v2'"
        ).fetchone()
        details = {
            "preserved_tables": [
                "tasks", "attempts", "requirement_reviews", "architecture_snapshots"
            ],
            "added_entities": [
                "requirements", "plans", "plan_tasks", "task_dependencies",
                "events", "operations", "changesets", "commits", "validations",
                "artifacts", "review_items", "document_runs", "document_items",
            ],
            "foreign_keys_enabled": bool(
                self._conn.execute("PRAGMA foreign_keys").fetchone()[0]
            ),
            "rollback": "restore a pre-migration SQLite backup created by maintenance.py --backup",
        }
        now = _now()
        if existing:
            self._conn.execute(
                "UPDATE schema_migrations SET details=? WHERE version='p12-v2'",
                (json.dumps(details, ensure_ascii=False),),
            )
        else:
            self._conn.execute(
                "INSERT INTO schema_migrations(version,applied_at,details) VALUES ('p12-v2',?,?)",
                (now, json.dumps(details, ensure_ascii=False)),
            )
        (DB_FILE.parent / "migration-p12-v2.json").write_text(
            json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _migrate_p12_history(self) -> None:
        """Repair legacy projections without inventing missing evidence."""
        import json
        done = self._conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version='p12-v1'"
        ).fetchone()
        if done:
            return
        self._conn.execute(
            """UPDATE attempts SET attempt_id='legacy-' || task_id || '-' || attempt
               WHERE attempt_id IS NULL OR attempt_id=''"""
        )
        self._conn.execute(
            """UPDATE tasks SET attempt=COALESCE(
                 (SELECT MAX(a.attempt) FROM attempts a WHERE a.task_id=tasks.task_id),
                 attempt
               )"""
        )
        issues = [
            {
                "task_id": row["task_id"],
                "issue": "legacy completed task has no deterministic validation evidence",
            }
            for row in self._conn.execute(
                """SELECT t.task_id FROM tasks t
                   WHERE t.status='COMPLETED' AND NOT EXISTS
                     (SELECT 1 FROM validations v WHERE v.task_id=t.task_id)"""
            ).fetchall()
        ]
        now = _now()
        for item in issues:
            task_id = item["task_id"]
            exists = self._conn.execute(
                "SELECT 1 FROM events WHERE task_id=? AND event_type='task.migrated'",
                (task_id,),
            ).fetchone()
            if not exists:
                self._conn.execute(
                    """INSERT INTO events
                       (event_id,event_type,occurred_at,task_id,status,payload,schema_version)
                       VALUES (?,?,?,?,?,?,1)""",
                    (f"evt-{uuid.uuid4().hex}", "task.migrated", now, task_id,
                     "LEGACY", json.dumps(item, ensure_ascii=False)),
                )
        details = {"legacy_validation_gaps": issues, "repaired_attempt_projection": True}
        self._conn.execute(
            "INSERT INTO schema_migrations(version,applied_at,details) VALUES ('p12-v1',?,?)",
            (now, json.dumps(details, ensure_ascii=False)),
        )
        report = DB_FILE.parent / "migration-p12-v1.json"
        report.write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------ basic

    def create_task(
        self, task_id: str, prompt_file: str, project: str, run_id: str = ""
    ) -> bool:
        """Register a task without destroying its execution history.

        Re-claiming a recovered task updates its queue location and re-opens
        the projection, while attempts, commits and creation time survive.
        """
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO tasks
                   (task_id, prompt_file, project, status, attempt, run_id,
                    created_at, updated_at)
                   VALUES (?, ?, ?, 'QUEUED', 0, ?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                     prompt_file=excluded.prompt_file,
                     project=excluded.project,
                     run_id=COALESCE(excluded.run_id, tasks.run_id),
                     status='QUEUED',
                     worker_id=NULL, lease_id=NULL, block_reason=NULL,
                     version=tasks.version+1, updated_at=excluded.updated_at
                   WHERE tasks.status IN ('FAILED','CANCELLED','PENDING')""",
                (task_id, prompt_file, project, run_id or None, now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT status FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        accepted = bool(cur.rowcount == 1 and row and row["status"] == "QUEUED")
        self.append_event(
            "task.queued" if accepted else "task.duplicate.rejected",
            task_id=task_id, run_id=run_id,
            status="QUEUED" if accepted else "REJECTED",
            payload={"prompt_file": prompt_file},
        )
        return accepted

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_tasks(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at,task_id"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_status(self, task_id: str, status: str) -> None:
        self.transition_task(task_id, status)

    def transition_task(self, task_id: str, status: str, block_reason: str = "") -> None:
        now = _now()
        terminal = status in ("COMPLETED", "FAILED", "CANCELLED")
        with self._lock:
            current = self._conn.execute(
                "SELECT status,block_reason FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if (current and current["status"] == status
                    and (current["block_reason"] or "") == (block_reason or "")):
                return
            self._conn.execute(
                """UPDATE tasks SET status=?, block_reason=?, version=version+1,
                   completed_at=CASE WHEN ? THEN ? ELSE completed_at END,
                   worker_id=CASE WHEN ? THEN NULL ELSE worker_id END,
                   lease_id=CASE WHEN ? THEN NULL ELSE lease_id END,
                   updated_at=? WHERE task_id=?""",
                (status, block_reason or None, int(terminal), now,
                 int(terminal), int(terminal), now, task_id),
            )
            self._conn.commit()
        row = self.get_task(task_id) or {}
        self.append_event("task.status.changed", task_id=task_id,
                          run_id=row.get("run_id") or "", status=status,
                          payload={"block_reason": block_reason})

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
            cur = self._conn.execute(
                """UPDATE tasks
                   SET worker_id = ?, lease_id = ?, started_at = ?,
                       heartbeat_at = ?, status='RUNNING', block_reason=NULL,
                       version=version+1, updated_at = ?
                   WHERE task_id = ? AND status IN
                     ('QUEUED','READY','BLOCKED','PENDING') AND lease_id IS NULL""",
                (worker_id, lease_id, now, now, now, task_id),
            )
            self._conn.commit()
            if cur.rowcount != 1:
                raise RuntimeError(f"task {task_id} is not leaseable")
        row = self.get_task(task_id) or {}
        self.append_event("task.lease.claimed", task_id=task_id,
                          run_id=row.get("run_id") or "", worker_id=worker_id,
                          lease_id=lease_id, status="RUNNING")
        return lease_id

    def update_heartbeat(
        self, task_id: str, worker_id: str = "", lease_id: str = ""
    ) -> bool:
        """Renew only the caller's current lease.

        Owner-less updates are retained for node checkpoints in single-process
        compatibility mode, but never used by the background heartbeat.
        """
        with self._lock:
            now = _now()
            if worker_id and lease_id:
                cur = self._conn.execute(
                    """UPDATE tasks SET heartbeat_at=?, updated_at=?
                       WHERE task_id=? AND worker_id=? AND lease_id=?
                         AND status='RUNNING'""",
                    (now, now, task_id, worker_id, lease_id),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE tasks SET heartbeat_at=?, updated_at=? WHERE task_id=? AND status='RUNNING'",
                    (now, now, task_id),
                )
            self._conn.commit()
        return cur.rowcount == 1

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

    def begin_attempt(self, task_id: str, worker_id: str, lease_id: str) -> tuple[int, str]:
        """Atomically allocate and persist the next attempt."""
        now = _now()
        attempt_id = f"attempt-{uuid.uuid4().hex}"
        with self._lock:
            row = self._conn.execute(
                "SELECT attempt,run_id FROM tasks WHERE task_id=? AND worker_id=? AND lease_id=?",
                (task_id, worker_id, lease_id),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"lease lost before attempt for {task_id}")
            attempt = int(row["attempt"] or 0) + 1
            self._conn.execute(
                """UPDATE tasks SET attempt=?, current_attempt_id=?,
                   failure_type=NULL, failure_message=NULL, diagnosis=NULL,
                   updated_at=? WHERE task_id=?""",
                (attempt, attempt_id, now, task_id),
            )
            self._conn.execute(
                """INSERT INTO attempts
                   (task_id,attempt,status,started_at,attempt_id,worker_id,lease_id,config_hash)
                   VALUES (?,?, 'RUNNING', ?,?,?,?,?)""",
                (task_id, attempt, now, attempt_id, worker_id, lease_id,
                 __import__("os").getenv("RUNNER_CONFIG_HASH", "")),
            )
            self._conn.commit()
        self.append_event("attempt.started", task_id=task_id,
                          run_id=row["run_id"] or "", attempt_id=attempt_id,
                          worker_id=worker_id, lease_id=lease_id,
                          status="RUNNING", payload={"attempt_no": attempt})
        return attempt, attempt_id

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
            started = self._conn.execute(
                "SELECT started_at FROM attempts WHERE task_id=? AND attempt=?",
                (task_id, attempt),
            ).fetchone()
            duration_ms = None
            if started and started["started_at"]:
                try:
                    duration_ms = int(
                        (datetime.fromisoformat(now) - datetime.fromisoformat(started["started_at"]))
                        .total_seconds() * 1000
                    )
                except ValueError:
                    pass
            self._conn.execute(
                """INSERT INTO attempts
                   (task_id, attempt, status, failure_type, failure_message,
                    diagnosis, started_at, finished_at, response)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, attempt, status, failure_type, failure_message,
                 diagnosis, now, now, response),
            ) if not self._conn.execute(
                "SELECT 1 FROM attempts WHERE task_id=? AND attempt=?",
                (task_id, attempt),
            ).fetchone() else self._conn.execute(
                """UPDATE attempts SET status=?, failure_type=?,
                   failure_message=?, diagnosis=?, finished_at=?, response=?,duration_ms=?
                   WHERE task_id=? AND attempt=?""",
                (status, failure_type, failure_message, diagnosis, now,
                 response, duration_ms, task_id, attempt),
            )
            self._conn.execute(
                """UPDATE tasks
                   SET failure_type = ?, failure_message = ?, diagnosis = ?,
                       updated_at = ?
                   WHERE task_id = ?""",
                (failure_type, failure_message, diagnosis, now, task_id),
            )
            self._conn.commit()
        row = self.get_task(task_id) or {}
        self.append_event("attempt.finished", task_id=task_id,
                          run_id=row.get("run_id") or "",
                          attempt_id=row.get("current_attempt_id") or "",
                          status=status,
                          payload={"attempt_no": attempt,
                                   "failure_type": failure_type,
                                   "failure_message": failure_message[:1000]})

    def set_attempt_context(
        self, task_id: str, attempt: int, *, model: str = "",
        prompt_hash: str = "", session_id: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE attempts SET
                   model=COALESCE(NULLIF(?,''),model),
                   prompt_hash=COALESCE(NULLIF(?,''),prompt_hash),
                   session_id=COALESCE(NULLIF(?,''),session_id)
                   WHERE task_id=? AND attempt=?""",
                (model, prompt_hash, session_id, task_id, attempt),
            )
            self._conn.commit()

    def set_commit(self, task_id: str, commit: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET commit_sha = ?, updated_at = ? WHERE task_id = ?",
                (commit, _now(), task_id),
            )
            row = self._conn.execute(
                "SELECT current_attempt_id FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if commit:
                self._conn.execute(
                    """INSERT OR IGNORE INTO commits
                       (commit_id,task_id,attempt_id,kind,commit_sha,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (f"commit-{uuid.uuid4().hex}", task_id,
                     row["current_attempt_id"] if row else None,
                     "TASK", commit, _now()),
                )
            self._conn.commit()

    def set_merge_commit(self, task_id: str, commit: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET merge_commit_sha=?, updated_at=? WHERE task_id=?",
                (commit, _now(), task_id),
            )
            row = self._conn.execute(
                "SELECT current_attempt_id FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if commit:
                self._conn.execute(
                    """INSERT OR IGNORE INTO commits
                       (commit_id,task_id,attempt_id,kind,commit_sha,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (f"commit-{uuid.uuid4().hex}", task_id,
                     row["current_attempt_id"] if row else None,
                     "MERGE", commit, _now()),
                )
            self._conn.commit()

    # ------------------------------------------------------ event / evidence

    def append_event(
        self, event_type: str, *, run_id: str = "", plan_id: str = "",
        task_id: str = "", attempt_id: str = "", operation_id: str = "",
        worker_id: str = "", lease_id: str = "", causation_id: str = "",
        status: str = "", payload: dict | None = None,
    ) -> str:
        import json
        event_id = f"evt-{uuid.uuid4().hex}"
        with self._lock:
            self._conn.execute(
                """INSERT INTO events
                   (event_id,event_type,occurred_at,run_id,plan_id,task_id,
                    attempt_id,operation_id,worker_id,lease_id,causation_id,
                    status,payload,schema_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (event_id, event_type, _now(), run_id or None, plan_id or None,
                 task_id or None, attempt_id or None, operation_id or None,
                 worker_id or None, lease_id or None, causation_id or None,
                 status or None, json.dumps(payload or {}, ensure_ascii=False)),
            )
            self._conn.commit()
        return event_id

    def list_events(self, *, run_id: str = "", task_id: str = "") -> list[dict]:
        import json
        where, value = ("run_id", run_id) if run_id else ("task_id", task_id)
        if not value:
            return []
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM events WHERE {where}=? ORDER BY occurred_at,event_id",
                (value,),
            ).fetchall()
        result = [dict(r) for r in rows]
        for item in result:
            try:
                item["payload"] = json.loads(item.get("payload") or "{}")
            except Exception:
                pass
        return result

    def record_validation(
        self, task_id: str, attempt_id: str, phase: str, status: str,
        *, command: str = "", exit_code: int | None = None,
        duration_ms: int = 0, output: str = "", changed_files: list[str] | None = None,
        violations: list[str] | None = None,
    ) -> str:
        import json
        validation_id = f"val-{uuid.uuid4().hex}"
        with self._lock:
            self._conn.execute(
                """INSERT INTO validations
                   (validation_id,task_id,attempt_id,phase,status,command,
                    exit_code,duration_ms,output,changed_files,violations,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (validation_id, task_id, attempt_id or None, phase, status,
                 command or None, exit_code, duration_ms, _redact(output)[:12000],
                 json.dumps(changed_files or [], ensure_ascii=False),
                 json.dumps(violations or [], ensure_ascii=False), _now()),
            )
            self._conn.commit()
        row = self.get_task(task_id) or {}
        self.append_event("task.validation.finished", task_id=task_id,
                          run_id=row.get("run_id") or "", attempt_id=attempt_id,
                          status=status, payload={"validation_id": validation_id,
                                                  "phase": phase,
                                                  "exit_code": exit_code,
                                                  "violations": violations or []})
        return validation_id

    def record_changeset(
        self, task_id: str, attempt_id: str, changed_files: list[str]
    ) -> str:
        import json
        changeset_id = f"changeset-{uuid.uuid4().hex}"
        with self._lock:
            self._conn.execute(
                """INSERT INTO changesets
                   (changeset_id,task_id,attempt_id,changed_files,created_at)
                   VALUES (?,?,?,?,?)""",
                (changeset_id, task_id, attempt_id or None,
                 json.dumps(sorted(set(changed_files)), ensure_ascii=False), _now()),
            )
            self._conn.commit()
        return changeset_id

    def register_artifact(
        self, kind: str, path: str, *, run_id: str = "", task_id: str = "",
        attempt_id: str = "", content_hash: str = "", status: str = "AVAILABLE",
    ) -> str:
        artifact_id = f"artifact-{uuid.uuid4().hex}"
        with self._lock:
            self._conn.execute(
                """INSERT INTO artifacts
                   (artifact_id,run_id,task_id,attempt_id,kind,path,content_hash,status,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (artifact_id, run_id or None, task_id or None, attempt_id or None,
                 kind, path, content_hash or None, status, _now()),
            )
            self._conn.commit()
        return artifact_id

    def reconcile_artifacts(self) -> dict[str, int]:
        """Mark registered paths that disappeared without breaking queries."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT artifact_id,path,status,run_id,task_id FROM artifacts"
            ).fetchall()
            changed: list[tuple[dict, str]] = []
            for raw in rows:
                row = dict(raw)
                expected = "AVAILABLE" if Path(row["path"]).exists() else "LOST"
                if row["status"] != expected:
                    self._conn.execute(
                        "UPDATE artifacts SET status=? WHERE artifact_id=?",
                        (expected, row["artifact_id"]),
                    )
                    changed.append((row, expected))
            self._conn.commit()
        for row, status in changed:
            self.append_event(
                "artifact.reconciled", run_id=row.get("run_id") or "",
                task_id=row.get("task_id") or "", status=status,
                payload={"artifact_id": row["artifact_id"], "path": row["path"]},
            )
        return {
            "available": sum(1 for row in rows if Path(row["path"]).exists()),
            "lost": sum(1 for row in rows if not Path(row["path"]).exists()),
            "changed": len(changed),
        }

    def begin_operation(self, operation_id: str, task_id: str,
                        attempt_id: str, kind: str) -> dict:
        import json
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO operations
                   (operation_id,task_id,attempt_id,kind,status,result,created_at,updated_at)
                   VALUES (?,?,?,?, 'INTENT','{}',?,?)
                   ON CONFLICT(operation_id) DO NOTHING""",
                (operation_id, task_id, attempt_id or None, kind, now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            task = self._conn.execute(
                "SELECT run_id FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        self.append_event(f"operation.{kind}.intent",
                          run_id=(task["run_id"] if task else "") or "",
                          task_id=task_id,
                          attempt_id=attempt_id, operation_id=operation_id,
                          status=dict(row)["status"] if row else "INTENT")
        return dict(row) if row else {}

    def finish_operation(self, operation_id: str, status: str,
                         result: dict | None = None) -> None:
        import json
        with self._lock:
            self._conn.execute(
                "UPDATE operations SET status=?,result=?,updated_at=? WHERE operation_id=?",
                (status, json.dumps(result or {}, ensure_ascii=False), _now(), operation_id),
            )
            row = self._conn.execute(
                "SELECT task_id,attempt_id,kind FROM operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            self._conn.commit()
        if row:
            item = dict(row)
            task = self.get_task(item["task_id"]) or {}
            self.append_event(f"operation.{item['kind']}.finished",
                              run_id=task.get("run_id") or "",
                              task_id=item["task_id"],
                              attempt_id=item.get("attempt_id") or "",
                              operation_id=operation_id, status=status,
                              payload=result or {})

    def list_validations(self, task_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM validations WHERE task_id=? ORDER BY created_at",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------- document runs

    def begin_document_run(self, mode: str, files: list[dict]) -> str:
        import json
        run_id = f"doc-run-{uuid.uuid4().hex}"
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO document_runs VALUES (?,?, 'RUNNING',NULL,'{}',?,?)",
                (run_id, mode, now, now),
            )
            for item in files:
                self._conn.execute(
                    """INSERT INTO document_items
                       (run_id,item_id,source_name,source_path,status,updated_at)
                       VALUES (?,?,?,?, 'SCANNED',?)""",
                    (run_id, item["item_id"], item["name"], item["path"], now),
                )
            self._conn.commit()
        self.append_event("document.run.started", run_id=run_id,
                          status="RUNNING", payload={"count": len(files), "mode": mode})
        return run_id

    def update_document_item(self, run_id: str, item_id: str, **fields) -> None:
        allowed = {"sha256", "project_id", "category", "confidence", "action",
                   "target_path", "status", "detail"}
        pairs = [(k, v) for k, v in fields.items() if k in allowed]
        if not pairs:
            return
        cols = ",".join(f"{k}=?" for k, _ in pairs)
        vals = [v for _, v in pairs] + [_now(), run_id, item_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE document_items SET {cols},updated_at=? WHERE run_id=? AND item_id=?",
                vals,
            )
            self._conn.commit()
        self.append_event("document.item.changed", run_id=run_id,
                          status=str(fields.get("status") or ""),
                          payload={"item_id": item_id, **fields})

    def finish_document_run(self, run_id: str, status: str,
                            report_path: str = "", stats: dict | None = None) -> None:
        import json
        with self._lock:
            self._conn.execute(
                """UPDATE document_runs SET status=?,report_path=?,stats=?,updated_at=?
                   WHERE run_id=?""",
                (status, report_path or None,
                 json.dumps(stats or {}, ensure_ascii=False), _now(), run_id),
            )
            self._conn.commit()
        self.append_event("document.run.finished", run_id=run_id,
                          status=status, payload={"report_path": report_path,
                                                  "stats": stats or {}})

    def fail_running_document_runs(self, error: str) -> list[str]:
        """Close stranded document runs owned by the single document runner."""
        now = _now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id FROM document_runs WHERE status='RUNNING'"
            ).fetchall()
            run_ids = [str(row["run_id"]) for row in rows]
            self._conn.execute(
                "UPDATE document_runs SET status='FAILED',stats=?,updated_at=? WHERE status='RUNNING'",
                (__import__("json").dumps({"error": error[:2000]}, ensure_ascii=False), now),
            )
            self._conn.commit()
        for run_id in run_ids:
            self.append_event(
                "document.run.failed", run_id=run_id, status="FAILED",
                payload={"error": error[:2000]},
            )
        return run_ids

    def requeue_document_review(
        self, source_name: str, project_id: str, category: str, target_path: str
    ) -> str:
        with self._lock:
            row = self._conn.execute(
                """SELECT run_id,item_id FROM document_items
                   WHERE source_name=? AND status='REVIEW'
                   ORDER BY updated_at DESC LIMIT 1""",
                (source_name,),
            ).fetchone()
            if row:
                self._conn.execute(
                    """UPDATE document_items SET status='REQUEUED',project_id=?,category=?,
                       target_path=?,detail='human review confirmed',updated_at=?
                       WHERE run_id=? AND item_id=?""",
                    (project_id, category, target_path, _now(), row["run_id"], row["item_id"]),
                )
                self._conn.commit()
        run_id = str(row["run_id"]) if row else ""
        self.append_event(
            "document.review.requeued", run_id=run_id, status="REQUEUED",
            payload={"source_name": source_name, "project_id": project_id,
                     "category": category, "target_path": target_path},
        )
        return run_id

    # ---------------------------------------------------- P13 document import

    def save_document_import(self, package: dict) -> None:
        """Persist one P13 import package atomically and idempotently."""
        import json
        run_id = str(package["import_run_id"])
        now = _now()
        plan = package.get("plan") or {}
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO document_import_runs
                   (import_run_id,status,mode,plan_path,plan_hash,effective_config,
                    stats,error,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM
                    document_import_runs WHERE import_run_id=?),?),?)""",
                (run_id, package.get("status", "COMPLETED"),
                 package.get("mode", "rules"), package.get("plan_path") or None,
                 package.get("plan_hash", ""),
                 json.dumps(plan.get("effective_config") or {}, ensure_ascii=False),
                 json.dumps(package.get("stats") or {}, ensure_ascii=False),
                 package.get("error") or None, run_id, now, now),
            )
            for source in plan.get("sources") or []:
                self._conn.execute(
                    """INSERT OR REPLACE INTO document_source_roots
                       (source_root_id,import_run_id,input_path,resolved_path,source_is_file,
                        source_type,profile,project_refs,actions,recursive,
                        scan_error,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (source["source_root_id"], run_id, source.get("input_path", ""),
                     source.get("resolved_path", ""), int(bool(source.get("source_is_file"))),
                     source.get("source_type", "folder"),
                     source.get("profile") or None,
                     json.dumps(source.get("project_refs") or [], ensure_ascii=False),
                     json.dumps(source.get("actions") or {}, ensure_ascii=False),
                     int(bool(source.get("recursive", True))),
                     source.get("scan_error") or None, now),
                )
            for item in package.get("items") or []:
                self._conn.execute(
                    """INSERT OR REPLACE INTO document_source_files
                       (source_file_id,import_run_id,source_root_id,original_path,
                        relative_path,sha256,normalized_hash,size_bytes,modified_at,
                        project_id,category,topic,target_path,status,detail,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item["source_file_id"], run_id, item["source_root_id"],
                     item["original_path"], item["relative_path"], item["sha256"],
                     item.get("normalized_hash"), int(item.get("size_bytes", 0)),
                     item.get("modified_at"), item.get("project_id"), item.get("category"),
                     item.get("topic"), item.get("target_path"), item.get("status", "PLANNED"),
                     item.get("detail"), now, now),
                )
            for claim in package.get("claims") or []:
                self._conn.execute(
                    """INSERT OR REPLACE INTO document_claims
                       (claim_id,import_run_id,source_file_id,ordinal,claim_type,text,
                        source_location,project_ids,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (claim["claim_id"], run_id, claim["source_file_id"],
                     int(claim["ordinal"]), claim["claim_type"], claim["text"],
                     claim.get("source_location"),
                     json.dumps(claim.get("project_ids") or [], ensure_ascii=False), now),
                )
            for evidence in package.get("evidence") or []:
                self._conn.execute(
                    """INSERT OR REPLACE INTO document_evidence
                       (evidence_id,import_run_id,claim_id,project_id,repository_path,
                        revision,file_path,line_number,symbol,excerpt,excerpt_hash,method,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (evidence["evidence_id"], run_id, evidence["claim_id"],
                     evidence["project_id"], evidence["repository_path"],
                     evidence["revision"], evidence["file_path"], evidence.get("line_number"),
                     evidence.get("symbol"), _redact(evidence.get("excerpt", ""))[:500],
                     evidence.get("excerpt_hash"),
                     evidence.get("method", "git-grep"), now),
                )
            for val in package.get("validations") or []:
                self._conn.execute(
                    """INSERT OR REPLACE INTO document_validations_p13
                       (validation_id,import_run_id,claim_id,project_id,revision,status,
                        confidence,detail,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (val["validation_id"], run_id, val["claim_id"], val["project_id"],
                     val["revision"], val["status"], float(val.get("confidence", 0)),
                     val.get("detail"), now),
                )
            for repair in package.get("repairs") or []:
                self._conn.execute(
                    """INSERT OR REPLACE INTO document_repairs
                       (repair_id,import_run_id,source_file_id,mode,draft_path,content_hash,
                        validation_revision,status,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (repair["repair_id"], run_id, repair["source_file_id"], repair["mode"],
                     repair["draft_path"], repair["content_hash"],
                     repair.get("validation_revision"), repair.get("status", "DRAFT"), now, now),
                )
            for relation in package.get("relations") or []:
                self._conn.execute(
                    """INSERT OR REPLACE INTO document_relations
                       (relation_id,import_run_id,source_document_id,target_document_id,
                        relation_type,confidence,detail,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                    (relation["relation_id"], run_id, relation["source_document_id"],
                     relation["target_document_id"], relation["relation_type"],
                     float(relation.get("confidence", 0)), relation.get("detail"), now),
                )
            for cluster in package.get("clusters") or []:
                self._conn.execute(
                    """INSERT OR REPLACE INTO document_clusters
                       (cluster_id,import_run_id,project_id,topic,status,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (cluster["cluster_id"], run_id, cluster["project_id"], cluster["topic"],
                     cluster.get("status", "DRAFT"), now, now),
                )
                for member in cluster.get("members") or []:
                    self._conn.execute(
                        """INSERT OR REPLACE INTO document_cluster_members
                           (cluster_id,source_file_id,relation) VALUES (?,?,?)""",
                        (cluster["cluster_id"], member, "member"),
                    )
            for summary in package.get("summaries") or []:
                self._conn.execute(
                    """INSERT OR REPLACE INTO document_summaries
                       (summary_id,import_run_id,cluster_id,project_id,topic,path,content_hash,
                        status,source_document_ids,validation_revisions,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (summary["summary_id"], run_id, summary["cluster_id"],
                     summary["project_id"], summary["topic"], summary["path"],
                     summary["content_hash"], summary.get("status", "DRAFT"),
                     json.dumps(summary.get("source_document_ids") or [], ensure_ascii=False),
                     json.dumps(summary.get("validation_revisions") or {}, ensure_ascii=False),
                     now, now),
                )
            self._conn.commit()
        self.append_event(
            "document.import.finished", run_id=run_id,
            status=package.get("status", "COMPLETED"), payload=package.get("stats") or {},
        )

    def list_document_imports(self) -> list[dict]:
        import json
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM document_import_runs ORDER BY created_at DESC"
            ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            for key in ("effective_config", "stats"):
                try:
                    row[key] = json.loads(row.get(key) or "{}")
                except ValueError:
                    pass
        return result

    def get_document_import(self, run_id: str) -> dict:
        import json
        with self._lock:
            run = self._conn.execute(
                "SELECT * FROM document_import_runs WHERE import_run_id=?", (run_id,)
            ).fetchone()
            if not run:
                return {}
            result = dict(run)
            for table, key in (
                ("document_source_roots", "sources"),
                ("document_source_files", "items"),
                ("document_claims", "claims"),
                ("document_evidence", "evidence"),
                ("document_validations_p13", "validations"),
                ("document_repairs", "repairs"),
                ("document_relations", "relations"),
                ("document_clusters", "clusters"),
                ("document_summaries", "summaries"),
            ):
                rows = self._conn.execute(
                    f"SELECT * FROM {table} WHERE import_run_id=?", (run_id,)
                ).fetchall()
                result[key] = [dict(row) for row in rows]
        for key in ("effective_config", "stats"):
            try:
                result[key] = json.loads(result.get(key) or "{}")
            except ValueError:
                pass
        return result

    def mark_document_validations_stale(
        self, project_id: str, current_revision: str
    ) -> int:
        """Mark evidence bound to older revisions stale without deleting it."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT validation_id,status,revision FROM document_validations_p13
                   WHERE project_id=? AND revision<>? AND status<>'stale'""",
                (project_id, current_revision),
            ).fetchall()
            for row in rows:
                detail = f"previous_status={row['status']}; current_revision={current_revision}"
                self._conn.execute(
                    """UPDATE document_validations_p13 SET status='stale',detail=?
                       WHERE validation_id=?""",
                    (detail, row["validation_id"]),
                )
            summaries = self._conn.execute(
                "SELECT summary_id,validation_revisions FROM document_summaries WHERE project_id=?",
                (project_id,),
            ).fetchall()
            import json
            stale_summaries = 0
            for summary in summaries:
                try:
                    revisions = json.loads(summary["validation_revisions"] or "{}")
                except ValueError:
                    revisions = {}
                if revisions.get(project_id) and revisions[project_id] != current_revision:
                    self._conn.execute(
                        "UPDATE document_summaries SET status='STALE',updated_at=? WHERE summary_id=?",
                        (_now(), summary["summary_id"]),
                    )
                    stale_summaries += 1
            self._conn.commit()
        if rows or stale_summaries:
            self.append_event(
                "document.validation.stale", status="STALE",
                payload={"project_id": project_id, "current_revision": current_revision,
                         "count": len(rows), "summaries": stale_summaries},
            )
        return len(rows)

    def get_all_running(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status = 'RUNNING'"
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------- requirement runs

    def create_run(
        self,
        run_id: str,
        requirement_file: str,
        requirement_hash: str,
        project: str,
        base_revision: str,
        planner_mode: str,
        config_hash: str = "",
        requirement_id: str = "",
    ) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO requirement_runs
                   (run_id, requirement_file, requirement_hash, project,
                    base_revision, status, planner_mode, round,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'PLANNED', ?, 1, ?, ?)""",
                (run_id, requirement_file, requirement_hash, project,
                 base_revision, planner_mode, now, now),
            )
            self._conn.commit()
            if config_hash:
                self._conn.execute(
                    "UPDATE requirement_runs SET config_hash=? WHERE run_id=?",
                    (config_hash, run_id),
                )
            if requirement_id:
                self._conn.execute(
                    "UPDATE requirement_runs SET requirement_id=? WHERE run_id=?",
                    (requirement_id, run_id),
                )
            self._conn.commit()

    def register_requirement(
        self, content_hash: str, source_path: str, snapshot_path: str
    ) -> str:
        requirement_id = f"req-{content_hash.removeprefix('sha256:')[:24]}"
        with self._lock:
            self._conn.execute(
                """INSERT INTO requirements
                   (requirement_id,content_hash,source_path,snapshot_path,created_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(content_hash) DO NOTHING""",
                (requirement_id, content_hash, source_path, snapshot_path, _now()),
            )
            row = self._conn.execute(
                "SELECT requirement_id FROM requirements WHERE content_hash=?",
                (content_hash,),
            ).fetchone()
            self._conn.commit()
        return str(row["requirement_id"])

    def record_plan(
        self, run_id: str, round_: int, json_path: str,
        tasks: list[dict], status: str = "PUBLISHED",
        source_review_id: str = "",
    ) -> str:
        """Persist one immutable plan and its DAG in one DB transaction."""
        import hashlib
        import json
        raw = json.dumps(tasks, ensure_ascii=False, sort_keys=True)
        plan_id = f"plan-{run_id}-{round_:03d}"
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT INTO plans
                       (plan_id,run_id,round,status,json_path,content_hash,created_at,published_at)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(run_id,round) DO UPDATE SET
                         status=excluded.status,json_path=excluded.json_path,
                         content_hash=excluded.content_hash,published_at=excluded.published_at""",
                    (plan_id, run_id, round_, status, json_path,
                     f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}",
                     now, now if status == "PUBLISHED" else None),
                )
                self._conn.execute("DELETE FROM plan_tasks WHERE plan_id=?", (plan_id,))
                for ordinal, task in enumerate(tasks, 1):
                    self._conn.execute(
                        """INSERT INTO plan_tasks
                           (plan_id,task_id,ordinal,source_review_id,requirement_items,reason)
                           VALUES (?,?,?,?,?,?)""",
                        (plan_id, task["id"], ordinal, source_review_id or None,
                         json.dumps(task.get("requirement_ids") or [], ensure_ascii=False),
                         task.get("reason") or None),
                    )
                    self._conn.execute(
                        "DELETE FROM task_dependencies WHERE task_id=?", (task["id"],)
                    )
                    for dependency in task.get("depends_on") or []:
                        self._conn.execute(
                            "INSERT INTO task_dependencies(task_id,depends_on_task_id) VALUES (?,?)",
                            (task["id"], dependency),
                        )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        self.append_event(
            "run.plan.published", run_id=run_id, plan_id=plan_id, status=status,
            payload={"round": round_, "task_ids": [t["id"] for t in tasks]},
        )
        return plan_id

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM requirement_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_runs(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requirement_runs ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def register_run_projects(self, run_id: str, projects: list[dict]) -> None:
        with self._lock:
            for p in projects:
                self._conn.execute(
                    """INSERT OR REPLACE INTO run_projects
                       (run_id,project_id,project_ref,repository_path,branch,
                        base_revision,final_revision)
                       VALUES (?,?,?,?,?,?,NULL)""",
                    (run_id, p.get("id", ""), p.get("ref", ""),
                     p.get("path", ""), p.get("branch", ""),
                     p.get("base_revision", "")),
                )
            self._conn.commit()

    def update_run_project_final(
        self, run_id: str, project_id: str, final_revision: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE run_projects SET final_revision=? WHERE run_id=? AND project_id=?",
                (final_revision, run_id, project_id),
            )
            self._conn.commit()

    def get_run_projects(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM run_projects WHERE run_id=? ORDER BY project_id",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_run(self, run_id: str, **fields: object) -> None:
        """Update arbitrary columns on a requirement run row."""
        if not fields:
            return
        allowed = {
            "status", "review_status", "final_revision", "coverage_score",
            "risk_level", "round",
        }
        cols, vals = [], []
        for key, value in fields.items():
            if key in allowed:
                cols.append(f"{key} = ?")
                vals.append(value)
        if not cols:
            return
        vals.extend([_now(), run_id])
        with self._lock:
            self._conn.execute(
                f"""UPDATE requirement_runs
                    SET {', '.join(cols)}, updated_at = ?
                    WHERE run_id = ?""",
                vals,
            )
            self._conn.commit()
        if "status" in fields:
            self.append_event(
                "run.status.changed", run_id=run_id,
                status=str(fields["status"]),
                payload={k: v for k, v in fields.items() if k in allowed},
            )

    def find_run_by_requirement(
        self, requirement_file: str, project: str
    ) -> dict | None:
        """Latest run for a requirement file + project, if any."""
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM requirement_runs
                   WHERE requirement_file = ? AND project = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (requirement_file, project),
            ).fetchone()
        return dict(row) if row else None

    def get_run_tasks(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def runs_needing_review(self) -> list[dict]:
        """Runs whose tasks are all COMPLETED and that await final review.

        A run qualifies when it has at least one task, every task is
        COMPLETED, and its status is still PLANNED/EXECUTING (i.e. not
        already reviewed to a terminal state for the current round).
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT r.* FROM requirement_runs r
                   WHERE r.status IN ('PLANNED', 'QUEUED', 'EXECUTING', 'EXECUTION_FAILED')
                     AND EXISTS (SELECT 1 FROM tasks t WHERE t.run_id = r.run_id)
                      AND NOT EXISTS (
                         SELECT 1 FROM tasks t
                         WHERE t.run_id = r.run_id
                           AND t.status NOT IN ('COMPLETED','FAILED','CANCELLED')
                      )"""
            ).fetchall()
        return [dict(r) for r in rows]

    def runs_with_failed_tasks(self) -> list[dict]:
        """Runs whose tasks all reached a terminal state but at least one FAILED.

        Such a run can never satisfy its requirement as-is; the caller marks
        it FAILED so it does not linger in EXECUTING forever.  Requeueing a
        failed task file (prompts/) re-opens the run.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT r.* FROM requirement_runs r
                   WHERE r.status IN ('PLANNED', 'QUEUED', 'EXECUTING')
                     AND EXISTS (
                         SELECT 1 FROM tasks t
                         WHERE t.run_id = r.run_id AND t.status = 'FAILED'
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM tasks t
                         WHERE t.run_id = r.run_id
                           AND t.status NOT IN ('COMPLETED', 'FAILED')
                     )"""
            ).fetchall()
        return [dict(r) for r in rows]

    def record_review(
        self,
        review_id: str,
        run_id: str,
        round_: int,
        status: str,
        report_path: str,
        json_path: str,
        coverage_score: float,
        risk_level: str,
        followup_generated: bool,
        base_revision: str,
        final_revision: str,
    ) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO requirement_reviews
                   (review_id, run_id, round, status, report_path, json_path,
                    coverage_score, risk_level, followup_generated,
                    base_revision, final_revision, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (review_id, run_id, round_, status, report_path, json_path,
                 coverage_score, risk_level, int(followup_generated),
                 base_revision, final_revision, now, now),
            )
            self._conn.commit()

    def record_review_items(
        self, review_id: str, requirements: list, risks: list | None = None
    ) -> None:
        import json
        risk_data = [
            {
                "risk_id": item.risk_id, "type": item.type,
                "severity": item.severity, "description": item.description,
                "evidence": item.evidence,
                "recommended_action": item.recommended_action,
            }
            for item in (risks or [])
        ]
        with self._lock:
            self._conn.execute("DELETE FROM review_items WHERE review_id=?", (review_id,))
            for item in requirements:
                self._conn.execute(
                    """INSERT INTO review_items
                       (review_id,requirement_item_id,status,evidence_grade,evidence,risks)
                       VALUES (?,?,?,?,?,?)""",
                    (review_id, item.requirement_id, item.status,
                     item.evidence_grade or None,
                     json.dumps({
                         "task_ids": item.task_ids,
                         "changed_files": item.changed_files,
                         "evidence": item.evidence,
                         "missing_evidence": item.missing_evidence,
                         "tests": item.tests,
                     }, ensure_ascii=False),
                     json.dumps(risk_data, ensure_ascii=False)),
                )
            self._conn.commit()

    def get_review_count(self, run_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM requirement_reviews WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    # --------------------------------------------------- architecture (p10)

    def upsert_architecture_snapshot(self, snapshot) -> None:
        """Insert or update an architecture_snapshots row from a
        ArchitectureSnapshot dataclass."""
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO architecture_snapshots
                   (id, project_id, diagram_type, repository_path,
                    repository_revision, branch, status, json_path, html_path,
                    svg_path, metadata_path, archify_version, source_task_id,
                    error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (snapshot.id, snapshot.project_id, snapshot.diagram_type,
                 snapshot.repository_path, snapshot.repository_revision,
                 snapshot.branch, snapshot.status, snapshot.json_path,
                 snapshot.html_path, snapshot.svg_path, snapshot.metadata_path,
                 snapshot.archify_version, snapshot.source_task_id,
                 snapshot.error, snapshot.created_at or now, now),
            )
            self._conn.commit()

    def get_architecture_snapshot(self, project_id: str, snapshot_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM architecture_snapshots
                   WHERE project_id = ? AND id = ?""",
                (project_id, snapshot_id),
            ).fetchone()
        return dict(row) if row else None

    def get_latest_architecture(self, project_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM architecture_snapshots
                   WHERE project_id = ? AND status = 'COMPLETED'
                   ORDER BY updated_at DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_architecture_snapshots(self, project_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM architecture_snapshots
                   WHERE project_id = ? ORDER BY updated_at DESC""",
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_architecture_status(
        self, project_id: str, snapshot_id: str, status: str, error: str = ""
    ) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE architecture_snapshots SET status=?,error=?,updated_at=?
                   WHERE project_id=? AND id=?""",
                (status, error or None, _now(), project_id, snapshot_id),
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
