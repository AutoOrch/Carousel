from __future__ import annotations

import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from shutil import move
from typing import Any

from config import FAILED_DIR, PROCESSING_DIR, PROMPTS_DIR
from log import get_logger
from runtime.file_lock_manager import FileLockManager
from runtime.task_store import TaskStore
from schemas.task import Task
from task_graph import build_task_graph, set_task_store

logger = get_logger(__name__)


class WorkerPool:
    """Runs one LangGraph task graph per claimed task.

    * Tasks with ``allowed_paths`` are serialised via :class:`FileLockManager`.
    * Tasks with ``depends_on`` are held until all dependencies reach
      ``COMPLETED`` status in the SQLite store.  When a dependency fails
      (or can never run again), dependents are cascade-failed.
    * Each running task gets a lease (fencing token) so crash-recovery can
      detect stale workers.
    """

    def __init__(
        self,
        max_workers: int = 3,
        lock_manager: FileLockManager | None = None,
        task_store: TaskStore | None = None,
    ) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._lock_manager = lock_manager or FileLockManager()
        self._task_store = task_store
        if task_store:
            set_task_store(task_store)
        self._graph = build_task_graph()
        self._pending: list[Task] = []
        self._running = 0
        self._guard = threading.Lock()
        # Idle means: nothing running AND nothing pending.  Cleared whenever
        # a task is submitted or held back, so join() blocks correctly.
        self._idle = threading.Event()
        self._idle.set()
        self._worker_id = f"worker-{uuid.uuid4().hex[:6]}"

    def submit(self, task: Task) -> None:
        with self._guard:
            self._pending.append(task)
            self._dispatch_locked()

    def join(self) -> None:
        """Block until no task is running or pending."""
        self._idle.wait()

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)

    # -- internal ----------------------------------------------------------

    def _dependency_state(self, task: Task) -> str:
        """Classify a task's dependencies.

        Returns one of:
          * ``"met"``         — all dependencies COMPLETED, dispatch now
          * ``"waiting"``     — a dependency is still pending/running, or its
                                task file has not been claimed yet but still
                                exists in prompts/ or processing/
          * ``"cascade_fail"``— a dependency FAILED, or its task is unknown
                                and its file is gone (it can never complete)
        """
        if not task.depends_on or not self._task_store:
            return "met"
        for dep_id in task.depends_on:
            dep = self._task_store.get_task(dep_id)
            if dep is None:
                # Never registered — it may still be claimed later if its
                # file is sitting in the queue or mid-processing.
                for directory in (PROMPTS_DIR, PROCESSING_DIR):
                    if (directory / f"{dep_id}.md").exists():
                        return "waiting"
                return "cascade_fail"
            if dep["status"] == "COMPLETED":
                continue
            if dep["status"] == "FAILED":
                return "cascade_fail"
            return "waiting"
        return "met"

    def _cascade_fail(self, task: Task) -> None:
        """Fail a task because a dependency can never complete."""
        logger.error(
            f"[pool] {task.id} cascade-failed: dependencies "
            f"{task.depends_on} can never complete"
        )
        if self._task_store:
            self._task_store.update_status(task.id, "FAILED")
        _move_to_failed(task.prompt_file)

    def _dispatch_locked(self) -> None:
        still_pending: list[Task] = []
        for task in self._pending:
            dep_state = self._dependency_state(task)
            if dep_state == "cascade_fail":
                self._cascade_fail(task)
                continue
            if dep_state == "waiting":
                logger.info(f"[pool] {task.id} waiting for dependencies: {task.depends_on}")
                still_pending.append(task)
                continue
            if self._lock_manager.acquire(task):
                self._running += 1
                self._idle.clear()

                lease_id = ""
                if self._task_store:
                    lease_id = self._task_store.claim_lease(task.id, self._worker_id)

                logger.info(f"[pool] dispatching {task.id} (lease={lease_id})")
                future = self._executor.submit(self._run, task, lease_id)
                future.add_done_callback(lambda f, t=task: self._on_done(t))
            else:
                logger.info(f"[pool] {task.id} waiting for resources: {task.allowed_paths}")
                still_pending.append(task)
        self._pending = still_pending
        if self._pending:
            # Held-back tasks are not idle — join() must keep waiting.
            self._idle.clear()

    def _on_done(self, task: Task) -> None:
        with self._guard:
            self._lock_manager.release(task)
            self._running -= 1
            self._dispatch_locked()
            if self._running == 0 and not self._pending:
                self._idle.set()

    def _run(self, task: Task, lease_id: str) -> dict[str, Any]:
        config = {"configurable": {"thread_id": task.id}}
        try:
            return self._graph.invoke(
                {"task": task, "_lease_id": lease_id},
                config=config,
            )
        except Exception as exc:
            logger.error(
                f"[pool] {task.id} crashed: {exc}\n{traceback.format_exc()}"
            )
            if self._task_store:
                self._task_store.update_status(task.id, "FAILED")
            # The graph could not finalise the task file — move it to
            # failed/ so it does not stay orphaned in processing/.
            _move_to_failed(task.prompt_file)
            return {"task": task.id, "error": str(exc)}


def _move_to_failed(prompt_file: Path) -> None:
    src = Path(prompt_file)
    if not src.exists():
        return
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    try:
        move(str(src), str(FAILED_DIR / src.name))
    except Exception as exc:
        logger.warning(f"[pool] failed to move {src.name} to failed/: {exc}")
