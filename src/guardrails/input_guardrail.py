import logging
import os
import re
from pathlib import Path

from src.config.settings import settings
from src.graph.state import NodeTimer, PipelineState
from src.guardrails.injection_check import detect_injection

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".csv", ".json", ".pdf", ".png", ".jpg", ".jpeg"}
TEXT_EXTENSIONS = {".csv", ".json"}

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# Multipart clients (e.g. httpx) percent-encode control bytes in filenames, so a
# NUL arrives as "%00" — strip encoded control sequences too.
_ENCODED_CONTROL_CHARS = re.compile(r"%(?:[01][0-9a-fA-F]|7[fF])")


def sanitize_for_log(name: str | None) -> str:
    """Strip control characters (raw and percent-encoded) and path-traversal
    components before logging or using a name on disk."""
    if not name:
        return ""
    name = _ENCODED_CONTROL_CHARS.sub("", str(name))
    name = _CONTROL_CHARS.sub("", name)
    name = name.replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".", "..")]
    return parts[-1] if parts else ""


def input_guardrail_node(state: PipelineState) -> dict:
    timer = NodeTimer("input_guardrail")
    try:
        file_path = state["file_path"]
        safe_name = sanitize_for_log(state.get("file_name") or os.path.basename(file_path))
        ext = Path(state.get("file_name") or file_path).suffix.lower()

        def reject(reason: str, injection: bool = False) -> dict:
            logger.info("input rejected: file=%s reason=%s", safe_name, reason)
            return {
                "input_valid": False,
                "input_injection_flag": injection,
                "rejection_reason": reason,
                "node_trace": [timer.entry("ok")],
            }

        if not os.path.isfile(file_path):
            return reject("file does not exist")
        if ext not in ALLOWED_EXTENSIONS:
            return reject(f"extension '{sanitize_for_log(ext)}' not allowed")
        size = os.path.getsize(file_path)
        if size > settings.MAX_FILE_SIZE_BYTES:
            return reject(f"file size {size} bytes exceeds limit {settings.MAX_FILE_SIZE_BYTES}")
        if size == 0:
            return reject("file is empty")

        if ext in TEXT_EXTENSIONS:
            # Text formats: scan the actual content.
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            injection = detect_injection(content) or detect_injection(state.get("file_name"))
        else:
            # Binary formats: filename-only sanity check. The authoritative content
            # check runs later on raw_text (injection_check_parsed_node).
            injection = detect_injection(state.get("file_name"))

        if injection:
            return reject("prompt-injection pattern detected", injection=True)

        logger.info("input accepted: file=%s", safe_name)
        return {
            "input_valid": True,
            "input_injection_flag": False,
            "rejection_reason": None,
            "node_trace": [timer.entry("ok")],
        }
    except Exception as e:  # noqa: BLE001
        return timer.failed(e)
