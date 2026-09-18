"""EXTRACT node — sha256 + front matter + content extraction (p11 §15).

Extraction is deterministic and dependency-light:

* ``.md`` / ``.markdown`` / ``.txt`` — read as UTF-8 (BOM tolerant)
* ``.docx`` / ``.pptx`` / ``.xlsx`` — stdlib ``zipfile`` + XML tag stripping
* ``.pdf`` — ``pypdf`` when installed, otherwise metadata-only (name-based
  classification still works, the note records the limitation)
"""

from __future__ import annotations

import html
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import DocumentsConfig
from log import get_logger
from runtime.task_resolver import parse_markdown

from document.schemas import DocFile, ExtractedDoc
from document.utils import sha256_file

logger = get_logger(__name__)

TEXT_SUFFIXES = {".md", ".markdown", ".txt"}


def extract_all(
    docs: list[DocFile], cfg: DocumentsConfig
) -> list[ExtractedDoc]:
    """Extract documents in parallel (I/O bound)."""
    if not docs:
        return []
    workers = max(1, min(cfg.max_workers, len(docs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(
            pool.map(
                lambda doc: extract_one(doc, cfg.content_preview_chars), docs
            )
        )


def extract_one(doc: DocFile, preview_chars: int = 4000) -> ExtractedDoc:
    digest = sha256_file(doc.path)
    text, note = _read_text(doc.path)

    meta: dict = {}
    content = text
    if text and doc.suffix in TEXT_SUFFIXES:
        meta, body = parse_markdown(text)
        if not isinstance(meta, dict):
            meta = {}
        content = body

    title = (
        str(meta.get("title", "") or "")
        or _first_heading(content)
        or doc.path.stem
    )
    if note:
        logger.info(f"[extract] {doc.name}: {note}")

    return ExtractedDoc(
        doc=doc,
        sha256=digest,
        front_matter=meta,
        title=title,
        text=content,
        preview=content[:preview_chars],
        note=note,
    )


# --------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------

def _read_text(path: Path) -> tuple[str, str]:
    """Return (content, note) for any supported file."""
    suffix = path.suffix.lower()
    try:
        if suffix in TEXT_SUFFIXES:
            return path.read_text(encoding="utf-8-sig", errors="replace"), ""
        if suffix == ".docx":
            return _zip_xml_text(path, r"word/document\.xml"), ""
        if suffix == ".pptx":
            return _zip_xml_text(path, r"ppt/slides/slide\d+\.xml"), ""
        if suffix == ".xlsx":
            return _zip_xml_text(path, r"xl/sharedStrings\.xml"), ""
        if suffix == ".pdf":
            return _read_pdf(path)
    except Exception as exc:
        return "", f"content extraction failed: {exc}"
    return "", f"unsupported type: {suffix}"


def _zip_xml_text(path: Path, name_pattern: str) -> str:
    chunks: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            n for n in archive.namelist() if re.fullmatch(name_pattern, n)
        )
        for name in names:
            xml = archive.read(name).decode("utf-8", errors="ignore")
            text = _xml_to_text(xml)
            if text.strip():
                chunks.append(text)
    return "\n".join(chunks)


def _xml_to_text(xml: str) -> str:
    xml = re.sub(r"</w:p>", "\n", xml)   # docx paragraphs
    xml = re.sub(r"</a:p>", "\n", xml)   # pptx paragraphs
    xml = re.sub(r"</si>", "\n", xml)    # xlsx shared strings
    xml = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(xml)


def _read_pdf(path: Path) -> tuple[str, str]:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            return "", "PDF content not extracted (pypdf not installed)"
    try:
        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return text, ""
    except Exception as exc:
        return "", f"PDF extraction failed: {exc}"


def _first_heading(content: str) -> str:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""
