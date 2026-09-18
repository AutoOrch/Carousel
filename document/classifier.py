"""CLASSIFY node — project + category identification (p11 §5, §6).

Deterministic rules first; the LLM is only consulted in opencode mode
when the rules are below the confidence threshold.  Categories come from
a fixed taxonomy — the LLM picks, it never invents.

Project identification priority (p11 §4):
    1. front matter ``project`` (config ID or repo path)  -> confidence 1.0
    2. file name (project id / keywords)
    3. content keywords (config.yaml ``projects.<id>.keywords``)
    4. LLM semantic classification (opencode mode only)
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from config import ROOT, Config, DocumentsConfig, get_project
from log import get_logger
from opencode_client import OpenCodeClient

from document.schemas import DOC_CATEGORIES, Classification, ExtractedDoc
from document.utils import extract_json_object, id_in_text, response_text

logger = get_logger(__name__)

# p11 §6 taxonomy — filename hits weigh more than content hits.
CATEGORY_PATTERNS: dict[str, list[str]] = {
    "requirements": ["需求", "requirement", "prd", "用户故事", "user story", "验收"],
    "design": ["设计", "方案", "design", "spec", "原型"],
    "architecture": ["架构", "architecture", "部署", "拓扑", "deployment"],
    "research": ["调研", "研究", "research", "对比", "评测", "分析"],
    "implementation": ["实现", "落地", "implementation", "implemented", "开发记录"],
    "validation": ["验证", "验收", "测试结果", "validation", "verified"],
    "meeting": ["会议", "纪要", "meeting", "minutes", "周会", "评审"],
    "api": ["api", "接口", "endpoint", "openapi", "swagger"],
    "task": ["任务", "计划", "task", "todo", "排期"],
    "report": ["报告", "report", "总结", "复盘", "summary"],
    "decision": ["决策", "decision", "adr", "为什么", "选型"],
    "archive": ["归档", "archive", "备份", "backup", "旧版"],
}

CLASSIFY_PROMPT_TEMPLATE = """你是文档归档分类 Agent，不是编码 Agent。

任务：判断下面的文档属于哪个项目、哪个类别。禁止执行任何文件操作。

候选项目（id — 关键词）：
{projects_block}

项目 id 必须从上面选择；确实无法判断时填 "unknown"，不要发明不存在的项目。

类别（固定 taxonomy，只能从中选择一个）：
{categories}

文档信息：
- 文件名：{name}
- 标题：{title}
- front matter：{front_matter}
- 内容摘要：
{preview}

严格只输出一个 ```json 代码块，不要输出其它内容，格式：
```json
{{"project": "<项目 id 或 unknown>", "category": "<类别>", "confidence": <0.0-1.0 的数字>, "reason": "<简短中文理由>"}}
```"""


def classify_all(
    extracted: list[ExtractedDoc],
    config: Config,
    docs_cfg: DocumentsConfig,
    mode: str = "dry-run",
    opencode_url: str = "",
    forced_project: str = "",
) -> list[Classification]:
    """Classify documents in parallel (LLM calls are the slow part).

    ``forced_project`` (--project) skips project identification entirely —
    same authority as a front-matter project (confidence 1.0); the category
    is still derived from rules.
    """
    if not extracted:
        return []
    workers = max(1, min(docs_cfg.max_workers, len(extracted)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(
            pool.map(
                lambda ex: classify_one(
                    ex, config, docs_cfg, mode, opencode_url, forced_project
                ),
                extracted,
            )
        )


def classify_one(
    ex: ExtractedDoc,
    config: Config,
    docs_cfg: DocumentsConfig,
    mode: str = "dry-run",
    opencode_url: str = "",
    forced_project: str = "",
) -> Classification:
    category, _ = _rule_category(ex)

    # Human review decisions are keyed by content hash, so a same-named later
    # document cannot accidentally inherit an old decision.
    decision_path = docs_cfg.review_dir / "decisions.json"
    if decision_path.exists():
        try:
            decisions = json.loads(decision_path.read_text(encoding="utf-8"))
            decision = (decisions.get("decisions") or {}).get(ex.sha256)
            if isinstance(decision, dict):
                project = get_project(config, str(decision.get("project") or ""))
                chosen_category = str(decision.get("category") or category)
                if project is not None and chosen_category in DOC_CATEGORIES:
                    return Classification(
                        project=project.id,
                        category=chosen_category,
                        confidence=1.0,
                        reason=str(decision.get("reason") or "人工复核确认"),
                        method="human-review",
                        candidates=[(project.id, 1.0)],
                    )
        except (OSError, ValueError) as exc:
            logger.warning(f"[classify] cannot read human decisions: {exc}")

    # CLI --project override — highest authority next to front matter.
    if forced_project:
        return Classification(
            project=forced_project,
            category=category,
            confidence=1.0,
            reason=f"--project 指定项目：{forced_project}",
            method="forced",
            candidates=[(forced_project, 1.0)],
        )

    # Priority 1: front matter project (p11 §4).
    fm_project = str((ex.front_matter or {}).get("project", "") or "").strip()
    if fm_project:
        project = get_project(config, fm_project)
        if project is not None:
            return Classification(
                project=project.id,
                category=category,
                confidence=1.0,
                reason=f"front matter 指定 project: {fm_project}",
                method="front-matter",
                candidates=[(project.id, 1.0)],
            )
        logger.warning(
            f"[classify] front matter project '{fm_project}' unknown "
            f"({ex.doc.name}) — falling back to rules"
        )

    # Priorities 2 + 3: filename and content rules (p11 §5).
    scores = _project_scores(ex, config)
    ranked = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)

    # Priority 4: LLM — only when rules are inconclusive (p11 §5 flow).
    if mode == "opencode" and (
        not ranked or ranked[0][1][0] < docs_cfg.confidence_threshold
    ):
        llm = _llm_classify(ex, config, opencode_url, ranked)
        if llm is not None:
            if llm.project and llm.project not in config.projects:
                logger.warning(
                    f"[classify] LLM invented project '{llm.project}' "
                    f"({ex.doc.name}) — treating as unknown"
                )
                llm.project = ""
            if llm.category not in DOC_CATEGORIES:
                llm.category = category
            return llm

    if ranked:
        best_id, (score, reasons) = ranked[0]
        return Classification(
            project=best_id,
            category=category,
            confidence=score,
            reason="；".join(reasons[:3]),
            method="rules",
            candidates=[(pid, s) for pid, (s, _) in ranked],
        )

    return Classification(
        project="",
        category=category,
        confidence=0.0,
        reason="无法识别项目归属",
        method="rules",
        candidates=[],
    )


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

def _project_scores(
    ex: ExtractedDoc, config: Config
) -> dict[str, tuple[float, list[str]]]:
    name = ex.doc.name
    content = ex.preview or ""
    result: dict[str, tuple[float, list[str]]] = {}

    for pid, project in config.projects.items():
        score = 0.0
        reasons: list[str] = []
        if id_in_text(name, pid):
            score += 0.9
            reasons.append("文件名包含项目 ID")
        if id_in_text(content, pid):
            score += 0.3
            reasons.append("内容提到项目 ID")
        for keyword in project.keywords:
            if not keyword:
                continue
            if id_in_text(name, keyword):
                score += 0.15
                reasons.append(f"文件名命中关键词「{keyword}」")
            elif id_in_text(content, keyword):
                score += 0.08
                reasons.append(f"内容命中关键词「{keyword}」")
        if score > 0:
            result[pid] = (min(score, 0.95), reasons)
    return result


def _rule_category(ex: ExtractedDoc) -> tuple[str, float]:
    name = ex.doc.name
    content = ex.preview or ""
    scores: dict[str, int] = {}
    for category, patterns in CATEGORY_PATTERNS.items():
        score = 0
        for pattern in patterns:
            if id_in_text(name, pattern):
                score += 2
            elif id_in_text(content, pattern):
                score += 1
        if score:
            scores[category] = score
    if not scores:
        return "archive", 0.0
    best = max(scores, key=scores.get)
    return best, min(0.9, 0.5 + 0.1 * scores[best])


# --------------------------------------------------------------------------
# LLM (opencode mode only)
# --------------------------------------------------------------------------

def _llm_classify(
    ex: ExtractedDoc,
    config: Config,
    opencode_url: str,
    ranked: list[tuple[str, tuple[float, list[str]]]],
) -> Classification | None:
    projects_block = "\n".join(
        f"- {pid}: {', '.join(project.keywords) or '(无关键词)'}"
        for pid, project in config.projects.items()
    )
    prompt = CLASSIFY_PROMPT_TEMPLATE.format(
        projects_block=projects_block or "(无注册项目)",
        categories=", ".join(DOC_CATEGORIES),
        name=ex.doc.name,
        title=ex.title,
        front_matter=(
            json.dumps(ex.front_matter, ensure_ascii=False)
            if ex.front_matter
            else "(无)"
        ),
        preview=ex.preview or "(无文本内容)",
    )

    try:
        client = OpenCodeClient(base_url=opencode_url, timeout=600)
        session = client.create_session(
            title=f"doc-classify-{ex.doc.name}", directory=str(ROOT)
        )
        session_id = session["id"]
        try:
            response = client.send_message(session_id, prompt)
        finally:
            client.delete_session(session_id)
    except Exception as exc:
        logger.warning(f"[classify] LLM classification failed ({ex.doc.name}): {exc}")
        return None

    data = extract_json_object(response_text(response))
    if data is None:
        logger.warning(f"[classify] no JSON in LLM reply ({ex.doc.name})")
        return None

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    project = str(data.get("project", "") or "").strip()
    if project.lower() in ("unknown", "none", "null"):
        project = ""

    return Classification(
        project=project,
        category=str(data.get("category", "") or "").strip(),
        confidence=max(0.0, min(confidence, 1.0)),
        reason=str(data.get("reason", "") or "LLM 语义判断")[:300],
        method="llm",
        candidates=[(pid, score) for pid, (score, _) in ranked],
    )
