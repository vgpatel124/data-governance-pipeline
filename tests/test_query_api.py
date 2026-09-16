"""/query endpoint: text-to-SQL with the AST guardrail in front of execution."""
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

import src.api.main as api_main
import src.agents.extraction as extraction
from src.api.main import create_app
from src.config.settings import settings
from src.graph.graph import compile_graph

SYN = Path(__file__).resolve().parents[1] / "data" / "synthetic"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "pipeline.db"))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")


@pytest.fixture
def client():
    with TestClient(create_app(graph=compile_graph(checkpointer=MemorySaver()))) as c:
        yield c


@pytest.fixture
def sql_of(monkeypatch):
    """Force the generated SQL, bypassing the LLM."""
    def _set(sql, model_used="external"):
        monkeypatch.setattr(api_main, "generate_sql", lambda q, dt, sensitive=False: (sql, model_used))
    return _set


def _ingest_customers(client):
    r = client.post("/ingest", files={"file": ("customers_clean.csv", (SYN / "customers_clean.csv").read_bytes())},
                    data={"needs_annotation": "false", "dataset_type": "customer_records"})
    assert r.status_code == 202
    document_id = r.json()["document_id"]
    deadline = time.time() + 60
    while time.time() < deadline:
        row = client.get(f"/status/{document_id}").json()
        if row.get("run_status") in ("paused", "done"):
            assert row["final_status"] == "completed"
            return document_id
        time.sleep(0.05)
    raise AssertionError("ingest did not finish")


def _query(client, question="how many customers?", dataset_type="customer_records"):
    return client.post("/query", json={"question": question, "dataset_type": dataset_type})


def test_legitimate_select_returns_rows(client, sql_of):
    _ingest_customers(client)
    sql_of("SELECT customer_id, email FROM governed_customer_records ORDER BY row_index")
    r = _query(client)
    assert r.status_code == 200
    body = r.json()
    assert body["blocked"] is False and body["reason"] is None
    assert body["columns"] == ["customer_id", "email"]
    assert body["row_count"] == 20
    assert body["table"] == "governed_customer_records"
    assert body["sql"].startswith("SELECT customer_id")   # generated SQL is returned for display
    assert all("****@" in row[1] for row in body["rows"])  # governed values only


def test_aggregate_query(client, sql_of):
    _ingest_customers(client)
    sql_of("SELECT count(*) AS total FROM governed_customer_records")
    body = _query(client).json()
    assert body["blocked"] is False
    assert body["rows"] == [[20]]


@pytest.mark.parametrize("sql, reason", [
    ("DROP TABLE governed_customer_records", "DROP is not allowed"),
    ("DELETE FROM governed_customer_records", "DELETE is not allowed"),
    ("UPDATE governed_customer_records SET email = 'x'", "UPDATE is not allowed"),
    ("SELECT 1; DROP TABLE governed_customer_records", "multiple statements are not allowed"),
    ("SELECT * FROM governed_customer_records UNION SELECT * FROM pipeline_runs", "table 'pipeline_runs' is not queryable"),
    ("SELECT * FROM pipeline_runs", "table 'pipeline_runs' is not queryable"),
    ("SELECT * FROM pipeline_events", "table 'pipeline_events' is not queryable"),
    ("SELECT * FROM read_csv_auto('/etc/passwd')", "table functions are not allowed"),
])
def test_blocked_queries_are_refused_with_a_reason(client, sql_of, sql, reason):
    _ingest_customers(client)
    sql_of(sql)
    body = _query(client).json()
    assert body["blocked"] is True
    assert body["reason"] == reason
    assert body["rows"] == [] and body["row_count"] == 0
    assert "Traceback" not in json.dumps(body)


def test_cross_dataset_table_is_blocked(client, sql_of):
    # Only the requested dataset_type's table is queryable.
    sql_of("SELECT * FROM governed_product_catalog")
    body = _query(client, dataset_type="customer_records").json()
    assert body["blocked"] is True
    assert body["reason"] == "table 'governed_product_catalog' is not queryable"


def test_row_limit_is_enforced(client, sql_of, monkeypatch):
    _ingest_customers(client)
    monkeypatch.setattr(settings, "GOVERNED_ROW_LIMIT", 5)
    sql_of("SELECT * FROM governed_customer_records")
    body = _query(client).json()
    assert body["row_count"] == 5 and body["row_limit"] == 5


def test_unknown_dataset_type_and_empty_question(client, sql_of):
    sql_of("SELECT * FROM governed_customer_records")
    assert client.post("/query", json={"question": "x", "dataset_type": "nope"}).status_code == 400
    assert client.post("/query", json={"question": "   ", "dataset_type": "customer_records"}).status_code == 400


def test_sql_generation_failure_returns_502(client, monkeypatch):
    def boom(question, dataset_type, sensitive=False):
        raise RuntimeError("GROQ_API_KEY is not set")
    monkeypatch.setattr(api_main, "generate_sql", boom)
    r = _query(client)
    assert r.status_code == 502
    assert "RuntimeError" in r.json()["detail"]


def test_invalid_column_is_reported_not_crashed(client, sql_of):
    _ingest_customers(client)
    sql_of("SELECT no_such_column FROM governed_customer_records")
    body = _query(client).json()
    assert body["blocked"] is True
    assert "could not be executed" in body["reason"]


def test_governed_tables_endpoint(client):
    _ingest_customers(client)
    tables = {t["dataset_type"]: t for t in client.get("/governed/tables").json()}
    assert tables["customer_records"]["table"] == "governed_customer_records"
    assert tables["customer_records"]["row_count"] == 20
    assert tables["product_catalog"]["row_count"] == 0
    assert "email" in tables["customer_records"]["columns"]


def test_text_to_sql_prompt_uses_governed_schema(monkeypatch):
    import src.governed.text_to_sql as t2s
    client = MagicMock()
    client.invoke.return_value = SimpleNamespace(content="```sql\nSELECT * FROM governed_product_catalog;\n```")
    monkeypatch.setattr(t2s, "get_external_client", lambda: client)
    sql, model_used = t2s.generate_sql("show everything", "product_catalog")
    prompt = client.invoke.call_args[0][0]
    assert "governed_product_catalog" in prompt and "product_name" in prompt
    assert "<question>" in prompt          # question is delimited as data
    assert sql == "SELECT * FROM governed_product_catalog"   # fences and semicolon stripped
    assert model_used == "external"


def test_sensitive_flag_routes_to_local_model(monkeypatch):
    import src.governed.text_to_sql as t2s
    local, external = MagicMock(), MagicMock()
    local.invoke.return_value = SimpleNamespace(content="SELECT 1 FROM governed_documents")
    monkeypatch.setattr(t2s, "get_local_client", lambda: local)
    monkeypatch.setattr(t2s, "get_external_client", lambda: external)
    _, model_used = t2s.generate_sql("q", "documents", sensitive=True)
    assert model_used == "local"
    external.invoke.assert_not_called()
