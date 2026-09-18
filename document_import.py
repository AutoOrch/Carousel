"""P13 CLI: inventory historical folders and apply a frozen copy-only plan."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from config import load_config
from document.importer import apply_import_plan, build_import_plan


def _source_specs(args, config) -> list[dict]:
    specs = [{"path": value, "profile": args.profile or ""} for value in args.source]
    for value in args.source_profile:
        if "=" not in value:
            raise ValueError("--source-profile must be PATH=PROFILE")
        path, profile = value.rsplit("=", 1)
        specs.append({"path": path, "profile": profile})
    if not specs:
        specs = [{
            "path": str(source.path), "profile": source.profile,
            "project": source.project, "projects": source.projects,
            "actions": source.actions,
        } for source in config.documents.imports.sources]
    return specs


def _cli_actions(args) -> dict[str, bool | None]:
    """Merge --all with the per-action flags; explicit flags win over --all."""
    actions = {
        "validate": args.validate, "repair": args.repair,
        "consolidate": args.consolidate, "merge": args.merge,
    }
    if args.all is not None:
        for name, value in actions.items():
            if value is None:
                actions[name] = args.all
    return actions


def _print_result(result: dict) -> None:
    merged = [
        {
            "output_path": document["output_path"],
            "members": len(document.get("members") or []),
            "generation": document.get("generation", ""),
        }
        for document in result.get("merged_documents") or []
    ]
    print(json.dumps({
        "import_run_id": result["import_run_id"], "status": result["status"],
        "stats": result["stats"], "plan_path": result["plan_path"],
        "merged_documents": merged,
    }, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="P13 historical document importer")
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--source-profile", action="append", default=[], metavar="PATH=PROFILE")
    parser.add_argument("--profile", default="")
    parser.add_argument("--project", action="append", default=[])
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--mode", choices=("dry-run", "opencode"), default="dry-run",
                        help="dry-run: deterministic merge; opencode: LLM analyzes "
                             "and synthesizes the merged document")
    parser.add_argument("--all", action=argparse.BooleanOptionalAction, default=None,
                        help="enable validate + repair + consolidate + merge together "
                             "(per-action flags still override)")
    parser.add_argument("--validate", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--repair", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--consolidate", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--merge", action=argparse.BooleanOptionalAction, default=None,
                        help="read all files of each --source folder, analyze them "
                             "against the current code, and write ONE consolidated "
                             "document (requires validate; output via --merge-output)")
    parser.add_argument("--merge-output", default="",
                        help="explicit path of the consolidated document, e.g. "
                             "C:/Users/.../Desktop/服务质量-综合分析（合并版）.md "
                             "(default: <source-folder>-综合分析（合并版）.md beside "
                             "the source)")
    parser.add_argument("--repair-mode", choices=("annotate", "rewrite-draft"))
    actions_group = parser.add_mutually_exclusive_group()
    actions_group.add_argument("--plan-only", action="store_true",
                               help="print a zero-write frozen plan (planning is always read-only)")
    actions_group.add_argument("--run", action="store_true",
                               help="build the frozen plan and apply it immediately (copy-only)")
    actions_group.add_argument("--apply", type=Path, help="apply a previously saved JSON plan")
    args = parser.parse_args(argv)
    config = load_config()
    opencode_url = config.opencode_url
    if args.apply:
        plan = json.loads(args.apply.read_text(encoding="utf-8-sig"))
        _print_result(apply_import_plan(
            plan, config, mode=args.mode, opencode_url=opencode_url,
        ))
        return
    specs = _source_specs(args, config)
    plan = build_import_plan(
        config, specs, project_refs=args.project,
        cli_actions=_cli_actions(args), repair_mode=args.repair_mode,
        requested_revision=args.revision, merge_output=args.merge_output or None,
    )
    if args.run:
        _print_result(apply_import_plan(
            plan, config, mode=args.mode, opencode_url=opencode_url,
        ))
        return
    print(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
