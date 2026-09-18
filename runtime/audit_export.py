"""Build self-contained Requirement Run audit packages from SQLite."""
from __future__ import annotations

import json
import sqlite3
import argparse
from pathlib import Path
from typing import Any

from config import DB_FILE
from requirement_run import load_plan, load_requirement


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        return []


def _decode(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        for key in ("payload", "changed_files", "violations", "result", "evidence", "risks"):
            if key in row and isinstance(row[key], str):
                try:
                    row[key] = json.loads(row[key])
                except (TypeError, ValueError):
                    pass
    return rows


def export_run(run_id: str, db_file: Path = DB_FILE) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    try:
        runs = _rows(conn, "SELECT * FROM requirement_runs WHERE run_id=?", (run_id,))
        if not runs:
            raise KeyError(run_id)
        tasks = _rows(conn, "SELECT * FROM tasks WHERE run_id=? ORDER BY created_at", (run_id,))
        task_ids = [row["task_id"] for row in tasks]
        package: dict[str, Any] = {
            "schema_version": 1,
            "run": runs[0],
            "requirement": load_requirement(run_id),
            "plan_snapshot": load_plan(run_id),
            "projects": _rows(conn, "SELECT * FROM run_projects WHERE run_id=? ORDER BY project_id", (run_id,)),
            "plans": _rows(conn, "SELECT * FROM plans WHERE run_id=? ORDER BY round", (run_id,)),
            "tasks": tasks,
            "attempts": [],
            "dependencies": _rows(
                conn,
                """SELECT d.* FROM task_dependencies d
                   JOIN plan_tasks pt ON pt.task_id=d.task_id
                   JOIN plans p ON p.plan_id=pt.plan_id
                   WHERE p.run_id=? ORDER BY d.task_id,d.depends_on_task_id""",
                (run_id,),
            ),
            "events": _decode(_rows(conn, "SELECT * FROM events WHERE run_id=? ORDER BY occurred_at,event_id", (run_id,))),
            "operations": [],
            "changesets": [],
            "commits": [],
            "validations": [],
            "reviews": _rows(conn, "SELECT * FROM requirement_reviews WHERE run_id=? ORDER BY round", (run_id,)),
            "review_items": [],
            "artifacts": _rows(conn, "SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at", (run_id,)),
        }
        for task_id in task_ids:
            args = (task_id,)
            package["attempts"] += _rows(conn, "SELECT * FROM attempts WHERE task_id=? ORDER BY attempt", args)
            package["operations"] += _decode(_rows(conn, "SELECT * FROM operations WHERE task_id=? ORDER BY created_at", args))
            package["changesets"] += _decode(_rows(conn, "SELECT * FROM changesets WHERE task_id=? ORDER BY created_at", args))
            package["commits"] += _rows(conn, "SELECT * FROM commits WHERE task_id=? ORDER BY created_at", args)
            package["validations"] += _decode(_rows(conn, "SELECT * FROM validations WHERE task_id=? ORDER BY created_at", args))
        for review in package["reviews"]:
            package["review_items"] += _decode(_rows(
                conn, "SELECT * FROM review_items WHERE review_id=? ORDER BY requirement_item_id",
                (review["review_id"],),
            ))
        return package
    finally:
        conn.close()


def export_markdown(package: dict[str, Any]) -> str:
    run = package["run"]
    lines = [
        f"# Requirement Run Audit — {run['run_id']}", "",
        f"- Status: {run.get('status')}",
        f"- Review: {run.get('review_status') or '-'}",
        f"- Coverage: {run.get('coverage_score')}",
        f"- Risk: {run.get('risk_level') or '-'}", "",
        "## Projects", "",
        "| Project | Base | Final |", "|---|---|---|",
    ]
    for project in package["projects"]:
        lines.append(
            f"| {project['project_id']} | {(project.get('base_revision') or '')[:12]} | "
            f"{(project.get('final_revision') or '')[:12]} |"
        )
    lines += ["", "## Tasks", "", "| Task | Status | Attempts | Commit | Merge |", "|---|---|---:|---|---|"]
    for task in package["tasks"]:
        lines.append(
            f"| {task['task_id']} | {task['status']} | {task.get('attempt', 0)} | "
            f"{(task.get('commit_sha') or '')[:12]} | {(task.get('merge_commit_sha') or '')[:12]} |"
        )
    lines += [
        "", "## Evidence counts", "",
        f"- Attempts: {len(package['attempts'])}",
        f"- Events: {len(package['events'])}",
        f"- Operations: {len(package['operations'])}",
        f"- Validations: {len(package['validations'])}",
        f"- Change sets: {len(package['changesets'])}",
        f"- Commits: {len(package['commits'])}",
        f"- Reviews: {len(package['reviews'])}",
        f"- Artifacts: {len(package['artifacts'])}",
        "", "## Requirement snapshot", "", package.get("requirement") or "(missing)", "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a Requirement Run audit package")
    parser.add_argument("run_id")
    parser.add_argument("--format", choices=("json", "md"), default="json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        package = export_run(args.run_id)
    except KeyError:
        raise SystemExit(f"run not found: {args.run_id}")
    text = (json.dumps(package, ensure_ascii=False, indent=2)
            if args.format == "json" else export_markdown(package))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        print(args.output)
    else:
        print(text)


if __name__ == "__main__":
    main()
