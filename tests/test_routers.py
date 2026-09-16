import pytest

from src.graph import routers as r

FAILED = {"final_status": "failed"}


# ---------- failure short-circuit at every applicable router ----------

@pytest.mark.parametrize(
    "router, state",
    [
        (r.route_after_input_guardrail, {**FAILED, "input_valid": True}),
        (r.route_by_data_type, {**FAILED, "data_type": "unstructured"}),
        (r.route_after_extraction_parse, FAILED),
        (r.route_after_injection_check, {**FAILED, "parsed_injection_flag": True}),
        (r.route_after_extraction_structure, FAILED),
        (r.route_after_quality, {**FAILED, "quality_status": "FAIL", "data_type": "structured"}),
        (r.route_after_annotation, FAILED),
        (r.route_after_governance, FAILED),
        (r.route_after_output_guardrail, {**FAILED, "output_guardrail_passed": False}),
    ],
)
def test_failed_short_circuits_to_persistence(router, state):
    assert router(state) == "persistence"


# ---------- normal branches ----------

def test_route_after_input_guardrail():
    assert r.route_after_input_guardrail({"input_valid": True}) == "discovery"
    assert r.route_after_input_guardrail({"input_valid": False}) == "manual_review"


def test_route_by_data_type():
    assert r.route_by_data_type({"data_type": "unstructured"}) == "extraction_parse"
    assert r.route_by_data_type({"data_type": "structured"}) == "quality"


def test_route_after_extraction_parse():
    assert r.route_after_extraction_parse({}) == "injection_check_parsed"


def test_route_after_injection_check():
    assert r.route_after_injection_check({"parsed_injection_flag": True}) == "manual_review"
    assert r.route_after_injection_check({"parsed_injection_flag": False}) == "extraction_structure"


def test_route_after_extraction_structure():
    assert r.route_after_extraction_structure({}) == "quality"


def test_route_after_quality():
    assert r.route_after_quality({"quality_status": "FAIL", "data_type": "unstructured"}) == "retry"
    assert r.route_after_quality({"quality_status": "FAIL", "data_type": "structured"}) == "manual_review"
    assert r.route_after_quality({"quality_status": "PASS", "needs_annotation": True}) == "annotation"
    assert r.route_after_quality({"quality_status": "PASS", "needs_annotation": False}) == "governance"


def test_route_after_retry_uses_less_than_or_equal():
    assert r.route_after_retry({"retry_count": 0, "max_retries": 1}) == "extraction_parse"
    assert r.route_after_retry({"retry_count": 1, "max_retries": 1}) == "extraction_parse"  # boundary: <=
    assert r.route_after_retry({"retry_count": 2, "max_retries": 1}) == "manual_review"
    assert r.route_after_retry({"retry_count": 1, "max_retries": 0}) == "manual_review"


def test_route_after_annotation():
    assert r.route_after_annotation({}) == "governance"


def test_route_after_governance():
    assert r.route_after_governance({}) == "output_guardrail"


def test_route_after_output_guardrail():
    assert r.route_after_output_guardrail({"output_guardrail_passed": True, "final_status": "completed"}) == "persistence"
    assert r.route_after_output_guardrail({"output_guardrail_passed": False}) == "manual_review"


# ---------- route_after_manual_review: all three branches ----------

def test_manual_review_reject_or_hard_terminal_goes_to_persistence():
    assert r.route_after_manual_review({"final_status": "manual_review", "quality_status": "FAIL"}) == "persistence"
    # hard-terminal: input rejection (quality never ran)
    assert r.route_after_manual_review({"final_status": "manual_review", "input_valid": False}) == "persistence"
    # reject after a quality failure that was *not* overridden
    assert r.route_after_manual_review(
        {"final_status": "manual_review", "quality_status": "FAIL", "needs_annotation": True}
    ) == "persistence"


def test_manual_review_approve_from_quality_routes_like_pass():
    assert r.route_after_manual_review({"final_status": None, "quality_status": "PASS", "needs_annotation": True}) == "annotation"
    assert r.route_after_manual_review({"final_status": None, "quality_status": "PASS", "needs_annotation": False}) == "governance"


def test_manual_review_approve_from_guardrail_goes_to_persistence():
    # Approve from output guardrail: final_status="completed", guardrail flag stays False.
    state = {"final_status": "completed", "output_guardrail_passed": False, "quality_status": "PASS", "needs_annotation": True}
    # quality_status is already PASS here (quality passed before governance ran), so the
    # final_status=="completed" check must win — see PROGRESS.md Stage 4 deviation.
    assert r.route_after_manual_review(state) == "persistence"
    assert r.route_after_manual_review({**state, "needs_annotation": False}) == "persistence"
    # Fallthrough: no final_status, quality not PASS -> persistence.
    assert r.route_after_manual_review({"final_status": None, "quality_status": None}) == "persistence"
