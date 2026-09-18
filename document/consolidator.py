"""P13 deterministic relation clustering and reviewable summary drafts."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from runtime.assets import document_derived_dir


def _stable_id(prefix: str, *parts: object) -> str:
    payload = ":".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(payload.encode()).hexdigest()[:20]}"


def _atomic_json(path: Path, value: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def normalized_text_hash(text: str) -> str:
    normalized = re.sub(r"\s+", "", text).lower()
    normalized = re.sub(r"[^\w\u4e00-\u9fff]", "", normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def content_terms(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,8}", text.lower())
    return sorted(set(words))[:500]


def _similarity(left: list[str], right: list[str]) -> float:
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _polarity(item: dict[str, Any]) -> int:
    text = " ".join([
        str(item.get("title") or ""), str(item.get("relative_path") or ""),
        " ".join(item.get("content_terms") or []),
    ]).lower()
    return -1 if re.search(r"(?:不再|无需|禁止|未实现|移除|deprecated|disabled|not)", text) else 1


def build_relations(
    items: list[dict[str, Any]], action: str = "consolidate",
) -> list[dict]:
    relations: list[dict] = []
    candidates = [i for i in items if i.get("actions", {}).get(action)]
    for pos, left in enumerate(candidates):
        for right in candidates[pos + 1:]:
            relation_source, relation_target = left, right
            if left.get("project_id") != right.get("project_id"):
                continue
            if left["sha256"] == right["sha256"]:
                kind, confidence = "exact_duplicate", 1.0
            elif left.get("normalized_hash") == right.get("normalized_hash"):
                kind, confidence = "normalized_duplicate", 0.99
            else:
                score = _similarity(left.get("content_terms", []), right.get("content_terms", []))
                if score < 0.35:
                    continue
                left_label = f"{left.get('title', '')} {left.get('relative_path', '')}".lower()
                right_label = f"{right.get('title', '')} {right.get('relative_path', '')}".lower()
                final_pattern = r"(?:最终|新版|最新|final|v\d+)"
                if _polarity(left) != _polarity(right):
                    kind, confidence = "conflicts", min(0.9, score + 0.2)
                elif re.search(final_pattern, right_label) and not re.search(final_pattern, left_label):
                    kind, confidence = "supersedes", min(0.9, score + 0.15)
                    relation_source, relation_target = right, left
                elif left.get("category") != right.get("category"):
                    kind, confidence = "complements", min(0.85, score + 0.1)
                else:
                    kind, confidence = "similar", min(0.95, score)
            relations.append({
                "relation_id": _stable_id(
                    "relation", relation_source["source_file_id"],
                    relation_target["source_file_id"], kind,
                ),
                "source_document_id": relation_source["source_file_id"],
                "target_document_id": relation_target["source_file_id"],
                "relation_type": kind,
                "confidence": confidence,
                "detail": "deterministic content comparison",
            })
    return relations


def build_clusters(items: list[dict[str, Any]], run_id: str) -> list[dict]:
    buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    for item in items:
        if item.get("project_id") and item.get("actions", {}).get("consolidate"):
            buckets[(item["project_id"], item.get("topic") or "general")].append(
                item["source_file_id"]
            )
    return [{
        "cluster_id": _stable_id("cluster", run_id, pid, topic),
        "project_id": pid,
        "topic": topic,
        "status": "DRAFT",
        "members": sorted(members),
    } for (pid, topic), members in sorted(buckets.items())]


def write_summaries(
    clusters: list[dict], items: list[dict], validations: list[dict],
    projects_root: Path | None = None, relations: list[dict] | None = None,
) -> list[dict]:
    by_id = {item["source_file_id"]: item for item in items}
    val_by_claim = defaultdict(list)
    for value in validations:
        val_by_claim[value["claim_id"]].append(value)
    summaries: list[dict] = []
    for cluster in clusters:
        members = [by_id[mid] for mid in cluster["members"] if mid in by_id]
        member_ids = set(cluster["members"])
        cluster_relations = [
            row for row in (relations or [])
            if row["source_document_id"] in member_ids
            and row["target_document_id"] in member_ids
        ]
        revisions: dict[str, str] = {}
        lines = [
            f"# {cluster['topic']} 文档归纳草稿", "",
            "> 状态：DRAFT，需人工确认后才能作为权威结论。", "",
            "## 审核元数据", "",
            "- generator: `p13-rules-v1`",
            "- review_status: `DRAFT`",
            "- reviewer: `unassigned`", "",
            "## 当前结论", "",
            "本草稿只归纳可追溯的来源与代码校验状态，不自动裁决冲突或替代原文。", "",
            "## 已由代码验证的实现", "",
        ]
        verified_lines: list[str] = []
        gaps: list[str] = []
        unverifiable: list[str] = []
        for item in members:
            for snapshot in item.get("validation_snapshots") or []:
                revisions[snapshot["project_id"]] = snapshot["revision"]
            for val in item.get("validation_results") or []:
                label = (
                    f"`{item['source_file_id']}` / `{val['project_id']}` / "
                    f"`{val['claim_id']}` @ `{val['revision']}`"
                )
                if val["status"] == "verified":
                    verified_lines.append(f"- {label}")
                elif val["status"] == "not_implemented":
                    gaps.append(f"- {label}")
                elif val["status"] in ("unverifiable", "partially_verified"):
                    unverifiable.append(f"- {val['status']}: {label}")
        lines.extend(verified_lines or ["- 当前没有达到 verified 的代码证据。"])
        lines.extend(["", "## 尚未实现的需求", ""])
        lines.extend(gaps or ["- 当前没有被标记为 not_implemented 的需求主张。"])
        lines.extend(["", "## 文档与代码不一致项", ""])
        lines.extend(unverifiable or ["- 当前没有自动识别出的未验证或部分验证项。"])
        lines.extend(["", "## 历史方案和替代关系", ""])
        history_relations = [
            row for row in cluster_relations
            if row["relation_type"] in ("supersedes", "similar", "complements")
        ]
        if history_relations:
            for row in history_relations:
                lines.append(
                    f"- {row['relation_type']}: `{row['source_document_id']}` → "
                    f"`{row['target_document_id']}`（confidence {row['confidence']:.2f}）"
                )
        else:
            lines.append("- 当前没有自动识别出的相似、互补或替代关系。")
        lines.extend([
            "## 未解决冲突", "",
        ])
        conflicts = [row for row in cluster_relations if row["relation_type"] == "conflicts"]
        if conflicts:
            for row in conflicts:
                lines.append(
                    f"- `{row['source_document_id']}` ↔ `{row['target_document_id']}` "
                    f"（confidence {row['confidence']:.2f}）"
                )
        else:
            lines.append("- 自动流程未发现明确冲突；需求或架构冲突仍需人工复核。")
        lines.extend(["", "## 来源文档", ""])
        for item in members:
            lines.append(
                f"- `{item['source_file_id']}` {item.get('title') or item['relative_path']} "
                f"（{item.get('category') or 'archive'}，{item.get('status')}）"
            )
        lines.extend(["", "## 代码证据与 Git Revision", ""])
        if revisions:
            for project_id, revision in sorted(revisions.items()):
                lines.append(f"- `{project_id}`: `{revision}`")
        else:
            lines.append("- not_requested：本主题未开启代码校验。")
        lines.append("")
        directory = document_derived_dir(cluster["project_id"], projects_root) / "topics" / cluster["topic"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "SUMMARY.md"
        content = "\n".join(lines)
        temp = path.with_suffix(".md.tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)
        _atomic_json(directory / "sources.json", [
            {
                "source_file_id": item["source_file_id"],
                "relative_path": item.get("relative_path"),
                "sha256": item.get("sha256"),
                "status": item.get("status"),
            } for item in members
        ])
        _atomic_json(directory / "conflicts.json", conflicts)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        summaries.append({
            "summary_id": _stable_id("summary", cluster["cluster_id"], digest),
            "cluster_id": cluster["cluster_id"],
            "project_id": cluster["project_id"],
            "topic": cluster["topic"],
            "path": str(path),
            "content_hash": f"sha256:{digest}",
            "status": "DRAFT",
            "source_document_ids": cluster["members"],
            "validation_revisions": revisions,
        })
    return summaries
