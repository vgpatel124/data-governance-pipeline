import operator
import typing
from datetime import datetime
from uuid import UUID

import yaml

from src.config.settings import settings
from src.graph.state import PipelineState, NodeTimer, build_initial_state


def test_initial_state_defaults():
    s = build_initial_state("/tmp/x.csv", "x.csv", needs_annotation=True, dataset_type="customer_records")
    UUID(s["document_id"])  # valid uuid4 string
    assert s["retry_count"] == 0
    assert s["max_retries"] == int(settings.MAX_RETRIES)
    assert s["quality_issues"] == []
    assert s["pii_findings"] == []
    assert s["policy_violations"] == []
    assert s["node_trace"] == []
    assert s["human_reviewer_notes"] is None
    assert s["final_status"] is None
    datetime.fromisoformat(s["started_at"])


def test_caller_supplied_fields_are_passed_through():
    s = build_initial_state("/tmp/x.pdf", "x.pdf", needs_annotation=False, dataset_type="documents")
    assert s["needs_annotation"] is False
    assert s["dataset_type"] == "documents"


def test_initial_state_has_every_schema_key():
    s = build_initial_state("/tmp/x.csv", "x.csv", False, "customer_records")
    assert set(s.keys()) == set(PipelineState.__annotations__.keys())


def test_node_trace_uses_add_reducer():
    hints = typing.get_type_hints(PipelineState, include_extras=True)
    assert operator.add in hints["node_trace"].__metadata__


def test_node_timer_failed_shape():
    out = NodeTimer("x").failed(ValueError("boom"))
    assert set(out) == {"final_status", "error_message", "node_trace"}
    assert out["final_status"] == "failed"
    assert out["node_trace"][0]["status"] == "failed"


def test_validation_schemas_have_required_fields():
    with open(settings.VALIDATION_SCHEMAS_PATH) as f:
        schemas = yaml.safe_load(f)
    assert any(3 <= len(v["required_fields"]) <= 4 for v in schemas.values())
