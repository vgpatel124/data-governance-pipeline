"""Governed analytical store (DuckDB).

A third database, separate from pipeline.db (business outcomes) and
langgraph_checkpoints.db (LangGraph execution state). Never merge them.

Write condition (deliberately strict):
    final_status == "completed" AND output_guardrail_passed is True

A document approved by a human *after* an output-guardrail failure keeps
output_guardrail_passed=False, so it is governed, logged and visible in the
dashboard — but never becomes queryable. Only governed/masked content is
written; raw PII never reaches this store.
"""
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb

from src.agents.quality import load_schemas
from src.config.settings import settings

logger = logging.getLogger(__name__)

TABLE_PREFIX = "governed_"
META_COLUMNS = ["document_id", "row_index", "ingested_at"]
EXTRA_COLUMN = "extra_json"
_write_lock = threading.Lock()


def table_name(dataset_type: str) -> str:
    return f"{TABLE_PREFIX}{dataset_type}"


def governed_tables() -> dict[str, str]:
    """dataset_type -> governed table name, derived from validation_schemas.yaml."""
    return {dt: table_name(dt) for dt in load_schemas()}


def table_columns(dataset_type: str) -> list[str]:
    schema = load_schemas().get(dataset_type) or {}
    return [*META_COLUMNS, *schema.get("required_fields", []), EXTRA_COLUMN]


@lru_cache(maxsize=8)
def _connect(db_path: str) -> duckdb.DuckDBPyConnection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(db_path)


def get_connection() -> duckdb.DuckDBPyConnection:
    """Single process-wide connection per path (DuckDB is single-writer)."""
    return _connect(str(Path(settings.DB_GOVERNED_PATH).resolve()))


def ensure_table(dataset_type: str) -> str:
    name = table_name(dataset_type)
    cols = table_columns(dataset_type)
    ddl = ", ".join(
        f'"{c}" INTEGER' if c == "row_index" else f'"{c}" VARCHAR' for c in cols
    )
    get_connection().execute(f'CREATE TABLE IF NOT EXISTS "{name}" ({ddl})')
    return name


def ensure_all_tables() -> None:
    for dataset_type in load_schemas():
        ensure_table(dataset_type)


def _cell(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)


def _rows_from_content(content: Any) -> list[dict]:
    if content is None:
        return []
    if isinstance(content, dict):
        return [content]
    if isinstance(content, list):
        return [r for r in content if isinstance(r, dict)]
    return []


def is_queryable(state: dict) -> bool:
    """The single authority on what may enter the governed store."""
    return state.get("final_status") == "completed" and state.get("output_guardrail_passed") is True


def write_governed_rows(state: dict) -> int:
    """Write governed rows for one document. Returns the number of rows written
    (0 when the document is not eligible). Idempotent per document_id."""
    if not is_queryable(state):
        return 0
    dataset_type = state.get("dataset_type")
    if dataset_type not in load_schemas():
        logger.warning("governed store: unknown dataset_type for document_id=%s", state.get("document_id"))
        return 0

    rows = _rows_from_content(state.get("final_content"))
    if not rows:
        return 0

    name = ensure_table(dataset_type)
    columns = table_columns(dataset_type)
    data_fields = [c for c in columns if c not in META_COLUMNS and c != EXTRA_COLUMN]
    document_id = state["document_id"]
    ingested_at = state.get("completed_at") or ""

    payload = []
    for i, row in enumerate(rows, start=1):
        extra = {k: v for k, v in row.items() if k not in data_fields}
        payload.append([
            document_id, i, ingested_at,
            *[_cell(row.get(f)) for f in data_fields],
            _cell(extra) if extra else None,
        ])

    placeholders = ", ".join(["?"] * len(columns))
    quoted = ", ".join(f'"{c}"' for c in columns)
    con = get_connection()
    with _write_lock:
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(f'DELETE FROM "{name}" WHERE document_id = ?', [document_id])
            con.executemany(f'INSERT INTO "{name}" ({quoted}) VALUES ({placeholders})', payload)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    logger.info("governed store: wrote %d row(s) for document_id=%s", len(payload), document_id)
    return len(payload)


def describe_table(dataset_type: str) -> str:
    """Schema text for the text-to-SQL prompt (structure only, no data)."""
    name = table_name(dataset_type)
    cols = ", ".join(f'"{c}"' for c in table_columns(dataset_type))
    return f"Table {name} (all columns are VARCHAR except row_index INTEGER): {cols}"


def run_query(sql: str, timeout_s: float | None = None) -> tuple[list[str], list[list]]:
    """Execute an already-validated, already-limited query with a timeout."""
    timeout_s = settings.GOVERNED_QUERY_TIMEOUT_S if timeout_s is None else timeout_s
    con = get_connection()

    def _run():
        cur = con.execute(sql)
        columns = [d[0] for d in cur.description or []]
        return columns, [list(r) for r in cur.fetchall()]

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeout:
            con.interrupt()
            raise TimeoutError(f"query exceeded {timeout_s}s and was cancelled") from None


def row_count(dataset_type: str) -> int:
    ensure_table(dataset_type)
    return get_connection().execute(f'SELECT count(*) FROM "{table_name(dataset_type)}"').fetchone()[0]
