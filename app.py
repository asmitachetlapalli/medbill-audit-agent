"""Streamlit UI for the medical-bill audit agent.

Upload a bill PDF -> extract text -> audit it -> view flagged issues and a dispute letter.

The structured audit (findings table, metrics, dispute letter) is produced by the
deterministic tools and works without any API key. The plain-language AI summary uses
the Gemini-backed agent and appears only when GOOGLE_API_KEY is configured.
"""

import os
import tempfile

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from agent.tools.anomaly_detector import detect_anomalies
from agent.tools.cpt_lookup import lookup_codes, parse_line_items
from agent.tools.pdf_extractor import extract_bill_text

load_dotenv()

st.set_page_config(page_title="Medical Bill Auditor", page_icon="🧾", layout="wide")

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
SEVERITY_EMOJI = {"high": "🔴", "medium": "🟠", "low": "🟡"}


def extract_text_from_upload(uploaded_file) -> str:
    """Write the uploaded PDF to a temp file and return its extracted raw text."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(uploaded_file.getvalue())
        tmp_path = tmp.name
    try:
        return extract_bill_text(tmp_path)["raw_text"]
    finally:
        os.unlink(tmp_path)


def build_dispute_letter(line_items: list[dict], result: dict) -> str:
    """Build a template dispute letter from the flagged findings.

    Deterministic and self-contained so a patient gets a usable letter even without the
    LLM. Placeholders in [brackets] are for the patient to fill in.
    """
    findings = result["findings"]
    total = result["summary"]["total_estimated_overcharge"]

    disputed_lines = []
    for f in findings:
        # Pull the original charge for this code, if available.
        item = next((li for li in line_items if li["code"] == f["code"]), None)
        charge = f" (charged ${item['charge']:,.2f})" if item else ""
        disputed_lines.append(f"  - Code {f['code']}{charge}: {f['message']}")
    disputed_block = "\n".join(disputed_lines) if disputed_lines else "  - (none)"

    savings_line = (
        f"Based on typical and Medicare-allowed rates, I estimate potential overcharges "
        f"of approximately ${total:,.2f}."
        if total > 0
        else "Please provide documentation supporting the charges noted above."
    )

    return f"""[Your Name]
[Your Address]
[City, State ZIP]
[Date]

Billing Department
[Provider / Hospital Name]
[Provider Address]

Re: Dispute of charges on account [Account Number]
    Patient: [Patient Name]    Date(s) of service: [Date of Service]

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
disputed amounts until this matter is resolved. I can be reached at [Your Phone] or
[Your Email].

Thank you for your prompt attention.

Sincerely,
[Your Name]

---
Note: This letter is generated from an automated review using estimated reference pricing.
Flagged items are charges to question with your provider, not proven billing errors.
"""


def render_findings(result: dict) -> None:
    """Render summary metrics and the findings table."""
    summary = result["summary"]
    findings = result["findings"]

    c1, c2, c3 = st.columns(3)
    c1.metric("Line items", summary["line_item_count"])
    c2.metric("Issues flagged", summary["finding_count"])
    c3.metric("Est. potential overcharge", f"${summary['total_estimated_overcharge']:,.2f}")

    if not findings:
        st.success("No anomalies found. The bill appears consistent with typical pricing.")
        return

    if summary["has_high_severity"]:
        st.warning("This bill contains high-severity issues worth disputing. See below.")

    rows = sorted(findings, key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    table = pd.DataFrame(
        [
            {
                "Severity": f"{SEVERITY_EMOJI.get(f['severity'], '')} {f['severity'].title()}",
                "Code": f["code"],
                "Type": f["type"].replace("_", " ").title(),
                "Est. overcharge": f"${f['estimated_overcharge']:,.2f}",
                "Details": f["message"],
            }
            for f in rows
        ]
    )
    st.dataframe(table, use_container_width=True, hide_index=True)


def render_ai_summary(raw_text: str) -> None:
    """Render the LLM-generated plain-language summary, if an API key is configured."""
    if not os.getenv("GOOGLE_API_KEY"):
        st.info(
            "Set GOOGLE_API_KEY (in your environment or a .env file) to enable the "
            "AI-written plain-language summary. The audit above runs without it."
        )
        return

    with st.spinner("Generating AI summary..."):
        try:
            # Imported lazily so the app loads even if the LLM deps aren't configured.
            from agent.graph import audit_bill

            st.markdown(audit_bill(raw_text))
        except Exception as exc:  # noqa: BLE001 - surface any agent/model error to the user
            st.error(f"Could not generate the AI summary: {exc}")


def main() -> None:
    st.title("🧾 Medical Bill Auditor")
    st.caption(
        "Upload a medical bill PDF to check it for overcharges, duplicate charges, and "
        "billing anomalies — and generate a dispute letter."
    )

    uploaded = st.file_uploader("Upload a medical bill (PDF)", type=["pdf"])
    if uploaded is None:
        st.stop()

    with st.spinner("Reading the bill..."):
        raw_text = extract_text_from_upload(uploaded)

    if not raw_text.strip():
        st.error("No text could be extracted from this PDF. It may be a scanned image.")
        st.stop()

    line_items = lookup_codes(parse_line_items(raw_text))
    if not line_items:
        st.error(
            "No billable line items were recognized in this PDF. The bill format may "
            "not be supported yet."
        )
        with st.expander("View extracted text"):
            st.text(raw_text)
        st.stop()

    result = detect_anomalies(line_items)

    st.subheader("Audit results")
    render_findings(result)

    st.subheader("AI summary")
    render_ai_summary(raw_text)

    st.subheader("Dispute letter")
    letter = build_dispute_letter(line_items, result)
    st.text_area("Editable dispute letter", value=letter, height=400)
    st.download_button(
        "Download dispute letter",
        data=letter,
        file_name="dispute_letter.txt",
        mime="text/plain",
    )

    with st.expander("View extracted bill text"):
        st.text(raw_text)


if __name__ == "__main__":
    main()
