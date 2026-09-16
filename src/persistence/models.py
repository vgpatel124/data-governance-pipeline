"""ORM tables for data/pipeline.db.

Explicitly excluded from every column: raw_text, extracted_data,
structured_records, final_content, and any raw or masked PII value.
"""
from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    document_id: Mapped[str] = mapped_column(String, primary_key=True)
    file_name: Mapped[str | None] = mapped_column(String)
    dataset_type: Mapped[str | None] = mapped_column(String)
    data_type: Mapped[str | None] = mapped_column(String)
    sensitivity_flag: Mapped[bool | None] = mapped_column(Boolean)
    quality_status: Mapped[str | None] = mapped_column(String)
    retry_count: Mapped[int | None] = mapped_column(Integer)
    needs_annotation: Mapped[bool | None] = mapped_column(Boolean)
    annotation_model_used: Mapped[str | None] = mapped_column(String)
    extraction_model_used: Mapped[str | None] = mapped_column(String)
    pii_findings_count: Mapped[int | None] = mapped_column(Integer)
    # JSON list of {"entity_type", "action"} only — never masked_value/location.
    pii_findings_summary: Mapped[str | None] = mapped_column(Text)
    final_status: Mapped[str | None] = mapped_column(String, index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    human_reviewer_notes: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[str | None] = mapped_column(String, index=True)  # "pending" | "resolved" | None
    # Execution lifecycle, independent of review_status:
    # "queued" -> "running" -> "paused" (awaiting review) | "done" (terminal write)
    run_status: Mapped[str | None] = mapped_column(String, index=True)
    review_issue_summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(String)
    completed_at: Mapped[str | None] = mapped_column(String)
    processing_time_ms: Mapped[float | None] = mapped_column(Float)


class PipelineEvent(Base):
    __tablename__ = "pipeline_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(
        String, ForeignKey("pipeline_runs.document_id", ondelete="CASCADE"), index=True
    )
    node_name: Mapped[str] = mapped_column(String)
    timestamp: Mapped[str] = mapped_column(String)
    duration_ms: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String)
