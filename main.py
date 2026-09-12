from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from graph import build_graph
from log import get_logger, setup_logging
from opencode_client import OpenCodeClient

logger = get_logger(__name__)


ROOT = Path(__file__).resolve().parent
REPO = ROOT / "demo-repo"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LangGraph + OpenCode minimal demo")
    parser.add_argument(
        "--mode",
        choices=("dry-run", "opencode"),
        default="dry-run",
        help="dry-run simulates coding locally; opencode uses the HTTP server",
    )
    args = parser.parse_args()
    os.environ["DEMO_MODE"] = args.mode
    setup_logging()

    if not (REPO / ".git").exists():
        logger.error(f"Git repository not found: {REPO}")
        logger.error("Run: python setup_demo_repo.py")
        raise SystemExit(1)

    if args.mode == "opencode":
        try:
            health = OpenCodeClient().health()
            logger.info(f"OpenCode health: {health}")
        except Exception as exc:
            logger.error(f"OpenCode Server unavailable: {exc}")
            raise SystemExit(2) from exc

    graph = build_graph()
    result = graph.invoke({"repo": str(REPO), "tasks": [], "results": []})

    logger.info("\n==========================")
    logger.info("        FINISHED")
    logger.info("==========================")
    for item in result["results"]:
        logger.info(f"\n{item['task_id']}")
        logger.info(f"commit = {item['commit']}")
        logger.info(f"worktree = {item['worktree']}")
