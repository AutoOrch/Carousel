from __future__ import annotations

import shutil
import threading
from pathlib import Path

from config import Config, FAILED_DIR, PROCESSED_DIR, PROCESSING_DIR, PROMPTS_DIR
from log import get_logger
from runtime.task_store import TaskStore
from worktree import abort_merge
from worktree import run_git
from runtime.task_resolver import resolve_task

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
                processed = PROCESSED_DIR / prompt_file.name
                failed = FAILED_DIR / prompt_file.name
                if processed.exists():
                    self._store.update_checkpoint(task_id, "recovered_done")
                    self._store.transition_task(task_id, "COMPLETED")
                    self._store.append_event(
                        "task.file_move.reconciled", task_id=task_id,
                        run_id=entry.get("run_id") or "", status="COMPLETED",
                        payload={"path": str(processed)},
                    )
                    recovered.append(task_id)
                elif failed.exists():
                    self._store.transition_task(task_id, "FAILED")
                    self._store.append_event(
                        "task.file_move.reconciled", task_id=task_id,
                        run_id=entry.get("run_id") or "", status="FAILED",
                        payload={"path": str(failed)},
                    )
                continue
            checkpoint = entry.get("checkpoint") or ""

            # Reconcile an external side effect that completed just before the
            # runner died.  A merged commit is authoritative and must not be
            # executed a second time.
            try:
                task = resolve_task(src, self._config)
                if (task.project_path / ".git" / "MERGE_HEAD").exists():
                    self._store.append_event(
                        "task.recovery.merge_conflict", task_id=task_id,
                        run_id=entry.get("run_id") or "", status="DETECTED",
                        payload={"project": task.project},
                    )
                    abort_merge(task.project_path)
                commit = entry.get("merge_commit_sha") or entry.get("commit_sha") or ""
                if not commit and str(checkpoint).startswith("commit"):
                    branch = f"agent/{task_id}"
                    try:
                        candidate = run_git(task.project_path, "rev-parse", branch)
                        message = run_git(
                            task.project_path, "show", "-s", "--format=%B", candidate
                        )
                        if f"Task-ID: {task_id}" in message:
                            commit = candidate
                            self._store.set_commit(task_id, candidate)
                            self._store.finish_operation(
                                f"{task_id}:{entry.get('attempt', 0)}:commit",
                                "COMPLETED", {"commit": candidate, "recovered": True},
                            )
                            self._store.update_checkpoint(task_id, "commit_done")
                            checkpoint = "commit_done"
                            self._store.append_event(
                                "task.commit.reconciled", task_id=task_id,
                                run_id=entry.get("run_id") or "", status="COMPLETED",
                                payload={"commit": candidate},
                            )
                    except Exception:
                        pass
                merged = False
                if commit and not str(commit).startswith("architecture:"):
                    try:
                        run_git(task.project_path, "merge-base", "--is-ancestor", commit, "HEAD")
                        merged = True
                    except Exception:
                        merged = False
                if checkpoint in ("merge_done", "done") or merged:
                    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(PROCESSED_DIR / src.name))
                    self._store.update_checkpoint(task_id, "recovered_done")
                    self._store.transition_task(task_id, "COMPLETED")
                    self._store.append_event(
                        "task.reconciled", task_id=task_id,
                        run_id=entry.get("run_id") or "", status="COMPLETED",
                        payload={"reason": "commit already merged", "commit": commit},
                    )
                    recovered.append(task_id)
                    continue
            except Exception as exc:
                logger.warning(f"[recovery] reconcile failed for {task_id}: {exc}")

            if (int(entry.get("attempt") or 0) >= self._config.max_attempts
                    and checkpoint != "commit_done"):
                if int(entry.get("attempt") or 0):
                    self._store.record_attempt(
                        task_id, int(entry["attempt"]), "FAILED",
                        "LEASE_LOST", "worker lease expired during attempt",
                    )
                FAILED_DIR.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(FAILED_DIR / src.name))
                self._store.transition_task(
                    task_id, "FAILED", "retry budget exhausted during recovery"
                )
                continue

            if int(entry.get("attempt") or 0) and checkpoint != "commit_done":
                self._store.record_attempt(
                    task_id, int(entry["attempt"]), "FAILED",
                    "LEASE_LOST", "worker lease expired during attempt",
                )

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
        """Deprecated: heartbeats are owned by WorkerPool lease holders."""
        return None

    def stop_heartbeat(self) -> None:
        self._hb_stop.set()
        if self._hb_thread:
            self._hb_thread.join(timeout=5)

    def _heartbeat_loop(self) -> None:
        # Kept for API compatibility.  A process must never renew leases that
        # belong to another worker; WorkerPool supplies worker_id + lease_id.
        return None
