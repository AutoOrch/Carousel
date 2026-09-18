"""Document pipeline entry (p11): doc/ → classify → archive → index → report.

Usage:
    python document_run.py --mode dry-run          # rules only, no OpenCode
    python document_run.py --mode opencode         # LLM for ambiguous docs
    python document_run.py --mode dry-run --watch  # keep watching doc/
    python document_run.py --project new-api       # force the project for
                                                   # every document this run
"""

from __future__ import annotations

import argparse
import os
import signal
import time
from typing import Any

from config import get_project, load_config
from document import executor, scanner
from document_graph import build_document_graph
from log import get_logger, setup_logging
from opencode_client import OpenCodeClient
from runtime.assets import migrate_legacy_assets
from runtime.task_store import TaskStore
from runtime.singleton import SingletonLock
from config import DATA_ROOT

logger = get_logger(__name__)
_running = True


def _handle_sigint(signum, frame) -> None:
    global _running
    _running = False
    logger.info("[document] stopping...")


def _summarise(state: dict[str, Any]) -> None:
    stats = state.get("stats") or {}
    if not stats.get("scanned"):
        logger.info("[document] no documents waiting in the input directory")
        return
    logger.info(
        "[document] done — scanned={scanned} archived={archived} "
        "duplicate={duplicate} review={review} failed={failed}".format(
            scanned=stats.get("scanned", 0),
            archived=stats.get("archived", 0),
            duplicate=stats.get("duplicate", 0),
            review=stats.get("review", 0),
            failed=stats.get("failed", 0),
        )
    )
    if state.get("report_path"):
        logger.info(f"[document] report: {state['report_path']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Document organize pipeline (p11)"
    )
    parser.add_argument(
        "--mode",
        choices=("dry-run", "opencode"),
        default="dry-run",
        help="dry-run uses rules only; opencode adds LLM classification",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="keep polling the input directory instead of exiting",
    )
    parser.add_argument(
        "--no-recovery",
        action="store_true",
        help="skip recovering stranded files from processing/documents/",
    )
    parser.add_argument(
        "--project",
        default="",
        help="force this project (config.yaml ID or repo path) for every "
        "document this run — skips project identification entirely",
    )
    args = parser.parse_args()

    config = load_config()
    os.environ["OPENCODE_URL"] = config.opencode_url
    setup_logging()
    signal.signal(signal.SIGINT, _handle_sigint)

    forced_project = ""
    if args.project:
        try:
            project = get_project(config, args.project)
        except ValueError as exc:
            logger.error(f"Invalid --project: {exc}")
            raise SystemExit(1) from exc
        if project is None:
            logger.error(
                f"Unknown project '{args.project}' — not in config.yaml and "
                "not an existing path"
            )
            raise SystemExit(1)
        forced_project = project.id
        logger.info(
            f"[document] --project {args.project}: all documents forced to "
            f"project '{forced_project}' (confidence 1.0)"
        )

    docs_cfg = config.documents
    logger.info(
        f"[document] mode={args.mode} input={docs_cfg.input_dir} "
        f"archive_root={docs_cfg.archive_root} "
        f"threshold={docs_cfg.confidence_threshold}"
    )

    if args.mode == "opencode":
        try:
            health = OpenCodeClient().health()
            logger.info(f"[document] OpenCode health: {health}")
        except Exception as exc:
            logger.error(f"OpenCode Server unavailable: {exc}")
            raise SystemExit(2) from exc

    singleton = SingletonLock(DATA_ROOT / "runtime" / "document-watcher.lock")
    singleton.acquire()
    task_store = TaskStore()

    try:
        migrated = migrate_legacy_assets(
            [], sorted(set(config.projects) | ({forced_project} if forced_project else set()))
        )
        if migrated:
            logger.info(
                f"[document] migrated {len(migrated)} legacy asset path(s)"
            )
        if not args.no_recovery:
            failed_runs = task_store.fail_running_document_runs(
                "document runner restarted before terminal state"
            )
            journal_stats = executor.reconcile_journals(docs_cfg)
            if failed_runs or any(journal_stats.values()):
                logger.info(
                    f"[document] recovered runs={failed_runs}, journals={journal_stats}"
                )
            recovered = scanner.recover_processing(docs_cfg)
            if recovered:
                logger.info(
                    f"[document] recovered {recovered} document(s) from processing/"
                )
        graph = build_document_graph()
        while _running:
            try:
                state = graph.invoke(
                    {
                        "config": config,
                        "mode": args.mode,
                        "opencode_url": config.opencode_url,
                        "forced_project": forced_project,
                        "task_store": task_store,
                    }
                )
            except Exception as exc:
                task_store.fail_running_document_runs(str(exc))
                raise
            _summarise(state)
            if not args.watch:
                break
            time.sleep(config.poll_interval)
    finally:
        task_store.close()
        singleton.release()
    logger.info("[document] stopped.")


if __name__ == "__main__":
    main()
