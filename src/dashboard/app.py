"""Streamlit dashboard for the data governance pipeline.

Run (API must be running):  uv run streamlit run src/dashboard/app.py
Talks only to the FastAPI service (API_URL, default http://localhost:8000);
never opens the databases directly. Everything shown is already value-free.
"""
import os
import time
from pathlib import Path

import altair as alt
import httpx
import pandas as pd
import streamlit as st
import yaml

API_URL = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
SCHEMAS_PATH = Path(__file__).resolve().parents[1] / "config" / "validation_schemas.yaml"
UPLOAD_TYPES = ["csv", "json", "pdf", "png", "jpg", "jpeg"]

# Reserved status palette (fixed across light/dark). Every chart and table pairs
# a color with its written status name, so meaning is never carried by color alone.
STATUS_COLOR = {
    "completed": "#0ca30c",      # good
    "manual_review": "#fab219",  # warning
    "rejected": "#ec835a",       # serious
    "failed": "#d03b3b",         # critical
}
# Single-series magnitude (PII by type): categorical slot 1, stepped per theme.
SERIES_1 = {"light": "#2a78d6", "dark": "#3987e5"}
GRID = {"light": "#e1e0d9", "dark": "#2c2c2a"}
MUTED_INK = "#898781"
BAND_STEP_PX = 30  # height per bar band; chart height grows with category count

st.set_page_config(page_title="Data Governance Pipeline", layout="wide")


# ---------- helpers ----------

def theme_mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:  # older Streamlit / no theme info
        return "light"


def api(method: str, path: str, timeout: float = 30.0, **kwargs):
    """Returns (data, error_message)."""
    try:
        r = httpx.request(method, f"{API_URL}{path}", timeout=timeout, **kwargs)
    except httpx.HTTPError as e:
        return None, f"Could not reach the API at {API_URL} ({type(e).__name__}). Is `uvicorn src.api.main:app` running?"
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        return None, f"API error {r.status_code}: {detail}"
    return r.json(), None


@st.cache_data
def dataset_types() -> list[str]:
    with open(SCHEMAS_PATH, encoding="utf-8") as f:
        return list((yaml.safe_load(f) or {}).keys())


def status_label(status) -> str:
    """Readable label for a final status. None/NaN (no final status yet) = pending."""
    if status is None or (isinstance(status, float) and pd.isna(status)) or status == "pending_review":
        return "pending review"
    return status.replace("_", " ") if status in STATUS_COLOR else str(status)


def format_duration(ms) -> str:
    if ms is None:
        return "—"
    if ms >= 60_000:
        return f"{ms / 60_000:.1f} min"
    if ms >= 1000:
        return f"{ms / 1000:.1f} s"
    return f"{ms:.0f} ms"


def show_result(result: dict) -> None:
    status = result.get("status")
    if status == "pending_review":
        st.warning(f"**{status_label(status)}** — the pipeline paused for a human decision "
                   f"({(result.get('reason') or '').replace('_', ' ')}). Open the **Review queue** page to approve or reject.")
        st.caption("Issue summary")
        st.text(result.get("issue_summary", ""))
    elif status == "completed":
        st.success(f"**{status_label(status)}**")
    elif status == "manual_review":
        st.warning(f"**{status_label(status)}**" +
                   (f" — {result['rejection_reason']}" if result.get("rejection_reason") else ""))
    else:
        st.error(f"**{status_label(status)}**" +
                 (f" — {result['error_message']}" if result.get("error_message") else ""))
    with st.expander("Run summary (counts only, no content)"):
        st.json(result)


def bar_chart(df: pd.DataFrame, category: str, value: str, color_scale: alt.Scale, mode: str):
    """Thin horizontal bars, 4px rounded data-ends, hairline grid, hover tooltip.
    Height is a per-band step so every category label always has room."""
    chart = (
        alt.Chart(df)
        .mark_bar(cornerRadiusEnd=4, size=16)
        .encode(
            y=alt.Y(f"{category}:N", sort=None, title=None,
                    axis=alt.Axis(labelLimit=220, labelOverlap=False, ticks=False, domain=False,
                                  labelColor=MUTED_INK, labelPadding=8)),
            x=alt.X(f"{value}:Q", title=None,
                    axis=alt.Axis(tickMinStep=1, format="d", grid=True, gridColor=GRID[mode], gridDash=[],
                                  domain=False, ticks=False, labelColor=MUTED_INK)),
            color=alt.Color(f"{category}:N", scale=color_scale, legend=None),
            tooltip=[alt.Tooltip(f"{category}:N", title=category.replace("_", " ")),
                     alt.Tooltip(f"{value}:Q", title=value)],
        )
        .properties(height=alt.Step(BAND_STEP_PX))
        .configure_view(strokeWidth=0)
    )
    st.altair_chart(chart, width="stretch")


# ---------- pages ----------

def page_upload():
    st.header("Upload a file")
    with st.form("ingest", clear_on_submit=False):
        file = st.file_uploader("File", type=UPLOAD_TYPES)
        col1, col2 = st.columns([2, 1])
        dataset_type = col1.selectbox("Dataset type", dataset_types(),
                                      help="Selects the validation schema. Use `documents` for PDFs and images.")
        needs_annotation = col2.checkbox("Needs annotation", value=False)
        submitted = st.form_submit_button("Run pipeline", type="primary")

    if submitted:
        if file is None:
            st.error("Choose a file first.")
            return
        ack, err = api(
            "POST", "/ingest", timeout=60,
            files={"file": (file.name, file.getvalue(), file.type or "application/octet-stream")},
            data={"needs_annotation": str(needs_annotation).lower(), "dataset_type": dataset_type},
        )
        if err:
            st.error(err)
            return
        st.session_state["watch_document_id"] = ack["document_id"]

    document_id = st.session_state.get("watch_document_id")
    if document_id:
        _watch_run(document_id)


RUN_STATUS_LABEL = {
    "queued": "queued",
    "running": "running",
    "paused": "awaiting review",
    "done": "finished",
}


def _watch_run(document_id: str, poll_seconds: float = 1.0, timeout_s: float = 900.0):
    """Ingestion is asynchronous: poll /status until the background run finishes."""
    st.caption(f"Document ID: {document_id}")
    placeholder = st.empty()
    deadline = time.time() + timeout_s
    row = None
    while time.time() < deadline:
        row, err = api("GET", f"/status/{document_id}")
        if err:
            placeholder.error(err)
            return
        run_status = row.get("run_status")
        if run_status in ("paused", "done"):
            break
        placeholder.info(f"{RUN_STATUS_LABEL.get(run_status, run_status)} — the pipeline is working on this file.")
        time.sleep(poll_seconds)
    else:
        placeholder.warning("Still running. Check the Recent runs page for the outcome.")
        return

    placeholder.empty()
    if row.get("review_status") == "pending":
        st.warning("**Pending review** — the pipeline paused for a human decision. "
                   "Open the **Review queue** page to approve or reject.")
        st.caption("Issue summary")
        st.text(row.get("review_issue_summary") or "")
    elif row.get("final_status") == "completed":
        st.success("**Completed**")
    elif row.get("final_status") == "manual_review":
        st.warning("**Manual review**")
    else:
        st.error(f"**{status_label(row.get('final_status'))}**" +
                 (f" — {row['error_message']}" if row.get("error_message") else ""))
    with st.expander("Run summary (counts only, no content)"):
        st.json(row)


def page_review_queue():
    st.header("Review queue")
    if msg := st.session_state.pop("review_message", None):
        kind, text = msg
        getattr(st, kind)(text)

    pending, err = api("GET", "/reviews/pending")
    if err:
        st.error(err)
        return
    if not pending:
        st.info("No documents are awaiting review.")
        return

    st.caption(f"{len(pending)} document(s) awaiting a decision. Summaries describe issues only — never field values.")
    for item in pending:
        doc_id = item["document_id"]
        with st.container(border=True):
            st.subheader(item.get("file_name") or "(unnamed file)")
            st.caption(f"Document ID: {doc_id}")
            st.text(item.get("review_issue_summary") or "")
            with st.form(f"review-{doc_id}"):
                # No default: approval must be an explicit choice, never a one-click pass-through.
                decision = st.radio("Decision", ["approve", "reject"], index=None, horizontal=True,
                                    format_func=lambda d: d.capitalize(),
                                    key=f"decision-{doc_id}")
                notes = st.text_area("Reviewer notes", key=f"notes-{doc_id}",
                                     placeholder="Why are you approving or rejecting this document?")
                if st.form_submit_button("Submit decision"):
                    if decision is None:
                        st.error("Choose Approve or Reject before submitting.")
                    else:
                        with st.spinner("Resuming the pipeline…"):
                            result, rerr = api("POST", f"/review/{doc_id}", timeout=600,
                                               json={"decision": decision, "notes": notes})
                        if rerr:
                            st.session_state["review_message"] = ("error", rerr)
                        elif result.get("status") == "pending_review":
                            st.session_state["review_message"] = (
                                "warning", f"{item.get('file_name')}: resumed, then paused again — "
                                           f"{(result.get('reason') or '').replace('_', ' ')}.")
                        else:
                            st.session_state["review_message"] = (
                                "success", f"{item.get('file_name')}: {status_label(result.get('final_status'))}")
                        st.rerun()


def page_metrics():
    st.header("Metrics")
    st.button("Refresh")
    data, err = api("GET", "/dashboard-data")
    if err:
        st.error(err)
        return

    mode = theme_mode()
    total = data["total_processed"]

    # Two rows so tile values never clip at narrow widths.
    row1 = st.columns(3)
    row1[0].metric("Processed", f"{total:,}", help="Documents with a final status")
    row1[1].metric("Pending review", f"{data['pending_review_count']:,}", help="Paused, awaiting a human decision")
    row1[2].metric("Avg time", format_duration(data.get("avg_processing_time_ms")),
                   help="Average started→completed time per document. For reviewed documents this "
                        "includes the time spent waiting for the human decision.")
    row2 = st.columns(3)
    row2[0].metric("Retry rate", f"{data['retry_rate']:.1%}",
                   help="Share of processed documents that needed an extraction retry")
    row2[1].metric("Review rate", f"{data['manual_review_rate']:.1%}",
                   help="Share of processed documents ending in manual review")

    if total == 0:
        st.info("No finished runs yet. Upload a file to get started.")
        return

    left, right = st.columns(2)
    with left:
        st.subheader("Final status")
        st.caption("Technical failures are counted separately from manual review.")
        statuses = list(STATUS_COLOR)
        df = pd.DataFrame({
            "status": [status_label(s) for s in statuses],
            "documents": [data["status_breakdown"].get(s, 0) for s in statuses],
        })
        scale = alt.Scale(domain=list(df["status"]), range=[STATUS_COLOR[s] for s in statuses])
        bar_chart(df, "status", "documents", scale, mode)
        with st.expander("Table view"):
            st.dataframe(df, hide_index=True, width="stretch")

    with right:
        st.subheader("PII findings by type")
        st.caption("Counts of detected entities across all processed documents.")
        pii = data.get("pii_findings_by_type") or {}
        if not pii:
            st.info("No PII findings recorded yet.")
        else:
            df = (pd.DataFrame({"entity_type": list(pii), "findings": list(pii.values())})
                  .sort_values("findings", ascending=False, kind="stable"))
            scale = alt.Scale(range=[SERIES_1[mode]])
            bar_chart(df, "entity_type", "findings", scale, mode)
            with st.expander("Table view"):
                st.dataframe(df, hide_index=True, width="stretch")


def page_recent_runs():
    st.header("Recent runs")
    limit = st.slider("Rows", min_value=10, max_value=500, value=50, step=10)
    runs, err = api("GET", "/runs", params={"limit": limit})
    if err:
        st.error(err)
        return
    if not runs:
        st.info("No runs yet.")
        return
    df = pd.DataFrame(runs)
    df["final_status"] = df["final_status"].map(status_label)
    df["run_status"] = df["run_status"].map(lambda s: RUN_STATUS_LABEL.get(s, s or "—"))
    columns = ["started_at", "file_name", "final_status", "run_status", "dataset_type", "data_type", "quality_status",
               "retry_count", "sensitivity_flag", "extraction_model_used", "annotation_model_used",
               "pii_findings_count", "review_status", "processing_time_ms", "document_id"]
    st.dataframe(df[[c for c in columns if c in df.columns]], hide_index=True, width="stretch")


def page_query():
    st.header("Query governed data")
    st.caption("Ask in plain English. Only data that completed the pipeline **and** passed the output "
               "guardrail is queryable — human-overridden documents are deliberately excluded.")

    tables, err = api("GET", "/governed/tables")
    if err:
        st.error(err)
        return
    by_type = {t["dataset_type"]: t for t in tables}
    if not by_type:
        st.info("No governed tables yet.")
        return

    cols = st.columns(len(by_type))
    for col, (dataset_type, info) in zip(cols, by_type.items()):
        col.metric(dataset_type, f"{info['row_count']:,}", help=f"Rows in {info['table']}")

    with st.form("query"):
        dataset_type = st.selectbox("Dataset", list(by_type), index=None,
                                    placeholder="Choose the dataset to query")
        question = st.text_area("Question", placeholder="e.g. how many customers are there?")
        submitted = st.form_submit_button("Run query", type="primary")

    if submitted:
        if dataset_type is None or not question.strip():
            st.error("Choose a dataset and enter a question.")
            return
        with st.spinner("Generating SQL and running it…"):
            result, qerr = api("POST", "/query", timeout=120,
                               json={"question": question, "dataset_type": dataset_type})
        if qerr:
            st.error(qerr)
            return
        st.session_state["last_query_result"] = result

    result = st.session_state.get("last_query_result")
    if not result:
        return

    st.caption(f"Generated SQL (read-only) — ran against `{result['table']}`")
    st.code(result.get("sql") or "", language="sql")

    if result.get("blocked"):
        st.error(f"Query blocked: {result.get('reason')}")
        st.caption("The SQL guardrail parses every generated query and allows only read-only SELECTs "
                   "over governed tables.")
        return

    rows, columns = result.get("rows") or [], result.get("columns") or []
    if not rows:
        st.info("Query ran successfully and returned no rows.")
        return
    st.dataframe(pd.DataFrame(rows, columns=columns), hide_index=True, width="stretch")
    limit = result.get("row_limit")
    suffix = f" (capped at {limit})" if limit and result["row_count"] >= limit else ""
    st.caption(f"{result['row_count']} row(s){suffix} · SQL generated by the {result.get('model_used')} model")


PAGES = {
    "Upload": page_upload,
    "Review queue": page_review_queue,
    "Query": page_query,
    "Metrics": page_metrics,
    "Recent runs": page_recent_runs,
}

st.sidebar.title("Governance pipeline")
choice = st.sidebar.radio("Page", list(PAGES), label_visibility="collapsed")
st.sidebar.caption(f"API: {API_URL}")
PAGES[choice]()
