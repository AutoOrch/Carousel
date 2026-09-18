"""P13 historical-folder inventory, frozen planning and copy-only apply."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import DATA_ROOT, ROOT, Config, DocumentImportProfile, get_project
from document.classifier import classify_one
from document.consolidator import (
    build_clusters, build_relations, content_terms, normalized_text_hash,
    write_summaries,
)
from document.extractor import extract_one
from document.indexer import update_index
from document.planner import load_hash_index
from document.schemas import DOC_CATEGORIES, DocFile
from document.utils import response_text, sha256_file
from document.validator import validate_claims
from opencode_client import OpenCodeClient
from runtime.assets import document_derived_dir, document_import_runs_dir, documents_dir
from runtime.task_store import TaskStore


_MANIFEST_LOCK = threading.Lock()
_TOPIC_SAFE = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_digest(value: dict) -> str:
    payload = dict(value)
    payload.pop("plan_hash", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _source_id(run_id: str, ordinal: int, path: Path) -> str:
    # Source roots are observations within an import run.  Including the run
    # and ordinal preserves provenance when the same folder is imported again.
    return _stable_id("source", run_id, ordinal, path.as_posix().lower())


def _stable_id(prefix: str, *parts: object) -> str:
    payload = ":".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(payload.encode()).hexdigest()[:20]}"


def _source_type(path: Path) -> str:
    lowered = path.as_posix().lower()
    if "/desktop" in lowered:
        return "desktop"
    if "/backup" in lowered or "备份" in lowered:
        return "backup"
    if (path / ".git").exists():
        return "old-repository"
    return "history-folder"


def _profile_dict(profile: DocumentImportProfile) -> dict[str, Any]:
    return {
        "actions": asdict(profile.actions),
        "recursive": profile.recursive,
        "copy_source": profile.copy_source,
        "repair_mode": profile.repair_mode,
        "review_required": profile.review_required,
    }


def effective_settings(
    config: Config, source_spec: dict[str, Any],
    cli_actions: dict[str, bool | None] | None = None,
    repair_mode: str | None = None,
) -> dict[str, Any]:
    """Resolve defaults < profile < source < CLI and validate dependencies."""
    imports = config.documents.imports
    profile_name = str(source_spec.get("profile") or "")
    if profile_name and profile_name not in imports.profiles:
        raise ValueError(f"unknown document import profile: {profile_name}")
    profile = imports.profiles.get(profile_name, imports.defaults)
    result = _profile_dict(profile)
    source_actions = source_spec.get("actions") or {}
    for name in ("validate", "repair", "consolidate", "merge"):
        if name in source_actions:
            result["actions"][name] = bool(source_actions[name])
        override = (cli_actions or {}).get(name)
        if override is not None:
            result["actions"][name] = bool(override)
    for key in ("recursive", "copy_source", "review_required"):
        if key in source_spec:
            result[key] = bool(source_spec[key])
    result["repair_mode"] = str(
        repair_mode or source_spec.get("repair_mode") or result["repair_mode"]
    )
    if result["repair_mode"] not in ("annotate", "rewrite-draft"):
        raise ValueError("repair_mode must be annotate or rewrite-draft")
    if result["actions"]["repair"] and not result["actions"]["validate"]:
        raise ValueError("repair=true requires validate=true")
    if result["actions"]["merge"] and not result["actions"]["validate"]:
        raise ValueError("merge=true requires validate=true")
    if not result["copy_source"]:
        raise ValueError("P13 v1 is copy-only; copy_source=false is not supported")
    result["profile"] = profile_name
    return result


def _iter_source_files(root: Path, recursive: bool, excluded: set[str]) -> Iterable[Path]:
    if root.is_file():
        if not root.is_symlink():
            yield root
        return
    if not recursive:
        for item in sorted(root.iterdir()):
            if item.is_file() and not item.is_symlink():
                yield item
        return
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name for name in dirs
            if name not in excluded and not (Path(current) / name).is_symlink()
        )
        for name in sorted(files):
            path = Path(current) / name
            if not path.is_symlink():
                yield path


def _topic(meta: dict, title: str, relative: Path) -> str:
    raw = str(meta.get("topic") or "").strip()
    if not raw and relative.parent != Path("."):
        raw = relative.parent.name
    if not raw:
        raw = title or relative.stem
    value = _TOPIC_SAFE.sub("-", raw).strip("-.")[:80]
    return value or f"topic-{hashlib.sha256(raw.encode()).hexdigest()[:8]}"


# Merge is folder-level: every merge-enabled document under one specified
# source folder and one project folds into a single archive copy, and one
# consolidated analysis document — grounded in the current code — is written
# to a user-visible path beside the source folder.

_MERGE_DOC_SUFFIX = "综合分析（合并版）"

MERGE_PROMPT_TEMPLATE = """你是文档合并分析 Agent，不是编码 Agent。禁止执行任何文件操作。

任务：阅读下列同一来源文件夹的多份历史文档，基于给出的当前代码分析证据，合并为一份综合分析文档。

要求：
- 输出一份完整、结构化的中文 Markdown 文档（含标题与章节）；
- 合并各文档中重复或互补的内容，形成单一连贯的分析，不要逐篇罗列；
- 与代码相关的结论注明来源文件与行号（引用下方代码证据）；
- 不要输出 verified、partially_verified、not_implemented、unverifiable 等校验状态标签；
- 不要编造代码中不存在的结论，代码证据优先于文档叙述。

代码仓库：{repo_label}

当前代码分析证据（文档主张 → 代码位置）：
{facts_block}

待合并文档：
{documents_block}

只输出合并后的 Markdown 文档内容，不要输出其它说明。"""


def _merge_output_default(source_path: Path) -> Path:
    """Default merged-document location: beside the source folder."""
    name = source_path.stem if source_path.is_file() else source_path.name
    return source_path.parent / f"{name}-{_MERGE_DOC_SUFFIX}.md"


def _freeze_merge_outputs(
    merge_groups: list[dict], sources: list[dict], merge_output: str | None,
) -> None:
    """Resolve and freeze the output path of every merge group."""
    if not merge_groups:
        return
    source_by_id = {
        row["source_root_id"]: Path(str(row["resolved_path"])) for row in sources
    }
    explicit = Path(merge_output).resolve() if merge_output else None
    if explicit and len(merge_groups) > 1:
        raise ValueError(
            "--merge-output 只能指定一个输出文件；多个来源文件夹各自生成合并文档"
        )
    for group in merge_groups:
        source_path = source_by_id.get(group.get("source_root_id") or "")
        output = explicit or _merge_output_default(
            source_path or Path(group["primary_id"])
        )
        if source_path is not None and (
            output == source_path or source_path in output.parents
        ):
            raise ValueError(
                f"合并输出不能写入只读来源目录内: {output}"
            )
        group["output_path"] = str(output)


def _merge_groups(items: list[dict], run_id: str) -> list[dict]:
    """Group merge-enabled documents by (source root, project).

    The primary member (first in plan order) is archived byte-for-byte;
    every other member folds into that archive copy and is recorded as
    MERGED.
    """
    order = {item["source_file_id"]: pos for pos, item in enumerate(items)}
    buckets: dict[tuple[str, str], list[dict]] = {}
    for item in items:
        if (
            item.get("actions", {}).get("merge")
            and item.get("project_id")
            and item.get("status") == "PLANNED"
        ):
            key = (item.get("source_root_id") or "", item["project_id"])
            buckets.setdefault(key, []).append(item)
    groups: list[dict] = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda item: order[item["source_file_id"]])
        primary = members[0]
        groups.append({
            "merge_id": _stable_id(
                "merge", run_id, *[m["source_file_id"] for m in members]
            ),
            "primary_id": primary["source_file_id"],
            "project_id": primary["project_id"],
            "source_root_id": primary.get("source_root_id") or "",
            "members": [m["source_file_id"] for m in members],
        })
    return groups


def _code_facts(claims: list[dict], evidence: list[dict]) -> list[str]:
    """Plain code-analysis facts: claim text → code location. No verdicts."""
    claim_by_id = {claim["claim_id"]: claim for claim in claims}
    facts: list[str] = []
    for row in evidence:
        claim = claim_by_id.get(row["claim_id"])
        if claim is None:
            continue
        location = f"{row.get('file_path', '?')}"
        if row.get("line_number"):
            location += f":{row['line_number']}"
        if row.get("symbol"):
            location += f"（{row['symbol']}）"
        facts.append(f"- {claim['text']} — {row.get('project_id', '')} {location}")
    return facts


def _deterministic_merged_content(
    group: dict, members: list[dict], merge_texts: dict[str, str],
    facts: list[str], repo_label: str,
) -> str:
    """Rules-mode consolidated document: sources, code facts, merged content."""
    primary = members[0]
    lines = [
        f"# {primary.get('title') or primary['relative_path']}"
        f"（{_MERGE_DOC_SUFFIX}）", "",
        f"> 由 {len(members)} 份源文档基于当前代码（{repo_label}）合并生成。", "",
        "## 来源文档", "",
    ]
    for item in members:
        lines.append(
            f"- {item['relative_path']}（{item.get('title') or ''}，"
            f"sha256 {item['sha256'][:8]}）"
        )
    lines.extend(["", "## 代码依据", ""])
    lines.extend(facts or ["- 本次合并未提取到代码证据。"])
    lines.extend(["", "## 合并内容", ""])
    for item in members:
        text = str(merge_texts.get(item["source_file_id"]) or "")
        lines.append(f"### {item.get('title') or item['relative_path']}")
        lines.append("")
        lines.append(text or "（无文本内容）")
        lines.append("")
    return "\n".join(lines)


def _llm_merged_content(
    members: list[dict], merge_texts: dict[str, str], facts: list[str],
    repo_label: str, opencode_url: str, run_id: str,
) -> str | None:
    """Ask the LLM to synthesize one consolidated analysis document."""
    documents_block = "\n\n".join(
        f"### 文档 {ordinal}：{item['relative_path']}"
        f"（{item.get('title') or ''}）\n\n"
        f"{merge_texts.get(item['source_file_id']) or '（无文本内容）'}"
        for ordinal, item in enumerate(members, 1)
    )
    prompt = MERGE_PROMPT_TEMPLATE.format(
        repo_label=repo_label,
        facts_block="\n".join(facts) or "-（无代码证据）",
        documents_block=documents_block,
    )
    try:
        client = OpenCodeClient(base_url=opencode_url, timeout=1800)
        session = client.create_session(
            title=f"doc-merge-{run_id}", directory=str(ROOT)
        )
        session_id = session["id"]
        try:
            response = client.send_message(session_id, prompt)
        finally:
            client.delete_session(session_id)
    except Exception as exc:
        return None
    content = response_text(response).strip()
    return content or None


def _write_merged_document(
    group: dict, items_by_id: dict[str, dict], merge_texts: dict[str, str],
    claims: list[dict], evidence: list[dict], projects_root: Path,
    mode: str = "dry-run", opencode_url: str = "",
) -> dict | None:
    """Write the consolidated analysis document of one merge group."""
    primary = items_by_id.get(group["primary_id"])
    if primary is None or primary.get("status") not in ("IMPORTED", "DUPLICATE"):
        return None
    members = [items_by_id[sid] for sid in group["members"] if sid in items_by_id]
    member_ids = set(group["members"])
    member_claims = [
        claim for claim in claims
        if claim.get("source_file_id") in member_ids
    ]
    member_claim_ids = {claim["claim_id"] for claim in member_claims}
    member_evidence = [
        row for row in evidence if row["claim_id"] in member_claim_ids
    ]
    facts = _code_facts(member_claims, member_evidence)
    revisions: dict[str, str] = {}
    for item in members:
        for snapshot in item.get("validation_snapshots") or []:
            revisions[snapshot["project_id"]] = snapshot["revision"]
    repo_label = (
        "；".join(f"{pid}@{revision}" for pid, revision in sorted(revisions.items()))
        or "未开启代码校验"
    )
    content = None
    generation = "rules"
    if mode == "opencode":
        content = _llm_merged_content(
            members, merge_texts, facts, repo_label, opencode_url,
            group["merge_id"],
        )
        if content is not None:
            generation = "llm"
    if content is None:
        content = _deterministic_merged_content(
            group, members, merge_texts, facts, repo_label
        )
    output = Path(str(group.get("output_path") or ""))
    if not output.is_absolute():
        output = (DATA_ROOT / output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(output)
    merged_into = primary.get("target_path") or primary.get("duplicate_of") or ""
    meta = {
        "merge_id": group["merge_id"],
        "project_id": group["project_id"],
        "primary_id": group["primary_id"],
        "source_root_id": group.get("source_root_id") or "",
        "merged_into": merged_into,
        "output_path": str(output),
        "members": group["members"],
        "generation": generation,
        "content_hash": f"sha256:{sha256_file(output)}",
        "validation_revisions": revisions,
        "status": "DRAFT",
    }
    directory = document_derived_dir(group["project_id"], projects_root) / "merged" / group["merge_id"]
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(directory / "merge-metadata.json", meta)
    return meta


def _resolve_projects(config: Config, refs: list[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for ref in refs:
        project = get_project(config, ref)
        if project is None:
            raise ValueError(f"unknown project: {ref}")
        result[project.id] = {
            "id": project.id, "ref": ref, "path": str(project.path),
            "branch": project.default_branch, "requested_revision": "HEAD",
        }
    return result


def build_import_plan(
    config: Config,
    source_specs: list[dict[str, Any]],
    *,
    project_refs: list[str] | None = None,
    cli_actions: dict[str, bool | None] | None = None,
    repair_mode: str | None = None,
    requested_revision: str = "HEAD",
    merge_output: str | None = None,
) -> dict[str, Any]:
    """Build an inventory and frozen action plan without writing any file or DB."""
    if not source_specs:
        raise ValueError("at least one --source or configured import source is required")
    run_id = f"import-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    refs = list(project_refs or [])
    projects = _resolve_projects(config, refs)
    for project in projects.values():
        project["requested_revision"] = requested_revision
    known_hashes = load_hash_index(config.documents, config)
    batch_hashes: dict[str, str] = {}
    known_normalized: dict[str, str] = {}
    if config.documents.archive_root.is_dir():
        for manifest in config.documents.archive_root.glob("*/documents/MANIFEST.json"):
            try:
                manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for row in manifest_data.get("documents") or []:
                norm = str(row.get("normalized_hash") or "")
                if norm:
                    known_normalized.setdefault(
                        norm, f"projects/{manifest.parent.parent.name}/documents/{row.get('path', '')}"
                    )
    batch_normalized: dict[str, str] = {}
    total_size = 0
    sources: list[dict] = []
    items: list[dict] = []
    excluded = set(config.documents.imports.exclude_dirs)

    for source_ordinal, spec in enumerate(source_specs, 1):
        raw_path = Path(str(spec.get("path") or ""))
        source_path = (raw_path if raw_path.is_absolute() else Path.cwd() / raw_path).resolve()
        if not source_path.exists():
            raise ValueError(f"source does not exist: {source_path}")
        settings = effective_settings(config, spec, cli_actions, repair_mode)
        source_refs = [str(x) for x in (spec.get("projects") or [])]
        if spec.get("project"):
            source_refs.insert(0, str(spec["project"]))
        if not source_refs:
            source_refs = refs
        source_projects = _resolve_projects(config, source_refs)
        for project in source_projects.values():
            project["requested_revision"] = requested_revision
        projects.update(source_projects)
        root = source_path if source_path.is_dir() else source_path.parent
        root_resolved = root.resolve()
        source_root_id = _source_id(run_id, source_ordinal, source_path)
        source_row = {
            "source_root_id": source_root_id,
            "input_path": str(spec.get("path") or source_path),
            "resolved_path": str(source_path),
            "source_is_file": source_path.is_file(),
            "source_type": str(spec.get("source_type") or _source_type(source_path)),
            "profile": settings.get("profile", ""),
            "project_refs": source_refs,
            "actions": settings["actions"],
            "recursive": settings["recursive"],
            "repair_mode": settings["repair_mode"],
            "review_required": settings["review_required"],
            "scan_error": "",
        }
        sources.append(source_row)
        try:
            discovered_paths = list(
                _iter_source_files(source_path, settings["recursive"], excluded)
            )
        except OSError as exc:
            source_row["scan_error"] = str(exc)
            continue
        for path in discovered_paths:
            if path.suffix.lower() not in config.documents.supported_extensions:
                continue
            resolved = path.resolve()
            try:
                relative = resolved.relative_to(root_resolved)
            except ValueError as exc:
                raise ValueError(f"source path escaped configured root: {resolved}") from exc
            size = resolved.stat().st_size
            if size > config.documents.imports.max_file_bytes:
                continue
            total_size += size
            if total_size > config.documents.imports.max_total_bytes:
                raise ValueError("document import exceeds max_total_bytes")
            if len(items) >= config.documents.imports.max_files:
                raise ValueError("document import exceeds max_files")
            digest = sha256_file(resolved)
            doc = DocFile(resolved, resolved.name, resolved.suffix.lower(), size)
            extracted = extract_one(doc, config.documents.content_preview_chars)
            normalized_hash = normalized_text_hash(extracted.text) if extracted.text else digest
            forced = ""
            if spec.get("project"):
                primary = get_project(config, str(spec["project"]))
                forced = primary.id if primary is not None else ""
            elif len(source_projects) == 1:
                forced = next(iter(source_projects))
            classification = classify_one(
                extracted, config, config.documents, "dry-run", forced_project=forced
            )
            pid = classification.project
            if pid and pid not in projects:
                project_ref = str(extracted.front_matter.get("project") or pid)
                resolved_project = get_project(config, project_ref)
                if resolved_project is not None and resolved_project.id == pid:
                    projects[pid] = {
                        "id": pid, "ref": project_ref,
                        "path": str(resolved_project.path),
                        "branch": resolved_project.default_branch,
                        "requested_revision": requested_revision,
                    }
            source_file_id = _stable_id(
                "source-file", run_id, source_root_id, relative.as_posix()
            )
            duplicate = known_hashes.get(digest)
            duplicate_of = (
                f"projects/{duplicate[0]}/documents/{duplicate[1]}" if duplicate
                else batch_hashes.get(digest, "")
            )
            duplicate_kind = "exact_duplicate" if duplicate_of else ""
            if not duplicate_of:
                normalized_candidate = (
                    known_normalized.get(normalized_hash)
                    or batch_normalized.get(normalized_hash, "")
                )
                if normalized_candidate:
                    duplicate_of = normalized_candidate
                    duplicate_kind = "normalized_duplicate"
            if duplicate_kind == "exact_duplicate":
                status = "DUPLICATE"
            elif not pid:
                status = "REVIEW"
            else:
                status = "PLANNED"
            category = classification.category
            target = ""
            if pid:
                target = str(documents_dir(pid, config.documents.archive_root) / category / resolved.name)
            item = {
                "source_file_id": source_file_id,
                "source_root_id": source_root_id,
                "original_path": str(resolved),
                "relative_path": relative.as_posix(),
                "sha256": digest,
                "normalized_hash": normalized_hash,
                "size_bytes": size,
                "modified_at": datetime.fromtimestamp(
                    resolved.stat().st_mtime, timezone.utc
                ).isoformat(),
                "project_id": pid,
                "project_ids": sorted(source_projects) if source_projects else ([pid] if pid else []),
                "category": category,
                "topic": _topic(extracted.front_matter, extracted.title, relative),
                "title": extracted.title,
                "target_path": target,
                "status": status,
                "detail": classification.reason,
                "duplicate_of": duplicate_of,
                "duplicate_kind": duplicate_kind,
                "actions": dict(settings["actions"]),
                "action_results": {
                    name: ("PLANNED" if enabled else "NOT_REQUESTED")
                    for name, enabled in settings["actions"].items()
                },
                "repair_mode": settings["repair_mode"],
                "review_required": settings["review_required"],
                "content_terms": content_terms(extracted.text),
                "extraction_note": extracted.note,
            }
            items.append(item)
            batch_hashes.setdefault(digest, source_file_id)
            batch_normalized.setdefault(normalized_hash, source_file_id)

    effective = {
        "source_count": len(sources),
        "cli_actions": cli_actions or {},
        "requested_revision": requested_revision,
        "limits": {
            "max_files": config.documents.imports.max_files,
            "max_file_bytes": config.documents.imports.max_file_bytes,
            "max_total_bytes": config.documents.imports.max_total_bytes,
        },
    }
    plan = {
        "version": "p13-v1",
        "import_run_id": run_id,
        "created_at": _now(),
        "status": "PLANNED",
        "copy_only": True,
        "effective_config": effective,
        "projects": projects,
        "sources": sources,
        "items": items,
        "merge_groups": _merge_groups(items, run_id),
        "stats": {
            "scanned": len(items),
            "bytes": total_size,
            "source_errors": sum(bool(source["scan_error"]) for source in sources),
            "planned": sum(i["status"] == "PLANNED" for i in items),
            "duplicate": sum(i["status"] == "DUPLICATE" for i in items),
            "review": sum(i["status"] == "REVIEW" for i in items),
            "merge_groups": 0,
        },
    }
    _freeze_merge_outputs(plan["merge_groups"], sources, merge_output)
    plan["stats"]["merge_groups"] = len(plan["merge_groups"])
    plan["plan_hash"] = _json_digest(plan)
    return plan


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _update_manifest(item: dict, target: Path, projects_root: Path) -> None:
    root = documents_dir(item["project_id"], projects_root)
    path = root / "MANIFEST.json"
    with _MANIFEST_LOCK:
        data = {"documents": []}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        docs = data.setdefault("documents", [])
        if any(row.get("sha256") == item["sha256"] for row in docs):
            return
        next_id = 1 + max([
            int(str(row.get("id", "doc-000")).replace("doc-", ""))
            for row in docs if str(row.get("id", "")).startswith("doc-")
            and str(row.get("id", ""))[4:].isdigit()
        ] or [0])
        docs.append({
            "id": f"doc-{next_id:03d}",
            "path": target.relative_to(root).as_posix(),
            "sha256": item["sha256"],
            "normalized_hash": item.get("normalized_hash", ""),
            "type": item.get("category", "archive"),
            "topic": item.get("topic", "general"),
            "status": "archived",
            "created_at": _now(),
            "source": item["original_path"],
            "source_root_id": item["source_root_id"],
            "source_relative_path": item["relative_path"],
        })
        _write_json(path, data)


def _write_repair(
    item: dict, claims: list[dict], validations: list[dict], evidence: list[dict],
    revision: str, projects_root: Path,
) -> dict:
    directory = document_derived_dir(item["project_id"], projects_root) / "repairs" / item["source_file_id"]
    directory.mkdir(parents=True, exist_ok=True)
    mode = item.get("repair_mode", "annotate")
    name = "annotations.md" if mode == "annotate" else "revised-draft.md"
    path = directory / name
    evidence_counts: dict[str, int] = {}
    for row in evidence:
        evidence_counts[row["claim_id"]] = evidence_counts.get(row["claim_id"], 0) + 1
    validation_by_claim = {row["claim_id"]: row for row in validations}
    lines = [
        f"# {item.get('title') or item['relative_path']} 修订草稿", "",
        "> 状态：DRAFT。原始文档未被修改，本草稿必须人工审核。", "",
        f"- repaired_from: `{item['source_file_id']}`",
        f"- source: `{item['original_path']}`",
        f"- revision: `{revision}`", f"- mode: `{mode}`", "",
        "## 校验与修订建议", "",
    ]
    if not claims:
        lines.append("未提取到可静态校验的主张，请人工检查原文。")
    for claim in claims:
        validation = validation_by_claim.get(claim["claim_id"], {})
        status = validation.get("status", "unverifiable")
        lines.extend([
            f"### {claim['ordinal']}. {status}", "",
            claim["text"], "",
            f"代码证据：{evidence_counts.get(claim['claim_id'], 0)} 条。", "",
        ])
        if claim["claim_type"] == "requirement" and status == "not_implemented":
            lines.append("修订建议：保留需求原意，并标注当前代码尚未实现。")
            lines.append("")
    content = "\n".join(lines)
    temp = path.with_suffix(".md.tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)
    meta = {
        "repair_id": _stable_id(
            "repair", item["source_file_id"], mode, revision
        ),
        "source_file_id": item["source_file_id"], "mode": mode,
        "draft_path": str(path),
        "content_hash": f"sha256:{sha256_file(path)}",
        "validation_revision": revision, "status": "DRAFT",
    }
    _write_json(directory / "repair-metadata.json", meta)
    return meta


def _write_validation(
    item: dict, project_id: str, snapshot: dict, claims: list[dict],
    evidence: list[dict], validations: list[dict], projects_root: Path,
) -> dict:
    directory = (
        document_derived_dir(item["project_id"], projects_root)
        / "validations" / item["source_file_id"] / project_id
    )
    revision = snapshot["revision"]
    path = directory / f"{revision}.json"
    payload = {
        "version": "p13-v1",
        "status": "DRAFT",
        "review_required": True,
        "source_file_id": item["source_file_id"],
        "project_id": project_id,
        "snapshot": snapshot,
        "claims": claims,
        "evidence": evidence,
        "validations": validations,
    }
    _write_json(path, payload)
    return {
        "project_id": project_id,
        "revision": revision,
        "path": str(path),
        "content_hash": f"sha256:{sha256_file(path)}",
    }


def apply_import_plan(
    plan: dict[str, Any], config: Config, *, store: TaskStore | None = None,
    projects_root: Path | None = None, runs_root: Path | None = None,
    mode: str = "dry-run", opencode_url: str = "",
) -> dict[str, Any]:
    """Apply a frozen plan using copy-only publication and source hash fencing."""
    expected = plan.get("plan_hash")
    if not expected or expected != _json_digest(plan):
        raise ValueError("invalid or modified P13 import plan hash")
    if plan.get("version") != "p13-v1" or not plan.get("copy_only"):
        raise ValueError("unsupported or unsafe import plan")
    root = (projects_root or config.documents.archive_root).resolve()
    run_root = (runs_root or document_import_runs_dir()) / plan["import_run_id"]
    run_root.mkdir(parents=True, exist_ok=True)
    plan_path = run_root / "plan.json"
    _write_json(plan_path, plan)
    package: dict[str, Any] = {
        "import_run_id": plan["import_run_id"], "status": "RUNNING",
        "mode": mode, "plan_path": str(plan_path), "plan_hash": expected,
        "plan": plan, "items": [], "claims": [], "evidence": [],
        "validations": [], "repairs": [], "relations": [], "clusters": [],
        "summaries": [], "merged_documents": [], "stats": {},
    }
    owned_store = store is None
    active_store = store or TaskStore()
    try:
        source_roots = {
            row["source_root_id"]: (
                Path(row["resolved_path"]).resolve(),
                bool(row.get("source_is_file", False)),
            )
            for row in plan.get("sources") or []
        }
        merge_groups = plan.get("merge_groups") or _merge_groups(
            plan.get("items") or [], plan["import_run_id"]
        )
        merge_membership = {
            sid: group for group in merge_groups for sid in group["members"]
        }
        merge_texts: dict[str, str] = {}
        archive_states: dict[str, dict] = {}
        for original in plan.get("items") or []:
            item = dict(original)
            source = Path(item["original_path"])
            allowed_source = source_roots.get(item.get("source_root_id"))
            source_resolved = source.resolve()
            allowed_source_root = allowed_source[0] if allowed_source else None
            exact_source = bool(allowed_source and allowed_source[1])
            if (
                allowed_source_root is None
                or (exact_source and allowed_source_root != source_resolved)
                or (not exact_source and allowed_source_root != source_resolved
                    and allowed_source_root not in source_resolved.parents)
                or source.is_symlink()
                or not source.is_file()
                or sha256_file(source) != item["sha256"]
            ):
                item["status"] = "FAILED"
                item["detail"] = "source escaped root, is linked, missing, or hash changed"
                for name, enabled in (item.get("actions") or {}).items():
                    if enabled:
                        item.setdefault("action_results", {})[name] = "FAILED"
                package["items"].append(item)
                continue
            actions = item.get("actions") or {}
            if actions.get("repair") and not actions.get("validate"):
                raise ValueError("repair=true requires validate=true")
            if actions.get("merge") and not actions.get("validate"):
                raise ValueError("merge=true requires validate=true")
            if item["status"] == "REVIEW":
                for name, enabled in actions.items():
                    if enabled:
                        item.setdefault("action_results", {})[name] = "REVIEW_REQUIRED"
                package["items"].append(item)
                continue
            pid = item.get("project_id")
            if not pid or pid not in plan.get("projects", {}):
                item["status"] = "REVIEW"
                item["detail"] = "target project is unresolved"
                for name, enabled in actions.items():
                    if enabled:
                        item.setdefault("action_results", {})[name] = "REVIEW_REQUIRED"
                package["items"].append(item)
                continue
            category = item.get("category") or "archive"
            if category not in DOC_CATEGORIES:
                raise ValueError(f"unsupported document category: {category}")
            # Merge folding: a non-primary member whose primary is already
            # archived (or already present) is not copied again — it folds
            # into the primary's archive copy and is recorded as MERGED.
            group = merge_membership.get(item["source_file_id"])
            if (
                group and group["primary_id"] != item["source_file_id"]
                and item["status"] == "PLANNED"
            ):
                primary_state = archive_states.get(group["primary_id"]) or {}
                if primary_state.get("status") in ("IMPORTED", "DUPLICATE"):
                    merged_into = str(primary_state.get("target_path") or "")
                    item["status"] = "MERGED"
                    item["merged_into"] = merged_into
                    item["target_path"] = merged_into
            if item["status"] != "MERGED":
                target_root = documents_dir(pid, root)
                target = target_root / category / Path(item["relative_path"]).name
                target_resolved = target.resolve()
                if root != target_resolved and root not in target_resolved.parents:
                    raise ValueError(f"unsafe import target: {target}")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if sha256_file(target) == item["sha256"]:
                        item["status"] = "DUPLICATE"
                        item["duplicate_of"] = str(target)
                        # A previous process may have crashed after the copy but
                        # before publishing the Manifest entry.  Idempotently
                        # complete that half-published operation.
                        _update_manifest(item, target, root)
                    else:
                        target = target.with_name(
                            f"{target.stem}-{item['sha256'][:8]}{target.suffix}"
                        )
                if item["status"] != "DUPLICATE":
                    journal_path = run_root / "journals" / f"{item['source_file_id']}.json"
                    journal = {
                        "source_file_id": item["source_file_id"],
                        "source": str(source), "target": str(target),
                        "sha256": item["sha256"], "status": "INTENT",
                    }
                    _write_json(journal_path, journal)
                    staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                    shutil.copy2(source, staging)
                    if sha256_file(staging) != item["sha256"]:
                        staging.unlink(missing_ok=True)
                        raise RuntimeError(f"copy hash verification failed: {source}")
                    staging.replace(target)
                    journal["status"] = "COPIED"
                    _write_json(journal_path, journal)
                    _update_manifest(item, target, root)
                    journal["status"] = "COMMITTED"
                    _write_json(journal_path, journal)
                    item["status"] = "IMPORTED"
                    item["target_path"] = str(target)
                    active_store.register_artifact(
                        "DOCUMENT_SOURCE", str(target), run_id=plan["import_run_id"],
                        content_hash=f"sha256:{item['sha256']}",
                    )
                archive_states[item["source_file_id"]] = {
                    "status": item["status"],
                    "target_path": (
                        item.get("target_path") or item.get("duplicate_of")
                        or str(target)
                    ),
                }
            else:
                archive_states[item["source_file_id"]] = {
                    "status": item["status"],
                    "target_path": item.get("merged_into") or "",
                }
            extracted = extract_one(DocFile(
                source, source.name, source.suffix.lower(), source.stat().st_size
            ), config.documents.content_preview_chars)
            if item["source_file_id"] in merge_membership:
                merge_texts[item["source_file_id"]] = extracted.text[
                    :config.documents.content_preview_chars
                ]
            if item.get("actions", {}).get("validate"):
                all_claims: dict[str, dict] = {}
                all_evidence: list[dict] = []
                all_validations: list[dict] = []
                snapshots: list[dict] = []
                validation_artifacts: list[dict] = []
                validation_projects = item.get("project_ids") or [pid]
                for validation_pid in validation_projects:
                    project = plan["projects"].get(validation_pid)
                    if not project:
                        continue
                    claims, evidence, validations, snapshot = validate_claims(
                        item, extracted.text, project
                    )
                    all_claims.update({claim["claim_id"]: claim for claim in claims})
                    all_evidence.extend(evidence)
                    all_validations.extend(validations)
                    public_snapshot = {
                        "project_id": validation_pid, "revision": snapshot["revision"],
                        "dirty": snapshot["dirty"],
                    }
                    snapshots.append(public_snapshot)
                    artifact = _write_validation(
                        item, validation_pid, snapshot, claims, evidence,
                        validations, root,
                    )
                    validation_artifacts.append(artifact)
                    active_store.register_artifact(
                        "DOCUMENT_VALIDATION", artifact["path"],
                        run_id=plan["import_run_id"],
                        content_hash=artifact["content_hash"],
                    )
                claims = list(all_claims.values())
                item["validation_snapshots"] = snapshots
                item["validation_results"] = all_validations
                item["validation_artifacts"] = validation_artifacts
                package["claims"].extend(claims)
                package["evidence"].extend(all_evidence)
                package["validations"].extend(all_validations)
                item.setdefault("action_results", {})["validate"] = "COMPLETED"
                if item.get("actions", {}).get("repair"):
                    revision_label = ",".join(
                        f"{row['project_id']}@{row['revision']}" for row in snapshots
                    )
                    repair = _write_repair(
                        item, claims, all_validations, all_evidence, revision_label, root
                    )
                    package["repairs"].append(repair)
                    active_store.register_artifact(
                        "DOCUMENT_REPAIR_DRAFT", repair["draft_path"],
                        run_id=plan["import_run_id"], content_hash=repair["content_hash"],
                    )
                    item.setdefault("action_results", {})["repair"] = "COMPLETED"
            package["items"].append(item)

        items_by_id = {item["source_file_id"]: item for item in package["items"]}
        for group in merge_groups:
            document = _write_merged_document(
                group, items_by_id, merge_texts,
                package["claims"], package["evidence"], root,
                mode=mode, opencode_url=opencode_url,
            )
            if document is None:
                # Primary was not archived (failed/reviewed): never fold
                # members silently — flag the group for human review.
                for sid in group["members"]:
                    member = items_by_id.get(sid)
                    if member and member.get("actions", {}).get("merge") and (
                        member.get("action_results", {}).get("merge") == "PLANNED"
                    ):
                        member.setdefault("action_results", {})["merge"] = "REVIEW_REQUIRED"
                continue
            package["merged_documents"].append(document)
            active_store.register_artifact(
                "DOCUMENT_MERGE_DRAFT", document["output_path"],
                run_id=plan["import_run_id"], content_hash=document["content_hash"],
            )
            for sid in group["members"]:
                member = items_by_id.get(sid)
                if member and member.get("status") in ("IMPORTED", "DUPLICATE", "MERGED"):
                    member.setdefault("action_results", {})["merge"] = "COMPLETED"
        for item in package["items"]:
            if (
                item.get("actions", {}).get("merge")
                and item.get("action_results", {}).get("merge") == "PLANNED"
            ):
                item["action_results"]["merge"] = "SKIPPED"
        package["relations"] = build_relations(package["items"])
        package["clusters"] = build_clusters(package["items"], plan["import_run_id"])
        package["summaries"] = write_summaries(
            package["clusters"], package["items"], package["validations"], root,
            package["relations"],
        )
        for item in package["items"]:
            if (
                item.get("actions", {}).get("consolidate")
                and item.get("status") in ("IMPORTED", "DUPLICATE", "MERGED")
            ):
                item.setdefault("action_results", {})["consolidate"] = "COMPLETED"
        indexed_projects = {
            item["project_id"] for item in package["items"]
            if item.get("project_id") and item.get("status") in ("IMPORTED", "DUPLICATE")
        }
        index_config = replace(config.documents, archive_root=root)
        for project_id in sorted(indexed_projects):
            update_index(project_id, index_config)
        for summary in package["summaries"]:
            active_store.register_artifact(
                "DOCUMENT_SUMMARY_DRAFT", summary["path"],
                run_id=plan["import_run_id"], content_hash=summary["content_hash"],
            )
        counts: dict[str, int] = {}
        for item in package["items"]:
            counts[item["status"].lower()] = counts.get(item["status"].lower(), 0) + 1
        counts.update({
            "source_errors": sum(
                bool(source.get("scan_error")) for source in plan.get("sources") or []
            ),
            "claims": len(package["claims"]), "evidence": len(package["evidence"]),
            "repairs": len(package["repairs"]), "relations": len(package["relations"]),
            "summaries": len(package["summaries"]),
            "merge_documents": len(package["merged_documents"]),
        })
        package["stats"] = counts
        package["status"] = "PARTIAL" if (
            counts.get("failed") or counts.get("review")
            or counts.get("source_errors")
        ) else "COMPLETED"
        inventory_path = run_root / "inventory.json"
        _write_json(inventory_path, {"sources": plan["sources"], "items": package["items"]})
        report_path = run_root / "report.md"
        report_path.write_text(
            "# P13 文档导入报告\n\n" + "\n".join(
                f"- {key}: {value}" for key, value in sorted(counts.items())
            ) + "\n", encoding="utf-8"
        )
        active_store.register_artifact(
            "DOCUMENT_IMPORT_INVENTORY", str(inventory_path), run_id=plan["import_run_id"]
        )
        active_store.register_artifact(
            "DOCUMENT_IMPORT_REPORT", str(report_path), run_id=plan["import_run_id"]
        )
        active_store.save_document_import(package)
        return package
    finally:
        if owned_store:
            active_store.close()
