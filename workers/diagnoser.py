from __future__ import annotations

from typing import Any

from log import get_logger
from opencode_client import OpenCodeClient
from schemas.task import Task

logger = get_logger(__name__)


DIAGNOSE_PROMPT_TEMPLATE = """你现在是 Diagnosis Agent。

任务：{task_id}
任务名称：{title}

上一次执行失败了。

失败类型：{failure_type}
失败信息：
{failure_message}

当前工作目录：{worktree}

请分析：
1. 失败的根本原因是什么？
2. 哪些文件需要修改？
3. 具体应该怎么调整执行计划？

完成后输出：

## FAILURE_TYPE
{failure_type}

## ROOT_CAUSE
根本原因

## AFFECTED_FILES
需要修改的文件

## RECOMMENDED_CHANGES
具体调整建议

## RETRYABLE
YES 或 NO
"""


def diagnose_failure(
    task: Task,
    worktree: str,
    failure_type: str,
    failure_message: str,
    mode: str = "dry-run",
    opencode_url: str = "http://127.0.0.1:4096",
) -> dict[str, Any]:
    """Analyse a failure and return a structured diagnosis."""

    if mode == "dry-run":
        return _dry_run_diagnosis(task.id, failure_type, failure_message)

    prompt = DIAGNOSE_PROMPT_TEMPLATE.format(
        task_id=task.id,
        title=task.title,
        failure_type=failure_type,
        failure_message=failure_message,
        worktree=worktree,
    )

    # Diagnosis is a secondary analysis — it inherits the configured agent
    # timeout (opencode.timeout / OPENCODE_TIMEOUT).  Sending is async and
    # polled, so this is a wait budget, not a blocking HTTP read.
    client = OpenCodeClient(base_url=opencode_url)
    session = client.create_session(
        title=f"diagnose-{task.id}",
        directory=str(worktree),
    )
    session_id = session["id"]
    try:
        response = client.send_message(
            session_id, prompt, model=task.model or None, agent=task.agent or None
        )
        text = _response_text(response)
        return _parse_diagnosis(text)
    finally:
        client.delete_session(session_id)


def _dry_run_diagnosis(
    task_id: str, failure_type: str, failure_message: str
) -> dict[str, Any]:
    return {
        "failure_type": failure_type,
        "root_cause": f"[dry-run] Simulated failure for {task_id}",
        "affected_files": [],
        "recommended_changes": "Adjust approach and retry.",
        "retryable": True,
        "raw": f"## FAILURE_TYPE\n{failure_type}\n\n## ROOT_CAUSE\n{failure_message}\n",
    }


def _parse_diagnosis(text: str) -> dict[str, Any]:
    return {
        "failure_type": _section(text, "FAILURE_TYPE"),
        "root_cause": _section(text, "ROOT_CAUSE"),
        "affected_files": _section(text, "AFFECTED_FILES"),
        "recommended_changes": _section(text, "RECOMMENDED_CHANGES"),
        "retryable": _section(text, "RETRYABLE").upper().startswith("Y"),
        "raw": text,
    }


def _section(text: str, name: str) -> str:
    marker = f"## {name}"
    upper = text.upper()
    if marker.upper() not in upper:
        return ""
    start = upper.index(marker.upper()) + len(marker)
    rest = text[start:].lstrip("\n")
    end = rest.find("\n## ")
    return rest[:end].strip() if end != -1 else rest.strip()


def _response_text(response: Any) -> str:
    """Extract the assistant's text from an OpenCode message response."""
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


def build_replan_context(diagnosis: dict[str, Any], attempt: int) -> str:
    """Build extra prompt context for the next execution attempt."""
    return (
        f"\n\n--- RE-PLAN (attempt {attempt}) ---\n"
        f"上一次执行失败，诊断结果如下：\n\n"
        f"根本原因：{diagnosis.get('root_cause', '未知')}\n"
        f"建议修改：{diagnosis.get('recommended_changes', '无')}\n"
        f"涉及文件：{diagnosis.get('affected_files', '无')}\n\n"
        f"请在现有代码基础上调整实现，不要从头开始。\n"
    )
