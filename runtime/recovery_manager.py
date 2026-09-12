from __future__ import annotations

import shutil
import threading
from pathlib import Path

from config import Config, PROCESSING_DIR, PROMPTS_DIR
from log import get_logger
from runtime.task_store import TaskStore
from worktree import abort_merge

logger = get_logger(__name__)


class RecoveryManager:
    """Handles crash recovery at startup and background heartbeat updates.

    Called before the normal scan loop begins so that stale tasks are
    requeued and interrupted merges are cleaned up.
    """

    def __init__(self, task_store: TaskStore, config: Config) -> None:
        self._store = task_store
        self._config = config
        self._hb_stop = threading.Event()
        self._hb_thread: threading.Thread | None = None

    # ----------------------------------------------------- startup recovery

    def recover_stale_tasks(self) -> list[str]:
        """Move tasks with expired leases back to prompts/ for re-execution.

        Also aborts any interrupted merges left in project repos.
        """
        stale = self._store.get_stale_tasks(self._config.lease_timeout)
        recovered: list[str] = []

        for entry in stale:
            task_id = entry["task_id"]
            prompt_file = Path(entry["prompt_file"])

            # The .md should be in processing/ — move it back to prompts/.
            src = PROCESSING_DIR / prompt_file.name if prompt_file.parent.name == "processing" else prompt_file
            if not src.exists():
                # File might already be gone.
                continue

            dst = PROMPTS_DIR / src.name
            PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(src), str(dst))
            except Exception:
                continue

            self._store.clear_lease(task_id)
            self._store.update_status(task_id, "PENDING")
            recovered.append(task_id)
            logger.info(f"[recovery] requeued stale task: {task_id}")

        self._recover_interrupted_merges()
        return recovered

    def _recover_interrupted_merges(self) -> None:
        """Abort any merge left in-progress by a crash (MERGE_HEAD exists)."""
        for project in self._config.projects.values():
            merge_head = project.path / ".git" / "MERGE_HEAD"
            if merge_head.exists():
                logger.info(f"[recovery] aborting interrupted merge in {project.id}")
                try:
                    abort_merge(project.path)
                except Exception:
                    pass

    # ------------------------------------------------------- heartbeat loop

    def start_heartbeat(self) -> None:
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="heartbeat"
        )
        self._hb_thread.start()

    def stop_heartbeat(self) -> None:
        self._hb_stop.set()
        if self._hb_thread:
            self._hb_thread.join(timeout=5)

    def _heartbeat_loop(self) -> None:
        interval = self._config.heartbeat_interval
        while not self._hb_stop.wait(interval):
            for task in self._store.get_all_running():
                try:
                    self._store.update_heartbeat(task["task_id"])
                except Exception:
                    pass
