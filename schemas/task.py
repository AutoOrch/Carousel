from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


TASK_TYPE_CODE_CHANGE = "CODE_CHANGE"
TASK_TYPE_ARCHITECTURE_INIT = "ARCHITECTURE_INIT"
VALID_TASK_TYPES = (TASK_TYPE_CODE_CHANGE, TASK_TYPE_ARCHITECTURE_INIT)


@dataclass
class Task:
    """A coding task claimed from the prompts folder and resolved to a project.

    One prompt file = one task = one project = one worktree
    = one OpenCode session = one commit.

    ``type`` distinguishes normal code changes (worktree + commit + merge)
    from architecture initialisation (read-only analysis producing assets
    under architecture-data/, no business-repo commits).
    """

    id: str
    prompt_file: Path
    project: str
    project_path: Path
    base_branch: str
    prompt: str
    type: str = TASK_TYPE_CODE_CHANGE
    title: str = ""
    test_command: str = ""
    agent: str = ""
    model: str = ""
    allowed_paths: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    simulate_failure: int = 0  # dry-run: fail on first N attempts (0 = never)
    run_id: str = ""           # requirement run this task belongs to
    allow_empty: bool = False  # code tasks require a real change by default
    # Effective dirty base-repo policy (resolved from project/global config
    # at claim time): refuse | allow | stash.
    dirty_base_policy: str = "refuse"
