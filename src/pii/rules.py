"""Regex + validation PII rules. No LLM.

Per-finding actions:
  email, phone          -> "masked"   (partial value kept, e.g. jo****@example.com)
  pan, aadhaar, card    -> "redacted" (no value at all; masked_value=None)
  gstin (and others)    -> "allowed"  (left in content; finding still never stores the raw value)

The raw matched substring is never returned from this module.
"""
import re
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from src.graph.state import PIIFinding

# Order matters: longer/more specific patterns claim their span first so e.g. a
# PAN embedded inside a GSTIN is not double-reported.
PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("gstin", re.compile(r"(?<![A-Z0-9])\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z](?![A-Z0-9])")),
    ("card", re.compile(r"(?<![\d])(?:\d[ -]?){12,18}\d(?![\d])")),
    ("aadhaar", re.compile(r"(?<!\d)(?<!\d[ -])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![ -]?\d)")),
    ("pan", re.compile(r"(?<![A-Z0-9])[A-Z]{5}[0-9]{4}[A-Z]{1}(?![A-Z0-9])")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("phone", re.compile(r"(?<![\d+])(?:\+91[ -]?|0)?[6-9]\d{9}(?!\d)")),
]

ACTIONS = {
    "email": "masked",
    "phone": "masked",
    "pan": "redacted",
    "aadhaar": "redacted",
    "card": "redacted",
}


def luhn_valid(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask_email(value: str) -> str:
    local, _, domain = value.partition("@")
    return f"{local[:2]}****@{domain}"


def mask_generic(value: str, keep_last: int = 4) -> str:
    compact = re.sub(r"[\s-]", "", value)
    return "*" * max(len(compact) - keep_last, 4) + compact[-keep_last:]


def masked_representation(entity_type: str, value: str) -> Optional[str]:
    action = ACTIONS.get(entity_type, "allowed")
    if action == "redacted":
        return None
    if entity_type == "email":
        return mask_email(value)
    if entity_type == "phone":
        return mask_generic(value, keep_last=4)
    return mask_generic(value, keep_last=3)  # allowed categories: still never raw


def redaction_token(entity_type: str) -> str:
    return f"[REDACTED_{entity_type.upper()}]"


@dataclass
class _Match:
    entity_type: str
    start: int
    end: int
    value: str


def _find_matches(text: str) -> List[_Match]:
    claimed: List[Tuple[int, int]] = []
    matches: List[_Match] = []
    for entity_type, pattern in PATTERNS:
        for m in pattern.finditer(text):
            s, e = m.span()
            if any(s < ce and e > cs for cs, ce in claimed):
                continue
            if entity_type == "card" and not luhn_valid(m.group()):
                continue
            claimed.append((s, e))
            matches.append(_Match(entity_type, s, e, m.group()))
    return sorted(matches, key=lambda m: m.start)


def scan_text(text: str, location: str) -> Tuple[str, List[PIIFinding]]:
    """Returns (governed_text, findings). Findings never contain raw values."""
    if not isinstance(text, str) or not text:
        return text, []
    findings: List[PIIFinding] = []
    out, cursor = [], 0
    for m in _find_matches(text):
        action = ACTIONS.get(m.entity_type, "allowed")
        masked = masked_representation(m.entity_type, m.value)
        findings.append(
            PIIFinding(
                entity_type=m.entity_type,
                masked_value=masked,
                location=f"{location}@{m.start}",
                action=action,
            )
        )
        out.append(text[cursor : m.start])
        if action == "redacted":
            out.append(redaction_token(m.entity_type))
        elif action == "masked":
            out.append(masked)
        else:
            out.append(m.value)
        cursor = m.end
    out.append(text[cursor:])
    return "".join(out), findings


def _child_location(parent: str, key: Any, in_list: bool) -> str:
    if in_list:
        return f"{parent}[{key}]"
    return f"{parent}.{key}" if parent else str(key)


def govern(content: Any, location: str = "") -> Tuple[Any, List[PIIFinding]]:
    """Recursively walk dicts/lists, governing every string value in place
    (on a copy). Preserves the original shape."""
    if isinstance(content, str):
        return scan_text(content, location or "value")
    if isinstance(content, dict):
        result, findings = {}, []
        for k, v in content.items():
            gv, f = govern(v, _child_location(location, k, in_list=False))
            result[k] = gv
            findings.extend(f)
        return result, findings
    if isinstance(content, list):
        result, findings = [], []
        for i, v in enumerate(content):
            gv, f = govern(v, _child_location(location, i, in_list=True))
            result.append(gv)
            findings.extend(f)
        return result, findings
    return content, []


def detect(content: Any) -> List[PIIFinding]:
    """Scan only (no content returned). Used by the output guardrail."""
    return govern(content)[1]
