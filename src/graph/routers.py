"""Conditional-edge functions (Section 3). Every router that follows a node which
can throw checks final_status == "failed" first and short-circuits to persistence."""


def route_after_input_guardrail(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    return "discovery" if state["input_valid"] else "manual_review"


def route_by_data_type(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    return "extraction_parse" if state["data_type"] == "unstructured" else "quality"


def route_after_extraction_parse(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    return "injection_check_parsed"


def route_after_injection_check(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    return "manual_review" if state["parsed_injection_flag"] else "extraction_structure"


def route_after_extraction_structure(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    return "quality"


def route_after_quality(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    if state["quality_status"] == "FAIL":
        return "retry" if state["data_type"] == "unstructured" else "manual_review"
    return "annotation" if state["needs_annotation"] else "governance"


def route_after_retry(state) -> str:
    # <=, not <: max_retries means "this many retries allowed," not "this many
    # total attempts." With MAX_RETRIES=1, this permits exactly one retry
    # (retry_count reaches 1, 1 <= 1 is true) then stops on the next failure
    # (retry_count reaches 2, 2 <= 1 is false).
    return "extraction_parse" if state["retry_count"] <= state["max_retries"] else "manual_review"


def route_after_annotation(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    return "governance"


def route_after_governance(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    return "output_guardrail"


def route_after_output_guardrail(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    return "persistence" if state["output_guardrail_passed"] else "manual_review"


def route_after_manual_review(state) -> str:
    # Reject, or one of the two hard-terminal reasons (input rejection /
    # injection flag) — either way final_status is already "manual_review"
    # and there's nothing left to route to but persistence.
    if state.get("final_status") == "manual_review":
        return "persistence"
    # Approve, resumed from an output guardrail failure: manual_review_node set
    # final_status="completed". Checked BEFORE quality_status (deviation from the
    # plan's ordering, see PROGRESS.md Stage 4): by the time the output guardrail
    # runs, quality_status is already "PASS", so the plan's original order would
    # send this case back to annotation/governance and loop.
    if state.get("final_status") == "completed":
        return "persistence"
    # Approve, resumed from a quality failure: quality_status was overridden
    # to "PASS" inside manual_review_node — route exactly like a normal PASS.
    if state.get("quality_status") == "PASS":
        return "annotation" if state["needs_annotation"] else "governance"
    # Approve, resumed from an output guardrail failure: final_content and
    # pii_findings already exist, nothing left to do but persist.
    return "persistence"
