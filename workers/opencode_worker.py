from __future__ import annotations

from pathlib import Path
from typing import Any

from log import get_logger
from opencode_client import OpenCodeClient
from schemas.task import Task

logger = get_logger(__name__)


PROMPT_TEMPLATE = """你是一个 Coding Worker。

任务来源：{task_id}
任务名称：{title}
项目：{project}
基础分支：{base_branch}
当前工作目录：{worktree}

任务内容：
{prompt}

测试命令：{test_command}

要求：
1. 分析当前代码
2. 只实现当前任务，不要实现其它任务
3. 尽量遵循项目现有代码风格
4. 完成后运行相关测试（见上方测试命令）
5. 如果任务无法完成，明确说明原因
6. 不要等待用户回答问题，尽可能自主完成任务

完成后输出：

## STATUS
SUCCESS 或 FAILED

## CHANGED_FILES
修改的文件列表

## SUMMARY
完成内容

## TESTS
测试结果

## ISSUES
遗留问题
"""


def _format_test_command(command: str) -> str:
    return command if command and command.strip() else "（未指定，请按项目约定执行）"


def run_opencode(
    task: Task,
    worktree: Path,
    mode: str = "dry-run",
    opencode_url: str = "http://127.0.0.1:4096",
    replan_context: str = "",
) -> dict[str, Any]:
    full_prompt = PROMPT_TEMPLATE.format(
        task_id=task.id,
        title=task.title,
        project=task.project,
        base_branch=task.base_branch,
        worktree=worktree,
        prompt=task.prompt,
        test_command=_format_test_command(task.test_command),
    )
    if replan_context:
        full_prompt += replan_context

    if mode == "dry-run":
        return _dry_run(task.id, worktree, full_prompt)

    client = OpenCodeClient(base_url=opencode_url)
    session = client.create_session(title=task.title, directory=str(worktree))
    session_id = session["id"]
    try:
        response = client.send_message(
            session_id,
            full_prompt,
            model=task.model or None,
            agent=task.agent or None,
        )
        diff = client.get_diff(session_id)
    finally:
        client.delete_session(session_id)
        logger.info(f"[{task.id}] OpenCode session {session_id} cleaned up")
    return {
        "session_id": session_id,
        "response": response,
        "diff": diff,
        "prompt": full_prompt,
    }


def _dry_run(task_id: str, worktree: Path, full_prompt: str) -> dict[str, Any]:
    """Simulate a code change by appending to README.md so the worktree has something to commit.

    Two tasks targeting the same project will both modify README.md from the
    same base — the first merge succeeds, the second produces a real conflict
    that exercises the Merge Agent.
    """
    readme = worktree / "README.md"
    if readme.exists():
        with readme.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## {task_id}\n\nDry-run change by agent worker.\n")
    else:
        readme.write_text(
            f"# Demo\n\n## {task_id}\n\nDry-run change by agent worker.\n",
            encoding="utf-8",
        )
    return {
        "session_id": f"dry-{task_id}",
        "response": {
            "mode": "dry-run",
            "message": "## STATUS\nSUCCESS\n\n## SUMMARY\nDry-run change appended to README.md.\n",
        },
        "diff": [{"file": "README.md"}],
        "prompt": full_prompt,
    }
