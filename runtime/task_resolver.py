from __future__ import annotations

from pathlib import Path

import yaml

from config import Config, get_project
from schemas.task import VALID_TASK_TYPES, TASK_TYPE_CODE_CHANGE, Task


def parse_markdown(text: str) -> tuple[dict, str]:
    """Split a prompt file into (front-matter metadata, body).

    Front matter is an optional YAML block delimited by `---` lines at the top.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    end = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end = idx
            break

    if end is None:
        return {}, text

    meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    if not isinstance(meta, dict):
        meta = {}
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return meta, body


def resolve_task(task_file: Path, config: Config) -> Task:
    """Parse a prompt file and resolve its `project` against the config.

    The `project` front-matter value may be either a project ID from
    config.yaml or a direct filesystem path to a git repository.

    Raises ValueError when the project is missing or unknown so the caller can
    route the file to failed/.
    """
    # utf-8-sig tolerates a BOM written by Windows editors/PowerShell,
    # which would otherwise break front-matter detection.
    raw = task_file.read_text(encoding="utf-8-sig", errors="replace")
    meta, body = parse_markdown(raw)

    project_id = meta.get("project")
    if not project_id:
        raise ValueError(f"missing 'project' in front matter: {task_file.name}")

    project = get_project(config, str(project_id))
    if project is None:
        raise ValueError(
            f"unknown project '{project_id}' in {task_file.name} "
            f"(not in config.yaml and not an existing path)"
        )

    raw_paths = meta.get("allowed_paths") or []
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    for item in raw_paths:
        candidate = Path(str(item).replace("\\", "/"))
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"unsafe allowed_path '{item}' in {task_file.name}")

    raw_deps = meta.get("depends_on") or []
    if isinstance(raw_deps, str):
        raw_deps = [raw_deps]
    if task_file.stem in [str(x) for x in raw_deps]:
        raise ValueError(f"task cannot depend on itself: {task_file.name}")

    simulate_failure = int(meta.get("simulate_failure", 0) or 0)
    run_id = str(meta.get("run_id", "") or "")
    task_type = str(meta.get("type", TASK_TYPE_CODE_CHANGE) or TASK_TYPE_CODE_CHANGE).upper()
    if task_type not in VALID_TASK_TYPES:
        raise ValueError(
            f"invalid task type '{task_type}' in {task_file.name} "
            f"(expected one of {', '.join(VALID_TASK_TYPES)})"
        )

    return Task(
        id=task_file.stem,
        prompt_file=task_file,
        project=project.id,
        project_path=project.path,
        base_branch=project.default_branch,
        prompt=body,
        type=task_type,
        title=_extract_title(body, task_file.stem),
        test_command=project.test_command,
        agent=project.agent,
        model=project.model,
        allowed_paths=list(raw_paths),
        depends_on=list(raw_deps),
        simulate_failure=simulate_failure,
        run_id=run_id,
        allow_empty=bool(meta.get("allow_empty", False)),
        resume=bool(meta.get("resume", True)),
        dirty_base_policy=(
            project.dirty_base_policy
            or config.dirty_base_policy
            or "refuse"
        ).strip().lower(),
    )


def _extract_title(body: str, fallback: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return fallback
