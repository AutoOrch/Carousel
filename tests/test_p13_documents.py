from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import runtime.task_store as task_store_module
from config import (
    Config, DocumentImportActions, DocumentImportProfile, DocumentImportsConfig,
    DocumentsConfig, Project, _validate_raw_config,
)
from document.importer import apply_import_plan, build_import_plan, effective_settings


def init_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "p13@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "P13 Test"], cwd=repo, check=True)
    (repo / "feature.py").write_text("class FeatureFlag:\n    enabled = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


def make_config(root: Path, repo: Path) -> Config:
    imports = DocumentImportsConfig(
        defaults=DocumentImportProfile(),
        profiles={
            "full": DocumentImportProfile(
                actions=DocumentImportActions(True, True, True, True),
                repair_mode="rewrite-draft",
            )
        },
        max_files=100,
        max_file_bytes=1024 * 1024,
        max_total_bytes=10 * 1024 * 1024,
    )
    docs = DocumentsConfig(
        archive_root=root / "projects", imports=imports,
        supported_extensions=[".md", ".txt"],
    )
    return Config(projects={"p": Project("p", repo, "main")}, documents=docs)


class P13ImportTests(unittest.TestCase):
    def test_plan_is_zero_write_and_freezes_actions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            source = root / "history"
            source.mkdir()
            (source / "analysis.md").write_text("# Analysis\nFeatureFlag is used.\n", encoding="utf-8")
            config = make_config(root, repo)
            plan = build_import_plan(
                config, [{"path": str(source), "profile": "full"}],
                project_refs=["p"],
            )
            self.assertFalse((root / "projects").exists())
            self.assertEqual(
                {"validate": True, "repair": True, "consolidate": True, "merge": True},
                plan["sources"][0]["actions"],
            )
            self.assertEqual(
                {"validate": "PLANNED", "repair": "PLANNED", "consolidate": "PLANNED",
                 "merge": "PLANNED"},
                plan["items"][0]["action_results"],
            )
            self.assertTrue(plan["plan_hash"].startswith("sha256:"))

    def test_repair_requires_validation_after_cli_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            config = make_config(root, repo)
            with self.assertRaisesRegex(ValueError, "repair=true requires validate=true"):
                effective_settings(
                    config, {"path": str(root), "profile": "full"},
                    {"validate": False, "repair": True, "consolidate": None},
                )

    def test_apply_copies_validates_repairs_and_summarizes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            source = root / "history"
            source.mkdir()
            original = source / "implementation.md"
            content = "# Feature implementation\n`FeatureFlag` must remain enabled in the implementation.\n"
            original.write_text(content, encoding="utf-8")
            config = make_config(root, repo)
            plan = build_import_plan(
                config, [{"path": str(source), "profile": "full"}],
                project_refs=["p"],
            )
            db = root / "tasks.db"
            with patch.object(task_store_module, "DB_FILE", db):
                store = task_store_module.TaskStore()
                package = apply_import_plan(
                    plan, config, store=store, projects_root=root / "projects",
                    runs_root=root / "import-runs",
                )
                persisted = store.get_document_import(plan["import_run_id"])
                store.close()
            self.assertEqual("COMPLETED", package["status"])
            self.assertEqual(content, original.read_text(encoding="utf-8"))
            self.assertEqual(1, package["stats"]["imported"])
            self.assertTrue(package["claims"])
            self.assertTrue(package["evidence"])
            self.assertEqual("feature.py", package["evidence"][0]["file_path"])
            self.assertEqual(1, package["evidence"][0]["line_number"])
            validation_path = Path(
                package["items"][0]["validation_artifacts"][0]["path"]
            )
            self.assertTrue(validation_path.is_file())
            self.assertEqual(1, package["stats"]["repairs"])
            self.assertEqual(1, package["stats"]["summaries"])
            self.assertTrue(Path(package["repairs"][0]["draft_path"]).is_file())
            self.assertTrue(Path(package["summaries"][0]["path"]).is_file())
            self.assertTrue((root / "projects" / "p" / "documents" / "INDEX.md").is_file())
            journal = next((root / "import-runs" / plan["import_run_id"] / "journals").glob("*.json"))
            self.assertEqual("COMMITTED", json.loads(journal.read_text())["status"])
            self.assertEqual("COMPLETED", persisted["status"])
            self.assertTrue(persisted["claims"])

    def test_apply_rejects_source_changed_after_plan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            source = root / "history"
            source.mkdir()
            original = source / "spec.md"
            original.write_text("# Spec\nFeatureFlag is used.\n", encoding="utf-8")
            config = make_config(root, repo)
            plan = build_import_plan(config, [{"path": str(source)}], project_refs=["p"])
            original.write_text("changed after plan", encoding="utf-8")
            with patch.object(task_store_module, "DB_FILE", root / "tasks.db"):
                store = task_store_module.TaskStore()
                package = apply_import_plan(
                    plan, config, store=store, projects_root=root / "projects",
                    runs_root=root / "import-runs",
                )
                store.close()
            self.assertEqual("PARTIAL", package["status"])
            self.assertEqual("FAILED", package["items"][0]["status"])
            self.assertFalse(any((root / "projects").glob("p/documents/*/spec.md")))

    def test_plan_hash_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            source = root / "history"
            source.mkdir()
            (source / "x.md").write_text("# x\nFeatureFlag is used.\n", encoding="utf-8")
            config = make_config(root, repo)
            plan = build_import_plan(config, [{"path": str(source)}], project_refs=["p"])
            plan["items"][0]["target_path"] = str(root / "outside.md")
            with self.assertRaisesRegex(ValueError, "plan hash"):
                apply_import_plan(plan, config, projects_root=root / "projects",
                                  runs_root=root / "runs")

    def test_normalized_duplicate_is_only_a_candidate_and_keeps_sources(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            source = root / "history"
            source.mkdir()
            (source / "a.md").write_text("# A\nFeatureFlag is used.\n", encoding="utf-8")
            (source / "b.md").write_text("#A\n\nFeatureFlag   is used.\n", encoding="utf-8")
            config = make_config(root, repo)
            plan = build_import_plan(config, [{"path": str(source)}], project_refs=["p"])
            self.assertEqual(["PLANNED", "PLANNED"], [i["status"] for i in plan["items"]])
            self.assertEqual("normalized_duplicate", plan["items"][1]["duplicate_kind"])
            self.assertTrue((source / "a.md").is_file())
            self.assertTrue((source / "b.md").is_file())

    def test_duplicate_source_keeps_its_requested_validation_action(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            first = root / "history-a"
            second = root / "history-b"
            first.mkdir()
            second.mkdir()
            content = "# Implementation\n`FeatureFlag` must remain enabled.\n"
            (first / "same.md").write_text(content, encoding="utf-8")
            (second / "same.md").write_text(content, encoding="utf-8")
            config = make_config(root, repo)
            plan = build_import_plan(
                config,
                [
                    {"path": str(first)},
                    {"path": str(second), "profile": "full"},
                ],
                project_refs=["p"],
            )
            with patch.object(task_store_module, "DB_FILE", root / "tasks.db"):
                store = task_store_module.TaskStore()
                package = apply_import_plan(
                    plan, config, store=store, projects_root=root / "projects",
                    runs_root=root / "import-runs",
                )
                store.close()
            duplicate = package["items"][1]
            self.assertEqual("DUPLICATE", duplicate["status"])
            self.assertTrue(duplicate["validation_results"])
            self.assertTrue(Path(
                duplicate["validation_artifacts"][0]["path"]
            ).is_file())

    def test_merge_requires_validation_after_cli_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            config = make_config(root, repo)
            with self.assertRaisesRegex(ValueError, "merge=true requires validate=true"):
                effective_settings(
                    config,
                    {"path": str(root), "actions": {"validate": False, "merge": True}},
                )

    def test_merged_config_rejects_merge_without_validate(self) -> None:
        from config import _validate_config
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            bad = make_config(root, repo)
            bad.documents.imports.defaults.actions.merge = True
            with self.assertRaisesRegex(ValueError, "merge=true requires validate=true"):
                _validate_config(bad)

    def test_apply_folds_normalized_duplicates_into_one_merged_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            source = root / "history"
            source.mkdir()
            content_a = "# A\nFeatureFlag is used.\n"
            content_b = "#A\n\nFeatureFlag   is used.\n"
            (source / "a.md").write_text(content_a, encoding="utf-8")
            (source / "b.md").write_text(content_b, encoding="utf-8")
            config = make_config(root, repo)
            plan = build_import_plan(
                config, [{"path": str(source), "profile": "full"}],
                project_refs=["p"],
            )
            self.assertEqual("normalized_duplicate", plan["items"][1]["duplicate_kind"])
            with patch.object(task_store_module, "DB_FILE", root / "tasks.db"):
                store = task_store_module.TaskStore()
                package = apply_import_plan(
                    plan, config, store=store, projects_root=root / "projects",
                    runs_root=root / "import-runs",
                )
                store.close()
            primary, folded = package["items"]
            self.assertEqual("IMPORTED", primary["status"])
            self.assertEqual("MERGED", folded["status"])
            self.assertEqual(primary["target_path"], folded["merged_into"])
            self.assertEqual(1, package["stats"]["imported"])
            self.assertEqual(1, package["stats"]["merged"])
            self.assertEqual(1, package["stats"]["merge_documents"])
            self.assertTrue(any((root / "projects").glob("p/documents/*/a.md")))
            self.assertFalse(any((root / "projects").glob("p/documents/*/b.md")))
            manifest = json.loads(
                (root / "projects" / "p" / "documents" / "MANIFEST.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(1, len(manifest["documents"]))
            output = Path(package["merged_documents"][0]["output_path"])
            self.assertTrue(output.is_file())
            merged_text = output.read_text(encoding="utf-8")
            self.assertIn("a.md", merged_text)
            self.assertIn("b.md", merged_text)
            self.assertEqual(
                "COMPLETED", primary["action_results"]["merge"]
            )
            self.assertEqual(
                "COMPLETED", folded["action_results"]["merge"]
            )
            self.assertTrue(folded["validation_results"])
            self.assertEqual(content_a, (source / "a.md").read_text(encoding="utf-8"))
            self.assertEqual(content_b, (source / "b.md").read_text(encoding="utf-8"))

    def test_apply_does_not_merge_across_source_folders(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            first = root / "history-a"
            second = root / "history-b"
            first.mkdir()
            second.mkdir()
            (first / "keep.md").write_text(
                "# Cache\nFeatureFlag enabled cache policy keep.\n", encoding="utf-8"
            )
            (second / "drop.md").write_text(
                "# Cache\nFeatureFlag enabled cache policy deprecated.\n", encoding="utf-8"
            )
            config = make_config(root, repo)
            plan = build_import_plan(
                config,
                [
                    {"path": str(first), "profile": "full"},
                    {"path": str(second), "profile": "full"},
                ],
                project_refs=["p"],
            )
            self.assertEqual([], plan["merge_groups"])
            with patch.object(task_store_module, "DB_FILE", root / "tasks.db"):
                store = task_store_module.TaskStore()
                package = apply_import_plan(
                    plan, config, store=store, projects_root=root / "projects",
                    runs_root=root / "import-runs",
                )
                store.close()
            self.assertEqual(2, package["stats"]["imported"])
            self.assertEqual(0, package["stats"].get("merged", 0))
            self.assertEqual(0, package["stats"]["merge_documents"])
            for item in package["items"]:
                self.assertEqual("IMPORTED", item["status"])
                self.assertEqual("SKIPPED", item["action_results"]["merge"])
                self.assertTrue(any(
                    (root / "projects").glob(f"p/documents/*/{item['relative_path']}")
                ))

    def test_apply_folds_folder_into_one_merged_document_with_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            source = root / "history"
            source.mkdir()
            content_keep = "# Cache\nFeatureFlag enabled cache policy keep.\n"
            content_drop = "# Cache\nFeatureFlag enabled cache policy deprecated.\n"
            (source / "keep.md").write_text(content_keep, encoding="utf-8")
            (source / "drop.md").write_text(content_drop, encoding="utf-8")
            config = make_config(root, repo)
            plan = build_import_plan(
                config, [{"path": str(source), "profile": "full"}],
                project_refs=["p"],
            )
            self.assertEqual(1, len(plan["merge_groups"]))
            self.assertEqual(2, len(plan["merge_groups"][0]["members"]))
            # Default output is frozen into the plan, beside the source folder.
            self.assertEqual(
                str((root / "history-综合分析（合并版）.md").resolve()),
                plan["merge_groups"][0]["output_path"],
            )
            with patch.object(task_store_module, "DB_FILE", root / "tasks.db"):
                store = task_store_module.TaskStore()
                package = apply_import_plan(
                    plan, config, store=store, projects_root=root / "projects",
                    runs_root=root / "import-runs",
                )
                store.close()
            primary, folded = package["items"]
            self.assertEqual("IMPORTED", primary["status"])
            self.assertEqual("MERGED", folded["status"])
            self.assertEqual(1, package["stats"]["imported"])
            self.assertEqual(1, package["stats"]["merged"])
            self.assertEqual(1, package["stats"]["merge_documents"])
            # Plan order is alphabetical: drop.md is the primary archive copy.
            self.assertTrue(any((root / "projects").glob("p/documents/*/drop.md")))
            self.assertFalse(any((root / "projects").glob("p/documents/*/keep.md")))
            manifest = json.loads(
                (root / "projects" / "p" / "documents" / "MANIFEST.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(1, len(manifest["documents"]))
            document = package["merged_documents"][0]
            self.assertEqual("rules", document["generation"])
            output = Path(document["output_path"])
            self.assertTrue(output.is_file())
            merged_text = output.read_text(encoding="utf-8")
            # Every member's actual content is included in the merged document.
            self.assertIn("FeatureFlag enabled cache policy keep.", merged_text)
            self.assertIn("FeatureFlag enabled cache policy deprecated.", merged_text)
            self.assertIn("keep.md", merged_text)
            self.assertIn("drop.md", merged_text)
            # Code evidence (fact + location) is included, without verdict labels.
            self.assertIn("feature.py", merged_text)
            for label in ("verified", "partially_verified", "not_implemented",
                          "unverifiable"):
                self.assertNotIn(label, merged_text)
            self.assertEqual("COMPLETED", primary["action_results"]["merge"])
            self.assertEqual("COMPLETED", folded["action_results"]["merge"])
            self.assertTrue(folded["validation_results"])
            self.assertEqual(content_keep, (source / "keep.md").read_text(encoding="utf-8"))
            self.assertEqual(content_drop, (source / "drop.md").read_text(encoding="utf-8"))

    def test_merge_output_explicit_path_and_llm_generation(self) -> None:
        import document.importer as importer_module
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            source = root / "history"
            source.mkdir()
            (source / "a.md").write_text("# A\nFeatureFlag is used.\n", encoding="utf-8")
            (source / "b.md").write_text("# B\nFeatureFlag is used too.\n", encoding="utf-8")
            config = make_config(root, repo)
            explicit = root / "桌面输出-综合分析（合并版）.md"
            plan = build_import_plan(
                config, [{"path": str(source), "profile": "full"}],
                project_refs=["p"], merge_output=str(explicit),
            )
            self.assertEqual(str(explicit.resolve()), plan["merge_groups"][0]["output_path"])
            llm_content = "# 综合分析（LLM）\n\n基于当前代码：FeatureFlag 位于 feature.py:1。"
            with patch.object(task_store_module, "DB_FILE", root / "tasks.db"), \
                    patch.object(importer_module, "_llm_merged_content",
                                 return_value=llm_content) as llm_mock:
                store = task_store_module.TaskStore()
                package = apply_import_plan(
                    plan, config, store=store, projects_root=root / "projects",
                    runs_root=root / "import-runs", mode="opencode",
                    opencode_url="http://127.0.0.1:4096",
                )
                store.close()
            llm_mock.assert_called_once()
            document = package["merged_documents"][0]
            self.assertEqual("llm", document["generation"])
            self.assertEqual(llm_content, explicit.read_text(encoding="utf-8"))
            for label in ("verified", "partially_verified", "not_implemented",
                          "unverifiable"):
                self.assertNotIn(label, explicit.read_text(encoding="utf-8"))

    def test_merge_output_refuses_to_write_inside_source_folder(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            source = root / "history"
            source.mkdir()
            (source / "a.md").write_text("# A\nFeatureFlag is used.\n", encoding="utf-8")
            (source / "b.md").write_text("# B\nFeatureFlag is used too.\n", encoding="utf-8")
            config = make_config(root, repo)
            with self.assertRaisesRegex(ValueError, "只读来源目录"):
                build_import_plan(
                    config, [{"path": str(source), "profile": "full"}],
                    project_refs=["p"],
                    merge_output=str(source / "out.md"),
                )

    def test_import_config_rejects_non_boolean_actions(self) -> None:
        with self.assertRaisesRegex(ValueError, "actions.validate must be bool"):
            _validate_raw_config({
                "documents": {"imports": {"defaults": {
                    "actions": {"validate": "yes"}
                }}}
            })

        with self.assertRaisesRegex(ValueError, "copy_source must be bool"):
            _validate_raw_config({
                "documents": {"imports": {"defaults": {
                    "copy_source": "false"
                }}}
            })


class P13CliMergeTests(unittest.TestCase):
    """--all merges the three action switches; --run merges plan + apply."""

    def test_all_flag_merges_four_actions(self) -> None:
        import document_import
        cases = [
            (Namespace(all=True, validate=None, repair=None, consolidate=None, merge=None),
             {"validate": True, "repair": True, "consolidate": True, "merge": True}),
            (Namespace(all=True, validate=None, repair=False, consolidate=None, merge=None),
             {"validate": True, "repair": False, "consolidate": True, "merge": True}),
            (Namespace(all=True, validate=None, repair=None, consolidate=None, merge=False),
             {"validate": True, "repair": True, "consolidate": True, "merge": False}),
            (Namespace(all=False, validate=True, repair=None, consolidate=None, merge=None),
             {"validate": True, "repair": False, "consolidate": False, "merge": False}),
            (Namespace(all=None, validate=True, repair=None, consolidate=None, merge=None),
             {"validate": True, "repair": None, "consolidate": None, "merge": None}),
        ]
        for args, expected in cases:
            self.assertEqual(expected, document_import._cli_actions(args))

    def test_cli_all_run_plans_and_applies_in_one_step(self) -> None:
        import document_import
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            source = root / "history"
            source.mkdir()
            content = "# Feature implementation\n`FeatureFlag` must remain enabled in the implementation.\n"
            (source / "implementation.md").write_text(content, encoding="utf-8")
            config = make_config(root, repo)
            stdout = io.StringIO()
            argv = [
                "document_import.py", "--source", str(source),
                "--project", "p", "--all", "--run",
            ]
            with patch.object(document_import, "load_config", return_value=config), \
                    patch.object(task_store_module, "DB_FILE", root / "tasks.db"), \
                    patch("runtime.assets.PROJECTS_ROOT", root / "projects"), \
                    patch.object(sys, "argv", argv), redirect_stdout(stdout):
                document_import.main()
            result = json.loads(stdout.getvalue())
            self.assertEqual("COMPLETED", result["status"])
            self.assertEqual(1, result["stats"]["imported"])
            self.assertTrue(result["stats"]["claims"])
            self.assertEqual(1, result["stats"]["repairs"])
            self.assertEqual(1, result["stats"]["summaries"])
            self.assertEqual(0, result["stats"]["merge_documents"])
            self.assertTrue(Path(result["plan_path"]).is_file())
            self.assertTrue(any(
                (root / "projects").glob("p/documents/*/implementation.md")
            ))
            self.assertEqual(content, (source / "implementation.md").read_text(encoding="utf-8"))

    def test_cli_run_rejects_combined_mode_flags(self) -> None:
        import document_import
        stderr = io.StringIO()
        argv = ["document_import.py", "--source", "x", "--run", "--apply", "plan.json"]
        with patch.object(sys, "argv", argv), redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                document_import.main()
        self.assertIn("not allowed with argument", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
