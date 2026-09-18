"""Final requirement review (p9: Requirement Closure).

Runs after every task of a requirement run has COMPLETED.  Collects the
original requirement, the plan, per-task reports, the real git diff and
(final) test results, asks the reviewer (OpenCode or a dry-run simulation)
whether the *requirement* — not just the tasks — is fulfilled, and writes
structured reports.  When the verdict is PARTIAL/FAILED it generates
follow-up tasks back into prompts/ (bounded by max_rounds).
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from config import Config, REPORTS_DIR, load_config
from log import get_logger
from opencode_client import OpenCodeClient
from requirement_run import load_plan, load_requirement
from runtime.task_store import TaskStore
from runtime.assets import run_reports_dir, task_report_path
from schemas.requirement_review import (
    FollowupTask,
    RequirementReview,
    RequirementResult,
    RequirementRisk,
)

logger = get_logger(__name__)

MAX_DIFF_LINES = 3000
MAX_REPORT_CHARS = 4000

REVIEWER_PROMPT_TEMPLATE = """你是最终需求验收审查 Agent。

你的任务不是重新实现代码，而是判断：
当前项目的最终代码，是否真正完成了原始需求。

请严格基于以下证据分析：

1. 原始需求文件
2. Planner 生成的任务清单
3. 每个任务的执行报告
4. Git 实际变更
5. 最终代码
6. 最终测试结果

禁止仅根据任务状态 COMPLETED 判断需求完成。

请逐项检查：

一、原始需求拆解
- 将原始需求拆分成可验证的需求项
- 为每个需求项分配唯一 requirement_id

二、任务覆盖
- 每个需求项由哪些任务负责
- 是否存在没有任务覆盖的需求

三、代码证据
- 每个需求项对应哪些实际修改文件
- 是否能从代码中找到实现证据
- Agent 声称修改的文件是否与 Git 实际变更一致

四、集成分析
- 多个任务合并后是否存在接口、字段、状态、调用链不一致
- 是否存在前后端、数据库、服务层之间的契约问题

五、回归风险
- 是否破坏旧逻辑
- 是否遗漏兼容路径
- 是否存在未测试分支
- 是否存在无关修改

六、最终结论
只能返回以下结果之一：

COMPLETE
PARTIAL
FAILED
BLOCKED
RISK_ACCEPTED

如果结果不是 COMPLETE 或 RISK_ACCEPTED，
必须生成补充任务规划（followup_tasks 不能为空）。

补充任务必须包含：

- id（followup-NNN-简短描述 格式）
- title
- project（使用下方项目列表中的 ref）
- prompt（详细任务描述）
- depends_on
- allowed_paths
- reason
- requirement_ids
- acceptance_criteria
- validation_command
- priority（P0/P1/P2）

只输出一个 JSON 对象（可包裹在 ```json 代码块中），结构如下：

{{
  "status": "COMPLETE|PARTIAL|FAILED|BLOCKED|RISK_ACCEPTED",
  "summary": "总体结论",
  "risk_level": "low|medium|high",
  "coverage": {{"total": N, "completed": N, "partial": N, "missing": N, "score": 0.0}},
  "requirements": [
    {{
      "requirement_id": "R-001",
      "description": "需求描述",
      "status": "PASS|PARTIAL|NOT_VERIFIED|FAIL",
      "evidence_grade": "A|B|C|D|E",
      "task_ids": ["task-001-xxx"],
      "changed_files": ["path/to/file.go"],
      "evidence": ["代码证据"],
      "missing_evidence": ["缺失证据"],
      "tests": ["TestXxx"]
    }}
  ],
  "risks": [
    {{
      "risk_id": "RISK-001",
      "type": "REGRESSION_RISK",
      "severity": "high|medium|low",
      "description": "风险描述",
      "evidence": ["涉及文件"],
      "recommended_action": "建议动作"
    }}
  ],
  "followup_tasks": [
    {{
      "id": "followup-001-简短描述",
      "title": "补充任务标题",
      "project": "项目 ref",
      "prompt": "任务正文（含验收标准）",
      "depends_on": [],
      "allowed_paths": ["path/to/file"],
      "reason": "产生原因",
      "requirement_ids": ["R-002"],
      "acceptance_criteria": ["验收标准"],
      "validation_command": "go test ./...",
      "priority": "P0"
    }}
  ]
}}

==================== 证据材料 ====================

【可用项目列表】
{projects}

【原始需求】
{requirement}

【Planner 任务清单】
{task_list}

【任务执行报告】
{reports}

【Git 实际变更】
{git_diff}

【Agent 声称 vs Git 实际修改对比】
{claimed_vs_actual}

【最终测试结果】
{test_results}
"""


def _run_git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return f"(git {' '.join(args)} failed: {result.stderr.strip()[:200]})"
    except Exception as exc:
        return f"(git error: {exc})"


def _extract_changed_files_from_report(text: str) -> list[str]:
    """Pull the file list under '## CHANGED_FILES' headings in a report."""
    files: list[str] = []
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = "CHANGED_FILES" in stripped.upper()
            continue
        if not in_section or not stripped:
            continue
        if stripped.startswith(("#", "-", "*", "|")):
            stripped = stripped.lstrip("-*| ").strip()
        if not stripped or stripped.lower().startswith("修改的文件"):
            continue
        # Heuristic: a path-ish token
        if re.search(r"\.[a-zA-Z0-9]{1,8}$", stripped) or "/" in stripped:
            files.append(stripped.split()[0])
    return files


class RequirementClosure:
    def __init__(self, config: Config, task_store: TaskStore) -> None:
        self._config = config
        self._store = task_store

    # ------------------------------------------------------------- triggers

    def runs_needing_review(self) -> list[dict]:
        return self._store.runs_needing_review()

    def review_run(self, run_id: str, mode: str, simulate: str = "") -> dict[str, Any]:
        """Execute one final-review round.  Returns a summary dict."""
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run: {run_id}")

        round_ = self._store.get_review_count(run_id) + 1
        self._store.update_run(run_id, status="FINAL_REVIEWING")

        context = self._collect_context(run_id, mode)
        try:
            review = self._perform_review(run_id, context, mode, simulate)
        except Exception:
            # Review could not complete (e.g. OpenCode server unreachable).
            # Revert to EXECUTING so the run is picked up again on a later
            # trigger instead of being stuck in FINAL_REVIEWING forever.
            self._store.update_run(run_id, status="EXECUTING")
            raise
        hard_failures = context.get("hard_gate_failures") or []
        if hard_failures:
            review.status = "FAILED"
            review.summary = (
                "Deterministic gates failed:\n- " + "\n- ".join(hard_failures)
                + "\n\n" + (review.summary or "")
            )
        review.base_revision = context["base_revision"]
        review.final_revision = context["final_revision"]

        report_path, json_path = self._write_reports(run_id, round_, review)
        self._store.record_review(
            review_id=f"{run_id}-r{round_}",
            run_id=run_id,
            round_=round_,
            status=review.status,
            report_path=str(report_path),
            json_path=str(json_path),
            coverage_score=review.coverage_score,
            risk_level=review.risk_level,
            followup_generated=bool(review.followup_tasks),
            base_revision=review.base_revision,
            final_revision=review.final_revision,
        )
        review_id = f"{run_id}-r{round_}"
        self._store.record_review_items(review_id, review.requirements, review.risks)
        self._store.register_artifact(
            "FINAL_REVIEW_MARKDOWN", str(report_path), run_id=run_id
        )
        self._store.register_artifact(
            "FINAL_REVIEW_JSON", str(json_path), run_id=run_id
        )

        self._store.update_run(
            run_id,
            review_status=review.status,
            final_revision=review.final_revision,
            coverage_score=review.coverage_score,
            risk_level=review.risk_level,
            round=round_,
        )

        logger.info(
            f"[closure] {run_id} round {round_}: {review.status} "
            f"(coverage={review.coverage_score:.2f}, risks={len(review.risks)}, "
            f"followups={len(review.followup_tasks)})"
        )

        followup_files: list[Path] = []
        if self._should_replan(review, round_):
            followup_files = self._write_followup_tasks(run_id, context, review)
            if followup_files:
                self._store.update_run(run_id, status="EXECUTING", round=round_ + 1)
                logger.info(
                    f"[closure] {run_id}: {len(followup_files)} follow-up task(s) "
                    f"queued (round {round_ + 1}/{self._config.final_review.max_rounds})"
                )
            else:
                self._store.update_run(run_id, status="NEEDS_ACTION")
                logger.warning(
                    f"[closure] {run_id}: {review.status} but no usable follow-up tasks; "
                    "run needs manual attention"
                )
        else:
            final_status = (
                "COMPLETED" if review.status == "COMPLETE"
                else "NEEDS_ACTION" if review.status == "RISK_ACCEPTED"
                else "FAILED" if review.status == "FAILED"
                else "NEEDS_ACTION"
            )
            self._store.update_run(run_id, status=final_status)
            logger.info(f"[closure] {run_id} closed as {final_status}")

        return {
            "run_id": run_id,
            "round": round_,
            "status": review.status,
            "coverage_score": review.coverage_score,
            "followup_files": [str(p.name) for p in followup_files],
            "report": str(report_path),
        }

    # ------------------------------------------------------------ collection

    def _collect_context(self, run_id: str, mode: str = "opencode") -> dict[str, Any]:
        run = self._store.get_run(run_id)
        plan = load_plan(run_id) or {}
        requirement = load_requirement(run_id)
        if requirement is None and run:
            req_path = Path(run["requirement_file"])
            if req_path.exists():
                requirement = req_path.read_text(encoding="utf-8", errors="replace")
        requirement = requirement or "(original requirement not found)"

        tasks = self._store.get_run_tasks(run_id)

        # Reports per task (truncated).
        reports: list[dict[str, Any]] = []
        claimed: dict[str, list[str]] = {}
        for t in tasks:
            entry: dict[str, Any] = {
                "task_id": t["task_id"],
                "status": t["status"],
                "attempt": t["attempt"],
                "commit": (t.get("commit_sha") or "")[:12],
            }
            report_path = task_report_path(t["project"], t["task_id"])
            if not report_path.exists():
                report_path = REPORTS_DIR / f"{t['task_id']}.md"  # legacy fallback
            if report_path.exists():
                text = report_path.read_text(encoding="utf-8-sig", errors="replace")
                claimed[t["task_id"]] = _extract_changed_files_from_report(text)
                entry["report"] = text[:MAX_REPORT_CHARS]
                if len(text) > MAX_REPORT_CHARS:
                    entry["report"] += "\n…(truncated)"
            else:
                entry["report"] = "(no report)"
            reports.append(entry)

        # Git evidence per project.
        projects = plan.get("projects") or []
        git_sections: list[str] = []
        actual_changed: set[str] = set()
        base_revision = ""
        final_revision = ""
        for p in projects:
            repo = Path(p["path"])
            base = p.get("base_revision") or ""
            if not base:
                base = _run_git(repo, "rev-parse", "HEAD")
            final = _run_git(repo, "rev-parse", "HEAD")
            self._store.update_run_project_final(run_id, p["id"], final)
            if not base_revision:
                base_revision, final_revision = base, final
            log = _run_git(repo, "log", "--oneline", f"{base}..HEAD") if base else "(no base)"
            stat = _run_git(repo, "diff", "--stat", f"{base}...HEAD") if base else "(no base)"
            files = _run_git(repo, "diff", "--name-status", f"{base}...HEAD") if base else ""
            diff = _run_git(repo, "diff", f"{base}...HEAD") if base else ""
            diff_lines = diff.splitlines()
            if len(diff_lines) > MAX_DIFF_LINES:
                diff = "\n".join(diff_lines[:MAX_DIFF_LINES]) + "\n…(truncated)"
            git_sections.append(
                f"### 项目 {p['id']} ({repo.as_posix()})\n"
                f"base: {base[:12]}  final: {final[:12]}  branch: {p.get('branch', '?')}\n\n"
                f"commits:\n{log}\n\n"
                f"diff --stat:\n{stat}\n\n"
                f"changed files (name-status):\n{files or '(none)'}\n\n"
                f"diff:\n```diff\n{diff or '(none)'}\n```"
            )
            for line in files.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    actual_changed.add(parts[-1])

        # Deterministic claimed-vs-actual check.
        mismatch_lines: list[str] = []
        for task_id, files in claimed.items():
            missing = [f for f in files if f not in actual_changed]
            if missing:
                mismatch_lines.append(
                    f"- {task_id}: claimed but not in git diff: {', '.join(missing)}"
                )
        claimed_vs_actual = "\n".join(mismatch_lines) or "(all claimed files match git)"

        # Final tests: only meaningful for real (opencode) runs; config-
        # registered projects only — ad-hoc path projects have no test command.
        test_sections: list[str] = []
        hard_gate_failures: list[str] = [
            f"task {t['task_id']} ended as {t['status']}"
            for t in tasks if t["status"] != "COMPLETED"
        ]
        for p in projects:
            project = self._config.projects.get(p["id"]) or next(
                (x for x in self._config.projects.values()
                 if x.path.as_posix() == Path(p["path"]).as_posix()),
                None,
            )
            if not project or not project.test_command:
                test_sections.append(f"### {p['id']}: (no test command configured)")
                continue
            if mode != "opencode" or not self._config.final_review.require_tests:
                test_sections.append(f"### {p['id']}: skipped (mode={mode})")
                continue
            try:
                result = subprocess.run(
                    project.test_command,
                    shell=True,
                    cwd=str(project.path),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=1800,
                )
            except subprocess.TimeoutExpired:
                test_sections.append(
                    f"### {p['id']}: `{project.test_command}` → TIMEOUT after 1800s"
                )
                hard_gate_failures.append(f"{p['id']}: final test timed out")
                continue
            output = (result.stdout + "\n" + result.stderr).strip()[:4000]
            test_sections.append(
                f"### {p['id']}: `{project.test_command}` → "
                f"exit={result.returncode}\n```\n{output}\n```"
            )
            if result.returncode != 0:
                hard_gate_failures.append(
                    f"{p['id']}: final test failed with exit {result.returncode}"
                )

        return {
            "run": run or {},
            "plan": plan,
            "requirement": requirement,
            "tasks": tasks,
            "reports": reports,
            "projects": projects,
            "git_sections": git_sections,
            "claimed_vs_actual": claimed_vs_actual,
            "test_sections": test_sections,
            "hard_gate_failures": hard_gate_failures,
            "base_revision": base_revision,
            "final_revision": final_revision,
        }

    # --------------------------------------------------------------- review

    def _perform_review(
        self, run_id: str, context: dict[str, Any], mode: str, simulate: str
    ) -> RequirementReview:
        if mode == "dry-run":
            return self._simulate_review(run_id, context, simulate)

        prompt = self._build_prompt(context)
        client = OpenCodeClient(base_url=self._config.opencode_url)
        fr = self._config.final_review
        session = client.create_session(title=f"final-review-{run_id}")
        session_id = session["id"]
        try:
            response = client.send_message(
                session_id,
                prompt,
                model=fr.reviewer_model or None,
                agent=fr.reviewer_agent or None,
            )
        finally:
            client.delete_session(session_id)

        text = _response_text(response)
        review = self._parse_review(run_id, text)
        review.raw_response = text
        return review

    def _simulate_review(
        self, run_id: str, context: dict[str, Any], simulate: str
    ) -> RequirementReview:
        """Deterministic review for dry-run mode.

        ``simulate="partial"`` makes round 1 return PARTIAL with one follow-up
        task so the closure loop can be exercised end-to-end.
        """
        round_ = self._store.get_review_count(run_id) + 1
        task_ids = [t["task_id"] for t in context["tasks"]]
        changed = sorted({
            f
            for section in context["git_sections"]
            for f in re.findall(r"^[AM]\t(\S+)", section, re.MULTILINE)
        }) or ["README.md"]

        if simulate == "partial" and round_ == 1:
            review = RequirementReview(
                run_id=run_id,
                status="PARTIAL",
                summary="[dry-run] 模拟：需求部分完成，需要补充验证任务",
                requirements=[
                    RequirementResult(
                        requirement_id="R-001",
                        description="dry-run 模拟需求项",
                        status="PASS",
                        evidence_grade="A",
                        task_ids=task_ids,
                        changed_files=changed,
                        evidence=["dry-run change appended"],
                    ),
                    RequirementResult(
                        requirement_id="R-002",
                        description="dry-run 模拟遗漏项",
                        status="NOT_VERIFIED",
                        evidence_grade="D",
                        task_ids=[],
                        missing_evidence=["缺少回归验证"],
                    ),
                ],
                risks=[
                    RequirementRisk(
                        risk_id="RISK-001",
                        type="UNTESTED_PATH",
                        severity="medium",
                        description="[dry-run] 模拟风险：部分路径未验证",
                        recommended_action="补充验证任务",
                    )
                ],
                coverage={"total": 2, "completed": 1, "partial": 0, "missing": 1, "score": 0.5},
                risk_level="medium",
            )
            primary = (context["projects"] or [{}])[0].get("ref", "")
            review.followup_tasks.append(FollowupTask(
                id="followup-001-dryrun-verify",
                title="补充验证（dry-run 模拟）",
                project=primary,
                prompt=(
                    "# 补充任务：补充验证（dry-run 模拟）\n\n"
                    "## 产生原因\nFinal Review 模拟发现 R-002 未验证。\n\n"
                    "## 验收标准\n- 验证完成\n"
                ),
                allowed_paths=["README.md"],
                requirement_ids=["R-002"],
                reason="dry-run simulated gap",
                acceptance_criteria=["验证完成"],
                priority="P1",
            ))
            return review

        return RequirementReview(
            run_id=run_id,
            status="COMPLETE",
            summary="[dry-run] 模拟审查：所有需求项已满足",
            requirements=[
                RequirementResult(
                    requirement_id="R-001",
                    description="dry-run 模拟需求项",
                    status="PASS",
                    evidence_grade="A",
                    task_ids=task_ids,
                    changed_files=changed,
                    evidence=["dry-run change appended"],
                )
            ],
            coverage={"total": 1, "completed": 1, "partial": 0, "missing": 0, "score": 1.0},
            risk_level="low",
        )

    def _build_prompt(self, context: dict[str, Any]) -> str:
        projects = "\n".join(
            f"- {p.get('ref')}: {p.get('path')} (branch={p.get('branch', '?')})"
            for p in context["projects"]
        ) or "(unknown)"

        task_list = "\n".join(
            f"- {t['task_id']}: {t['status']} (attempt={t['attempt']}, "
            f"commit={(t.get('commit_sha') or '')[:12]})"
            for t in context["tasks"]
        ) or "(no tasks)"

        reports = "\n\n".join(
            f"#### {r['task_id']}\n{r['report']}" for r in context["reports"]
        ) or "(no reports)"

        git_diff = "\n\n".join(context["git_sections"]) or "(no git evidence)"
        test_results = "\n\n".join(context["test_sections"]) or "(no tests)"

        return REVIEWER_PROMPT_TEMPLATE.format(
            projects=projects,
            requirement=context["requirement"],
            task_list=task_list,
            reports=reports,
            git_diff=git_diff,
            claimed_vs_actual=context["claimed_vs_actual"],
            test_results=test_results,
        )

    def _parse_review(self, run_id: str, text: str) -> RequirementReview:
        """Parse the reviewer's JSON answer (tolerates ```json wrapping)."""
        match = re.search(r"```json\s*(\{.*\})\s*```", text, re.DOTALL)
        raw = match.group(1) if match else None
        if raw is None:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            raw = match.group() if match else ""
        try:
            data = json.loads(raw)
        except Exception as exc:
            logger.error(f"[closure] reviewer returned unparseable JSON: {exc}")
            return RequirementReview(
                run_id=run_id,
                status="BLOCKED",
                summary=f"reviewer output could not be parsed: {exc}",
                raw_response=text[:4000],
            )
        data["run_id"] = run_id
        return RequirementReview.from_dict(data)

    # --------------------------------------------------------------- output

    def _write_reports(self, run_id: str, round_: int, review: RequirementReview) -> tuple[Path, Path]:
        output_dir = run_reports_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / f"{run_id}-final-review-r{round_}.json"
        md_path = output_dir / f"{run_id}-final-review-r{round_}.md"

        json_path.write_text(
            json.dumps(review.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        lines = [
            f"# Final Requirement Review — {run_id} (round {round_})",
            "",
            f"- **状态**: {review.status}",
            f"- **覆盖率**: {review.coverage.get('completed', 0)}/{review.coverage.get('total', 0)}"
            f" (score={review.coverage_score:.2f})",
            f"- **风险等级**: {review.risk_level}",
            f"- **revisions**: {review.base_revision[:12]} → {review.final_revision[:12]}",
            "",
            f"## 总结",
            "",
            review.summary or "(none)",
            "",
            "## 需求项",
            "",
            "| ID | 描述 | 状态 | 证据等级 | 任务 |",
            "|----|------|------|---------|------|",
        ]
        for r in review.requirements:
            lines.append(
                f"| {r.requirement_id} | {r.description} | {r.status} | "
                f"{r.evidence_grade or '-'} | {', '.join(r.task_ids) or '-'} |"
            )
        lines += ["", "## 风险", ""]
        if review.risks:
            lines.append("| ID | 类型 | 严重度 | 描述 |")
            lines.append("|----|------|--------|------|")
            for k in review.risks:
                lines.append(
                    f"| {k.risk_id} | {k.type} | {k.severity} | {k.description} |"
                )
        else:
            lines.append("(none)")
        lines += ["", "## 补充任务", ""]
        if review.followup_tasks:
            for t in review.followup_tasks:
                lines.append(f"- **{t.id}**: {t.title} [{t.priority}] → {t.project}")
        else:
            lines.append("(none)")
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return md_path, json_path

    # ------------------------------------------------------------- replan

    def _should_replan(self, review: RequirementReview, round_: int) -> bool:
        fr = self._config.final_review
        if review.status == "RISK_ACCEPTED":
            logger.warning("[closure] RISK_ACCEPTED requires human acknowledgement")
            return False
        if review.status == "COMPLETE":
            # fail_on_high_risk: a high risk downgrades a COMPLETE verdict.
            if (fr.fail_on_high_risk and review.status == "COMPLETE"
                    and any(k.severity == "high" for k in review.risks)):
                logger.warning(
                    "[closure] COMPLETE downgraded: high risk present "
                    "(final_review.fail_on_high_risk)"
                )
                return True
            return False
        if not fr.auto_replan:
            return False
        if round_ >= fr.max_rounds:
            logger.warning(
                f"[closure] max_rounds={fr.max_rounds} reached; no more replanning"
            )
            return False
        return True

    def _write_followup_tasks(
        self, run_id: str, context: dict[str, Any], review: RequirementReview
    ) -> list[Path]:
        if not review.followup_tasks:
            return []
        primary_ref = (context["projects"] or [{}])[0].get("ref", "")
        from planner import validate_plan, write_tasks  # reuse numbering + front-matter

        task_dicts = []
        for t in review.followup_tasks:
            prompt = t.prompt or _compose_followup_prompt(t)
            task_dicts.append({
                "id": t.id,
                "title": t.title,
                "project": t.project or primary_ref,
                "prompt": prompt,
                "allowed_paths": t.allowed_paths,
                "depends_on": t.depends_on,
                "requirement_ids": t.requirement_ids,
                "reason": t.reason,
            })
        refs = {str(p.get("ref") or "") for p in context["projects"]}
        existing_ids = {str(t.get("task_id") or "") for t in context["tasks"]}
        validate_plan(
            task_dicts, allowed_projects=refs,
            allowed_dependency_ids=existing_ids,
        )
        written = write_tasks(task_dicts, primary_ref, run_id=run_id)
        from requirement_run import save_plan_tasks
        next_round = self._store.get_review_count(run_id) + 1
        save_plan_tasks(
            run_id, task_dicts, task_store=self._store, round_=next_round,
            source_review_id=f"{run_id}-r{next_round - 1}",
        )
        return written


def _compose_followup_prompt(t: FollowupTask) -> str:
    lines = [f"# 补充任务：{t.title}", ""]
    if t.reason:
        lines += ["## 产生原因", "", t.reason, ""]
    if t.requirement_ids:
        lines += ["关联需求项: " + ", ".join(t.requirement_ids), ""]
    if t.acceptance_criteria:
        lines += ["## 验收标准", ""]
        lines += [f"{i+1}. {c}" for i, c in enumerate(t.acceptance_criteria)]
        lines.append("")
    if t.validation_command:
        lines += ["## 验证命令", "", f"```", t.validation_command, "```", ""]
    return "\n".join(lines)


def _response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        parts = response.get("parts")
        if isinstance(parts, list):
            texts = [
                p.get("text", "")
                for p in parts
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
        for key in ("message", "text", "content", "output"):
            if key in response:
                return str(response[key])
    return str(response)
