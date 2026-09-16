"""FastAPI service (async ingestion: the graph runs in a background task).

Run: uv run uvicorn src.api.main:app --reload

Responses carry status, counts, and value-free issue summaries only — never
raw_text, extracted_data, structured_records, final_content, or PII values.
"""
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional
from datetime import datetime
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from langgraph.types import Command
from pydantic import BaseModel
from sqlalchemy import func, select

from src.agents.quality import load_schemas
from src.governed import store as governed_store
from src.governed.sql_guard import enforce_row_limit, validate_sql
from src.governed.text_to_sql import generate_sql
from src.config.settings import settings
from src.graph.state import build_initial_state
from src.guardrails.input_guardrail import sanitize_for_log
from src.observability.tracing import get_run_config
from src.persistence.db import get_engine, get_session
from src.persistence.models import PipelineRun
from src.persistence.writer import (
    set_run_status,
    write_failed_run,
    write_pending_review,
    write_queued_run,
)

logger = logging.getLogger(__name__)

MAX_ISSUE_SUMMARY_CHARS = 2000
UPLOAD_CHUNK_BYTES = 1024 * 1024
FINAL_STATUSES = ["completed", "manual_review", "rejected", "failed"]


# ---------- helpers ----------

def build_issue_summary(payload: dict) -> str:
    """Value-free summary from an interrupt payload (issues are already value-free)."""
    parts = [f"reason: {payload.get('reason', 'unknown')}"]
    issues = list(payload.get("quality_issues") or []) + list(payload.get("policy_violations") or [])
    if issues:
        parts.append("; ".join(issues))
    summary = " | ".join(parts)
    if len(summary) > MAX_ISSUE_SUMMARY_CHARS:
        summary = summary[: MAX_ISSUE_SUMMARY_CHARS - 20] + " …(truncated)"
    return summary


def summarize_state(state: dict) -> dict:
    findings = state.get("pii_findings") or []
    by_type: dict[str, int] = {}
    for f in findings:
        by_type[f["entity_type"]] = by_type.get(f["entity_type"], 0) + 1
    return {
        "document_id": state.get("document_id"),
        "status": state.get("final_status"),
        "final_status": state.get("final_status"),
        "file_name": state.get("file_name"),
        "dataset_type": state.get("dataset_type"),
        "data_type": state.get("data_type"),
        "sensitivity_flag": state.get("sensitivity_flag"),
        "quality_status": state.get("quality_status"),
        "quality_issue_count": len(state.get("quality_issues") or []),
        "retry_count": state.get("retry_count"),
        "extraction_model_used": state.get("extraction_model_used"),
        "annotation_model_used": state.get("annotation_model_used"),
        "pii_findings_count": len(findings),
        "pii_findings_by_type": by_type,
        "output_guardrail_passed": state.get("output_guardrail_passed"),
        "policy_violation_count": len(state.get("policy_violations") or []),
        "rejection_reason": state.get("rejection_reason"),
        "error_message": state.get("error_message"),
        "human_reviewer_notes": state.get("human_reviewer_notes"),
        "started_at": state.get("started_at"),
        "completed_at": state.get("completed_at"),
    }


def handle_result(result: dict, document_id: str, file_name: str) -> dict:
    """Shared by /ingest and /review: detect interrupt → pending row, else summary."""
    interrupts = result.get("__interrupt__")
    if interrupts:
        payload = interrupts[0].value
        summary = build_issue_summary(payload)
        write_pending_review(document_id, file_name, summary)
        return {
            "document_id": document_id,
            "status": "pending_review",
            "reason": payload.get("reason"),
            "issue_summary": summary,
            "quality_issues": payload.get("quality_issues") or [],
            "policy_violations": payload.get("policy_violations") or [],
        }
    return summarize_state(result)


def _row_to_dict(row: PipelineRun) -> dict:
    d = {c.name: getattr(row, c.name) for c in PipelineRun.__table__.columns}
    d["pii_findings_summary"] = json.loads(d["pii_findings_summary"]) if d.get("pii_findings_summary") else []
    return d


# ---------- app ----------

class ReviewDecision(BaseModel):
    decision: Literal["approve", "reject"]
    notes: str = ""


class QueryRequest(BaseModel):
    question: str
    dataset_type: str
    sensitive: bool = False


def execute_pipeline(graph, document_id: str, file_path: str, file_name: str,
                     needs_annotation: bool, dataset_type: str, started_at: str) -> dict:
    """Runs the graph for one document. Executed in a background task, never in
    the request thread. The checkpointer (SqliteSaver, check_same_thread=False
    with an internal lock) is safe to use from here, so interrupt/resume still
    works exactly as before."""
    set_run_status(document_id, "running")
    state = build_initial_state(file_path, file_name, needs_annotation, dataset_type, document_id=document_id)
    state["started_at"] = started_at
    try:
        result = graph.invoke(state, config=get_run_config(document_id))
    except Exception as e:  # noqa: BLE001 — node failures are handled in-graph; this is infra
        logger.exception("background pipeline run failed for document_id=%s", document_id)
        write_failed_run(document_id, f"pipeline error: {type(e).__name__}")
        return {"document_id": document_id, "status": "failed"}
    # handle_result writes the pending-review row (run_status="paused") when the
    # run paused at manual_review.
    summary = handle_result(result, document_id, file_name)
    if summary.get("status") != "pending_review":
        # persistence_node already wrote the terminal row; close the lifecycle field
        # here too so run_status is correct even if a run ends without it.
        set_run_status(document_id, "done")
    return summary


def create_app(graph: Any = None) -> FastAPI:
    """`graph` lets tests inject a MemorySaver-compiled graph; by default the
    app compiles with SqliteSaver on DB_CHECKPOINT_PATH at startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if getattr(app.state, "graph", None) is None:
            from src.graph.graph import compile_api_graph
            app.state.graph = compile_api_graph()
        get_engine()  # create pipeline.db tables eagerly
        governed_store.ensure_all_tables()
        yield

    app = FastAPI(title="Data Governance Pipeline", version="1.0.0", lifespan=lifespan)
    app.state.graph = graph

    def get_graph(request: Request):
        return request.app.state.graph

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/ingest", status_code=202)
    def ingest(
        background_tasks: BackgroundTasks,
        file: UploadFile = File(...),
        needs_annotation: bool = Form(...),
        dataset_type: str = Form(...),
        graph=Depends(get_graph),
    ):
        if dataset_type not in load_schemas():
            raise HTTPException(status_code=400, detail=f"unknown dataset_type; expected one of {sorted(load_schemas())}")

        document_id = str(uuid4())
        file_name = sanitize_for_log(file.filename) or "upload"
        upload_dir = Path(settings.UPLOAD_DIR) / document_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest = upload_dir / file_name

        # Stream to disk; stop one chunk past the limit so the input guardrail
        # (the single source of truth for validation) still sees and rejects it.
        written = 0
        with open(dest, "wb") as out:
            while chunk := file.file.read(UPLOAD_CHUNK_BYTES):
                out.write(chunk)
                written += len(chunk)
                if written > settings.MAX_FILE_SIZE_BYTES:
                    break

        # Row exists from the moment of ingestion, so GET /status is meaningful
        # before the graph has run at all.
        started_at = datetime.utcnow().isoformat()
        write_queued_run(document_id, file_name, dataset_type, needs_annotation, started_at)
        background_tasks.add_task(execute_pipeline, graph, document_id, str(dest),
                                  file_name, needs_annotation, dataset_type, started_at)
        return JSONResponse(status_code=202, content={
            "document_id": document_id,
            "status": "queued",
            "run_status": "queued",
            "file_name": file_name,
            "dataset_type": dataset_type,
            "poll": f"/status/{document_id}",
        })

    @app.get("/status/{document_id}")
    def status(document_id: str):
        with get_session() as session:
            row = session.get(PipelineRun, document_id)
            if row is None:
                raise HTTPException(status_code=404, detail="document not found")
            return _row_to_dict(row)

    @app.get("/reviews/pending")
    def pending_reviews():
        with get_engine().connect() as conn:
            rows = conn.execute(
                select(PipelineRun.document_id, PipelineRun.file_name, PipelineRun.review_issue_summary)
                .where(PipelineRun.review_status == "pending")
                .order_by(PipelineRun.document_id)
            ).mappings().all()
        return [dict(r) for r in rows]

    @app.post("/review/{document_id}")
    def review(document_id: str, body: ReviewDecision, graph=Depends(get_graph)):
        config = {"configurable": {"thread_id": document_id}}
        snapshot = graph.get_state(config)
        if not snapshot or not snapshot.values:
            raise HTTPException(status_code=404, detail="no pipeline run found for document_id")
        if "manual_review" not in (snapshot.next or ()):
            raise HTTPException(status_code=409, detail="document is not awaiting review")

        file_name = snapshot.values.get("file_name") or ""
        try:
            result = graph.invoke(
                Command(resume={"decision": body.decision, "notes": body.notes}),
                config={**get_run_config(document_id), **config},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("graph resume failed for document_id=%s", document_id)
            raise HTTPException(status_code=500, detail=f"pipeline error: {type(e).__name__}") from e
        return handle_result(result, document_id, file_name)

    @app.post("/query")
    def query(body: QueryRequest, graph=Depends(get_graph)):
        """Natural-language question -> guarded SELECT over the governed store.

        Blocked queries return 200 with blocked=true and a value-free reason, so
        the dashboard can render the reason instead of a stack trace."""
        if body.dataset_type not in load_schemas():
            raise HTTPException(status_code=400, detail=f"unknown dataset_type; expected one of {sorted(load_schemas())}")
        if not body.question.strip():
            raise HTTPException(status_code=400, detail="question must not be empty")

        table = governed_store.ensure_table(body.dataset_type)
        try:
            sql, model_used = generate_sql(body.question, body.dataset_type, sensitive=body.sensitive)
        except Exception as e:  # noqa: BLE001 — model/transport failure, not a user error
            logger.exception("text-to-sql generation failed")
            raise HTTPException(status_code=502, detail=f"SQL generation failed: {type(e).__name__}") from e

        guard = validate_sql(sql, {table})
        if not guard.allowed:
            logger.warning("blocked query for table=%s reason=%s", table, guard.reason)
            return {"dataset_type": body.dataset_type, "table": table, "sql": sql, "model_used": model_used,
                    "blocked": True, "reason": guard.reason, "columns": [], "rows": [], "row_count": 0}

        limited = enforce_row_limit(sql, settings.GOVERNED_ROW_LIMIT)
        try:
            columns, rows = governed_store.run_query(limited)
        except TimeoutError as e:
            logger.warning("query timeout for table=%s", table)
            return {"dataset_type": body.dataset_type, "table": table, "sql": sql, "model_used": model_used,
                    "blocked": True, "reason": str(e), "columns": [], "rows": [], "row_count": 0}
        except Exception as e:  # noqa: BLE001 — invalid column, type error, etc.
            logger.warning("query execution failed for table=%s: %s", table, type(e).__name__)
            return {"dataset_type": body.dataset_type, "table": table, "sql": sql, "model_used": model_used,
                    "blocked": True, "reason": f"query could not be executed ({type(e).__name__})",
                    "columns": [], "rows": [], "row_count": 0}

        return {"dataset_type": body.dataset_type, "table": table, "sql": sql, "model_used": model_used,
                "blocked": False, "reason": None, "columns": columns, "rows": rows, "row_count": len(rows),
                "row_limit": settings.GOVERNED_ROW_LIMIT}

    @app.get("/governed/tables")
    def governed_tables():
        out = []
        for dataset_type, name in governed_store.governed_tables().items():
            out.append({"dataset_type": dataset_type, "table": name,
                        "columns": governed_store.table_columns(dataset_type),
                        "row_count": governed_store.row_count(dataset_type)})
        return out

    @app.get("/runs")
    def recent_runs(limit: int = 50):
        limit = max(1, min(limit, 500))
        with get_session() as session:
            rows = session.execute(
                select(PipelineRun).order_by(PipelineRun.started_at.desc().nulls_last()).limit(limit)
            ).scalars().all()
            return [_row_to_dict(r) for r in rows]

    @app.get("/dashboard-data")
    def dashboard_data():
        with get_engine().connect() as conn:
            rows = conn.execute(
                select(
                    PipelineRun.final_status, PipelineRun.retry_count, PipelineRun.review_status,
                    PipelineRun.processing_time_ms, PipelineRun.pii_findings_summary,
                )
            ).all()
            pending = conn.execute(
                select(func.count()).select_from(PipelineRun).where(PipelineRun.review_status == "pending")
            ).scalar_one()

        finished = [r for r in rows if r.final_status is not None]
        total = len(finished)
        breakdown = {s: 0 for s in FINAL_STATUSES}
        pii_by_type: dict[str, int] = {}
        times = []
        retried = 0
        for r in finished:
            breakdown[r.final_status] = breakdown.get(r.final_status, 0) + 1
            if (r.retry_count or 0) > 0:
                retried += 1
            if r.processing_time_ms is not None:
                times.append(r.processing_time_ms)
            for f in json.loads(r.pii_findings_summary or "[]"):
                pii_by_type[f["entity_type"]] = pii_by_type.get(f["entity_type"], 0) + 1

        def rate(n: int) -> Optional[float]:
            return round(n / total, 4) if total else 0.0

        return {
            "total_processed": total,
            "status_breakdown": breakdown,  # "failed" is its own slice, distinct from manual_review
            "retry_rate": rate(retried),
            "manual_review_rate": rate(breakdown["manual_review"]),
            "pending_review_count": pending,
            "avg_processing_time_ms": round(sum(times) / len(times), 3) if times else None,
            "pii_findings_by_type": pii_by_type,
        }

    return app


app = create_app()
