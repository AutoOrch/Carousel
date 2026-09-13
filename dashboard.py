"""Read-only web dashboard for the task runner.

Serves a small static UI (web/) plus JSON APIs over the SQLite task store,
task files, reports, and the agent log.  Never writes to the store —
watcher.py stays the single writer.

Usage:
    python dashboard.py                # http://127.0.0.1:8080
    python dashboard.py --port 9000
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from config import (
    DB_FILE,
    FAILED_DIR,
    PROCESSED_DIR,
    PROCESSING_DIR,
    PROMPTS_DIR,
    REPORTS_DIR,
    ROOT,
    load_config,
)
from runtime.task_resolver import parse_markdown

app = FastAPI(title="LangGraph + OpenCode runner dashboard")

WEB_DIR = ROOT / "web"
LOG_FILE = ROOT / "logs" / "agent.log"

# Lease liveness threshold — mirrors RecoveryManager semantics.
LEASE_TIMEOUT = 300

LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] ([\w.]+): (.*)$"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_db() -> list[dict[str, Any]]:
    """Read all rows from the tasks table (short-lived connection)."""
    if not DB_FILE.exists():
        return []
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    try:
        rows = conn.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _read_attempts(task_id: str) -> list[dict[str, Any]]:
    if not DB_FILE.exists():
        return []
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    try:
        rows = conn.execute(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _read_front_matters() -> dict[str, dict[str, Any]]:
    """Scan every lifecycle dir for task files; index front-matter by id."""
    result: dict[str, dict[str, Any]] = {}
    dirs = {
        PROMPTS_DIR: "prompts",
        PROCESSING_DIR: "processing",
        PROCESSED_DIR: "processed",
        FAILED_DIR: "failed",
    }
    for directory, label in dirs.items():
        if not directory.exists():
            continue
        for f in sorted(directory.glob("*.md")):
            try:
                meta, body = parse_markdown(
                    f.read_text(encoding="utf-8-sig", errors="replace")
                )
            except Exception:
                continue
            result[f.stem] = {
                "id": f.stem,
                "location": label,
                "file": f.name,
                "project": meta.get("project"),
                "allowed_paths": meta.get("allowed_paths") or [],
                "depends_on": meta.get("depends_on") or [],
                "simulate_failure": int(meta.get("simulate_failure", 0) or 0),
                "title": _first_heading(body) or f.stem,
                "body_preview": body.strip()[:300],
            }
    return result


def _first_heading(body: str) -> str | None:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _liveness(task: dict[str, Any]) -> str:
    """Classify heartbeat freshness for RUNNING tasks."""
    if task["status"] != "RUNNING":
        return "n/a"
    hb = task.get("heartbeat_at")
    if not hb:
        return "no-heartbeat"
    try:
        hb_dt = datetime.fromisoformat(hb)
        age = (datetime.now(timezone.utc) - hb_dt).total_seconds()
        if age > LEASE_TIMEOUT:
            return "stale"
        return "alive"
    except ValueError:
        return "unknown"


def _canonical_project(ref: str | None) -> str:
    """Normalise a project reference to its canonical id.

    Front-matter carries the raw ref (often a path like ``D:/Workspace/Talen``)
    while the DB stores the canonical id (directory name, e.g. ``Talen``).
    Both must map to the same key or the same project splits into two rows.
    """
    if not ref:
        return "unknown"
    ref = str(ref).replace("\\", "/").rstrip("/")
    if "/" in ref:
        return ref.rsplit("/", 1)[-1] or "unknown"
    return ref


def _build_task_view() -> list[dict[str, Any]]:
    """Merge DB rows with front-matter data into the task list view."""
    db_rows = {r["task_id"]: r for r in _read_db()}
    fm_rows = _read_front_matters()
    all_ids = list(dict.fromkeys([*db_rows.keys(), *fm_rows.keys()]))

    tasks: list[dict[str, Any]] = []
    for task_id in all_ids:
        db = db_rows.get(task_id)
        fm = fm_rows.get(task_id)
        raw_ref = (fm or {}).get("project") or (db or {}).get("project")
        entry: dict[str, Any] = {
            "id": task_id,
            "title": fm["title"] if fm else task_id,
            "project": _canonical_project(raw_ref),
            "project_ref": raw_ref,
            "location": (fm or {}).get("location", "gone"),
            "depends_on": (fm or {}).get("depends_on", []),
            "allowed_paths": (fm or {}).get("allowed_paths", []),
            "status": (db or {}).get("status", "UNKNOWN"),
            "attempt": (db or {}).get("attempt", 0),
            "checkpoint": (db or {}).get("checkpoint"),
            "commit_sha": (db or {}).get("commit_sha"),
            "failure_type": (db or {}).get("failure_type"),
            "created_at": (db or {}).get("created_at"),
            "updated_at": (db or {}).get("updated_at"),
        }
        if db:
            entry["heartbeat_at"] = db.get("heartbeat_at")
            entry["liveness"] = _liveness(db)
        else:
            entry["liveness"] = "n/a"
        tasks.append(entry)
    return tasks


def _parse_log(lines_limit: int = 5000) -> list[dict[str, Any]]:
    """Parse the tail of agent.log into structured events."""
    if not LOG_FILE.exists():
        return []
    try:
        with LOG_FILE.open(encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-lines_limit:]
    except OSError:
        return []
    events = []
    for line in lines:
        m = LOG_LINE_RE.match(line.rstrip("\n"))
        if not m:
            continue
        events.append(
            {
                "time": m.group(1),
                "level": m.group(2),
                "module": m.group(3),
                "message": m.group(4),
            }
        )
    return events


def _task_events(task_id: str) -> list[dict[str, Any]]:
    """Log events for one task, with node markers for the timeline."""
    events = _parse_log()
    scoped = [e for e in events if f"[{task_id}]" in e["message"]]
    for e in scoped:
        e["message"] = e["message"].replace(f"[{task_id}] ", "")
    return scoped


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/api/summary")
def api_summary() -> dict[str, Any]:
    tasks = _build_task_view()
    counts: dict[str, int] = {}
    by_project: dict[str, dict[str, int]] = {}
    running = []
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
        proj = str(t["project"] or "unknown")
        by_project.setdefault(proj, {})
        by_project[proj][t["status"]] = by_project[proj].get(t["status"], 0) + 1
        if t["status"] == "RUNNING":
            running.append(t)
    pending_queue = sum(1 for t in tasks if t["location"] == "prompts")
    blocked = sum(
        1
        for t in tasks
        if t["location"] == "processing" and t["status"] in ("PENDING", "UNKNOWN")
    )
    recent = _parse_log(lines_limit=300)
    interesting = [
        e
        for e in recent
        if any(k in e["message"] for k in ("SUCCESS", "FAILED", "merge", "crashed", "cascade"))
    ][-30:][::-1]
    return {
        "generated_at": _now_iso(),
        "total": len(tasks),
        "counts": counts,
        "by_project": by_project,
        "queue": {"prompts": pending_queue, "blocked": blocked},
        "running": running,
        "recent_events": interesting,
    }


@app.get("/api/tasks")
def api_tasks() -> list[dict[str, Any]]:
    return _build_task_view()


@app.get("/api/tasks/{task_id}")
def api_task(task_id: str) -> dict[str, Any]:
    tasks = {t["id"]: t for t in _build_task_view()}
    if task_id not in tasks:
        raise HTTPException(404, "task not found")
    detail = dict(tasks[task_id])
    detail["attempts"] = _read_attempts(task_id)
    detail["timeline"] = _task_events(task_id)
    return detail


@app.get("/api/tasks/{task_id}/report")
def api_task_report(task_id: str) -> dict[str, str]:
    path = REPORTS_DIR / f"{task_id}.md"
    if not path.exists():
        raise HTTPException(404, "report not found")
    return {"task_id": task_id, "markdown": path.read_text(encoding="utf-8-sig", errors="replace")}


@app.get("/api/graph")
def api_graph() -> dict[str, Any]:
    tasks = _build_task_view()
    nodes = [
        {
            "id": t["id"],
            "label": t["id"],
            "status": t["status"],
            "project": str(t["project"] or "unknown"),
            "location": t["location"],
        }
        for t in tasks
    ]
    ids = {n["id"] for n in nodes}
    edges = []
    for t in tasks:
        for dep in t["depends_on"]:
            edges.append({"from": str(dep), "to": t["id"], "known": str(dep) in ids})
    return {"nodes": nodes, "edges": edges}


@app.get("/api/projects")
def api_projects() -> list[dict[str, Any]]:
    config = load_config()
    projects = [{"id": p.id, "path": str(p.path), "branch": p.default_branch}
                for p in config.projects.values()]
    tasks = _build_task_view()
    for p in projects:
        p["tasks"] = [t["id"] for t in tasks if t["project"] == p["id"]]
    # ad-hoc path projects (not in config.yaml) — keep the raw ref as path
    known = {p["id"] for p in projects}
    refs: dict[str, str] = {}
    for t in tasks:
        proj = t["project"]
        if proj and proj not in known and proj != "unknown":
            refs.setdefault(proj, str(t.get("project_ref") or proj))
    for proj, ref in refs.items():
        projects.append({
            "id": proj,
            "path": ref,
            "branch": "?",
            "tasks": [t["id"] for t in tasks if t["project"] == proj],
        })
    return projects


@app.get("/api/events/stream")
async def api_events_stream() -> StreamingResponse:
    """SSE stream: pushes a snapshot digest whenever the tasks table changes."""

    async def gen() -> AsyncIterator[bytes]:
        last_digest: str | None = None
        while True:
            rows = _read_db()
            digest = ";".join(
                f"{r['task_id']}:{r['status']}:{r['checkpoint']}:{r['attempt']}:{r['updated_at']}"
                for r in rows
            )
            if digest != last_digest:
                last_digest = digest
                payload = {
                    "type": "tasks",
                    "generated_at": _now_iso(),
                    "tasks": [
                        {
                            "task_id": r["task_id"],
                            "status": r["status"],
                            "checkpoint": r["checkpoint"],
                            "attempt": r["attempt"],
                        }
                        for r in rows
                        if r["status"] == "RUNNING"
                    ],
                    "counts": _status_counts(rows),
                }
                yield f"data: {__import__('json').dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
            await asyncio.sleep(2)

    return StreamingResponse(gen(), media_type="text/event-stream")


def _status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Static UI
# ---------------------------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/graph")
def graph_page() -> FileResponse:
    return FileResponse(WEB_DIR / "graph.html")


@app.get("/tasks")
def tasks_page() -> FileResponse:
    return FileResponse(WEB_DIR / "tasks.html")


@app.get("/task")
def task_page() -> FileResponse:
    return FileResponse(WEB_DIR / "task.html")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


def main() -> None:
    parser = argparse.ArgumentParser(description="Task runner dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    import uvicorn

    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
