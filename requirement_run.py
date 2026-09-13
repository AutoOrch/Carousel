"""Requirement Run management (p9: one requirement → one run → tasks → review).

A run ties together the original requirement snapshot, the planned tasks,
and the git base revisions each project started from, so the final review
can diff exactly what this requirement produced.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from config import ROOT
from log import get_logger
from runtime.task_store import TaskStore

logger = get_logger(__name__)

RUNS_DIR = ROOT / "runtime" / "requirement_runs"


def new_run_id() -> str:
    return f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{hashlib.sha256(datetime.now().isoformat().encode()).hexdigest()[:4]}"


def requirement_hash(requirement_text: str) -> str:
    return f"sha256:{hashlib.sha256(requirement_text.encode('utf-8')).hexdigest()}"


def _git_head(repo: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def create_run(
    task_store: TaskStore,
    requirement_path: Path,
    requirement_text: str,
    projects: list[dict[str, Any]],
    planner_mode: str,
) -> str:
    """Create a run: DB row + requirement snapshot + plan skeleton.

    ``projects`` entries: {"ref": normalized ref, "id": project id,
    "path": repo path (str), "branch": default branch}.
    """
    run_id = new_run_id()
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot the original requirement — the review baseline.
    (run_dir / "requirement.md").write_text(requirement_text, encoding="utf-8")

    base_revisions = {}
    for p in projects:
        rev = _git_head(Path(p["path"]))
        base_revisions[p["ref"]] = rev
        logger.info(f"[run] {run_id} base revision for {p['id']}: {rev or '(unknown)'}")

    plan = {
        "run_id": run_id,
        "requirement_file": str(requirement_path.resolve()),
        "requirement_hash": requirement_hash(requirement_text),
        "planner_mode": planner_mode,
        "projects": [
            {
                "ref": p["ref"],
                "id": p["id"],
                "path": p["path"],
                "branch": p["branch"],
                "base_revision": base_revisions[p["ref"]],
            }
            for p in projects
        ],
        "tasks": [],
    }
    (run_dir / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    task_store.create_run(
        run_id=run_id,
        requirement_file=str(requirement_path.resolve()),
        requirement_hash=plan["requirement_hash"],
        project=",".join(p["id"] for p in projects),
        base_revision=base_revisions.get(projects[0]["ref"], ""),
        planner_mode=planner_mode,
    )
    logger.info(f"[run] created {run_id} ({len(projects)} project(s))")
    return run_id


def save_plan_tasks(run_id: str, tasks: list[dict[str, Any]]) -> None:
    """Record the planned (renumbered) task ids into plan.json."""
    plan_path = RUNS_DIR / run_id / "plan.json"
    if not plan_path.exists():
        return
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["tasks"] = [
        {
            "id": t["id"],
            "title": t.get("title", ""),
            "project": t.get("project", ""),
            "depends_on": t.get("depends_on", []),
            "allowed_paths": t.get("allowed_paths", []),
        }
        for t in tasks
    ]
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_plan(run_id: str) -> dict[str, Any] | None:
    plan_path = RUNS_DIR / run_id / "plan.json"
    if not plan_path.exists():
        return None
    try:
        return json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"[run] cannot parse {plan_path}: {exc}")
        return None


def load_requirement(run_id: str) -> str | None:
    req_path = RUNS_DIR / run_id / "requirement.md"
    if not req_path.exists():
        return None
    return req_path.read_text(encoding="utf-8")
