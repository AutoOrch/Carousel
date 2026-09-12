from __future__ import annotations

import shutil
from pathlib import Path

from config import Config, FAILED_DIR, PROCESSING_DIR, PROMPTS_DIR
from log import get_logger
from runtime.task_resolver import resolve_task
from runtime.task_store import TaskStore
from schemas.task import Task

logger = get_logger(__name__)


def ensure_dirs() -> None:
    for directory in (PROMPTS_DIR, PROCESSING_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def claim_task(config: Config, task_store: TaskStore) -> Task | None:
    """Atomically claim one task by moving it from prompts/ to processing/.

    Registers the task in the SQLite store so its lifecycle (lease,
    heartbeat, checkpoint, attempts) can be tracked independently of the
    filesystem.
    """
    ensure_dirs()
    for candidate in sorted(PROMPTS_DIR.glob("*.md")):
        target = PROCESSING_DIR / candidate.name
        try:
            shutil.move(str(candidate), str(target))
        except Exception:
            continue

        try:
            task = resolve_task(target, config)
        except Exception as exc:
            logger.info(f"[watcher] reject {candidate.name}: {exc}")
            _move_to_failed(target)
            continue

        task_store.create_task(
            task_id=task.id,
            prompt_file=str(target),
            project=task.project,
        )
        return task
    return None


def _move_to_failed(file: Path) -> None:
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(file), str(FAILED_DIR / file.name))
    except Exception:
        pass
