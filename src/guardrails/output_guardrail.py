"""Final safety net before persistence.

1. PII re-scan of final_content: any masked/redacted-category entity still
   present in clear text means governance missed it.
2. Policy validation (configured below).
Violation strings describe the problem only — never the offending value.
"""
import json
from typing import Any, Iterable

from src.graph.state import NodeTimer, PipelineState
from src.pii.rules import detect

# --- configured policy ---
FORBIDDEN_KEYS = {"raw_text", "extracted_data", "structured_records", "password", "secret", "api_key"}
MAX_CONTENT_BYTES = 5 * 1024 * 1024


def _walk_keys(content: Any, path: str = "") -> Iterable[str]:
    if isinstance(content, dict):
        for k, v in content.items():
            p = f"{path}.{k}" if path else str(k)
            if str(k).lower() in FORBIDDEN_KEYS:
                yield p
            yield from _walk_keys(v, p)
    elif isinstance(content, list):
        for i, v in enumerate(content):
            yield from _walk_keys(v, f"{path}[{i}]")


def check_policy(content: Any) -> list[str]:
    violations = []
    if content is None:
        violations.append("policy: final_content is empty")
        return violations
    for p in _walk_keys(content):
        violations.append(f"policy: forbidden field '{p}' present in output")
    try:
        size = len(json.dumps(content, default=str).encode("utf-8"))
        if size > MAX_CONTENT_BYTES:
            violations.append(f"policy: output size {size} bytes exceeds {MAX_CONTENT_BYTES}")
    except (TypeError, ValueError):
        violations.append("policy: output is not serializable")
    return violations


def output_guardrail_node(state: PipelineState) -> dict:
    timer = NodeTimer("output_guardrail")
    try:
        content = state.get("final_content")
        violations = []
        for f in detect(content):
            if f["action"] != "allowed":
                violations.append(f"unmasked {f['entity_type']} at {f['location']}")
        violations.extend(check_policy(content))

        passed = not violations
        update = {
            "output_guardrail_passed": passed,
            "policy_violations": violations,
            "node_trace": [timer.entry("ok")],
        }
        if passed:
            update["final_status"] = "completed"
        return update
    except Exception as e:  # noqa: BLE001
        return timer.failed(e)
