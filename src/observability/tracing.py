"""Langfuse tracing with content-field exclusion and an offline fallback.

Only node name, status, and timing may leave the process. Every input/output/
metadata payload passes through `mask_payload`, which keeps a small allow-list
of non-content keys and drops everything else — so raw_text, extracted_data,
structured_records, final_content, and PII never reach a trace.
"""
import logging
from functools import lru_cache
from typing import Any

from src.config.settings import settings

logger = logging.getLogger(__name__)

ALLOWED_TRACE_KEYS = {
    "node_name", "langgraph_node", "langgraph_step", "status", "final_status",
    "duration_ms", "timestamp", "document_id", "retry_count", "data_type",
}
EXCLUDED_CONTENT_KEYS = {"raw_text", "extracted_data", "structured_records", "final_content", "pii_findings"}
REDACTED = "[redacted]"


def mask_payload(*args: Any, data: Any = None, **kwargs: Any) -> Any:
    """Langfuse `mask` hook. Accepts `data` as keyword (Langfuse v3/v4) or positional."""
    if data is None and args:
        data = args[0]
    return _mask(data)


def _mask(data: Any) -> Any:
    if data is None or isinstance(data, (bool, int, float)):
        return data
    if isinstance(data, dict):
        # Allow-listed keys are non-content metadata: keep scalar values (e.g.
        # status="ok"); anything nested under them is still masked recursively.
        return {k: (v if isinstance(v, (str, bool, int, float)) or v is None else _mask(v))
                for k, v in data.items()
                if k in ALLOWED_TRACE_KEYS and k not in EXCLUDED_CONTENT_KEYS}
    # strings, lists, messages, prompts: could all carry document content
    return REDACTED


def langfuse_enabled() -> bool:
    return bool(settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY)


@lru_cache(maxsize=1)
def _build_handler():
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    Langfuse(
        public_key=settings.LANGFUSE_PUBLIC_KEY,
        secret_key=settings.LANGFUSE_SECRET_KEY,
        host=settings.LANGFUSE_HOST,
        mask=mask_payload,
    )
    return CallbackHandler(public_key=settings.LANGFUSE_PUBLIC_KEY)


def get_callbacks() -> list:
    """Returns [] when Langfuse keys are missing (offline fallback) or when the
    handler cannot be constructed — observability is never a hard dependency."""
    if not langfuse_enabled():
        return []
    try:
        return [_build_handler()]
    except Exception as e:  # noqa: BLE001
        logger.warning("Langfuse disabled: handler construction failed (%s)", type(e).__name__)
        return []


def get_run_config(document_id: str) -> dict:
    """Config for graph.invoke: thread_id=document_id plus tracing callbacks."""
    return {"configurable": {"thread_id": document_id}, "callbacks": get_callbacks()}
