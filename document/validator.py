"""P13 read-only validation of document claims against committed Git code."""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any


NORMATIVE = re.compile(r"(?:应该|应当|必须|需要|shall|should|must)", re.I)
IDENTIFIER = re.compile(r"`([^`]{2,80})`|\b([A-Za-z_][A-Za-z0-9_.:/-]{2,80})\b")
SKIP_WORDS = {
    "the", "and", "for", "with", "from", "this", "that", "into", "true",
    "false", "http", "https", "project", "document", "code",
}


def _stable_id(prefix: str, *parts: object) -> str:
    payload = ":".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(payload.encode()).hexdigest()[:20]}"


def _safe_excerpt(value: str) -> str:
    text = str(value or "")
    text = re.sub(
        r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+",
        r"\1[REDACTED]", text,
    )
    return text.strip()[:300]


def git_snapshot(repo: Path, revision: str = "HEAD") -> dict[str, Any]:
    """Resolve a committed revision without changing the repository."""
    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

    resolved = run("rev-parse", "--verify", f"{revision}^{{commit}}")
    if resolved.returncode != 0:
        raise ValueError(f"unknown git revision {revision!r}: {repo}")
    branch = run("rev-parse", "--abbrev-ref", "HEAD")
    status = run("status", "--porcelain")
    return {
        "repository_path": str(repo.resolve()),
        "revision": resolved.stdout.strip(),
        "branch": branch.stdout.strip() if branch.returncode == 0 else "",
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
        "status_fingerprint": hashlib.sha256(status.stdout.encode()).hexdigest(),
    }


def extract_claims(item: dict[str, Any], text: str) -> list[dict[str, Any]]:
    """Extract conservative, line-addressable claims from prose and lists."""
    claims: list[dict[str, Any]] = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r"^\s*(?:[-*+] |\d+[.)]\s*)", "", raw).strip()
        if line.startswith("#"):
            continue
        line = re.sub(r"\s+", " ", line)
        if len(line) < 12 or len(line) > 500:
            continue
        if not (IDENTIFIER.search(line) or NORMATIVE.search(line) or re.search(
            r"(?:实现|使用|支持|负责|配置|接口|发布|调用|存储|校验|验证|运行|返回)", line
        )):
            continue
        claim_type = "requirement" if NORMATIVE.search(line) else "current-state"
        claims.append({
            "claim_id": _stable_id("claim", item["source_file_id"], line_no, line),
            "source_file_id": item["source_file_id"],
            "ordinal": len(claims) + 1,
            "claim_type": claim_type,
            "text": line,
            "source_location": f"line:{line_no}",
            "project_ids": list(item.get("project_ids") or (
                [item["project_id"]] if item.get("project_id") else []
            )),
        })
        if len(claims) >= 40:
            break
    return claims


def _query_terms(claim: str) -> list[str]:
    terms: list[str] = []
    for match in IDENTIFIER.finditer(claim):
        term = (match.group(1) or match.group(2) or "").strip(".,:;/")
        if len(term) < 3 or term.lower() in SKIP_WORDS or term.isdigit():
            continue
        if term not in terms:
            terms.append(term)
    return sorted(terms, key=lambda value: (-len(value), value))[:6]


def validate_claims(
    item: dict[str, Any], text: str, project: dict[str, Any]
) -> tuple[list[dict], list[dict], list[dict], dict]:
    """Return claims, evidence, validations and immutable Git snapshot."""
    repo = Path(project["path"])
    before = git_snapshot(repo, project.get("requested_revision") or "HEAD")
    claims = extract_claims(item, text)
    evidence_rows: list[dict] = []
    validations: list[dict] = []
    for claim in claims:
        evidence_for_claim: list[dict] = []
        terms = _query_terms(claim["text"])
        for term in terms:
            result = subprocess.run(
                ["git", "grep", "-n", "-I", "-F", term, before["revision"], "--"],
                cwd=repo, capture_output=True, text=True, encoding="utf-8",
                errors="replace",
            )
            if result.returncode not in (0, 1):
                continue
            for raw in result.stdout.splitlines()[:3]:
                # Supplying a tree-ish makes git-grep prefix each result with
                # ``<revision>:``.  Strip that prefix before parsing path and
                # line so evidence remains directly navigable.
                result_line = raw
                revision_prefix = f"{before['revision']}:"
                if result_line.startswith(revision_prefix):
                    result_line = result_line[len(revision_prefix):]
                parts = result_line.split(":", 2)
                if len(parts) != 3:
                    continue
                file_path, line_raw, excerpt = parts
                try:
                    line_number = int(line_raw)
                except ValueError:
                    line_number = None
                evidence = {
                    "evidence_id": _stable_id(
                        "evidence", claim["claim_id"], project["id"], raw
                    ),
                    "claim_id": claim["claim_id"],
                    "project_id": project["id"],
                    "repository_path": before["repository_path"],
                    "revision": before["revision"],
                    "file_path": file_path,
                    "line_number": line_number,
                    "symbol": term,
                    "excerpt": _safe_excerpt(excerpt),
                    "excerpt_hash": f"sha256:{hashlib.sha256(excerpt.encode()).hexdigest()}",
                    "method": "git-grep",
                }
                evidence_rows.append(evidence)
                evidence_for_claim.append(evidence)
            if len(evidence_for_claim) >= 4:
                break
        backticked = bool(re.search(r"`[^`]+`", claim["text"]))
        if evidence_for_claim and backticked:
            status, confidence = "verified", 0.85
        elif evidence_for_claim:
            status, confidence = "partially_verified", 0.65
        elif claim["claim_type"] == "requirement":
            status, confidence = "not_implemented", 0.55
        else:
            status, confidence = "unverifiable", 0.35
        validations.append({
            "validation_id": _stable_id(
                "docval", claim["claim_id"], project["id"], before["revision"]
            ),
            "claim_id": claim["claim_id"],
            "project_id": project["id"],
            "revision": before["revision"],
            "status": status,
            "confidence": confidence,
            "detail": f"{len(evidence_for_claim)} code evidence match(es)",
        })
    after = git_snapshot(repo, before["revision"])
    if before["status_fingerprint"] != after["status_fingerprint"]:
        raise RuntimeError(f"repository changed during document validation: {repo}")
    return claims, evidence_rows, validations, before
