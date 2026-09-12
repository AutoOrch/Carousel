from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from config import FAILED_DIR, PROCESSED_DIR, REPORTS_DIR, ROOT, WORKTREES_DIR
from log import get_logger
from schemas.task import Task
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
        _task_store.update_heartbeat(task.id)


def _report(state: TaskState, heading: str, body: str) -> None:
    """Append a timestamped section to reports/<task_id>.md.

    Records what OpenCode (or the simulation) reported at each stage so the
    full feedback — status, changed files, summary, tests, issues, diagnosis
    — survives after the session has been deleted.
    """
    task: Task = state["task"]
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORTS_DIR / f"{task.id}.md"
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## {heading}  ({stamp})\n\n{body.strip()}\n")
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


def _cleanup_task(task: Task, worktree_path: str) -> None:
    """Remove the worktree directory and agent branch after task completion."""
    if not worktree_path:
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
    worktree = WORKTREES_DIR / task.project / task.id
    branch = f"agent/{task.id}"
    attempt = state.get("attempt", 0)

    _checkpoint(state, "prepare")

    if attempt == 0 and not (REPORTS_DIR / f"{task.id}.md").exists():
        # First prepare of this task — write the report header.
        _report(
            state,
            "Task",
            "id: {i}\nproject: {p}\nrepo: {r}\nbase branch: {b}\ntitle: {t}\nallowed paths: {ap}\ndependencies: {dp}".format(
                i=task.id, p=task.project, r=repo, b=task.base_branch,
                t=task.title, ap=", ".join(task.allowed_paths) or "(any)",
                dp=", ".join(task.depends_on) or "(none)",
            ),
        )

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

    return {"worktree": str(worktree)}


def execute(state: TaskState) -> dict[str, Any]:
    task: Task = state["task"]
    worktree = Path(state["worktree"])
    attempt = state.get("attempt", 0) + 1

    _checkpoint(state, f"execute[{attempt}]")

    mode = os.getenv("EXEC_MODE", "dry-run")
    url = os.getenv("OPENCODE_URL", DEFAULT_OPENCODE_URL)
    replan_context = state.get("replan_context", "")

    logger.info(f"[{task.id}] execute attempt={attempt} (mode={mode})")
    try:
        result = run_opencode(
            task, worktree, mode=mode, opencode_url=url,
            replan_context=replan_context,
        )
        _report(
            state,
            f"Execute attempt {attempt} — OpenCode response",
            _response_text(result.get("response")),
        )
        return {"attempt": attempt, "worker_result": result}
    except Exception as exc:
        logger.info(f"[{task.id}] execute error: {exc}")
        _report(state, f"Execute attempt {attempt} — ERROR", str(exc))
        return {
            "attempt": attempt,
            "worker_result": {},
            "error": str(exc),
            "failure_type": "EXECUTION_ERROR",
            "failure_message": str(exc),
        }


def validate(state: TaskState) -> dict[str, Any]:
    task: Task = state["task"]
    result = state.get("worker_result") or {}
    attempt = state.get("attempt", 1)

    _checkpoint(state, f"validate[{attempt}]")

    if state.get("error"):
        return {"status": "failed"}

    if os.getenv("EXEC_MODE", "dry-run") == "dry-run":
        if attempt <= task.simulate_failure:
            logger.info(f"[{task.id}] validate: simulated failure (attempt {attempt}/{task.simulate_failure})")
            _report(state, f"Validate attempt {attempt}", "FAILED (simulated)")
            return {
                "status": "failed",
                "failure_type": "TEST_FAILURE",
                "failure_message": f"Simulated failure on attempt {attempt}",
            }
        _report(state, f"Validate attempt {attempt}", "SUCCESS (dry-run)")
        return {"status": "success"}

    text = _response_text(result.get("response"))
    status = "success" if _is_success(text) else "failed"
    failure_type = "" if status == "success" else "TEST_FAILURE"
    failure_msg = "" if status == "success" else text
    logger.info(f"[{task.id}] validated as {status}")
    _report(state, f"Validate attempt {attempt}", status.upper())
    return {
        "status": status,
        "failure_type": failure_type,
        "failure_message": failure_msg,
    }


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
    diagnosis = diagnose_failure(
        task, worktree, failure_type, failure_message, mode=mode, opencode_url=url,
    )

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
    worktree = Path(state["worktree"])

    _checkpoint(state, f"replan[{attempt}]")

    if attempt >= _max_attempts():
        logger.info(f"[{task.id}] replan: max_attempts={_max_attempts()} reached")
        return {}

    if not diagnosis.get("retryable", True):
        logger.info(f"[{task.id}] replan: diagnosis says not retryable")
        return {"_force_fail": True}

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

    commit_sha = commit_worktree(worktree, f"agent: {task.title}")
    logger.info(f"[{task.id}] commit {commit_sha}")

    if _task_store and commit_sha:
        _task_store.set_commit(task.id, commit_sha)
        _task_store.record_attempt(
            task.id, attempt, "SUCCESS",
            response=_response_text((state.get("worker_result") or {}).get("response")),
        )

    _report(state, "Commit", commit_sha or "(nothing to commit)")
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

    lock = get_project_lock(repo)
    with lock:
        current = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if current != task.base_branch:
            run_git(repo, "checkout", task.base_branch)

        logger.info(f"[{task.id}] merging {branch} into {task.base_branch}...")
        result = merge_branch(repo, branch, message=f"merge: {task.title}")

        if result["success"]:
            logger.info(f"[{task.id}] merge: clean")
            _report(state, "Merge", f"clean merge into {task.base_branch}")
            return {"merge_status": "merged", "merge_resolved": False}

        if not result["conflict"]:
            abort_merge(repo)
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
        )

        if resolved:
            logger.info(f"[{task.id}] merge: resolved by Merge Agent")
            _report(state, "Merge", f"CONFLICT resolved by Merge Agent into {task.base_branch}")
            return {"merge_status": "merged", "merge_resolved": True}

        abort_merge(repo)
        logger.info(f"[{task.id}] merge: Merge Agent failed")
        _report(state, "Merge", "CONFLICT — Merge Agent failed to resolve")
        return {"merge_status": "conflict", "merge_resolved": False}


def finalize_success(state: TaskState) -> dict[str, Any]:
    task: Task = state["task"]
    _checkpoint(state, "done")
    _cleanup_task(task, state.get("worktree", ""))
    _move_task_file(task, PROCESSED_DIR)

    if _task_store:
        _task_store.update_status(task.id, "COMPLETED")

    note = ""
    if state.get("merge_resolved"):
        note = " (conflict resolved by Merge Agent)"
    attempt = state.get("attempt", 1)
    logger.info(
        f"[{task.id}] SUCCESS -> processed/{note} "
        f"(attempt={attempt}, commit={state.get('commit')})"
    )
    _report(
        state,
        "FINAL — SUCCESS",
        "task file -> processed/\ncommit: {c}\nattempts: {a}{n}".format(
            c=state.get("commit") or "n/a", a=attempt, n=note,
        ),
    )
    return {}


def finalize_failed(state: TaskState) -> dict[str, Any]:
    task: Task = state["task"]
    _checkpoint(state, "failed")
    try:
        abort_merge(task.project_path)
    except Exception:
        pass
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
    return {}


def _move_task_file(task: Task, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / task.prompt_file.name
    try:
        task.prompt_file.replace(target)
    except Exception as exc:
        logger.info(f"[{task.id}] failed to move task file: {exc}")


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


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------
def build_task_graph():
    """Build the per-task graph with retry loop and LangGraph checkpointer.

    The checkpointer (MemorySaver) preserves graph state between nodes so
    that if a node throws an unhandled exception, the state is recoverable.
    Each task uses ``thread_id = task.id`` as the checkpoint key.

    .. code-block:: text

       START → PREPARE → EXECUTE → VALIDATE
                                 │
                    ┌────────────┴────────────┐
                    │ success                 │ failure
                    ▼                         ▼
                  COMMIT                   DIAGNOSE
                    │                         │
                    ▼                         ▼
                  MERGE                     REPLAN
                    │                         │
              ┌─────┴─────┐          ┌────────┴────────┐
              ▼           ▼          │ attempt < max?  │
           SUCCESS     FAILED        ▼                 ▼
                                       EXECUTE       FAILED
    """
    builder = StateGraph(TaskState)

    builder.add_node("prepare", prepare)
    builder.add_node("execute", execute)
    builder.add_node("validate", validate)
    builder.add_node("diagnose", diagnose)
    builder.add_node("replan", replan)
    builder.add_node("commit", commit)
    builder.add_node("merge", merge)
    builder.add_node("finalize_success", finalize_success)
    builder.add_node("finalize_failed", finalize_failed)

    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "execute")
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
