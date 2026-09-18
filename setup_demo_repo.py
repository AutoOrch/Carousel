from __future__ import annotations

import subprocess
from pathlib import Path

from log import get_logger

logger = get_logger(__name__)


ROOT = Path(__file__).resolve().parent


def setup(repo: Path, title: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    readme = repo / "README.md"
    if not readme.exists():
        readme.write_text(f"# {title}\n\nLangGraph + OpenCode Demo\n", encoding="utf-8")

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True)

    if not (repo / ".git").exists():
        run("-c", "init.defaultBranch=master", "init")
        run("add", ".")
        run("-c", "user.name=Demo User", "-c", "user.email=demo@example.com",
            "commit", "-m", "initial commit")
        run("branch", "-M", "master")
        logger.info(f"Initialized demo repository: {repo}")
    else:
        logger.info(f"Demo repository already exists: {repo}")


setup(ROOT / "demo-repo", "Demo Project")
setup(ROOT / "demo-repo-2", "Demo Project 2")
