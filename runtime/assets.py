"""Canonical project-scoped runner assets (P11/P12)."""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from config import ARCHITECTURE_ROOT, DATA_ROOT, PROJECTS_ROOT, REPORTS_DIR, ROOT, WORKTREES_DIR


def _safe(project_id: str) -> str:
    value = str(project_id).strip()
    if not value or value in (".", "..") or "/" in value or "\\" in value:
        raise ValueError(f"unsafe project id: {project_id!r}")
    return value


def project_root(project_id: str, projects_root: Path | None = None) -> Path:
    return (projects_root or PROJECTS_ROOT) / _safe(project_id)


def reports_dir(project_id: str) -> Path:
    return project_root(project_id) / "reports"


def task_report_path(project_id: str, task_id: str) -> Path:
    return reports_dir(project_id) / f"{task_id}.md"


def run_reports_dir() -> Path:
    return PROJECTS_ROOT / "_runs" / "reports"


def requirement_runs_dir() -> Path:
    return PROJECTS_ROOT / "_runs" / "requirement-runs"


def project_requirement_runs_dir(project_id: str) -> Path:
    return project_root(project_id) / "requirement-runs"


def worktree_path(project_id: str, task_id: str) -> Path:
    return project_root(project_id) / "worktrees" / task_id


def architecture_dir(project_id: str) -> Path:
    return project_root(project_id) / "architecture"


def documents_dir(project_id: str, projects_root: Path | None = None) -> Path:
    """Return the canonical collected-document directory for a project."""
    return project_root(project_id, projects_root) / "documents"


def document_derived_dir(project_id: str, projects_root: Path | None = None) -> Path:
    return documents_dir(project_id, projects_root) / "_derived"


def document_import_runs_dir() -> Path:
    return PROJECTS_ROOT / "_runs" / "imports"


def _asset_digest(path: Path) -> str:
    digest = hashlib.sha256()
    files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    for item in files:
        digest.update(item.relative_to(path.parent if path.is_file() else path).as_posix().encode())
        digest.update(item.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def migrate_legacy_assets(task_rows: list[dict], project_ids: list[str]) -> list[str]:
    """Idempotently move known legacy assets into project-scoped storage."""
    changes: list[str] = []
    records: list[dict[str, str]] = []
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    # Document runs can be launched independently of the task watcher.  Find
    # legacy per-project ``doc`` directories as well as caller-known projects
    # so the canonical ``documents`` name is applied on either entry point.
    known_ids = {_safe(pid) for pid in project_ids if pid}
    known_ids.update(
        child.name
        for child in PROJECTS_ROOT.iterdir()
        if child.is_dir() and child.name != "_runs" and (child / "doc").is_dir()
    )
    for pid in sorted(known_ids):
        for old, new in (
            (WORKTREES_DIR / pid, project_root(pid) / "worktrees"),
            (ARCHITECTURE_ROOT / "projects" / pid, architecture_dir(pid)),
            (project_root(pid) / "doc", documents_dir(pid)),
        ):
            if old.exists() and not new.exists():
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), str(new))
                changes.append(f"{old} -> {new}")
                records.append({"old_path": str(old), "new_path": str(new),
                                "digest": _asset_digest(new),
                                "rollback": f"{new} -> {old}"})
    for row in task_rows:
        pid, task_id = row.get("project"), row.get("task_id")
        if not pid or not task_id:
            continue
        old = REPORTS_DIR / f"{task_id}.md"
        new = task_report_path(pid, task_id)
        if old.exists() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(new))
            changes.append(f"{old} -> {new}")
            records.append({"old_path": str(old), "new_path": str(new),
                            "digest": _asset_digest(new),
                            "rollback": f"{new} -> {old}"})
    if REPORTS_DIR.exists():
        for old in REPORTS_DIR.iterdir():
            if not old.is_file():
                continue
            new = run_reports_dir() / old.name
            if not new.exists():
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), str(new))
                changes.append(f"{old} -> {new}")
                records.append({"old_path": str(old), "new_path": str(new),
                                "digest": _asset_digest(new),
                                "rollback": f"{new} -> {old}"})
    legacy_runs = DATA_ROOT / "runtime" / "requirement_runs"
    if legacy_runs.exists():
        target_runs = requirement_runs_dir()
        target_runs.mkdir(parents=True, exist_ok=True)
        for old in legacy_runs.iterdir():
            new = target_runs / old.name
            if not new.exists():
                shutil.move(str(old), str(new))
                changes.append(f"{old} -> {new}")
                records.append({"old_path": str(old), "new_path": str(new),
                                "digest": _asset_digest(new),
                                "rollback": f"{new} -> {old}"})
    report_path = PROJECTS_ROOT / "_runs" / "asset-migration-p12.json"
    if not records and not report_path.exists():
        # Older P12 builds moved assets before the versioned report existed.
        # Reconstruct verifiable old/new mappings from canonical targets.
        for row in task_rows:
            pid, task_id = row.get("project"), row.get("task_id")
            if not pid or not task_id:
                continue
            old = REPORTS_DIR / f"{task_id}.md"
            new = task_report_path(pid, task_id)
            if new.exists() and not old.exists():
                records.append({"old_path": str(old), "new_path": str(new),
                                "digest": _asset_digest(new),
                                "rollback": f"{new} -> {old}",
                                "evidence": "reconstructed-after-migration"})
        for pid in project_ids:
            for old, new in (
                (WORKTREES_DIR / pid, project_root(pid) / "worktrees"),
                (ARCHITECTURE_ROOT / "projects" / pid, architecture_dir(pid)),
            ):
                if new.exists() and not old.exists():
                    records.append({"old_path": str(old), "new_path": str(new),
                                    "digest": _asset_digest(new),
                                    "rollback": f"{new} -> {old}",
                                    "evidence": "reconstructed-after-migration"})
    if records:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {"version": "p12-v1", "batches": []}
        if report_path.exists():
            try:
                existing = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        existing.setdefault("version", "p12-v1")
        existing.setdefault("batches", []).append({
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "assets": records,
        })
        temp = report_path.with_suffix(".tmp")
        temp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(report_path)
    return changes
