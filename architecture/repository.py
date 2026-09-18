"""Architecture asset storage layout (p10 §19).

``projects/<project_id>/architecture/
    metadata.json          # latest snapshot pointer (per-project)
    baseline/              # default snapshot
        architecture.json
        architecture.html
        architecture.svg (best-effort)
        revision.txt
    snapshots/<id>/        # historical snapshots
    analysis/
        modules.md / dependencies.md / risks.md
```

Assets live in the runner's control plane — never inside a business repo.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from architecture.schemas import ArchitectureSnapshot
from config import ARCHITECTURE_ROOT, ArchitectureConfig
from runtime.assets import architecture_dir
from log import get_logger

logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ArchitectureRepository:
    def __init__(self, config: ArchitectureConfig) -> None:
        self._config = config

    # ------------------------------------------------------------- layout

    def project_dir(self, project_id: str) -> Path:
        return architecture_dir(project_id)

    def snapshot_dir(self, project_id: str, snapshot_id: str) -> Path:
        return self.project_dir(project_id) / snapshot_id

    def analysis_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "analysis"

    # ------------------------------------------------------------ lifecycle

    def create_snapshot_dir(self, project_id: str, snapshot_id: str) -> Path:
        """Create an isolated staging dir; the current snapshot stays valid."""
        staging = self.project_dir(project_id) / ".staging"
        target = staging / f"{snapshot_id}-{uuid.uuid4().hex[:10]}"
        target.mkdir(parents=True, exist_ok=False)
        return target

    def _archive_name(self, project_id: str, snapshot_id: str) -> Path:
        snapshots = self.project_dir(project_id) / "snapshots"
        snapshots.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return snapshots / f"{stamp}-{uuid.uuid4().hex[:6]}-{snapshot_id}"

    # ------------------------------------------------------------- persist

    def write_snapshot(
        self,
        project_id: str,
        snapshot_id: str,
        revision: str,
        branch: str,
        repository_path: str,
        source: str,
        source_task_id: str,
        json_path: Path,
        html_path: Path,
        svg_path: Path | None,
        archify_version: str,
        analysis_files: dict[str, str] | None = None,
    ) -> ArchitectureSnapshot:
        """Finalise a snapshot dir: revision.txt, metadata.json, analysis docs,
        and the per-project metadata pointer."""
        # json/html were produced and validated in the staging directory.
        snap_dir = json_path.parent

        (snap_dir / "revision.txt").write_text(revision, encoding="utf-8")

        # Analysis documents (modules / dependencies / risks).
        if analysis_files:
            analysis_dir = self.analysis_dir(project_id)
            analysis_dir.mkdir(parents=True, exist_ok=True)
            for name, content in analysis_files.items():
                (analysis_dir / name).write_text(content, encoding="utf-8")

        snapshot = ArchitectureSnapshot(
            id=snapshot_id,
            project_id=project_id,
            diagram_type="architecture",
            repository_path=repository_path,
            repository_revision=revision,
            branch=branch,
            status="COMPLETED",
            json_path=str(json_path),
            html_path=str(html_path),
            svg_path=str(svg_path) if svg_path else "",
            archify_version=archify_version,
            source=source,
            source_task_id=source_task_id,
            created_at=_now_iso(),
            updated_at=_now_iso(),
        )
        metadata_path = snap_dir / "metadata.json"
        snapshot.metadata_path = str(metadata_path)
        metadata_path.write_text(
            json.dumps(snapshot.to_metadata(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Publish only after every staging artifact and metadata file exists.
        target = self.snapshot_dir(project_id, snapshot_id)
        publish_journal = self.project_dir(project_id) / ".publish.json"
        archive_to = self._archive_name(project_id, snapshot_id) if target.exists() else None
        publish_journal.write_text(json.dumps({
            "snapshot_id": snapshot_id,
            "staging": str(snap_dir),
            "target": str(target),
            "archive": str(archive_to) if archive_to else "",
            "status": "INTENT",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        if target.exists():
            target.rename(archive_to)
            logger.info(
                "[arch-store] existing snapshot %s/%s archived to %s",
                project_id, snapshot_id, archive_to.name,
            )
        snap_dir.replace(target)
        snapshot.json_path = str(target / json_path.name)
        snapshot.html_path = str(target / html_path.name)
        snapshot.svg_path = str(target / svg_path.name) if svg_path else ""
        snapshot.metadata_path = str(target / "metadata.json")
        Path(snapshot.metadata_path).write_text(
            json.dumps(snapshot.to_metadata(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Per-project pointer (which snapshot is current), also atomically.
        pointer = {
            "project_id": project_id,
            "current_snapshot": snapshot_id,
            "updated_at": snapshot.updated_at,
        }
        pointer_path = self.project_dir(project_id) / "metadata.json"
        pointer_tmp = pointer_path.with_suffix(".json.tmp")
        pointer_tmp.write_text(
            json.dumps(pointer, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        pointer_tmp.replace(pointer_path)
        publish_journal.unlink(missing_ok=True)
        return snapshot

    def reconcile_publish(self, project_id: str) -> bool:
        """Complete a directory switch interrupted after publish intent."""
        journal = self.project_dir(project_id) / ".publish.json"
        if not journal.exists():
            return False
        data = json.loads(journal.read_text(encoding="utf-8"))
        target = Path(data["target"])
        staging = Path(data["staging"])
        archive = Path(data["archive"]) if data.get("archive") else None
        if not target.exists() and staging.exists():
            staging.replace(target)
        elif not target.exists() and archive and archive.exists():
            archive.replace(target)
        if not target.exists():
            raise RuntimeError(f"cannot reconcile architecture publish: {project_id}")
        metadata = target / "metadata.json"
        if metadata.exists():
            snapshot = json.loads(metadata.read_text(encoding="utf-8"))
            pointer = {
                "project_id": project_id,
                "current_snapshot": snapshot.get("id", data["snapshot_id"]),
                "updated_at": snapshot.get("updated_at", _now_iso()),
            }
            pointer_path = self.project_dir(project_id) / "metadata.json"
            temp = pointer_path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(pointer, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(pointer_path)
        journal.unlink(missing_ok=True)
        return True

    # -------------------------------------------------------------- reading

    def load_snapshot(self, project_id: str, snapshot_id: str) -> ArchitectureSnapshot | None:
        metadata_path = self.snapshot_dir(project_id, snapshot_id) / "metadata.json"
        if not metadata_path.exists():
            metadata_path = ARCHITECTURE_ROOT / "projects" / project_id / snapshot_id / "metadata.json"
        if not metadata_path.exists():
            return None
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            snap = ArchitectureSnapshot.from_metadata(data)
            snap.metadata_path = str(metadata_path)
            return snap
        except Exception as exc:
            logger.warning("[arch-store] cannot read %s: %s", metadata_path, exc)
            return None

    def list_snapshots(self, project_id: str) -> list[ArchitectureSnapshot]:
        result: list[ArchitectureSnapshot] = []
        pdir = self.project_dir(project_id)
        if not pdir.exists():
            return result
        for candidate in sorted(pdir.iterdir()):
            if not candidate.is_dir() or candidate.name in ("analysis", "snapshots", ".staging"):
                continue
            snap = self.load_snapshot(project_id, candidate.name)
            if snap:
                result.append(snap)
        # historical snapshots under snapshots/
        hist = pdir / "snapshots"
        if hist.exists():
            for candidate in sorted(hist.iterdir()):
                if candidate.is_dir():
                    # historical snapshot metadata sits in its own dir
                    snap = self._load_historical(project_id, candidate)
                    if snap:
                        result.append(snap)
        return result

    def _load_historical(self, project_id: str, directory: Path) -> ArchitectureSnapshot | None:
        metadata_path = directory / "metadata.json"
        if not metadata_path.exists():
            return None
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            snap = ArchitectureSnapshot.from_metadata(data)
            snap.id = directory.name
            snap.metadata_path = str(metadata_path)
            return snap
        except Exception:
            return None

    def current_snapshot_id(self, project_id: str) -> str | None:
        pointer = self.project_dir(project_id) / "metadata.json"
        if not pointer.exists():
            pointer = ARCHITECTURE_ROOT / "projects" / project_id / "metadata.json"
        if not pointer.exists():
            return None
        try:
            return json.loads(pointer.read_text(encoding="utf-8")).get("current_snapshot")
        except Exception:
            return None
