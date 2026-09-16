# PLAN.md — Multi-Agent Data Curation & Governance Pipeline (V1)

**Instructions for the coding agent (Claude Code / Cursor):** Execute this plan stage by stage, in order. After completing each stage, append a summary to `PROGRESS.md` (create it if it doesn't exist) covering: what was built, files created/modified, any decisions you had to make that weren't explicitly specified here, and what's next. Do not skip ahead. Do not ask the user to review between stages unless something in this plan is genuinely ambiguous or a dependency fails to install — in that case, make the most reasonable decision, note it clearly in `PROGRESS.md` under a "Deviations" heading, and continue.

## Authoritative References

When implementation details, API usage, or framework behavior are uncertain, consult the official documentation for the relevant technology rather than relying on memory or possibly outdated examples — this matters most for LangGraph specifically, since `interrupt()`, `Command(resume=...)`, and the checkpointer API (all used in this plan) are newer surface area with a real history of breaking changes across versions. Use these references to resolve implementation and API-level uncertainty. Do not change the architecture, routing, security requirements, or behavior explicitly defined in this `PLAN.md` based solely on documentation examples.

- LangGraph: https://langchain-ai.github.io/langgraph/
- LangChain: https://python.langchain.com/docs/
- `unstructured` library (OCR/parsing, used in `extraction_parse_node` — the `strategy="fast"`/`"hi_res"` parameter the retry design depends on lives here): https://docs.unstructured.io/
- LangChain Ollama integration (local-model routing): https://python.langchain.com/docs/integrations/providers/ollama/
- Groq API / LangChain Groq integration: https://console.groq.com/docs/
- FastAPI: https://fastapi.tiangolo.com/
- Langfuse: https://langfuse.com/docs/
- SQLAlchemy: https://docs.sqlalchemy.org/

---

## 0. Project setup

```bash
mkdir data-governance-pipeline && cd data-governance-pipeline
uv init --python 3.11
uv add langgraph langgraph-checkpoint-sqlite langchain langchain-groq langchain-ollama \
       fastapi "uvicorn[standard]" streamlit pydantic pandas \
       pyyaml python-magic unstructured[pdf] pillow \
       langfuse sqlalchemy python-dotenv httpx
uv add --dev pytest pytest-asyncio faker
```

**Two separate SQLite databases, two separate concerns — don't merge them:**
- `data/pipeline.db` (Section 4) — business-level outcomes for the dashboard: one row per document, written by `persistence_node`.
- `data/langgraph_checkpoints.db` — LangGraph's own execution-state persistence, required for `interrupt()`/`Command(resume=...)` to work across two separate HTTP requests. Managed entirely by `langgraph-checkpoint-sqlite`'s own schema — don't hand-roll around it or try to share tables with `pipeline.db`.
- Tests use `from langgraph.checkpoint.memory import MemorySaver` (in-memory, resets cleanly between test runs, no filesystem I/O). The running API service uses `from langgraph.checkpoint.sqlite import SqliteSaver` pointed at `data/langgraph_checkpoints.db`.

Create `.env.example`:
```
GROQ_API_KEY=
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2:3b
GROQ_MODEL=llama-3.3-70b-versatile
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
DB_PATH=./data/pipeline.db
DB_CHECKPOINT_PATH=./data/langgraph_checkpoints.db
# MAX_RETRIES = number of RETRY attempts allowed AFTER the original extraction
# attempt. Does NOT include the original. Router uses retry_count <= max_retries,
# so MAX_RETRIES=1 means exactly 1 retry (2 total attempts: fast, then hi_res)
# before routing to manual_review. Locked at 1 for V1 because only two OCR
# strategies (fast, hi_res) are defined — a value above 1 would repeat the
# hi_res strategy on the second retry instead of trying something new, which
# defeats the point of strategy-varying retries. Raise this only once a third
# distinct strategy (e.g. a vision-LLM fallback) exists.
MAX_RETRIES=1
```

Defaults locked for this build (swappable later via `.env`, don't ask the user, just use these):
- **External model:** Groq (`llama-3.3-70b-versatile`), via `langchain-groq`.
- **Local model:** Ollama running `llama3.2:3b`, via `langchain-ollama`.
- **OCR/parsing:** `unstructured` library (`partition` function) — handles PDF/image/doc uniformly.
- **Observability:** Langfuse — open-source, self-hostable, free tier covers this project. **Trace payloads must never include `raw_text`, `extracted_data`, or `structured_records` — log node name, status, and timing only.**
- **Persistence:** SQLite via SQLAlchemy — no external DB server needed. **Never persists raw content or raw PII — see Section 4.**

### Folder structure to create

```
data-governance-pipeline/
├── pyproject.toml
├── .env.example
├── README.md
├── PROGRESS.md
├── PLAN.md                        (this file)
├── src/
│   ├── __init__.py
│   ├── config/
│   │   ├── __init__.py
│   │   ├── settings.py             # loads .env
│   │   └── validation_schemas.yaml # required-field rules, keyed by dataset_type
│   ├── graph/
│   │   ├── __init__.py
│   │   ├── state.py                 # PipelineState TypedDict
│   │   ├── graph.py                 # StateGraph assembly: nodes + edges
│   │   └── routers.py               # all conditional-edge functions
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── discovery.py
│   │   ├── extraction.py            # extraction_parse_node + extraction_structure_node
│   │   ├── quality.py
│   │   ├── retry.py
│   │   ├── annotation.py
│   │   ├── governance.py
│   │   └── manual_review.py
│   ├── guardrails/
│   │   ├── __init__.py
│   │   ├── input_guardrail.py
│   │   ├── injection_check.py
│   │   └── output_guardrail.py
│   ├── pii/
│   │   ├── __init__.py
│   │   └── rules.py                 # regex + Luhn validators, per-finding action logic
│   ├── models/
│   │   ├── __init__.py
│   │   └── llm_clients.py           # get_local_client(), get_external_client()
│   ├── persistence/
│   │   ├── __init__.py
│   │   ├── db.py                    # SQLAlchemy engine/session
│   │   ├── models.py                # ORM tables
│   │   └── writer.py                # write_pipeline_result(state)
│   ├── observability/
│   │   ├── __init__.py
│   │   └── tracing.py               # Langfuse handler setup, content-field exclusion
│   ├── api/
│   │   ├── __init__.py
│   │   └── main.py                  # FastAPI app
│   └── dashboard/
│       └── app.py                   # Streamlit app
├── data/
│   ├── synthetic/                   # generated test fixtures
│   └── uploads/                     # runtime uploaded files, gitignored
├── tests/
│   ├── __init__.py
│   ├── test_state.py
│   ├── test_discovery.py
│   ├── test_extraction.py
│   ├── test_annotation.py
│   ├── test_quality.py
│   ├── test_governance.py
│   ├── test_routers.py
│   └── test_graph_integration.py
└── scripts/
    └── generate_synthetic_data.py
```

---

## 1. State schema (`src/graph/state.py`)

```python
import operator
from typing import TypedDict, Literal, Optional, List, Dict, Any, Annotated
from datetime import datetime

class NodeTraceEntry(TypedDict):
    node_name: str
    timestamp: str
    duration_ms: float
    status: Literal["ok", "failed"]

class PIIFinding(TypedDict):
    entity_type: str                 # "email", "phone", "pan", "aadhaar", "gstin", "card"
    masked_value: Optional[str]      # e.g. "jo****@example.com" — NEVER the raw match.
                                      # For fully-redacted categories (PAN/Aadhaar/card),
                                      # leave this as None; store only entity_type + location.
    location: str                    # field name or row/char offset
    action: Literal["masked", "redacted", "allowed"]   # decided PER FINDING, not per document

class PipelineState(TypedDict):
    # --- Ingestion (set once, at graph invocation) ---
    document_id: str
    file_path: str
    file_name: str
    needs_annotation: bool          # caller-supplied objective, NOT inferred by any agent
    dataset_type: str               # caller-supplied, e.g. "customer_records", "invoices" —
                                     # selects which schema quality_node validates against.
                                     # Same "caller states it, no agent infers it" pattern as
                                     # needs_annotation.

    # --- Input guardrail ---
    input_valid: Optional[bool]
    input_injection_flag: Optional[bool]
    rejection_reason: Optional[str]

    # --- Discovery output ---
    data_type: Optional[Literal["structured", "unstructured"]]
    sensitivity_flag: Optional[bool]

    # --- Extraction output (unstructured path only) ---
    # raw_text = untouched OCR/parse output. extracted_data = LLM-structured result.
    # Kept separate because: (a) quality validation runs against extracted_data, not
    # raw_text, and (b) the injection check MUST run on raw_text, BEFORE any LLM call,
    # or it's checking after the fact and the LLM has already read the payload.
    raw_text: Optional[str]
    extracted_data: Optional[Dict[str, Any]]
    extraction_model_used: Optional[Literal["local", "external"]]
    parsed_injection_flag: Optional[bool]

    # --- Structured data (loaded directly, no LLM, no raw/extracted split needed) ---
    structured_records: Optional[List[Dict[str, Any]]]

    # --- Quality ---
    quality_status: Optional[Literal["PASS", "FAIL"]]
    quality_issues: List[str]        # describe the problem; never embed the raw field value
    # retry_count = number of RETRIES made so far (excludes the original attempt).
    # max_retries = retries allowed after the original attempt. Router checks
    # retry_count <= max_retries, so max_retries=1 means exactly 1 retry (2 total
    # extraction attempts: fast, then hi_res) before manual_review. Do NOT use a
    # strict "<" here — that silently changes the semantic to "N total attempts"
    # instead of "N retries," which is what caused the original version of this
    # file to document a value that didn't match its own router code.
    retry_count: int
    max_retries: int

    # --- Annotation (optional, only if needs_annotation) ---
    annotation_results: Optional[Dict[str, Any]]
    annotation_model_used: Optional[Literal["local", "external"]]

    # --- Governance ---
    pii_findings: List[PIIFinding]   # per-finding action, masked values only, never raw
    final_content: Optional[Any]     # governed output — the ONLY content field allowed
                                      # to reach persistence

    # --- Output guardrail ---
    output_guardrail_passed: Optional[bool]
    policy_violations: List[str]

    # --- Terminal status ---
    # "failed" = technical/infrastructure error (model unreachable, timeout, crash) —
    # kept distinct from "manual_review" (a data/business judgment call) so the two
    # don't pollute the same dashboard metric.
    final_status: Optional[Literal["completed", "manual_review", "rejected", "failed"]]
    error_message: Optional[str]     # set only when final_status == "failed"
    # Set by a human reviewer on resume from interrupt() — approval or rejection
    # reasoning. On reject, final_status stays "manual_review" (NOT "rejected" —
    # that value is reserved for automated input-guardrail rejections; reusing it
    # here would conflate two different populations in the dashboard's status
    # breakdown). This field is what distinguishes "reviewed and declined" from
    # "reviewed and approved" when final_status alone doesn't say so.
    human_reviewer_notes: Optional[str]

    # --- Timing / tracing ---
    started_at: str
    completed_at: Optional[str]
    # Annotated with operator.add: multiple nodes each contribute entries over the
    # course of ONE graph run (including nodes revisited via the retry loop). Without
    # this reducer, LangGraph's default behavior OVERWRITES the field on every node
    # return — you'd silently end up with only the last node's single entry instead
    # of the full trace. Nodes should return {"node_trace": [my_single_entry]}, a
    # ONE-ITEM list — never read-modify-write the existing accumulated list
    # themselves; the reducer does the concatenation.
    node_trace: Annotated[List[NodeTraceEntry], operator.add]
```

Initialize on graph invocation: `retry_count=0`, `max_retries=int(settings.MAX_RETRIES)`, `quality_issues=[]`, `pii_findings=[]`, `policy_violations=[]`, `node_trace=[]`, `human_reviewer_notes=None`, `started_at=datetime.utcnow().isoformat()`, `document_id=str(uuid4())`. `dataset_type` and `needs_annotation` come from the caller at ingestion — never defaulted or inferred.

Note: `human_reviewer_notes: Optional[str]` — no `= None` in the class body. `TypedDict` fields are annotations only; a default there isn't part of the `TypedDict` mechanism and won't do anything at instance-construction time. The actual default is set here, alongside every other field's initial value.

Every node appends exactly one `NodeTraceEntry` for its own execution (via the reducer) before returning.

---

## 2. Nodes (one file per agent under `src/agents/` and `src/guardrails/`)

Each node is a plain function `def node_name(state: PipelineState) -> dict` returning only the keys it updates. **Every node that calls an external service (LLM, OCR library) wraps its core logic in try/except.** On exception: return `{"final_status": "failed", "error_message": str(e), "node_trace": [entry with status="failed"]}` and nothing else — do not also set the node's normal success fields.

### `input_guardrail_node` (`src/guardrails/input_guardrail.py`)
- Validate file exists, extension is one of allowed types (`.csv`, `.json`, `.pdf`, `.png`, `.jpg`, `.jpeg`).
- Validate file size under a configured max (e.g., 10MB).
- Raw injection heuristic — **scope differs by file type**: for `.csv`/`.json`, scan the actual text content for known injection patterns ("ignore previous instructions", "system:", etc.). For `.pdf`/`.png`/`.jpg`/`.jpeg`, binary formats aren't meaningfully regex-scannable before parsing — skip content scanning here and check only the filename for injection-like strings as a cheap sanity check. The authoritative check for unstructured content happens later, on `raw_text`, before it reaches any LLM.
- Sanitize filename/path components before logging (strip control characters, path traversal sequences).
- Sets: `input_valid`, `input_injection_flag`, `rejection_reason` (if invalid).

### `discovery_node` (`src/agents/discovery.py`)
- Determine `data_type`: `.csv`/`.json` → `"structured"`; `.pdf`/`.png`/`.jpg`/`.jpeg` → `"unstructured"`.
- **Structured files: load the CSV/JSON once and use it for both purposes** — check column headers against the sensitivity keyword list below, and populate `structured_records` with the parsed rows. Don't load the file twice.
- Determine `sensitivity_flag`:
  - **Structured:** column headers checked against a keyword list (`email`, `phone`, `pan`, `aadhaar`, `ssn`, `salary`, `dob`, `account`, `card`) — case-insensitive substring match. Any match → `True`.
  - **Unstructured:** cheap heuristic only — do NOT run OCR here, that's Extraction's job and would duplicate work. Check filename/path metadata against the same keyword list plus filename patterns (`invoice`, `medical`, `patient`, `passport`, `id_card`). Default to `True` (fail-safe) if no signal is available.
- File parsing can throw (malformed CSV, bad encoding) — wrap in try/except per the node preamble above; on failure, `final_status="failed"` as usual.
- Sets: `data_type`, `sensitivity_flag`, and — **for structured files only** — `structured_records` (the parsed rows; left `None` for unstructured, where the equivalent data lives in `extracted_data` after Extraction runs instead).

### `extraction_parse_node` (`src/agents/extraction.py`) — unstructured path only
- Run `unstructured.partition.auto.partition(file_path)` to get raw text. **No LLM call in this node.**
- Implementation note: for the retry loop to actually do something different on a second attempt (not just repeat the same call), vary the parse strategy by `retry_count` — e.g., use `strategy="fast"` on the first attempt and `strategy="hi_res"` on retries. A retry that reruns the identical deterministic call is a no-op; don't build that.
- Sets: `raw_text`.

### `injection_check_parsed_node` (`src/guardrails/injection_check.py`)
- Run the injection heuristic against `raw_text` — **before** it is sent to any LLM. This is what catches an injection payload hidden inside document content that the file-level check in Input Guardrail can't see (binary formats aren't scanned there).
- Sets: `parsed_injection_flag`.

### `extraction_structure_node` (`src/agents/extraction.py`)
- Select model client via `sensitivity_flag`: `get_local_client()` if `True`, `get_external_client()` if `False` (from `src/models/llm_clients.py`).
- Call the selected model to structure `raw_text` into a target schema (define a simple target schema for your synthetic dataset, e.g., `{title, date, amount, parties}` — adjust to whatever your synthetic unstructured docs represent).
- Sets: `extracted_data`, `extraction_model_used`.

### `quality_node` (`src/agents/quality.py`)
- Load the schema from `src/config/validation_schemas.yaml` keyed by `dataset_type`.
- **Structured:** validate every record in `structured_records` against required fields (non-null) and format rules (regex per field). **Rule: any single invalid required field/row → file-level `quality_status = "FAIL"`.** No thresholds in V1.
- **Unstructured:** validate `extracted_data` against the same kind of required-field schema.
- Sets: `quality_status`, `quality_issues` (describe the problem, e.g., `"row 42: email field missing"` — never embed the actual field value, in case the value itself is sensitive).

### `retry_handler_node` (`src/agents/retry.py`)
- Only reached for `data_type == "unstructured"` and `quality_status == "FAIL"`.
- Increments `retry_count`. No other state change — the routing decision happens in the conditional edge.

### `manual_review_node` (`src/agents/manual_review.py`)
Reached from five different places, but **only three of them are human-in-the-loop resumable.** The other two stay hard-terminal — deliberately. "Approve" has no coherent meaning for a file that failed basic validation (Discovery never ran, nothing to hand off to), and a one-click approve on a prompt-injection flag would bypass the exact control that flag exists for. Don't make every entry point interruptible just because you can.

**Interruptible (calls `interrupt()`, waits for a human decision):**
- Structured quality FAIL (`quality_status == "FAIL"`, `data_type == "structured"`)
- Unstructured retries exhausted (`quality_status == "FAIL"`, `data_type == "unstructured"`, `retry_count > max_retries`)
- Output guardrail failure (`output_guardrail_passed == False`)

For these: call `interrupt({"document_id": ..., "reason": ..., "quality_issues": state["quality_issues"], "policy_violations": state["policy_violations"]})` — pass the issue summary, never raw content. On resume via `Command(resume={"decision": "approve"|"reject", "notes": "..."})`:
- **Approve:** set `human_reviewer_notes`, then branch by what triggered the review:
  - **From a quality failure:** also set `quality_status = "PASS"` (the override).
  - **From an output guardrail failure:** also set `final_status = "completed"`. Leave `output_guardrail_passed` itself untouched — it stays `False`, an accurate record of what the guardrail actually found. The override is that a human looked at that specific finding and let it through anyway, which `human_reviewer_notes` records; it is not a retroactive claim that the guardrail found nothing. Skipping `final_status = "completed"` here was a bug in the previous version of this plan — the document would reach `persistence_node` with `final_status` still unset (`None`), which breaks every dashboard query grouped by status.
- **Reject:** set `final_status = "manual_review"` (not `"rejected"` — see the state schema note) and `human_reviewer_notes`.

**Hard-terminal, no interrupt (input guardrail rejection, parsed-injection-flag trip):**
- Sets `final_status = "manual_review"` directly, same as before this change. No pause, no resume offered.

In both cases, not reached at all if `final_status == "failed"` upstream — that routes straight to persistence (Section 3).

### `annotation_node` (`src/agents/annotation.py`) — only if `needs_annotation == True`
- **Operates on `extracted_data` if `data_type == "unstructured"`, or `structured_records` if `data_type == "structured"`** — same branching pattern as Quality and Governance. Don't guess which field to read; check `data_type` explicitly.
- Select model client via `sensitivity_flag` (same pattern as Extraction).
- Run the enrichment/labeling task for your synthetic dataset (e.g., category tagging with a confidence score).
- Sets: `annotation_results`, `annotation_model_used`.

### `governance_node` (`src/agents/governance.py`)
- **Operates on `extracted_data` if `data_type == "unstructured"`, or `structured_records` if `data_type == "structured"`** — same branching pattern as Quality and Annotation. Recursively walk all string values in the dicts/lists (both are nested dict/list structures by this point) rather than assuming a flat schema.
- Uses `src/pii/rules.py` — **regex + validation rules only, no LLM call.** Detects: emails, phone numbers (Indian formats), PAN (`[A-Z]{5}[0-9]{4}[A-Z]{1}`), Aadhaar-like 12-digit sequences, GSTIN pattern, credit card numbers (regex + **Luhn algorithm check** to cut false positives).
- **Decide `action` per finding, not once for the whole document**: mask emails/phones (partial, e.g. `jo****@example.com`), fully redact PAN/Aadhaar/card numbers (no value shown at all, just entity_type + location), allow everything else.
- **Never store the raw matched substring anywhere** — not in `pii_findings`, not in logs, not in a trace. `masked_value` is either a partially-masked representation or `None` for fully-redacted categories.
- Sets: `pii_findings`, `final_content` — the governed output, preserving the original data's shape (dict for `extracted_data`, list-of-dicts for `structured_records`) with PII masked/redacted in place. This is the only content field that may reach persistence.

### `output_guardrail_node` (`src/guardrails/output_guardrail.py`)
- Final PII scan: re-run `src/pii/rules.py` against `final_content` — if anything unmasked slipped through Governance, this is the safety net.
- Policy validation: confirm `final_content` doesn't violate additional configured policy.
- Sets: `output_guardrail_passed`, `policy_violations`, and `final_status = "completed"` if passed.

### `persistence_node` (`src/persistence/writer.py`)
- Writes the final state to SQLite (see Section 4). **Must never write `raw_text`, `extracted_data`, or `structured_records`.** Only `final_content` (post-governance) and metadata/counts.
- Sets: `completed_at`.
- The only node that writes to persistence — every path, including `"failed"`, must reach it before `END`.

---

## 3. Edges and conditional edges (`src/graph/graph.py`, routing logic in `src/graph/routers.py`)

Build with `langgraph.graph.StateGraph(PipelineState)`. Register every node from Section 2. **Every router function checks for a technical failure first** — if `final_status == "failed"`, route straight to `persistence`, bypassing manual_review and any further normal-path logic.

```python
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
```

**Compile with a checkpointer — required for `interrupt()`/`Command(resume=...)` to work at all:**

```python
compiled_graph = graph.compile(checkpointer=checkpointer)  # SqliteSaver (API) or MemorySaver (tests)
```

**Every invocation must pass a `thread_id`, using `document_id`** — this is what lets a *separate* later HTTP request (`POST /review/{document_id}`) resume the *same* paused execution the original `/ingest` call started:

```python
config = {"configurable": {"thread_id": document_id}}
result = compiled_graph.invoke(initial_state, config=config)
# ... later, in a different request:
result = compiled_graph.invoke(Command(resume={"decision": "approve", "notes": "..."}), config=config)
```

### Router function logic (`src/graph/routers.py`)

```python
def route_after_input_guardrail(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    return "discovery" if state["input_valid"] else "manual_review"

def route_by_data_type(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    return "extraction_parse" if state["data_type"] == "unstructured" else "quality"

def route_after_extraction_parse(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    return "injection_check_parsed"

def route_after_injection_check(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    return "manual_review" if state["parsed_injection_flag"] else "extraction_structure"

def route_after_extraction_structure(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    return "quality"

def route_after_quality(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    if state["quality_status"] == "FAIL":
        return "retry" if state["data_type"] == "unstructured" else "manual_review"
    return "annotation" if state["needs_annotation"] else "governance"

def route_after_retry(state) -> str:
    # <=, not <: max_retries means "this many retries allowed," not "this many
    # total attempts." With MAX_RETRIES=1, this permits exactly one retry
    # (retry_count reaches 1, 1 <= 1 is true) then stops on the next failure
    # (retry_count reaches 2, 2 <= 1 is false).
    return "extraction_parse" if state["retry_count"] <= state["max_retries"] else "manual_review"

def route_after_annotation(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    return "governance"

def route_after_governance(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    return "output_guardrail"

def route_after_output_guardrail(state) -> str:
    if state.get("final_status") == "failed":
        return "persistence"
    return "persistence" if state["output_guardrail_passed"] else "manual_review"

def route_after_manual_review(state) -> str:
    # Reject, or one of the two hard-terminal reasons (input rejection /
    # injection flag) — either way final_status is already "manual_review"
    # and there's nothing left to route to but persistence.
    if state.get("final_status") == "manual_review":
        return "persistence"
    # Approve, resumed from a quality failure: quality_status was overridden
    # to "PASS" inside manual_review_node — route exactly like a normal PASS.
    if state.get("quality_status") == "PASS":
        return "annotation" if state["needs_annotation"] else "governance"
    # Approve, resumed from an output guardrail failure: final_content and
    # pii_findings already exist, nothing left to do but persist.
    return "persistence"
```

This is the complete routing logic, including the failure short-circuit at every stage that can throw. Do not add, remove, or reorder branches without flagging it in `PROGRESS.md`.

---

## 4. Persistence (`src/persistence/`)

SQLite table `pipeline_runs`, one row per document: `document_id, file_name, dataset_type, data_type, sensitivity_flag, quality_status, retry_count, needs_annotation, annotation_model_used, extraction_model_used, pii_findings_count, final_status, error_message, human_reviewer_notes, review_status, review_issue_summary, started_at, completed_at, processing_time_ms`.

**`review_issue_summary`** (new): `Optional[str]`. This is what `write_pending_review`'s `issue_summary` argument actually gets stored in — without this column the value has nowhere to go, and `GET /reviews/pending` has nothing to return to the Streamlit review queue.

**`review_status`** (new): `Optional[Literal["pending", "resolved"]]`. `document_id` is the primary key and `write_pipeline_result` is an **upsert** (`INSERT ... ON CONFLICT(document_id) DO UPDATE`), not a blind insert — a document that goes through `interrupt()` gets written twice: once as `review_status="pending"` at the moment of interruption (from the API layer, see Section 6 — `persistence_node` itself never runs mid-interrupt), and again as `review_status="resolved"` with the full final data once `persistence_node` actually runs after resume. A blind insert would produce two rows for the same document.

**Explicitly excluded from this table and from every other persisted or logged artifact: `raw_text`, `extracted_data`, `structured_records`, and any raw PII value.** `pii_findings_count` is a count, not the findings themselves; if you also want the finding list persisted for the dashboard's "PII findings by type" view, store `[{entity_type, action}]` only — never `masked_value`, and never anything resembling the original matched text.

Second table `pipeline_events` for the lineage view: `document_id, node_name, timestamp, duration_ms, status` — one row per entry in `node_trace`, unpacked and written by `persistence_node`.

`write_pipeline_result(state)` in `writer.py` writes both tables in one transaction, called for **every** terminal path — `completed`, `manual_review`, `rejected`, and `failed` alike. A separate, smaller function `write_pending_review(document_id, file_name, issue_summary)` handles the interrupted-but-not-yet-resolved case (see Section 6).

---

## 5. Observability (`src/observability/tracing.py`)

Langfuse callback handler attached to graph invocation (`graph.invoke(initial_state, config={"callbacks": [langfuse_handler]})`). **Configure the handler to log node name, status, and timing only — never pass `raw_text`, `extracted_data`, or `structured_records` into trace metadata.** Pull keys from settings.

**Offline fallback, required:** if `LANGFUSE_PUBLIC_KEY` or `LANGFUSE_SECRET_KEY` is empty or missing, return `callbacks=[]` instead of constructing the handler. Observability must never be a hard dependency for the pipeline to run — someone cloning the repo without Langfuse credentials set up should get a fully working, un-traced pipeline, not a crash.

---

## 6. FastAPI (`src/api/main.py`)

- `POST /ingest` — accepts file upload + `needs_annotation: bool` + `dataset_type: str` form fields. Saves file to `data/uploads/{document_id}/`, builds initial state, invokes the graph via `compiled_graph.invoke(initial_state, config={"configurable": {"thread_id": document_id}})` (V1 — synchronous, no async job queue). **Check the result for an interrupt** (LangGraph surfaces this as `result["__interrupt__"]` on a paused run): if interrupted, call `write_pending_review(document_id, file_name, issue_summary)` and return a `status: "pending_review"` response; otherwise return the final state summary (status + counts, not raw content) as before.
- `GET /status/{document_id}` — reads from `pipeline_runs`.
- `GET /reviews/pending` — lists rows where `review_status == "pending"`, with their issue summaries, for the Streamlit review queue to display.
- `POST /review/{document_id}` — accepts `{"decision": "approve"|"reject", "notes": str}`. Resumes with `compiled_graph.invoke(Command(resume={"decision": ..., "notes": ...}), config={"configurable": {"thread_id": document_id}})`, using the **same `document_id` as `thread_id`** used at ingestion — this is what lets the graph pick back up from exactly where it paused. Returns the final state summary once the graph completes.
- `GET /dashboard-data` — aggregated metrics: total processed, `final_status` breakdown (including `failed` as its own slice, distinct from `manual_review`), retry rate, manual review rate, pending-review count, average `processing_time_ms`, PII findings count by entity type.

---

## 7. Streamlit dashboard (`src/dashboard/app.py`)

- Upload widget (file + `needs_annotation` checkbox + `dataset_type` selector) → `POST /ingest`, shows the result (including a clear "awaiting review" state if the graph interrupted).
- **Review queue page**: calls `GET /reviews/pending`, lists each pending document with its issue summary (already sanitized — never raw field values or raw PII, per the same rule `quality_issues` follows), an Approve/Reject choice, and a notes text field → `POST /review/{document_id}`.
- Metrics view via `GET /dashboard-data`: status breakdown (bar chart, `failed` shown separately from `manual_review`), retry rate, manual review rate, pending-review count, PII findings by type, average processing time.
- Recent-runs table from `pipeline_runs`.

---

## 8. Tests (`tests/`)

- `test_state.py` — state initializes with correct defaults.
- `test_discovery.py` — `data_type`/`sensitivity_flag` correct for known fixtures.
- `test_extraction.py` — mock both local and external clients; assert the correct one is called based on `sensitivity_flag` on the *unstructured* path; assert `extraction_parse_node` varies strategy by `retry_count`; assert an exception from the mocked client results in `final_status="failed"`, not a crash.
- `test_annotation.py` — same local/external assertion as extraction, for the annotation path.
- `test_quality.py` — one invalid required field fails the whole record set; a clean set passes; correct schema is selected by `dataset_type`.
- `test_governance.py` — known PII strings (valid and invalid-Luhn card numbers, real vs. fake PAN format); assert each finding carries its own `action`; assert `masked_value` is never the raw matched string, and is `None` for redacted categories.
- `test_routers.py` — every router function in Section 3 against constructed state dicts, covering every branch **including the `final_status == "failed"` short-circuit** at each applicable router, and all three `route_after_manual_review` branches (reject/hard-terminal, approve-from-quality, approve-from-guardrail).
- `test_manual_review.py` — using `MemorySaver`: structured quality FAIL triggers `interrupt()` with the right payload; resume with `{"decision": "approve"}` sets `quality_status="PASS"` and the graph proceeds to completion; resume with `{"decision": "reject"}` ends at `final_status="manual_review"` with `human_reviewer_notes` set (not `"rejected"`); input guardrail rejection and injection-flag trips do **not** call `interrupt()` at all — assert the graph reaches `manual_review_node` and terminates without ever pausing for those two.
- `test_graph_integration.py` — full graph end-to-end against synthetic fixtures for: structured PASS → completed; structured FAIL → interrupt → approve → completed; structured FAIL → interrupt → reject → manual_review; unstructured PASS first try → completed; unstructured FAIL then PASS on retry → completed; unstructured FAIL all retries → interrupt → approve → completed; input guardrail rejection → manual_review (no interrupt); parsed injection flagged → manual_review (no interrupt); a mocked model exception → failed.

`scripts/generate_synthetic_data.py` — using `faker`, generate:
- Structured: clean CSVs; CSVs with intentional missing/invalid required fields; **at least one CSV with a sensitive column (email/PAN/phone) and at least one without**, to exercise both sides of `sensitivity_flag`.
- Unstructured: simple PDFs/images (plain text rendered to PDF is fine); **at least one with a sensitive-sounding filename (e.g. `patient_medical_record.pdf`) to force the local-model path, and at least one with a clearly non-sensitive filename (e.g. `public_press_release.pdf`) to force the external/Groq path** — both paths must actually get exercised by the fixture set, not left to chance.

---

## Build stages (execute in order, update PROGRESS.md after each)

- **Stage 0:** Section 0 (scaffolding, dependencies, `.env.example`, folder structure, empty `PROGRESS.md`).
- **Stage 1:** Section 1 (state schema) + `src/models/llm_clients.py` + `src/config/validation_schemas.yaml` (at least one `dataset_type` entry with 3–4 required fields).
- **Stage 2:** `input_guardrail_node`, `discovery_node`, `scripts/generate_synthetic_data.py` run once to produce fixtures in `data/synthetic/` (including the sensitive/non-sensitive pairs from Section 8).
- **Stage 3:** `extraction_parse_node`, `injection_check_parsed_node`, `extraction_structure_node`.
- **Stage 4:** `quality_node`, `retry_handler_node`, `manual_review_node` **without** the `interrupt()` behavior yet — just get it setting `final_status="manual_review"` correctly for all five entry reasons first, as a plain terminal node. Confirm the base graph runs end-to-end before adding pause/resume complexity on top of it.
- **Stage 5:** `annotation_node`, `src/pii/rules.py`, `governance_node`, `output_guardrail_node`.
- **Stage 6:** Persistence (Section 4, including the upsert), full graph assembly with all failure short-circuits (Section 3), observability (Section 5). Run `test_graph_integration.py` — all non-interrupt paths (structured/unstructured PASS, plain manual_review terminal cases, `failed`) must pass before moving on.
- **Stage 7:** Add the checkpointer and rewrite `manual_review_node` to the full `interrupt()`/resume behavior from Section 2, plus `route_after_manual_review`. Run `test_manual_review.py` and the interrupt-related cases in `test_graph_integration.py` — do not proceed until approve, reject, and the two hard-terminal (non-interruptible) paths all behave correctly.
- **Stage 8:** FastAPI (Section 6), including `/reviews/pending` and `/review/{document_id}`.
- **Stage 9:** Streamlit dashboard (Section 7) including the review queue page, README with setup/run instructions, final full test suite pass.

After Stage 9, `PROGRESS.md` should read as a complete build log a reviewer could follow without needing this plan open next to it.
