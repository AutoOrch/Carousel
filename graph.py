from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from log import get_logger
from opencode_client import OpenCodeClient
from worktree import commit_worktree, create_worktree, reset_worktree

logger = get_logger(__name__)


class Task(TypedDict):
    id: str
    title: str
    prompt: str


class State(TypedDict):
    repo: str
    tasks: list[Task]
    results: list[dict[str, Any]]


def planner(state: State) -> dict[str, Any]:
    logger.info("\n========== PLANNER ==========")
    tasks: list[Task] = [
        {
            "id": "T001",
            "title": "增加后端 hello API",
            "prompt": (
                "在当前项目中增加一个最简单的 hello API。"
                "如果项目使用 Go，请遵循现有结构；增加必要测试；"
                "完成后运行相关测试，并总结修改文件和测试结果。"
            ),
        },
        {
            "id": "T002",
            "title": "增加 README Quick Start",
            "prompt": (
                "在 README.md 中保留原有内容并增加 Quick Start 运行说明。"
                "不要修改代码。完成后总结修改内容。"
            ),
        },
    ]
    logger.info(f"Planner generated {len(tasks)} tasks")
    return {"tasks": tasks}


def run_task(state: State, task: Task) -> dict[str, Any]:
    repo = Path(state["repo"]).resolve()
    worktree = repo.parent / ".worktrees" / task["id"]
    branch = f"agent-{task['id']}"

    logger.info(f"\n========== TASK {task['id']} ==========")
    reset_worktree(repo, worktree, branch)
    create_worktree(repo, worktree, branch)
    logger.info(f"Worktree: {worktree}")

    mode = os.getenv("DEMO_MODE", "dry-run")
    if mode == "dry-run":
        session_id = f"dry-{task['id'].lower()}"
        logger.info(f"Mock OpenCode Session: {session_id}")
        if task["id"] == "T001":
            changed_file = worktree / "hello.py"
            changed_file.write_text(
                "def hello() -> dict[str, str]:\n"
                "    return {\"message\": \"hello\"}\n",
                encoding="utf-8",
            )
        else:
            changed_file = worktree / "README.md"
            with changed_file.open("a", encoding="utf-8") as handle:
                handle.write("\n## Quick Start\n\nRun `python hello.py`.\n")
        response = {"mode": "dry-run", "message": "Local change generated"}
        diff = [{"file": changed_file.name}]
    else:
        client = OpenCodeClient()
        session = client.create_session(task["title"], directory=str(worktree))
        session_id = session["id"]
        logger.info(f"OpenCode Session: {session_id}")

        prompt = f"""
你正在执行自动化 Coding Task。
任务 ID：{task['id']}
任务名称：{task['title']}
当前工作目录：{worktree}
任务要求：
{task['prompt']}

约束：只修改当前工作目录；不要修改无关文件；完成后运行相关测试。
"""
        response = client.send_message(session_id, prompt)
        diff = client.get_diff(session_id)

    commit = commit_worktree(worktree, f"agent: {task['title']}")

    return {
        "task_id": task["id"],
        "session_id": session_id,
        "worktree": str(worktree),
        "response": response,
        "diff": diff,
        "commit": commit,
    }


def execute_tasks(state: State) -> dict[str, Any]:
    logger.info("\n========== EXECUTE ==========")
    with ThreadPoolExecutor(max_workers=len(state["tasks"])) as executor:
        futures = [executor.submit(run_task, state, task) for task in state["tasks"]]
        results = [future.result() for future in futures]
    return {"results": results}


def review(state: State) -> dict[str, Any]:
    logger.info("\n========== REVIEW ==========")
    for result in state["results"]:
        diff = result.get("diff") or []
        diff_count = len(diff) if isinstance(diff, list) else 1
        logger.info(f"Task: {result['task_id']}")
        logger.info(f"Commit: {result['commit']}")
        logger.info(f"Worktree: {result['worktree']}")
        logger.info(f"Diff entries: {diff_count}\n")
    return {}


def build_graph():
    builder = StateGraph(State)
    builder.add_node("planner", planner)
    builder.add_node("execute", execute_tasks)
    builder.add_node("review", review)
    builder.add_edge(START, "planner")
    builder.add_edge("planner", "execute")
    builder.add_edge("execute", "review")
    builder.add_edge("review", END)
    return builder.compile()
