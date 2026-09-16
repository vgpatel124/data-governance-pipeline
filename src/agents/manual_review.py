"""manual_review_node — human-in-the-loop review.

Five entry points, only three are interruptible:

  Interruptible (interrupt() and wait for a human decision):
    - structured quality FAIL
    - unstructured retries exhausted
    - output guardrail failure

  Hard-terminal (no interrupt, no resume offered):
    - input guardrail rejection   — nothing coherent to "approve"
    - parsed-injection-flag trip  — a one-click approve would bypass the control

Resume payload: Command(resume={"decision": "approve"|"reject", "notes": "..."}).
The interrupt payload carries issue summaries only, never raw content.
"""
from langgraph.types import interrupt

from src.graph.state import NodeTimer, PipelineState

HARD_TERMINAL_REASONS = {"input_rejected", "parsed_injection", "unknown"}
INTERRUPTIBLE_REASONS = {"structured_quality_fail", "retries_exhausted", "output_guardrail_failed"}


def review_reason(state: PipelineState) -> str:
    if state.get("input_valid") is False:
        return "input_rejected"
    if state.get("parsed_injection_flag"):
        return "parsed_injection"
    # Checked before quality: after an approved quality override, quality_status is
    # "PASS" and a later guardrail failure is the live reason.
    if state.get("output_guardrail_passed") is False:
        return "output_guardrail_failed"
    if state.get("quality_status") == "FAIL":
        if state.get("data_type") == "unstructured":
            return "retries_exhausted"
        return "structured_quality_fail"
    return "unknown"


def build_interrupt_payload(state: PipelineState, reason: str) -> dict:
    return {
        "document_id": state["document_id"],
        "reason": reason,
        "quality_issues": list(state.get("quality_issues") or []),
        "policy_violations": list(state.get("policy_violations") or []),
    }


def manual_review_node(state: PipelineState) -> dict:
    reason = review_reason(state)

    if reason in HARD_TERMINAL_REASONS:
        timer = NodeTimer("manual_review")
        return {"final_status": "manual_review", "node_trace": [timer.entry("ok")]}

    # On resume LangGraph re-runs this node from the top; interrupt() then returns
    # the resume value instead of pausing. The timer is started after the pause so
    # duration_ms reflects node work, not human wait time.
    decision = interrupt(build_interrupt_payload(state, reason))
    timer = NodeTimer("manual_review")

    decision = decision if isinstance(decision, dict) else {}
    notes = str(decision.get("notes") or "")

    if decision.get("decision") == "approve":
        update = {"human_reviewer_notes": notes, "node_trace": [timer.entry("ok")]}
        if reason == "output_guardrail_failed":
            # output_guardrail_passed intentionally stays False — an accurate record
            # of what the guardrail found; the human override lives in the notes.
            update["final_status"] = "completed"
        else:
            update["quality_status"] = "PASS"  # quality override
        return update

    # Reject (or any unrecognized decision — fail closed). NOT "rejected": that
    # value is reserved for automated input-guardrail rejections.
    return {
        "final_status": "manual_review",
        "human_reviewer_notes": notes,
        "node_trace": [timer.entry("ok")],
    }
