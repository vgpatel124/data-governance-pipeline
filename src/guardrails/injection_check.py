"""Prompt-injection heuristic.

`detect_injection` is shared by the input guardrail (file-level scan) and
`injection_check_parsed_node` (scan of raw_text BEFORE any LLM sees it).
"""
import re

from src.graph.state import NodeTimer, PipelineState

INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|prompts?|rules)",
    r"disregard\s+(all\s+)?(the\s+)?(previous|prior|above|earlier|your)\s+(instructions|prompts?|rules)",
    r"forget\s+(all\s+)?(your|previous|prior)\s+(instructions|rules)",
    r"override\s+(your|the|all)\s+(instructions|rules|guidelines)",
    r"(^|[\s\"'\[{(<,;|])system\s*:",
    r"you\s+are\s+now\s+(a|an|in)\b",
    r"reveal\s+(your|the)\s+(system\s+)?prompt",
    r"<\|?\s*(im_start|im_end|system)\s*\|?>",
    r"\[\s*/?\s*inst\s*\]",
    r"new\s+instructions\s*:",
]
_COMPILED = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in INJECTION_PATTERNS]


def detect_injection(text: str | None) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _COMPILED)


def injection_check_parsed_node(state: PipelineState) -> dict:
    timer = NodeTimer("injection_check_parsed")
    return {
        "parsed_injection_flag": detect_injection(state.get("raw_text")),
        "node_trace": [timer.entry("ok")],
    }
