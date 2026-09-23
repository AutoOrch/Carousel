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

from runtime.assets import project_requirement_runs_dir, requirement_runs_dir
from config import config_version
from log import get_logger
from runtime.task_store import TaskStore

logger = get_logger(__name__)

RUNS_DIR = requirement_runs_dir()


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
    requirement_path: Path | None,
    requirement_text: str,
    projects: list[dict[str, Any]],
    planner_mode: str,
) -> str:
    """Create a run: DB row + requirement snapshot + plan skeleton.

    ``projects`` entries: {"ref": normalized ref, "id": project id,
    "path": repo path (str), "branch": default branch}.

    ``requirement_path`` may be None when the requirement came from an
    inline ``--prompt`` string instead of a file; ``(inline prompt)`` is
    used as the sentinel path in that case.
    """
    run_id = new_run_id()
    run_dir = RUNS_DIR / run_id

    req_file_str = str(requirement_path.resolve()) if requirement_path else "(inline prompt)"

    base_revisions = {}
    for p in projects:
        rev = _git_head(Path(p["path"]))
        base_revisions[p["ref"]] = rev
        logger.info(f"[run] {run_id} base revision for {p['id']}: {rev or '(unknown)'}")

    plan = {
        "run_id": run_id,
        "requirement_file": req_file_str,
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
    req_hash = plan["requirement_hash"]
    requirement_id = task_store.register_requirement(
        req_hash,
        req_file_str,
        str((run_dir / "requirement.md").resolve()),
    )
    task_store.create_run(
        run_id=run_id,
        requirement_file=req_file_str,
        requirement_hash=req_hash,
        project=",".join(p["id"] for p in projects),
        base_revision=base_revisions.get(projects[0]["ref"], ""),
        planner_mode=planner_mode,
        config_hash=config_version(),
        requirement_id=requirement_id,
    )
    task_store.register_run_projects(run_id, plan["projects"])
    task_store.append_event(
        "run.created", run_id=run_id, status="PLANNING",
        payload={"requirement_hash": plan["requirement_hash"],
                 "projects": [p["id"] for p in projects]},
    )
    task_store.update_run(run_id, status="PLANNING")
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
        requirement_tmp = run_dir / "requirement.md.tmp"
        plan_tmp = run_dir / "plan.json.tmp"
        requirement_tmp.write_text(requirement_text, encoding="utf-8")
        plan_tmp.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        requirement_tmp.replace(run_dir / "requirement.md")
        plan_tmp.replace(run_dir / "plan.json")
        task_store.register_artifact(
            "REQUIREMENT_SNAPSHOT", str(run_dir / "requirement.md"), run_id=run_id,
            content_hash=req_hash,
        )
        task_store.register_artifact(
            "PLAN_SNAPSHOT", str(run_dir / "plan.json"), run_id=run_id,
        )
        for project in plan["projects"]:
            pointer_dir = project_requirement_runs_dir(project["id"])
            pointer_dir.mkdir(parents=True, exist_ok=True)
            pointer = pointer_dir / f"{run_id}.json"
            pointer.write_text(json.dumps({
                "run_id": run_id,
                "canonical_path": str(run_dir),
                "requirement_hash": req_hash,
                "base_revision": project["base_revision"],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            task_store.register_artifact(
                "PROJECT_RUN_POINTER", str(pointer), run_id=run_id,
            )
    except Exception as exc:
        task_store.update_run(run_id, status="PLANNING_FAILED")
        task_store.append_event(
            "run.snapshot.failed", run_id=run_id, status="PLANNING_FAILED",
            payload={"error": str(exc)[:2000]},
        )
        raise
    logger.info(f"[run] created {run_id} ({len(projects)} project(s))")
    return run_id


def save_plan_tasks(
    run_id: str, tasks: list[dict[str, Any]], task_store: TaskStore | None = None,
    round_: int = 1, source_review_id: str = "",
) -> None:
    """Record the planned (renumbered) task ids into plan.json."""
    plan_path = RUNS_DIR / run_id / ("plan.json" if round_ == 1 else f"plan-r{round_}.json")
    if round_ > 1 and not plan_path.exists():
        base_path = RUNS_DIR / run_id / "plan.json"
        if base_path.exists():
            plan_path.write_text(base_path.read_text(encoding="utf-8"), encoding="utf-8")
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
    plan["round"] = round_
    if source_review_id:
        plan["source_review_id"] = source_review_id
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if task_store is not None:
        task_store.record_plan(
            run_id, round_, str(plan_path.resolve()), tasks,
            source_review_id=source_review_id,
        )


def load_plan(run_id: str) -> dict[str, Any] | None:
    plan_path = RUNS_DIR / run_id / "plan.json"
    if not plan_path.exists():
        plan_path = Path(__file__).resolve().parent / "runtime" / "requirement_runs" / run_id / "plan.json"
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
        req_path = Path(__file__).resolve().parent / "runtime" / "requirement_runs" / run_id / "requirement.md"
    if not req_path.exists():
        return None
    return req_path.read_text(encoding="utf-8")
