"""P13 rebuild reviewable topic summaries from a project's document manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

from config import get_project, load_config
from document.consolidator import build_clusters, content_terms, write_summaries
from document.extractor import extract_one
from document.schemas import DocFile
from runtime.assets import documents_dir
from runtime.task_store import TaskStore


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Create P13 topic summary drafts")
    parser.add_argument("--project", required=True)
    parser.add_argument("--topic", default="")
    parser.add_argument("--apply", action="store_true",
                        help="write DRAFT summaries; without this flag the command is read-only")
    args = parser.parse_args()
    config = load_config()
    project = get_project(config, args.project)
    if project is None:
        parser.error(f"unknown project: {args.project}")
    root = documents_dir(project.id)
    manifest_path = root / "MANIFEST.json"
    if not manifest_path.is_file():
        parser.error(f"document manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items: list[dict] = []
    for row in manifest.get("documents") or []:
        topic = str(row.get("topic") or "general")
        if args.topic and topic != args.topic:
            continue
        path = (root / str(row.get("path") or "")).resolve()
        if root.resolve() not in path.parents or not path.is_file():
            continue
        extracted = extract_one(
            DocFile(path, path.name, path.suffix.lower(), path.stat().st_size),
            config.documents.content_preview_chars,
        )
        items.append({
            "source_file_id": f"manifest-{project.id}-{row.get('id')}",
            "project_id": project.id, "topic": topic,
            "title": extracted.title, "relative_path": str(row.get("path")),
            "category": row.get("type", "archive"), "status": "ARCHIVED",
            "sha256": row.get("sha256", ""),
            "normalized_hash": row.get("normalized_hash", ""),
            "content_terms": content_terms(extracted.text),
            "actions": {"consolidate": True},
        })
    run_id = (
        f"summary-{datetime.now().strftime('%Y%m%d-%H%M%S')}-"
        f"{uuid.uuid4().hex[:6]}"
    )
    clusters = build_clusters(items, run_id)
    if not args.apply:
        print(json.dumps({
            "mode": "plan-only", "project_id": project.id,
            "topics": [{"topic": c["topic"], "members": c["members"]} for c in clusters],
        }, ensure_ascii=False, indent=2))
        return
    summaries = write_summaries(clusters, items, [], config.documents.archive_root)
    store = TaskStore()
    try:
        for summary in summaries:
            store.register_artifact(
                "DOCUMENT_SUMMARY_DRAFT", summary["path"], run_id=run_id,
                content_hash=summary["content_hash"],
            )
        package = {
            "import_run_id": run_id, "status": "COMPLETED", "mode": "summary",
            "plan_hash": f"sha256:{hashlib.sha256(run_id.encode()).hexdigest()}",
            "plan": {"sources": [], "effective_config": {"topic": args.topic}},
            "items": [], "claims": [], "evidence": [], "validations": [],
            "repairs": [], "relations": [], "clusters": clusters,
            "summaries": summaries, "stats": {"summaries": len(summaries)},
        }
        store.save_document_import(package)
    finally:
        store.close()
    print(json.dumps({"run_id": run_id, "summaries": summaries}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
