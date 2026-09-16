import json
from pathlib import Path

import pytest

from src.agents.discovery import discovery_node
from src.agents.governance import governance_node
from src.graph.state import build_initial_state
from src.guardrails.output_guardrail import output_guardrail_node
from src.pii.rules import govern, luhn_valid, scan_text

SYN = Path(__file__).resolve().parents[1] / "data" / "synthetic"

VALID_CARD = "4111 1111 1111 1111"
INVALID_LUHN_CARD = "4111 1111 1111 1112"
REAL_PAN = "ABCPE1234F"
EMAIL = "john.doe@example.com"
PHONE = "+91 9876543210"
AADHAAR = "2345 6789 0123"
GSTIN = "27ABCPE1234F1Z5"


def _findings(text):
    return scan_text(text, "f")[1]


def _types(text):
    return [f["entity_type"] for f in _findings(text)]


# ---------- detection ----------

def test_luhn():
    assert luhn_valid(VALID_CARD)
    assert not luhn_valid(INVALID_LUHN_CARD)


def test_valid_luhn_card_detected_and_redacted():
    [f] = _findings(f"card {VALID_CARD} on file")
    assert f["entity_type"] == "card" and f["action"] == "redacted" and f["masked_value"] is None


def test_invalid_luhn_card_not_detected():
    assert _types(f"ref {INVALID_LUHN_CARD}") == []
    assert _types("ref 4111111111111112") == []


def test_real_vs_fake_pan_format():
    assert _types(f"PAN {REAL_PAN}") == ["pan"]
    for fake in ["ABCP1234F", "ABCPE12345", "abcpe1234f", "ABCPE1234FX", "1BCPE1234F"]:
        assert "pan" not in _types(f"PAN {fake}"), fake


def test_gstin_detected_once_not_as_pan():
    assert _types(f"GSTIN {GSTIN}") == ["gstin"]


def test_aadhaar_and_phone():
    assert _types(f"aadhaar {AADHAAR}") == ["aadhaar"]
    assert _types("aadhaar 234567890123") == ["aadhaar"]
    assert _types(f"call {PHONE}") == ["phone"]
    assert _types("call 9876543210") == ["phone"]
    assert _types("call 1234567890") == []  # Indian mobiles start 6-9


# ---------- per-finding actions & no raw values ----------

def test_each_finding_carries_its_own_action():
    # Delimited like real text; bare-space-adjacent digit groups are ambiguous
    # (see PROGRESS.md Stage 5).
    text = f"{EMAIL}; {PHONE}; {REAL_PAN}; {AADHAAR}; {VALID_CARD}; {GSTIN}"
    by_type = {f["entity_type"]: f for f in _findings(text)}
    assert by_type["email"]["action"] == "masked"
    assert by_type["phone"]["action"] == "masked"
    assert by_type["pan"]["action"] == "redacted"
    assert by_type["aadhaar"]["action"] == "redacted"
    assert by_type["card"]["action"] == "redacted"
    assert by_type["gstin"]["action"] == "allowed"


@pytest.mark.parametrize("raw", [EMAIL, PHONE, REAL_PAN, AADHAAR, VALID_CARD, GSTIN])
def test_masked_value_never_raw(raw):
    [f] = _findings(f"x {raw} y")
    assert f["masked_value"] != raw
    if f["masked_value"] is not None:
        assert raw not in f["masked_value"]
        assert raw.replace(" ", "") not in f["masked_value"]
    assert raw not in json.dumps(f)


def test_masked_value_is_none_for_redacted_categories():
    for raw in [REAL_PAN, AADHAAR, VALID_CARD]:
        [f] = _findings(raw)
        assert f["masked_value"] is None


def test_email_mask_format():
    [f] = _findings(EMAIL)
    assert f["masked_value"] == "jo****@example.com"


def test_content_masked_in_place():
    governed, _ = scan_text(f"mail {EMAIL} pan {REAL_PAN}", "f")
    assert EMAIL not in governed and REAL_PAN not in governed
    assert "jo****@example.com" in governed and "[REDACTED_PAN]" in governed


def test_govern_walks_nested_and_preserves_shape():
    content = {"a": [{"b": EMAIL}, "plain", 5], "c": {"d": REAL_PAN}, "n": None}
    governed, findings = govern(content, "document")
    assert governed["a"][1] == "plain" and governed["a"][2] == 5 and governed["n"] is None
    assert governed["c"]["d"] == "[REDACTED_PAN]"
    assert {f["location"].split("@")[0] for f in findings} == {"document.a[0].b", "document.c.d"}


# ---------- governance_node ----------

def test_governance_node_structured_fixture():
    s = build_initial_state(str(SYN / "customers_clean.csv"), "customers_clean.csv", False, "customer_records")
    s.update(discovery_node(s))
    raw_records = json.dumps(s["structured_records"])
    out = governance_node(s)
    assert isinstance(out["final_content"], list) and len(out["final_content"]) == 20
    types = {f["entity_type"] for f in out["pii_findings"]}
    assert {"email", "phone", "pan"} <= types
    # no raw email/pan from the source survives into final_content or findings
    for rec in s["structured_records"]:
        dumped = json.dumps(out["final_content"]) + json.dumps(out["pii_findings"])
        assert rec["email"] not in dumped and rec["pan"] not in dumped
    assert json.dumps(s["structured_records"]) == raw_records  # input not mutated


def test_governance_node_unstructured_reads_extracted_data():
    s = build_initial_state("/tmp/x.pdf", "x.pdf", False, "documents")
    s.update({"data_type": "unstructured", "extracted_data": {"title": "t", "parties": [EMAIL]},
              "structured_records": [{"email": "other@example.com"}]})
    out = governance_node(s)
    assert isinstance(out["final_content"], dict)
    assert out["final_content"]["parties"] == ["jo****@example.com"]
    assert len(out["pii_findings"]) == 1


# ---------- output_guardrail_node ----------

def _og_state(final_content):
    s = build_initial_state("/tmp/x", "x", False, "documents")
    s["final_content"] = final_content
    return s


def test_output_guardrail_passes_governed_content():
    governed, _ = govern({"t": f"{EMAIL} {REAL_PAN} {GSTIN}"})
    out = output_guardrail_node(_og_state(governed))
    assert out["output_guardrail_passed"] is True
    assert out["final_status"] == "completed"
    assert out["policy_violations"] == []


def test_output_guardrail_catches_unmasked_pii_without_leaking_it():
    out = output_guardrail_node(_og_state({"note": f"card {VALID_CARD}"}))
    assert out["output_guardrail_passed"] is False
    assert "final_status" not in out
    assert out["policy_violations"] and all(VALID_CARD not in v for v in out["policy_violations"])


def test_output_guardrail_policy_forbidden_key_and_empty():
    assert output_guardrail_node(_og_state({"raw_text": "x"}))["output_guardrail_passed"] is False
    assert output_guardrail_node(_og_state(None))["output_guardrail_passed"] is False
