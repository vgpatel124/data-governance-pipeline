from src.graph.state import NodeTimer, PipelineState


def retry_handler_node(state: PipelineState) -> dict:
    """Increments retry_count only; routing is decided by route_after_retry."""
    timer = NodeTimer("retry_handler")
    return {
        "retry_count": state.get("retry_count", 0) + 1,
        "node_trace": [timer.entry("ok")],
    }
