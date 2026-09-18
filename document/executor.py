"""ARCHIVE node — the only place files are moved (p11 §10).

Pure Python executor: mkdir / move / hash verification / audit records /
MANIFEST.json append.  The LLM never executes filesystem operations, which
keeps the pipeline recoverable, auditable and rollback-able:

* every move is recorded in processed/documents/<stem>-<sha8>.json
* MANIFEST.json is updated per project under a lock, atomically
* the moved file's sha256 is re-verified before the manifest append
"""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from config import DocumentsConfig
from runtime.assets import documents_dir
from log import get_logger

from document.planner import manifest_path
from document.schemas import (
    ACTION_ARCHIVE,
    ACTION_REVIEW,
    ACTION_SKIP_DUPLICATE,
    ArchiveResult,
    DocumentPlan,
)
from document.utils import sha256_file

logger = get_logger(__name__)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _project_lock(project: str) -> threading.Lock:
    with _locks_guard:
        if project not in _locks:
            _locks[project] = threading.Lock()
        return _locks[project]


def execute_plans(
    plans: list[DocumentPlan], docs_cfg: DocumentsConfig
) -> list[ArchiveResult]:
    """Execute plans in parallel — different files never conflict."""
    if not plans:
        return []
    workers = max(1, min(docs_cfg.max_workers, len(plans)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(
            pool.map(lambda plan: _execute_one(plan, docs_cfg), plans)
        )


def _execute_one(plan: DocumentPlan, docs_cfg: DocumentsConfig) -> ArchiveResult:
    journal = _write_journal(plan, docs_cfg, "INTENT")
    try:
        if plan.action == ACTION_REVIEW:
            _update_journal(journal, "INTENT", final_path=str(plan.target))
            result = _move(plan, plan.target, "review", f"待人工确认 → {plan.target}")
            _update_journal(journal, "COMMITTED", final_path=str(plan.target))
            return result

        if plan.action == ACTION_SKIP_DUPLICATE:
            target = _unique_path(
                docs_cfg.processed_dir / plan.file_path.name, plan.sha256
            )
            _update_journal(journal, "INTENT", final_path=str(target))
            result = _move(plan, target, "duplicate", f"重复文件（{plan.duplicate_of}）")
            _update_journal(journal, "MOVED", final_path=str(target))
            try:
                _write_audit(plan, target, docs_cfg)
                _update_journal(journal, "COMMITTED", final_path=str(target))
                return result
            except Exception:
                _rollback_move(target, plan.file_path)
                _update_journal(journal, "ROLLED_BACK", error="audit failed")
                raise

        if plan.action == ACTION_ARCHIVE:
            plan.target.parent.mkdir(parents=True, exist_ok=True)
            target = _unique_path(plan.target, plan.sha256)
            _update_journal(journal, "INTENT", final_path=str(target))
            result = _move(plan, target, "archived", str(target))
            _update_journal(journal, "MOVED", final_path=str(target))
            if result.status == "archived":
                try:
                    if sha256_file(target) != plan.sha256:
                        raise ValueError("moved file failed sha256 verification")
                    _append_manifest(plan, target, docs_cfg)
                    _write_audit(plan, target, docs_cfg)
                    _update_journal(journal, "COMMITTED", final_path=str(target))
                except Exception:
                    _remove_manifest_hash(plan, docs_cfg)
                    _rollback_move(target, plan.file_path)
                    _update_journal(journal, "ROLLED_BACK", error="publish failed")
                    raise
            return result

        return ArchiveResult(plan, "failed", f"unknown action: {plan.action}")
    except Exception as exc:
        _update_journal(journal, "FAILED", error=str(exc))
        logger.error(f"[archive] {plan.source} failed: {exc}")
        return ArchiveResult(plan, "failed", str(exc))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _journal_dir(docs_cfg: DocumentsConfig) -> Path:
    return docs_cfg.processing_dir / ".journal"


def _write_journal(
    plan: DocumentPlan, docs_cfg: DocumentsConfig, status: str
) -> Path:
    directory = _journal_dir(docs_cfg)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"docop-{uuid.uuid4().hex}.json"
    data = {
        "version": 1,
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(plan.file_path),
        "display_source": plan.source,
        "target": str(plan.target),
        "final_path": "",
        "sha256": plan.sha256,
        "action": plan.action,
        "project": plan.project,
        "category": plan.category,
        "reason": plan.reason,
        "confidence": plan.confidence,
        "duplicate_of": plan.duplicate_of,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _update_journal(path: Path, status: str, **fields: str) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(fields)
        data["status"] = status
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    except Exception as exc:
        logger.error(f"[archive] operation journal update failed: {exc}")


def reconcile_journals(docs_cfg: DocumentsConfig) -> dict[str, int]:
    """Finish or roll back document publishes interrupted by process exit."""
    stats = {"committed": 0, "rolled_back": 0, "lost": 0}
    directory = _journal_dir(docs_cfg)
    if not directory.exists():
        return stats
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("status") == "COMMITTED":
                continue
            source = Path(str(data.get("source") or ""))
            target = Path(str(data.get("final_path") or data.get("target") or ""))
            expected = str(data.get("sha256") or "")
            if target.is_file() and sha256_file(target) == expected:
                plan = DocumentPlan(
                    source=str(data.get("display_source") or source.name),
                    file_path=source,
                    sha256=expected,
                    project=str(data.get("project") or ""),
                    category=str(data.get("category") or "archive"),
                    target=target,
                    action=str(data.get("action") or ""),
                    reason=str(data.get("reason") or "recovered operation"),
                    confidence=float(data.get("confidence") or 0),
                    duplicate_of=str(data.get("duplicate_of") or ""),
                )
                if plan.action == ACTION_ARCHIVE:
                    _append_manifest(plan, target, docs_cfg)
                    _write_audit(plan, target, docs_cfg)
                elif plan.action == ACTION_SKIP_DUPLICATE:
                    _write_audit(plan, target, docs_cfg)
                _update_journal(path, "COMMITTED", final_path=str(target), recovered="true")
                stats["committed"] += 1
            elif source.is_file():
                _update_journal(path, "ROLLED_BACK", recovered="true")
                stats["rolled_back"] += 1
            else:
                _update_journal(path, "LOST", error="source and target are missing")
                stats["lost"] += 1
        except Exception as exc:
            _update_journal(path, "FAILED", error=f"reconciliation: {exc}")
            stats["lost"] += 1
    return stats

def _move(
    plan: DocumentPlan, target: Path, status: str, detail: str
) -> ArchiveResult:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(plan.file_path), str(target))
    logger.info(f"[archive] {plan.source} -> {target} ({status})")
    return ArchiveResult(plan=plan, status=status, detail=detail, final_path=str(target))


def _rollback_move(source: Path, original: Path) -> None:
    """Best-effort compensation before reporting an archive operation failed."""
    if not source.exists():
        return
    original.parent.mkdir(parents=True, exist_ok=True)
    if not original.exists():
        shutil.move(str(source), str(original))


def _unique_path(target: Path, sha256: str) -> Path:
    """Disambiguate a name collision with the incoming hash (p11 §11)."""
    if not target.exists():
        return target
    return target.with_name(f"{target.stem}-{sha256[:8]}{target.suffix}")


def _append_manifest(
    plan: DocumentPlan, target: Path, docs_cfg: DocumentsConfig
) -> None:
    doc_dir = documents_dir(plan.project, docs_cfg.archive_root)
    mpath = manifest_path(docs_cfg, plan.project)

    with _project_lock(plan.project):
        data: dict = {"documents": []}
        if mpath.exists():
            try:
                loaded = json.loads(mpath.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, json.JSONDecodeError):
                logger.warning(f"[archive] manifest corrupted, rebuilding: {mpath}")

        documents = data.setdefault("documents", [])
        if any(
            isinstance(entry, dict) and entry.get("sha256") == plan.sha256
            for entry in documents
        ):
            return
        next_num = 1
        for entry in documents:
            if not isinstance(entry, dict):
                continue
            try:
                num = int(str(entry.get("id", "")).replace("doc-", ""))
                next_num = max(next_num, num + 1)
            except ValueError:
                continue

        documents.append({
            "id": f"doc-{next_num:03d}",
            "path": target.relative_to(doc_dir).as_posix(),
            "sha256": plan.sha256,
            "type": plan.category,
            "created_at": datetime.now(timezone.utc).date().isoformat(),
            "source": plan.source,
        })

        # Atomic write (tmp + replace) so a crash never corrupts the manifest.
        tmp = mpath.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(mpath)


def _remove_manifest_hash(plan: DocumentPlan, docs_cfg: DocumentsConfig) -> None:
    """Compensate a partially published manifest entry."""
    if not plan.project:
        return
    mpath = manifest_path(docs_cfg, plan.project)
    if not mpath.exists():
        return
    with _project_lock(plan.project):
        try:
            data = json.loads(mpath.read_text(encoding="utf-8"))
            docs = data.get("documents") or []
            data["documents"] = [d for d in docs if d.get("sha256") != plan.sha256]
            tmp = mpath.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(mpath)
        except Exception as exc:
            logger.error(f"[archive] manifest compensation failed: {exc}")


def _write_audit(
    plan: DocumentPlan, final_path: Path, docs_cfg: DocumentsConfig
) -> None:
    docs_cfg.processed_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": plan.source,
        "target": str(final_path),
        "sha256": plan.sha256,
        "action": plan.action,
        "project": plan.project,
        "category": plan.category,
        "confidence": plan.confidence,
        "reason": plan.reason,
    }
    path = docs_cfg.processed_dir / f"{plan.file_path.stem}-{plan.sha256[:8]}.json"
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
