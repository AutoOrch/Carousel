"""Architecture analysis orchestrator (p10 §8).

Pipeline: git revision → JSON producer (OpenCode or deterministic dry-run)
→ archify validate → archify render → persist snapshot → register in SQLite.

Validation is a **self-repairing loop**: when archify validate fails, the
structured diagnostics are parsed and deterministic repairs applied (widen
a component for its label, drop an overlapping edge label, re-layout with
larger gaps, progressively simplify) — re-validating after each round.
Only when every repair round is exhausted does the failure propagate to
the LLM Diagnose → Re-plan → Retry loop.

The business repository is only ever *read*: analysis output goes to the
runner's architecture-data/ control plane, and the repo's git status is
checked afterwards — any modification is reported as an error.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from architecture import ir_tools
from architecture.archify_runner import ArchifyResult, ArchifyRunner
from architecture.repository import ArchitectureRepository
from config import Config
from log import get_logger
from runtime.task_store import TaskStore
from schemas.task import Task

logger = get_logger(__name__)

MAX_REPAIR_ROUNDS = 6

# Diagnostic message patterns (matched against archify's diagnostics[].message).
_LABEL_WIDER_RE = re.compile(
    r'Label "(.+?)" \(~(\d+)px\) is wider than component "(.+?)" \((\d+)px\)'
)
_SUBLABEL_NEEDS_RE = re.compile(
    r'Sublabel "(.+?)" needs ~(\d+)px.*?component "(.+?)" provides (\d+)px'
)
_LABEL_OVERLAP_RE = re.compile(r'Label "(.+?)" overlaps component')


class ArchitectureError(RuntimeError):
    """Raised when the architecture pipeline fails (feeds Diagnose→Replan)."""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise ArchitectureError(
            f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}"
        )
    return result.stdout.strip()


class ArchitectureManager:
    def __init__(self, config: Config, task_store: TaskStore | None = None) -> None:
        self._config = config
        self._store = task_store
        self._repo = ArchitectureRepository(config.architecture)
        self._runner = ArchifyRunner(config.architecture)

    @property
    def repository(self) -> ArchitectureRepository:
        return self._repo

    @property
    def runner(self) -> ArchifyRunner:
        return self._runner

    # ------------------------------------------------------------- pipeline

    def run_initialization(
        self,
        task: Task,
        json_producer: Callable[[Path, str], dict[str, Any]],
        mode: str,
        fence: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Run the full ARCHITECTURE_INIT pipeline for one task.

        ``json_producer(work_dir, revision)`` must write ``architecture.json``
        into *work_dir* and return ``{"analysis": {name: content}, "response": str}``.
        Raises ArchitectureError on any failure (task_graph routes that into
        the standard Diagnose → Re-plan → Retry loop).
        """
        project_path = task.project_path
        snapshot_id = self._config.architecture.default_snapshot

        # 1. revision + clean check (read-only requirement, p10 §26.1)
        revision = _git(project_path, "rev-parse", "HEAD")
        dirty = _git(project_path, "status", "--porcelain")
        if dirty:
            logger.warning(
                "[%s] repository has uncommitted changes — architecture "
                "analysis may not match a committed revision", task.id,
            )

        # 2. fresh snapshot dir (archives any previous baseline)
        work_dir = self._repo.create_snapshot_dir(task.project, snapshot_id)

        # 3. produce the JSON IR (LLM or deterministic dry-run)
        produced = json_producer(work_dir, revision)
        json_path = work_dir / "architecture.json"
        if not json_path.exists():
            raise ArchitectureError(
                "architecture.json was not produced "
                f"(producer returned: {str(produced)[:200]})"
            )

        # 4. structural validation with self-repair loop (archify validate)
        data = json.loads(json_path.read_text(encoding="utf-8-sig"))
        validation, repair_fixes = self._validate_with_repair(json_path, data)
        for fix in repair_fixes:
            logger.info("[%s] %s", task.id, fix)
        if not validation.ok:
            raise ArchitectureError(f"archify validate failed: {validation.output[:500]}")
        logger.info("[%s] validate OK: %s", task.id, validation.output[:120])

        # 5. render (archify render)
        html_path = work_dir / "architecture.html"
        render = self._runner.render(json_path, html_path, revision=revision[:12])
        if not render.ok or not html_path.exists():
            raise ArchitectureError(f"archify render failed: {render.output[:500]}")
        logger.info("[%s] render OK: %s", task.id, render.output[:120])

        # inspect (best-effort)
        inspect = self._runner.inspect(json_path)
        archify_version = self._runner.version()

        # 6. verify the business repo was not modified (p10 §26.1)
        dirty_after = _git(project_path, "status", "--porcelain")
        if dirty_after != dirty:
            raise ArchitectureError(
                "business repository was modified during architecture "
                "analysis — refusing to accept the snapshot"
            )

        # 7. persist snapshot + analysis docs + register in SQLite
        branch = _git(project_path, "rev-parse", "--abbrev-ref", "HEAD")
        if fence is not None and not fence():
            raise ArchitectureError("lease lost before architecture publish")
        snapshot = self._repo.write_snapshot(
            project_id=task.project,
            snapshot_id=snapshot_id,
            revision=revision,
            branch=branch,
            repository_path=str(project_path),
            source=mode,
            source_task_id=task.id,
            json_path=json_path,
            html_path=html_path,
            svg_path=None,
            archify_version=archify_version,
            analysis_files=produced.get("analysis") or {},
        )
        if self._store:
            self._store.upsert_architecture_snapshot(snapshot)
            for kind, path in (
                ("ARCHITECTURE_JSON", snapshot.json_path),
                ("ARCHITECTURE_HTML", snapshot.html_path),
                ("ARCHITECTURE_METADATA", snapshot.metadata_path),
            ):
                if path:
                    self._store.register_artifact(
                        kind, path, task_id=task.id,
                        run_id=task.run_id,
                    )

        logger.info(
            "[%s] architecture snapshot ready: %s/%s (rev %s)",
            task.id, task.project, snapshot_id, revision[:10],
        )
        return {
            "session_id": f"arch-{task.id}",
            "revision": revision,
            "snapshot_id": snapshot_id,
            "artifacts": {
                "json": snapshot.json_path,
                "html": snapshot.html_path,
                "metadata": snapshot.metadata_path,
            },
            "validation": validation.output[:500],
            "response": produced.get("response", ""),
        }

    # -------------------------------------------------- self-repair loop

    def _validate_with_repair(
        self, json_path: Path, data: dict[str, Any]
    ) -> tuple[ArchifyResult, list[str]]:
        """validate → parse diagnostics → deterministic repair → re-validate.

        Escalation ladder when targeted repairs don't apply (or geometric
        clean-flow errors persist):
          1. re-layout with wider gaps (420/170)
          2. drop connection labels + boundaries, re-layout (500/190)
          3. minimal IR — truncate everything, re-layout (520/190, no widen)

        Returns (final_result, applied_fixes).  Mutates and re-saves
        *json_path* as repairs are applied.
        """
        fixes: list[str] = []
        escalation = 0
        result = self._runner.validate(json_path)

        for round_ in range(1, MAX_REPAIR_ROUNDS + 1):
            if result.ok:
                return result, fixes

            messages = self._messages(result.output)
            targeted = self._apply_targeted(data, messages)
            fixes.extend(f"[repair r{round_}] {f}" for f in targeted)

            geometric = any(self._is_geometric(m) for m in messages)
            if geometric or not targeted:
                escalation += 1
                fixes.append(
                    f"[repair r{round_}] escalation stage {escalation}: "
                    + self._apply_escalation(data, escalation)
                )

            json_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            result = self._runner.validate(json_path)

        return result, fixes

    # ------------------------------------------------------------ internals

    @staticmethod
    def _messages(output: str) -> list[str]:
        """Extract diagnostic messages from a validate --json output."""
        try:
            parsed = json.loads(output)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            diags = [d for d in parsed.get("diagnostics") or [] if isinstance(d, dict)]
            msgs = [str(d.get("message", "")).strip() for d in diags]
            msgs = [m for m in msgs if m]
            if msgs:
                return msgs
            err = str(parsed.get("error", ""))
            if err:
                return [ln for ln in err.splitlines() if ln.strip().startswith("-")]
        return [output[:2000]]

    @staticmethod
    def _is_geometric(message: str) -> bool:
        return (
            "crosses component" in message
            or "does not honor" in message
            or "clean-flow" in message
            or "overlaps component" in message
        )

    def _apply_targeted(self, data: dict[str, Any], messages: list[str]) -> list[str]:
        """Deterministic fixes driven by parsed diagnostics."""
        fixes: list[str] = []
        comps = {
            c["id"]: c
            for c in data.get("components") or []
            if isinstance(c, dict) and c.get("id")
        }
        conns = [c for c in data.get("connections") or [] if isinstance(c, dict)]
        need_viewbox = False

        for msg in messages:
            m = _LABEL_WIDER_RE.search(msg)
            if m:
                label_text, need_px, comp_id = m.group(1), int(m.group(2)), m.group(3)
                comp = comps.get(comp_id)
                if comp:
                    need = need_px + 16
                    if need <= 300:
                        size = comp.get("size") or [ir_tools.COMP_W, ir_tools.COMP_H]
                        comp["size"] = [need, size[1]]
                        fixes.append(f"widen '{comp_id}' to {need}px for its label")
                    else:
                        comp["label"] = ir_tools.truncate_px(label_text, 300 - 16)
                        comp["size"] = [300, ir_tools.COMP_H]
                        fixes.append(f"truncate label of '{comp_id}' (too wide to fit)")

            m = _SUBLABEL_NEEDS_RE.search(msg)
            if m:
                sub_text, comp_id = m.group(1), m.group(3)
                comp = comps.get(comp_id)
                if comp:
                    w = (comp.get("size") or [ir_tools.COMP_W])[0]
                    comp["sublabel"] = ir_tools.truncate_px(sub_text, w - 16)
                    fixes.append(f"truncate sublabel of '{comp_id}'")

            m = _LABEL_OVERLAP_RE.search(msg)
            if m:
                label_text = m.group(1)
                for conn in conns:
                    if conn.get("label") == label_text:
                        # Truncation was already tried during layout; if the
                        # label still overlaps (geometry shifted after other
                        # repairs) drop it — edge semantics live in from/to.
                        del conn["label"]
                        fixes.append(f"drop overlapping edge label '{label_text}'")
                        break

            if "falls outside the viewBox" in msg:
                need_viewbox = True

            if "additionalProperties" in msg or "schema validation failed" in msg:
                _, norm_fixes = ir_tools.normalize_ir(data)
                fixes.extend(norm_fixes)

        if need_viewbox:
            ir_tools.recompute_view_box(data)
            fixes.append("viewBox recomputed to cover all components")

        return fixes

    def _apply_escalation(self, data: dict[str, Any], stage: int) -> str:
        """Progressive simplification for geometric failures."""
        conns = [c for c in data.get("connections") or [] if isinstance(c, dict)]
        comps = [c for c in data.get("components") or [] if isinstance(c, dict)]

        if stage <= 1:
            ir_tools.auto_layout(data, x_gap=420, y_gap=170)
            return "re-layout with x_gap=420, y_gap=170"
        if stage == 2:
            for conn in conns:
                conn.pop("label", None)
            data["boundaries"] = []
            ir_tools.auto_layout(data, x_gap=500, y_gap=190)
            return "dropped edge labels + boundaries; re-layout x_gap=500"
        # stage >= 3: minimal IR
        for comp in comps:
            comp["label"] = ir_tools.truncate_px(
                comp.get("label", ""), ir_tools.COMP_W - 16
            )
            comp.pop("sublabel", None)
        for conn in conns:
            conn.pop("label", None)
        data["boundaries"] = []
        ir_tools.auto_layout(
            data, x_gap=520, y_gap=190, max_comp_w=ir_tools.COMP_W
        )
        return "minimal IR: labels truncated, decorations dropped"
