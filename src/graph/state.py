import operator
import time
from datetime import datetime
from typing import TypedDict, Literal, Optional, List, Dict, Any, Annotated
from uuid import uuid4

from src.config.settings import settings


class NodeTraceEntry(TypedDict):
    node_name: str
    timestamp: str
    duration_ms: float
    status: Literal["ok", "failed"]


class PIIFinding(TypedDict):
    entity_type: str                 # "email", "phone", "pan", "aadhaar", "gstin", "card"
    masked_value: Optional[str]      # partially-masked value, or None for redacted categories.
                                     # NEVER the raw match.
    location: str                    # field name or row/char offset
    action: Literal["masked", "redacted", "allowed"]   # decided PER FINDING


class PipelineState(TypedDict):
    # --- Ingestion (set once, at graph invocation) ---
    document_id: str
    file_path: str
    file_name: str
    needs_annotation: bool          # caller-supplied objective, NOT inferred by any agent
    dataset_type: str               # caller-supplied; selects the quality schema

    # --- Input guardrail ---
    input_valid: Optional[bool]
    input_injection_flag: Optional[bool]
    rejection_reason: Optional[str]

    # --- Discovery output ---
    data_type: Optional[Literal["structured", "unstructured"]]
    sensitivity_flag: Optional[bool]

    # --- Extraction output (unstructured path only) ---
    # raw_text = untouched OCR/parse output. extracted_data = LLM-structured result.
    # The injection check runs on raw_text BEFORE any LLM call.
    raw_text: Optional[str]
    extracted_data: Optional[Dict[str, Any]]
    extraction_model_used: Optional[Literal["local", "external"]]
    parsed_injection_flag: Optional[bool]

    # --- Structured data (loaded directly, no LLM) ---
    structured_records: Optional[List[Dict[str, Any]]]

    # --- Quality ---
    quality_status: Optional[Literal["PASS", "FAIL"]]
    quality_issues: List[str]        # describe the problem; never embed the raw field value
    # retry_count = RETRIES made so far (excludes the original attempt).
    # max_retries = retries allowed after the original attempt. Router checks
    # retry_count <= max_retries (NOT strict "<").
    retry_count: int
    max_retries: int

    # --- Annotation (optional, only if needs_annotation) ---
    annotation_results: Optional[Dict[str, Any]]
    annotation_model_used: Optional[Literal["local", "external"]]

    # --- Governance ---
    pii_findings: List[PIIFinding]
    final_content: Optional[Any]     # governed output — the ONLY content field allowed
                                     # to reach persistence

    # --- Output guardrail ---
    output_guardrail_passed: Optional[bool]
    policy_violations: List[str]

    # --- Terminal status ---
    # "failed" = technical error; "manual_review" = data/business judgment call.
    final_status: Optional[Literal["completed", "manual_review", "rejected", "failed"]]
    error_message: Optional[str]     # set only when final_status == "failed"
    # Set by a human reviewer on resume from interrupt(). On reject, final_status
    # stays "manual_review" (NOT "rejected", which is reserved for automated
    # input-guardrail rejections).
    human_reviewer_notes: Optional[str]

    # --- Timing / tracing ---
    started_at: str
    completed_at: Optional[str]
    # operator.add reducer: nodes return a ONE-ITEM list; LangGraph concatenates.
    node_trace: Annotated[List[NodeTraceEntry], operator.add]


def build_initial_state(
    file_path: str,
    file_name: str,
    needs_annotation: bool,
    dataset_type: str,
    document_id: Optional[str] = None,
) -> PipelineState:
    """Initial state per Section 1. `needs_annotation` and `dataset_type` are
    caller-supplied and never defaulted or inferred."""
    return PipelineState(
        document_id=document_id or str(uuid4()),
        file_path=file_path,
        file_name=file_name,
        needs_annotation=needs_annotation,
        dataset_type=dataset_type,
        input_valid=None,
        input_injection_flag=None,
        rejection_reason=None,
        data_type=None,
        sensitivity_flag=None,
        raw_text=None,
        extracted_data=None,
        extraction_model_used=None,
        parsed_injection_flag=None,
        structured_records=None,
        quality_status=None,
        quality_issues=[],
        retry_count=0,
        max_retries=int(settings.MAX_RETRIES),
        annotation_results=None,
        annotation_model_used=None,
        pii_findings=[],
        final_content=None,
        output_guardrail_passed=None,
        policy_violations=[],
        final_status=None,
        error_message=None,
        human_reviewer_notes=None,
        started_at=datetime.utcnow().isoformat(),
        completed_at=None,
        node_trace=[],
    )


class NodeTimer:
    """Helper for the one-NodeTraceEntry-per-node rule.

    Usage:
        timer = NodeTimer("discovery")
        ...
        return {..., "node_trace": [timer.entry("ok")]}
    """

    def __init__(self, node_name: str):
        self.node_name = node_name
        self.timestamp = datetime.utcnow().isoformat()
        self._start = time.perf_counter()

    def entry(self, status: Literal["ok", "failed"] = "ok") -> NodeTraceEntry:
        return NodeTraceEntry(
            node_name=self.node_name,
            timestamp=self.timestamp,
            duration_ms=round((time.perf_counter() - self._start) * 1000, 3),
            status=status,
        )

    def failed(self, exc: BaseException) -> dict:
        """Standard failure return for nodes calling external services."""
        return {
            "final_status": "failed",
            "error_message": str(exc),
            "node_trace": [self.entry("failed")],
        }
