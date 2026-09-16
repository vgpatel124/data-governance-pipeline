"""Async ingestion: /ingest returns 202 and the graph runs in a background task."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from sqlalchemy import select

import src.agents.extraction as extraction
import src.api.main as api_main
from src.api.main import create_app, execute_pipeline
from src.config.settings import settings
from src.graph.graph import compile_api_graph, compile_graph
from src.persistence.db import get_engine
from src.persistence.models import PipelineRun
from src.persistence.writer import write_queued_run

SYN = Path(__file__).resolve().parents[1] / "data" / "synthetic"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "pipeline.db"))
    monkeypatch.setattr(settings, "DB_CHECKPOINT_PATH", str(tmp_path / "checkpoints.db"))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    return tmp_path


@pytest.fixture
def client():
    with TestClient(create_app(graph=compile_graph(checkpointer=MemorySaver()))) as c:
        yield c


def _post(client, fixture, dataset_type, name=None):
    return client.post("/ingest",
                       files={"file": (name or fixture, (SYN / fixture).read_bytes())},
                       data={"needs_annotation": "false", "dataset_type": dataset_type})


def _await_run(client, document_id, timeout_s=60.0):
    deadline = time.time() + timeout_s
    seen = []
    while time.time() < deadline:
        row = client.get(f"/status/{document_id}").json()
        seen.append(row.get("run_status"))
        if row.get("run_status") in ("paused", "done"):
            return row, seen
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def _row(document_id):
    with get_engine().connect() as conn:
        return conn.execute(select(PipelineRun).where(PipelineRun.document_id == document_id)).mappings().first()


# ---------- 202 + immediate row ----------

def test_ingest_returns_202_with_document_id(client):
    r = _post(client, "customers_clean.csv", "customer_records")
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued" and body["run_status"] == "queued"
    assert body["document_id"] and body["poll"] == f"/status/{body['document_id']}"
    # the acknowledgement carries no run outcome — that is what polling is for
    assert "final_status" not in body and "pii_findings_count" not in body


def test_row_is_queryable_from_the_moment_of_ingestion():
    """A row exists with run_status=queued and started_at before the graph runs."""
    write_queued_run("doc-queued", "x.csv", "customer_records", False, "2026-09-16T10:00:00")
    row = _row("doc-queued")
    assert row is not None
    assert row["run_status"] == "queued"
    assert row["started_at"] == "2026-09-16T10:00:00"
    assert row["final_status"] is None          # not terminal yet
    assert row["dataset_type"] == "customer_records"


def test_status_endpoint_exposes_queued_row(client):
    write_queued_run("doc-1", "x.csv", "customer_records", False, "2026-09-16T10:00:00")
    body = client.get("/status/doc-1").json()
    assert body["run_status"] == "queued" and body["final_status"] is None


# ---------- run_status lifecycle ----------

def test_run_status_transitions_queued_running_done():
    """execute_pipeline reports 'running' while the graph is executing and
    'done' once the terminal write lands."""
    observed = {}
    write_queued_run("doc-2", "x.csv", "customer_records", False, "2026-09-16T10:00:00")
    assert _row("doc-2")["run_status"] == "queued"

    class StubGraph:
        def invoke(self, state, config=None):
            observed["during_run"] = _row("doc-2")["run_status"]
            return {**state, "final_status": "completed", "output_guardrail_passed": True,
                    "final_content": [], "completed_at": "2026-09-16T10:00:01"}

    out = execute_pipeline(StubGraph(), "doc-2", "/tmp/x.csv", "x.csv", False, "customer_records",
                           "2026-09-16T10:00:00")
    assert observed["during_run"] == "running"
    assert out["final_status"] == "completed"


def test_polling_sees_running_then_terminal():
    """A slow run is observable as 'running' through /status while it executes."""
    release = threading.Event()
    started = threading.Event()
    write_queued_run("doc-slow", "x.csv", "customer_records", False, "2026-09-16T10:00:00")

    class SlowGraph:
        def invoke(self, state, config=None):
            started.set()
            release.wait(timeout=10)
            return {**state, "final_status": "completed", "output_guardrail_passed": True,
                    "final_content": [], "completed_at": "2026-09-16T10:00:05"}

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_pipeline, SlowGraph(), "doc-slow", "/tmp/x.csv", "x.csv",
                             False, "customer_records", "2026-09-16T10:00:00")
        assert started.wait(timeout=10)
        assert _row("doc-slow")["run_status"] == "running"   # visible mid-flight
        release.set()
        future.result(timeout=30)
    assert _row("doc-slow")["run_status"] == "done"


def test_background_infrastructure_failure_is_recorded(client):
    class BrokenGraph:
        def invoke(self, state, config=None):
            raise RuntimeError("checkpointer unavailable")

    write_queued_run("doc-3", "x.csv", "customer_records", False, "2026-09-16T10:00:00")
    out = execute_pipeline(BrokenGraph(), "doc-3", "/tmp/x.csv", "x.csv", False, "customer_records",
                           "2026-09-16T10:00:00")
    assert out["status"] == "failed"
    row = _row("doc-3")
    assert row["final_status"] == "failed" and row["run_status"] == "done"
    assert "RuntimeError" in row["error_message"]
    assert row["completed_at"]


# ---------- interrupt/resume still works from a background task ----------

def test_interrupt_in_background_still_pauses_and_resumes(client):
    """CRITICAL REGRESSION TEST: the graph now runs off the request thread, so the
    checkpointer must still pause at manual_review and resume via /review."""
    r = _post(client, "customers_invalid.csv", "customer_records")
    assert r.status_code == 202
    doc_id = r.json()["document_id"]

    row, _ = _await_run(client, doc_id)
    assert row["run_status"] == "paused"
    assert row["review_status"] == "pending"
    assert "row 5: email field missing" in row["review_issue_summary"]
    assert [p["document_id"] for p in client.get("/reviews/pending").json()] == [doc_id]

    rr = client.post(f"/review/{doc_id}", json={"decision": "approve", "notes": "ok from async run"})
    assert rr.status_code == 200
    assert rr.json()["final_status"] == "completed"
    final = client.get(f"/status/{doc_id}").json()
    assert final["final_status"] == "completed"
    assert final["run_status"] == "done"
    assert final["review_status"] == "resolved"
    assert final["human_reviewer_notes"] == "ok from async run"


def test_interrupt_resume_across_app_instances_with_sqlite_checkpointer(isolated):
    """Background execution + SqliteSaver: ingest in one app, resume in another."""
    with TestClient(create_app(graph=compile_api_graph())) as c1:
        doc_id = _post(c1, "customers_invalid.csv", "customer_records").json()["document_id"]
        row, _ = _await_run(c1, doc_id)
        assert row["run_status"] == "paused"
    with TestClient(create_app(graph=compile_api_graph())) as c2:
        rr = c2.post(f"/review/{doc_id}", json={"decision": "approve", "notes": "after restart"})
        assert rr.status_code == 200 and rr.json()["final_status"] == "completed"


# ---------- concurrency ----------

def test_concurrent_runs_do_not_corrupt_or_lock_the_db(client):
    """Several background runs write pipeline.db (and the governed store) at once."""
    graph = compile_graph(checkpointer=MemorySaver())
    n = 8
    ids = [f"doc-conc-{i}" for i in range(n)]
    for doc_id in ids:
        write_queued_run(doc_id, "customers_clean.csv", "customer_records", False, "2026-09-16T10:00:00")

    def run(doc_id):
        return execute_pipeline(graph, doc_id, str(SYN / "customers_clean.csv"), "customers_clean.csv",
                                False, "customer_records", "2026-09-16T10:00:00")

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = [f.result(timeout=120) for f in [pool.submit(run, d) for d in ids]]

    assert all(r["final_status"] == "completed" for r in results)
    with get_engine().connect() as conn:
        rows = conn.execute(select(PipelineRun).where(PipelineRun.document_id.in_(ids))).mappings().all()
    assert len(rows) == n
    assert all(r["run_status"] == "done" and r["final_status"] == "completed" for r in rows)
    assert all(r["pii_findings_count"] == 60 for r in rows)

    # governed store received every document exactly once (20 rows each)
    from src.governed import store
    assert store.row_count("customer_records") == 20 * n


def test_concurrent_ingests_through_the_api(client):
    posts = [_post(client, "customers_clean.csv", "customer_records") for _ in range(5)]
    assert all(r.status_code == 202 for r in posts)
    ids = [r.json()["document_id"] for r in posts]
    assert len(set(ids)) == 5
    for doc_id in ids:
        row, _ = _await_run(client, doc_id)
        assert row["final_status"] == "completed"
