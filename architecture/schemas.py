"""Architecture asset data structures (p10)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ArchitectureSnapshot:
    """One persisted architecture analysis of a project at a git revision."""

    id: str                          # e.g. "baseline" or "2026-09-13-a1b2c3d"
    project_id: str
    diagram_type: str = "architecture"
    repository_path: str = ""
    repository_revision: str = ""
    branch: str = ""
    status: str = "RUNNING"          # RUNNING / COMPLETED / FAILED
    json_path: str = ""
    html_path: str = ""
    svg_path: str = ""
    metadata_path: str = ""
    archify_version: str = ""
    source_task_id: str = ""
    source: str = ""                 # "opencode" | "dry-run"
    error: str = ""
    created_at: str = ""
    updated_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.id,
            "project_id": self.project_id,
            "diagram_type": self.diagram_type,
            "repository_path": self.repository_path,
            "repository_revision": self.repository_revision,
            "branch": self.branch,
            "status": self.status,
            "json_path": self.json_path,
            "html_path": self.html_path,
            "svg_path": self.svg_path,
            "archify_version": self.archify_version,
            "source": self.source,
            "source_task_id": self.source_task_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_metadata(cls, data: dict[str, Any]) -> ArchitectureSnapshot:
        return cls(
            id=str(data.get("snapshot_id", "")),
            project_id=str(data.get("project_id", "")),
            diagram_type=str(data.get("diagram_type", "architecture")),
            repository_path=str(data.get("repository_path", "")),
            repository_revision=str(data.get("repository_revision", "")),
            branch=str(data.get("branch", "")),
            status=str(data.get("status", "RUNNING")),
            json_path=str(data.get("json_path", "")),
            html_path=str(data.get("html_path", "")),
            svg_path=str(data.get("svg_path", "")),
            metadata_path=str(data.get("metadata_path", "")),
            archify_version=str(data.get("archify_version", "")),
            source=str(data.get("source", "")),
            source_task_id=str(data.get("source_task_id", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )
