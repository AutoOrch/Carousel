"""Schemas for the document pipeline (p11)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Fixed taxonomy (p11 §6) — categories are NOT invented by the LLM.
DOC_CATEGORIES = (
    "requirements",
    "design",
    "architecture",
    "research",
    "implementation",
    "validation",
    "meeting",
    "api",
    "task",
    "report",
    "decision",
    "archive",
)

ACTION_ARCHIVE = "archive"
ACTION_REVIEW = "review"
ACTION_SKIP_DUPLICATE = "skip_duplicate"


@dataclass
class DocFile:
    """A document claimed from the input directory.

    ``path`` is the *current* physical location (inside processing/);
    ``name`` preserves the original file name for reports and targets.
    """

    path: Path
    name: str
    suffix: str
    size: int


@dataclass
class ExtractedDoc:
    doc: DocFile
    sha256: str
    front_matter: dict = field(default_factory=dict)
    title: str = ""
    text: str = ""      # content without front matter ("" for binaries)
    preview: str = ""   # first N chars, what the LLM gets to see
    note: str = ""      # extraction limitation (e.g. pdf without pypdf)


@dataclass
class Classification:
    project: str = ""            # project id ("" when unknown)
    category: str = "archive"
    confidence: float = 0.0      # project confidence — gates archiving
    reason: str = ""
    method: str = ""             # front-matter | rules | llm
    candidates: list[tuple[str, float]] = field(default_factory=list)


@dataclass
class DocumentPlan:
    """Structured plan — the only contract between LLM and executor (p11 §2)."""

    source: str                  # display origin, e.g. "doc/流量池方案.md"
    file_path: Path              # current physical path (processing/)
    sha256: str
    project: str
    category: str
    target: Path
    action: str                  # archive | review | skip_duplicate
    reason: str
    confidence: float
    duplicate_of: str = ""


@dataclass
class ArchiveResult:
    plan: DocumentPlan
    status: str                  # archived | duplicate | review | failed
    detail: str = ""
    final_path: str = ""
