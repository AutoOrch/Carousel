"""Generate ARCHITECTURE_INIT tasks for projects (p10 §16).

Usage:
    python architecture_init.py --project new-api
    python architecture_init.py --all
    python architecture_init.py --list

Writes ``prompts/arch-init-<project_id>.md`` task files; execution goes
through the normal watcher (dependency scheduling, retry loop, arch lock).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from config import load_config
from log import get_logger, setup_logging
from runtime.task_store import TaskStore
from config import PROMPTS_DIR, get_project

logger = get_logger(__name__)


TASK_TEMPLATE = """---
type: ARCHITECTURE_INIT
project: "{project}"
depends_on: []
allowed_paths: []
---

# 初始化 {project} 项目架构

## 目标

分析当前仓库，生成项目现状架构基线。

## 要求

1. 分析 README、入口文件、配置文件和主要模块。
2. 识别 {max_nodes} 个左右核心组件。
3. 展示主要请求链路。
4. 标记数据库、缓存、外部服务和边界。
5. 不要将每个文件都画成节点。
6. 所有组件关系必须有代码或配置依据。
7. 记录当前 Git commit。
8. 生成 Archify Typed JSON IR。
9. 执行 Archify validate。
10. 执行 Archify render。
11. 输出模块、依赖和风险分析。

## 输出

- architecture.json
- architecture.html
- metadata.json
- modules.md / dependencies.md / risks.md
"""


def generate_task_file(
    project_id: str, project, force: bool = False, project_ref: str = ""
) -> Path | None:
    """Write the arch-init task file.

    ``project_ref`` is what goes into the front-matter: a config.yaml ID for
    registered projects, or the normalised absolute path for ad-hoc path
    projects (the resolver cannot resolve a bare directory name that is not
    in config.yaml — same rule as planner.py).
    """
    task_id = f"arch-init-{project_id}"
    target = PROMPTS_DIR / f"{task_id}.md"
    if target.exists() and not force:
        logger.info(f"[arch-init] task already queued: {target.name}")
        return None
    ref = project_ref or project_id
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(
        TASK_TEMPLATE.format(
            project=ref,
            max_nodes=project.architecture.max_core_nodes if project else 12,
        ),
        encoding="utf-8",
        newline="",
    )
    logger.info(f"[arch-init] wrote {target.name} (project ref: {ref})")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate ARCHITECTURE_INIT tasks"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--project", help="project ID or repo path")
    source.add_argument("--all", action="store_true",
                        help="all projects with architecture.enabled: true")
    source.add_argument("--list", action="store_true", help="show status")
    parser.add_argument("--force", action="store_true",
                        help="re-generate even if the task file exists")
    args = parser.parse_args()

    config = load_config()
    setup_logging()

    if args.list:
        store = TaskStore()
        try:
            for pid, project in config.projects.items():
                snap = store.get_latest_architecture(pid)
                print(
                    f"{pid}: enabled={project.architecture.enabled} "
                    f"baseline={'yes (' + snap['repository_revision'][:10] + ')' if snap else 'no'}"
                )
        finally:
            store.close()
        return

    store = TaskStore()
    written: list[Path] = []
    try:
        if args.all:
            targets = [
                (pid, p, pid) for pid, p in config.projects.items()
                if p.architecture.enabled
            ]
            if not targets:
                logger.warning(
                    "[arch-init] no projects with architecture.enabled: true"
                )
                return
        else:
            try:
                project = get_project(config, args.project)
            except ValueError as exc:
                logger.error(f"Invalid --project: {exc}")
                raise SystemExit(1)
            if project is None:
                logger.error(f"Unknown project: {args.project}")
                raise SystemExit(1)
            # Config-registered: reference by ID.  Path-resolved ad-hoc
            # projects must reference the path (the resolver cannot resolve
            # a bare directory name that is not in config.yaml).
            ref = (
                args.project
                if args.project in config.projects
                else args.project.replace("\\", "/")
            )
            targets = [(project.id, project, ref)]

        for pid, project, ref in targets:
            # Skip when a valid baseline already exists (p10 §25).
            snap = store.get_latest_architecture(pid)
            if snap and not args.force:
                logger.info(
                    f"[arch-init] {pid}: baseline already exists "
                    f"(rev {snap['repository_revision'][:10]}) — skipping"
                )
                continue
            path = generate_task_file(pid, project, force=args.force, project_ref=ref)
            if path:
                written.append(path)
    finally:
        store.close()

    if written:
        logger.info(
            f"[arch-init] {len(written)} task(s) queued — run "
            "`python watcher.py --mode opencode --once` to execute"
        )


if __name__ == "__main__":
    main()
