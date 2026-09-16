from langgraph.graph import END, START, StateGraph

from src.agents.annotation import annotation_node
from src.agents.discovery import discovery_node
from src.agents.extraction import extraction_parse_node, extraction_structure_node
from src.agents.governance import governance_node
from src.agents.manual_review import manual_review_node
from src.agents.quality import quality_node
from src.agents.retry import retry_handler_node
from src.graph.routers import (
    route_after_annotation,
    route_after_extraction_parse,
    route_after_extraction_structure,
    route_after_governance,
    route_after_injection_check,
    route_after_input_guardrail,
    route_after_manual_review,
    route_after_output_guardrail,
    route_after_quality,
    route_after_retry,
    route_by_data_type,
)
from src.graph.state import PipelineState
from src.guardrails.injection_check import injection_check_parsed_node
from src.guardrails.input_guardrail import input_guardrail_node
from src.guardrails.output_guardrail import output_guardrail_node
from src.persistence.writer import persistence_node


def build_graph() -> StateGraph:
    graph = StateGraph(PipelineState)

    graph.add_node("input_guardrail", input_guardrail_node)
    graph.add_node("discovery", discovery_node)
    graph.add_node("extraction_parse", extraction_parse_node)
    graph.add_node("injection_check_parsed", injection_check_parsed_node)
    graph.add_node("extraction_structure", extraction_structure_node)
    graph.add_node("quality", quality_node)
    graph.add_node("retry_handler", retry_handler_node)
    graph.add_node("annotation", annotation_node)
    graph.add_node("governance", governance_node)
    graph.add_node("output_guardrail", output_guardrail_node)
    graph.add_node("manual_review", manual_review_node)
    graph.add_node("persistence", persistence_node)

    graph.add_edge(START, "input_guardrail")

    graph.add_conditional_edges(
        "input_guardrail", route_after_input_guardrail,
        {"discovery": "discovery", "manual_review": "manual_review", "persistence": "persistence"}
    )
    graph.add_conditional_edges(
        "discovery", route_by_data_type,
        {"extraction_parse": "extraction_parse", "quality": "quality", "persistence": "persistence"}
    )
    graph.add_conditional_edges(
        "extraction_parse", route_after_extraction_parse,
        {"injection_check_parsed": "injection_check_parsed", "persistence": "persistence"}
    )
    graph.add_conditional_edges(
        "injection_check_parsed", route_after_injection_check,
        {"extraction_structure": "extraction_structure", "manual_review": "manual_review", "persistence": "persistence"}
    )
    graph.add_conditional_edges(
        "extraction_structure", route_after_extraction_structure,
        {"quality": "quality", "persistence": "persistence"}
    )
    graph.add_conditional_edges(
        "quality", route_after_quality,
        {"retry": "retry_handler", "manual_review": "manual_review",
         "annotation": "annotation", "governance": "governance", "persistence": "persistence"}
    )
    graph.add_conditional_edges(
        "retry_handler", route_after_retry,
        {"extraction_parse": "extraction_parse", "manual_review": "manual_review"}
    )
    graph.add_conditional_edges(
        "annotation", route_after_annotation,
        {"governance": "governance", "persistence": "persistence"}
    )
    graph.add_conditional_edges(
        "governance", route_after_governance,
        {"output_guardrail": "output_guardrail", "persistence": "persistence"}
    )
    graph.add_conditional_edges(
        "output_guardrail", route_after_output_guardrail,
        {"persistence": "persistence", "manual_review": "manual_review"}
    )
    graph.add_conditional_edges(
        "manual_review", route_after_manual_review,
        {"governance": "governance", "annotation": "annotation", "persistence": "persistence"}
    )
    graph.add_edge("persistence", END)

    return graph


def compile_graph(checkpointer=None):
    """A checkpointer is required for interrupt()/Command(resume=...) to work:
    MemorySaver in tests, SqliteSaver (see compile_api_graph) in the API."""
    return build_graph().compile(checkpointer=checkpointer)


def get_sqlite_checkpointer(path: str | None = None):
    """SqliteSaver on data/langgraph_checkpoints.db — LangGraph's own schema,
    kept entirely separate from pipeline.db."""
    import sqlite3
    from pathlib import Path

    from langgraph.checkpoint.sqlite import SqliteSaver

    from src.config.settings import settings

    db_path = Path(path or settings.DB_CHECKPOINT_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return SqliteSaver(conn)


def compile_api_graph(checkpoint_path: str | None = None):
    return compile_graph(checkpointer=get_sqlite_checkpointer(checkpoint_path))
