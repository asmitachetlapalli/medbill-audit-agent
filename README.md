# 🧾 Medical Bill Auditor

Upload a medical bill PDF and get an automated audit: parsed line items priced against a
reference dataset, flagged overcharges / duplicates / excess quantities, an AI-written
plain-language summary, and a ready-to-send dispute letter.

The structured audit (findings table, dispute letter) is fully deterministic and needs
**no API key**. The optional AI summary uses Google Gemini.

## Architecture

```
PDF → pdf_extractor → cpt_lookup (parse + price) → anomaly_detector (flag) → Streamlit UI
                                                  ↘ LangGraph agent (Gemini) → AI summary
```

- `app.py` — Streamlit UI (tabs: Findings, AI Summary, Dispute Letter, Bill Text).
- `agent/tools/` — `pdf_extractor`, `cpt_lookup`, `anomaly_detector`.
- `agent/graph.py` — LangGraph ReAct agent that orchestrates the tools.
- `data/cpt_codes.csv` — reference pricing dataset.

A sidebar holds the runtime controls: a **Gemini model picker** and an **overcharge
sensitivity slider** (the multiple of the typical price above which a charge is flagged).
The slider drives every view — the findings table, the dispute letter, and the AI summary.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env        # then add your GOOGLE_API_KEY (optional, for the AI summary)
streamlit run app.py
```

Tested on Python 3.12.

## Deploy to Streamlit Community Cloud

1. Push this repo to GitHub (already done if you cloned it from there).
2. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
3. **New app** → pick this repo, branch `main`, main file `app.py`.
4. Under **Advanced settings → Secrets**, paste your key (TOML format):
   ```toml
   GOOGLE_API_KEY = "your-api-key-here"
   APP_PASSWORD = "choose-a-strong-password"  # gate the public app (recommended)
   # GEMINI_MODEL = "gemini-2.5-flash-lite"   # optional
   ```
5. Under **Advanced settings → Python version**, select **3.12** (the version this app
   is tested on). This avoids dependency-install failures from version-specific wheels.
6. Deploy. The app reads secrets via `st.secrets` automatically (see `app.py`).

The structured audit works even if you skip the secret; only the AI Summary tab needs it.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_API_KEY` | — | Required for the AI summary. |
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | Gemini model for the agent. |
| `GEMINI_MAX_RETRIES` | `6` | Retries on transient rate limits. |
| `APP_PASSWORD` | — | If set, gates the app behind a password. Recommended for public deploys; when unset the app is open. |

Set these via `.env` (local) or Streamlit secrets (deployed).

## Notes & limits

- The bill parser is tuned to the sample Mayo Clinic itemization layout; other formats
  may need the regexes in `agent/tools/cpt_lookup.py` adjusted.
- Reference prices are estimates of fair/typical cost — flagged items are charges to
  **question** with your provider, not proven billing errors.
- A public deployment uses **your** API key/quota for every visitor's AI summary; the
  summary is behind a button click to limit accidental spend.
