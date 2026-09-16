from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import src.agents.annotation as annotation
from src.graph.state import build_initial_state

RESPONSE = '{"category": "customer_data", "confidence": 0.92, "tags": ["crm", "contacts"]}'
RECORDS = [{"customer_id": "C00001", "name": "Asha", "email": "asha@example.com"}]
DOC = {"title": "Press Release", "date": "2025-10-05", "amount": 10, "parties": ["Acme"]}


def _mock_client(content=RESPONSE):
    c = MagicMock()
    c.invoke.return_value = SimpleNamespace(content=content)
    return c


@pytest.fixture
def clients(monkeypatch):
    local, external = _mock_client(), _mock_client()
    monkeypatch.setattr(annotation, "get_local_client", lambda: local)
    monkeypatch.setattr(annotation, "get_external_client", lambda: external)
    return SimpleNamespace(local=local, external=external)


def _state(data_type="structured", sensitivity_flag=True):
    s = build_initial_state("/tmp/x", "x", True, "customer_records")
    s.update({
        "data_type": data_type,
        "sensitivity_flag": sensitivity_flag,
        "structured_records": RECORDS if data_type == "structured" else None,
        "extracted_data": DOC if data_type == "unstructured" else None,
    })
    return s


@pytest.mark.parametrize("data_type", ["structured", "unstructured"])
def test_sensitive_uses_local(clients, data_type):
    out = annotation.annotation_node(_state(data_type, sensitivity_flag=True))
    clients.local.invoke.assert_called_once()
    clients.external.invoke.assert_not_called()
    assert out["annotation_model_used"] == "local"
    assert out["annotation_results"] == {"category": "customer_data", "confidence": 0.92, "tags": ["crm", "contacts"]}


@pytest.mark.parametrize("data_type", ["structured", "unstructured"])
def test_non_sensitive_uses_external(clients, data_type):
    out = annotation.annotation_node(_state(data_type, sensitivity_flag=False))
    clients.external.invoke.assert_called_once()
    clients.local.invoke.assert_not_called()
    assert out["annotation_model_used"] == "external"


def test_reads_field_by_data_type(clients):
    annotation.annotation_node(_state("structured"))
    assert "C00001" in clients.local.invoke.call_args[0][0]
    clients.local.invoke.reset_mock()
    annotation.annotation_node(_state("unstructured"))
    prompt = clients.local.invoke.call_args[0][0]
    assert "Press Release" in prompt and "C00001" not in prompt


def test_normalizes_bad_category_and_confidence(monkeypatch):
    monkeypatch.setattr(annotation, "get_local_client",
                        lambda: _mock_client('{"category": "weird", "confidence": 7}'))
    out = annotation.annotation_node(_state())
    assert out["annotation_results"]["category"] == "other"
    assert out["annotation_results"]["confidence"] == 1.0


def test_exception_sets_failed(monkeypatch):
    c = MagicMock()
    c.invoke.side_effect = TimeoutError("timed out")
    monkeypatch.setattr(annotation, "get_local_client", lambda: c)
    out = annotation.annotation_node(_state())
    assert out["final_status"] == "failed"
    assert set(out) == {"final_status", "error_message", "node_trace"}
    assert out["node_trace"][0]["status"] == "failed"
