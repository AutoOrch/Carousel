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
from schemas.task import TASK_TYPE_ARCHITECTURE_INIT, Task
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
    * While an ``ARCHITECTURE_INIT`` task for a project is pending or
      running, CODE_CHANGE tasks of the same project are held back (p10
      §17 — architecture analysis reads the repo and must not race with
      code modifications).
    """

    def __init__(
        self,
        max_workers: int = 3,
        lock_manager: FileLockManager | None = None,
        task_store: TaskStore | None = None,
        heartbeat_interval: int = 30,
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
        # Projects with a pending/running ARCHITECTURE_INIT task (p10 §17).
        self._arch_projects: set[str] = set()
        self._active_leases: dict[str, str] = {}
        self._heartbeat_interval = max(1, heartbeat_interval)
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="worker-heartbeat"
        )
        self._heartbeat_thread.start()

    def submit(self, task: Task) -> None:
        with self._guard:
            if task.type == TASK_TYPE_ARCHITECTURE_INIT:
                self._arch_projects.add(task.project)
            self._pending.append(task)
            cycle = self._dependency_cycle()
            if cycle:
                logger.error(f"[pool] dependency cycle rejected: {sorted(cycle)}")
                doomed = [t for t in self._pending if t.id in cycle]
                self._pending = [t for t in self._pending if t.id not in cycle]
                for item in doomed:
                    if self._task_store:
                        self._task_store.transition_task(
                            item.id, "FAILED", "dependency cycle"
                        )
                    _move_to_failed(item.prompt_file)
            self._dispatch_locked()

    def join(self) -> None:
        """Block until no task is running or pending."""
        self._idle.wait()

    def shutdown(self) -> None:
        self._heartbeat_stop.set()
        self._heartbeat_thread.join(timeout=5)
        self._executor.shutdown(wait=True)

    # -- internal ----------------------------------------------------------

    def _dependency_cycle(self) -> set[str]:
        graph = {t.id: [d for d in t.depends_on] for t in self._pending}
        visiting: list[str] = []
        visited: set[str] = set()
        for start in graph:
            stack: list[tuple[str, int]] = [(start, 0)]
            while stack:
                node, index = stack[-1]
                if node in visited:
                    stack.pop()
                    continue
                if node not in visiting:
                    visiting.append(node)
                deps = [d for d in graph.get(node, []) if d in graph]
                if index < len(deps):
                    dep = deps[index]
                    stack[-1] = (node, index + 1)
                    if dep in visiting:
                        return set(visiting[visiting.index(dep):])
                    if dep not in visited:
                        stack.append((dep, 0))
                else:
                    visiting.remove(node)
                    visited.add(node)
                    stack.pop()
        return set()

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
        if task.type == TASK_TYPE_ARCHITECTURE_INIT:
            self._release_arch_project(task)
        if self._task_store:
            self._task_store.transition_task(
                task.id, "FAILED", "dependency can never complete"
            )
        _move_to_failed(task.prompt_file)

    def _release_arch_project(self, task: Task) -> None:
        """Stop blocking CODE_CHANGE tasks once no arch-init remains for it."""
        still_queued = any(
            t.type == TASK_TYPE_ARCHITECTURE_INIT and t.project == task.project
            for t in self._pending
        )
        if not still_queued:
            self._arch_projects.discard(task.project)

    def _dispatch_locked(self) -> None:
        still_pending: list[Task] = []
        for task in self._pending:
            dep_state = self._dependency_state(task)
            if dep_state == "cascade_fail":
                self._cascade_fail(task)
                continue
            if dep_state == "waiting":
                if self._task_store:
                    self._task_store.transition_task(
                        task.id, "BLOCKED", "waiting for dependencies: "
                        + ",".join(task.depends_on)
                    )
                logger.info(f"[pool] {task.id} waiting for dependencies: {task.depends_on}")
                still_pending.append(task)
                continue
            if (
                task.type != TASK_TYPE_ARCHITECTURE_INIT
                and task.project in self._arch_projects
            ):
                # p10 §17: no CODE_CHANGE while the project's architecture
                # initialisation is pending/running.
                logger.info(
                    f"[pool] {task.id} waiting for architecture init of {task.project}"
                )
                if self._task_store:
                    self._task_store.transition_task(
                        task.id, "BLOCKED", f"waiting for architecture init: {task.project}"
                    )
                still_pending.append(task)
                continue
            if self._lock_manager.acquire(task):
                self._running += 1
                self._idle.clear()

                lease_id = ""
                if self._task_store:
                    lease_id = self._task_store.claim_lease(task.id, self._worker_id)
                    self._active_leases[task.id] = lease_id

                logger.info(f"[pool] dispatching {task.id} (lease={lease_id})")
                future = self._executor.submit(self._run, task, lease_id)
                future.add_done_callback(lambda f, t=task: self._on_done(t))
            else:
                if self._task_store:
                    self._task_store.transition_task(
                        task.id, "BLOCKED", "waiting for resource lock"
                    )
                logger.info(f"[pool] {task.id} waiting for resources: {task.allowed_paths}")
                still_pending.append(task)
        self._pending = still_pending
        if self._pending:
            # Held-back tasks are not idle — join() must keep waiting.
            self._idle.clear()

    def _on_done(self, task: Task) -> None:
        with self._guard:
            if task.type == TASK_TYPE_ARCHITECTURE_INIT:
                self._release_arch_project(task)
            self._active_leases.pop(task.id, None)
            self._lock_manager.release(task)
            self._running -= 1
            self._dispatch_locked()
            if self._running == 0 and not self._pending:
                self._idle.set()

    def _run(self, task: Task, lease_id: str) -> dict[str, Any]:
        config = {"configurable": {"thread_id": task.id}}
        try:
            stored = self._task_store.get_task(task.id) if self._task_store else {}
            return self._graph.invoke(
                {
                    "task": task,
                    "_lease_id": lease_id,
                    "_worker_id": self._worker_id,
                    "attempt": int((stored or {}).get("attempt") or 0),
                    "attempt_id": str((stored or {}).get("current_attempt_id") or ""),
                    "resume_checkpoint": str((stored or {}).get("checkpoint") or ""),
                    "commit": (stored or {}).get("commit_sha"),
                    "replan_context": _prior_failure_context(stored, task),
                },
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

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self._heartbeat_interval):
            if not self._task_store:
                continue
            with self._guard:
                leases = list(self._active_leases.items())
            for task_id, lease_id in leases:
                ok = self._task_store.update_heartbeat(
                    task_id, self._worker_id, lease_id
                )
                if not ok:
                    logger.warning(f"[pool] {task_id} heartbeat rejected (lease lost)")


def _prior_failure_context(stored: dict | None, task: Task) -> str:
    """Failure context of the previous run, injected on requeue.

    A requeued task whose stored projection carries a failure resumes the
    preserved agent branch; telling the agent why the earlier run died
    turns the retry into a continuation ("fix this") instead of a redo.
    Returns "" for first-time tasks.
    """
    if not stored:
        return ""
    failure_type = str(stored.get("failure_type") or "")
    message = str(stored.get("failure_message") or "")
    if not failure_type and not message:
        return ""
    prior_attempt = int(stored.get("attempt") or 0)
    return (
        "\n\n## PREVIOUS_RUN_FAILURE\n"
        "This task failed in an earlier run"
        + (f" (after {prior_attempt} attempt(s))" if prior_attempt else "")
        + ". Prior progress is preserved on the agent branch — inspect the "
        "worktree first, continue the existing work, and fix the failure "
        "described below. Do not redo work that is already present.\n\n"
        f"failure_type: {failure_type or 'unknown'}\n"
        f"failure_message: {message[:2000]}\n"
    )


def _move_to_failed(prompt_file: Path) -> None:
    src = Path(prompt_file)
    if not src.exists():
        return
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    try:
        move(str(src), str(FAILED_DIR / src.name))
    except Exception as exc:
        logger.warning(f"[pool] failed to move {src.name} to failed/: {exc}")
