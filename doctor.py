"""Preflight diagnostics for the runner control plane."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import tempfile

from config import DB_FILE, PROJECTS_ROOT, PROMPTS_DIR, load_config
from opencode_client import OpenCodeClient


def check(run_tests: bool = False) -> dict:
    result: dict = {"ok": True, "checks": []}

    def add(name: str, ok: bool, detail: str) -> None:
        result["checks"].append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            result["ok"] = False

    try:
        config = load_config()
        add("config", True, "schema and ranges valid")
    except Exception as exc:
        add("config", False, str(exc))
        return result
    add("git", shutil.which("git") is not None, shutil.which("git") or "not found")
    for project in config.projects.values():
        git_dir = project.path / ".git"
        if not git_dir.exists():
            add(f"project:{project.id}", False, f"missing git repository: {project.path}")
            continue
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", project.default_branch],
            cwd=project.path, capture_output=True, text=True,
        )
        add(f"project:{project.id}", proc.returncode == 0,
            f"path={project.path}, branch={project.default_branch}")
        if run_tests and project.test_command:
            tested = subprocess.run(
                project.test_command, shell=True, cwd=project.path,
                capture_output=True, text=True, timeout=1800,
            )
            add(f"test:{project.id}", tested.returncode == 0,
                f"exit={tested.returncode}: {project.test_command}")
    for name, directory in (
        ("prompts", PROMPTS_DIR), ("runtime", DB_FILE.parent),
        ("projects", PROJECTS_ROOT),
    ):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory, delete=True):
                pass
            add(f"write:{name}", True, str(directory))
        except Exception as exc:
            add(f"write:{name}", False, str(exc))
    try:
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_FILE)
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        conn.close()
        add("sqlite", quick == "ok", quick)
    except Exception as exc:
        add("sqlite", False, str(exc))
    try:
        health = OpenCodeClient(config.opencode_url).health()
        add("opencode", True, json.dumps(health, ensure_ascii=False))
    except Exception as exc:
        # OpenCode is optional for dry-run, so expose it without failing the
        # whole doctor result.
        result["checks"].append({"name": "opencode", "ok": False,
                                 "optional": True, "detail": str(exc)})
    entry = config.architecture.archify_entry
    add("archify", bool(entry and __import__("pathlib").Path(entry).exists()),
        entry or "not configured")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Runner preflight diagnostics")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--run-tests", action="store_true",
                        help="also execute each configured project test command")
    args = parser.parse_args()
    result = check(args.run_tests)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in result["checks"]:
            mark = "OK" if item["ok"] else "WARN" if item.get("optional") else "FAIL"
            print(f"[{mark}] {item['name']}: {item['detail']}")
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
