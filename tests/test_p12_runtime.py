from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import json
import hashlib
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch, MagicMock

import planner
import runtime.task_store as task_store_module
import task_graph
from architecture.repository import ArchitectureRepository
from config import ArchitectureConfig, Config, DocumentsConfig, Project, _validate_raw_config
from document.classifier import classify_one
from document.executor import _execute_one, _write_journal, reconcile_journals
from document.planner import load_hash_index
from document.schemas import ACTION_ARCHIVE, DocFile, DocumentPlan, ExtractedDoc
from opencode_client import OpenCodeClient
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


class DirtyBasePolicyTests(unittest.TestCase):
    """Dirty base-repo handling must never crash the worker graph."""

    def _invoke_graph(self, root: Path, task: Task, run_opencode=None, extra=None):
        env = {"EXEC_MODE": "dry-run", "MAX_ATTEMPTS": "2", "BACKOFF_SECONDS": "0"}
        patches = [
            patch.dict(os.environ, env),
            patch.object(task_graph, "PROCESSED_DIR", root / "processed"),
            patch.object(task_graph, "FAILED_DIR", root / "failed"),
            patch.object(task_graph, "task_report_path", lambda p, t: root / "reports" / f"{t}.md"),
            patch.object(task_graph, "worktree_path", lambda p, t: root / "worktrees" / p / t),
        ]
        if run_opencode is not None:
            patches.append(patch.object(task_graph, "run_opencode", run_opencode))
        if extra:
            patches.extend(extra)
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            task_graph.set_task_store(None)
            return task_graph.build_task_graph().invoke(
                {"task": task}, {"configurable": {"thread_id": task.id}}
            )

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=str(repo), check=True,
                              capture_output=True, text=True).stdout

    def _dirty_repo_task(self, root: Path, name: str, **task_kwargs) -> tuple:
        repo = init_repo(root)
        (repo / "notes.md").write_text("work in progress", encoding="utf-8")
        processing = root / "processing"
        processing.mkdir()
        prompt = processing / f"{name}.md"
        prompt.write_text(f"# {name}", encoding="utf-8")
        task = Task(name, prompt, "demo", repo, "main", name, **task_kwargs)
        return task, prompt, repo

    def test_dirty_base_refuse_fails_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            task, prompt, repo = self._dirty_repo_task(root, "dirty")
            result = self._invoke_graph(root, task)
            self.assertIn("uncommitted", result["error"])
            self.assertEqual("BASE_REPOSITORY_DIRTY", result["failure_type"])
            self.assertTrue((root / "failed" / prompt.name).exists())
            report = (root / "reports" / "dirty.md").read_text(encoding="utf-8")
            self.assertIn("BASE_REPOSITORY_DIRTY", report)
            # user's uncommitted change untouched, nothing leaked
            self.assertEqual("work in progress",
                             (repo / "notes.md").read_text(encoding="utf-8"))
            self.assertFalse((root / "worktrees").exists())

    def test_dirty_base_allow_proceeds(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            task, prompt, repo = self._dirty_repo_task(
                root, "allowed", dirty_base_policy="allow")

            def fake(task, worktree, **kwargs):
                (Path(worktree) / "feature.txt").write_text("done", encoding="utf-8")
                return {"response": {"message": "## STATUS\nSUCCESS"}}

            result = self._invoke_graph(root, task, run_opencode=fake)
            self.assertEqual("", result["error"])
            self.assertTrue((root / "processed" / prompt.name).exists())
            # dirty change survives the whole run, merge still landed
            self.assertEqual("work in progress",
                             (repo / "notes.md").read_text(encoding="utf-8"))
            self.assertEqual("done", (repo / "feature.txt").read_text(encoding="utf-8"))

    def test_dirty_base_stash_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            task, prompt, repo = self._dirty_repo_task(
                root, "stashed", dirty_base_policy="stash")
            seen = {}

            def fake(task, worktree, **kwargs):
                seen["base_status"] = subprocess.run(
                    ["git", "status", "--porcelain"], cwd=str(task.project_path),
                    capture_output=True, text=True,
                ).stdout
                (Path(worktree) / "feature.txt").write_text("done", encoding="utf-8")
                return {"response": {"message": "## STATUS\nSUCCESS"}}

            result = self._invoke_graph(root, task, run_opencode=fake)
            self.assertEqual("", result["error"])
            # base repo was clean while the agent ran
            self.assertEqual("", seen["base_status"].strip())
            # ...and the user's change came back afterwards, stash drained
            self.assertEqual("work in progress",
                             (repo / "notes.md").read_text(encoding="utf-8"))
            self.assertEqual("", self._git(repo, "stash", "list").strip())
            self.assertEqual("done", (repo / "feature.txt").read_text(encoding="utf-8"))

    def test_merge_checkout_failure_is_graceful(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = init_repo(root)
            subprocess.run(["git", "checkout", "-b", "other"], cwd=repo,
                           check=True, capture_output=True)
            processing = root / "processing"
            processing.mkdir()
            prompt = processing / "mergeme.md"
            prompt.write_text("# merge", encoding="utf-8")

            def fake(task, worktree, **kwargs):
                (Path(worktree) / "feature.txt").write_text("done", encoding="utf-8")
                return {"response": {"message": "## STATUS\nSUCCESS"}}

            real_git = task_graph.run_git

            def guarded(repo_arg, *args):
                if args and args[0] == "checkout":
                    raise RuntimeError("simulated checkout refusal")
                return real_git(repo_arg, *args)

            task = Task("mergeme", prompt, "demo", repo, "main", "mergeme")
            result = self._invoke_graph(
                root, task, run_opencode=fake,
                extra=[patch.object(task_graph, "run_git", guarded)],
            )
            self.assertEqual("error", result["merge_status"])
            self.assertIn("checkout", result["error"])
            self.assertTrue((root / "failed" / prompt.name).exists())

    def test_config_rejects_unknown_dirty_policy(self) -> None:
        with self.assertRaisesRegex(ValueError, "dirty_base_policy"):
            _validate_raw_config({"worker": {"dirty_base_policy": "explode"}})
        with self.assertRaisesRegex(ValueError, "dirty_base_policy"):
            _validate_raw_config({"projects": {"p": {"dirty_base_policy": "maybe"}}})


class OpenCodeAsyncClientTests(unittest.TestCase):
    """send_message must survive arbitrarily long agent runs."""

    @staticmethod
    def _response(status_code: int = 200, json_data=None) -> MagicMock:
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = json_data
        return response

    def _client(self) -> OpenCodeClient:
        client = OpenCodeClient(base_url="http://test", timeout=30, poll_interval=0)
        client.POLL_HTTP_TIMEOUT = 0.1
        return client

    def test_async_send_polls_until_completed(self) -> None:
        client = self._client()
        streaming = {"info": {"role": "assistant", "time": {"completed": ""}},
                     "parts": [{"type": "text", "text": "partial"}]}
        done = {"info": {"role": "assistant", "time": {"completed": 12345}},
                "parts": [{"type": "text", "text": "## STATUS\nSUCCESS"}]}
        polls = {"count": 0}

        def fake_post(url, **kwargs):
            self.assertIn("/prompt_async", url)
            return self._response(204)

        def fake_list(session_id):
            polls["count"] += 1
            return [{"info": {"role": "user"}, "parts": []},
                    streaming if polls["count"] < 3 else done]

        with patch.object(OpenCodeClient, "_send_async", return_value=True), \
             patch.object(OpenCodeClient, "list_messages", side_effect=fake_list):
            reply = client.send_message("ses_x", "do the thing")
        self.assertEqual(done, reply)
        self.assertGreaterEqual(polls["count"], 3)

    def test_timeout_aborts_session(self) -> None:
        client = self._client()
        streaming = {"info": {"role": "assistant", "time": {"completed": ""}},
                     "parts": []}
        aborted = {"called": False}

        def fake_abort(session_id):
            aborted["called"] = True

        with patch.object(OpenCodeClient, "_send_async", return_value=True), \
             patch.object(OpenCodeClient, "list_messages",
                          return_value=[{"info": {"role": "user"}, "parts": []},
                                        streaming]), \
             patch.object(OpenCodeClient, "abort_session", side_effect=fake_abort), \
             patch("opencode_client.time.sleep"):
            with self.assertRaisesRegex(TimeoutError, "did not finish"):
                client.send_message("ses_x", "too slow")
        self.assertTrue(aborted["called"])

    def test_transient_poll_errors_are_tolerated(self) -> None:
        client = self._client()
        done = {"info": {"role": "assistant", "time": {"completed": 1}},
                "parts": [{"type": "text", "text": "ok"}]}
        attempts = {"count": 0}

        def flaky_list(session_id):
            attempts["count"] += 1
            if attempts["count"] % 2 == 1:
                import requests
                raise requests.ConnectionError("poll hiccup")
            return [{"info": {"role": "user"}}, done]

        with patch.object(OpenCodeClient, "_send_async", return_value=True), \
             patch.object(OpenCodeClient, "list_messages", side_effect=flaky_list), \
             patch("opencode_client.time.sleep"):
            reply = client.send_message("ses_x", "flaky")
        self.assertEqual(done, reply)

    def test_persistent_poll_errors_raise(self) -> None:
        import requests
        client = self._client()

        def always_fails(session_id):
            raise requests.ConnectionError("server gone")

        with patch.object(OpenCodeClient, "_send_async", return_value=True), \
             patch.object(OpenCodeClient, "list_messages", side_effect=always_fails), \
             patch("opencode_client.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "polling failed"):
                client.send_message("ses_x", "nope")

    def test_falls_back_to_blocking_when_async_unavailable(self) -> None:
        client = self._client()
        blocked = {"info": {"role": "assistant", "time": {"completed": 1}},
                   "parts": [{"type": "text", "text": "ok"}]}

        def fake_post(url, **kwargs):
            self.assertIn("/message", url)
            self.assertNotIn("prompt_async", url)
            return self._response(200, blocked)

        with patch.object(OpenCodeClient, "_send_async", return_value=False), \
             patch("opencode_client.requests.post", side_effect=fake_post):
            reply = client.send_message("ses_x", "legacy server")
        self.assertEqual(blocked, reply)

    def test_progress_callback_receives_elapsed_and_message(self) -> None:
        client = OpenCodeClient(base_url="http://test", timeout=100, poll_interval=0)
        streaming = {"info": {"role": "assistant", "time": {"completed": ""}},
                     "parts": [{"type": "text", "text": "partial"}]}
        progress_calls: list = []

        def on_progress(elapsed, message):
            progress_calls.append((elapsed, message))

        # Fake clock: monotonic() returns 0 (start), then 40, 80, 120 per
        # loop iteration → progress fires at 80s, timeout at 120s.
        ticks = iter([0.0, 40.0, 80.0, 120.0, 160.0])
        with patch.object(OpenCodeClient, "_send_async", return_value=True), \
             patch.object(OpenCodeClient, "list_messages",
                          return_value=[{"info": {"role": "user"}}, streaming]), \
             patch("opencode_client.time.sleep"), \
             patch("opencode_client.time.monotonic", side_effect=lambda: next(ticks)):
            with self.assertRaises(TimeoutError):
                client.send_message("ses_x", "slow", on_progress=on_progress)
        self.assertEqual(1, len(progress_calls))
        elapsed, message = progress_calls[0]
        self.assertEqual(80, elapsed)
        self.assertEqual("partial", message["parts"][0]["text"])

    def test_config_rejects_bad_opencode_timeout(self) -> None:
        with self.assertRaisesRegex(ValueError, "opencode.timeout"):
            _validate_raw_config({"opencode": {"timeout": "forever"}})


class ResumeAfterFailureTests(unittest.TestCase):
    """A terminal failure preserves the work; a requeue resumes it."""

    def _invoke(self, root: Path, task: Task, run_opencode=None, max_attempts="1"):
        env = {"EXEC_MODE": "dry-run", "MAX_ATTEMPTS": max_attempts,
               "BACKOFF_SECONDS": "0"}
        patches = [
            patch.dict(os.environ, env),
            patch.object(task_graph, "PROCESSED_DIR", root / "processed"),
            patch.object(task_graph, "FAILED_DIR", root / "failed"),
            patch.object(task_graph, "task_report_path", lambda p, t: root / "reports" / f"{t}.md"),
            patch.object(task_graph, "worktree_path", lambda p, t: root / "worktrees" / p / t),
        ]
        if run_opencode is not None:
            patches.append(patch.object(task_graph, "run_opencode", run_opencode))
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            task_graph.set_task_store(None)
            return task_graph.build_task_graph().invoke(
                {"task": task}, {"configurable": {"thread_id": task.id}}
            )

    @staticmethod
    def _task(root: Path, name: str, **kwargs) -> Task:
        prompt = root / "processing" / f"{name}.md"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text(f"# {name}", encoding="utf-8")
        return Task(name, prompt, "demo", root / "repo", "main", name, **kwargs)

    def test_failure_preserves_wip_and_requeue_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            init_repo(root)
            task = self._task(root, "resumable")

            def failing(task, worktree, **kwargs):
                (Path(worktree) / "wip.txt").write_text("half done", encoding="utf-8")
                raise RuntimeError("agent crashed mid-run")

            self._invoke(root, task, run_opencode=failing)

            # Terminal failure: file in failed/, worktree dir gone, branch kept.
            self.assertTrue((root / "failed" / "resumable.md").exists())
            worktree_dir = root / "worktrees" / "demo" / "resumable"
            self.assertFalse(worktree_dir.exists())
            repo = root / "repo"
            branches = subprocess.run(["git", "branch", "--list", "agent/resumable"],
                                      cwd=repo, capture_output=True, text=True).stdout
            self.assertIn("agent/resumable", branches)
            # WIP survived on the branch
            blob = subprocess.run(
                ["git", "show", "agent/resumable:wip.txt"],
                cwd=repo, capture_output=True, text=True).stdout
            self.assertEqual("half done", blob)

            # Requeue: same task file, agent succeeds this time.
            requeued = self._task(root, "resumable")
            captured = {}

            def succeeding(task, worktree, **kwargs):
                captured["replan_context"] = kwargs.get("replan_context", "")
                self.assertTrue((Path(worktree) / "wip.txt").exists())  # resumed!
                (Path(worktree) / "feature.txt").write_text("done", encoding="utf-8")
                return {"response": {"message": "## STATUS\nSUCCESS"}}

            result = self._invoke(root, requeued, run_opencode=succeeding,
                                  max_attempts="2")
            self.assertEqual("", result["error"])
            # Prior work + new work merged into main
            self.assertEqual("half done", (repo / "wip.txt").read_text(encoding="utf-8"))
            self.assertEqual("done", (repo / "feature.txt").read_text(encoding="utf-8"))
            self.assertTrue((root / "processed" / "resumable.md").exists())
            # Branch merged and cleaned up after success
            branches = subprocess.run(["git", "branch", "--list", "agent/resumable"],
                                      cwd=repo, capture_output=True, text=True).stdout
            self.assertEqual("", branches.strip())

    def test_resume_false_starts_clean(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            init_repo(root)
            task = self._task(root, "freshstart")

            def failing(task, worktree, **kwargs):
                (Path(worktree) / "wip.txt").write_text("half done", encoding="utf-8")
                raise RuntimeError("boom")

            self._invoke(root, task, run_opencode=failing)
            repo = root / "repo"
            self.assertIn("agent/freshstart", subprocess.run(
                ["git", "branch", "--list", "agent/freshstart"],
                cwd=repo, capture_output=True, text=True).stdout)

            requeued = self._task(root, "freshstart", resume=False)

            def succeeding(task, worktree, **kwargs):
                self.assertFalse((Path(worktree) / "wip.txt").exists())  # clean slate
                (Path(worktree) / "feature.txt").write_text("done", encoding="utf-8")
                return {"response": {"message": "## STATUS\nSUCCESS"}}

            result = self._invoke(root, requeued, run_opencode=succeeding,
                                  max_attempts="2")
            self.assertEqual("", result["error"])
            self.assertFalse((repo / "wip.txt").exists())
            self.assertEqual("done", (repo / "feature.txt").read_text(encoding="utf-8"))

    def test_pool_injects_prior_failure_context(self) -> None:
        from runtime.worker_pool import _prior_failure_context
        self.assertEqual("", _prior_failure_context(None, None))
        self.assertEqual("", _prior_failure_context({"failure_type": ""}, None))
        context = _prior_failure_context(
            {"failure_type": "EXECUTION_ERROR", "failure_message": "read timeout",
             "attempt": 3}, None
        )
        self.assertIn("PREVIOUS_RUN_FAILURE", context)
        self.assertIn("EXECUTION_ERROR", context)
        self.assertIn("read timeout", context)
        self.assertIn("3 attempt(s)", context)
        self.assertIn("Do not redo work", context)


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
