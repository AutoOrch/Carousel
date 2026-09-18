"""SCAN node — deterministic claim of input documents (p11 §15).

Only the top level of the input directory is scanned: ``doc/review/``
(human review parking lot) is intentionally never re-claimed.  Files are
atomically moved into ``processing/documents/`` — the same claim semantics
as the task watcher.  Crash recovery moves stranded files back.
"""

from __future__ import annotations

import shutil

from config import DocumentsConfig
from log import get_logger

from document.schemas import DocFile

logger = get_logger(__name__)


def recover_processing(cfg: DocumentsConfig) -> int:
    """Move files stranded in processing/documents/ back to the input dir."""
    if not cfg.processing_dir.exists():
        return 0
    recovered = 0
    for path in sorted(cfg.processing_dir.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in cfg.supported_extensions:
            continue
        target = cfg.input_dir / path.name
        if target.exists():
            target = cfg.input_dir / f"{path.stem}-recovered{path.suffix}"
        try:
            shutil.move(str(path), str(target))
            recovered += 1
        except Exception as exc:
            logger.warning(f"[scan] failed to recover {path.name}: {exc}")
    return recovered


def claim_documents(cfg: DocumentsConfig) -> list[DocFile]:
    """Atomically claim input files by moving them into processing/."""
    cfg.input_dir.mkdir(parents=True, exist_ok=True)
    cfg.processing_dir.mkdir(parents=True, exist_ok=True)

    claimed: list[DocFile] = []
    ignored: list[str] = []
    for path in sorted(cfg.input_dir.iterdir()):
        if not path.is_file():
            continue  # subdirectories (review/) are never claimed
        name = path.name
        if name.startswith(".") or name.startswith("~$"):
            continue  # hidden files / office lock files
        if path.suffix.lower() not in cfg.supported_extensions:
            ignored.append(name)
            continue
        target = cfg.processing_dir / name
        try:
            shutil.move(str(path), str(target))
        except Exception as exc:
            logger.warning(f"[scan] failed to claim {name}: {exc}")
            continue
        claimed.append(
            DocFile(
                path=target,
                name=name,
                suffix=path.suffix.lower(),
                size=target.stat().st_size,
            )
        )

    if ignored:
        logger.info(f"[scan] ignored unsupported files: {', '.join(ignored)}")
    return claimed
