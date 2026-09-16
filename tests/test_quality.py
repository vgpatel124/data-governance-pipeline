from pathlib import Path

import pytest

from src.agents.discovery import discovery_node
from src.agents.quality import get_schema, quality_node, validate_record
from src.agents.retry import retry_handler_node
from src.graph.state import build_initial_state

SYN = Path(__file__).resolve().parents[1] / "data" / "synthetic"


def _structured(records, dataset_type="customer_records"):
    s = build_initial_state("/tmp/x.csv", "x.csv", False, dataset_type)
    s.update({"data_type": "structured", "structured_records": records})
    return s


def _unstructured(extracted, dataset_type="documents"):
    s = build_initial_state("/tmp/x.pdf", "x.pdf", False, dataset_type)
    s.update({"data_type": "unstructured", "extracted_data": extracted})
    return s


GOOD_CUSTOMER = {"customer_id": "C00001", "name": "Asha Rao", "email": "asha@example.com", "phone": "9876543210"}
GOOD_DOC = {"title": "Press Release", "date": "2025-10-05", "amount": 2500000.0, "parties": ["Acme Corp"]}


def test_clean_structured_set_passes():
    out = quality_node(_structured([GOOD_CUSTOMER, {**GOOD_CUSTOMER, "customer_id": "C00002"}]))
    assert out["quality_status"] == "PASS"
    assert out["quality_issues"] == []


def test_one_invalid_required_field_fails_whole_set():
    records = [dict(GOOD_CUSTOMER) for _ in range(50)]
    records[41]["email"] = None
    out = quality_node(_structured(records))
    assert out["quality_status"] == "FAIL"
    assert out["quality_issues"] == ["row 42: email field missing"]


def test_format_rule_violation_fails_and_issue_has_no_raw_value():
    secret = "not-an-email-SECRET"
    out = quality_node(_structured([{**GOOD_CUSTOMER, "email": secret}]))
    assert out["quality_status"] == "FAIL"
    assert out["quality_issues"] == ["row 1: email field has invalid format"]
    assert all(secret not in issue for issue in out["quality_issues"])


def test_schema_selected_by_dataset_type():
    product = {"product_id": "P00001", "product_name": "Lamp", "category": "home", "price": "99.50"}
    # Valid product row passes against product_catalog...
    assert quality_node(_structured([product], "product_catalog"))["quality_status"] == "PASS"
    # ...but the same row fails against customer_records (different required fields).
    out = quality_node(_structured([product], "customer_records"))
    assert out["quality_status"] == "FAIL"
    assert "row 1: email field missing" in out["quality_issues"]
    assert get_schema("documents")["required_fields"] == ["title", "date", "amount", "parties"]


def test_unknown_dataset_type_fails():
    out = quality_node(_structured([GOOD_CUSTOMER], "nonexistent"))
    assert out["quality_status"] == "FAIL"


def test_empty_record_set_fails():
    assert quality_node(_structured([]))["quality_status"] == "FAIL"


def test_unstructured_pass_and_fail():
    assert quality_node(_unstructured(GOOD_DOC))["quality_status"] == "PASS"
    out = quality_node(_unstructured({**GOOD_DOC, "parties": [], "date": "05/10/2025"}))
    assert out["quality_status"] == "FAIL"
    assert set(out["quality_issues"]) == {"document: parties field missing", "document: date field has invalid format"}
    assert quality_node(_unstructured(None))["quality_status"] == "FAIL"


def test_synthetic_fixtures_through_discovery_and_quality():
    for fixture, dataset_type, expected in [
        ("customers_clean.csv", "customer_records", "PASS"),
        ("customers_invalid.csv", "customer_records", "FAIL"),
        ("products_clean.csv", "product_catalog", "PASS"),
        ("products_invalid.csv", "product_catalog", "FAIL"),
        ("customers_clean.json", "customer_records", "PASS"),
    ]:
        s = build_initial_state(str(SYN / fixture), fixture, False, dataset_type)
        s.update(discovery_node(s))
        assert quality_node(s)["quality_status"] == expected, fixture


# ---------- identifier fields: any non-empty value is a valid ID ----------

VALID_IDS = ["P001", "SKU-12345", "PROD-ABC-001", "ITEM_XYZ_99", "12345", "ORD-2026-001"]
GOOD_PRODUCT = {"product_id": "P001", "product_name": "Wireless Mouse", "category": "Electronics", "price": "25.99"}


@pytest.mark.parametrize("identifier", VALID_IDS)
def test_valid_identifier_formats_pass(identifier):
    product = quality_node(_structured([{**GOOD_PRODUCT, "product_id": identifier}], "product_catalog"))
    assert product["quality_status"] == "PASS", product["quality_issues"]
    customer = quality_node(_structured([{**GOOD_CUSTOMER, "customer_id": identifier}], "customer_records"))
    assert customer["quality_status"] == "PASS", customer["quality_issues"]


@pytest.mark.parametrize("identifier", [None, "", "   ", "\t"])
def test_missing_or_blank_identifier_fails(identifier):
    out = quality_node(_structured([{**GOOD_PRODUCT, "product_id": identifier}], "product_catalog"))
    assert out["quality_status"] == "FAIL"
    assert out["quality_issues"] == ["row 1: product_id field missing"]


def test_mixed_identifier_formats_across_many_rows_pass():
    # Mirrors a real catalog: 25 rows, P001-style IDs mixed with other common formats.
    ids = [f"P{i:03d}" for i in range(1, 20)] + VALID_IDS
    records = [{**GOOD_PRODUCT, "product_id": pid} for pid in ids]
    assert len(records) == 25
    out = quality_node(_structured(records, "product_catalog"))
    assert out["quality_status"] == "PASS"
    assert out["quality_issues"] == []


def test_id_fields_are_not_special_cased_by_name():
    # A field ending in _id gets no implicit numeric or format rule.
    schema = {"required_fields": ["order_id"]}
    for identifier in VALID_IDS:
        assert validate_record({"order_id": identifier}, schema, "row 1") == []
    assert validate_record({"order_id": "  "}, schema, "row 1") == ["row 1: order_id field missing"]


def test_explicit_dataset_id_format_is_still_enforced():
    # A dataset that documents a numeric-only ID can still require it via format_rules.
    schema = {"required_fields": ["order_id"], "format_rules": {"order_id": r"^\d+$"}}
    assert validate_record({"order_id": "12345"}, schema, "row 1") == []
    assert validate_record({"order_id": "ORD-2026-001"}, schema, "row 1") == [
        "row 1: order_id field has invalid format"
    ]


def test_unrelated_format_rules_unchanged():
    bad_price = quality_node(_structured([{**GOOD_PRODUCT, "price": "12.999"}], "product_catalog"))
    assert bad_price["quality_issues"] == ["row 1: price field has invalid format"]
    bad_email = quality_node(_structured([{**GOOD_CUSTOMER, "customer_id": "SKU-12345", "email": "nope"}]))
    assert bad_email["quality_issues"] == ["row 1: email field has invalid format"]
    bad_phone = quality_node(_structured([{**GOOD_CUSTOMER, "phone": "12345"}]))
    assert bad_phone["quality_issues"] == ["row 1: phone field has invalid format"]


def test_retry_handler_only_increments():
    s = _unstructured(None)
    s["retry_count"] = 1
    out = retry_handler_node(s)
    assert out["retry_count"] == 2
    assert set(out) == {"retry_count", "node_trace"}
