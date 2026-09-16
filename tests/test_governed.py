"""Governed analytical store + SQL guardrail."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

import src.agents.extraction as extraction
from src.config.settings import settings
from src.governed import store
from src.governed.sql_guard import enforce_row_limit, validate_sql
from src.graph.graph import compile_graph
from src.graph.state import build_initial_state

SYN = Path(__file__).resolve().parents[1] / "data" / "synthetic"
ALLOWED = {"governed_customer_records"}


@pytest.fixture(autouse=True)
def tmp_pipeline_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "pipeline.db"))


def _state(**overrides):
    s = build_initial_state("/tmp/x.csv", "x.csv", False, "product_catalog")
    s.update({
        "data_type": "structured",
        "final_status": "completed",
        "output_guardrail_passed": True,
        "completed_at": "2026-09-16T10:00:00",
        "final_content": [
            {"product_id": "P001", "product_name": "Mouse", "category": "Electronics", "price": "25.99", "status": "Active"},
            {"product_id": "SKU-12345", "product_name": "Desk", "category": "Furniture", "price": "349.00", "status": "Active"},
        ],
    })
    s.update(overrides)
    return s


# ---------- AST guardrail ----------

@pytest.mark.parametrize("sql, expected_reason", [
    ("DROP TABLE governed_customer_records", "DROP is not allowed"),
    ("DELETE FROM governed_customer_records", "DELETE is not allowed"),
    ("UPDATE governed_customer_records SET name = 'x'", "UPDATE is not allowed"),
    ("INSERT INTO governed_customer_records VALUES ('a')", "INSERT is not allowed"),
    ("ALTER TABLE governed_customer_records ADD COLUMN x VARCHAR", "ALTER is not allowed"),
    ("TRUNCATE TABLE governed_customer_records", "TRUNCATE is not allowed"),
    ("SELECT * FROM governed_customer_records; DROP TABLE governed_customer_records", "multiple statements are not allowed"),
    ("SELECT * FROM governed_customer_records UNION SELECT * FROM pipeline_runs", "table 'pipeline_runs' is not queryable"),
    ("SELECT * FROM pipeline_runs", "table 'pipeline_runs' is not queryable"),
    ("SELECT * FROM pipeline_events", "table 'pipeline_events' is not queryable"),
    ("SELECT * FROM checkpoints", "table 'checkpoints' is not queryable"),
    ("SELECT * FROM read_csv_auto('/etc/passwd')", "table functions are not allowed"),
    ("ATTACH 'other.db' AS other", "ATTACH is not allowed"),
    ("PRAGMA database_list", "PRAGMA is not allowed"),
    ("COPY governed_customer_records TO '/tmp/x.csv'", "COPY is not allowed"),
    ("CREATE TABLE evil AS SELECT * FROM governed_customer_records", "CREATE is not allowed"),
    ("SELECT 1", "query must read from a governed table"),
    ("not sql at all !!", None),
    ("", "empty query"),
])
def test_blocked_sql(sql, expected_reason):
    result = validate_sql(sql, ALLOWED)
    assert result.allowed is False
    if expected_reason:
        assert result.reason == expected_reason
    assert result.reason  # always a value-free reason


@pytest.mark.parametrize("sql", [
    "SELECT * FROM governed_customer_records",
    "SELECT customer_id, email FROM governed_customer_records WHERE email LIKE '%@example.com'",
    "SELECT count(*) AS n FROM governed_customer_records",
    "WITH recent AS (SELECT * FROM governed_customer_records) SELECT * FROM recent LIMIT 5",
    "SELECT * FROM GOVERNED_CUSTOMER_RECORDS",
])
def test_allowed_sql(sql):
    result = validate_sql(sql, ALLOWED)
    assert result.allowed is True, result.reason
    assert result.tables == ("governed_customer_records",)


def test_row_limit_wrapper_caps_results():
    sql = enforce_row_limit("SELECT * FROM governed_product_catalog", 500)
    assert sql.endswith("LIMIT 500")
    assert validate_sql(sql, {"governed_product_catalog"}).allowed is True


# ---------- write condition ----------

def test_completed_and_guardrail_passed_is_written():
    assert store.write_governed_rows(_state()) == 2
    assert store.row_count("product_catalog") == 2


def test_human_overridden_guardrail_failure_is_not_queryable():
    # Approve-from-guardrail-failure: final_status becomes "completed" but
    # output_guardrail_passed stays False. It must never become queryable.
    written = store.write_governed_rows(_state(output_guardrail_passed=False,
                                               human_reviewer_notes="approved by reviewer"))
    assert written == 0
    assert store.row_count("product_catalog") == 0


@pytest.mark.parametrize("overrides", [
    {"final_status": "manual_review"},
    {"final_status": "failed", "output_guardrail_passed": None},
    {"final_status": "rejected"},
    {"final_status": None},
    {"output_guardrail_passed": None},
])
def test_non_completed_documents_are_not_written(overrides):
    assert store.write_governed_rows(_state(**overrides)) == 0
    assert store.row_count("product_catalog") == 0


def test_write_is_idempotent_per_document():
    s = _state()
    store.write_governed_rows(s)
    store.write_governed_rows(s)
    assert store.row_count("product_catalog") == 2


def test_unstructured_dict_content_writes_one_row():
    s = _state(dataset_type="documents", data_type="unstructured",
               final_content={"title": "T", "date": "2026-01-01", "amount": "10", "parties": ["Acme", "Beta"]})
    assert store.write_governed_rows(s) == 1
    cols, rows = store.run_query('SELECT parties FROM governed_documents')
    assert json.loads(rows[0][0]) == ["Acme", "Beta"]  # list values stored as JSON


def test_extra_columns_land_in_extra_json():
    store.write_governed_rows(_state())
    cols, rows = store.run_query('SELECT extra_json FROM governed_product_catalog ORDER BY row_index')
    assert json.loads(rows[0][0]) == {"status": "Active"}


def test_legitimate_select_returns_correct_rows():
    store.write_governed_rows(_state())
    cols, rows = store.run_query(
        'SELECT product_id, price FROM governed_product_catalog ORDER BY row_index')
    assert cols == ["product_id", "price"]
    assert rows == [["P001", "25.99"], ["SKU-12345", "349.00"]]


# ---------- end-to-end: no raw PII in the governed store ----------

def test_no_raw_pii_in_governed_store(monkeypatch):
    graph = compile_graph(checkpointer=MemorySaver())
    s = build_initial_state(str(SYN / "customers_clean.csv"), "customers_clean.csv", False, "customer_records")
    out = graph.invoke(s, config={"configurable": {"thread_id": s["document_id"]}})
    assert out["final_status"] == "completed" and out["output_guardrail_passed"] is True

    cols, rows = store.run_query("SELECT * FROM governed_customer_records")
    assert len(rows) == 20
    dump = json.dumps(rows)
    for record in out["structured_records"]:
        assert record["email"] not in dump          # raw email never stored
        assert record["pan"] not in dump            # raw PAN never stored
        assert record["phone"] not in dump          # raw phone never stored
    assert "[REDACTED_PAN]" in dump                 # governed content is what landed
    assert "****@" in dump                          # emails are masked in place


def test_guardrail_failure_document_never_reaches_store(monkeypatch, tmp_path):
    # A real output-guardrail failure (forbidden 'password' column), approved by a human.
    csv = tmp_path / "accounts.csv"
    csv.write_text("product_id,product_name,category,price,password\nP001,Lamp,home,9.99,hunter2\n")
    graph = compile_graph(checkpointer=MemorySaver())
    s = build_initial_state(str(csv), csv.name, False, "product_catalog")
    config = {"configurable": {"thread_id": s["document_id"]}}
    first = graph.invoke(s, config=config)
    assert "__interrupt__" in first

    out = graph.invoke(Command(resume={"decision": "approve", "notes": "internal"}), config=config)
    assert out["final_status"] == "completed"
    assert out["output_guardrail_passed"] is False
    assert store.row_count("product_catalog") == 0
    cols, rows = store.run_query("SELECT * FROM governed_product_catalog")
    assert rows == []
    assert "hunter2" not in json.dumps(rows)
