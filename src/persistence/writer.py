"""The only module that writes pipeline outcomes.

Never writes raw_text, extracted_data, structured_records, final_content, or
any PII value (raw or masked). Only metadata, counts, and value-free summaries.
"""
import json
import logging
import threading
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert

from src.governed.store import write_governed_rows
from src.graph.state import NodeTimer, PipelineState
from src.persistence.db import get_engine
from src.persistence.models import PipelineEvent, PipelineRun

logger = logging.getLogger(__name__)

# SQLite allows one writer at a time. Background runs are threads in this process,
# so serializing writes here avoids contention before SQLite's busy timeout is hit.
_write_lock = threading.Lock()


def _processing_time_ms(started_at: str | None, completed_at: str | None) -> float | None:
    try:
        return round(
            (datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)).total_seconds() * 1000, 3
        )
    except (TypeError, ValueError):
        return None


def _upsert_run(conn, values: dict) -> None:
    stmt = insert(PipelineRun).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[PipelineRun.document_id],
        set_={k: stmt.excluded[k] for k in values if k != "document_id"},
    )
    conn.execute(stmt)


def write_pipeline_result(state: PipelineState) -> None:
    """Upsert pipeline_runs + replace pipeline_events, in one transaction.
    Called for every terminal path: completed, manual_review, rejected, failed."""
    document_id = state["document_id"]
    findings = state.get("pii_findings") or []
    engine = get_engine()
    with _write_lock, engine.begin() as conn:
        existing_review = conn.execute(
            select(PipelineRun.review_status, PipelineRun.review_issue_summary)
            .where(PipelineRun.document_id == document_id)
        ).first()
        was_pending = existing_review is not None and existing_review.review_status in ("pending", "resolved")

        values = {
            "document_id": document_id,
            "file_name": state.get("file_name"),
            "dataset_type": state.get("dataset_type"),
            "data_type": state.get("data_type"),
            "sensitivity_flag": state.get("sensitivity_flag"),
            "quality_status": state.get("quality_status"),
            "retry_count": state.get("retry_count"),
            "needs_annotation": state.get("needs_annotation"),
            "annotation_model_used": state.get("annotation_model_used"),
            "extraction_model_used": state.get("extraction_model_used"),
            "pii_findings_count": len(findings),
            "pii_findings_summary": json.dumps(
                [{"entity_type": f["entity_type"], "action": f["action"]} for f in findings]
            ),
            "final_status": state.get("final_status"),
            "error_message": state.get("error_message"),
            "human_reviewer_notes": state.get("human_reviewer_notes"),
            "review_status": "resolved" if was_pending else None,
            "run_status": "done",
            "review_issue_summary": existing_review.review_issue_summary if existing_review else None,
            "started_at": state.get("started_at"),
            "completed_at": state.get("completed_at"),
            "processing_time_ms": _processing_time_ms(state.get("started_at"), state.get("completed_at")),
        }
        _upsert_run(conn, values)

        # node_trace is cumulative across a resumed run, so replace rather than append.
        conn.execute(delete(PipelineEvent).where(PipelineEvent.document_id == document_id))
        trace = state.get("node_trace") or []
        if trace:
            conn.execute(
                PipelineEvent.__table__.insert(),
                [
                    {
                        "document_id": document_id,
                        "node_name": e["node_name"],
                        "timestamp": e["timestamp"],
                        "duration_ms": e["duration_ms"],
                        "status": e["status"],
                    }
                    for e in trace
                ],
            )


def write_pending_review(document_id: str, file_name: str, issue_summary: str) -> None:
    """Record an interrupted-but-unresolved document (called by the API layer,
    never by a graph node). issue_summary must already be value-free."""
    with _write_lock, get_engine().begin() as conn:
        _upsert_run(
            conn,
            {
                "document_id": document_id,
                "file_name": file_name,
                "review_status": "pending",
                "review_issue_summary": issue_summary,
                "run_status": "paused",
            },
        )


def write_queued_run(document_id: str, file_name: str, dataset_type: str,
                     needs_annotation: bool, started_at: str) -> None:
    """Row written at ingestion time so GET /status is meaningful immediately,
    before the background run starts."""
    with _write_lock, get_engine().begin() as conn:
        _upsert_run(conn, {
            "document_id": document_id,
            "file_name": file_name,
            "dataset_type": dataset_type,
            "needs_annotation": needs_annotation,
            "started_at": started_at,
            "run_status": "queued",
            "retry_count": 0,
            "pii_findings_count": 0,
        })


def set_run_status(document_id: str, run_status: str) -> None:
    with _write_lock, get_engine().begin() as conn:
        conn.execute(
            PipelineRun.__table__.update()
            .where(PipelineRun.document_id == document_id)
            .values(run_status=run_status)
        )


def write_failed_run(document_id: str, error_message: str) -> None:
    """Terminal write for an infrastructure failure outside the graph (the graph's
    own node failures are persisted by persistence_node as usual)."""
    completed_at = datetime.utcnow().isoformat()
    with _write_lock, get_engine().begin() as conn:
        conn.execute(
            PipelineRun.__table__.update()
            .where(PipelineRun.document_id == document_id)
            .values(final_status="failed", error_message=error_message,
                    run_status="done", completed_at=completed_at)
        )


def persistence_node(state: PipelineState) -> dict:
    timer = NodeTimer("persistence")
    completed_at = datetime.utcnow().isoformat()
    entry = timer.entry("ok")
    try:
        final_state = {**state, "completed_at": completed_at,
                       "node_trace": [*(state.get("node_trace") or []), entry]}
        write_pipeline_result(final_state)
        # Governed analytical store: only completed AND guardrail-passed documents
        # become queryable. A failure here must not flip an already-persisted
        # terminal status, so it is logged rather than raised.
        try:
            write_governed_rows(final_state)
        except Exception as e:  # noqa: BLE001
            logger.error("governed store write failed for document_id=%s: %s",
                         state.get("document_id"), type(e).__name__)
        return {"completed_at": completed_at, "node_trace": [entry]}
    except Exception as e:  # noqa: BLE001
        logger.error("persistence failed for document_id=%s: %s", state.get("document_id"), type(e).__name__)
        return timer.failed(e)
