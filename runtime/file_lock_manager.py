from __future__ import annotations

import threading

from schemas.task import Task


def _normalize_path(p: str) -> str:
    p = p.replace("\\", "/").strip().strip("/").lower()
    if p.endswith("/**"):
        p = p[:-3].rstrip("/") + "/"
    return p


def _paths_conflict(a: str, b: str) -> bool:
    na = _normalize_path(a)
    nb = _normalize_path(b)
    if na in ("", "*", "**") or nb in ("", "*", "**"):
        return True
    if na == nb:
        return True
    for p1, p2 in ((na, nb), (nb, na)):
        if p1.endswith("/"):
            if p2.startswith(p1) or p2 == p1.rstrip("/"):
                return True
    return False


class FileLockManager:
    """Tracks file paths claimed by running tasks to prevent conflicting parallel edits.

    A task that declares ``allowed_paths`` in its front matter will be held in the
    pending queue until every declared path is free.  Tasks without
    ``allowed_paths`` bypass locking and run immediately (preserving maximum
    parallelism).
    """

    def __init__(self) -> None:
        self._locks: dict[str, str] = {}
        self._guard = threading.Lock()

    def acquire(self, task: Task) -> bool:
        paths = task.allowed_paths or ["**"]

        with self._guard:
            for p in paths:
                for held, holder in self._locks.items():
                    held_project, held_path = held.split("::", 1)
                    if (held_project == task.project.lower()
                            and holder != task.id
                            and _paths_conflict(p, held_path)):
                        return False
            for p in paths:
                self._locks[f"{task.project.lower()}::{_normalize_path(p)}"] = task.id
            return True

    def release(self, task: Task) -> None:
        paths = task.allowed_paths or ["**"]
        with self._guard:
            for p in paths:
                key = f"{task.project.lower()}::{_normalize_path(p)}"
                if self._locks.get(key) == task.id:
                    del self._locks[key]
