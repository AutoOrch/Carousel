"""REPORT node — writes projects/_runs/reports/doc-organize-*.md."""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path

from config import DocumentsConfig
from runtime.assets import run_reports_dir

from document.schemas import ArchiveResult


def generate_report(
    results: list[ArchiveResult], docs_cfg: DocumentsConfig, mode: str = "dry-run"
) -> Path:
    reports = run_reports_dir()
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"doc-organize-{time.strftime('%Y%m%d-%H%M%S')}.md"

    counts: dict[str, int] = defaultdict(int)
    for result in results:
        counts[result.status] += 1

    by_project: dict[str, list[ArchiveResult]] = defaultdict(list)
    for result in results:
        if result.status in ("archived", "duplicate"):
            by_project[result.plan.project or "(未知项目)"].append(result)

    lines = [
        "# 文档整理报告",
        "",
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"模式：{mode}",
        "",
        "## 总览",
        "",
        f"扫描文档：{len(results)}",
        f"成功归档：{counts['archived']}",
        f"重复文档：{counts['duplicate']}",
        f"待人工确认：{counts['review']}",
        f"失败：{counts['failed']}",
        "",
    ]

    for project, items in by_project.items():
        lines.append(f"## {project}")
        lines.append("")
        archived = [r for r in items if r.status == "archived"]
        duplicates = [r for r in items if r.status == "duplicate"]
        if archived:
            lines.append("### 归档")
            lines.append("")
            for r in archived:
                lines.append(
                    f"- {r.plan.source} → {r.plan.target}"
                    f"（confidence {r.plan.confidence:.2f} · {r.plan.reason}）"
                )
            lines.append("")
        if duplicates:
            lines.append("### 重复")
            lines.append("")
            for r in duplicates:
                lines.append(f"- {r.plan.source} → 与 {r.plan.duplicate_of} SHA256 相同")
            lines.append("")

    review = [r for r in results if r.status == "review"]
    if review:
        lines.append("## 待人工确认")
        lines.append("")
        for r in review:
            lines.append(f"- {r.plan.source} → {r.plan.target}")
            lines.append("")
            lines.append(f"  候选/原因：{r.plan.reason}")
            lines.append("")

    failed = [r for r in results if r.status == "failed"]
    if failed:
        lines.append("## 失败")
        lines.append("")
        for r in failed:
            lines.append(f"- {r.plan.source}：{r.detail}")
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
