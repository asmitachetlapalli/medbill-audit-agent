# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A medical-bill audit agent: parse a medical bill PDF, look up CPT (procedure) codes, and flag billing anomalies. The UI is a Streamlit app; the audit logic is a LangGraph agent backed by Google Gemini (`langchain-google-genai`).

## Modules

All modules are implemented. Each tool file has a `__main__` block that runs it against `sample_bills/sample.pdf` for quick smoke-testing.

- `app.py` — Streamlit UI: upload PDF → metrics + findings table → optional AI summary → dispute letter (editable + downloadable).
- `agent/graph.py` — LangGraph ReAct agent (Gemini + the two tools). Exposes `build_agent()` (compiled, cached) and `audit_bill(raw_text)`.
- `agent/prompts.py` — `SYSTEM_PROMPT` and `REPORT_INSTRUCTIONS` for the agent.
- `agent/tools/cpt_lookup.py` — `parse_line_items()` (text → line items), `lookup_codes()` (enrich with reference data), and the `lookup_bill` tool.
- `agent/tools/anomaly_detector.py` — `detect_anomalies()` and the `find_anomalies` tool.
- `agent/tools/pdf_extractor.py` — `extract_bill_text(pdf_path)`.
- `data/cpt_codes.csv` — reference dataset: `code, description, category, medicare_rate, typical_charge, max_units_per_day`.

## Commands

```bash
pip install -r requirements.txt        # install deps (no venv configured yet)
streamlit run app.py                   # run the app
python -m agent.tools.cpt_lookup       # smoke-test parsing + lookup on the sample bill
python -m agent.tools.anomaly_detector # smoke-test anomaly detection on the sample bill
python -m agent.graph                  # run the full agent audit (needs GOOGLE_API_KEY)
```

There is no test suite, linter, or CI configured yet.

## Architecture notes

- Data flow: PDF → `pdf_extractor.extract_bill_text()` → `cpt_lookup` (parse + price against `data/cpt_codes.csv`) → `anomaly_detector` (flag) → rendered in `app.py`. The LangGraph agent (`agent/graph.py`) orchestrates the two `@tool`-decorated functions; `app.py` calls the deterministic functions directly for the table and dispute letter, and layers the agent's narrative on top.
- **The structured audit (findings, metrics, dispute letter) is fully deterministic and needs no API key.** Only the AI summary in the UI uses the LLM. Keep this separation when extending — don't make the core audit depend on the model.
- The bill parser in `cpt_lookup.py` is tuned to the Mayo Clinic itemization layout in `sample_bills/sample.pdf` (date-led rows ending in `<qty> $<amount>`, codes matching `[A-Z]\d{4}` or `\d{5}`). Other bill formats may need the regexes adjusted; the app degrades gracefully when no line items are recognized.
- `extract_bill_text(pdf_path)` returns `{"raw_text": str, "pages": [{"page_number", "text"}]}`. Its `__main__` block hardcodes an absolute Windows path — use a relative path when generalizing.

## LLM configuration (via `.env`, loaded with `python-dotenv`)

- `GOOGLE_API_KEY` — required for the AI summary / `audit_bill`.
- `GEMINI_MODEL` — defaults to `gemini-2.5-flash-lite`. Note: `gemini-1.5-*` is retired (404), and `gemini-2.0-flash*` returned `limit: 0` on free tier here. `gemini-2.5-flash-lite` and `gemini-2.5-flash` are verified working.
- `GEMINI_MAX_RETRIES` — defaults to 6; retries transient 429 rate limits with exponential backoff.
- The agent is a ReAct loop, so one audit makes several model calls — quota burns faster than a single request.

## Platform

Developed on Windows (PowerShell). Avoid hardcoded absolute paths like the one in `pdf_extractor.py`'s test block when adding new code.
