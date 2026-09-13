"""Standalone CLI for the final requirement review (p9).

Usage:
    python requirement_closure.py --run-id run-20260912-103000 [--mode opencode]
    python requirement_closure.py --requirement ..\\res.md --project D:\\Workspace\\resource [--mode opencode]
    python requirement_closure.py --list

The watcher also triggers this automatically after `--once` finishes
(config: final_review.enabled).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from config import load_config
from log import get_logger, setup_logging
from runtime.task_store import TaskStore
from workers.requirement_closure import RequirementClosure

logger = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Final requirement review (Requirement Closure)"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-id", help="requirement run id (run-YYYYMMDD-HHMMSS-xxxx)")
    source.add_argument("--requirement", help="locate the run by its requirement file")
    source.add_argument("--list", action="store_true", help="list all runs and exit")
    parser.add_argument(
        "--project",
        help="project ref (id or path); required with --requirement to disambiguate",
    )
    parser.add_argument(
        "--mode", choices=("dry-run", "opencode"), default="dry-run",
        help="review mode (default: dry-run)",
    )
    parser.add_argument(
        "--simulate", choices=("", "partial"), default="",
        help="dry-run only: simulate a PARTIAL first round to exercise the replan loop",
    )
    args = parser.parse_args()

    config = load_config()
    os.environ.setdefault("OPENCODE_URL", config.opencode_url)
    setup_logging()

    # Fail fast when the OpenCode Server is down.
    if args.mode == "opencode":
        from opencode_client import OpenCodeClient

        try:
            OpenCodeClient(base_url=config.opencode_url).health()
        except Exception as exc:
            logger.error(f"OpenCode Server unavailable: {config.opencode_url} ({exc})")
            logger.error("Start it first:  opencode serve --hostname 127.0.0.1 --port 4096")
            raise SystemExit(2)

    task_store = TaskStore()
    closure = RequirementClosure(config, task_store)

    try:
        if args.list:
            _list_runs(task_store)
            return

        run_id = args.run_id
        if args.requirement:
            if not args.project:
                parser.error("--project is required together with --requirement")
            # The run stores the project *id* (not the raw ref), so resolve
            # the way planner did: config ID first, then path → directory name.
            from config import get_project

            try:
                project = get_project(config, args.project)
            except ValueError as exc:
                logger.error(f"Invalid --project: {exc}")
                raise SystemExit(1)
            if project is None:
                logger.error(f"Unknown project: {args.project}")
                raise SystemExit(1)
            run = task_store.find_run_by_requirement(
                str(Path(args.requirement).resolve()), project.id
            )
            if run is None:
                logger.error(
                    f"no requirement run found for {args.requirement} "
                    f"(project={project.id})"
                )
                raise SystemExit(1)
            run_id = run["run_id"]

        if task_store.get_run(run_id) is None:
            logger.error(f"unknown run id: {run_id}")
            raise SystemExit(1)

        try:
            result = closure.review_run(run_id, args.mode, simulate=args.simulate)
        except Exception as exc:
            logger.error(f"final review failed for {run_id}: {exc}")
            logger.error("the run stays retryable — fix the cause and re-run this command")
            raise SystemExit(1)

        logger.info(
            f"[closure] done: status={result['status']} "
            f"coverage={result['coverage_score']:.2f} "
            f"followups={len(result['followup_files'])} report={result['report']}"
        )
        if result["followup_files"]:
            logger.info(
                "[closure] follow-up tasks are queued in prompts/ — "
                "run `python watcher.py --mode opencode --once` to execute them"
            )
    finally:
        task_store.close()


def _list_runs(task_store: TaskStore) -> None:
    import sqlite3

    from config import DB_FILE

    if not DB_FILE.exists():
        print("no runs")
        return
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT run_id, project, status, review_status, round, created_at "
        "FROM requirement_runs ORDER BY created_at DESC"
    ).fetchall()
    if not rows:
        print("no runs")
    for r in rows:
        print(
            f"{r['run_id']}  status={r['status']:<18} "
            f"review={r['review_status'] or '-':<14} round={r['round']} "
            f"project={r['project']}  created={r['created_at'][:19]}"
        )
    conn.close()


if __name__ == "__main__":
    main()
