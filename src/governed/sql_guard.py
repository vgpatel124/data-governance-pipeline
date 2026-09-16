"""AST-based SQL guardrail (sqlglot, not regex).

Only read-only SELECT/UNION statements over an explicit allow-list of governed
tables are permitted. Every rejection returns a short, value-free reason.
"""
import logging
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

DIALECT = "duckdb"

# Statement types that must never run, mapped to the reason shown to the caller.
FORBIDDEN_NODES = {
    exp.Drop: "DROP is not allowed",
    exp.Delete: "DELETE is not allowed",
    exp.Update: "UPDATE is not allowed",
    exp.Insert: "INSERT is not allowed",
    exp.Alter: "ALTER is not allowed",
    exp.TruncateTable: "TRUNCATE is not allowed",
    exp.Create: "CREATE is not allowed",
    exp.Attach: "ATTACH is not allowed",
    exp.Copy: "COPY is not allowed",
    exp.Pragma: "PRAGMA is not allowed",
    exp.Set: "SET is not allowed",
    exp.Command: "administrative statements are not allowed",
}
ALLOWED_TOP_LEVEL = (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Subquery)


@dataclass
class GuardResult:
    allowed: bool
    reason: str | None = None
    tables: tuple[str, ...] = ()

    @property
    def blocked_reason(self) -> str:
        return self.reason or ""


def _cte_names(statement: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}


def validate_sql(sql: str, allowed_tables: set[str]) -> GuardResult:
    """Parse `sql` and allow it only if it is a single read-only statement
    touching nothing outside `allowed_tables`."""
    if not sql or not sql.strip():
        return GuardResult(False, "empty query")

    try:
        statements = [s for s in sqlglot.parse(sql, dialect=DIALECT) if s is not None]
    except Exception:  # noqa: BLE001 — sqlglot raises several parse error types
        return GuardResult(False, "query could not be parsed as SQL")

    if not statements:
        return GuardResult(False, "empty query")
    if len(statements) > 1:
        return GuardResult(False, "multiple statements are not allowed")

    statement = statements[0]

    # Forbidden statement types, anywhere in the tree (top level or nested).
    for node_type, reason in FORBIDDEN_NODES.items():
        if isinstance(statement, node_type) or statement.find(node_type) is not None:
            return GuardResult(False, reason)

    if not isinstance(statement, ALLOWED_TOP_LEVEL):
        return GuardResult(False, f"only SELECT queries are allowed (got {type(statement).__name__.upper()})")

    allowed_lower = {t.lower() for t in allowed_tables}
    cte_names = _cte_names(statement)
    referenced: set[str] = set()

    for table in statement.find_all(exp.Table):
        name = table.name.lower()
        if not name:
            # Table functions such as read_csv_auto('/etc/passwd') parse with an empty name.
            return GuardResult(False, "table functions are not allowed")
        if table.catalog or table.db:
            return GuardResult(False, "schema- or catalog-qualified tables are not allowed")
        if name in cte_names:
            continue
        if name not in allowed_lower:
            return GuardResult(False, f"table '{name}' is not queryable")
        referenced.add(name)

    if not referenced:
        return GuardResult(False, "query must read from a governed table")

    return GuardResult(True, None, tuple(sorted(referenced)))


def enforce_row_limit(sql: str, row_limit: int) -> str:
    """Wrap the validated query so it can never return more than row_limit rows."""
    return f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS _governed_query LIMIT {int(row_limit)}"
