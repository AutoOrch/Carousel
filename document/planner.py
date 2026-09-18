"""PLAN node — builds structured DocumentPlans (p11 §2, §13).

The plan is derived deterministically from the classification plus the
per-project MANIFEST.json hash index — the LLM never produces file paths
on its own.  Decision order per document (p11 §13):

    1. SHA256 already in any project MANIFEST (or earlier in this batch)
       -> skip_duplicate
    2. project unknown or confidence < threshold
       -> review (doc/review/, never auto-archived)
    3. otherwise -> archive under
       <archive_root>/<project>/documents/<category>/<name>
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config import Config, DocumentsConfig
from runtime.assets import documents_dir
from log import get_logger

from document.schemas import (
    ACTION_ARCHIVE,
    ACTION_REVIEW,
    ACTION_SKIP_DUPLICATE,
    DocumentPlan,
)

logger = get_logger(__name__)

MANIFEST_NAME = "MANIFEST.json"


def manifest_path(docs_cfg: DocumentsConfig, project: str) -> Path:
    return documents_dir(project, docs_cfg.archive_root) / MANIFEST_NAME


def load_hash_index(
    docs_cfg: DocumentsConfig, config: Config
) -> dict[str, tuple[str, str]]:
    """Map sha256 -> (project_id, relative doc path) across all projects."""
    index: dict[str, tuple[str, str]] = {}
    project_ids = set(config.projects)
    if docs_cfg.archive_root.is_dir():
        project_ids.update(
            item.name
            for item in docs_cfg.archive_root.iterdir()
            if item.is_dir()
            and item.name != "_runs"
            and (documents_dir(item.name, docs_cfg.archive_root) / MANIFEST_NAME).is_file()
        )
    for pid in sorted(project_ids):
        path = manifest_path(docs_cfg, pid)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"[plan] cannot read {path}: {exc}")
            continue
        for entry in data.get("documents", []):
            if isinstance(entry, dict) and entry.get("sha256"):
                index[entry["sha256"]] = (pid, str(entry.get("path", "")))
    return index


def build_plans(
    entries: list[dict[str, Any]],
    config: Config,
    docs_cfg: DocumentsConfig,
) -> list[DocumentPlan]:
    hash_index = load_hash_index(docs_cfg, config)
    # Intra-batch dedup: identical files dropped in the same run.
    planned: dict[str, Path] = {}

    plans: list[DocumentPlan] = []
    for entry in entries:
        ex = entry["extracted"]
        cl = entry["classification"]
        source = f"{docs_cfg.input_dir.name}/{ex.doc.name}"

        # 1. Hash dedup takes precedence over classification (p11 §13).
        duplicate = hash_index.get(ex.sha256)
        if duplicate is not None:
            dup_project, dup_path = duplicate
            plans.append(_skip_duplicate(
                entry, docs_cfg, source,
                f"projects/{dup_project}/documents/{dup_path}",
            ))
            continue
        if ex.sha256 in planned:
            plans.append(_skip_duplicate(
                entry, docs_cfg, source, str(planned[ex.sha256])
            ))
            continue

        # 2. Safety gate — low confidence or unknown project goes to review.
        if not cl.project or cl.confidence < docs_cfg.confidence_threshold:
            candidates = "、".join(
                f"{pid} {score:.2f}" for pid, score in cl.candidates[:3]
            ) or "无"
            plans.append(DocumentPlan(
                source=source,
                file_path=ex.doc.path,
                sha256=ex.sha256,
                project=cl.project,
                category=cl.category,
                target=docs_cfg.review_dir / ex.doc.name,
                action=ACTION_REVIEW,
                reason=f"{cl.reason}（候选：{candidates}）",
                confidence=cl.confidence,
            ))
            continue

        # 3. Archive — name conflicts resolved by hash, never _1/_final (p11 §11).
        target = (
            documents_dir(cl.project, docs_cfg.archive_root) / cl.category / ex.doc.name
        )
        if target.exists():
            target = target.with_name(
                f"{target.stem}-{ex.sha256[:8]}{target.suffix}"
            )
        planned[ex.sha256] = target
        plans.append(DocumentPlan(
            source=source,
            file_path=ex.doc.path,
            sha256=ex.sha256,
            project=cl.project,
            category=cl.category,
            target=target,
            action=ACTION_ARCHIVE,
            reason=cl.reason,
            confidence=cl.confidence,
        ))

    return plans


def _skip_duplicate(
    entry: dict[str, Any],
    docs_cfg: DocumentsConfig,
    source: str,
    duplicate_of: str,
) -> DocumentPlan:
    ex = entry["extracted"]
    cl = entry["classification"]
    return DocumentPlan(
        source=source,
        file_path=ex.doc.path,
        sha256=ex.sha256,
        project=cl.project,
        category=cl.category,
        # Duplicates are parked in processed/documents/ — originals are
        # never deleted and never re-scanned (p11 §11).
        target=docs_cfg.processed_dir / ex.doc.name,
        action=ACTION_SKIP_DUPLICATE,
        reason=f"SHA256 与 {duplicate_of} 相同",
        confidence=cl.confidence,
        duplicate_of=duplicate_of,
    )
