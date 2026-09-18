"""Document pipeline graph (p11 §7, §15): the 7 first-version nodes.

    START → SCAN → EXTRACT → CLASSIFY → PLAN → ARCHIVE → INDEX → REPORT → END

Per-document parallelism (p11 §7 "互不冲突的文档可以并行处理") happens
*inside* EXTRACT / CLASSIFY / ARCHIVE via worker threads — independent
documents are processed concurrently while the graph stays linear and
robust (same approach as the task WorkerPool).

Note: this module lives at the repo root next to ``task_graph.py`` — a
``graph/`` package would shadow the existing ``graph.py`` P1 entrypoint.
"""

from __future__ import annotations

from typing import Any, TypedDict
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from config import Config
from document import classifier, executor, extractor, indexer, planner, reporter, scanner
from document.schemas import ArchiveResult, DocumentPlan
from log import get_logger

logger = get_logger(__name__)


def _phase_event(state: "DocumentState", phase: str, count: int = 0) -> None:
    store = state.get("task_store")
    run_id = state.get("document_run_id")
    if store and run_id:
        store.append_event(
            f"document.phase.{phase}.completed", run_id=run_id,
            status="COMPLETED", payload={"phase": phase, "count": count},
        )


class DocumentState(TypedDict, total=False):
    config: Config
    mode: str                 # dry-run | opencode
    opencode_url: str
    forced_project: str       # --project: skip project identification
    entries: list[dict[str, Any]]   # {doc, extracted, classification, plan}
    plans: list[DocumentPlan]
    results: list[ArchiveResult]
    report_path: str
    stats: dict[str, int]
    task_store: Any
    document_run_id: str


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def scan_node(state: DocumentState) -> dict[str, Any]:
    config: Config = state["config"]
    files = scanner.claim_documents(config.documents)
    run_id = ""
    store = state.get("task_store")
    if files and store:
        run_id = store.begin_document_run(
            state.get("mode", "dry-run"),
            [{"item_id": f.name, "name": f.name, "path": str(f.path)} for f in files],
        )
    logger.info(
        f"[document] SCAN: {len(files)} document(s) claimed from "
        f"{config.documents.input_dir}"
    )
    return {"entries": [{"doc": f} for f in files], "document_run_id": run_id}


def extract_node(state: DocumentState) -> dict[str, Any]:
    config: Config = state["config"]
    entries = state.get("entries") or []
    extracted = extractor.extract_all(
        [e["doc"] for e in entries], config.documents
    )
    for entry, ex in zip(entries, extracted):
        entry["extracted"] = ex
        if state.get("task_store") and state.get("document_run_id"):
            state["task_store"].update_document_item(
                state["document_run_id"], ex.doc.name,
                sha256=ex.sha256, status="EXTRACTED", detail=ex.note,
            )
    logger.info(f"[document] EXTRACT: {len(entries)} document(s) hashed + parsed")
    _phase_event(state, "extract", len(entries))
    return {"entries": entries}


def classify_node(state: DocumentState) -> dict[str, Any]:
    config: Config = state["config"]
    entries = state.get("entries") or []
    classified = classifier.classify_all(
        [e["extracted"] for e in entries],
        config,
        config.documents,
        mode=state.get("mode", "dry-run"),
        opencode_url=state.get("opencode_url", ""),
        forced_project=state.get("forced_project", ""),
    )
    for entry, cl in zip(entries, classified):
        entry["classification"] = cl
        if state.get("task_store") and state.get("document_run_id"):
            state["task_store"].update_document_item(
                state["document_run_id"], entry["extracted"].doc.name,
                project_id=cl.project, category=cl.category,
                confidence=cl.confidence, status="CLASSIFIED",
            )
    for entry in entries:
        cl = entry["classification"]
        logger.info(
            f"[document] CLASSIFY: {entry['extracted'].doc.name} -> "
            f"project={cl.project or '未知'} category={cl.category} "
            f"confidence={cl.confidence:.2f} ({cl.method})"
        )
    _phase_event(state, "classify", len(entries))
    return {"entries": entries}


def plan_node(state: DocumentState) -> dict[str, Any]:
    config: Config = state["config"]
    entries = state.get("entries") or []
    plans = planner.build_plans(entries, config, config.documents)
    for entry, plan in zip(entries, plans):
        entry["plan"] = plan
        logger.info(
            f"[document] PLAN: {plan.source} -> {plan.action} "
            f"{plan.target} ({plan.reason[:80]})"
        )
        if state.get("task_store") and state.get("document_run_id"):
            state["task_store"].update_document_item(
                state["document_run_id"], entry["doc"].name,
                action=plan.action, target_path=str(plan.target), status="PLANNED",
            )
    _phase_event(state, "plan", len(plans))
    return {"plans": plans, "entries": entries}


def archive_node(state: DocumentState) -> dict[str, Any]:
    config: Config = state["config"]
    plans = state.get("plans") or []
    results = executor.execute_plans(plans, config.documents)
    if state.get("task_store") and state.get("document_run_id"):
        for item in results:
            state["task_store"].update_document_item(
                state["document_run_id"], Path(item.plan.source).name,
                status=item.status.upper(), target_path=item.final_path or str(item.plan.target),
                detail=item.detail,
            )
            if item.status in ("archived", "review", "duplicate"):
                artifact_path = item.final_path or str(item.plan.target)
                state["task_store"].register_artifact(
                    f"DOCUMENT_{item.status.upper()}", artifact_path,
                    run_id=state["document_run_id"], content_hash=item.plan.sha256,
                )
    _phase_event(state, "archive", len(results))
    return {"results": results}


def index_node(state: DocumentState) -> dict[str, Any]:
    config: Config = state["config"]
    results = state.get("results") or []
    projects = sorted({
        r.plan.project for r in results if r.status == "archived" and r.plan.project
    })
    for pid in projects:
        path = indexer.update_index(pid, config.documents)
        if path is not None:
            logger.info(f"[document] INDEX: updated {path}")
    _phase_event(state, "index", len(projects))
    return {}


def report_node(state: DocumentState) -> dict[str, Any]:
    config: Config = state["config"]
    results = state.get("results") or []

    stats: dict[str, int] = {
        "scanned": len(state.get("entries") or []),
        "archived": 0,
        "duplicate": 0,
        "review": 0,
        "failed": 0,
    }
    for result in results:
        stats[result.status] = stats.get(result.status, 0) + 1

    if not results:
        return {"stats": stats, "report_path": ""}

    path = reporter.generate_report(
        results, config.documents, state.get("mode", "dry-run")
    )
    logger.info(f"[document] REPORT: {path}")
    if state.get("task_store") and state.get("document_run_id"):
        final = "FAILED" if stats.get("failed") else (
            "NEEDS_REVIEW" if stats.get("review") else "COMPLETED"
        )
        state["task_store"].finish_document_run(
            state["document_run_id"], final, str(path), stats
        )
        state["task_store"].register_artifact(
            "DOCUMENT_REPORT", str(path), run_id=state["document_run_id"]
        )
    return {"stats": stats, "report_path": str(path)}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def build_document_graph():
    """Compile the document pipeline graph (p11 §15 seven nodes)."""
    builder = StateGraph(DocumentState)

    builder.add_node("scan", scan_node)
    builder.add_node("extract", extract_node)
    builder.add_node("classify", classify_node)
    builder.add_node("plan", plan_node)
    builder.add_node("archive", archive_node)
    builder.add_node("index", index_node)
    builder.add_node("report", report_node)

    builder.add_edge(START, "scan")
    builder.add_edge("scan", "extract")
    builder.add_edge("extract", "classify")
    builder.add_edge("classify", "plan")
    builder.add_edge("plan", "archive")
    builder.add_edge("archive", "index")
    builder.add_edge("index", "report")
    builder.add_edge("report", END)

    return builder.compile()
