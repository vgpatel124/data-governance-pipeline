import re
from functools import lru_cache

import yaml

from src.config.settings import settings
from src.graph.state import NodeTimer, PipelineState


@lru_cache(maxsize=4)
def load_schemas(path: str | None = None) -> dict:
    with open(path or settings.VALIDATION_SCHEMAS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_schema(dataset_type: str) -> dict | None:
    return load_schemas().get(dataset_type)


def _is_missing(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def validate_record(record: dict, schema: dict, label: str) -> list[str]:
    """Returns issue descriptions. Never embeds the field value itself."""
    issues = []
    for field in schema.get("required_fields", []):
        if _is_missing(record.get(field)):
            issues.append(f"{label}: {field} field missing")
    for field, pattern in (schema.get("format_rules") or {}).items():
        value = record.get(field)
        if _is_missing(value) or isinstance(value, (list, dict)):
            continue
        if not re.fullmatch(pattern, str(value).strip()):
            issues.append(f"{label}: {field} field has invalid format")
    return issues


def quality_node(state: PipelineState) -> dict:
    timer = NodeTimer("quality")
    dataset_type = state.get("dataset_type")
    schema = get_schema(dataset_type)
    issues: list[str] = []

    if schema is None:
        issues.append(f"no validation schema for dataset_type '{dataset_type}'")
    elif state.get("data_type") == "structured":
        records = state.get("structured_records") or []
        if not records:
            issues.append("no records found")
        for i, record in enumerate(records, start=1):
            issues.extend(validate_record(record, schema, f"row {i}"))
    else:
        extracted = state.get("extracted_data")
        if not isinstance(extracted, dict):
            issues.append("document: no extracted data")
        else:
            issues.extend(validate_record(extracted, schema, "document"))

    return {
        "quality_status": "FAIL" if issues else "PASS",  # any single issue fails the file
        "quality_issues": issues,
        "node_trace": [timer.entry("ok")],
    }
