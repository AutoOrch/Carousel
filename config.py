from __future__ import annotations

import subprocess
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.yaml"


def _resolve_data_root() -> Path:
    """Runtime data root: env RUNNER_DATA_ROOT > config.yaml data_root > ROOT.

    All runner-owned working directories (prompts, processing, projects, logs,
    the SQLite DB, the doc inbox, etc.) live under this root so source code
    and runtime data stay separated.
    """
    env = os.environ.get("RUNNER_DATA_ROOT")
    if env:
        return Path(env).resolve()
    try:
        raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        configured = raw.get("data_root") if isinstance(raw, dict) else None
        if configured:
            p = Path(str(configured))
            return (p if p.is_absolute() else (ROOT / p)).resolve()
    except OSError:
        pass
    return ROOT


DATA_ROOT = _resolve_data_root()

# Task lifecycle directories
PROMPTS_DIR = DATA_ROOT / "prompts"
PROCESSING_DIR = DATA_ROOT / "processing"
PROCESSED_DIR = DATA_ROOT / "processed"
FAILED_DIR = DATA_ROOT / "failed"
REPORTS_DIR = DATA_ROOT / "reports"

# Canonical runner-owned project assets.  ``--project`` selects the business
# repository, but reports, architecture snapshots, worktrees and collected
# documents stay in the runner control plane under this root.
PROJECTS_ROOT = DATA_ROOT / "projects"

# Legacy worktree root, read only by the one-version asset migrator.  New
# worktrees resolve to projects/<stable-project-id>/worktrees/<task_id>/.
WORKTREES_DIR = DATA_ROOT / "worktrees"

DB_FILE = DATA_ROOT / "runtime" / "tasks.db"

# Legacy architecture root, read only by compatibility/migration code.
ARCHITECTURE_ROOT = DATA_ROOT / "architecture-data"

DEFAULT_MAX_WORKERS = 3
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_OPENCODE_URL = "http://127.0.0.1:4096"
DEFAULT_OPENCODE_TIMEOUT = 1800
DIRTY_BASE_POLICIES = ("refuse", "allow", "stash")

# Fallback archify entry: the locally installed skill, when present.
_SKILL_ARCHIFY = Path.home() / ".agents" / "skills" / "archify" / "bin" / "archify.mjs"
DEFAULT_ARCHIFY_ENTRY = str(_SKILL_ARCHIFY) if _SKILL_ARCHIFY.exists() else ""


def config_version() -> str:
    data = CONFIG_FILE.read_bytes() if CONFIG_FILE.exists() else b""
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass
class ProjectArchitectureConfig:
    """Per-project architecture analysis settings (p10)."""

    enabled: bool = False
    auto_initialize: bool = False
    diagram_type: str = "architecture"
    max_core_nodes: int = 12
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=lambda: [
        "vendor", "node_modules", ".git", "dist", "build",
    ])


@dataclass
class Project:
    id: str
    path: Path
    default_branch: str = "main"
    language: str = ""
    test_command: str = ""
    agent: str = ""
    model: str = ""
    # Document pipeline (p11 §5): rule-based project identification keys.
    keywords: list[str] = field(default_factory=list)
    # Dirty base-repo handling override: "" inherits worker.dirty_base_policy.
    dirty_base_policy: str = ""
    architecture: ProjectArchitectureConfig = field(
        default_factory=ProjectArchitectureConfig
    )


@dataclass
class ArchitectureConfig:
    """Global architecture asset settings (p10)."""

    # Kept for API compatibility.  New writes are resolved through
    # runtime.assets and always land below PROJECTS_ROOT.
    root_dir: Path = PROJECTS_ROOT
    default_snapshot: str = "baseline"
    archify_command: str = "node"
    archify_entry: str = DEFAULT_ARCHIFY_ENTRY
    validation_enabled: bool = True
    rendering_enabled: bool = True


@dataclass
class FinalReviewConfig:
    enabled: bool = True
    auto_replan: bool = True
    max_rounds: int = 3
    require_tests: bool = True
    fail_on_high_risk: bool = True
    reviewer_agent: str = "plan"
    reviewer_model: str = ""


@dataclass
class DocumentImportActions:
    validate: bool = False
    repair: bool = False
    consolidate: bool = False
    merge: bool = False


@dataclass
class DocumentImportProfile:
    actions: DocumentImportActions = field(default_factory=DocumentImportActions)
    recursive: bool = True
    copy_source: bool = True
    repair_mode: str = "annotate"
    review_required: bool = True


@dataclass
class DocumentImportSource:
    path: Path
    profile: str = ""
    project: str = ""
    projects: list[str] = field(default_factory=list)
    actions: dict[str, bool] = field(default_factory=dict)


@dataclass
class DocumentImportsConfig:
    defaults: DocumentImportProfile = field(default_factory=DocumentImportProfile)
    profiles: dict[str, DocumentImportProfile] = field(default_factory=dict)
    sources: list[DocumentImportSource] = field(default_factory=list)
    exclude_dirs: list[str] = field(default_factory=lambda: [
        ".git", ".venv", "node_modules", "vendor", "dist", "build",
        "__pycache__",
    ])
    max_files: int = 10000
    max_file_bytes: int = 50 * 1024 * 1024
    max_total_bytes: int = 2 * 1024 * 1024 * 1024


@dataclass
class DocumentsConfig:
    """Document pipeline settings (p11)."""

    input_dir: Path = DATA_ROOT / "doc"
    review_dir: Path = DATA_ROOT / "doc" / "review"
    processing_dir: Path = DATA_ROOT / "processing" / "documents"
    processed_dir: Path = DATA_ROOT / "processed" / "documents"
    # Internal/test override for the project asset root.  load_config() always
    # supplies the canonical PROJECTS_ROOT so production consumers agree.
    archive_root: Path = PROJECTS_ROOT
    confidence_threshold: float = 0.8   # below → doc/review/, never archived
    max_workers: int = 4
    content_preview_chars: int = 4000
    supported_extensions: list[str] = field(default_factory=lambda: [
        ".md", ".markdown", ".txt", ".pdf", ".docx", ".pptx", ".xlsx",
    ])
    imports: DocumentImportsConfig = field(default_factory=DocumentImportsConfig)


@dataclass
class Config:
    projects: dict[str, Project] = field(default_factory=dict)
    max_workers: int = DEFAULT_MAX_WORKERS
    poll_interval: float = DEFAULT_POLL_INTERVAL
    # How CODE_CHANGE tasks handle uncommitted changes in the base repo:
    # refuse (fail fast) | allow (warn + proceed) | stash (auto-stash/pop).
    dirty_base_policy: str = "refuse"
    opencode_url: str = DEFAULT_OPENCODE_URL
    # Total seconds one OpenCode agent run may take (send_message budget).
    opencode_timeout: int = DEFAULT_OPENCODE_TIMEOUT
    max_attempts: int = 3
    backoff_seconds: float = 10.0
    lease_timeout: int = 300        # seconds before a lease is considered stale
    heartbeat_interval: int = 30    # seconds between heartbeat updates
    final_review: FinalReviewConfig = field(default_factory=FinalReviewConfig)
    architecture: ArchitectureConfig = field(default_factory=ArchitectureConfig)
    documents: DocumentsConfig = field(default_factory=DocumentsConfig)


def load_config() -> Config:
    data: dict = {}
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    _validate_raw_config(data)

    projects: dict[str, Project] = {}
    for pid, pdata in (data.get("projects") or {}).items():
        pdata = pdata or {}
        raw_path = Path(pdata.get("path", ""))
        project_path = raw_path if raw_path.is_absolute() else (ROOT / raw_path)
        project_path = project_path.resolve()

        opencode = pdata.get("opencode") or {}
        test = pdata.get("test") or {}
        arch = pdata.get("architecture") or {}

        projects[pid] = Project(
            id=pid,
            path=project_path,
            default_branch=pdata.get("default_branch", "main"),
            language=pdata.get("language", ""),
            test_command=test.get("command", "") if isinstance(test, dict) else "",
            agent=opencode.get("agent", "") if isinstance(opencode, dict) else "",
            model=opencode.get("model", "") if isinstance(opencode, dict) else "",
            keywords=[str(k) for k in (pdata.get("keywords") or [])],
            dirty_base_policy=str(pdata.get("dirty_base_policy", "") or "").strip().lower(),
            architecture=ProjectArchitectureConfig(
                enabled=bool(arch.get("enabled", False)),
                auto_initialize=bool(arch.get("auto_initialize", False)),
                diagram_type=str(arch.get("diagram_type", "architecture")),
                max_core_nodes=int(arch.get("max_core_nodes", 12)),
                include=list(arch.get("include") or []),
                exclude=list(arch.get("exclude") or [
                    "vendor", "node_modules", ".git", "dist", "build",
                ]),
            ),
        )

    worker = data.get("worker") or {}
    opencode = data.get("opencode") or {}
    retry = data.get("retry") or {}
    recovery = data.get("recovery") or {}
    final_review = data.get("final_review") or {}
    arch = data.get("architecture") or {}
    docs = data.get("documents") or {}

    fr = FinalReviewConfig(
        enabled=bool(final_review.get("enabled", True)),
        auto_replan=bool(final_review.get("auto_replan", True)),
        max_rounds=int(final_review.get("max_rounds", 3)),
        require_tests=bool(final_review.get("require_tests", True)),
        fail_on_high_risk=bool(final_review.get("fail_on_high_risk", True)),
        reviewer_agent=str(final_review.get("reviewer_agent", "plan")),
        reviewer_model=str(final_review.get("reviewer_model", "")),
    )

    def _doc_dir(key: str, default: str) -> Path:
        raw = Path(str(docs.get(key, default)))
        return raw if raw.is_absolute() else (DATA_ROOT / raw).resolve()

    doc_input = _doc_dir("input_dir", "./doc")

    def _actions(raw: object) -> DocumentImportActions:
        block = raw if isinstance(raw, dict) else {}
        return DocumentImportActions(
            validate=bool(block.get("validate", False)),
            repair=bool(block.get("repair", False)),
            consolidate=bool(block.get("consolidate", False)),
            merge=bool(block.get("merge", False)),
        )

    def _profile(raw: object, base: DocumentImportProfile | None = None) -> DocumentImportProfile:
        block = raw if isinstance(raw, dict) else {}
        parent = base or DocumentImportProfile()
        raw_actions = block.get("actions") if isinstance(block.get("actions"), dict) else {}
        actions = DocumentImportActions(
            validate=bool(raw_actions.get("validate", parent.actions.validate)),
            repair=bool(raw_actions.get("repair", parent.actions.repair)),
            consolidate=bool(raw_actions.get("consolidate", parent.actions.consolidate)),
            merge=bool(raw_actions.get("merge", parent.actions.merge)),
        )
        return DocumentImportProfile(
            actions=actions,
            recursive=bool(block.get("recursive", parent.recursive)),
            copy_source=bool(block.get("copy_source", parent.copy_source)),
            repair_mode=str(block.get("repair_mode", parent.repair_mode)),
            review_required=bool(block.get("review_required", parent.review_required)),
        )

    imports_raw = docs.get("imports") or {}
    defaults = _profile(imports_raw.get("defaults") or {})
    profiles = {
        str(name): _profile(value, defaults)
        for name, value in (imports_raw.get("profiles") or {}).items()
    }
    import_sources: list[DocumentImportSource] = []
    for source in imports_raw.get("sources") or []:
        raw_source_path = Path(str(source.get("path", "")))
        resolved_source = (
            raw_source_path if raw_source_path.is_absolute()
            else ROOT / raw_source_path
        ).resolve()
        source_actions = source.get("actions") or {}
        import_sources.append(DocumentImportSource(
            path=resolved_source,
            profile=str(source.get("profile", "") or ""),
            project=str(source.get("project", "") or ""),
            projects=[str(x) for x in (source.get("projects") or [])],
            actions={str(k): bool(v) for k, v in source_actions.items()},
        ))
    import_cfg = DocumentImportsConfig(
        defaults=defaults,
        profiles=profiles,
        sources=import_sources,
        exclude_dirs=[str(x) for x in imports_raw.get(
            "exclude_dirs", DocumentImportsConfig().exclude_dirs
        )],
        max_files=int(imports_raw.get("max_files", 10000)),
        max_file_bytes=int(imports_raw.get("max_file_bytes", 50 * 1024 * 1024)),
        max_total_bytes=int(imports_raw.get("max_total_bytes", 2 * 1024 * 1024 * 1024)),
    )
    dcfg = DocumentsConfig(
        input_dir=doc_input,
        review_dir=_doc_dir("review_dir", str(doc_input / "review")),
        processing_dir=_doc_dir("processing_dir", "./processing/documents"),
        processed_dir=_doc_dir("processed_dir", "./processed/documents"),
        archive_root=PROJECTS_ROOT,
        confidence_threshold=float(docs.get("confidence_threshold", 0.8)),
        max_workers=int(docs.get("max_workers", 4)),
        content_preview_chars=int(docs.get("content_preview_chars", 4000)),
        supported_extensions=[
            str(e).lower()
            for e in (docs.get("supported_extensions") or [
                ".md", ".markdown", ".txt", ".pdf", ".docx", ".pptx", ".xlsx",
            ])
        ],
        imports=import_cfg,
    )

    ac = ArchitectureConfig(
        root_dir=PROJECTS_ROOT,
        default_snapshot=str(arch.get("default_snapshot", "baseline")),
        archify_command=str(arch.get("archify_command", "node")),
        archify_entry=str(arch.get("archify_entry", "") or DEFAULT_ARCHIFY_ENTRY),
        validation_enabled=bool(arch.get("validation", {}).get("enabled", True)
                                if isinstance(arch.get("validation"), dict) else True),
        rendering_enabled=bool(arch.get("rendering", {}).get("enabled", True)
                               if isinstance(arch.get("rendering"), dict) else True),
    )

    config = Config(
        projects=projects,
        max_workers=int(worker.get("max_workers", DEFAULT_MAX_WORKERS)),
        poll_interval=float(worker.get("poll_interval", DEFAULT_POLL_INTERVAL)),
        dirty_base_policy=str(worker.get("dirty_base_policy", "refuse") or "refuse").strip().lower(),
        opencode_url=opencode.get("base_url", DEFAULT_OPENCODE_URL),
        opencode_timeout=int(opencode.get("timeout", DEFAULT_OPENCODE_TIMEOUT)),
        max_attempts=int(retry.get("max_attempts", 3)),
        backoff_seconds=float(retry.get("backoff_seconds", 10.0)),
        lease_timeout=int(recovery.get("lease_timeout", 300)),
        heartbeat_interval=int(recovery.get("heartbeat_interval", 30)),
        final_review=fr,
        architecture=ac,
        documents=dcfg,
    )
    _validate_config(config)
    return config


def _validate_raw_config(data: dict) -> None:
    """Reject common YAML coercion mistakes before Python casts hide them."""
    if not isinstance(data, dict):
        raise ValueError("invalid config: root must be a mapping")
    allowed_sections = {
        "projects", "worker", "retry", "recovery", "opencode",
        "architecture", "final_review", "documents", "data_root",
    }
    unknown = sorted(set(data) - allowed_sections)
    errors = [f"unknown top-level field: {name}" for name in unknown]

    checks = {
        ("worker", "max_workers"): (int,),
        ("worker", "poll_interval"): (int, float),
        ("worker", "dirty_base_policy"): (str,),
        ("retry", "max_attempts"): (int,),
        ("retry", "backoff_seconds"): (int, float),
        ("recovery", "lease_timeout"): (int,),
        ("recovery", "heartbeat_interval"): (int,),
        ("final_review", "enabled"): (bool,),
        ("final_review", "auto_replan"): (bool,),
        ("final_review", "max_rounds"): (int,),
        ("final_review", "require_tests"): (bool,),
        ("final_review", "fail_on_high_risk"): (bool,),
        ("documents", "confidence_threshold"): (int, float),
        ("documents", "max_workers"): (int,),
        ("documents", "content_preview_chars"): (int,),
        ("opencode", "timeout"): (int,),
    }
    for (section, key), expected in checks.items():
        block = data.get(section) or {}
        if not isinstance(block, dict):
            errors.append(f"{section} must be a mapping")
            continue
        if key in block and (isinstance(block[key], bool) and bool not in expected
                             or not isinstance(block[key], expected)):
            names = "/".join(t.__name__ for t in expected)
            errors.append(f"{section}.{key} must be {names}")
    projects = data.get("projects") or {}
    if not isinstance(projects, dict):
        errors.append("projects must be a mapping")
    else:
        worker_block = data.get("worker") or {}
        global_policy = ""
        if isinstance(worker_block, dict):
            global_policy = str(worker_block.get("dirty_base_policy", "") or "").strip().lower()
            if global_policy and global_policy not in DIRTY_BASE_POLICIES:
                errors.append(
                    "worker.dirty_base_policy must be one of " + "/".join(DIRTY_BASE_POLICIES)
                )
        for pid, pdata in projects.items():
            if not isinstance(pdata, dict):
                continue
            policy = str(pdata.get("dirty_base_policy", "") or "").strip().lower()
            if policy and policy not in DIRTY_BASE_POLICIES:
                errors.append(
                    f"projects.{pid}.dirty_base_policy must be one of "
                    + "/".join(DIRTY_BASE_POLICIES)
                )
    docs = data.get("documents") or {}
    imports = docs.get("imports") or {} if isinstance(docs, dict) else {}
    if imports and not isinstance(imports, dict):
        errors.append("documents.imports must be a mapping")
    elif isinstance(imports, dict):
        for key in ("max_files", "max_file_bytes", "max_total_bytes"):
            if key in imports and (
                isinstance(imports[key], bool) or not isinstance(imports[key], int)
            ):
                errors.append(f"documents.imports.{key} must be int")
        raw_profiles = imports.get("profiles") or {}
        if not isinstance(raw_profiles, dict):
            errors.append("documents.imports.profiles must be a mapping")
            raw_profiles = {}
        for label, raw_profile in [
            ("defaults", imports.get("defaults") or {}),
            *[(f"profiles.{name}", value) for name, value in raw_profiles.items()],
        ]:
            if not isinstance(raw_profile, dict):
                errors.append(f"documents.imports.{label} must be a mapping")
                continue
            actions = raw_profile.get("actions") or {}
            if not isinstance(actions, dict):
                errors.append(f"documents.imports.{label}.actions must be a mapping")
                continue
            for action in ("validate", "repair", "consolidate", "merge"):
                if action in actions and not isinstance(actions[action], bool):
                    errors.append(
                        f"documents.imports.{label}.actions.{action} must be bool"
                    )
            for option in ("recursive", "copy_source", "review_required"):
                if option in raw_profile and not isinstance(raw_profile[option], bool):
                    errors.append(
                        f"documents.imports.{label}.{option} must be bool"
                    )
            mode = str(raw_profile.get("repair_mode", "annotate"))
            if mode not in ("annotate", "rewrite-draft"):
                errors.append(
                    f"documents.imports.{label}.repair_mode must be annotate or rewrite-draft"
                )
        raw_sources = imports.get("sources") or []
        if not isinstance(raw_sources, list):
            errors.append("documents.imports.sources must be a list")
        else:
            for index, source in enumerate(raw_sources):
                if not isinstance(source, dict):
                    errors.append(f"documents.imports.sources[{index}] must be a mapping")
                    continue
                if not str(source.get("path") or "").strip():
                    errors.append(f"documents.imports.sources[{index}].path is required")
                actions = source.get("actions") or {}
                if not isinstance(actions, dict):
                    errors.append(
                        f"documents.imports.sources[{index}].actions must be a mapping"
                    )
                    continue
                for action in ("validate", "repair", "consolidate", "merge"):
                    if action in actions and not isinstance(actions[action], bool):
                        errors.append(
                            f"documents.imports.sources[{index}].actions.{action} must be bool"
                        )
    if errors:
        raise ValueError("invalid config:\n- " + "\n- ".join(errors))


def _validate_config(config: Config) -> None:
    errors: list[str] = []
    if config.max_workers < 1:
        errors.append("worker.max_workers must be >= 1")
    if config.poll_interval <= 0:
        errors.append("worker.poll_interval must be > 0")
    if config.max_attempts < 1:
        errors.append("retry.max_attempts must be >= 1")
    if config.backoff_seconds < 0:
        errors.append("retry.backoff_seconds must be >= 0")
    if config.heartbeat_interval < 1:
        errors.append("recovery.heartbeat_interval must be >= 1")
    if config.lease_timeout <= config.heartbeat_interval:
        errors.append("recovery.lease_timeout must exceed heartbeat_interval")
    if config.final_review.max_rounds < 1:
        errors.append("final_review.max_rounds must be >= 1")
    if not 0 <= config.documents.confidence_threshold <= 1:
        errors.append("documents.confidence_threshold must be between 0 and 1")
    if config.documents.max_workers < 1:
        errors.append("documents.max_workers must be >= 1")
    imports = config.documents.imports
    if imports.max_files < 1 or imports.max_file_bytes < 1 or imports.max_total_bytes < 1:
        errors.append("documents.imports size limits must be >= 1")
    for name, profile in {"defaults": imports.defaults, **imports.profiles}.items():
        if profile.actions.repair and not profile.actions.validate:
            errors.append(
                f"documents.imports.{name}: repair=true requires validate=true"
            )
        if profile.actions.merge and not profile.actions.validate:
            errors.append(
                f"documents.imports.{name}: merge=true requires validate=true"
            )
        if profile.repair_mode not in ("annotate", "rewrite-draft"):
            errors.append(
                f"documents.imports.{name}.repair_mode must be annotate or rewrite-draft"
            )
        if not profile.copy_source:
            errors.append(
                f"documents.imports.{name}.copy_source must be true in P13 v1"
            )
    for source in imports.sources:
        if source.profile and source.profile not in imports.profiles:
            errors.append(
                f"documents.imports source {source.path}: unknown profile {source.profile}"
            )
            continue
        base = imports.profiles.get(source.profile, imports.defaults)
        validate = source.actions.get("validate", base.actions.validate)
        repair = source.actions.get("repair", base.actions.repair)
        merge = source.actions.get("merge", base.actions.merge)
        if repair and not validate:
            errors.append(
                f"documents.imports source {source.path}: repair=true requires validate=true"
            )
        if merge and not validate:
            errors.append(
                f"documents.imports source {source.path}: merge=true requires validate=true"
            )
    if errors:
        raise ValueError("invalid config:\n- " + "\n- ".join(errors))


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
        id=(candidate.name + "-" + hashlib.sha256(
            candidate.as_posix().lower().encode("utf-8")
        ).hexdigest()[:8]),
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
    project = project_from_path(ref)
    return project
