from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import json
import hashlib
from pathlib import Path
from unittest.mock import patch

import planner
import runtime.task_store as task_store_module
import task_graph
from architecture.repository import ArchitectureRepository
from config import ArchitectureConfig, Config, DocumentsConfig, Project, _validate_raw_config
from document.classifier import classify_one
from document.executor import _execute_one, _write_journal, reconcile_journals
from document.planner import load_hash_index
from document.schemas import ACTION_ARCHIVE, DocFile, DocumentPlan, ExtractedDoc
from runtime.audit_export import export_run
import runtime.assets as assets_module
from schemas.task import Task
from runtime.worker_pool import WorkerPool
from runtime.recovery_manager import RecoveryManager
import runtime.recovery_manager as recovery_module


def init_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"],
                   cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=repo, check=True)
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True,
                   capture_output=True)
    return repo


class PlanValidationTests(unittest.TestCase):
    def test_rejects_cycle_and_unsafe_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "cycle"):
            planner.validate_plan([
                {"id": "a", "prompt": "a", "depends_on": ["b"]},
                {"id": "b", "prompt": "b", "depends_on": ["a"]},
            ])
        with self.assertRaisesRegex(ValueError, "unsafe"):
            planner.validate_plan([
                {"id": "a", "prompt": "a", "allowed_paths": ["../secret"]},
            ])

    def test_plan_publish_rolls_back_as_a_set(self) -> None:
        with tempfile.TemporaryDirectory() as raw, \
             patch.object(planner, "PROMPTS_DIR", Path(raw)), \
             patch.object(planner, "_max_existing_seq", return_value=0):
            (Path(raw) / "task-002-b.md").write_text("occupied", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                planner.write_tasks([
                    {"id": "task-001-a", "prompt": "a"},
                    {"id": "task-002-b", "prompt": "b"},
                ], "p", "run-x")
            self.assertFalse((Path(raw) / "task-001-a.md").exists())

    def test_config_schema_rejects_coerced_boolean(self) -> None:
        with self.assertRaisesRegex(ValueError, "final_review.enabled"):
            _validate_raw_config({"final_review": {"enabled": "false"}})


class StoreTests(unittest.TestCase):
    def test_attempts_survive_requeue_and_heartbeat_is_owned(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.object(
            task_store_module, "DB_FILE", Path(raw) / "tasks.db"
        ):
            store = task_store_module.TaskStore()
            self.assertTrue(store.create_task("t", "processing/t.md", "p", "r"))
            lease = store.claim_lease("t", "worker-a")
            attempt, _ = store.begin_attempt("t", "worker-a", lease)
            self.assertFalse(store.update_heartbeat("t", "worker-b", lease))
            self.assertTrue(store.update_heartbeat("t", "worker-a", lease))
            store.record_attempt("t", attempt, "FAILED", "TEST_FAILURE", "x")
            store.clear_lease("t")
            store.transition_task("t", "PENDING")
            self.assertTrue(store.create_task("t", "processing/t.md", "p", "r"))
            lease = store.claim_lease("t", "worker-a")
            attempt2, _ = store.begin_attempt("t", "worker-a", lease)
            self.assertEqual(2, attempt2)
            self.assertEqual(2, store.get_task("t")["attempt"])
            self.assertGreaterEqual(len(store.list_events(task_id="t")), 6)
            store.close()

    def test_recovery_reconciles_file_moved_before_terminal_db_update(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.object(
            task_store_module, "DB_FILE", Path(raw) / "tasks.db"
        ):
            root = Path(raw)
            processing, processed = root / "processing", root / "processed"
            processing.mkdir(); processed.mkdir()
            moved = processed / "t.md"
            moved.write_text("done", encoding="utf-8")
            store = task_store_module.TaskStore()
            store.create_task("t", str(processing / "t.md"), "p")
            lease = store.claim_lease("t", "w")
            store.begin_attempt("t", "w", lease)
            store._conn.execute("UPDATE tasks SET heartbeat_at='2000-01-01T00:00:00+00:00'")
            store._conn.commit()
            with patch.object(recovery_module, "PROCESSING_DIR", processing), \
                 patch.object(recovery_module, "PROCESSED_DIR", processed), \
                 patch.object(recovery_module, "FAILED_DIR", root / "failed"), \
                 patch.object(recovery_module, "PROMPTS_DIR", root / "prompts"):
                RecoveryManager(store, Config(lease_timeout=2, heartbeat_interval=1)).recover_stale_tasks()
            self.assertEqual("COMPLETED", store.get_task("t")["status"])
            store.close()

    def test_recovery_discovers_commit_from_trailers(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.object(
            task_store_module, "DB_FILE", Path(raw) / "tasks.db"
        ):
            root = Path(raw)
            repo = init_repo(root)
            processing = root / "processing"; processing.mkdir()
            prompt = processing / "t.md"; prompt.write_text("task", encoding="utf-8")
            subprocess.run(["git", "checkout", "-b", "agent/t"], cwd=repo, check=True, capture_output=True)
            (repo / "x.txt").write_text("x", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "agent\n\nTask-ID: t\nAttempt-ID: a\nRun-ID: r"], cwd=repo, check=True, capture_output=True)
            candidate = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                                       capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
            task = Task("t", prompt, "p", repo, "main", "task")
            store = task_store_module.TaskStore()
            store.create_task("t", str(prompt), "p", "r")
            lease = store.claim_lease("t", "w")
            store.begin_attempt("t", "w", lease)
            store.update_checkpoint("t", "commit[1]")
            store._conn.execute("UPDATE tasks SET heartbeat_at='2000-01-01T00:00:00+00:00'")
            store._conn.commit()
            with patch.object(recovery_module, "PROCESSING_DIR", processing), \
                 patch.object(recovery_module, "PROCESSED_DIR", root / "processed"), \
                 patch.object(recovery_module, "FAILED_DIR", root / "failed"), \
                 patch.object(recovery_module, "PROMPTS_DIR", root / "prompts"), \
                 patch.object(recovery_module, "resolve_task", return_value=task):
                RecoveryManager(
                    store, Config(projects={"p": Project("p", repo)},
                                  max_attempts=3, lease_timeout=2, heartbeat_interval=1)
                ).recover_stale_tasks()
            row = store.get_task("t")
            self.assertEqual(candidate, row["commit_sha"])
            self.assertEqual("commit_done", row["checkpoint"])
            self.assertEqual("PENDING", row["status"])
            store.close()

    def test_only_one_store_can_claim_a_lease(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.object(
            task_store_module, "DB_FILE", Path(raw) / "tasks.db"
        ):
            first = task_store_module.TaskStore()
            second = task_store_module.TaskStore()
            first.create_task("t", "processing/t.md", "p")
            self.assertTrue(first.claim_lease("t", "worker-a"))
            with self.assertRaisesRegex(RuntimeError, "not leaseable"):
                second.claim_lease("t", "worker-b")
            first.close()
            second.close()

    def test_plan_entities_and_multi_project_revisions_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.object(
            task_store_module, "DB_FILE", Path(raw) / "tasks.db"
        ):
            store = task_store_module.TaskStore()
            store.create_run("r", "req.md", "sha256:x", "p1,p2", "base", "dry-run")
            store.register_run_projects("r", [
                {"id": "p1", "path": "a", "base_revision": "a1"},
                {"id": "p2", "path": "b", "base_revision": "b1"},
            ])
            store.record_plan("r", 1, "plan.json", [
                {"id": "a", "depends_on": []},
                {"id": "b", "depends_on": ["a"]},
            ])
            store.update_run_project_final("r", "p1", "a2")
            store.update_run_project_final("r", "p2", "b2")
            self.assertEqual(["a2", "b2"], [p["final_revision"] for p in store.get_run_projects("r")])
            package = export_run("r", Path(raw) / "tasks.db")
            self.assertEqual(1, len(package["plans"]))
            self.assertEqual([{"task_id": "b", "depends_on_task_id": "a"}], package["dependencies"])
            store.close()

    def test_validation_output_redacts_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.object(
            task_store_module, "DB_FILE", Path(raw) / "tasks.db"
        ):
            store = task_store_module.TaskStore()
            store.create_task("t", "t.md", "p")
            store.record_validation("t", "", "test", "FAILED", output="token=super-secret-value")
            self.assertIn("[REDACTED]", store.list_validations("t")[0]["output"])
            store.close()

    def test_missing_artifact_is_reconciled_as_lost(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.object(
            task_store_module, "DB_FILE", Path(raw) / "tasks.db"
        ):
            store = task_store_module.TaskStore()
            store.register_artifact("REPORT", str(Path(raw) / "missing.md"), run_id="r")
            result = store.reconcile_artifacts()
            self.assertEqual(1, result["lost"])
            row = store._conn.execute("SELECT status FROM artifacts").fetchone()
            self.assertEqual("LOST", row["status"])
            store.close()


class TaskGraphTests(unittest.TestCase):
    def test_worker_pool_persists_complete_trace(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.object(
            task_store_module, "DB_FILE", Path(raw) / "tasks.db"
        ):
            root = Path(raw)
            repo = init_repo(root)
            processing = root / "processing"
            processing.mkdir()
            prompt = processing / "tracked.md"
            prompt.write_text("# tracked", encoding="utf-8")
            task = Task("tracked", prompt, "demo", repo, "main", "tracked",
                        allowed_paths=["README.md"])
            store = task_store_module.TaskStore()
            self.assertTrue(store.create_task(task.id, str(prompt), task.project, ""))
            env = {"EXEC_MODE": "dry-run", "MAX_ATTEMPTS": "2", "BACKOFF_SECONDS": "0"}
            with patch.dict(os.environ, env), \
                 patch.object(task_graph, "PROCESSED_DIR", root / "processed"), \
                 patch.object(task_graph, "FAILED_DIR", root / "failed"), \
                 patch.object(task_graph, "task_report_path", lambda p, t: root / "reports" / f"{t}.md"), \
                 patch.object(task_graph, "worktree_path", lambda p, t: root / "worktrees" / p / t):
                pool = WorkerPool(max_workers=1, task_store=store, heartbeat_interval=1)
                pool.submit(task)
                pool.join()
                pool.shutdown()
            row = store.get_task(task.id)
            self.assertEqual("COMPLETED", row["status"])
            self.assertEqual(1, row["attempt"])
            self.assertTrue(row["merge_commit_sha"])
            self.assertTrue(store.list_validations(task.id))
            event_types = {e["event_type"] for e in store.list_events(task_id=task.id)}
            self.assertIn("attempt.started", event_types)
            self.assertIn("task.validation.finished", event_types)
            self.assertIn("operation.merge.finished", event_types)
            store.close()
            task_graph.set_task_store(None)

    def test_transient_execution_error_does_not_poison_retry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            processing = root / "processing"
            processing.mkdir()
            prompt = processing / "retry.md"
            prompt.write_text("# retry", encoding="utf-8")
            calls = {"count": 0}

            def flaky(task, worktree, **kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError("temporary transport failure")
                (Path(worktree) / "ok.txt").write_text("ok", encoding="utf-8")
                return {"response": {"message": "## STATUS\nSUCCESS"}}

            task = Task("retry", prompt, "demo", repo, "main", "retry")
            env = {"EXEC_MODE": "dry-run", "MAX_ATTEMPTS": "2", "BACKOFF_SECONDS": "0"}
            with patch.dict(os.environ, env), \
                 patch.object(task_graph, "WORKTREES_DIR", root / "worktrees", create=True), \
                 patch.object(task_graph, "PROCESSED_DIR", root / "processed"), \
                 patch.object(task_graph, "FAILED_DIR", root / "failed"), \
                 patch.object(task_graph, "task_report_path", lambda p, t: root / "reports" / f"{t}.md"), \
                 patch.object(task_graph, "worktree_path", lambda p, t: root / "worktrees" / p / t), \
                 patch.object(task_graph, "run_opencode", flaky):
                task_graph.set_task_store(None)
                result = task_graph.build_task_graph().invoke(
                    {"task": task}, {"configurable": {"thread_id": task.id}}
                )
            self.assertEqual(2, calls["count"])
            self.assertEqual("success", result["status"])
            self.assertEqual("", result["error"])
            self.assertTrue((root / "processed" / prompt.name).exists())

    def test_changed_file_scope_is_a_hard_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            (repo / "outside.txt").write_text("x", encoding="utf-8")
            task = Task("scope", root / "x.md", "demo", repo, "main", "scope",
                        allowed_paths=["README.md"])
            result = task_graph._validate_code_change(
                task, repo, {"response": {"message": "## STATUS\nSUCCESS"}}
            )
            self.assertEqual("failed", result["status"])
            self.assertEqual("SCOPE_VIOLATION", result["failure_type"])


class AssetSafetyTests(unittest.TestCase):
    def test_architecture_staging_keeps_current_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            # Patch the resolver because canonical project assets normally live
            # under the runner workspace.
            root = Path(raw)
            with patch("architecture.repository.architecture_dir", lambda p: root / p):
                repo = ArchitectureRepository(ArchitectureConfig(root_dir=root))
                current = repo.snapshot_dir("p", "baseline")
                current.mkdir(parents=True)
                (current / "marker").write_text("old", encoding="utf-8")
                staging = repo.create_snapshot_dir("p", "baseline")
                self.assertTrue((current / "marker").exists())
                self.assertNotEqual(current, staging)

    def test_document_publish_failure_restores_processing_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            processing = root / "processing"
            processing.mkdir()
            source = processing / "doc.md"
            source.write_text("content", encoding="utf-8")
            import hashlib
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            target = root / "projects" / "p" / "documents" / "design" / "doc.md"
            plan = DocumentPlan("doc.md", source, digest, "p", "design", target,
                                ACTION_ARCHIVE, "test", 1.0)
            cfg = DocumentsConfig(processing_dir=processing,
                                  processed_dir=root / "processed",
                                  archive_root=root / "projects")
            with patch("document.executor._append_manifest",
                       side_effect=RuntimeError("manifest unavailable")):
                result = _execute_one(plan, cfg)
            self.assertEqual("failed", result.status)
            self.assertTrue(source.exists())
            self.assertFalse(target.exists())

    def test_document_journal_finishes_move_after_crash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            processing = root / "processing"
            processing.mkdir()
            source = processing / "doc.md"
            source.write_text("content", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            target = root / "projects" / "p" / "documents" / "design" / "doc.md"
            plan = DocumentPlan("doc/doc.md", source, digest, "p", "design", target,
                                ACTION_ARCHIVE, "test", 1.0)
            cfg = DocumentsConfig(processing_dir=processing,
                                  processed_dir=root / "processed",
                                  archive_root=root / "projects")
            journal = _write_journal(plan, cfg, "INTENT")
            data = json.loads(journal.read_text(encoding="utf-8"))
            data["final_path"] = str(target)
            journal.write_text(json.dumps(data), encoding="utf-8")
            target.parent.mkdir(parents=True)
            source.replace(target)  # injected crash point: after move, before manifest
            stats = reconcile_journals(cfg)
            self.assertEqual(1, stats["committed"])
            self.assertTrue(
                (root / "projects" / "p" / "documents" / "MANIFEST.json").exists()
            )
            self.assertEqual("COMMITTED", json.loads(journal.read_text())["status"])

    def test_human_review_decision_overrides_classifier(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            review = root / "review"
            review.mkdir()
            source = root / "doc.md"
            source.write_text("ambiguous", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            (review / "decisions.json").write_text(json.dumps({"decisions": {
                digest: {"project": "p", "category": "design", "reason": "confirmed"}
            }}), encoding="utf-8")
            cfg = Config(projects={"p": Project("p", root, "main")})
            docs = DocumentsConfig(review_dir=review)
            ex = ExtractedDoc(DocFile(source, source.name, ".md", source.stat().st_size), digest)
            result = classify_one(ex, cfg, docs)
            self.assertEqual(("p", "design", "human-review"),
                             (result.project, result.category, result.method))

    def test_document_dedup_discovers_ad_hoc_project_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            documents = root / "projects" / "repo-deadbeef" / "documents"
            documents.mkdir(parents=True)
            (documents / "MANIFEST.json").write_text(json.dumps({
                "documents": [{"sha256": "abc", "path": "design/spec.md"}]
            }), encoding="utf-8")
            cfg = DocumentsConfig(archive_root=root / "projects")
            self.assertEqual(
                ("repo-deadbeef", "design/spec.md"),
                load_hash_index(cfg, Config())["abc"],
            )

    def test_asset_migration_writes_versioned_rollback_record(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            reports = root / "reports"
            reports.mkdir()
            (reports / "t.md").write_text("report", encoding="utf-8")
            with patch.object(assets_module, "PROJECTS_ROOT", root / "projects"), \
                 patch.object(assets_module, "REPORTS_DIR", reports), \
                 patch.object(assets_module, "WORKTREES_DIR", root / "worktrees"), \
                 patch.object(assets_module, "ARCHITECTURE_ROOT", root / "architecture"):
                changes = assets_module.migrate_legacy_assets(
                    [{"project": "p", "task_id": "t"}], ["p"]
                )
                self.assertEqual(1, len(changes))
                report = json.loads(
                    (root / "projects" / "_runs" / "asset-migration-p12.json")
                    .read_text(encoding="utf-8")
                )
                self.assertEqual("p12-v1", report["version"])
                self.assertIn("rollback", report["batches"][0]["assets"][0])

    def test_asset_migration_moves_legacy_project_documents(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            legacy = root / "projects" / "p" / "doc"
            legacy.mkdir(parents=True)
            (legacy / "INDEX.md").write_text("legacy", encoding="utf-8")
            with patch.object(assets_module, "PROJECTS_ROOT", root / "projects"), \
                 patch.object(assets_module, "REPORTS_DIR", root / "reports"), \
                 patch.object(assets_module, "WORKTREES_DIR", root / "worktrees"), \
                 patch.object(assets_module, "ARCHITECTURE_ROOT", root / "architecture"):
                changes = assets_module.migrate_legacy_assets([], ["p"])
            self.assertTrue(changes)
            self.assertFalse(legacy.exists())
            self.assertEqual(
                "legacy",
                (root / "projects" / "p" / "documents" / "INDEX.md")
                .read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
