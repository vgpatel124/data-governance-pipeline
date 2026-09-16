"""FastAPI endpoint tests. LLMs and partition are mocked; graph, guardrails,
persistence, and checkpointing run for real."""
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

import src.agents.extraction as extraction
from src.api.main import create_app
from src.config.settings import settings
from src.graph.graph import compile_api_graph, compile_graph

SYN = Path(__file__).resolve().parents[1] / "data" / "synthetic"
GOOD = {"title": "Press Release", "date": "2025-10-05", "amount": 100, "parties": ["Acme", "jane.roe@example.com"]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "pipeline.db"))
    monkeypatch.setattr(settings, "DB_CHECKPOINT_PATH", str(tmp_path / "checkpoints.db"))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    c = MagicMock()
    c.invoke.side_effect = lambda prompt: SimpleNamespace(content=json.dumps(GOOD))
    monkeypatch.setattr(extraction, "get_local_client", lambda: c)
    monkeypatch.setattr(extraction, "get_external_client", lambda: c)
    monkeypatch.setattr(extraction, "partition", lambda filename, strategy: ["Press Release text"])
    return tmp_path


@pytest.fixture
def client():
    with TestClient(create_app(graph=compile_graph(checkpointer=MemorySaver()))) as c:
        yield c


def _ingest(client, fixture, dataset_type, needs_annotation=False, content=None, name=None):
    """POST /ingest — returns the 202 acknowledgement, not the run outcome."""
    data = content if content is not None else (SYN / fixture).read_bytes()
    return client.post(
        "/ingest",
        files={"file": (name or fixture, data)},
        data={"needs_annotation": str(needs_annotation).lower(), "dataset_type": dataset_type},
    )


def _await_run(client, document_id, timeout_s=60.0):
    """Poll /status until the background run leaves queued/running."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        row = client.get(f"/status/{document_id}").json()
        if row.get("run_status") in ("paused", "done"):
            return row
        time.sleep(0.05)
    raise AssertionError(f"run did not finish within {timeout_s}s")


def _ingest_and_wait(client, fixture, dataset_type, needs_annotation=False, content=None, name=None):
    r = _ingest(client, fixture, dataset_type, needs_annotation, content, name)
    assert r.status_code == 202, r.text
    return r.json()["document_id"], _await_run(client, r.json()["document_id"])


def test_ingest_completed_returns_summary_without_content(client, isolated):
    r = _ingest(client, "customers_clean.csv", "customer_records")
    assert r.status_code == 202
    ack = r.json()
    assert ack["status"] == "queued"
    doc_id = ack["document_id"]
    for forbidden in ("raw_text", "extracted_data", "structured_records", "final_content", "pii_findings"):
        assert forbidden not in ack

    row = _await_run(client, doc_id)
    assert row["final_status"] == "completed"
    assert row["pii_findings_count"] == 60          # 20 each of email/phone/pan
    assert {f["entity_type"] for f in row["pii_findings_summary"]} == {"email", "phone", "pan"}
    status_text = client.get(f"/status/{doc_id}").text
    raw_emails = [line.split(",")[2] for line in (SYN / "customers_clean.csv").read_text().splitlines()[1:]]
    assert not any(e in status_text for e in raw_emails)
    assert (isolated / "uploads" / doc_id / "customers_clean.csv").exists()


def test_ingest_interrupt_pending_then_approve(client):
    doc_id, row = _ingest_and_wait(client, "customers_invalid.csv", "customer_records")
    assert row["run_status"] == "paused"
    assert row["review_status"] == "pending"
    assert "row 5: email field missing" in row["review_issue_summary"]

    pending = client.get("/reviews/pending").json()
    assert [p["document_id"] for p in pending] == [doc_id]
    assert pending[0]["review_issue_summary"] == row["review_issue_summary"]
    assert client.get("/dashboard-data").json()["pending_review_count"] == 1

    rr = client.post(f"/review/{doc_id}", json={"decision": "approve", "notes": "ok"})
    assert rr.status_code == 200
    assert rr.json()["final_status"] == "completed"
    assert rr.json()["human_reviewer_notes"] == "ok"
    assert client.get("/reviews/pending").json() == []
    row = client.get(f"/status/{doc_id}").json()
    assert row["review_status"] == "resolved" and row["final_status"] == "completed"

    # can't review twice
    assert client.post(f"/review/{doc_id}", json={"decision": "approve"}).status_code == 409


def test_review_reject(client):
    doc_id, _ = _ingest_and_wait(client, "customers_invalid.csv", "customer_records")
    rr = client.post(f"/review/{doc_id}", json={"decision": "reject", "notes": "bad"})
    assert rr.json()["final_status"] == "manual_review"
    assert client.get(f"/status/{doc_id}").json()["human_reviewer_notes"] == "bad"


def test_review_validation_and_unknown_document(client):
    assert client.post("/review/does-not-exist", json={"decision": "approve"}).status_code == 404
    doc_id, _ = _ingest_and_wait(client, "customers_invalid.csv", "customer_records")
    assert client.post(f"/review/{doc_id}", json={"decision": "maybe"}).status_code == 422
    completed, _ = _ingest_and_wait(client, "customers_clean.csv", "customer_records")
    assert client.post(f"/review/{completed}", json={"decision": "approve"}).status_code == 409


def test_hard_terminal_is_not_pending(client):
    _, row = _ingest_and_wait(client, "products_injection.csv", "product_catalog")
    assert row["final_status"] == "manual_review"
    assert client.get("/reviews/pending").json() == []


def test_ingest_rejects_unknown_dataset_type_and_sanitizes_filename(client, isolated):
    assert _ingest(client, "customers_clean.csv", "nope").status_code == 400
    doc_id, _ = _ingest_and_wait(client, "customers_clean.csv", "customer_records", name="../../evil\x00.csv")
    saved = list((isolated / "uploads" / doc_id).iterdir())
    assert [p.name for p in saved] == ["evil.csv"]


def test_status_404(client):
    assert client.get("/status/nope").status_code == 404


def test_dashboard_data_aggregates(client, monkeypatch):
    _ingest_and_wait(client, "customers_clean.csv", "customer_records")          # completed
    _ingest_and_wait(client, "products_injection.csv", "product_catalog")        # manual_review
    _ingest_and_wait(client, "customers_invalid.csv", "customer_records")        # pending
    boom = MagicMock()
    boom.invoke.side_effect = ConnectionError("down")
    monkeypatch.setattr(extraction, "get_external_client", lambda: boom)
    _ingest_and_wait(client, "public_press_release.pdf", "documents")            # failed

    d = client.get("/dashboard-data").json()
    assert d["total_processed"] == 3
    assert d["status_breakdown"] == {"completed": 1, "manual_review": 1, "rejected": 0, "failed": 1}
    assert d["manual_review_rate"] == round(1 / 3, 4)
    assert d["pending_review_count"] == 1
    assert d["retry_rate"] == 0.0
    assert d["avg_processing_time_ms"] is not None
    assert d["pii_findings_by_type"]["email"] == 20
    runs = client.get("/runs").json()
    assert len(runs) == 4


def test_sqlite_checkpointer_resume_across_app_instances(isolated):
    """Ingest and review happen in separate app instances (e.g. a restart between
    requests) sharing only the on-disk SqliteSaver checkpoint DB."""
    with TestClient(create_app(graph=compile_api_graph())) as c1:
        doc_id, _ = _ingest_and_wait(c1, "customers_invalid.csv", "customer_records")
    assert (isolated / "checkpoints.db").exists()
    with TestClient(create_app(graph=compile_api_graph())) as c2:
        assert [p["document_id"] for p in c2.get("/reviews/pending").json()] == [doc_id]
        rr = c2.post(f"/review/{doc_id}", json={"decision": "approve", "notes": "after restart"})
        assert rr.status_code == 200 and rr.json()["final_status"] == "completed"


def test_default_app_compiles_sqlite_graph_on_startup(isolated):
    app = create_app()
    with TestClient(app) as c:
        assert c.get("/health").json() == {"status": "ok"}
        assert app.state.graph is not None
