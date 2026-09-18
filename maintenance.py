"""SQLite backup/integrity and control-plane retention utilities."""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from config import DB_FILE, DATA_ROOT
from runtime.assets import PROJECTS_ROOT


def check_database() -> str:
    conn = sqlite3.connect(str(DB_FILE))
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def backup_database(output: Path | None = None) -> Path:
    target = output or (
        PROJECTS_ROOT / "_backups" /
        f"tasks-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.db"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(DB_FILE))
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup integrity check failed")
    finally:
        destination.close()
        source.close()
    return target


def retention_candidates(days: int) -> list[Path]:
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    candidates: list[Path] = []
    roots = [DATA_ROOT / "logs", PROJECTS_ROOT / "_runs" / "reports"]
    for root in roots:
        if root.exists():
            candidates.extend(
                path for path in root.rglob("*")
                if path.is_file() and path.stat().st_mtime < cutoff
            )
    # Historical architecture snapshots may be retained independently;
    # current baseline directories are deliberately excluded.
    for snapshots in PROJECTS_ROOT.glob("*/architecture/snapshots"):
        candidates.extend(
            path for path in snapshots.iterdir()
            if path.stat().st_mtime < cutoff
        )
    return sorted(candidates)


def main() -> None:
    parser = argparse.ArgumentParser(description="Runner maintenance")
    parser.add_argument("--integrity", action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--apply", action="store_true", help="delete cleanup candidates")
    args = parser.parse_args()
    if args.days < 1:
        raise SystemExit("--days must be >= 1")
    if args.integrity:
        result = check_database()
        print(f"database integrity: {result}")
        if result != "ok":
            raise SystemExit(2)
    if args.backup:
        print(f"database backup: {backup_database(args.output)}")
    if args.cleanup:
        candidates = retention_candidates(args.days)
        for path in candidates:
            print(("DELETE " if args.apply else "WOULD DELETE ") + str(path))
            if args.apply:
                if path.is_dir():
                    import shutil
                    shutil.rmtree(path)
                else:
                    path.unlink()
        print(f"retention candidates: {len(candidates)}")
    if not (args.integrity or args.backup or args.cleanup):
        parser.error("choose --integrity, --backup, or --cleanup")


if __name__ == "__main__":
    main()
