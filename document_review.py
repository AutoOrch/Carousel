"""Confirm or reclassify a document parked in doc/review and requeue it."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from config import get_project, load_config
from document.schemas import DOC_CATEGORIES
from document.utils import sha256_file
from runtime.task_store import TaskStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Requeue a reviewed document")
    parser.add_argument("--file", required=True, help="file name under documents.review_dir")
    parser.add_argument("--project", required=True, help="configured project id")
    parser.add_argument("--category", required=True, choices=DOC_CATEGORIES)
    parser.add_argument("--reason", default="人工复核确认")
    args = parser.parse_args()

    config = load_config()
    project = get_project(config, args.project)
    if project is None or project.id not in config.projects:
        raise SystemExit(f"unknown configured project: {args.project}")
    source = (config.documents.review_dir / args.file).resolve()
    review_root = config.documents.review_dir.resolve()
    if source.parent != review_root or not source.is_file():
        raise SystemExit(f"review file not found or unsafe: {args.file}")

    digest = sha256_file(source)
    decision_path = review_root / "decisions.json"
    data = {"version": 1, "decisions": {}}
    if decision_path.exists():
        data = json.loads(decision_path.read_text(encoding="utf-8"))
    data.setdefault("decisions", {})[digest] = {
        "project": project.id,
        "category": args.category,
        "reason": args.reason,
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "source_name": source.name,
    }
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    temp = decision_path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(decision_path)

    target = (config.documents.input_dir / source.name).resolve()
    if target.parent != config.documents.input_dir.resolve() or target.exists():
        raise SystemExit(f"input target already exists or unsafe: {target}")
    source.replace(target)
    store = TaskStore()
    try:
        try:
            store.requeue_document_review(source.name, project.id, args.category, str(target))
        except Exception:
            if target.exists() and not source.exists():
                target.replace(source)
            raise
    finally:
        store.close()
    print(f"requeued {source.name} -> {project.id}/{args.category}")


if __name__ == "__main__":
    main()
