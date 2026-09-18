"""P13 direct document validation and revision-staleness audit."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from config import get_project, load_config
from document.extractor import extract_one
from document.schemas import DocFile
from document.validator import git_snapshot, validate_claims
from runtime.task_store import TaskStore


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Validate documents against committed code")
    parser.add_argument("--project", required=True, help="config ID or Git repository path")
    parser.add_argument("--file", type=Path, help="document to validate without importing")
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--stale-only", action="store_true",
                        help="compare persisted validation revisions with current revision")
    parser.add_argument("--mark-stale", action="store_true",
                        help="persist stale status (requires --stale-only)")
    args = parser.parse_args()
    if args.mark_stale and not args.stale_only:
        parser.error("--mark-stale requires --stale-only")
    config = load_config()
    project = get_project(config, args.project)
    if project is None:
        parser.error(f"unknown project: {args.project}")
    snapshot = git_snapshot(project.path, args.revision)
    if args.stale_only:
        store = TaskStore()
        try:
            rows = store._conn.execute(
                """SELECT * FROM document_validations_p13
                   WHERE project_id=? AND revision<>? ORDER BY created_at""",
                (project.id, snapshot["revision"]),
            ).fetchall()
            result = [dict(row) for row in rows]
            marked = store.mark_document_validations_stale(
                project.id, snapshot["revision"]
            ) if args.mark_stale else 0
        finally:
            store.close()
        print(json.dumps({
            "project_id": project.id, "current_revision": snapshot["revision"],
            "stale": result, "marked": marked,
        }, ensure_ascii=False, indent=2))
        return
    if not args.file:
        parser.error("--file is required unless --stale-only is used")
    path = args.file.resolve()
    if not path.is_file():
        parser.error(f"document not found: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    item = {
        "source_file_id": f"adhoc-{digest[:20]}", "project_id": project.id,
        "original_path": str(path), "relative_path": path.name,
    }
    extracted = extract_one(
        DocFile(path, path.name, path.suffix.lower(), path.stat().st_size),
        config.documents.content_preview_chars,
    )
    project_data = {
        "id": project.id, "path": str(project.path),
        "requested_revision": args.revision,
    }
    claims, evidence, validations, snapshot = validate_claims(
        item, extracted.text, project_data
    )
    print(json.dumps({
        "document": str(path), "project_id": project.id, "snapshot": snapshot,
        "claims": claims, "evidence": evidence, "validations": validations,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
