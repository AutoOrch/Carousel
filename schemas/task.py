from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Task:
    """A coding task claimed from the prompts folder and resolved to a project.

    One prompt file = one task = one project = one worktree
    = one OpenCode session = one commit.
    """

    id: str
    prompt_file: Path
    project: str
    project_path: Path
    base_branch: str
    prompt: str
    title: str = ""
    test_command: str = ""
    agent: str = ""
    model: str = ""
    allowed_paths: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    simulate_failure: int = 0  # dry-run: fail on first N attempts (0 = never)
