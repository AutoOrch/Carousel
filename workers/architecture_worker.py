"""Architecture analysis worker (p10 §18).

Reads the project (never modifies it), asks OpenCode — or, in dry-run mode,
a deterministic scanner — to produce an Archify architecture JSON IR, then
hands off to ArchitectureManager for validate (with the self-repair loop),
render and persistence.

Output contract (message response):
    ## STATUS / ## COMPONENTS / ## DEPENDENCIES / ## RISKS / ## ISSUES
The architecture JSON itself is extracted from a ```json fenced block so
the business repo is never written to.

IR sanitising and geometry live in :mod:`architecture.ir_tools`; the
validate self-repair loop lives in :mod:`architecture.manager`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from architecture import ir_tools
from architecture.manager import ArchitectureManager
from config import Config
from log import get_logger
from opencode_client import OpenCodeClient
from schemas.task import Task
from worktree import get_project_lock

logger = get_logger(__name__)


ARCHITECTURE_PROMPT_TEMPLATE = """你是架构分析 Agent，不是编码 Agent。

目标：
分析当前项目，生成 Archify 架构 JSON（architecture diagram）。

禁止：
1. 修改业务代码
2. 创建或删除业务文件
3. 修改 Git 分支
4. 编造不存在的模块
5. 将猜测当成代码事实

必须：
1. 阅读项目入口（main/cmd/README）
2. 阅读配置和依赖（go.mod / package.json / 配置文件）
3. 识别 {max_nodes} 个左右核心组件（不要把每个文件画成节点）
4. 识别主要调用链 / 请求链路
5. 标记数据库、缓存、外部服务和边界
6. 记录证据文件和代码位置（用于 analysis 文档）
7. 当前 Git revision 是 {revision}（只需记录，不要执行 git 操作）

输出格式（严格遵守）：

先输出一个 ```json 代码块，内容是 Archify JSON IR，要求：
- schema_version: 1
- diagram_type: "architecture"
- meta: 只包含 title（不要写 viewBox / views / legend 等其它字段，
  viewBox 由渲染器自动计算）
- components: 每个含 id / type / label（可选 sublabel）
  - type 只能是: external / cloud / backend / frontend / database /
    security / messagebus
  - **不要写 pos / size / fromSide / toSide** — 系统会自动计算布局
  - id 用简短英文（如 "api-gateway"），label 是显示名
- boundaries: 按需（kind 只能是 region / security-group，含 label + wraps）
- connections: 每个至少含 id / from / to（可选 label；关系必须有代码或配置依据）
  - **不要写 pos / fromSide / toSide / via** — 系统自动设置
  - 只定义拓扑关系（谁连到谁），布局由系统计算

然后输出：

## STATUS
SUCCESS 或 FAILED

## COMPONENTS
每个核心组件一行：名称 — 职责 — 证据文件

## DEPENDENCIES
组件间主要依赖关系及依据

## RISKS
架构层面观察到的风险（无则写 无）

## EVIDENCE
关键证据文件列表

## ISSUES
分析过程中的问题（无则写 无）
"""


def run_architecture_init(
    task: Task,
    config: Config,
    task_store: Any = None,
    mode: str = "dry-run",
    opencode_url: str = "http://127.0.0.1:4096",
    replan_context: str = "",
    lease_id: str = "",
) -> dict[str, Any]:
    """Entry point called from task_graph.execute for ARCHITECTURE_INIT."""
    manager = ArchitectureManager(config, task_store)

    if mode == "dry-run":
        producer = _dry_run_producer(task, config)
    else:
        producer = _opencode_producer(task, config, opencode_url, replan_context)

    # Architecture observes the base repository and publishes a revision-bound
    # snapshot, so merges for this project must wait until publication ends.
    with get_project_lock(task.project_path):
        fence = None
        if task_store is not None and lease_id:
            fence = lambda: task_store.check_lease(task.id, lease_id)
        return manager.run_initialization(task, producer, mode, fence=fence)


# --------------------------------------------------------------------------
# OpenCode producer
# --------------------------------------------------------------------------

def _opencode_producer(task: Task, config: Config, opencode_url: str, replan_context: str):
    def produce(work_dir: Path, revision: str) -> dict[str, Any]:
        arch_conf = _project_arch_conf(config, task)
        prompt = ARCHITECTURE_PROMPT_TEMPLATE.format(
            max_nodes=arch_conf["max_core_nodes"], revision=revision[:12],
        )
        if replan_context:
            prompt += replan_context

        client = OpenCodeClient(base_url=opencode_url)
        session = client.create_session(
            title=f"arch-init-{task.project}", directory=str(task.project_path),
        )
        session_id = session["id"]
        try:
            response = client.send_message(
                session_id, prompt, model=task.model or None, agent=task.agent or None,
            )
        finally:
            client.delete_session(session_id)

        text = _response_text(response)
        json_text = _extract_json_block(text)
        if not json_text:
            raise RuntimeError(
                "OpenCode did not return a ```json architecture block: "
                + text[:300]
            )
        data = json.loads(json_text)
        data, fixes = ir_tools.normalize_ir(data)
        for fix in fixes:
            logger.info("[%s] IR normalised: %s", task.id, fix)
        for fix in ir_tools.auto_layout(data):
            logger.info("[%s] layout: %s", task.id, fix)

        (work_dir / "architecture.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {
            "analysis": _analysis_from_sections(text),
            "response": text,
            "revision": revision[:12],
        }

    return produce


# --------------------------------------------------------------------------
# Dry-run producer — deterministic, no OpenCode required
# --------------------------------------------------------------------------

def _dry_run_producer(task: Task, config: Config):
    def produce(work_dir: Path, revision: str) -> dict[str, Any]:
        arch_conf = _project_arch_conf(config, task)
        repo = task.project_path
        include = arch_conf["include"]
        exclude = arch_conf["exclude"]

        # Top-level directories as component candidates.
        dirs: list[str] = []
        for entry in sorted(repo.iterdir()):
            if not entry.is_dir() or entry.name.startswith(".") or entry.name in exclude:
                continue
            if include and entry.name not in include:
                continue
            dirs.append(entry.name)
            if len(dirs) >= arch_conf["max_core_nodes"]:
                break

        components = [
            {"id": "client", "type": "external",
             "label": "Client", "sublabel": "dry-run",
             "pos": [40, 250], "size": [120, 60]},
            {"id": "entry", "type": "cloud",
             "label": "Entry", "sublabel": "repo root",
             "pos": [220, 250], "size": [120, 60]},
        ]
        for i, d in enumerate(dirs):
            components.append({
                "id": d, "type": ir_tools.guess_type(d), "label": d,
                "sublabel": "dir",
                "pos": [460, 100 + i * 150],
                "size": [190, 60],
            })
        connections = [
            {"id": "client-entry", "from": "client", "to": "entry", "label": "requests"},
            *(
                {"id": f"entry-{d}", "from": "entry", "to": d,
                 "fromSide": "right", "toSide": "left"}
                for d in dirs
            ),
        ]
        data = {
            "schema_version": 1,
            "diagram_type": "architecture",
            "meta": {"title": f"{task.project} — dry-run baseline",
                     "visual_preset": "signal-flow"},
            "components": components,
            "boundaries": [
                {"kind": "region", "label": task.project, "wraps": ["entry", *dirs]},
            ],
            "connections": connections,
        }
        (work_dir / "architecture.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        analysis = {
            "modules.md": (
                f"# Modules — {task.project} (dry-run)\n\n"
                + "\n".join(f"- `{d}/`" for d in dirs)
                or "- (none)"
            ),
            "dependencies.md": (
                "# Dependencies — dry-run\n\n"
                "entry → " + ", ".join(dirs)
            ),
            "risks.md": (
                "# Risks — dry-run\n\n"
                "Deterministic scan: component granularity is directory-level; "
                "run with --mode opencode for a real analysis."
            ),
        }
        return {
            "analysis": analysis,
            "response": (
                "## STATUS\nSUCCESS\n\n## COMPONENTS\n"
                + "\n".join(dirs)
                + "\n\n## ISSUES\nnone (dry-run)\n"
            ),
            "revision": revision[:12],
        }

    return produce


def _project_arch_conf(config: Config, task: Task) -> dict[str, Any]:
    project = config.projects.get(task.project)
    arch = project.architecture if project else None
    return {
        "max_core_nodes": arch.max_core_nodes if arch else 12,
        "include": arch.include if arch else [],
        "exclude": arch.exclude if arch else [
            "vendor", "node_modules", ".git", "dist", "build",
        ],
    }


# --------------------------------------------------------------------------
# Response parsing helpers
# --------------------------------------------------------------------------

def _extract_json_block(text: str) -> str | None:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return match.group(1)
    match = re.search(r"```(?:\w+)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    return match.group(1) if match else None


def _analysis_from_sections(text: str) -> dict[str, str]:
    return {
        "modules.md": _section(text, "COMPONENTS") or "(not provided)",
        "dependencies.md": _section(text, "DEPENDENCIES") or "(not provided)",
        "risks.md": _section(text, "RISKS") or "(not provided)",
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
