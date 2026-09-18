from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path


# ---------------------------------------------------------------------------
# Per-project git lock — serialises all git operations on the same repo
# (worktree add/remove *and* merge) so concurrent tasks never race on the
# same repository.
# ---------------------------------------------------------------------------
_project_locks: dict[str, threading.RLock] = {}
_project_locks_guard = threading.Lock()


def get_project_lock(repo: str | Path) -> threading.RLock:
    key = str(Path(repo).resolve()).lower()
    with _project_locks_guard:
        if key not in _project_locks:
            _project_locks[key] = threading.RLock()
        return _project_locks[key]


def run_git(repo: str | Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Git command failed: git {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Worktree management
# ---------------------------------------------------------------------------
def create_worktree(
    repo: str | Path,
    worktree_dir: str | Path,
    branch: str,
    base_ref: str = "HEAD",
) -> Path:
    repo = Path(repo).resolve()
    worktree_dir = Path(worktree_dir).resolve()
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    if worktree_dir.exists():
        raise FileExistsError(f"Worktree already exists: {worktree_dir}")

    with get_project_lock(repo):
        run_git(repo, "worktree", "add", "-b", branch, str(worktree_dir), base_ref)
    return worktree_dir


def reset_worktree(repo: str | Path, worktree_dir: str | Path, branch: str) -> None:
    """Remove an existing worktree (and its branch) so a task can start clean."""
    repo = Path(repo).resolve()
    worktree_dir = Path(worktree_dir).resolve()

    with get_project_lock(repo):
        run_git(repo, "worktree", "prune")
        registered = run_git(repo, "worktree", "list", "--porcelain")
        normalized = str(worktree_dir).replace("\\", "/").lower()
        if normalized in registered.replace("\\", "/").lower():
            run_git(repo, "worktree", "remove", "--force", str(worktree_dir))
        elif worktree_dir.exists():
            shutil.rmtree(worktree_dir, ignore_errors=True)

        branches = run_git(repo, "branch", "--list", branch)
        if branches.strip():
            try:
                run_git(repo, "branch", "-D", branch)
            except RuntimeError:
                _force_remove_worktrees_on_branch(repo, branch)
                run_git(repo, "branch", "-D", branch)


def _force_remove_worktrees_on_branch(repo: Path, branch: str) -> None:
    listed = run_git(repo, "worktree", "list", "--porcelain")
    paths = [
        line.split(maxsplit=1)[1]
        for line in listed.splitlines()
        if line.startswith("worktree ")
    ]
    for path in paths:
        try:
            current = run_git(path, "rev-parse", "--abbrev-ref", "HEAD")
        except RuntimeError:
            continue
        if current == branch:
            run_git(repo, "worktree", "remove", "--force", path)


def commit_worktree(worktree_dir: str | Path, message: str) -> str | None:
    worktree_dir = Path(worktree_dir)
    run_git(worktree_dir, "add", ".")
    result = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(worktree_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    combined = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        if "nothing to commit" in combined.lower():
            return None
        raise RuntimeError(combined)
    return run_git(worktree_dir, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# Merge support (Layer 3 + Layer 4)
# ---------------------------------------------------------------------------
def merge_branch(
    repo: str | Path,
    branch: str,
    message: str = "",
) -> dict[str, object]:
    """Merge *branch* into the current branch of *repo*.

    Returns ``{"success": bool, "conflict": bool, "output": str}``.
    """
    # Keep the merge uncommitted until the runner's deterministic integration
    # gate passes.  This makes clean and conflict-resolved paths obey the same
    # validation policy and lets a failed gate abort without publishing code.
    args = ["git", "merge", "--no-ff", "--no-commit"]
    if message:
        args += ["-m", message]
    args.append(branch)

    result = subprocess.run(
        args,
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = f"{result.stdout}\n{result.stderr}"

    if result.returncode == 0:
        return {"success": True, "conflict": False, "output": output}

    if "CONFLICT" in output.upper():
        return {"success": False, "conflict": True, "output": output}

    return {"success": False, "conflict": False, "output": output}


def abort_merge(repo: str | Path) -> None:
    """Abort an in-progress merge (best-effort, ignores errors)."""
    subprocess.run(
        ["git", "merge", "--abort"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def list_conflicted_files(repo: str | Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return [f for f in result.stdout.strip().splitlines() if f]


def has_unmerged_paths(repo: str | Path) -> bool:
    return bool(list_conflicted_files(repo))


def commit_merge(repo: str | Path, message: str) -> None:
    """Commit a resolved merge."""
    run_git(repo, "commit", "-m", message)
