from __future__ import annotations

import os
import hashlib
import subprocess
import time
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from config import FAILED_DIR, PROCESSED_DIR, ROOT, load_config
from log import get_logger, redact
from runtime.assets import task_report_path, worktree_path
from schemas.task import TASK_TYPE_ARCHITECTURE_INIT, Task
from workers.architecture_worker import run_architecture_init
from workers.diagnoser import build_replan_context, diagnose_failure
from workers.merge_agent import run_merge_agent
from workers.opencode_worker import run_opencode
from worktree import (
    abort_merge,
    commit_worktree,
    create_worktree,
    get_project_lock,
    merge_branch,
    reset_worktree,
    run_git,
)

DEFAULT_OPENCODE_URL = "http://127.0.0.1:4096"
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level task store — avoids putting a non-serialisable Python object
# into the graph state (which would break the LangGraph checkpointer).
# ---------------------------------------------------------------------------
_task_store: Any = None


def set_task_store(store: Any) -> None:
    global _task_store
    _task_store = store


class TaskState(TypedDict, total=False):
    task: Task
    worktree: str
    attempt: int
    worker_result: dict[str, Any]
    commit: str | None
    status: str
    error: str
    failure_type: str
    failure_message: str
    diagnosis: dict[str, Any]
    replan_context: str
    merge_status: str
    merge_resolved: bool
    _lease_id: str
    _force_fail: bool
    _worker_id: str
    attempt_id: str
    validation_result: dict[str, Any]
    resume_checkpoint: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _max_attempts() -> int:
    return int(os.getenv("MAX_ATTEMPTS", "3"))


def _backoff() -> float:
    return float(os.getenv("BACKOFF_SECONDS", "10"))


def _checkpoint(state: TaskState, node: str) -> None:
    """Update checkpoint + heartbeat in the SQLite store."""
    task: Task = state["task"]
    if _task_store:
        _task_store.update_checkpoint(task.id, node)
        _task_store.update_heartbeat(
            task.id, state.get("_worker_id", ""), state.get("_lease_id", "")
        )


def _report(state: TaskState, heading: str, body: str) -> None:
    """Append a timestamped section to reports/<task_id>.md.

    Records what OpenCode (or the simulation) reported at each stage so the
    full feedback — status, changed files, summary, tests, issues, diagnosis
    — survives after the session has been deleted.
    """
    task: Task = state["task"]
    try:
        path = task_report_path(task.project, task.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## {heading}  ({stamp})\n\n{redact(body).strip()}\n")
    except Exception as exc:
        logger.warning(f"[{task.id}] failed to write report: {exc}")


def _verify_lease(state: TaskState, node_name: str) -> dict[str, Any] | None:
    """Fencing: verify the current worker still holds the lease.

    Returns ``None`` if the lease is valid (or no store), or a failure dict
    if another worker has taken over.
    """
    if not _task_store:
        return None
    task: Task = state["task"]
    lease_id = state.get("_lease_id", "")
    if not lease_id:
        return None
    if not _task_store.check_lease(task.id, lease_id):
        logger.info(f"[{task.id}] {node_name}: LEASE LOST (fencing token mismatch)")
        return {
            "error": "lease lost — another worker took over",
            "failure_type": "LEASE_EXPIRED",
            "failure_message": "Fencing: lease token no longer matches",
        }
    return None


def _is_valid_worktree(repo: Path, worktree: Path, branch: str) -> bool:
    try:
        registered = run_git(repo, "worktree", "list", "--porcelain")
        normalized = str(worktree).replace("\\", "/").lower()
        return normalized in registered.replace("\\", "/").lower()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Dirty base-repo policy (robustness)
# ---------------------------------------------------------------------------
_STASH_PREFIX = "runner-stash:"


def _normalize_policy(value: str) -> str:
    policy = (value or "").strip().lower()
    return policy if policy in ("refuse", "allow", "stash") else "refuse"


def _dirty_files(repo: Path) -> list[str]:
    """Uncommitted paths in the base repo (unquoted so logs stay readable)."""
    out = run_git(repo, "-c", "core.quotepath=off", "status", "--porcelain")
    files: list[str] = []
    for line in out.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            files.append(parts[1].strip().strip('"'))
    return files


def _stash_base_changes(repo: Path, task_id: str) -> str:
    """Stash uncommitted base-repo changes (dirty_base_policy=stash).

    Idempotent across crash recovery: when a stash created by a previous
    run of the same task already exists, it is kept as-is so finalize
    restores the original state exactly once.  Returns "" on success or
    an error message.
    """
    marker = f"{_STASH_PREFIX}{task_id}"
    with get_project_lock(repo):
        try:
            listing = run_git(repo, "stash", "list")
            if any(marker in line for line in listing.splitlines()):
                logger.info(
                    f"[{task_id}] reusing base-repo stash from a previous run"
                )
                return ""
        except Exception:
            return ""  # listing failed — let the push below surface the error
        result = subprocess.run(
            ["git", "stash", "push", "--include-untracked", "-m", marker],
            cwd=str(repo), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        combined = (result.stdout + result.stderr)
        if result.returncode != 0 and "no local changes" not in combined.lower():
            return f"git stash failed: {combined.strip()[:300]}"
        return ""


def _restore_stash(task: Task) -> str:
    """Pop this task's base-repo stash at finalize time (best effort).

    Never raises: on conflict git keeps the stash entry, so the message
    tells the user how to recover manually.  Returns "" when there is
    nothing to restore.
    """
    repo = task.project_path
    marker = f"{_STASH_PREFIX}{task.id}"
    try:
        with get_project_lock(repo):
            listing = run_git(repo, "stash", "list")
            if not any(marker in line for line in listing.splitlines()):
                return ""
            run_git(repo, "stash", "pop")
        return ""
    except Exception as exc:
        return (
            f"stash pop failed: {str(exc)[:300]} — your changes are preserved "
            "in the stash; recover manually with 'git stash list' and "
            "'git stash pop'"
        )


def _restore_base_stash(state: TaskState) -> None:
    """Report a failed stash restore loudly instead of failing finalize."""
    task: Task = state["task"]
    try:
        problem = _restore_stash(task)
    except Exception as exc:
        problem = str(exc)
    if problem:
        logger.warning(f"[{task.id}] {problem}")
        _report(state, "Stash restore — ATTENTION", problem)


def _prepare_failure(
    state: TaskState, task: Task, failure_type: str, message: str
) -> dict[str, Any]:
    """Turn a prepare-time error into a normal failure state.

    Routing it through finalize_failed (instead of raising) keeps the
    worker alive, writes a proper report section, and moves the task
    file to failed/ — no crash, no orphaned processing/ file.
    """
    logger.error(f"[{task.id}] prepare failed ({failure_type}): {message}")
    _report(state, "Prepare — FAILED", f"type: {failure_type}\n\n{message}")
    return {
        "error": message,
        "failure_type": failure_type,
        "failure_message": message,
        "status": "failed",
    }


def _cleanup_task(task: Task, worktree_path: str) -> None:
    """Remove the worktree directory and agent branch after task completion."""
    if not worktree_path:
        return
    if task.type == TASK_TYPE_ARCHITECTURE_INIT:
        # "worktree" holds the business repo path for architecture tasks —
        # it must never be removed.
        return
    repo = task.project_path
    worktree = Path(worktree_path)
    branch = f"agent/{task.id}"
    try:
        reset_worktree(repo, worktree, branch)
        logger.info(f"[{task.id}] worktree cleaned up")
    except Exception as exc:
        logger.warning(f"[{task.id}] worktree cleanup failed: {exc}")


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
def prepare(state: TaskState) -> dict[str, Any]:
    task: Task = state["task"]
    repo = task.project_path
    worktree = worktree_path(task.project, task.id)
    branch = f"agent/{task.id}"
    attempt = state.get("attempt", 0)

    _checkpoint(state, "prepare")

    if attempt == 0 and not task_report_path(task.project, task.id).exists():
        # First prepare of this task — write the report header.
        _report(
            state,
            "Task",
            "id: {i}\nproject: {p}\nrepo: {r}\nbase branch: {b}\ntitle: {t}\ntype: {ty}\nallowed paths: {ap}\ndependencies: {dp}".format(
                i=task.id, p=task.project, r=repo, b=task.base_branch,
                t=task.title, ty=task.type,
                ap=", ".join(task.allowed_paths) or "(any)",
                dp=", ".join(task.depends_on) or "(none)",
            ),
        )

    if task.type == TASK_TYPE_ARCHITECTURE_INIT:
        # Architecture analysis reads the repo in place — no worktree, no
        # branch.  "worktree" carries the repo path for downstream nodes
        # (diagnoser prompts etc.).
        logger.info(f"[{task.id}] architecture task — analysing repo in place")
        return {"worktree": str(repo)}

    # --- Dirty base-repo policy: refuse | allow | stash ---
    try:
        dirty_files = _dirty_files(repo)
    except Exception as exc:
        return _prepare_failure(
            state, task, "PREPARE_ERROR",
            f"git status failed on base repository: {exc}",
        )

    if dirty_files:
        policy = _normalize_policy(task.dirty_base_policy)
        preview = "\n".join(dirty_files[:20])
        if policy == "stash":
            stash_error = _stash_base_changes(repo, task.id)
            if stash_error:
                return _prepare_failure(
                    state, task, "PREPARE_ERROR",
                    f"failed to stash base-repo changes: {stash_error}",
                )
            logger.info(
                f"[{task.id}] base repo dirty — stashed {len(dirty_files)} path(s) "
                "(dirty_base_policy=stash); restored at finalize"
            )
            _report(
                state, "Prepare — stashed base changes",
                f"{len(dirty_files)} uncommitted path(s) stashed:\n{preview}",
            )
        elif policy == "allow":
            logger.warning(
                f"[{task.id}] base repo has {len(dirty_files)} uncommitted path(s); "
                "proceeding (dirty_base_policy=allow)"
            )
            _report(
                state, "Prepare — dirty base allowed",
                f"uncommitted path(s) left in place:\n{preview}",
            )
        else:
            return _prepare_failure(
                state, task, "BASE_REPOSITORY_DIRTY",
                "base repository has uncommitted changes; refusing task "
                f"(dirty_base_policy=refuse):\n{preview}",
            )

    try:
        if attempt == 0 and not worktree.exists():
            reset_worktree(repo, worktree, branch)
            create_worktree(repo, worktree, branch, base_ref=task.base_branch)
            logger.info(f"[{task.id}] worktree created: {worktree}")
        elif worktree.exists() and _is_valid_worktree(repo, worktree, branch):
            logger.info(f"[{task.id}] reusing existing worktree (attempt={attempt})")
        else:
            logger.info(f"[{task.id}] worktree stale, recreating")
            reset_worktree(repo, worktree, branch)
            create_worktree(repo, worktree, branch, base_ref=task.base_branch)
    except Exception as exc:
        # Best-effort cleanup of a half-created worktree so nothing leaks.
        try:
            reset_worktree(repo, worktree, branch)
        except Exception:
            pass
        return _prepare_failure(
            state, task, "WORKTREE_ERROR",
            f"failed to set up worktree: {exc}",
        )

    return {"worktree": str(worktree)}


def execute(state: TaskState) -> dict[str, Any]:
    task: Task = state["task"]
    worktree = Path(state["worktree"])
    if _task_store and state.get("_lease_id"):
        attempt, attempt_id = _task_store.begin_attempt(
            task.id, state.get("_worker_id", ""), state.get("_lease_id", "")
        )
    else:
        attempt = state.get("attempt", 0) + 1
        attempt_id = f"{task.id}-attempt-{attempt}"

    _checkpoint(state, f"execute[{attempt}]")
    if _task_store:
        _task_store.set_attempt_context(
            task.id, attempt, model=task.model,
            prompt_hash=f"sha256:{hashlib.sha256(task.prompt.encode('utf-8')).hexdigest()}",
        )

    mode = os.getenv("EXEC_MODE", "dry-run")
    url = os.getenv("OPENCODE_URL", DEFAULT_OPENCODE_URL)
    replan_context = state.get("replan_context", "")

    logger.info(f"[{task.id}] execute attempt={attempt} (mode={mode})")
    try:
        if task.type == TASK_TYPE_ARCHITECTURE_INIT:
            result = run_architecture_init(
                task,
                load_config(),
                task_store=_task_store,
                mode=mode,
                opencode_url=url,
                replan_context=replan_context,
                lease_id=state.get("_lease_id", ""),
            )
        else:
            result = run_opencode(
                task, worktree, mode=mode, opencode_url=url,
                replan_context=replan_context,
            )
        _report(
            state,
            f"Execute attempt {attempt} — OpenCode response",
            _response_text(result.get("response")),
        )
        if task.type == TASK_TYPE_ARCHITECTURE_INIT:
            _report(
                state,
                f"Execute attempt {attempt} — architecture artifacts",
                "revision: {rev}\nsnapshot: {snap}\njson: {j}\nhtml: {h}\n\nvalidation:\n{v}".format(
                    rev=result.get("revision", "")[:12],
                    snap=result.get("snapshot_id", ""),
                    j=result.get("artifacts", {}).get("json", ""),
                    h=result.get("artifacts", {}).get("html", ""),
                    v=result.get("validation", ""),
                ),
            )
        session_id = str(result.get("session_id") or "")
        if _task_store:
            _task_store.set_attempt_context(
                task.id, attempt, model=task.model, session_id=session_id,
            )
        return {
            "attempt": attempt,
            "attempt_id": attempt_id,
            "worker_result": result,
            "error": "",
            "failure_type": "",
            "failure_message": "",
            "_force_fail": False,
            "status": "running",
        }
    except Exception as exc:
        logger.info(f"[{task.id}] execute error: {exc}")
        _report(state, f"Execute attempt {attempt} — ERROR", str(exc))
        return {
            "attempt": attempt,
            "attempt_id": attempt_id,
            "worker_result": {},
            "error": str(exc),
            "failure_type": "EXECUTION_ERROR",
            "failure_message": str(exc),
        }


def _changed_files(worktree: Path, base_branch: str) -> list[str]:
    """Return committed and uncommitted paths relative to the repository."""
    files: set[str] = set()
    try:
        diff = run_git(worktree, "diff", "--name-only", f"{base_branch}...HEAD")
        files.update(x.strip().replace("\\", "/") for x in diff.splitlines() if x.strip())
    except Exception:
        pass
    try:
        status = run_git(worktree, "status", "--porcelain")
    except Exception:
        status = ""
    for line in status.splitlines():
        parts = line.split(maxsplit=1)
        raw = parts[1] if len(parts) == 2 else line[2:].strip()
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        raw = raw.strip('"').replace("\\", "/")
        if raw:
            files.add(raw)
    return sorted(files)


def _path_allowed(path: str, patterns: list[str]) -> bool:
    from fnmatch import fnmatch
    value = path.replace("\\", "/").strip("/")
    for raw in patterns:
        pattern = str(raw).replace("\\", "/").strip("/")
        if pattern.endswith("/**"):
            prefix = pattern[:-3].rstrip("/")
            if value == prefix or value.startswith(prefix + "/"):
                return True
        if fnmatch(value, pattern):
            return True
    return False


def _run_test_command(command: str, cwd: Path) -> tuple[bool, str]:
    if not command or not command.strip():
        return True, "(no test command configured)"
    started = time.monotonic()
    try:
        result = subprocess.run(
            command, shell=True, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=1800,
        )
        elapsed = int((time.monotonic() - started) * 1000)
        output = (result.stdout + "\n" + result.stderr).strip()[:12000]
        return result.returncode == 0, (
            f"command: {command}\nexit: {result.returncode}\n"
            f"duration_ms: {elapsed}\n{output}"
        )
    except subprocess.TimeoutExpired:
        return False, f"command: {command}\nTIMEOUT after 1800s"


def _validate_code_change(task: Task, worktree: Path, result: dict[str, Any]) -> dict[str, Any]:
    changed = _changed_files(worktree, task.base_branch)
    violations: list[str] = []
    if not changed and not task.allow_empty:
        violations.append("code task produced no changed files")
    if task.allowed_paths:
        outside = [p for p in changed if not _path_allowed(p, task.allowed_paths)]
        if outside:
            violations.append("changed files outside allowed_paths: " + ", ".join(outside))
    claimed: list[str] = []
    for item in result.get("diff") or []:
        if isinstance(item, dict):
            name = item.get("file") or item.get("path") or item.get("filename")
            if name:
                claimed.append(str(name).replace("\\", "/"))
    claim_mismatch = sorted(set(claimed) ^ set(changed)) if claimed else []
    text = _response_text(result.get("response"))
    if os.getenv("EXEC_MODE", "dry-run") != "dry-run" and not _is_success(text):
        violations.append("agent did not report SUCCESS")
    tests_ok, test_output = _run_test_command(task.test_command, worktree)
    if not tests_ok:
        violations.append("configured test command failed")
    return {
        "status": "success" if not violations else "failed",
        "failure_type": "SCOPE_VIOLATION" if any("allowed_paths" in x for x in violations)
                        else "TEST_FAILURE" if not tests_ok else "VALIDATION_FAILURE",
        "changed_files": changed,
        "violations": violations,
        "test_command": task.test_command,
        "test_output": test_output,
        "test_exit_ok": tests_ok,
        "claim_mismatch": claim_mismatch,
    }


def _record_validation(
    state: TaskState, result_state: dict[str, Any], *,
    changed_files: list[str] | None = None,
    violations: list[str] | None = None,
    gate: dict[str, Any] | None = None,
) -> None:
    if not _task_store:
        return
    task: Task = state["task"]
    gate = gate or {}
    output = gate.get("test_output", "")
    exit_code = None
    if gate.get("test_command"):
        exit_code = 0 if gate.get("test_exit_ok") else 1
    _task_store.record_validation(
        task.id, state.get("attempt_id", ""), "pre_commit",
        "PASSED" if result_state.get("status") == "success" else "FAILED",
        command=gate.get("test_command", ""), exit_code=exit_code,
        output=output, changed_files=changed_files or [],
        violations=violations or [],
    )
    if changed_files:
        _task_store.record_changeset(
            task.id, state.get("attempt_id", ""), changed_files
        )
    if gate.get("claim_mismatch"):
        _task_store.append_event(
            "task.claim_mismatch", run_id=task.run_id, task_id=task.id,
            attempt_id=state.get("attempt_id", ""), status="CLAIM_MISMATCH",
            payload={"mismatch": gate["claim_mismatch"]},
        )


def validate(state: TaskState) -> dict[str, Any]:
    task: Task = state["task"]
    result = state.get("worker_result") or {}
    attempt = state.get("attempt", 1)

    _checkpoint(state, f"validate[{attempt}]")

    if state.get("error"):
        result_state = {
            "status": "failed",
            "failure_type": state.get("failure_type", "EXECUTION_ERROR"),
            "failure_message": state.get("failure_message") or state.get("error", ""),
        }
        _record_validation(state, result_state)
        return result_state

    if task.type == TASK_TYPE_ARCHITECTURE_INIT:
        # Success criteria (p10 §9): json + html + revision + metadata all
        # exist.  validate/render already ran inside the worker pipeline.
        artifacts = result.get("artifacts") or {}
        missing = [
            name
            for name, key in (
                ("architecture.json", "json"),
                ("architecture.html", "html"),
                ("metadata.json", "metadata"),
            )
            if not artifacts.get(key) or not Path(artifacts[key]).exists()
        ]
        if not result.get("revision"):
            missing.append("git revision")
        if missing:
            logger.info(f"[{task.id}] validate: missing artifacts: {missing}")
            _report(state, f"Validate attempt {attempt}", f"FAILED (missing: {', '.join(missing)})")
            result_state = {
                "status": "failed",
                "failure_type": "ARCHITECTURE_VALIDATION_FAILED",
                "failure_message": f"missing artifacts: {', '.join(missing)}",
            }
            _record_validation(state, result_state, violations=missing)
            return result_state
        logger.info(f"[{task.id}] validated as success (artifacts complete)")
        _report(state, f"Validate attempt {attempt}", "SUCCESS (artifacts complete)")
        result_state = {"status": "success"}
        _record_validation(state, result_state)
        return result_state

    gate = _validate_code_change(task, Path(state["worktree"]), result)
    if gate["status"] != "success":
        _report(
            state, f"Validate attempt {attempt}",
            "FAILED\n\n" + "\n".join(gate.get("violations") or [])
            + (f"\n\n{gate.get('test_output', '')}" if gate.get("test_output") else ""),
        )
        result_state = {
            "status": "failed",
            "failure_type": gate.get("failure_type", "VALIDATION_FAILURE"),
            "failure_message": "; ".join(gate.get("violations") or ["validation failed"]),
            "validation_result": gate,
        }
        _record_validation(state, result_state,
                           changed_files=gate.get("changed_files"),
                           violations=gate.get("violations"), gate=gate)
        return result_state

    if os.getenv("EXEC_MODE", "dry-run") == "dry-run":
        if attempt <= task.simulate_failure:
            logger.info(f"[{task.id}] validate: simulated failure (attempt {attempt}/{task.simulate_failure})")
            _report(state, f"Validate attempt {attempt}", "FAILED (simulated)")
            result_state = {
                "status": "failed",
                "failure_type": "TEST_FAILURE",
                "failure_message": f"Simulated failure on attempt {attempt}",
                "validation_result": gate,
            }
            _record_validation(state, result_state,
                               changed_files=gate.get("changed_files"),
                               violations=[result_state["failure_message"]], gate=gate)
            return result_state
        _report(state, f"Validate attempt {attempt}", "SUCCESS (dry-run)")
        result_state = {"status": "success", "validation_result": gate}
        _record_validation(state, result_state,
                           changed_files=gate.get("changed_files"), gate=gate)
        return result_state

    text = _response_text(result.get("response"))
    status = "success" if _is_success(text) else "failed"
    failure_type = "" if status == "success" else "TEST_FAILURE"
    failure_msg = "" if status == "success" else text
    logger.info(f"[{task.id}] validated as {status}")
    _report(state, f"Validate attempt {attempt}", status.upper())
    result_state = {
        "status": status,
        "failure_type": failure_type,
        "failure_message": failure_msg,
        "validation_result": gate,
    }
    _record_validation(state, result_state,
                       changed_files=gate.get("changed_files"),
                       violations=[] if status == "success" else [failure_msg], gate=gate)
    return result_state


def diagnose(state: TaskState) -> dict[str, Any]:
    task: Task = state["task"]
    worktree = state.get("worktree", "")
    failure_type = state.get("failure_type", "UNKNOWN")
    failure_message = state.get("failure_message", "")
    attempt = state.get("attempt", 1)

    _checkpoint(state, f"diagnose[{attempt}]")

    mode = os.getenv("EXEC_MODE", "dry-run")
    url = os.getenv("OPENCODE_URL", DEFAULT_OPENCODE_URL)

    logger.info(f"[{task.id}] diagnose failure (type={failure_type})")
    try:
        diagnosis = diagnose_failure(
            task, worktree, failure_type, failure_message, mode=mode, opencode_url=url,
        )
    except Exception as exc:
        # A diagnose failure (e.g. OpenCode timeout) must not crash the
        # graph — fall back to a minimal diagnosis so the retry loop can
        # still run.
        logger.warning(f"[{task.id}] diagnose agent failed: {exc}")
        diagnosis = {
            "failure_type": failure_type,
            "root_cause": f"(diagnosis unavailable: {str(exc)[:200]})",
            "affected_files": [],
            "recommended_changes": "Retry with the failure message as context.",
            "retryable": True,
            "raw": f"## FAILURE_TYPE\n{failure_type}\n\n## ROOT_CAUSE\n{failure_message[:1000]}\n",
        }

    if _task_store:
        _task_store.record_attempt(
            task.id, attempt, "FAILED", failure_type, failure_message,
            diagnosis.get("raw", ""),
            response=diagnosis.get("raw", ""),
        )

    _report(
        state,
        f"Diagnosis attempt {attempt}",
        "Root cause: {rc}\n\nAffected files: {af}\n\nRecommended changes: {r}\n\nRetryable: {retry}".format(
            rc=diagnosis.get("root_cause", "unknown"),
            af=diagnosis.get("affected_files", "none"),
            r=diagnosis.get("recommended_changes", "none"),
            retry=diagnosis.get("retryable", True),
        ),
    )

    return {"diagnosis": diagnosis}


def replan(state: TaskState) -> dict[str, Any]:
    task: Task = state["task"]
    attempt = state.get("attempt", 1)
    diagnosis = state.get("diagnosis") or {}
    failure_type = state.get("failure_type", "UNKNOWN")
    worktree = Path(state["worktree"])

    _checkpoint(state, f"replan[{attempt}]")

    if attempt >= _max_attempts():
        logger.info(f"[{task.id}] replan: max_attempts={_max_attempts()} reached")
        return {}

    if failure_type in {
        "LEASE_EXPIRED", "BASE_REPOSITORY_DIRTY", "INVALID_TASK",
        "PATH_TRAVERSAL", "CANCELLED",
    }:
        logger.info(f"[{task.id}] replan: {failure_type} is not retryable by policy")
        return {"_force_fail": True}

    if not diagnosis.get("retryable", True):
        logger.info(f"[{task.id}] replan: diagnosis says not retryable")
        return {"_force_fail": True}

    if task.type != TASK_TYPE_ARCHITECTURE_INIT:
        try:
            commit_worktree(worktree, f"checkpoint: attempt {attempt} failed")
        except Exception as exc:
            logger.info(f"[{task.id}] replan: checkpoint commit failed: {exc}")

    context = build_replan_context(diagnosis, attempt + 1)

    backoff = _backoff()
    if backoff > 0:
        logger.info(f"[{task.id}] replan: backing off {backoff}s before retry")
        time.sleep(backoff)

    logger.info(f"[{task.id}] replan: ready for attempt {attempt + 1}")
    return {"replan_context": context}


def commit(state: TaskState) -> dict[str, Any]:
    task: Task = state["task"]
    worktree = Path(state["worktree"])
    attempt = state.get("attempt", 1)

    _checkpoint(state, f"commit[{attempt}]")

    # --- Lease fencing (p5 §6) ---
    fence = _verify_lease(state, "commit")
    if fence:
        return fence
    operation_id = f"{task.id}:{attempt}:commit"
    if _task_store:
        op = _task_store.begin_operation(
            operation_id, task.id, state.get("attempt_id", ""), "commit"
        )
        if op.get("status") == "COMPLETED":
            result = __import__("json").loads(op.get("result") or "{}")
            return {"commit": result.get("commit")}

    if task.type == TASK_TYPE_ARCHITECTURE_INIT:
        # Architecture assets live in the runner's control plane — nothing
        # to commit in the business repo; record the snapshot marker instead.
        result = state.get("worker_result") or {}
        marker = f"architecture:{result.get('snapshot_id', 'unknown')}"
        logger.info(f"[{task.id}] commit skipped (architecture): {marker}")
        if _task_store:
            _task_store.set_commit(task.id, marker)
            _task_store.record_attempt(
                task.id, attempt, "SUCCESS",
                response=_response_text(result.get("response")),
            )
        _report(state, "Commit", f"(architecture snapshot — {marker})")
        if _task_store:
            _task_store.finish_operation(operation_id, "COMPLETED", {"commit": marker})
        _checkpoint(state, "commit_done")
        return {"commit": marker}

    commit_message = (
        f"agent: {task.title}\n\n"
        f"Task-ID: {task.id}\n"
        f"Attempt-ID: {state.get('attempt_id', '')}\n"
        f"Run-ID: {task.run_id or '-'}"
    )
    commit_sha = commit_worktree(worktree, commit_message)
    logger.info(f"[{task.id}] commit {commit_sha}")

    if _task_store:
        if commit_sha:
            _task_store.set_commit(task.id, commit_sha)
        _task_store.record_attempt(
            task.id, attempt, "SUCCESS",
            response=_response_text((state.get("worker_result") or {}).get("response")),
        )

    _report(state, "Commit", commit_sha or "(nothing to commit)")
    if _task_store:
        _task_store.finish_operation(operation_id, "COMPLETED", {"commit": commit_sha})
    _checkpoint(state, "commit_done")
    return {"commit": commit_sha}


def merge(state: TaskState) -> dict[str, Any]:
    task: Task = state["task"]
    repo = task.project_path
    branch = f"agent/{task.id}"
    mode = os.getenv("EXEC_MODE", "dry-run")
    url = os.getenv("OPENCODE_URL", DEFAULT_OPENCODE_URL)

    _checkpoint(state, "merge")

    # --- Lease fencing (p5 §6) ---
    fence = _verify_lease(state, "merge")
    if fence:
        return {**fence, "merge_status": "error"}
    operation_id = f"{task.id}:{state.get('attempt', 0)}:merge"
    if _task_store:
        op = _task_store.begin_operation(
            operation_id, task.id, state.get("attempt_id", ""), "merge"
        )
        if op.get("status") == "COMPLETED":
            return {"merge_status": "merged", "merge_resolved": False}

    if task.type == TASK_TYPE_ARCHITECTURE_INIT:
        # Nothing to merge — assets are already persisted by the worker.
        _report(state, "Merge", "(architecture — no merge required)")
        if _task_store:
            _task_store.finish_operation(operation_id, "COMPLETED", {})
        _checkpoint(state, "merge_done")
        return {"merge_status": "merged", "merge_resolved": False}

    lock = get_project_lock(repo)
    with lock:
        try:
            current = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
            if current != task.base_branch:
                run_git(repo, "checkout", task.base_branch)
        except Exception as exc:
            # e.g. checkout refused because of uncommitted local changes —
            # degrade to a normal merge error instead of crashing the graph.
            if _task_store:
                _task_store.finish_operation(
                    operation_id, "FAILED", {"error": str(exc)[:2000]}
                )
            _report(state, "Merge", f"ERROR (checkout)\n\n{exc}")
            return {"merge_status": "error", "error": str(exc)}

        logger.info(f"[{task.id}] merging {branch} into {task.base_branch}...")
        result = merge_branch(repo, branch, message=f"merge: {task.title}")

        if result["success"]:
            # merge --no-commit leaves a clean integration candidate in the
            # base repository.  Validate it before publishing the merge.
            ok, test_output = _run_test_command(task.test_command, repo)
            if not ok:
                abort_merge(repo)
                if _task_store:
                    _task_store.finish_operation(
                        operation_id, "FAILED", {"error": "integration test failed"}
                    )
                _report(state, "Merge", f"integration test FAILED\n\n{test_output}")
                return {"merge_status": "error", "error": "integration test failed"}
            merge_head = repo / ".git" / "MERGE_HEAD"
            if merge_head.exists():
                from worktree import commit_merge
                try:
                    commit_merge(
                        repo,
                        f"merge: {task.title}\n\nTask-ID: {task.id}\n"
                        f"Attempt-ID: {state.get('attempt_id', '')}\nRun-ID: {task.run_id or '-'}",
                    )
                except Exception as exc:
                    abort_merge(repo)
                    if _task_store:
                        _task_store.finish_operation(
                            operation_id, "FAILED", {"error": str(exc)[:2000]}
                        )
                    _report(state, "Merge", f"ERROR (commit)\n\n{exc}")
                    return {"merge_status": "error", "error": str(exc)}
            merge_sha = run_git(repo, "rev-parse", "HEAD")
            if _task_store:
                _task_store.set_merge_commit(task.id, merge_sha)
                _task_store.finish_operation(
                    operation_id, "COMPLETED", {"merge_commit": merge_sha}
                )
            logger.info(f"[{task.id}] merge: clean")
            _report(state, "Merge", f"clean merge into {task.base_branch}")
            _checkpoint(state, "merge_done")
            return {"merge_status": "merged", "merge_resolved": False}

        if not result["conflict"]:
            abort_merge(repo)
            if _task_store:
                _task_store.finish_operation(
                    operation_id, "FAILED", {"error": str(result["output"])[:2000]}
                )
            logger.info(f"[{task.id}] merge: error\n{result['output']}")
            _report(state, "Merge", f"ERROR\n\n{result['output']}")
            return {"merge_status": "error", "error": result["output"]}

        logger.info(f"[{task.id}] merge: CONFLICT, invoking Merge Agent...")
        resolved = run_merge_agent(
            task_id=task.id,
            project_path=repo,
            base_branch=task.base_branch,
            branch=branch,
            test_command=task.test_command,
            agent=task.agent,
            model=task.model,
            mode=mode,
            opencode_url=url,
            attempt_id=state.get("attempt_id", ""),
            run_id=task.run_id,
        )

        if resolved:
            merge_sha = run_git(repo, "rev-parse", "HEAD")
            if _task_store:
                _task_store.set_merge_commit(task.id, merge_sha)
                _task_store.finish_operation(
                    operation_id, "COMPLETED", {"merge_commit": merge_sha,
                                                  "resolved": True}
                )
            logger.info(f"[{task.id}] merge: resolved by Merge Agent")
            _report(state, "Merge", f"CONFLICT resolved by Merge Agent into {task.base_branch}")
            _checkpoint(state, "merge_done")
            return {"merge_status": "merged", "merge_resolved": True}

        abort_merge(repo)
        if _task_store:
            _task_store.finish_operation(
                operation_id, "FAILED", {"error": "merge conflict unresolved"}
            )
        logger.info(f"[{task.id}] merge: Merge Agent failed")
        _report(state, "Merge", "CONFLICT — Merge Agent failed to resolve")
        return {"merge_status": "conflict", "merge_resolved": False}


def finalize_success(state: TaskState) -> dict[str, Any]:
    task: Task = state["task"]
    _checkpoint(state, "done")
    _restore_base_stash(state)
    _cleanup_task(task, state.get("worktree", ""))
    moved = _move_task_file(task, PROCESSED_DIR)

    if _task_store and moved:
        _task_store.update_status(task.id, "COMPLETED")
    elif _task_store:
        _task_store.append_event(
            "task.reconciliation.required", task_id=task.id,
            run_id=task.run_id, status="RUNNING",
            payload={"reason": "processed file move failed"},
        )

    note = ""
    if state.get("merge_resolved"):
        note = " (conflict resolved by Merge Agent)"
    attempt = state.get("attempt", 1)
    logger.info(
        f"[{task.id}] {'SUCCESS -> processed/' if moved else 'MERGED; awaiting file reconciliation'}{note} "
        f"(attempt={attempt}, commit={state.get('commit')})"
    )
    _report(
        state,
        "FINAL — SUCCESS" if moved else "FINAL — RECONCILIATION REQUIRED",
        "task file -> {d}\ncommit: {c}\nattempts: {a}{n}".format(
            d="processed/" if moved else "processing/ (move failed)",
            c=state.get("commit") or "n/a", a=attempt, n=note,
        ),
    )
    if _task_store:
        _task_store.register_artifact(
            "TASK_REPORT", str(task_report_path(task.project, task.id)),
            run_id=task.run_id, task_id=task.id,
            attempt_id=state.get("attempt_id", ""),
        )
    return {}


def finalize_failed(state: TaskState) -> dict[str, Any]:
    task: Task = state["task"]
    _checkpoint(state, "failed")
    try:
        abort_merge(task.project_path)
    except Exception:
        pass
    _restore_base_stash(state)
    _cleanup_task(task, state.get("worktree", ""))
    _move_task_file(task, FAILED_DIR)

    if _task_store:
        _task_store.update_status(task.id, "FAILED")

    reason = (
        state.get("error")
        or state.get("merge_status")
        or state.get("failure_type")
        or "max attempts exceeded"
    )
    attempt = state.get("attempt", 1)
    logger.info(f"[{task.id}] FAILED -> failed/ (attempt={attempt}, reason={reason})")
    _report(
        state,
        "FINAL — FAILED",
        "task file -> failed/\nattempts: {a}\nreason: {r}".format(a=attempt, r=reason),
    )
    if _task_store:
        _task_store.register_artifact(
            "TASK_REPORT", str(task_report_path(task.project, task.id)),
            run_id=task.run_id, task_id=task.id,
            attempt_id=state.get("attempt_id", ""),
        )
    return {}


def _move_task_file(task: Task, dest_dir: Path) -> bool:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / task.prompt_file.name
    try:
        task.prompt_file.replace(target)
        return True
    except Exception as exc:
        logger.info(f"[{task.id}] failed to move task file: {exc}")
        return False


# ---------------------------------------------------------------------------
# Helpers (validate)
# ---------------------------------------------------------------------------
def _response_text(response: Any) -> str:
    """Extract the assistant's text from an OpenCode message response.

    The API returns ``{"info": ..., "parts": [...]}`` where parts contain
    ``step-start``/``reasoning``/``text`` entries — we only want the text.
    """
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        parts = response.get("parts")
        if isinstance(parts, list):
            texts = [
                p.get("text", "")
                for p in parts
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
        for key in ("message", "text", "content", "output"):
            if key in response:
                return str(response[key])
    return str(response)


def _is_success(text: str) -> bool:
    upper = text.upper()
    if "## STATUS" in upper:
        after = upper.split("## STATUS", 1)[1]
        head = "\n".join(after.split("\n", 2)[:2])
        return "SUCCESS" in head
    return "SUCCESS" in upper and "FAILED" not in upper


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
def route_after_prepare(state: TaskState) -> str:
    # Setup failures (dirty base repo, worktree errors) are terminal —
    # retrying immediately cannot fix them, so skip the retry loop.
    if state.get("error"):
        return "finalize_failed"
    return "execute"


def route_after_validate(state: TaskState) -> str:
    if state.get("status") == "success":
        return "commit"
    return "diagnose"


def route_after_replan(state: TaskState) -> str:
    if state.get("_force_fail"):
        return "finalize_failed"
    attempt = state.get("attempt", 1)
    if attempt >= _max_attempts():
        return "finalize_failed"
    return "execute"


def route_after_merge(state: TaskState) -> str:
    if state.get("merge_status") == "merged":
        return "finalize_success"
    return "finalize_failed"


def restore(state: TaskState) -> dict[str, Any]:
    """Choose a safe restart boundary from the durable task projection."""
    task: Task = state["task"]
    if task.type == TASK_TYPE_ARCHITECTURE_INIT:
        return {"worktree": str(task.project_path)}
    return {"worktree": str(worktree_path(task.project, task.id))}


def route_after_restore(state: TaskState) -> str:
    checkpoint = state.get("resume_checkpoint", "")
    if checkpoint in ("merge_done", "done"):
        return "finalize_success"
    if checkpoint == "commit_done" and state.get("commit"):
        return "merge"
    return "prepare"


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------
def build_task_graph():
    """Build the per-task graph with retry loop and LangGraph checkpointer.

    The checkpointer (MemorySaver) preserves graph state between nodes so
    that if a node throws an unhandled exception, the state is recoverable.
    Each task uses ``thread_id = task.id`` as the checkpoint key.

    .. code-block:: text

       START → RESTORE → PREPARE ── setup error ──► FINALIZE_FAILED
                              │
                            EXECUTE → VALIDATE
                                          │
                          ┌───────────────┴───────────────┐
                          │ success                       │ failure
                          ▼                               ▼
                        COMMIT                          DIAGNOSE
                          │                               │
                          ▼                               ▼
                         MERGE                           REPLAN
                          │                      ┌────────┴────────┐
                    ┌─────┴─────┐                │ attempt < max?  │
                    ▼           ▼                ▼                 ▼
                 SUCCESS     FAILED            EXECUTE         FAILED
    """
    builder = StateGraph(TaskState)

    builder.add_node("restore", restore)
    builder.add_node("prepare", prepare)
    builder.add_node("execute", execute)
    builder.add_node("validate", validate)
    builder.add_node("diagnose", diagnose)
    builder.add_node("replan", replan)
    builder.add_node("commit", commit)
    builder.add_node("merge", merge)
    builder.add_node("finalize_success", finalize_success)
    builder.add_node("finalize_failed", finalize_failed)

    builder.add_edge(START, "restore")
    builder.add_conditional_edges(
        "restore", route_after_restore,
        {"prepare": "prepare", "merge": "merge",
         "finalize_success": "finalize_success"},
    )
    builder.add_conditional_edges(
        "prepare", route_after_prepare,
        {"execute": "execute", "finalize_failed": "finalize_failed"},
    )
    builder.add_edge("execute", "validate")
    builder.add_conditional_edges(
        "validate",
        route_after_validate,
        {"commit": "commit", "diagnose": "diagnose"},
    )
    builder.add_edge("commit", "merge")
    builder.add_conditional_edges(
        "merge",
        route_after_merge,
        {"finalize_success": "finalize_success",
         "finalize_failed": "finalize_failed"},
    )
    builder.add_edge("diagnose", "replan")
    builder.add_conditional_edges(
        "replan",
        route_after_replan,
        {"execute": "execute", "finalize_failed": "finalize_failed"},
    )
    builder.add_edge("finalize_success", END)
    builder.add_edge("finalize_failed", END)

    # LangGraph checkpointer — preserves state across nodes.
    # MemorySaver is in-process; upgrade to SqliteSaver for persistence.
    checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer)
