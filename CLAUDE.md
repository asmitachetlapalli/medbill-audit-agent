# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A medical-bill audit agent: parse a medical bill PDF, look up CPT (procedure) codes, and flag billing anomalies. The UI is a Streamlit app; the audit logic is a LangGraph agent backed by Google Gemini (`langchain-google-genai`).

## Modules

All modules are implemented. Most tool files have a `__main__` block that runs them against `sample_bills/sample.pdf` for quick smoke-testing.

- `app.py` — Streamlit UI: upload PDF → metrics + findings table → optional AI summary → dispute letter (editable + downloadable).
- `agent/graph.py` — the agentic core. `audit_bill(raw_text, model, overcharge_ratio)` runs a Gemini ReAct agent over a bill-bound toolbox. `make_audit_tools(raw_text, ratio)` parses the bill once and returns five closure tools; `_get_llm(model, retries)` caches the Gemini client; `_message_text(msg)` normalizes str-or-list message content.
- `agent/prompts.py` — `SYSTEM_PROMPT` (investigation methodology + tool descriptions) and `REPORT_INSTRUCTIONS` (final report format).
- `agent/tools/cpt_lookup.py` — `parse_line_items()` (text → line items), `lookup_codes()` (enrich), `get_code_detail(code)` (single-code lookup), `parse_bill_metadata()`, and the `lookup_bill` tool.
- `agent/tools/anomaly_detector.py` — `detect_anomalies(line_items, overcharge_ratio, ...)` and the `make_find_anomalies_tool(ratio)` factory.
- `agent/tools/unbundling.py` — `check_unbundling(codes)`: flags code pairs that shouldn't be billed together, using `data/unbundling_pairs.csv`.
- `agent/tools/dispute_letter.py` — `compose_letter(disputed_lines, total, fields)`: shared letter template used by both the UI and the agent's draft tool.
- `agent/tools/pdf_extractor.py` — `extract_bill_text(pdf_path)`.
- `data/cpt_codes.csv` — reference dataset: `code, description, category, medicare_rate, typical_charge, max_units_per_day`.
- `data/unbundling_pairs.csv` — `code_a, code_b, relationship, note` (illustrative, not official CMS/NCCI edits).

## Commands

```bash
pip install -r requirements.txt        # install deps (no venv configured yet)
streamlit run app.py                   # run the app
python -m agent.tools.cpt_lookup       # smoke-test parsing + lookup on the sample bill
python -m agent.tools.anomaly_detector # smoke-test anomaly detection on the sample bill
python -m agent.tools.unbundling       # smoke-test the unbundling rules
python -m agent.graph                  # run the full agentic audit (needs GOOGLE_API_KEY)
```

There is no test suite, linter, or CI configured yet.

## Architecture notes

- Two layers, deliberately separate:
  - **Deterministic pipeline** (no API key): PDF → `pdf_extractor` → `cpt_lookup` (parse + price against `data/cpt_codes.csv`) → `anomaly_detector` (flag). `app.py` calls these directly for the metrics, findings table, and dispute letter.
  - **Agentic layer** (Gemini): `audit_bill` builds a per-bill toolbox via `make_audit_tools` and lets the agent investigate. The five tools are closures over the parsed line items, so the agent calls them with no/tiny args instead of shuttling JSON. The agent decides which to call: `get_line_items`, `find_overcharges`, `check_unbundling_on_bill`, `explain_code(code)`, `draft_dispute_letter(codes)`. Used only for the optional AI Summary tab.
- **The deterministic layer needs no API key; only the AI summary uses the LLM. Keep this separation when extending — don't make the core audit depend on the model.**
- Agent gotchas (both handled in `audit_bill`, keep them): the model sometimes ends with an **empty final turn** after its last tool call (we nudge it once to write the report), and message `content` may be a **str or a list of blocks** (use `_message_text`).
- Adding an agent tool: define it inside `make_audit_tools` as a closure over `line_items`/`codes`, give it a clear docstring (the agent reads it), and list it in `SYSTEM_PROMPT`.
- The bill parser in `cpt_lookup.py` is tuned to the Mayo Clinic itemization layout in `sample_bills/sample.pdf` (date-led rows ending in `<qty> $<amount>`, codes matching `[A-Z]\d{4}` or `\d{5}`). Other bill formats may need the regexes adjusted; the app degrades gracefully when no line items are recognized.
- `extract_bill_text(pdf_path)` returns `{"raw_text": str, "pages": [{"page_number", "text"}]}`. Its `__main__` block hardcodes an absolute Windows path — use a relative path when generalizing.

## LLM configuration (via `.env`, loaded with `python-dotenv`)

- `GOOGLE_API_KEY` — required for the AI summary / `audit_bill`.
- `GEMINI_MODEL` — defaults to `gemini-2.5-flash-lite`. Note: `gemini-1.5-*` is retired (404), and `gemini-2.0-flash*` returned `limit: 0` on free tier here. `gemini-2.5-flash-lite` and `gemini-2.5-flash` are verified working.
- `GEMINI_MAX_RETRIES` — defaults to 6; retries transient 429 rate limits with exponential backoff.
- The agentic loop makes **~7+ model calls per audit** (one per tool round-trip), so the free tier (≈20 requests/day per model) is exhausted after only a few audits. This is the main cost of the agentic design — keep it in mind before adding more tools or fan-out.

## Platform

Developed on Windows (PowerShell). Avoid hardcoded absolute paths like the one in `pdf_extractor.py`'s test block when adding new code.
