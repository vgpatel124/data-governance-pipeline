from src.graph.state import NodeTimer, PipelineState
from src.pii.rules import govern


def governance_node(state: PipelineState) -> dict:
    timer = NodeTimer("governance")
    try:
        if state.get("data_type") == "unstructured":
            content, location = state.get("extracted_data"), "document"
        else:
            content, location = state.get("structured_records"), "rows"
        governed, findings = govern(content, location)
        return {
            "pii_findings": findings,
            "final_content": governed,
            "node_trace": [timer.entry("ok")],
        }
    except Exception as e:  # noqa: BLE001
        return timer.failed(e)
