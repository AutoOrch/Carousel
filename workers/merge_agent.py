from __future__ import annotations

import subprocess
from pathlib import Path

from log import get_logger
from opencode_client import OpenCodeClient
from worktree import commit_merge, has_unmerged_paths, list_conflicted_files, run_git

logger = get_logger(__name__)


MERGE_PROMPT_TEMPLATE = """你现在是 Merge Agent。

当前主分支：{base_branch}
正在合并：{branch}

发生冲突的文件：
{conflicts}

要求：
1. 保留两个分支的正确修改
2. 不改变两个任务已经完成的业务逻辑
3. 解决所有 Git conflict
4. 运行测试：{test_command}
5. 测试通过后提交

完成后输出：

## STATUS
RESOLVED 或 FAILED

## SUMMARY
解决过程
"""


def run_merge_agent(
    task_id: str,
    project_path: Path,
    base_branch: str,
    branch: str,
    test_command: str,
    agent: str,
    model: str,
    mode: str = "dry-run",
    opencode_url: str = "http://127.0.0.1:4096",
    attempt_id: str = "",
    run_id: str = "",
) -> bool:
    """Resolve merge conflicts. Returns True if resolved successfully."""
    conflicts = list_conflicted_files(project_path)
    logger.info(
        f"[{task_id}] Merge Agent: {len(conflicts)} conflicted file(s): "
        f"{', '.join(conflicts) if conflicts else '(none)'}"
    )

    if mode == "dry-run":
        return _resolve_dry_run(task_id, project_path, test_command, attempt_id, run_id)

    return _resolve_opencode(
        task_id,
        project_path,
        base_branch,
        branch,
        test_command,
        agent,
        model,
        conflicts,
        opencode_url,
        attempt_id,
        run_id,
    )


def _resolve_dry_run(
    task_id: str, repo: Path, test_command: str,
    attempt_id: str = "", run_id: str = "",
) -> bool:
    """Auto-resolve conflicts by keeping both sides (dry-run simulation)."""
    conflicts = list_conflicted_files(repo)
    for filepath in conflicts:
        full = repo / filepath
        if not full.exists():
            continue
        content = full.read_text(encoding="utf-8", errors="replace")
        resolved = _strip_conflict_markers(content)
        full.write_text(resolved, encoding="utf-8")
        run_git(repo, "add", filepath)
        logger.info(f"[{task_id}] Merge Agent: resolved {filepath}")

    if has_unmerged_paths(repo):
        logger.info(f"[{task_id}] Merge Agent: still has unmerged paths")
        return False

    if test_command:
        logger.info(f"[{task_id}] Merge Agent: running tests: {test_command}")
        result = subprocess.run(
            test_command, shell=True, cwd=str(repo), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=1800,
        )
        if result.returncode != 0:
            logger.info(
                f"[{task_id}] Merge Agent: tests failed\n{result.stdout}\n{result.stderr}"
            )
            return False
    else:
        logger.info(f"[{task_id}] Merge Agent: no test command, skipping tests")
    commit_merge(
        repo, f"merge-agent: {task_id}\n\nTask-ID: {task_id}\n"
        f"Attempt-ID: {attempt_id}\nRun-ID: {run_id or '-'}"
    )
    return True


def _resolve_opencode(
    task_id: str,
    repo: Path,
    base_branch: str,
    branch: str,
    test_command: str,
    agent: str,
    model: str,
    conflicts: list[str],
    opencode_url: str,
    attempt_id: str = "",
    run_id: str = "",
) -> bool:
    prompt = MERGE_PROMPT_TEMPLATE.format(
        base_branch=base_branch,
        branch=branch,
        conflicts="\n".join(conflicts),
        test_command=test_command or "（未指定）",
    )

    client = OpenCodeClient(base_url=opencode_url)
    session = client.create_session(
        title=f"merge-{task_id}",
        directory=str(repo),
    )
    session_id = session["id"]
    try:
        client.send_message(session_id, prompt, model=model or None, agent=agent or None)

        if has_unmerged_paths(repo):
            logger.info(f"[{task_id}] Merge Agent: OpenCode left unmerged paths")
            return False

        if test_command:
            result = subprocess.run(
                test_command,
                shell=True,
                cwd=str(repo),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                logger.info(f"[{task_id}] Merge Agent: tests failed\n{result.stdout}\n{result.stderr}")
                return False

        status = run_git(repo, "status", "--porcelain")
        if status.strip():
            commit_merge(
                repo, f"merge-agent: {task_id}\n\nTask-ID: {task_id}\n"
                f"Attempt-ID: {attempt_id}\nRun-ID: {run_id or '-'}"
            )

        return True
    finally:
        client.delete_session(session_id)


def _strip_conflict_markers(text: str) -> str:
    """Remove git conflict markers, keeping both sides' content."""
    lines = text.splitlines(keepends=True)
    result: list[str] = []
    ours: list[str] = []
    theirs: list[str] = []
    state = "normal"

    for line in lines:
        if line.startswith("<<<<<<<"):
            state = "ours"
        elif line.startswith("=======") and state == "ours":
            state = "theirs"
        elif line.startswith(">>>>>>>"):
            result.extend(ours)
            result.extend(theirs)
            ours = []
            theirs = []
            state = "normal"
        elif state == "ours":
            ours.append(line)
        elif state == "theirs":
            theirs.append(line)
        else:
            result.append(line)

    result.extend(ours)
    result.extend(theirs)
    return "".join(result)
