from __future__ import annotations

import subprocess
from pathlib import Path

from log import get_logger

logger = get_logger(__name__)


ROOT = Path(__file__).resolve().parent
REPO = ROOT / "demo-repo"
README = REPO / "README.md"

REPO.mkdir(parents=True, exist_ok=True)
if not README.exists():
    README.write_text("# Demo Project\n\nLangGraph + OpenCode Demo\n", encoding="utf-8")


def run(*args: str) -> None:
    subprocess.run(["git", *args], cwd=REPO, check=True)


if not (REPO / ".git").exists():
    run("-c", "init.defaultBranch=master", "init")
    run("add", ".")
    run("-c", "user.name=Demo User", "-c", "user.email=demo@example.com", "commit", "-m", "initial commit")
    run("branch", "-M", "master")
    logger.info(f"Initialized demo repository: {REPO}")
else:
    logger.info(f"Demo repository already exists: {REPO}")
