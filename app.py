"""Streamlit UI for the medical-bill audit agent.

Upload a bill PDF -> extract text -> audit it -> view flagged issues and a dispute letter.

The structured audit (line-item table, metrics, dispute letter) is produced by the
deterministic tools and works without any API key. The plain-language AI summary uses
the Gemini-backed agent and appears only when GOOGLE_API_KEY is configured.
"""

import io
import os
import tempfile

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from agent.tools.anomaly_detector import detect_anomalies
from agent.tools.cpt_lookup import lookup_codes, parse_bill_metadata, parse_line_items
from agent.tools.pdf_extractor import extract_bill_text

load_dotenv()

# On Streamlit Community Cloud, secrets come from st.secrets (set in the app dashboard),
# not a .env file. Bridge them into the environment so the rest of the app — which reads
# os.getenv — works unchanged. Local .env values (loaded above) take precedence.
try:
    for _key in ("GOOGLE_API_KEY", "GEMINI_MODEL", "GEMINI_MAX_RETRIES"):
        if _key in st.secrets and _key not in os.environ:
            os.environ[_key] = str(st.secrets[_key])
except Exception:  # noqa: BLE001 - no secrets configured (e.g. local .env run) is fine
    pass

st.set_page_config(page_title="Medical Bill Auditor", page_icon="🧾", layout="wide")

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "ok": 3}
SEVERITY_LABEL = {"high": "🔴 High", "medium": "🟠 Medium", "low": "🟡 Low", "ok": "✅ OK"}
SEVERITY_ROW_COLOR = {"high": "#fdecea", "medium": "#fff4e5", "low": "#fffbe5", "ok": ""}
MODEL_CHOICES = ["gemini-2.5-flash-lite", "gemini-2.5-flash"]


# --------------------------------------------------------------------------- #
# Cached data helpers (keyed on their args, so reruns don't redo the work).    #
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def extract_text(file_bytes: bytes) -> str:
    """Write uploaded PDF bytes to a temp file and return its extracted raw text."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        return extract_bill_text(tmp_path)["raw_text"]
    finally:
        os.unlink(tmp_path)


@st.cache_data(show_spinner=False)
def enrich_line_items(raw_text: str) -> list[dict]:
    """Parse + price the bill's line items (deterministic; no API needed)."""
    return lookup_codes(parse_line_items(raw_text))


@st.cache_data(show_spinner=True)
def run_ai_summary(raw_text: str, model: str, overcharge_ratio: float) -> str:
    """Run the LLM agent for a plain-language summary (cached per text/model/ratio)."""
    from agent.graph import audit_bill  # lazy import: app loads even if LLM deps don't

    return audit_bill(raw_text, model=model, overcharge_ratio=overcharge_ratio)


# --------------------------------------------------------------------------- #
# Presentation builders.                                                       #
# --------------------------------------------------------------------------- #
def findings_by_code(result: dict) -> dict:
    """Map each code to its highest-severity finding type and severity."""
    by_code: dict[str, dict] = {}
    for f in result["findings"]:
        cur = by_code.get(f["code"])
        types = (cur["types"] if cur else []) + [f["type"].replace("_", " ")]
        sev = f["severity"]
        if cur and SEVERITY_ORDER[cur["severity"]] <= SEVERITY_ORDER[sev]:
            sev = cur["severity"]
        by_code[f["code"]] = {"types": types, "severity": sev}
    return by_code


def line_item_table(line_items: list[dict], result: dict):
    """A styled DataFrame of *every* line item, flagged or not."""
    by_code = findings_by_code(result)
    rows = []
    for li in line_items:
        qty = max(li.get("quantity") or 1, 1)
        flag = by_code.get(li["code"])
        rows.append(
            {
                "Code": li["code"],
                "Description": li["reference_description"] or li["description"],
                "Qty": qty,
                "Charged/unit": li["charge"] / qty,
                "Typical": li["typical_charge"],
                "Medicare": li["medicare_rate"],
                "Status": ", ".join(flag["types"]).title() if flag else "OK",
                "Severity": SEVERITY_LABEL[flag["severity"] if flag else "ok"],
                "_sev": flag["severity"] if flag else "ok",
            }
        )
    df = pd.DataFrame(rows)

    def color_row(row):
        return [f"background-color: {SEVERITY_ROW_COLOR[row['_sev']]}"] * len(row)

    money = lambda v: f"${v:,.2f}" if pd.notna(v) else "—"  # noqa: E731
    styler = (
        df.style.apply(color_row, axis=1)
        .format({"Charged/unit": money, "Typical": money, "Medicare": money})
        .hide(axis="columns", subset=["_sev"])
    )
    return styler


def price_chart_data(line_items: list[dict]) -> pd.DataFrame:
    """Per-code charged-vs-typical table for a bar chart (known codes only)."""
    rows = [
        {
            "Code": li["code"],
            "Charged/unit": li["charge"] / max(li.get("quantity") or 1, 1),
            "Typical": li["typical_charge"],
        }
        for li in line_items
        if li["known"] and li["typical_charge"]
    ]
    return pd.DataFrame(rows).set_index("Code") if rows else pd.DataFrame()


def build_dispute_letter(line_items, result, fields: dict) -> str:
    """Build a dispute letter, filling user-entered fields and listing flagged charges."""
    findings = result["findings"]
    total = result["summary"]["total_estimated_overcharge"]

    def val(key, placeholder):
        return fields.get(key) or placeholder

    disputed = []
    for f in findings:
        item = next((li for li in line_items if li["code"] == f["code"]), None)
        charge = f" (charged ${item['charge']:,.2f})" if item else ""
        disputed.append(f"  - Code {f['code']}{charge}: {f['message']}")
    disputed_block = "\n".join(disputed) if disputed else "  - (none)"

    savings_line = (
        f"Based on typical and Medicare-allowed rates, I estimate potential overcharges "
        f"of approximately ${total:,.2f}."
        if total > 0
        else "Please provide documentation supporting the charges noted above."
    )

    return f"""{val('sender_name', '[Your Name]')}
{val('sender_contact', '[Your Phone / Email]')}
{val('date', '[Date]')}

Billing Department
{val('provider', '[Provider / Hospital Name]')}

Re: Dispute of charges on account {val('account_number', '[Account Number]')}
    Patient: {val('patient_name', '[Patient Name]')}    Date(s) of service: {val('date_of_service', '[Date of Service]')}

To Whom It May Concern:

I am writing to formally dispute the following charges on the above account, which appear
to be inconsistent with typical and Medicare-allowed rates for these services:

{disputed_block}

{savings_line}

I respectfully request the following:
  1. A fully itemized bill listing every charge, procedure code, and quantity.
  2. Written justification for each disputed charge listed above.
  3. A corrected statement, or an explanation of why the charges are accurate.

Please treat this as a formal request and place a hold on any collection activity for the
disputed amounts until this matter is resolved.

Sincerely,
{val('sender_name', '[Your Name]')}

---
Note: This letter is generated from an automated review using estimated reference pricing.
Flagged items are charges to question with your provider, not proven billing errors.
"""


def letter_to_pdf(text: str) -> bytes | None:
    """Render the letter text to a simple PDF. Returns None if reportlab isn't installed."""
    try:
        from reportlab.lib.pagesizes import letter as letter_size
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Preformatted, SimpleDocTemplate
    except ImportError:
        return None

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter_size)
    style = getSampleStyleSheet()["Code"]
    style.fontSize = 9
    style.leading = 12
    doc.build([Preformatted(text, style)])
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Tabs.                                                                         #
# --------------------------------------------------------------------------- #
def render_findings_tab(line_items, result):
    summary = result["summary"]
    total_charged = sum(li["charge"] for li in line_items)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Line items", summary["line_item_count"])
    c2.metric("Total charged", f"${total_charged:,.2f}")
    c3.metric("Issues flagged", summary["finding_count"])
    c4.metric(
        "Est. overcharge",
        f"${summary['total_estimated_overcharge']:,.2f}",
        delta=(
            f"{summary['total_estimated_overcharge'] / total_charged:.0%} of bill"
            if total_charged
            else None
        ),
        delta_color="inverse",
    )

    if summary["finding_count"] == 0:
        st.success("No anomalies found. The bill appears consistent with typical pricing.")
    elif summary["has_high_severity"]:
        st.warning("This bill contains high-severity issues worth disputing.")

    st.markdown("**All line items** (flagged rows are highlighted)")
    st.dataframe(line_item_table(line_items, result), use_container_width=True, hide_index=True)

    chart = price_chart_data(line_items)
    if not chart.empty:
        st.markdown("**Charged vs. typical price, per code**")
        st.bar_chart(chart)


def render_ai_tab(raw_text, model, ratio):
    if not os.getenv("GOOGLE_API_KEY"):
        st.info(
            "Set GOOGLE_API_KEY (in your environment or a .env file) to enable the "
            "AI-written plain-language summary. The structured audit works without it."
        )
        return
    st.caption(f"Uses the sidebar settings: model `{model}`, sensitivity {ratio:.1f}×.")
    if not st.button("Generate AI summary", type="primary"):
        st.caption("Click to run the agent. Results are cached per bill, model, and sensitivity.")
        return
    try:
        st.markdown(run_ai_summary(raw_text, model, ratio))
    except Exception as exc:  # noqa: BLE001 - surface any agent/model error to the user
        st.error(f"Could not generate the AI summary: {exc}")


def render_letter_tab(line_items, result, meta):
    st.caption("Fill in your details — the letter updates automatically.")
    c1, c2 = st.columns(2)
    with c1:
        sender_name = st.text_input("Your name")
        sender_contact = st.text_input("Your phone / email")
        date = st.text_input("Date")
    with c2:
        provider = st.text_input("Provider / hospital", value=meta.get("provider", ""))
        patient_name = st.text_input("Patient name", value=meta.get("patient_name", ""))
        account_number = st.text_input("Account number", value=meta.get("account_number", ""))
    date_of_service = st.text_input("Date(s) of service", value=meta.get("date_of_service", ""))

    fields = {
        "sender_name": sender_name,
        "sender_contact": sender_contact,
        "date": date,
        "provider": provider,
        "patient_name": patient_name,
        "account_number": account_number,
        "date_of_service": date_of_service,
    }
    letter = build_dispute_letter(line_items, result, fields)
    st.text_area("Dispute letter", value=letter, height=420)

    d1, d2 = st.columns(2)
    d1.download_button("Download as .txt", data=letter, file_name="dispute_letter.txt", mime="text/plain")
    pdf = letter_to_pdf(letter)
    if pdf:
        d2.download_button("Download as .pdf", data=pdf, file_name="dispute_letter.pdf", mime="application/pdf")
    else:
        d2.caption("Install `reportlab` for PDF export.")


def findings_csv(result: dict) -> str:
    return pd.DataFrame(result["findings"]).to_csv(index=False)


# --------------------------------------------------------------------------- #
# Main.                                                                         #
# --------------------------------------------------------------------------- #
def sidebar() -> tuple[str, float]:
    with st.sidebar:
        st.header("Settings")
        if os.getenv("GOOGLE_API_KEY"):
            st.success("API key detected — AI summary available.")
        else:
            st.warning("No GOOGLE_API_KEY — AI summary disabled.")
        model = st.selectbox("Gemini model", MODEL_CHOICES, index=0)
        st.divider()
        ratio = st.slider(
            "Overcharge sensitivity",
            min_value=1.1,
            max_value=3.0,
            value=1.5,
            step=0.1,
            help="Flag a charge when it exceeds this multiple of the typical fair price. "
            "Lower = stricter (flags more).",
        )
        st.caption(f"Flagging charges above {ratio:.1f}× the typical price.")
    return model, ratio


def main() -> None:
    model, ratio = sidebar()

    st.title("🧾 Medical Bill Auditor")
    st.caption(
        "Upload a medical bill PDF to check it for overcharges, duplicate charges, and "
        "billing anomalies — and generate a dispute letter."
    )

    uploaded = st.file_uploader("Upload a medical bill (PDF)", type=["pdf"])
    if uploaded is None:
        st.info("Upload a PDF to begin.")
        st.stop()

    with st.spinner("Reading the bill..."):
        raw_text = extract_text(uploaded.getvalue())

    if not raw_text.strip():
        st.error("No text could be extracted from this PDF. It may be a scanned image.")
        st.stop()

    line_items = enrich_line_items(raw_text)
    if not line_items:
        st.error(
            "No billable line items were recognized in this PDF. The bill format may "
            "not be supported yet."
        )
        with st.expander("View extracted text"):
            st.text(raw_text)
        st.stop()

    result = detect_anomalies(line_items, overcharge_ratio=ratio)
    meta = parse_bill_metadata(raw_text)

    tab_findings, tab_ai, tab_letter, tab_text = st.tabs(
        ["📋 Findings", "🤖 AI Summary", "✉️ Dispute Letter", "📄 Bill Text"]
    )
    with tab_findings:
        render_findings_tab(line_items, result)
        if result["findings"]:
            st.download_button(
                "Download findings (.csv)",
                data=findings_csv(result),
                file_name="findings.csv",
                mime="text/csv",
            )
    with tab_ai:
        render_ai_tab(raw_text, model, ratio)
    with tab_letter:
        render_letter_tab(line_items, result, meta)
    with tab_text:
        st.text(raw_text)


if __name__ == "__main__":
    main()
