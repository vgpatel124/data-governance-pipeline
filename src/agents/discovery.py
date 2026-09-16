import json
import math
from pathlib import Path

import pandas as pd

from src.graph.state import NodeTimer, PipelineState

STRUCTURED_EXTENSIONS = {".csv", ".json"}
UNSTRUCTURED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}

SENSITIVE_KEYWORDS = ["email", "phone", "pan", "aadhaar", "ssn", "salary", "dob", "account", "card"]
SENSITIVE_FILENAME_PATTERNS = ["invoice", "medical", "patient", "passport", "id_card"]
# Positive non-sensitive signals. Without one of these, unstructured files default
# to sensitive (fail-safe) — see PROGRESS.md Stage 2 deviations.
PUBLIC_FILENAME_PATTERNS = ["public", "press_release", "brochure", "newsletter", "announcement"]


def _has_sensitive_keyword(text: str) -> bool:
    text = text.lower()
    return any(k in text for k in SENSITIVE_KEYWORDS)


def _clean_value(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def load_structured(file_path: str) -> tuple[list[str], list[dict]]:
    """Load CSV/JSON once; return (headers, records)."""
    ext = Path(file_path).suffix.lower()
    if ext == ".csv":
        df = pd.read_csv(file_path, dtype=str, keep_default_na=True)
        headers = [str(c) for c in df.columns]
        records = [
            {k: _clean_value(v) for k, v in row.items()}
            for row in df.to_dict(orient="records")
        ]
        return headers, records
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("records", [data])
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise ValueError("JSON must be an object, a list of objects, or {'records': [...]}")
    headers: list[str] = []
    for r in data:
        for k in r:
            if k not in headers:
                headers.append(str(k))
    return headers, data


def unstructured_sensitivity(file_path: str, file_name: str) -> bool:
    meta = f"{file_path} {file_name}".lower()
    if _has_sensitive_keyword(meta) or any(p in meta for p in SENSITIVE_FILENAME_PATTERNS):
        return True
    name = (file_name or Path(file_path).name).lower()
    if any(p in name for p in PUBLIC_FILENAME_PATTERNS):
        return False
    return True  # fail-safe default: no signal available


def discovery_node(state: PipelineState) -> dict:
    timer = NodeTimer("discovery")
    try:
        file_path = state["file_path"]
        file_name = state.get("file_name") or Path(file_path).name
        ext = Path(file_name).suffix.lower() or Path(file_path).suffix.lower()

        if ext in STRUCTURED_EXTENSIONS:
            headers, records = load_structured(file_path)
            return {
                "data_type": "structured",
                "sensitivity_flag": any(_has_sensitive_keyword(h) for h in headers),
                "structured_records": records,
                "node_trace": [timer.entry("ok")],
            }
        if ext in UNSTRUCTURED_EXTENSIONS:
            return {
                "data_type": "unstructured",
                "sensitivity_flag": unstructured_sensitivity(file_path, file_name),
                "structured_records": None,
                "node_trace": [timer.entry("ok")],
            }
        raise ValueError(f"unsupported extension for discovery: {ext}")
    except Exception as e:  # noqa: BLE001
        return timer.failed(e)
