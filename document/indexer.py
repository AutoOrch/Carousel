"""INDEX node — regenerates per-project INDEX.md (p11 §9④, §13).

MANIFEST.json is the source of truth (written by the executor); INDEX.md
is a rendered view of it, grouped by the fixed taxonomy.
"""

from __future__ import annotations

import json
from pathlib import Path

from config import DocumentsConfig
from log import get_logger
from runtime.assets import documents_dir

from document.planner import manifest_path
from document.schemas import DOC_CATEGORIES

logger = get_logger(__name__)


def update_index(project: str, docs_cfg: DocumentsConfig) -> Path | None:
    doc_dir = documents_dir(project, docs_cfg.archive_root)
    mpath = manifest_path(docs_cfg, project)
    if not mpath.exists():
        return None
    try:
        data = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"[index] cannot read {mpath}: {exc}")
        return None

    documents = [
        d for d in data.get("documents", [])
        if isinstance(d, dict) and d.get("path")
    ]

    by_category: dict[str, list[dict]] = {}
    for entry in documents:
        by_category.setdefault(str(entry.get("type", "archive")), []).append(entry)

    lines = [
        f"# {project} Documentation",
        "",
        f"> {len(documents)} documents · maintained by document-organizer (p11)",
        "",
    ]
    for category in DOC_CATEGORIES:
        items = by_category.get(category)
        if not items:
            continue
        lines.append(f"## {category.title()}")
        lines.append("")
        for entry in items:
            rel = str(entry["path"])
            name = rel.rsplit("/", 1)[-1]
            stamp = entry.get("created_at", "")
            short = str(entry.get("sha256", ""))[:8]
            lines.append(f"- [{name}]({rel}) — {stamp} ({short})")
        lines.append("")

    path = doc_dir / "INDEX.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
