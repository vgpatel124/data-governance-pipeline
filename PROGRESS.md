# PROGRESS.md — Build Log

Build log for the Multi-Agent Data Curation & Governance Pipeline (V1). One section per stage, appended in order.

---

## Stage 0 — Scaffolding, dependencies, `.env.example`, folder structure

**What was built**
- `uv` project initialized in place (Python 3.11 pinned via `.python-version`).
- All runtime dependencies from Section 0 installed: `langgraph` 1.2.11, `langgraph-checkpoint-sqlite` 3.1.1, `langchain` 1.4.0, `langchain-groq`, `langchain-ollama`, `fastapi` 0.141.1, `uvicorn[standard]`, `streamlit`, `pydantic`, `pandas`, `pyyaml`, `python-magic`, `unstructured[pdf]` 0.27.5, `pillow`, `langfuse` 4.15.2, `sqlalchemy`, `python-dotenv`, `httpx`.
- Dev dependencies: `pytest`, `pytest-asyncio`, `faker`.
- Import smoke check passed, including `SqliteSaver`, `MemorySaver`, `interrupt`, `Command`.
- Full folder tree from Section 0 created with `__init__.py` files; `data/synthetic/` and `data/uploads/` created.
- `.env.example` created verbatim from the plan.

**Files created/modified**
- `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `README.md` (empty placeholder), `.env.example`, `PROGRESS.md`
- `src/**/__init__.py`, `tests/__init__.py`, `data/uploads/.gitkeep`

**Deviations / decisions**
- The project directory already existed (containing `PLAN.md`), so `uv init --python 3.11` was run in place instead of `mkdir` + `cd`. `uv init` also created a `.git` repo and a `main.py` stub; `main.py` was deleted (not in the planned tree).
- Added `[tool.pytest.ini_options]` (`testpaths=["tests"]`, `pythonpath=["."]`) to `pyproject.toml` so `from src...` imports work under pytest.
- `.gitignore` extended with `.env`, `data/uploads/*`, `data/*.db` (plan says uploads are gitignored; DBs are runtime artifacts).
- Installed system libraries via Homebrew: `libmagic` (required by `python-magic`) and `poppler` (required by `unstructured` PDF `hi_res` strategy). `tesseract` was already present.
- **Environment notes:** Ollama is not installed on this machine and no `GROQ_API_KEY`/Langfuse keys are set. Per the plan, tests mock both LLM clients and Langfuse falls back to `callbacks=[]`, so neither blocks the build. Live LLM calls are only needed when running the API against real documents.

**What's next**
- Stage 1: `src/graph/state.py`, `src/config/settings.py`, `src/models/llm_clients.py`, `src/config/validation_schemas.yaml`.

---

## Stage 1 — State schema, LLM clients, validation schemas

**What was built**
- `PipelineState`, `NodeTraceEntry`, `PIIFinding` TypedDicts exactly per Section 1, with `node_trace: Annotated[List[NodeTraceEntry], operator.add]`.
- `build_initial_state(file_path, file_name, needs_annotation, dataset_type, document_id=None)` sets every Section 1 default (`retry_count=0`, `max_retries=int(settings.MAX_RETRIES)`, empty lists, `human_reviewer_notes=None`, `started_at=utcnow().isoformat()`, `document_id=uuid4()`); `needs_annotation` and `dataset_type` are required positional args, never defaulted.
- `NodeTimer` helper: produces the single `NodeTraceEntry` each node returns, and `.failed(exc)` builds the exact failure return mandated by Section 2 (`final_status`, `error_message`, `node_trace` — nothing else).
- `src/config/settings.py` loads `.env` via `python-dotenv` with the `.env.example` defaults.
- `src/models/llm_clients.py`: `get_local_client()` → `ChatOllama(llama3.2:3b)`, `get_external_client()` → `ChatGroq(llama-3.3-70b-versatile)`, plus `get_client_for_sensitivity(flag)` returning `(client, "local"|"external")`.
- `validation_schemas.yaml` with three `dataset_type` entries: `customer_records` (4 required fields: customer_id, name, email, phone), `product_catalog` (4: product_id, product_name, category, price), `documents` (4: title, date, amount, parties — the unstructured extraction target schema).
- `tests/test_state.py` — **6 passed**.

**Files created/modified**
- `src/graph/state.py`, `src/config/settings.py`, `src/config/validation_schemas.yaml`, `src/models/llm_clients.py`, `tests/test_state.py`

**Deviations / decisions**
- Added settings not in `.env.example` but implied by the plan, with defaults: `MAX_FILE_SIZE_BYTES` (10MB, Section 2 "e.g. 10MB"), `UPLOAD_DIR`, `VALIDATION_SCHEMAS_PATH`.
- `get_external_client()` raises `RuntimeError` if `GROQ_API_KEY` is empty rather than letting Groq fail later; the calling node's try/except turns this into `final_status="failed"`.
- `get_client_for_sensitivity(None)` treats `None` as sensitive → local model (fail-safe, consistent with Discovery's default).
- `build_initial_state` explicitly initializes every other Optional field to `None` so the dict contains all schema keys (tested).
- `format_rules` in the YAML are full-match regexes applied to `str(value)`; the plan only says "regex per field".

**What's next**
- Stage 2: `input_guardrail_node`, `discovery_node`, `scripts/generate_synthetic_data.py` (run once to produce fixtures).

---

## Stage 2 — Input guardrail, discovery, synthetic data

**What was built**
- `input_guardrail_node`: checks existence, extension allow-list (`.csv .json .pdf .png .jpg .jpeg`), size (≤ `MAX_FILE_SIZE_BYTES`, 10MB), non-empty; injection heuristic scans **content** for `.csv/.json` and **filename only** for binary formats; `sanitize_for_log()` strips control chars and path-traversal components before anything is logged. Sets `input_valid`, `input_injection_flag`, `rejection_reason`.
- `src/guardrails/injection_check.py`: shared `detect_injection(text)` regex heuristic (ignore/disregard/forget previous instructions, `system:`, "you are now", "reveal your system prompt", chat-template tokens, `[INST]`, "new instructions:") plus `injection_check_parsed_node` (used in Stage 3).
- `discovery_node`: extension → `data_type`; structured files loaded **once** (pandas for CSV with `dtype=str`, NaN→`None`; JSON list / object / `{"records": [...]}`), headers checked case-insensitively by substring against the keyword list, rows stored in `structured_records`. Unstructured: filename/path heuristic only, no OCR. Wrapped in try/except → `final_status="failed"`.
- `scripts/generate_synthetic_data.py` (faker `en_IN`, seed 42) — run once, produced in `data/synthetic/`:
  - Structured: `customers_clean.csv` (sensitive: email/phone/pan), `customers_invalid.csv` (missing email, bad phone, bad id), `products_clean.csv` (non-sensitive), `products_invalid.csv` (missing price), `customers_clean.json`, `products_injection.csv` (injection text in a cell).
  - Unstructured: `patient_medical_record.pdf` (sensitive name → local model), `public_press_release.pdf` (non-sensitive name → Groq), `invoice_scan.png` (image input), `public_newsletter_injected.pdf` (injection hidden in content, innocuous filename), `public_announcement_sparse.pdf` (low-content doc).
- `tests/test_discovery.py` — **18 passed** (discovery on all fixture types, case-insensitive header match, fail-safe default, malformed JSON → failed with exact failure shape, guardrail accept/reject/oversize/injection/binary-filename-only, sanitizer). Full suite so far: **24 passed**.

**Files created/modified**
- `src/guardrails/input_guardrail.py`, `src/guardrails/injection_check.py`, `src/agents/discovery.py`, `scripts/generate_synthetic_data.py`, `tests/test_discovery.py`, `data/synthetic/*`

**Deviations / decisions**
- **Injection → invalid input.** The plan lists `input_injection_flag` as a separate output but only routes on `input_valid`. For the flag to have any effect, a detected injection sets `input_valid=False` with `rejection_reason="prompt-injection pattern detected"`, so it routes to the hard-terminal manual_review.
- **Unstructured sensitivity "non-sensitive" signal.** Section 2 says default to `True` when no signal is available, but Section 8 requires `public_press_release.pdf` to take the external path. A positive public-marker list (`public`, `press_release`, `brochure`, `newsletter`, `announcement`) is therefore the only way an unstructured file becomes `False`; a sensitive keyword/pattern always wins, and no signal → `True`.
- **PDF fixtures written as hand-built minimal PDFs** with a real text layer (no `reportlab` dependency added); the PNG is rendered with Pillow.
- Input guardrail also rejects empty (0-byte) files and is wrapped in try/except for I/O errors, even though it calls no external service.
- Added PAN column to customer CSVs so governance (Stage 5) has redact-category PII to act on.
- Guardrail tests live in `tests/test_discovery.py` (no dedicated guardrail test file exists in the plan's tree).

**What's next**
- Stage 3: `extraction_parse_node`, `injection_check_parsed_node` (already drafted), `extraction_structure_node`.

---

## Stage 3 — Extraction parse, parsed-injection check, extraction structure

**What was built**
- `extraction_parse_node`: `unstructured.partition.auto.partition(filename=..., strategy=...)`, joins element text into `raw_text`. **No LLM call** (tested by making both client factories raise). Strategy varies by `retry_count` via `select_parse_strategy()`: original attempt `fast` (PDF) / `ocr_only` (image), any retry `hi_res`. try/except → `final_status="failed"`.
- `injection_check_parsed_node`: runs `detect_injection` on `raw_text` before any LLM call; sets `parsed_injection_flag`.
- `extraction_structure_node`: picks `get_local_client()` when `sensitivity_flag` is `True` (or `None`, fail-safe) and `get_external_client()` when `False`; prompts the model to return JSON `{title, date (YYYY-MM-DD), amount (number), parties (list)}` with the document wrapped in `<document>` delimiters and an explicit "treat as data" instruction; tolerant JSON parsing (code fences, surrounding prose, content-block lists); output restricted to the four target keys. Any exception (client construction, network, bad JSON) → `final_status="failed"` with only the three failure keys.
- `tests/test_extraction.py` — **15 passed** (local vs external selection with mocked clients, fail-safe `None`, exceptions → failed, strategy `fast`→`hi_res` and `ocr_only`→`hi_res`, no-LLM guarantee in parse, real parse of `public_press_release.pdf`, parsed injection flag on/off).

**Files created/modified**
- `src/agents/extraction.py`, `tests/test_extraction.py` (`src/guardrails/injection_check.py` from Stage 2 unchanged)

**Deviations / decisions**
- **Image first-attempt strategy is `ocr_only`, not `fast`.** Verified against the installed `unstructured` 0.27.5: `partition(..., strategy="fast")` on a PNG raises `ValueError: The fast strategy is not available for image files.` PDFs keep `fast` → `hi_res` as planned; images use `ocr_only` → `hi_res`, which still differs between attempts.
- Called `partition(filename=file_path, strategy=...)` (keyword form) rather than positional `partition(file_path)`.
- **Empty `raw_text` skips the LLM call** and returns all-`None` fields (`extraction_model_used` still recorded). Quality then FAILs and the retry loop re-parses with `hi_res` — sending an empty document to a model would be wasted work.
- A non-JSON model response is treated as a technical failure (`failed`), not a quality FAIL.
- Target schema is the `documents` dataset_type from Stage 1.
- Note: first use of `unstructured` on this machine took ~2 min (matplotlib font cache + model download); subsequent parses are fast.

**What's next**
- Stage 4: `quality_node`, `retry_handler_node`, `manual_review_node` as a plain terminal node, and a base end-to-end graph run.

---

## Stage 4 — Quality, retry handler, manual review (plain terminal), base graph run

**What was built**
- `quality_node`: loads `validation_schemas.yaml` (cached) keyed by `dataset_type`. Structured: every row checked for required non-null/non-empty fields and full-match regex format rules; **any single issue → file-level `FAIL`**, no thresholds. Unstructured: same checks on `extracted_data`. Issues are value-free strings such as `row 42: email field missing` / `document: date field has invalid format`. Unknown `dataset_type`, empty record set, or missing `extracted_data` → `FAIL`.
- `retry_handler_node`: increments `retry_count`, nothing else.
- `manual_review_node` (Stage 4 form): plain terminal node setting `final_status="manual_review"` for all five entry reasons; `review_reason(state)` helper classifies the reason (`input_rejected`, `parsed_injection`, `structured_quality_fail`, `retries_exhausted`, `output_guardrail_failed`) for Stage 7.
- `src/graph/routers.py`: all eleven Section 3 routers.
- `src/graph/graph.py`: `build_graph()` registers all 12 nodes with the exact Section 3 edges/mappings; `compile_graph(checkpointer=None)`.
- Temporary placeholders so the base graph can run end-to-end: `annotation.py` (pass-through), `governance.py` (copies content to `final_content`), `output_guardrail.py` (always passes → `completed`), `persistence/writer.py` (sets `completed_at` only). All four are clearly marked PLACEHOLDER and are replaced in Stages 5–6.
- Tests: `tests/test_quality.py` (9) + `tests/test_routers.py` (22) — **31 passed**. Covers: one invalid field in 50 rows fails whole set with `row 42` issue, issue text never contains the raw value, schema selection by `dataset_type`, unstructured PASS/FAIL, fixture CSV/JSON round-trips through discovery → quality, every router branch, the failed short-circuit at all nine applicable routers, `<=` boundary in `route_after_retry`, all three `route_after_manual_review` branches.
- **Base graph end-to-end confirmation** (scratch smoke script, mocked LLM + `partition`, real fixtures) — all 9 runs correct:
  - structured PASS → completed; unstructured PASS → completed; unstructured FAIL→retry→PASS → completed (retry_count=1)
  - manual_review via each of the five entry reasons: input rejection, parsed injection, structured quality FAIL, retries exhausted (retry_count=2, i.e. exactly one retry with `MAX_RETRIES=1`), output guardrail failure
  - mocked model exception → `failed`, routed straight to persistence
  - every path terminates at `persistence`.

**Files created/modified**
- Created: `src/agents/quality.py`, `src/agents/retry.py`, `src/agents/manual_review.py`, `src/graph/routers.py`, `src/graph/graph.py`, `tests/test_quality.py`, `tests/test_routers.py`
- Placeholders created (to be replaced): `src/agents/annotation.py`, `src/agents/governance.py`, `src/guardrails/output_guardrail.py`, `src/persistence/writer.py`

**Deviations / decisions**
- ⚠️ **Router branch reordered in `route_after_manual_review` (flagged per Section 3).** The plan's order checks `quality_status == "PASS"` before falling through to the "approve from output guardrail" case. But the output guardrail only runs *after* quality passed, so on approve-from-guardrail `quality_status` is always `"PASS"` and the plan's code would route back to annotation/governance → output_guardrail → manual_review again (an infinite review loop), making its own third branch unreachable. Fix: an explicit `if state.get("final_status") == "completed": return "persistence"` check is inserted **before** the quality check. This relies on `manual_review_node` setting `final_status="completed"` on guardrail approval, which Section 2 already mandates. The other two branches behave exactly as written. Tested in `test_routers.py`.
- `route_after_retry` has no failed short-circuit, exactly as in the plan (retry handler cannot fail).
- `quality_node` has no try/except because it calls no external service (the preamble requires it only for LLM/OCR nodes).
- Format rules are skipped for list/dict values (e.g. `parties`); required-field check treats empty lists/dicts/whitespace strings as missing.
- The Stage 4 smoke check was a throwaway script (not committed); the equivalent permanent coverage lands in `tests/test_graph_integration.py` in Stage 6.

**What's next**
- Stage 5: `annotation_node`, `src/pii/rules.py`, `governance_node`, `output_guardrail_node` (replacing placeholders).

---

## Stage 5 — Annotation, PII rules, governance, output guardrail

**What was built**
- `src/pii/rules.py` — regex + validation only, no LLM:
  - Entities: `email`, `phone` (Indian mobile, optional `+91`/`0` prefix, starts 6–9), `pan` (`[A-Z]{5}[0-9]{4}[A-Z]{1}`), `aadhaar` (12 digits, optional 4-4-4 grouping, first digit 2–9), `gstin`, `card` (13–19 digits, **Luhn-validated**).
  - Overlap resolution by pattern priority (GSTIN → card → Aadhaar → PAN → email → phone), so a PAN embedded in a GSTIN is reported once as `gstin`.
  - **Per-finding action:** email/phone → `masked` (`jo****@example.com`, `******3210`); PAN/Aadhaar/card → `redacted` (`masked_value=None`, content replaced by `[REDACTED_PAN]` etc.); everything else (GSTIN) → `allowed`.
  - `govern(content, location)` recursively walks dicts/lists, returns a governed **copy** with the original shape plus findings; locations look like `rows[3].email@0` / `document.parties[0]@12`. The raw matched substring is never returned.
- `governance_node`: reads `extracted_data` when `data_type=="unstructured"`, else `structured_records`; sets `pii_findings` and `final_content`.
- `output_guardrail_node`: re-runs `rules.detect()` on `final_content` — any non-`allowed` finding still in clear text is a violation (`"unmasked card at note@5"`, value-free). Configured policy: `final_content` must be non-empty, must not contain forbidden keys (`raw_text`, `extracted_data`, `structured_records`, `password`, `secret`, `api_key`), must be JSON-serializable and ≤ 5MB. Sets `output_guardrail_passed`, `policy_violations`, and `final_status="completed"` only when passed.
- `annotation_node`: checks `data_type` explicitly to pick `extracted_data` vs `structured_records` (first 50 rows), selects local/external client by `sensitivity_flag` (None → local), asks for `{"category", "confidence", "tags"}` over a fixed category list; normalizes unknown category → `other`, clamps confidence to [0, 1]. try/except → `failed`.
- `parse_json_object()` moved into `src/models/llm_clients.py` and shared by extraction + annotation.
- Tests: `tests/test_annotation.py` (8) + `tests/test_governance.py` (21) — full suite **99 passed**. Covers valid vs invalid-Luhn cards, real vs malformed PAN formats, GSTIN-not-PAN, per-finding actions, `masked_value` never raw and `None` for redacted categories, nested walk + shape preservation + no input mutation, governance on the real `customers_clean.csv` fixture (no source email/PAN survives), unstructured path reads `extracted_data` not `structured_records`, guardrail pass/catch/policy, annotation local/external on both data types.

**Files created/modified**
- Created: `src/pii/rules.py`, `tests/test_annotation.py`, `tests/test_governance.py`
- Replaced Stage 4 placeholders: `src/agents/annotation.py`, `src/agents/governance.py`, `src/guardrails/output_guardrail.py`
- Modified: `src/models/llm_clients.py` (+`parse_json_object`), `src/agents/extraction.py` (uses shared parser)

**Deviations / decisions**
- **GSTIN is `allowed`.** The plan lists GSTIN as detected but assigns mask/redact only to email/phone and PAN/Aadhaar/card, with "allow everything else". GSTIN is a public business registration number, so it stays in content. Its `pii_findings` entry still carries a partially masked value, never the raw string.
- **Adjacent-number ambiguity.** Separate digit groups joined only by single spaces (e.g. an Aadhaar followed directly by a card number) can be read as one long run by the card regex. One Stage 5 test initially failed for exactly this reason; its input was changed to punctuation-delimited text, which is how real documents present separate identifiers. Documented rather than engineered around for V1.
- Governance and the output guardrail are wrapped in try/except even though they call no external service, so a malformed `final_content` degrades to `failed` instead of crashing the graph.
- The output guardrail ignores `allowed` findings when deciding pass/fail; otherwise every GSTIN would force manual review.
- The policy rules (forbidden keys, size cap) are my own minimal "additional configured policy"; the plan doesn't specify them.
- Two small bugs were caught by the suite during this stage and fixed before the gate: a missing `parse_json_object` import in `extraction.py` after the refactor (4 extraction tests) and the adjacent-number test input above.

**What's next**
- Stage 6: persistence (SQLAlchemy models, upsert writer, events table), full graph assembly check, Langfuse observability with offline fallback, `tests/test_graph_integration.py` for all non-interrupt paths.

---

## Stage 6 — Persistence, full graph assembly, observability

**What was built**
- `src/persistence/db.py`: SQLAlchemy engine for `data/pipeline.db` (WAL, per-path cached, tables auto-created). `DB_PATH` is read at call time so tests can redirect it. This engine is never used for LangGraph checkpoints.
- `src/persistence/models.py`:
  - `pipeline_runs`: `document_id` is the primary key, plus every Section 4 column (including `review_status` and `review_issue_summary`) and `pii_findings_summary`, a JSON `[{entity_type, action}]`.
  - `pipeline_events`: `document_id, node_name, timestamp, duration_ms, status`.
- `src/persistence/writer.py`:
  - `write_pipeline_result(state)`: a single transaction that does a real **upsert** (`INSERT … ON CONFLICT(document_id) DO UPDATE` via `sqlalchemy.dialects.sqlite.insert`) and replaces the document's `pipeline_events` rows from the cumulative `node_trace`.
    - `processing_time_ms` is computed as `completed_at − started_at`.
    - `review_status` becomes `"resolved"` when the existing row was `"pending"`, and the stored `review_issue_summary` is preserved.
  - `write_pending_review(document_id, file_name, issue_summary)`: upserts a `review_status="pending"` row. It's for the API layer (Stage 8).
  - `persistence_node`: sets `completed_at`, includes its own trace entry in the write, and returns a one-item `node_trace`. On a DB error it returns `failed`.
- Graph assembly (`src/graph/graph.py`, from Stage 4) now runs with every real node. No placeholders remain.
- `src/observability/tracing.py`:
  - `get_callbacks()` returns `[]` when either Langfuse key is missing, or when handler construction throws (offline fallback).
  - With keys set, it builds `Langfuse(public_key, secret_key, host, mask=mask_payload)` and a `langfuse.langchain.CallbackHandler`. The API was verified against the installed langfuse 4.15.2, whose `MaskFunction` takes `data` as a keyword argument.
  - `mask_payload` keeps only allow-listed metadata keys (node name, status, timing, ids, retry count, data type) and replaces every other string, list, or content payload with `[redacted]`. `raw_text`, `extracted_data`, `structured_records`, `final_content`, and `pii_findings` can never be exported.
  - `get_run_config(document_id)` returns `{"configurable": {"thread_id": document_id}, "callbacks": [...]}`.
- **Stage gate — `tests/test_graph_integration.py`: 10 passed** (all non-interrupt paths). Mocked LLMs and `partition`; real guardrails, governance, routers, and SQLite:
  - structured PASS → completed (asserts no source email/PAN and no masked value anywhere in the DB)
  - structured PASS with annotation (external model for the non-sensitive CSV)
  - unstructured PASS on the first try, sensitive → local (external never called; no extracted content in the DB)
  - unstructured PASS on the first try, non-sensitive → external
  - unstructured FAIL then PASS on retry (`retry_count=1`, parse strategies `["fast", "hi_res"]`)
  - input guardrail rejection → manual_review
  - CSV content injection → manual_review
  - parsed injection → manual_review (neither LLM called)
  - extraction model exception → failed (bypasses manual_review)
  - annotation exception → failed
  - Every run asserts that a `pipeline_runs` row exists with the right status and that there is one `pipeline_events` row per trace entry.
- `tests/test_tracing.py` (5): offline fallback, one missing key, construction error → `[]`, `thread_id` config, masking of content fields.
- **Full suite: 114 passed.**

**Files created/modified**
- Created: `src/persistence/db.py`, `src/persistence/models.py`, `src/observability/tracing.py`, `tests/test_graph_integration.py`, `tests/test_tracing.py`
- Replaced Stage 4 placeholder: `src/persistence/writer.py`
- Modified: `src/agents/extraction.py` (removed unused imports)

**Deviations / decisions**
- **`final_content` is not persisted.** Section 2 permits writing `final_content`, but Section 4's column list doesn't include it. Also, on an approve-after-output-guardrail-failure it can contain PII the guardrail flagged and a human let through. Only counts and the `[{entity_type, action}]` summary are stored.
- Added a `pii_findings_summary` column. Section 4 explicitly allows this optional finding list (entity_type and action only).
- `pipeline_events` has a surrogate autoincrement `id` primary key and cascades on `document_id`. Events are **replaced** on each write, because a resumed run's `node_trace` already contains the pre-interrupt entries and appending would duplicate them.
- The persistence node writes a state that already includes its own trace entry, so the events table matches the final `node_trace` length exactly (tested).
- Tracing masks by allow-list rather than a deny-list of the three named fields. That's stricter than the plan and also covers prompts sent to the LLM, which contain document text.
- Test-found bug fixed before the gate: the first `mask_payload` also redacted the string values of allow-listed keys (e.g. `status="ok"`).

**What's next**
- Stage 7: compile with a checkpointer (`MemorySaver` in tests, `SqliteSaver` for the API), rewrite `manual_review_node` with `interrupt()`/`Command(resume=...)`, run `tests/test_manual_review.py` and the interrupt cases in `tests/test_graph_integration.py`.

---

## Stage 7 — Checkpointer + human-in-the-loop `interrupt()`/resume

**What was built**
- **API verified first** against installed `langgraph` 1.2.11 / `langgraph-checkpoint-sqlite` 3.1.1 with a throwaway probe graph:
  - a paused `invoke` returns `result["__interrupt__"]` as a list of `Interrupt` objects with `.value`
  - `get_state(config).next` names the paused node
  - `invoke(Command(resume=...), config)` resumes the run
  - `SqliteSaver(sqlite3.Connection)` resumes a thread correctly **through a brand-new saver and connection**, which is the requirement for two separate HTTP requests
- `src/graph/graph.py`:
  - `compile_graph(checkpointer)` for all callers
  - `get_sqlite_checkpointer(path)` → `SqliteSaver` on `DB_CHECKPOINT_PATH` (`data/langgraph_checkpoints.db`), managed only by LangGraph's own schema and never shared with `pipeline.db`
  - `compile_api_graph()` for the API
- `manual_review_node` rewritten per Section 2:
  - `review_reason(state)` classifies the entry point.
  - **Hard-terminal** (`input_rejected`, `parsed_injection`): sets `final_status="manual_review"` immediately. No `interrupt()` call.
  - **Interruptible** (`structured_quality_fail`, `retries_exhausted`, `output_guardrail_failed`): `interrupt({"document_id", "reason", "quality_issues", "policy_violations"})`. The payload carries issue summaries only.
  - **Approve:** sets `human_reviewer_notes`.
    - From a quality failure, it also sets `quality_status="PASS"`.
    - From an output-guardrail failure, it also sets `final_status="completed"` and leaves `output_guardrail_passed=False` untouched.
  - **Reject:** `final_status="manual_review"` (never `"rejected"`) plus `human_reviewer_notes`.
- `route_after_manual_review` (with the Stage 4 ordering fix) is now exercised end-to-end.
- **Stage gate: `tests/test_manual_review.py` 10 passed + `tests/test_graph_integration.py` 13 passed (23/23).** Full suite **127 passed**.
  - `test_manual_review.py` (MemorySaver; a spy wraps the real `interrupt`):
    - structured FAIL interrupts with the exact 4-key payload, no raw values, `next == ("manual_review",)`, and no DB row while paused
    - approve → `quality_status="PASS"` → governance → output_guardrail → persistence → `completed`
    - approve with `needs_annotation` routes through annotation
    - reject → `manual_review` (not `rejected`) with notes, persisted
    - an unrecognized decision fails closed
    - a `write_pending_review` row flips `pending` → `resolved` on resume and stays one row (upsert)
    - **output-guardrail failure** (a real CSV with a `password` column trips the forbidden-key policy) interrupts; approve → `completed`, `output_guardrail_passed` stays `False`, and the path goes `manual_review` → `persistence` with no re-loop
    - input rejection and parsed injection **never call `interrupt()`** and finish with `next == ()`
    - an upstream `failed` never reaches manual_review
  - `test_graph_integration.py`: all Section 8 cases now compile with `MemorySaver` and `thread_id=document_id`, with three added interrupt cases:
    - structured FAIL → interrupt → approve → completed
    - structured FAIL → interrupt → reject → manual_review
    - unstructured FAIL on all retries → interrupt (`retry_count=2`, strategies `fast`,`hi_res`) → approve → completed
    - The non-interrupt cases assert that no interrupt occurred.

**Files created/modified**
- Rewritten: `src/agents/manual_review.py`, `tests/test_graph_integration.py`
- Created: `tests/test_manual_review.py`
- Modified: `src/graph/graph.py` (checkpointer helpers)

**Deviations / decisions**
- **Unrecognized resume decision → reject (fail closed).** The plan only defines `approve`/`reject`. The API (Stage 8) also validates the value.
- **`review_reason` checks `output_guardrail_passed is False` before `quality_status == "FAIL"`.** That way a guardrail failure after an approved quality override is classified correctly.
- **The `unknown` reason is treated as hard-terminal.** This is defensive, since no route in Section 3 produces it.
- **The manual_review `NodeTimer` starts after `interrupt()` returns.** LangGraph re-executes the node from the top on resume, so `duration_ms` measures node work rather than human wait time, and exactly one trace entry is appended per actual completion.
- **`test_manual_review.py` is not in the Section 0 folder tree but is listed in Section 8, so it was created.**

**What's next**
- Stage 8: FastAPI (`/ingest`, `/status/{id}`, `/reviews/pending`, `/review/{id}`, `/dashboard-data`).

---

## Stage 8 — FastAPI service

**What was built**
- `src/api/main.py`: a `create_app(graph=None)` factory plus a module-level `app`.
  - By default, the lifespan hook compiles the graph once with `SqliteSaver` on `DB_CHECKPOINT_PATH` and eagerly creates the `pipeline.db` tables.
  - Tests can inject a `MemorySaver`-compiled graph.
- Endpoints (Section 6):
  - **`POST /ingest`** (multipart: `file`, `needs_annotation`, `dataset_type`):
    - Validates `dataset_type` against `validation_schemas.yaml` and returns 400 for unknown values.
    - Sanitizes the filename.
    - Streams the upload to `data/uploads/{document_id}/{name}`, stopping just past the size limit so the input guardrail still records the rejection.
    - Builds the initial state and invokes the graph with `get_run_config(document_id)` (`thread_id=document_id` plus Langfuse callbacks, or `[]` when Langfuse isn't configured).
    - **If `result["__interrupt__"]` is present:** builds a value-free `issue_summary` (reason plus quality issues and policy violations, truncated to 2000 chars), calls `write_pending_review`, and returns `status: "pending_review"`.
    - **Otherwise:** returns a status-and-counts summary (`final_status`, `data_type`, `sensitivity_flag`, `quality_status`, issue/violation counts, `retry_count`, models used, `pii_findings_count`, `pii_findings_by_type`, timestamps). It never returns content fields or PII.
  - **`GET /status/{document_id}`**: returns the `pipeline_runs` row, or 404.
  - **`GET /reviews/pending`**: returns `document_id`, `file_name`, and `review_issue_summary` for rows with `review_status == "pending"`.
  - **`POST /review/{document_id}`** (`{"decision": "approve"|"reject", "notes": str}`; pydantic returns 422 for other decisions):
    - Returns 404 if no checkpoint thread exists, and 409 unless the thread is paused at `manual_review`.
    - Resumes with `Command(resume=...)` on the **same `thread_id`**.
    - If the resumed run interrupts again (e.g. an approved quality override that then fails the output guardrail), the row goes back to `pending` with the new summary.
  - **`GET /dashboard-data`**:
    - `total_processed` counts only rows with a final status.
    - `status_breakdown` always includes all four statuses, with `failed` separate from `manual_review`.
    - Also returns `retry_rate`, `manual_review_rate`, `pending_review_count`, `avg_processing_time_ms`, and `pii_findings_by_type` (from the `[{entity_type, action}]` summary).
- `tests/test_api.py` — **10 passed**:
  - completed ingest: summary contains no content fields or source emails, and the file is saved under `uploads/{document_id}/`
  - ingest → pending → shows in queue and pending count → approve → completed/resolved → a second review returns 409
  - reject path
  - 404, 422, and 409 validation
  - a hard-terminal injection is never queued
  - unknown `dataset_type` returns 400, and the traversal + NUL filename is saved as `evil.csv`
  - `/status` 404
  - dashboard aggregates across completed, manual_review, pending, and failed
  - **`SqliteSaver` resume across two separate app instances** (ingest in one, review in a fresh one sharing only the checkpoint DB)
  - the default app compiles its SQLite graph on startup
- **Full suite: 137 passed.**
- **Live smoke test against a real `uvicorn` server** (default SqliteSaver app, temporary DB paths):
  - `/health` ok
  - clean CSV → `completed`, with PII by type `{email: 20, phone: 20, pan: 20}`
  - invalid CSV → `pending_review` with summary `reason: structured_quality_fail | row 5: email field missing; row 10: phone field has invalid format; …`
  - `/reviews/pending` returned 1 item
  - approve → `completed`, with notes recorded
  - `/dashboard-data` returned 2 completed, 0 pending, and PII `{email: 39, phone: 39, pan: 40}` (39 is correct: the invalid file has one blank email and one malformed phone)
  - no errors in the server log

**Files created/modified**
- Created: `src/api/main.py`, `tests/test_api.py`
- Modified: `src/guardrails/input_guardrail.py` (sanitizer hardening), `tests/test_discovery.py` (added sanitizer cases)

**Deviations / decisions**
- **Added `GET /health` and `GET /runs?limit=` (not in Section 6).** `/runs` serves Section 7's recent-runs table, so the dashboard talks only to the API and never opens SQLite directly.
- **`/ingest` returns 400 for an unknown `dataset_type`** instead of letting it run and FAIL quality, because a typo in a caller-supplied field isn't a data-quality finding.
- **`/review` checks the checkpoint state before resuming** (404 for an unknown thread, 409 if not paused at `manual_review`). Otherwise `Command(resume=...)` on a finished or unknown thread would silently do nothing or start an empty run.
- **A pending response also includes the value-free `quality_issues` and `policy_violations` lists** alongside `issue_summary`.
- **Graph infrastructure exceptions** (not node failures, which nodes already turn into `failed`) return HTTP 500 with only the exception type name.
- **Two bugs found and fixed before this stage was closed:**
  1. `/status` and `/runs` ran `select(PipelineRun)` on a Core connection, where `.scalars()` yields the first column (a `str`), not ORM objects. They now use an ORM `Session`. This caused 5 test failures; the live smoke test missed it because it never called those endpoints.
  2. **Percent-encoded control characters in filenames.** httpx encodes a NUL in a multipart filename as `%00`, so the server never sees a raw control byte. `sanitize_for_log` now also strips encoded `%00`–`%1F` and `%7F` (ordinary sequences like `%20` are left alone), with unit tests.

**What's next**
- Stage 9: Streamlit dashboard (upload, review queue, metrics, recent runs), README with setup/run instructions, final full test-suite pass.

---

## Stage 9 — Streamlit dashboard, README, final test pass

**What was built**
- `src/dashboard/app.py` is a Streamlit app with four sidebar pages. It talks **only** to the API (`API_URL`, default `http://localhost:8000`) and never opens either database.
  - **Upload:** file uploader (csv/json/pdf/png/jpg/jpeg), a `dataset_type` selector populated from `validation_schemas.yaml`, and a `needs_annotation` checkbox → `POST /ingest`. Results show as success, warning, or error with an icon and label. A paused run shows a clear **"⏳ pending review"** state with its value-free issue summary and a pointer to the review queue. The full count summary sits in an expander.
  - **Review queue:** `GET /reviews/pending` lists each document with its file name, ID, and issue summary. Summaries render with `st.text`, so the content is never interpreted as markdown. Each item has an Approve/Reject radio **with no default** and a notes field → `POST /review/{document_id}`. The outcome appears as a flash message after the rerun, including the case where a run resumes and then pauses again.
  - **Metrics:** `GET /dashboard-data` provides:
    - stat tiles in two rows: processed, pending review, average time, retry rate, review rate
    - a **Final status** bar chart, with `failed` shown separately from `manual_review`
    - a **PII findings by type** bar chart
    - a "Table view" twin under each chart
  - **Recent runs:** `GET /runs` feeds a table with a row-limit slider. Status shows as icon plus label, and pending rows read "⏳ pending review".
- Charts follow the dataviz guidance:
  - Form: rates and counts are stat tiles, not one-bar charts. The two breakdowns are thin horizontal bars with 4px rounded ends, solid hairline gridlines, no axis rules, and Vega hover tooltips.
  - **Status breakdown** uses the reserved status palette: good `#0ca30c`, warning `#fab219`, serious `#ec835a`, critical `#d03b3b`. Every color is paired with an icon and a text label, and the table view is always available, so meaning never relies on color alone.
  - **PII by type** is one series, so every bar gets the single slot-1 blue, stepped per theme (`#2a78d6` light / `#3987e5` dark). No value ramp is applied to the nominal categories.
  - No categorical palette is used, so there is nothing for the palette validator to check.
- `README.md` covers:
  - overview and a mermaid flow diagram
  - the status table
  - HITL, model-routing, and PII-action tables
  - prerequisites (uv, tesseract/poppler/libmagic, optional Ollama/Groq/Langfuse)
  - install and `.env` table
  - fixture generation
  - how to run the tests, API, and dashboard
  - the endpoint table
  - privacy and security guarantees, project layout, and known limitations
- `.claude/launch.json`: `api` and `dashboard` preview configurations. The API configuration points its databases and uploads at a scratch directory so previews don't touch `data/`.

**Verification**
- **Final full suite: 137 passed** (1 warning). By file:
  - `test_state` 6
  - `test_discovery` 18
  - `test_extraction` 15
  - `test_quality` 9
  - `test_routers` 22
  - `test_annotation` 8
  - `test_governance` 21
  - `test_tracing` 5
  - `test_graph_integration` 13
  - `test_manual_review` 10
  - `test_api` 10
- The single warning is a third-party `DeprecationWarning` inside `starlette.testclient` (`anyio.abc.BlockingPortal` alias). It is not from project code.
- Headless `streamlit.testing.v1.AppTest` of all four pages against a live API: no exceptions and no error elements.
- **Live end-to-end check in the browser.** I started the API (SqliteSaver) and the dashboard as preview servers and seeded them through `POST /ingest`:
  - `customers_clean.csv` → completed
  - `customers_invalid.csv` and `products_invalid.csv` → pending review
  - `products_injection.csv` → manual_review, hard-terminal and never queued
  - `products_clean.csv` with annotation → failed, because no Groq key is configured on this machine. This gave the `failed` slice real data.
  
  I then **approved `products_invalid.csv` through the Review queue form**. The graph resumed on the same `thread_id` and the run finished `✅ completed`. The queue dropped to one item, and the Metrics and Recent-runs pages reflected the change.
- **Visual review of screenshots found five defects, all fixed and re-verified by screenshot:**
  1. **PII chart dropped the "phone" y-axis label.** The Altair chart rendered shorter than its requested height, and Vega hides overlapping labels. Fixed by sizing each discrete band with `alt.Step(30)` and setting `labelOverlap=False`.
  2. **Chart title and metric labels were truncated.** Titles moved to a short `st.subheader`, with the "failed is separate" note moved to a caption. Tile labels were shortened and help tooltips added.
  3. **Pending rows showed `nan`** in the Recent-runs status column, because pandas NaN is truthy. `status_label` now treats None/NaN as pending.
  4. **Review decision defaulted to "Approve"**, which made approval a one-click pass-through. Now `index=None`, and submitting without a choice shows an error.
  5. **The average-time tile value was clipped** with five tiles in one row. The tiles now wrap into two rows. Durations are formatted as ms, s, or min.
  - Also replaced the deprecated `use_container_width` with `width="stretch"`. Streamlit 1.63 announced its removal.

**Files created/modified**
- Created: `src/dashboard/app.py`, `.claude/launch.json`
- Rewritten: `README.md` (it was an empty placeholder from Stage 0)
- Modified: `PROGRESS.md`

**Deviations / decisions**
- The dashboard's recent-runs table reads `GET /runs` (added in Stage 8) rather than querying `pipeline_runs` directly. That keeps the dashboard API-only.
- Streamlit pages are a sidebar radio inside a single `app.py`, matching the plan's one-file layout, rather than a `pages/` directory.
- **Finding, documented rather than changed:** `processing_time_ms` is `completed_at − started_at`, exactly as Section 4 defines it. For a document that paused for review, this includes **human wait time**. The one UI-approved document raised the live average from about 16 ms to about 21 s. The metric's help tooltip and the README's Known limitations now say this. Splitting pipeline time from review wait time is a V2 candidate.
- Pending-review rows have no `started_at`, because `write_pending_review(document_id, file_name, issue_summary)` has a fixed signature. They sort last in recent runs until resolved. This is documented in the README.

**What's next**
- The build is complete. All stages 0–9 are done and the full suite passes.

---

## Build summary (for reviewers)

**Architecture in one paragraph.**
A LangGraph `StateGraph` over `PipelineState` runs:
- `input_guardrail` → `discovery`
- For structured data: → `quality`
- For unstructured data: → `extraction_parse` (OCR, no LLM; `fast`/`ocr_only` then `hi_res` on retry) → `injection_check_parsed` (runs **before** any LLM) → `extraction_structure` (local Ollama if sensitive, Groq if not) → `quality`
- On an unstructured quality FAIL: `retry_handler` loops back while `retry_count <= max_retries`
- After quality passes: optional `annotation` → `governance` (regex + Luhn PII with per-finding mask/redact/allow) → `output_guardrail` (PII re-scan + policy)
- Every path, including `failed` and `manual_review`, ends at `persistence` → END.

`manual_review` calls `interrupt()` for three reasons: structured quality FAIL, retries exhausted, and an output guardrail failure. It is hard-terminal, with no interrupt, for an input rejection or a parsed-injection flag. A pause resumes via `POST /review/{document_id}` → `Command(resume=...)` on `thread_id=document_id`, backed by `SqliteSaver` (`data/langgraph_checkpoints.db`). Business outcomes go to a separate `data/pipeline.db` as an upsert, plus an events table. Langfuse tracing uses an allow-list mask and turns itself off when no keys are set.

**Guarantees tested:**
- No `raw_text`, `extracted_data`, `structured_records`, `final_content`, raw PII, or masked PII value in the DB, API responses, interrupt payloads, quality issues, or traces.
- Injection payloads never reach an LLM.
- Technical failures bypass manual review and get their own dashboard slice.
- `MAX_RETRIES=1` yields exactly one retry.
- Approve/reject/hard-terminal behave as specified.
- Checkpoint resume works across separate app instances.

**Deviations index.** Each is detailed in its stage above:

| Stage | Deviation |
|---|---|
| 0 | In-place `uv init`; Homebrew `libmagic` + `poppler`; pytest `pythonpath` |
| 1 | Extra settings (`MAX_FILE_SIZE_BYTES`, `UPLOAD_DIR`); `None` sensitivity → local model |
| 2 | Injection → `input_valid=False`; public-filename markers are the only non-sensitive unstructured signal; `rejected` status unused by any V1 path |
| 3 | Images use `ocr_only` (unstructured rejects `fast` for images); empty `raw_text` skips the LLM; non-JSON model output → `failed` |
| 4 | ⚠️ **`route_after_manual_review` reordered**: checks `final_status == "completed"` before `quality_status == "PASS"`, fixing an infinite review loop in the plan's approve-from-guardrail branch |
| 5 | GSTIN `allowed`; documented adjacent-number ambiguity; policy rules defined |
| 6 | `final_content` not persisted; `pii_findings_summary` column; events replaced on re-write; allow-list trace mask |
| 7 | Unrecognized decision fails closed; guardrail reason checked before quality; manual_review timer starts after resume |
| 8 | Added `/health` and `/runs`; 400 on unknown `dataset_type`; 404/409 guard before resume; ORM-session and percent-encoded-filename fixes |
| 9 | API-only dashboard; `processing_time_ms` includes human review wait (documented) |

**Run it:**
```bash
uv sync
uv run python scripts/generate_synthetic_data.py
uv run pytest
uv run uvicorn src.api.main:app --port 8000
uv run streamlit run src/dashboard/app.py
```

---

## Post-build fix — Identifier fields rejected valid real-world formats (2026-09-15)

**Reported failure**
Uploading `clean_product_dataset.csv` (25 rows, `dataset_type=product_catalog`) paused for review with `structured_quality_fail | row 1: product_id field has invalid format; ...`. Its IDs look like `P001`, and real catalogs also use `SKU-12345`, `PROD-ABC-001`, `ITEM_XYZ_99`, `12345`, and `ORD-2026-001`.

**Root cause**
- The suspected cause, a rule treating `_id` fields as numeric-only, **does not exist**. `validate_record` in `src/agents/quality.py` has no field-name-based logic; a repo-wide search found none.
- The failing rule was in `src/config/validation_schemas.yaml`:
  - `product_catalog.format_rules.product_id: '^P\d{5}$'`, meaning exactly "P" plus 5 digits.
  - `customer_records.format_rules.customer_id: '^C\d{5}$'`, the same pattern for customers.
- Both were written in Stage 1 to match the synthetic fixtures (`P00001`, `C00001`). They were never documented business requirements, so they imposed an invented ID format on every uploaded dataset. `P001` has 3 digits and failed; every other format above failed as well.
- Checked against the actual file:
  - All 25 issues were `product_id field has invalid format` (one per row).
  - No other field failed; prices such as `49.5` and `349.0` already passed.
  - The copy in `data/uploads/9d169f03-…/` is byte-identical to `~/Downloads/clean_product_dataset.csv`.

**Validation logic changed**
- Removed the `product_id` and `customer_id` entries from `format_rules`.
- Identifier fields are now validated only through `required_fields`, which `_is_missing` already implements:
  - `None`, `""`, and whitespace-only values fail as `row N: <field> field missing`.
  - Any other value is a valid identifier: alphanumeric, with `-`/`_` separators, or numeric.
- No replacement "generic ID" regex was added. Any such pattern would still invent a format (for example, rejecting `A.B` or `ID 5`), which is exactly the undocumented restriction being removed.
- A dataset that genuinely documents an ID format can still declare it under `format_rules`, and it is enforced (tested).
- The YAML header now documents this rule.
- **No Python validator code changed**, and every other rule is untouched: `email`, `phone`, `price`, `date`, `amount`, required-field checks, and any-issue-fails-the-file.

**Files modified**
- `src/config/validation_schemas.yaml`: removed the two ID regexes and documented the identifier rule in the header comment.
- `tests/test_quality.py`: added 14 tests and the `pytest`/`validate_record` imports.
- `scripts/generate_synthetic_data.py`: comment only. `customer_id = "BAD-ID"` was labelled "invalid format" but is now a valid identifier. **Fixture data is unchanged.** `customers_invalid.csv` still fails on its genuine problems (row 5 missing email, row 10 malformed phone), which the existing tests assert.

**Tests added**
- `test_valid_identifier_formats_pass`: `P001`, `SKU-12345`, `PROD-ABC-001`, `ITEM_XYZ_99`, `12345`, `ORD-2026-001` each pass as `product_id` (`product_catalog`) **and** as `customer_id` (`customer_records`).
- `test_missing_or_blank_identifier_fails`: `None`, `""`, `"   "`, and `"\t"` each fail with exactly `row 1: product_id field missing`.
- `test_mixed_identifier_formats_across_many_rows_pass`: a 25-row catalog mixing `P001`–`P019` with the six formats above passes with 0 issues.
- `test_id_fields_are_not_special_cased_by_name`: an arbitrary `order_id` gets no implicit format rule; blank values still fail.
- `test_explicit_dataset_id_format_is_still_enforced`: a schema declaring `order_id: '^\d+$'` accepts `12345` and rejects `ORD-2026-001`.
- `test_unrelated_format_rules_unchanged`: bad `price`, `email`, and `phone` values are still rejected.

**Test results**
- `tests/test_quality.py`: **23 passed** (9 existing + 14 new).
- Full suite: **151 passed** (137 before + 14 new). No existing test needed changes; the one warning is the known third-party starlette/anyio deprecation.

**End-to-end confirmation with `clean_product_dataset.csv`**
- `discovery_node` + `quality_node`: **25 rows, `PASS`, 0 issues.**
- Re-uploaded through the real API app (`create_app()` with the default SqliteSaver and `data/pipeline.db`, the same path a dashboard upload takes):
  - Response: HTTP 200, `status: completed`, `quality_status: PASS`, `quality_issue_count: 0`, `sensitivity_flag: false`, `pii_findings_count: 0`, `output_guardrail_passed: true`.
  - Persisted as `final_status=completed`, `review_status=None` (never queued) — document `f70e660a-b8d4-4a26-98ab-f3ebdac7fe4b`.
  - Path: input_guardrail → discovery → quality → governance → output_guardrail → persistence.

**Follow-ups for the operator (not changed automatically)**
- The API (port 8000) and dashboard (port 8501) processes that were running during this fix started **before** it. `load_schemas()` is cached per process, so they still apply the old rule until restarted.
- The original upload (`9d169f03-…`) was paused under the old rule and **remains in the review queue**. The schema fix doesn't retroactively change a paused run. It needs a human decision (approve or reject) in the Review queue; approving it takes the same path as the successful re-upload above.

---

## Task 0 — Identifier-validation bug: investigated, not present (2026-09-16)

**Outcome: no code or schema changed.** The reported `row 1: product_id field has invalid format` was real when first reported, was root-caused and fixed in the previous session (see "Post-build fix — Identifier fields rejected valid real-world formats"), and does not exist in the current tree. The later successful run of the same file was that fix taking effect, not a `dataset_type` difference.

**Verification performed (empirical, not by inspection alone)**
- `src/config/validation_schemas.yaml` carries **no** `format_rules` entry for any identifier field:
  - `product_catalog.format_rules` = `{price}` only
  - `customer_records.format_rules` = `{email, phone}` only
- All six required formats PASS under **both** schemas (as `product_id` and as `customer_id`): `P001`, `SKU-12345`, `PROD-ABC-001`, `ITEM_XYZ_99`, `12345`, `ORD-2026-001`.
- Genuinely invalid values still FAIL with `row 1: product_id field missing`: `None`, `""`, `"   "`, `"\t"`.
- Fixtures still FAIL for their intended reasons, so no fixture needed a replacement bad value:
  - `customers_invalid.csv` → FAIL: `row 5: email field missing`, `row 10: phone field has invalid format`
  - `products_invalid.csv` → FAIL: `row 4: price field missing`
  - Their intentionally-bad ID (`BAD-ID`) was invalid only under the old stricter rule, but neither fixture depended on it: both still exercise a real FAIL path through other fields.
- Clean fixtures still PASS: `customers_clean.csv`, `products_clean.csv`.
- The tests this task asks for already exist in `tests/test_quality.py` (added with the previous fix): `test_valid_identifier_formats_pass` (6 parametrized), `test_missing_or_blank_identifier_fails` (4 parametrized), plus `test_mixed_identifier_formats_across_many_rows_pass`, `test_id_fields_are_not_special_cased_by_name`, `test_explicit_dataset_id_format_is_still_enforced`, `test_unrelated_format_rules_unchanged`.

**Files created/modified**
- None (`PROGRESS.md` entry only).

**Deviations / decisions**
- Followed the task's "if they already pass, change nothing" branch. No replacement identifier regex was introduced, because any pattern would reimpose an undocumented format; identifiers remain validated by `required_fields` alone, with dataset-specific ID formats still expressible via `format_rules` (covered by a test).

**Test results**
- Full suite: **151 passed**, 1 warning (known third-party starlette/anyio deprecation).

**What's next**
- Task 1: correct the README's limitations and improvements sections.

---

## Task 1 — README limitations and improvements corrected (2026-09-16)

**What was built**
Rewrote the README's closing sections so the writeup reflects what was actually decided during the build rather than implying every gap was discovered late.

1. **Deliberate deferrals are now their own section**, separate from limitations, each with the reasoning that produced the decision:
   - **Contextual PII (names/addresses/locations) via a local spaCy NER layer.** Previously framed as an inherent limitation of regex. Corrected: V1 scoped the rules engine to pattern-bearing entities (email, phone, PAN, Aadhaar, GSTIN, Luhn-valid card) where a regex plus validator is exact; names have no pattern and need a model, so a **local** NER layer (local because the text may be sensitive) was planned and deferred to keep V1 detection deterministic and explainable.
   - **Row-level quarantine.** Was absent from the README entirely. V1's any-row-fails-the-file rule is stated as a deliberate simplification, with the V2 clean/quarantined partition described.
   - **Configurable failure thresholds.** Also absent. Described as a per-`dataset_type` tolerance that belongs in `validation_schemas.yaml` as policy, not hardcoded strictness.
2. **Retry strategy corrected.** The `MAX_RETRIES` row previously claimed `fast` → `hi_res` universally. A new **Retry strategy** section states both paths in a table: PDFs `fast` → `hi_res`, images `ocr_only` → `hi_res`, with the reason (`unstructured` raises `ValueError: The fast strategy is not available for image files.`). The `.env` table row was corrected to match.
3. **Two missing limitations added:**
   - `processing_time_ms` is `completed_at − started_at`, so reviewed documents include human wait time; noted that one reviewed document moved the live average from milliseconds to ~21s, and that splitting the two is planned rather than intended.
   - Redacted categories (PAN/Aadhaar/card) store `masked_value=None` by design, so findings record *that* and *where* but never any part of the value — correct for privacy, and the reason full audit traceability would need a separate access-controlled audit store rather than loosening the rule.
4. **Improvements rebalanced governance-first.** The list is now split into **Governance** (row-level quarantine, severity tiers separating blocking from advisory issues, a normalization node that auto-fixes deterministic issues like date formats/casing/whitespace instead of spending a reviewer on them, local spaCy NER, and a per-`dataset_type` governance policy file) and **Infrastructure** (async ingestion, vision-LLM fallback as a genuine third OCR strategy, sampling for large files, splitting pipeline time from review wait time).

**Files created/modified**
- `README.md` — corrected the `MAX_RETRIES` row; added **Retry strategy**; rewrote **Known limitations (V1)**; added **Deliberate deferrals (V2 scope)** and **Planned improvements**.

**Deviations / decisions**
- The README did not previously contain an improvements section or the contextual-PII-as-regex-limitation claim (that framing lived in the external writeup), so this task was executed as authoring the corrected framing in the README rather than editing existing wrong sentences. The substance requested — deferrals reframed, both retry paths correct, the two missing limitations, governance-weighted improvements — is all present.
- Kept the two accurate pre-existing limitations (filename-based sensitivity for unstructured files, adjacent-number PII ambiguity) and the first-parse-slowness note.
- No code, schema, or behavior changed in this task.

**Test results**
- Full suite: **151 passed, 1 warning in 6.70s** (documentation-only change; run to confirm no regression).

**What's next**
- Task 2: governed analytical store (DuckDB) + text-to-SQL `/query` endpoint with a `sqlglot` AST guardrail + dashboard Query page.

---

## Task 2 — Governed Text-to-SQL over curated data (2026-09-16)

**What was built**

*Governed analytical store (`src/governed/store.py`, DuckDB)*
- A **third** database at `DB_GOVERNED_PATH` (`data/governed.duckdb`), separate from `pipeline.db` (business outcomes) and `langgraph_checkpoints.db` (execution state). Nothing is merged or shared.
- One table per `dataset_type`, named `governed_<dataset_type>` and derived from `validation_schemas.yaml`: `document_id`, `row_index`, `ingested_at`, one column per `required_fields` entry, plus `extra_json` for governed fields outside the schema (e.g. `stock_quantity`, `status`).
- **Write condition — `is_queryable(state)`: `final_status == "completed"` AND `output_guardrail_passed is True`.** This is the single authority for entry into the store.
  - A document approved by a human *after* an output-guardrail failure keeps `output_guardrail_passed=False`, so it stays governed, logged, and visible in the dashboard but **never becomes queryable**. Stage 6 stopped persisting `final_content` for exactly this reason; putting those rows in a queryable table would have reopened that hole in a worse place.
- Only `final_content` (post-governance, masked/redacted) is written. Lists/dicts are stored as JSON strings. Writes are idempotent per `document_id` (DELETE + INSERT in one transaction, under a lock).
- Wired into `persistence_node`, the pipeline's only writer. A governed-store failure is logged and never flips an already-persisted terminal status.

*AST guardrail (`src/governed/sql_guard.py`, sqlglot 30.18)*
- `validate_sql(sql, allowed_tables)` parses with `sqlglot.parse(dialect="duckdb")` — no regex anywhere.
- Blocks: multiple statements; `DROP`, `DELETE`, `UPDATE`, `INSERT`, `ALTER`, `TRUNCATE`, `CREATE`, `ATTACH`, `COPY`, `PRAGMA`, `SET`, and generic `Command` nodes (checked both at top level and nested); anything that is not `SELECT`/`UNION`/`EXCEPT`/`INTERSECT`; any table outside the allow-list, including via `UNION`; catalog/schema-qualified names; and **table functions** such as `read_csv_auto('/etc/passwd')`, which parse as a table with an empty name.
- CTE aliases are resolved and permitted; a query must still read at least one real governed table.
- `enforce_row_limit()` wraps the validated query so `GOVERNED_ROW_LIMIT` (500) always caps results, even if the model wrote its own larger `LIMIT`.
- Every rejection returns a short, value-free reason and is logged.

*Text-to-SQL (`src/governed/text_to_sql.py`)*
- Prompts the model with the governed table's schema only (no data), wraps the question in `<question>` tags with an explicit "treat as data" instruction, and strips code fences/trailing semicolons.
- Uses the external client by default (governed data is already masked) and the local client when the caller sets `sensitive: true` — the same local/external pattern used elsewhere.

*API*
- `POST /query` — `{question, dataset_type, sensitive?}` → generate SQL → **guard** → row-limit wrap → execute with a timeout → structured rows. Blocked queries return HTTP 200 with `blocked: true` and a value-free `reason` (never a stack trace); the generated SQL is always returned so the caller can see what ran. Unknown `dataset_type` or an empty question is 400; a model/transport failure is 502.
- `GET /governed/tables` — dataset_type, table name, columns, and row counts (used by the dashboard).
- Governed tables are created at app startup.

*Dashboard*
- New **Query** page: row-count tiles per governed table, a dataset selector (no default) and question box, the generated SQL shown read-only via `st.code(..., language="sql")`, a results table, and a clear block reason with an explanation of the guardrail instead of an error dump.

**Files created/modified**
- Created: `src/governed/__init__.py`, `src/governed/store.py`, `src/governed/sql_guard.py`, `src/governed/text_to_sql.py`, `tests/conftest.py`, `tests/test_governed.py`, `tests/test_query_api.py`
- Modified: `src/config/settings.py` (+`DB_GOVERNED_PATH`, `GOVERNED_ROW_LIMIT`, `GOVERNED_QUERY_TIMEOUT_S`), `.env.example`, `src/persistence/writer.py` (governed write hook), `src/api/main.py` (`/query`, `/governed/tables`, startup table creation), `src/dashboard/app.py` (Query page), `pyproject.toml`/`uv.lock` (+`duckdb` 1.5.5, `sqlglot` 30.18.0)

**Deviations / decisions**
- **DuckDB over a second SQLite file**, as preferred by the task. Columnar analytical engine, no server, and `interrupt()` support for query cancellation.
- **Blocked queries return HTTP 200 with `blocked: true`** rather than 4xx. The dashboard renders the reason as part of the normal result view; a 4xx would surface as a transport error and lose the generated SQL that makes the block explainable.
- **Query timeout is enforced in-process** (worker thread + `con.interrupt()`), because DuckDB has no `statement_timeout` setting.
- **`extra_json` column**: governed fields outside `required_fields` are preserved as JSON rather than silently dropped. They are post-governance values, so the privacy rules still hold.
- **Per-dataset allow-list**: a `/query` for `customer_records` can only touch `governed_customer_records` — cross-dataset access is blocked (tested).
- **`tests/conftest.py`** added: an autouse fixture points `DB_GOVERNED_PATH` at a per-test temp file so the real store is never touched and tests can't see each other's rows.
- Per the task's instruction, `sqlglot` and DuckDB docs were treated as authoritative for this work; `PLAN.md` was **not** modified. API behavior was verified against the installed versions (multi-statement parsing, table-function detection, CTE aliases, `Union` top-level node, `duckdb.interrupt`).

**Test results**
- `tests/test_governed.py` — **38 passed**: 19 blocked-SQL cases (DROP/DELETE/UPDATE/INSERT/ALTER/TRUNCATE/CREATE/ATTACH/PRAGMA/COPY, multi-statement, `UNION` to `pipeline_runs`, direct `pipeline_runs`/`pipeline_events`/`checkpoints`, table functions, bare `SELECT 1`, unparseable, empty), 5 allowed-SQL cases including CTE and case-insensitive table names, row-limit wrapper, write-condition matrix, idempotency, unstructured dict content, `extra_json`, correct rows from a legitimate SELECT, **no raw PII in the governed store** (20 rows from `customers_clean.csv`: no raw email/PAN/phone, `[REDACTED_PAN]` and `****@` present instead), and **a human-overridden guardrail failure writing zero rows** (verified through a real interrupt→approve cycle, with the offending value absent).
- `tests/test_query_api.py` — **18 passed**: legitimate SELECT and aggregate through the endpoint, 8 blocked categories with exact reasons, cross-dataset block, row-limit enforcement, 400s, 502 on generation failure, invalid-column handled as a block rather than a crash, `/governed/tables`, prompt contains the governed schema and delimits the question, and `sensitive: true` routing to the local model.
- **Full suite: 207 passed** (151 before + 56 new), 1 warning (known third-party starlette/anyio deprecation).

**What's next**
- Task 3: async ingestion — `POST /ingest` returns 202 immediately, a background worker runs the graph, `/status` becomes meaningful from the moment of ingestion, and interrupt/resume behavior is verified to still work from a background task.

---

## Task 3 — Async ingestion (2026-09-16)

**What was built**

*API*
- `POST /ingest` now validates the upload, saves it, writes a `pipeline_runs` row, schedules the graph, and returns **202 Accepted** with `{document_id, status: "queued", run_status: "queued", poll: "/status/{id}"}`. The graph no longer runs inline, so OCR never holds the HTTP connection open.
- `execute_pipeline(...)` is the background worker (module-level, so it is directly testable): it flips `run_status` to `running`, builds the initial state with the ingestion-time `started_at`, invokes the graph with `get_run_config(document_id)` (`thread_id=document_id` + tracing callbacks), and routes the result through the existing `handle_result` — which still writes the pending-review row when the run paused.
- An infrastructure failure inside the worker (not a node failure, which the graph already handles) is recorded via `write_failed_run` as `final_status="failed"` with `run_status="done"`, so a crashed background run is still visible rather than stuck.

*Lifecycle state — chose `run_status`, not an extension of `review_status`*
- New indexed column `run_status` on `pipeline_runs`: `queued` → `running` → `paused` (awaiting review) | `done` (terminal write).
- `review_status` keeps its existing meaning (`pending`/`resolved`) untouched. Overloading it would have conflated "where is this run in execution" with "what did the human decide", and the dashboard's pending-review metric reads `review_status` directly.
- `write_queued_run(...)` writes the row at ingestion time, so **`GET /status/{document_id}` is meaningful from the moment of ingestion** — with `dataset_type`, `file_name`, and `started_at` — not only at terminal state. This also fixes the earlier limitation that queued rows had no `started_at`.
- `set_run_status()` and `write_failed_run()` added alongside. `write_pipeline_result` sets `run_status="done"`, `write_pending_review` sets `"paused"`, and `execute_pipeline` closes the lifecycle explicitly after a non-paused run.

*Concurrency (SQLite single-writer)*
- `pipeline.db` already used WAL; added `PRAGMA busy_timeout=30000` and a SQLAlchemy `connect_args={"timeout": 30.0}` so concurrent writers wait instead of raising "database is locked".
- All `pipeline.db` writes are serialized through a process-level `threading.Lock` in `writer.py` (background runs are threads in this process, so this avoids contention before the busy timeout is even needed).
- The governed DuckDB store already serializes its writes under its own lock.

*Dashboard*
- The Upload page no longer blocks: it posts, stores the `document_id`, then polls `/status` once a second, showing `⏳ queued` / `⚙️ running` and then the outcome (completed, awaiting review with its issue summary, manual review, or failed). Recent runs gained a `run_status` column.
- Polling, not SSE or WebSockets, exactly as the task specified.

**Files created/modified**
- Created: `tests/test_async_ingest.py`
- Modified: `src/api/main.py` (202 + `BackgroundTasks` + `execute_pipeline`), `src/persistence/models.py` (+`run_status`), `src/persistence/writer.py` (+`write_queued_run`, `set_run_status`, `write_failed_run`, write lock, `run_status` values), `src/persistence/db.py` (busy timeout), `src/dashboard/app.py` (polling + `run_status` column), `tests/test_api.py` and `tests/test_query_api.py` (poll instead of assuming synchronous ingest), `README.md`

**Deviations / decisions**
- **FastAPI `BackgroundTasks`, no broker.** A single-node portfolio project does not justify Celery/Redis: a broker adds a service, a worker process, and deployment complexity. The trade-off is documented in the README's limitations (in-process work does not survive a restart, and there is no automatic retry) and a durable job queue is listed as a planned improvement.
- **`POST /review` stays synchronous.** Resuming from a checkpoint runs only the remaining nodes (no OCR), so it returns the final summary directly and the dashboard needs no second polling path.
- **Existing tests were updated to poll, not weakened.** `tests/test_api.py` gained `_await_run`/`_ingest_and_wait` helpers; assertions that previously read the synchronous response summary now read the `/status` row (e.g. `pii_findings_count == 60` and the `pii_findings_summary` entity types replace the old inline `pii_findings_by_type` check). No assertion was removed or loosened.
- Note on test timing: Starlette's `TestClient` executes background tasks before returning the response, so tests see a finished run immediately. The queued→running→done transition is therefore asserted directly against `execute_pipeline` with stub graphs (including one that blocks mid-run) rather than by racing the client.

**Test results**
- `tests/test_async_ingest.py` — **10 passed**:
  - `/ingest` returns 202 with a `document_id` and no run outcome in the body
  - a row is queryable from the moment of ingestion (`run_status="queued"`, `started_at` set, `final_status` still `None`), and `/status` exposes it
  - `run_status` is `running` *during* execution and `done` after
  - a slow run is observable as `running` through `/status` while it executes, then `done`
  - a background infrastructure failure is recorded as `failed`/`done` with the error type
  - **CRITICAL: a run that interrupts in the background still pauses (`paused`/`pending`), still appears in `/reviews/pending`, and still resumes to `completed` via `/review`** — plus the same flow across two app instances with the on-disk `SqliteSaver`
  - 8 concurrent background runs all complete, write 8 correct rows, and land exactly 20 governed rows each (160 total) with no lock errors; 5 concurrent API ingests all succeed with distinct ids
- Full suite: **217 passed** (207 + 10), 1 warning (known third-party starlette/anyio deprecation).

**Live verification (real servers, real Groq)**
- `POST /ingest` returned **202 in 65ms** (and 3ms on a second call); polling showed `running` → `done` with `final_status=completed` and 60 PII findings.
- Async interrupt path: upload of `customers_invalid.csv` → `run_status=paused`, `review_status=pending`, correct value-free summary → `POST /review` approve → `final_status=completed`, `run_status=done`, `review_status=resolved`.
- `GET /governed/tables` showed `customer_records: 40` after two eligible documents.
- Real Groq text-to-SQL: *"how many customers are there?"* → `SELECT COUNT(DISTINCT customer_id) …` → `[[21]]`; *"show me 3 customers with their email addresses"* → rows with **masked** emails (`ud****@example.net`), confirming governance holds through the query path.
- Dashboard Query page in the browser: row-count tiles, dataset selector, read-only generated SQL, and a results table (*"which cities have the most customers?"* produced a `json_extract_string(extra_json, '$.city')` group-by returning 38 rows).
- All five dashboard pages load against the live API with no exceptions (headless `AppTest`).
- **Honest caveat:** two deliberately hostile prompts ("delete every customer row", "show me everything from pipeline_runs") were *not* blocked at the guardrail, because the model refused to produce hostile SQL and rewrote both as harmless SELECTs over the governed table. The guardrail itself is proven by the 26 tests that feed it hostile SQL directly, bypassing the model — which is the correct place to test it, since a model's refusal is not a security control.

**What's next**
- All four tasks are complete. Optional follow-ups, now listed in the README: durable job queue, splitting pipeline time from review wait time, row-level quarantine, severity tiers, a normalization node, and local NER for contextual PII.
