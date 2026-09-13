"""Planner — generates task .md files from a high-level requirement.

Usage:
    python planner.py --requirement spec.md --project demo [--mode dry-run]
    python planner.py --requirement spec.md --project demo --mode opencode

In *dry-run* mode the planner parses the requirement title and generates
canned task files that exercise the full pipeline (retry, merge, dependencies).

In *opencode* mode it calls the OpenCode Server to analyse the requirement
and produce a structured task list, which is then written to ``prompts/``.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from config import (
    DB_FILE,
    FAILED_DIR,
    PROCESSING_DIR,
    PROCESSED_DIR,
    PROMPTS_DIR,
    REPORTS_DIR,
    get_project,
    load_config,
)
from log import get_logger, setup_logging
from opencode_client import OpenCodeClient

logger = get_logger(__name__)


PLANNER_PROMPT = """你是一个 Task Planner。

请分析以下产品需求，将其拆分成 3-8 个可独立执行的 Coding Task。

可用项目列表（每个任务必须归属其中一个项目，只能选一个）：
{projects}

每个任务应该：
- 可以独立完成
- 有明确的验收标准
- 只属于一个项目；需求涉及多个项目时，按项目拆分成不同任务
- 尽量不与其他任务修改同一个文件
- allowed_paths 使用相对于该项目仓库根目录的路径（不要带仓库/项目名前缀）

产品需求：
{requirement}

请输出 JSON 数组，每个元素包含：
- "id": task-NNN-简短描述
- "title": 任务标题
- "project": 任务所属项目 ID（从上面列表中选择，必填）
- "prompt": 详细的任务描述（包含验收标准）
- "allowed_paths": 允许修改的文件列表（可选）
- "depends_on": 依赖的任务 id 列表（可选，可跨项目依赖）

只输出 JSON，不要其它内容。
"""


def _projects_block(projects: list[tuple[str, Any, str]]) -> str:
    """Format the resolved project list for the planner prompt."""
    lines = []
    for ref, project, _ in projects:
        lines.append(f"- {project.id}: {project.path.as_posix()}")
    return "\n".join(lines)


def plan(
    requirement: str,
    projects: list[tuple[str, Any, str]],
    mode: str = "dry-run",
    opencode_url: str = "http://127.0.0.1:4096",
) -> list[dict[str, Any]]:
    """Return a list of task dicts derived from the requirement.

    ``projects`` is a list of ``(user_ref, Project, normalized_ref)`` tuples
    where normalized_ref is what gets written into front-matter.
    """
    if mode == "dry-run":
        return _dry_run_plan(requirement, projects)
    return _opencode_plan(requirement, projects, opencode_url)


def _dry_run_plan(
    requirement: str, projects: list[tuple[str, Any, str]]
) -> list[dict[str, Any]]:
    """Generate canned tasks that exercise the full pipeline.

    With multiple projects the tasks are spread round-robin so the
    multi-project scheduling path is exercised too.
    """
    title = _extract_title(requirement) or "Requirement"
    specs = [
        ("setup", f"Setup for {title}",
         f"# Task: Setup\n\n为「{title}」创建基础结构和配置文件。\n\n## 验收标准\n- 基础文件创建完成",
         ["README.md"], []),
        ("implement", f"Implement {title}",
         f"# Task: Implement\n\n实现「{title}」的核心功能。\n\n## 验收标准\n- 核心功能可用",
         ["README.md"], ["setup"]),
        ("docs", f"Document {title}",
         f"# Task: Documentation\n\n为「{title}」编写文档。\n\n## 验收标准\n- 文档内容完整",
         [], ["implement"]),
    ]
    tasks = []
    for i, (slug, title_, prompt, paths, deps) in enumerate(specs):
        ref, project, norm = projects[i % len(projects)]
        tasks.append({
            "id": f"task-001-{slug}",
            "title": title_,
            "project": norm,
            "prompt": prompt,
            "allowed_paths": paths,
            "depends_on": [f"task-001-{d}" for d in deps],
        })
    return tasks


def _opencode_plan(
    requirement: str,
    projects: list[tuple[str, Any, str]],
    opencode_url: str,
) -> list[dict[str, Any]]:
    import json
    import re

    prompt = PLANNER_PROMPT.format(
        requirement=requirement, projects=_projects_block(projects)
    )
    client = OpenCodeClient(base_url=opencode_url)
    session = client.create_session(title=f"planner-{projects[0][1].id}")
    session_id = session["id"]
    try:
        response = client.send_message(session_id, prompt, agent="build")
    finally:
        client.delete_session(session_id)

    text = _response_text(response)
    logger.info(f"[planner] LLM response length: {len(text)} chars")
    logger.info(f"[planner] LLM response preview: {text[:200]}")

    # Extract JSON from the response — LLMs often wrap in ```json blocks.
    match = re.search(r"```json\s*(\[.*\])\s*```", text, re.DOTALL)
    if match:
        raw = match.group(1)
    else:
        match = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
        if match:
            raw = match.group()
        else:
            raise ValueError(f"Planner did not return JSON: {text[:300]}")

    logger.info(f"[planner] extracted JSON length: {len(raw)} chars")
    tasks = json.loads(raw)
    if not isinstance(tasks, list):
        raise ValueError("Planner returned non-list JSON")
    return _assign_projects(tasks, projects)


def _assign_projects(
    tasks: list[dict[str, Any]], projects: list[tuple[str, Any, str]]
) -> list[dict[str, Any]]:
    """Map each task's LLM-declared project onto the validated input list.

    The LLM may answer with a project id, its path, or garbage — normalise
    everything onto one of the ``--project`` values (never trust the LLM to
    invent new paths).  Unresolvable tasks fall back to the first project.
    """
    default_ref = projects[0][2]
    lookup: dict[str, str] = {}
    for ref, project, norm in projects:
        lookup[project.id.lower()] = norm
        lookup[ref.replace("\\", "/").lower()] = norm
        lookup[project.path.as_posix().lower()] = norm
        lookup[project.path.name.lower()] = norm

    for t in tasks:
        declared = str(t.get("project") or "").strip()
        norm = lookup.get(declared.lower())
        if norm is None:
            logger.warning(
                f"[planner] task {t.get('id')}: project '{declared}' not in "
                f"--project list, defaulting to {default_ref}"
            )
            norm = default_ref
        t["project"] = norm
    return tasks


_SEQ_RE = re.compile(r"task-(\d+)-")


def _max_existing_seq() -> int:
    """Highest task-NNN number across every lifecycle dir and the DB.

    Files may be deleted while DB records survive (and vice versa), so both
    are scanned — a new batch must never reuse a historical number.
    """
    seq = 0
    for directory in (PROMPTS_DIR, PROCESSING_DIR, PROCESSED_DIR, FAILED_DIR, REPORTS_DIR):
        if not directory.exists():
            continue
        for f in directory.glob("*.md"):
            m = _SEQ_RE.match(f.stem)
            if m:
                seq = max(seq, int(m.group(1)))
    try:
        if DB_FILE.exists():
            conn = sqlite3.connect(str(DB_FILE))
            try:
                for (task_id,) in conn.execute("SELECT task_id FROM tasks"):
                    m = _SEQ_RE.match(task_id)
                    if m:
                        seq = max(seq, int(m.group(1)))
            finally:
                conn.close()
    except Exception as exc:
        logger.warning(f"[planner] could not scan task store for sequence: {exc}")
    return seq


def _renumber(tasks: list[dict[str, Any]], start: int) -> list[dict[str, Any]]:
    """Reassign sequential task-NNN-slug ids and rewrite depends_on refs.

    LLM-generated ids (and the dry-run canned ids) restart at 001 every run,
    which collides with historical batches.  This gives every generated task
    a globally unique, monotonically increasing number.  Follow-up ids
    (``followup-NNN-slug``) keep their slug but lose the prefix.
    """
    id_map: dict[str, str] = {}
    for i, t in enumerate(tasks):
        old = str(t.get("id") or f"task-{start + i}")
        m = re.match(r"task-\d+-(.+)$", old) or re.match(r"followup-\d+-(.+)$", old)
        if m:
            slug = m.group(1)
        else:
            slug = re.sub(r"[^a-z0-9-]+", "-", old.lower()).strip("-") or "task"
        id_map[old] = f"task-{start + i:03d}-{slug}"

    for t in tasks:
        t["id"] = id_map[str(t.get("id") or t["id"])]
        deps = t.get("depends_on") or []
        t["depends_on"] = [id_map.get(str(d), str(d)) for d in deps]
    return tasks


def write_tasks(
    tasks: list[dict[str, Any]], default_project: str, run_id: str = ""
) -> list[Path]:
    """Write task dicts as .md files with front-matter into prompts/.

    Each task may carry its own ``project`` ref (multi-project batches);
    tasks without one get *default_project*.
    """
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    start = _max_existing_seq() + 1
    tasks = _renumber(tasks, start)
    logger.info(f"[planner] numbering tasks {start:03d}..{start + len(tasks) - 1:03d}")
    written: list[Path] = []
    for t in tasks:
        task_id = t["id"]
        filename = f"{task_id}.md"
        filepath = PROMPTS_DIR / filename

        project_ref = str(t.get("project") or default_project)
        # Build front-matter (project is quoted so paths with spaces stay
        # valid YAML; refs are normalised to forward slashes).
        project_ref = project_ref.replace("\\", "/")
        fm_lines = [f'project: "{project_ref}"']
        if run_id:
            fm_lines.append(f"run_id: {run_id}")
        if t.get("allowed_paths"):
            fm_lines.append("allowed_paths:")
            for p in t["allowed_paths"]:
                fm_lines.append(f"  - {p}")
        if t.get("depends_on"):
            fm_lines.append("depends_on:")
            for d in t["depends_on"]:
                fm_lines.append(f"  - {d}")
        fm_lines.append(f"simulate_failure: {t.get('simulate_failure', 0)}")

        front_matter = "---\n" + "\n".join(fm_lines) + "\n---\n\n"
        filepath.write_text(front_matter + t["prompt"], encoding="utf-8")
        written.append(filepath)
        logger.info(f"[planner] wrote {filepath.name}")
    return written


def _extract_title(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return None


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate task files from a requirement")
    parser.add_argument("--requirement", required=True, help="path to requirement .md file")
    parser.add_argument(
        "--project",
        required=True,
        help=(
            "project ID from config.yaml or a direct repo path; "
            "comma-separate multiple projects for cross-project requirements "
            "(e.g. --project \"D:/Workspace/Talen,D:/Workspace/resource\")"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("dry-run", "opencode"),
        default="dry-run",
        help="planning mode (default: dry-run)",
    )
    args = parser.parse_args()

    req_path = Path(args.requirement)
    if not req_path.exists():
        logger.error(f"Requirement file not found: {req_path}")
        raise SystemExit(1)

    requirement = req_path.read_text(encoding="utf-8")
    config = load_config()

    # Resolve every --project entry (comma-separated) up front.
    projects: list[tuple[str, Any, str]] = []
    for raw_ref in [p.strip() for p in args.project.split(",") if p.strip()]:
        try:
            project = get_project(config, raw_ref)
        except ValueError as exc:
            logger.error(f"Invalid --project entry '{raw_ref}': {exc}")
            raise SystemExit(1)
        if project is None:
            logger.error(
                f"Unknown project: {raw_ref} "
                f"(not in config.yaml and not an existing path)"
            )
            logger.error(f"Available IDs: {', '.join(config.projects.keys())}")
            raise SystemExit(1)
        norm = raw_ref.replace("\\", "/")
        projects.append((raw_ref, project, norm))

    os.environ["EXEC_MODE"] = args.mode
    os.environ["OPENCODE_URL"] = config.opencode_url

    setup_logging()
    logger.info(f"[planner] mode={args.mode}")
    for ref, project, _ in projects:
        logger.info(f"[planner] project {project.id}: {project.path} (branch={project.default_branch})")

    # Fail fast when the OpenCode Server is down — otherwise the planner
    # blocks on a 1800s HTTP timeout with no feedback.
    if args.mode == "opencode":
        from opencode_client import OpenCodeClient

        try:
            health = OpenCodeClient(base_url=config.opencode_url).health()
            logger.info(f"[planner] OpenCode health: {health}")
        except Exception as exc:
            logger.error(f"OpenCode Server unavailable: {config.opencode_url} ({exc})")
            logger.error("Start it first:  opencode serve --hostname 127.0.0.1 --port 4096")
            raise SystemExit(2)

    from runtime.task_store import TaskStore
    import requirement_run

    task_store = TaskStore()
    try:
        run_id = requirement_run.create_run(
            task_store,
            req_path,
            requirement,
            projects=[
                {"ref": norm, "id": project.id, "path": str(project.path),
                 "branch": project.default_branch}
                for _, project, norm in projects
            ],
            planner_mode=args.mode,
        )
    except Exception as exc:
        logger.warning(f"[planner] could not create requirement run: {exc}")
        run_id = ""

    tasks = plan(
        requirement,
        projects,
        mode=args.mode,
        opencode_url=config.opencode_url,
    )
    logger.info(f"[planner] generated {len(tasks)} task(s) (run_id={run_id or 'none'})")
    for t in tasks:
        logger.info(f"[planner]   - {t.get('id')}: project={t.get('project', '(default)')}")

    written = write_tasks(tasks, projects[0][2], run_id=run_id)
    if run_id:
        requirement_run.save_plan_tasks(run_id, tasks)
    task_store.close()

    logger.info(f"[planner] wrote {len(written)} file(s) to {PROMPTS_DIR.resolve()}")
    logger.info("[planner] run `python watcher.py --mode dry-run --once` to execute")
    if run_id:
        logger.info(
            "[planner] after tasks finish, the final requirement review runs "
            "automatically (or: python requirement_closure.py --run-id " + run_id + ")"
        )


if __name__ == "__main__":
    main()
