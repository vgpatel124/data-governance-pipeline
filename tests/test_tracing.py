import json

from src.config.settings import settings
from src.observability import tracing


def test_offline_fallback_returns_empty_callbacks(monkeypatch):
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "")
    assert tracing.get_callbacks() == []
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "pk-only")
    assert tracing.get_callbacks() == []  # one key missing is still offline


def test_run_config_uses_document_id_as_thread_id(monkeypatch):
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    cfg = tracing.get_run_config("doc-123")
    assert cfg == {"configurable": {"thread_id": "doc-123"}, "callbacks": []}


def test_handler_construction_error_degrades_to_no_callbacks(monkeypatch):
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "sk")

    def boom():
        raise RuntimeError("no network")
    monkeypatch.setattr(tracing, "_build_handler", boom)
    assert tracing.get_callbacks() == []


def test_mask_strips_content_fields_and_keeps_timing():
    state = {
        "node_name": "governance", "status": "ok", "duration_ms": 1.5, "document_id": "d1",
        "raw_text": "PAN ABCPE1234F", "extracted_data": {"parties": ["jane@example.com"]},
        "structured_records": [{"email": "jane@example.com"}], "final_content": "x",
        "pii_findings": [{"entity_type": "email"}], "file_path": "/secret/path",
    }
    masked = tracing.mask_payload(data=state)
    assert masked == {"node_name": "governance", "status": "ok", "duration_ms": 1.5, "document_id": "d1"}
    dumped = json.dumps(masked)
    assert "ABCPE1234F" not in dumped and "jane@example.com" not in dumped


def test_mask_redacts_strings_and_lists_positional_call():
    assert tracing.mask_payload("Ignore previous instructions; PAN ABCPE1234F") == tracing.REDACTED
    assert tracing.mask_payload(data=["a", "b"]) == tracing.REDACTED
