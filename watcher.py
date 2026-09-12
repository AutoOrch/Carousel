from __future__ import annotations

import argparse
import os
import signal
import sys
import time

from config import PROMPTS_DIR, load_config
from log import get_logger, setup_logging
from runtime.file_lock_manager import FileLockManager
from runtime.recovery_manager import RecoveryManager
from runtime.task_store import TaskStore
from runtime.watcher import claim_task
from runtime.worker_pool import WorkerPool

logger = get_logger(__name__)
_running = True


def _handle_sigint(signum, frame) -> None:
    global _running
    _running = False
    logger.info("[watcher] stopping (waiting for running workers)...")


def _print_projects(config) -> None:
    if not config.projects:
        logger.warning("[watcher] no projects configured in config.yaml")
    else:
        logger.info("[watcher] configured projects:")
        for project in config.projects.values():
            exists = (project.path / ".git").exists()
            mark = "ok" if exists else "MISSING .git"
            logger.info(
                f"  - {project.id}: {project.path} "
                f"(branch={project.default_branch}) [{mark}]"
            )
    logger.info(
        "[watcher] task 'project' front-matter may also be a direct "
        "repository path (absolute or relative)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Folder-watching agent runner")
    parser.add_argument(
        "--mode",
        choices=("dry-run", "opencode"),
        default=None,
        help="execution mode, overrides EXEC_MODE env (default: dry-run)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="process the currently queued prompts once and exit",
    )
    parser.add_argument(
        "--no-recovery",
        action="store_true",
        help="skip startup crash-recovery scan",
    )
    args = parser.parse_args()

    config = load_config()

    if args.mode:
        os.environ["EXEC_MODE"] = args.mode
    os.environ["OPENCODE_URL"] = config.opencode_url
    os.environ["MAX_ATTEMPTS"] = str(config.max_attempts)
    os.environ["BACKOFF_SECONDS"] = str(config.backoff_seconds)
    mode = os.getenv("EXEC_MODE", "dry-run")

    signal.signal(signal.SIGINT, _handle_sigint)

    setup_logging()
    logger.info(
        f"[watcher] mode={mode}  workers={config.max_workers}  "
        f"poll={config.poll_interval}s  max_attempts={config.max_attempts}"
    )
    _print_projects(config)

    # --- SQLite task store + recovery ---
    task_store = TaskStore()
    recovery = RecoveryManager(task_store, config)

    if not args.no_recovery:
        recovered = recovery.recover_stale_tasks()
        if recovered:
            logger.info(f"[watcher] recovered {len(recovered)} stale task(s): {recovered}")
        else:
            logger.info("[watcher] no stale tasks to recover")

    recovery.start_heartbeat()

    logger.info(f"[watcher] drop .md task files into: {PROMPTS_DIR.resolve()}")

    lock_manager = FileLockManager()
    pool = WorkerPool(
        max_workers=config.max_workers,
        lock_manager=lock_manager,
        task_store=task_store,
    )

    try:
        while _running:
            task = claim_task(config, task_store)
            if task:
                paths_info = f"  allowed_paths={task.allowed_paths}" if task.allowed_paths else ""
                logger.info(f"[watcher] claimed {task.id}: {task.title} -> {task.project}{paths_info}")
                pool.submit(task)
                continue
            if args.once:
                break
            time.sleep(config.poll_interval)

        if args.once:
            logger.info("[watcher] waiting for all tasks to finish...")
            pool.join()
    finally:
        recovery.stop_heartbeat()
        pool.shutdown()
        task_store.close()

    logger.info("[watcher] stopped.")


if __name__ == "__main__":
    main()
