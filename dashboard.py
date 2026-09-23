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
import hmac
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import (
    DATA_ROOT,
    DB_FILE,
    FAILED_DIR,
    PROCESSED_DIR,
    PROCESSING_DIR,
    PROMPTS_DIR,
    REPORTS_DIR,
    ROOT,
    load_config,
)
from architecture.repository import ArchitectureRepository
from runtime.task_resolver import parse_markdown
from runtime.assets import (
    PROJECTS_ROOT,
    architecture_dir,
    documents_dir,
    requirement_runs_dir,
    task_report_path,
)

app = FastAPI(title="LangGraph + OpenCode runner dashboard")


@app.middleware("http")
async def _dashboard_auth(request: Request, call_next):
    token = os.getenv("DASHBOARD_TOKEN", "")
    if token:
        supplied = request.headers.get("authorization", "")
        if not supplied.startswith("Bearer ") or not hmac.compare_digest(
            supplied[7:], token
        ):
            return JSONResponse({"detail": "valid bearer token required"}, status_code=401)
    return await call_next(request)

WEB_DIR = ROOT / "web"
LOG_FILE = DATA_ROOT / "logs" / "agent.log"

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


def _query_db(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    if not DB_FILE.exists():
        return []
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _decode_event(row: dict[str, Any]) -> dict[str, Any]:
    try:
        row["payload"] = json.loads(row.get("payload") or "{}")
    except (TypeError, json.JSONDecodeError):
        pass
    return row


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
        if age > load_config().lease_timeout:
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
        canonical = (db or {}).get("project") or _canonical_project(raw_ref)
        entry: dict[str, Any] = {
            "id": task_id,
            "title": fm["title"] if fm else task_id,
            "project": canonical,
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

class RequirementSubmitRequest(BaseModel):
    prompt: str
    project: str
    mode: str = "dry-run"
    expand: bool = False


@app.post("/api/requirement/submit")
def api_submit_requirement(req: RequirementSubmitRequest) -> dict[str, Any]:
    """Submit a one-liner prompt + project → create a requirement run (p15).

    Writes task .md files into prompts/ via the same planner logic as the CLI.
    The watcher remains the sole DB writer — this endpoint only produces files.
    """
    if not req.prompt.strip():
        raise HTTPException(400, "prompt must not be empty")
    if not req.project.strip():
        raise HTTPException(400, "project must not be empty")
    if req.mode not in ("dry-run", "opencode"):
        raise HTTPException(400, "mode must be dry-run or opencode")

    config = load_config()

    from config import get_project as _get_project
    from planner import plan as _plan, validate_plan as _validate, write_tasks as _write, generate_requirement as _gen_req
    from runtime.task_store import TaskStore
    import requirement_run

    projects: list[tuple[str, Any, str]] = []
    for raw_ref in [p.strip() for p in req.project.split(",") if p.strip()]:
        project = _get_project(config, raw_ref)
        if project is None:
            raise HTTPException(400, f"unknown project: {raw_ref}")
        projects.append((raw_ref, project, raw_ref.replace("\\", "/")))

    requirement = req.prompt
    if req.expand and req.mode == "opencode":
        try:
            requirement = _gen_req(
                req.prompt, projects[0][1],
                opencode_url=config.opencode_url,
                agent=config.planner.requirement_generator_agent,
                model=config.planner.requirement_generator_model,
            )
        except Exception as exc:
            raise HTTPException(500, f"requirement expansion failed: {exc}")

    task_store = TaskStore()
    try:
        run_id = requirement_run.create_run(
            task_store, None, requirement,
            projects=[
                {"ref": norm, "id": p.id, "path": str(p.path),
                 "branch": p.default_branch}
                for _, p, norm in projects
            ],
            planner_mode=req.mode,
        )
        tasks = _validate(
            _plan(requirement, projects, mode=req.mode, opencode_url=config.opencode_url),
            allowed_projects={item[2] for item in projects},
        )
        written = _write(tasks, projects[0][2], run_id=run_id)
        requirement_run.save_plan_tasks(run_id, tasks, task_store=task_store)
        task_store.update_run(run_id, status="QUEUED")
    except Exception as exc:
        task_store.close()
        raise HTTPException(500, f"planning failed: {exc}")
    finally:
        task_store.close()

    return {
        "run_id": run_id,
        "tasks_written": len(written),
        "requirement_length": len(requirement),
        "expanded": req.expand and req.mode == "opencode",
    }


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


@app.get("/api/metrics")
def api_metrics() -> dict[str, Any]:
    status_rows = _query_db("SELECT status,COUNT(*) count FROM tasks GROUP BY status")
    attempts = _query_db(
        "SELECT status,COUNT(*) count FROM attempts GROUP BY status"
    )
    failures = _query_db(
        """SELECT COALESCE(failure_type,'') failure_type,COUNT(*) count
           FROM attempts WHERE status='FAILED' GROUP BY failure_type"""
    )
    operations = _query_db(
        "SELECT kind,status,COUNT(*) count FROM operations GROUP BY kind,status"
    )
    return {
        "generated_at": _now_iso(),
        "tasks": {row["status"]: row["count"] for row in status_rows},
        "attempts": {row["status"]: row["count"] for row in attempts},
        "failures": {row["failure_type"] or "UNKNOWN": row["count"] for row in failures},
        "operations": operations,
        "blocked": _query_db(
            "SELECT task_id,project,block_reason,updated_at FROM tasks WHERE status='BLOCKED'"
        ),
        "active_leases": _query_db(
            "SELECT task_id,worker_id,lease_id,heartbeat_at FROM tasks WHERE status='RUNNING'"
        ),
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
    detail["events"] = [
        _decode_event(r) for r in _query_db(
            "SELECT * FROM events WHERE task_id=? ORDER BY occurred_at,event_id",
            (task_id,),
        )
    ]
    detail["validations"] = _query_db(
        "SELECT * FROM validations WHERE task_id=? ORDER BY created_at",
        (task_id,),
    )
    detail["operations"] = _query_db(
        "SELECT * FROM operations WHERE task_id=? ORDER BY created_at", (task_id,)
    )
    detail["changesets"] = _query_db(
        "SELECT * FROM changesets WHERE task_id=? ORDER BY created_at", (task_id,)
    )
    detail["commits"] = _query_db(
        "SELECT * FROM commits WHERE task_id=? ORDER BY created_at", (task_id,)
    )
    detail["artifacts"] = _query_db(
        "SELECT * FROM artifacts WHERE task_id=? ORDER BY created_at", (task_id,)
    )
    return detail


@app.get("/api/runs")
def api_runs() -> list[dict[str, Any]]:
    rows = _query_db(
        "SELECT * FROM requirement_runs ORDER BY created_at DESC"
    )
    for row in rows:
        row["projects"] = _query_db(
            "SELECT * FROM run_projects WHERE run_id=? ORDER BY project_id",
            (row["run_id"],),
        )
        row["task_counts"] = {
            x["status"]: x["count"] for x in _query_db(
                "SELECT status,COUNT(*) count FROM tasks WHERE run_id=? GROUP BY status",
                (row["run_id"],),
            )
        }
    return rows


@app.get("/api/runs/{run_id}")
def api_run(run_id: str) -> dict[str, Any]:
    rows = _query_db("SELECT * FROM requirement_runs WHERE run_id=?", (run_id,))
    if not rows:
        raise HTTPException(404, "run not found")
    run = rows[0]
    run["projects"] = _query_db(
        "SELECT * FROM run_projects WHERE run_id=? ORDER BY project_id", (run_id,)
    )
    run["tasks"] = _query_db(
        "SELECT * FROM tasks WHERE run_id=? ORDER BY created_at", (run_id,)
    )
    run["reviews"] = _query_db(
        "SELECT * FROM requirement_reviews WHERE run_id=? ORDER BY round", (run_id,)
    )
    run["plans"] = _query_db(
        "SELECT * FROM plans WHERE run_id=? ORDER BY round", (run_id,)
    )
    for plan in run["plans"]:
        plan["tasks"] = _query_db(
            "SELECT * FROM plan_tasks WHERE plan_id=? ORDER BY ordinal", (plan["plan_id"],)
        )
    for review in run["reviews"]:
        review["items"] = _query_db(
            "SELECT * FROM review_items WHERE review_id=? ORDER BY requirement_item_id",
            (review["review_id"],),
        )
    run["artifacts"] = _query_db(
        "SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at", (run_id,)
    )
    run["events"] = [
        _decode_event(r) for r in _query_db(
            "SELECT * FROM events WHERE run_id=? ORDER BY occurred_at,event_id", (run_id,)
        )
    ]
    run_dir = requirement_runs_dir() / run_id
    for name in ("requirement.md", "plan.json"):
        path = run_dir / name
        if path.is_file():
            run[name.replace(".", "_")] = path.read_text(
                encoding="utf-8-sig", errors="replace"
            )
    return run


@app.get("/api/runs/{run_id}/export")
def api_run_export(run_id: str, format: str = "json"):
    from runtime.audit_export import export_markdown, export_run
    try:
        package = export_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")
    if format == "json":
        return package
    if format in ("md", "markdown"):
        return PlainTextResponse(
            export_markdown(package), media_type="text/markdown; charset=utf-8"
        )
    raise HTTPException(400, "format must be json or md")


@app.get("/api/document-runs")
def api_document_runs() -> list[dict[str, Any]]:
    rows = _query_db("SELECT * FROM document_runs ORDER BY created_at DESC")
    for row in rows:
        try:
            row["stats"] = json.loads(row.get("stats") or "{}")
        except json.JSONDecodeError:
            pass
    return rows


@app.get("/api/document-runs/{run_id}")
def api_document_run(run_id: str) -> dict[str, Any]:
    rows = _query_db("SELECT * FROM document_runs WHERE run_id=?", (run_id,))
    if not rows:
        raise HTTPException(404, "document run not found")
    result = rows[0]
    result["items"] = _query_db(
        "SELECT * FROM document_items WHERE run_id=? ORDER BY source_name", (run_id,)
    )
    result["events"] = [
        _decode_event(r) for r in _query_db(
            "SELECT * FROM events WHERE run_id=? ORDER BY occurred_at,event_id", (run_id,)
        )
    ]
    return result


@app.get("/api/document-imports")
def api_document_imports() -> list[dict[str, Any]]:
    rows = _query_db(
        "SELECT * FROM document_import_runs ORDER BY created_at DESC"
    )
    for row in rows:
        for key in ("effective_config", "stats"):
            try:
                row[key] = json.loads(row.get(key) or "{}")
            except (TypeError, json.JSONDecodeError):
                pass
    return rows


@app.get("/api/document-imports/{run_id}")
def api_document_import(run_id: str) -> dict[str, Any]:
    rows = _query_db(
        "SELECT * FROM document_import_runs WHERE import_run_id=?", (run_id,)
    )
    if not rows:
        raise HTTPException(404, "document import run not found")
    result = rows[0]
    for key in ("effective_config", "stats"):
        try:
            result[key] = json.loads(result.get(key) or "{}")
        except (TypeError, json.JSONDecodeError):
            pass
    for table, key, order in (
        ("document_source_roots", "sources", "source_root_id"),
        ("document_source_files", "items", "relative_path"),
        ("document_claims", "claims", "source_file_id,ordinal"),
        ("document_evidence", "evidence", "claim_id,file_path,line_number"),
        ("document_validations_p13", "validations", "claim_id,project_id"),
        ("document_repairs", "repairs", "source_file_id"),
        ("document_relations", "relations", "source_document_id"),
        ("document_clusters", "clusters", "project_id,topic"),
        ("document_summaries", "summaries", "project_id,topic"),
    ):
        result[key] = _query_db(
            f"SELECT * FROM {table} WHERE import_run_id=? ORDER BY {order}",
            (run_id,),
        )
    for row in result["sources"]:
        for field in ("project_refs", "actions"):
            try:
                row[field] = json.loads(row.get(field) or ("[]" if field == "project_refs" else "{}"))
            except (TypeError, json.JSONDecodeError):
                pass
    for row in result["claims"]:
        try:
            row["project_ids"] = json.loads(row.get("project_ids") or "[]")
        except (TypeError, json.JSONDecodeError):
            pass
    for row in result["summaries"]:
        for field, fallback in (("source_document_ids", "[]"), ("validation_revisions", "{}")):
            try:
                row[field] = json.loads(row.get(field) or fallback)
            except (TypeError, json.JSONDecodeError):
                pass
    result["events"] = [
        _decode_event(row) for row in _query_db(
            "SELECT * FROM events WHERE run_id=? ORDER BY occurred_at,event_id",
            (run_id,),
        )
    ]
    return result


@app.get("/api/tasks/{task_id}/report")
def api_task_report(task_id: str) -> dict[str, str]:
    rows = _query_db("SELECT project FROM tasks WHERE task_id=?", (task_id,))
    path = task_report_path(rows[0]["project"], task_id) if rows else REPORTS_DIR / f"{task_id}.md"
    if not path.exists():
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
    # document-only projects (p14): archives written under PROJECTS_ROOT by the
    # pipelines for stable ids that no longer appear in config.yaml or tasks
    known = {p["id"] for p in projects}
    if PROJECTS_ROOT.is_dir():
        for child in sorted(PROJECTS_ROOT.iterdir()):
            pid = child.name
            if pid in known or pid.startswith("_"):
                continue
            if (documents_dir(pid) / "MANIFEST.json").is_file():
                projects.append({"id": pid, "path": "", "branch": "?", "tasks": []})
                known.add(pid)
    # architecture availability for every project (p10 §22)
    for p in projects:
        p["architecture"] = _architecture_summary(p["id"])
        p["current_revision"] = _git_head(p["path"])
        manifest = documents_dir(p["id"]) / "MANIFEST.json"
        p["document_manifest"] = str(manifest) if manifest.is_file() else ""
        if manifest.is_file():
            try:
                p["document_count"] = len(
                    (json.loads(manifest.read_text(encoding="utf-8")).get("documents") or [])
                )
            except (OSError, ValueError):
                p["document_count"] = 0
        else:
            p["document_count"] = 0
    return projects


@app.get("/api/projects/{project_id}")
def api_project(project_id: str) -> dict[str, Any]:
    project = next((item for item in api_projects() if item["id"] == project_id), None)
    if project is None:
        raise HTTPException(404, "project not found")
    project["task_rows"] = _query_db(
        "SELECT * FROM tasks WHERE project=? ORDER BY created_at DESC", (project_id,)
    )
    project["snapshots"] = _query_db(
        "SELECT * FROM architecture_snapshots WHERE project_id=? ORDER BY updated_at DESC",
        (project_id,),
    )
    project["documents"] = _query_db(
        "SELECT * FROM document_items WHERE project_id=? ORDER BY updated_at DESC",
        (project_id,),
    )
    project["imported_documents"] = _query_db(
        "SELECT * FROM document_source_files WHERE project_id=? ORDER BY updated_at DESC",
        (project_id,),
    )
    project["document_summaries"] = _query_db(
        "SELECT * FROM document_summaries WHERE project_id=? ORDER BY updated_at DESC",
        (project_id,),
    )
    return project


# --------------------------------------------------------------------------
# Document library assets (p14)
# --------------------------------------------------------------------------
TEXT_DOC_SUFFIXES = {".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".csv", ".log"}


def _project_documents(project_id: str) -> list[dict[str, Any]]:
    """MANIFEST.json entries for a project — the pipeline-written whitelist
    the file endpoint below resolves against (p14 §5: no user-supplied paths)."""
    try:
        manifest = documents_dir(project_id) / "MANIFEST.json"
    except ValueError:
        raise HTTPException(400, "unsafe project id")
    if not manifest.is_file():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [d for d in (data.get("documents") or []) if isinstance(d, dict)]


@app.get("/api/projects/{project_id}/documents")
def api_project_documents(project_id: str) -> dict[str, Any]:
    documents = _project_documents(project_id)
    categories: dict[str, int] = {}
    for entry in documents:
        category = str(entry.get("type") or "archive")
        categories[category] = categories.get(category, 0) + 1
    return {
        "project_id": project_id,
        "documents": documents,
        "categories": categories,
        "summaries": _query_db(
            "SELECT * FROM document_summaries WHERE project_id=? ORDER BY updated_at DESC",
            (project_id,),
        ),
    }


@app.get("/api/documents/{project_id}/{ref}")
def api_document_file(project_id: str, ref: str):
    """One archived document, addressed by manifest id or sha256 prefix."""
    entry = next(
        (d for d in _project_documents(project_id)
         if str(d.get("id")) == ref or str(d.get("sha256", "")).startswith(ref)),
        None,
    )
    if entry is None or not entry.get("path"):
        raise HTTPException(404, "document not found in manifest")
    doc_dir = documents_dir(project_id).resolve()
    try:
        target = (doc_dir / str(entry["path"])).resolve()
        target.relative_to(doc_dir)
    except ValueError:
        raise HTTPException(403, "document outside project directory")
    if not target.is_file():
        raise HTTPException(404, "document file missing")
    if target.suffix.lower() in TEXT_DOC_SUFFIXES:
        return {
            "project_id": project_id,
            "document": entry,
            "markdown": target.read_text(encoding="utf-8-sig", errors="replace"),
        }
    return FileResponse(target)


# --------------------------------------------------------------------------
# Architecture assets (p10)
# --------------------------------------------------------------------------
def _architecture_summary(project_id: str) -> dict[str, Any]:
    config = load_config()
    repo = ArchitectureRepository(config.architecture)
    snapshots = repo.list_snapshots(project_id)
    if not snapshots:
        return {"available": False}
    current_id = repo.current_snapshot_id(project_id)
    latest = next(
        (s for s in snapshots if s.id == current_id), snapshots[0]
    )

    # p10 §26.3: the architecture is only valid for the revision it was
    # generated from — flag when the repo has moved on.
    current_revision = _git_head(latest.repository_path)
    stale = bool(
        current_revision
        and current_revision != latest.repository_revision
    )

    summary = {
        "available": True,
        "status": latest.status,
        "latest_snapshot": latest.id,
        "revision": latest.repository_revision[:12],
        "current_revision": current_revision[:12],
        "stale": stale,
        "diagram_type": latest.diagram_type,
        "html_url": f"/api/architectures/{project_id}/{latest.id}/html",
        "json_url": f"/api/architectures/{project_id}/{latest.id}/json",
        "created_at": latest.updated_at,
    }
    # SVG is only advertised when the asset actually exists (the archify
    # CLI renders HTML with inline SVG by default).
    if latest.svg_path and Path(latest.svg_path).is_file():
        summary["svg_url"] = f"/api/architectures/{project_id}/{latest.id}/svg"
    return summary


def _git_head(repo_path: str) -> str:
    import subprocess

    if not repo_path:
        return ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _resolve_architecture_asset(
    project_id: str, snapshot_id: str, asset: str
) -> Path:
    """Map (project, snapshot, asset) onto a registered file — DB/config
    driven, with an explicit containment check (p10 §15: no path traversal,
    no user-supplied filesystem paths)."""
    if asset not in ("json", "html", "svg", "metadata"):
        raise HTTPException(404, "unknown asset type")

    config = load_config()
    repo = ArchitectureRepository(config.architecture)
    snap = repo.load_snapshot(project_id, snapshot_id)
    if snap is None:
        raise HTTPException(404, "snapshot not found")

    attr = {"json": snap.json_path, "html": snap.html_path,
            "svg": snap.svg_path, "metadata": snap.metadata_path}[asset]
    if not attr:
        raise HTTPException(404, f"snapshot has no {asset} asset")

    asset_path = Path(attr).resolve()
    arch_root = PROJECTS_ROOT.resolve()
    try:
        asset_path.relative_to(arch_root)
    except ValueError:
        raise HTTPException(403, "asset outside architecture root")
    if not asset_path.is_file():
        raise HTTPException(404, f"{asset} file missing")
    return asset_path


@app.get("/api/architectures")
def api_architectures() -> list[dict[str, Any]]:
    config = load_config()
    repo = ArchitectureRepository(config.architecture)
    projects = config.projects or {}
    ids = {p.id for p in projects.values()}
    # include projects that only exist as asset dirs
    proot = PROJECTS_ROOT
    if proot.exists():
        ids |= {
            d.name for d in proot.iterdir()
            if d.is_dir() and architecture_dir(d.name).exists()
        }
    return [
        {"project_id": pid, **_architecture_summary(pid)}
        for pid in sorted(ids)
    ]


@app.get("/api/architectures/{project_id}")
def api_project_architectures(project_id: str) -> dict[str, Any]:
    config = load_config()
    repo = ArchitectureRepository(config.architecture)
    snapshots = repo.list_snapshots(project_id)
    latest = None
    if snapshots:
        current = repo.current_snapshot_id(project_id)
        latest = next(
            (x for x in snapshots if x.id == current), snapshots[0]
        )
    return {
        "project_id": project_id,
        "available": bool(snapshots),
        "latest": (
            {
                "snapshot_id": latest.id,
                "diagram_type": latest.diagram_type,
                "revision": latest.repository_revision[:12],
                "branch": latest.branch,
                "status": latest.status,
                "source": latest.source,
                "html_url": f"/api/architectures/{project_id}/{latest.id}/html",
                "json_url": f"/api/architectures/{project_id}/{latest.id}/json",
                "created_at": latest.updated_at,
            }
            if latest else None
        ),
        "snapshots": [
            {
                "snapshot_id": s.id,
                "revision": s.repository_revision[:12],
                "status": s.status,
                "created_at": s.updated_at,
            }
            for s in snapshots
        ],
    }


@app.get("/api/architectures/{project_id}/{snapshot_id}/json")
def api_architecture_json(project_id: str, snapshot_id: str) -> FileResponse:
    return FileResponse(_resolve_architecture_asset(project_id, snapshot_id, "json"))


@app.get("/api/architectures/{project_id}/{snapshot_id}/html")
def api_architecture_html(project_id: str, snapshot_id: str) -> FileResponse:
    return FileResponse(
        _resolve_architecture_asset(project_id, snapshot_id, "html"),
        media_type="text/html",
    )


@app.get("/api/architectures/{project_id}/{snapshot_id}/svg")
def api_architecture_svg(project_id: str, snapshot_id: str) -> FileResponse:
    return FileResponse(
        _resolve_architecture_asset(project_id, snapshot_id, "svg"),
        media_type="image/svg+xml",
    )


@app.get("/api/events/stream")
async def api_events_stream() -> StreamingResponse:
    """SSE stream: pushes a snapshot digest whenever the tasks table changes."""

    async def gen() -> AsyncIterator[bytes]:
        last_digest: str | None = None
        while True:
            rows = _read_db()
            event_mark = _query_db(
                "SELECT COUNT(*) count, MAX(occurred_at) latest FROM events"
            )
            digest = ";".join(
                f"{r['task_id']}:{r['status']}:{r['checkpoint']}:{r['attempt']}:{r['updated_at']}"
                for r in rows
            ) + ";events=" + json.dumps(event_mark, sort_keys=True)
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
                    "latest_events": [
                        _decode_event(r) for r in _query_db(
                            "SELECT * FROM events ORDER BY occurred_at DESC,event_id DESC LIMIT 20"
                        )
                    ],
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


@app.get("/runs")
def runs_page() -> FileResponse:
    return FileResponse(WEB_DIR / "runs.html")


@app.get("/task")
def task_page() -> FileResponse:
    return FileResponse(WEB_DIR / "task.html")


@app.get("/operations")
def operations_page() -> FileResponse:
    return FileResponse(WEB_DIR / "operations.html")


@app.get("/documents")
def documents_page() -> FileResponse:
    return FileResponse(WEB_DIR / "documents.html")


@app.get("/project-docs")
def project_docs_page() -> FileResponse:
    return FileResponse(WEB_DIR / "project_docs.html")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


def main() -> None:
    parser = argparse.ArgumentParser(description="Task runner dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--token", default=os.getenv("DASHBOARD_TOKEN", ""),
        help="Bearer token; required when binding outside localhost",
    )
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
        raise SystemExit("--token is required when dashboard binds outside localhost")
    if args.token:
        os.environ["DASHBOARD_TOKEN"] = args.token

    import uvicorn

    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
