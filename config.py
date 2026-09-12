from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent

# Task lifecycle directories
PROMPTS_DIR = ROOT / "prompts"
PROCESSING_DIR = ROOT / "processing"
PROCESSED_DIR = ROOT / "processed"
FAILED_DIR = ROOT / "failed"
REPORTS_DIR = ROOT / "reports"

# Per-project worktrees are created under: worktrees/<project>/<task_id>/
WORKTREES_DIR = ROOT / "worktrees"

CONFIG_FILE = ROOT / "config.yaml"
DB_FILE = ROOT / "runtime" / "tasks.db"

DEFAULT_MAX_WORKERS = 3
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_OPENCODE_URL = "http://127.0.0.1:4096"


@dataclass
class Project:
    id: str
    path: Path
    default_branch: str = "main"
    language: str = ""
    test_command: str = ""
    agent: str = ""
    model: str = ""


@dataclass
class Config:
    projects: dict[str, Project] = field(default_factory=dict)
    max_workers: int = DEFAULT_MAX_WORKERS
    poll_interval: float = DEFAULT_POLL_INTERVAL
    opencode_url: str = DEFAULT_OPENCODE_URL
    max_attempts: int = 3
    backoff_seconds: float = 10.0
    lease_timeout: int = 300        # seconds before a lease is considered stale
    heartbeat_interval: int = 30    # seconds between heartbeat updates


def load_config() -> Config:
    data: dict = {}
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}

    projects: dict[str, Project] = {}
    for pid, pdata in (data.get("projects") or {}).items():
        pdata = pdata or {}
        raw_path = Path(pdata.get("path", ""))
        project_path = raw_path if raw_path.is_absolute() else (ROOT / raw_path)
        project_path = project_path.resolve()

        opencode = pdata.get("opencode") or {}
        test = pdata.get("test") or {}

        projects[pid] = Project(
            id=pid,
            path=project_path,
            default_branch=pdata.get("default_branch", "main"),
            language=pdata.get("language", ""),
            test_command=test.get("command", "") if isinstance(test, dict) else "",
            agent=opencode.get("agent", "") if isinstance(opencode, dict) else "",
            model=opencode.get("model", "") if isinstance(opencode, dict) else "",
        )

    worker = data.get("worker") or {}
    opencode = data.get("opencode") or {}
    retry = data.get("retry") or {}
    recovery = data.get("recovery") or {}

    return Config(
        projects=projects,
        max_workers=int(worker.get("max_workers", DEFAULT_MAX_WORKERS)),
        poll_interval=float(worker.get("poll_interval", DEFAULT_POLL_INTERVAL)),
        opencode_url=opencode.get("base_url", DEFAULT_OPENCODE_URL),
        max_attempts=int(retry.get("max_attempts", 3)),
        backoff_seconds=float(retry.get("backoff_seconds", 10.0)),
        lease_timeout=int(recovery.get("lease_timeout", 300)),
        heartbeat_interval=int(recovery.get("heartbeat_interval", 30)),
    )


def _detect_default_branch(repo: Path) -> str:
    """Return the currently checked-out branch of *repo* (fallback: main)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        branch = result.stdout.strip()
        if result.returncode == 0 and branch and branch != "HEAD":
            return branch
    except Exception:
        pass
    return "main"


def project_from_path(path_str: str) -> Project | None:
    """Create an ad-hoc Project from a filesystem path.

    Accepts absolute or relative (to the runner root) paths.  Returns None
    when the directory does not exist; raises ValueError when it exists but
    is not a git repository.  The project id is the directory name; the
    default branch is detected from the repo's current HEAD.
    """
    raw = Path(path_str.replace("\\", "/"))
    candidate = raw if raw.is_absolute() else (ROOT / raw)
    candidate = candidate.resolve()
    if not candidate.is_dir():
        return None
    if not (candidate / ".git").exists():
        raise ValueError(
            f"path is not a git repository (missing .git): {candidate}"
        )
    return Project(
        id=candidate.name,
        path=candidate,
        default_branch=_detect_default_branch(candidate),
    )


def get_project(config: Config, ref: str) -> Project | None:
    """Resolve *ref* to a Project — config ID first, then filesystem path."""
    if not ref:
        return None
    project = config.projects.get(ref)
    if project is not None:
        return project
    return project_from_path(ref)
