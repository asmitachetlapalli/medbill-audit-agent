"""Parse medical-bill line items and look up billing codes against the reference dataset.

Two responsibilities:
  1. `parse_line_items` turns the raw text from the PDF extractor into structured
     line items (code, description, quantity, charge).
  2. `lookup_codes` enriches each line item with reference data (Medicare rate,
     typical charge, per-day unit limit) loaded from data/cpt_codes.csv.

The `lookup_bill` LangChain tool ties both together so the agent can call it in one step.
"""

import os
import re
from functools import lru_cache

import pandas as pd
from langchain_core.tools import tool

# data/cpt_codes.csv lives two directories up from this file (agent/tools/ -> repo root).
_REFERENCE_CSV = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "cpt_codes.csv"
)

# CPT codes are 5 digits; HCPCS Level II codes are a letter followed by 4 digits.
_CODE_TOKEN = re.compile(r"\b([A-Z]\d{4}|\d{5})\b")
# A line item starts with a date of service: MM/DD/YYYY or MM/DD/YY.
_DATE = re.compile(r"^(\d{2}/\d{2}/\d{2,4})")
# Trailing "<qty> $<amount>" at the end of a line, e.g. "... 1 $462.00".
_QTY_CHARGE = re.compile(r"(\d+)\s+\$([\d,]+\.\d{2})\s*$")


@lru_cache(maxsize=1)
def load_reference() -> pd.DataFrame:
    """Load the reference code dataset, indexed by billing code (cached)."""
    df = pd.read_csv(_REFERENCE_CSV)
    df["code"] = df["code"].astype(str).str.strip()
    return df.set_index("code")


def parse_line_items(raw_text: str) -> list[dict]:
    """Extract structured line items from the raw bill text.

    Returns a list of dicts: {code, description, quantity, charge}. Only lines that
    begin with a date, contain a recognizable code token, and end with a quantity and
    dollar charge are treated as billable line items; headers and totals are skipped.
    """
    reference = load_reference()
    known_codes = set(reference.index)
    line_items: list[dict] = []

    for line in raw_text.splitlines():
        line = line.strip()
        if not _DATE.match(line):
            continue

        charge_match = _QTY_CHARGE.search(line)
        if not charge_match:
            continue

        # A line may carry several code-like tokens (NDC, procedure code). Prefer the
        # one present in our reference set; otherwise fall back to the last token, which
        # in this bill layout is the procedure code.
        codes = _CODE_TOKEN.findall(line)
        if not codes:
            continue
        code = next((c for c in codes if c in known_codes), codes[-1])

        quantity = int(charge_match.group(1))
        charge = float(charge_match.group(2).replace(",", ""))

        # Description is the text between the code and the trailing dx/rev/qty/charge.
        desc = line[line.index(code) + len(code):charge_match.start()].strip()
        # Strip any leading code-like tokens (e.g. a repeated NDC/procedure column).
        desc = re.sub(r"^(?:\s*(?:[A-Z]\d{4}|\d{5}))+\s*", "", desc)
        # Drop leading dx/ndc tokens and trailing rev/dx codes that aren't prose.
        desc = re.sub(r"\s+\b[A-Z]\d{2,3}\b", "", desc)  # e.g. dx code "I10"
        desc = re.sub(r"\s+\d{3}$", "", desc).strip()     # e.g. rev code "261"

        line_items.append(
            {
                "code": code,
                "description": desc,
                "quantity": quantity,
                "charge": charge,
            }
        )

    return line_items


def get_code_detail(code: str) -> dict:
    """Return reference detail for a single billing code, for explaining/reasoning.

    {known, code, description, category, medicare_rate, typical_charge, max_units_per_day}.
    Unknown codes return {known: False}.
    """
    reference = load_reference()
    code = str(code).strip()
    if code not in reference.index:
        return {"code": code, "known": False, "message": "Code not in reference dataset."}
    row = reference.loc[code]
    return {
        "code": code,
        "known": True,
        "description": row["description"],
        "category": row["category"],
        "medicare_rate": float(row["medicare_rate"]),
        "typical_charge": float(row["typical_charge"]),
        "max_units_per_day": int(row["max_units_per_day"]),
    }


def parse_bill_metadata(raw_text: str) -> dict:
    """Best-effort extraction of header fields used to pre-fill a dispute letter.

    Returns {patient_name, account_number, date_of_service, provider}. Any field that
    cannot be found is an empty string. Tuned to the sample bill layout; callers should
    treat these as editable suggestions, not authoritative.
    """

    def _find(pattern: str) -> str:
        m = re.search(pattern, raw_text, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    return {
        "patient_name": _find(r"for patient ([A-Za-z .,'-]+?)\."),
        "account_number": _find(r"for account (\d+)"),
        # First date of a "MM/DD/YY - MM/DD/YY" service range, or any standalone date.
        "date_of_service": _find(r"(\d{2}/\d{2}/\d{2,4})\s*-\s*\d{2}/\d{2}/\d{2,4}"),
        "provider": _find(r"Provider:\s*(.+)"),
    }


def lookup_codes(line_items: list[dict]) -> list[dict]:
    """Enrich each line item with reference data for its billing code.

    Adds: known (bool), reference_description, category, medicare_rate,
    typical_charge, max_units_per_day. Unknown codes get known=False and null
    reference fields so the anomaly detector can flag them.
    """
    reference = load_reference()
    enriched: list[dict] = []

    for item in line_items:
        code = item["code"]
        row = reference.loc[code] if code in reference.index else None
        enriched.append(
            {
                **item,
                "known": row is not None,
                "reference_description": None if row is None else row["description"],
                "category": None if row is None else row["category"],
                "medicare_rate": None if row is None else float(row["medicare_rate"]),
                "typical_charge": None if row is None else float(row["typical_charge"]),
                "max_units_per_day": None if row is None else int(row["max_units_per_day"]),
            }
        )

    return enriched


@tool
def lookup_bill(raw_text: str) -> list[dict]:
    """Parse a medical bill's raw text into line items and enrich each with reference
    pricing data (Medicare rate, typical charge, per-day unit limit) from the CPT/HCPCS
    dataset. Use this before checking a bill for anomalies."""
    return lookup_codes(parse_line_items(raw_text))


if __name__ == "__main__":
    from agent.tools.pdf_extractor import extract_bill_text

    text = extract_bill_text("sample_bills/sample.pdf")["raw_text"]
    for li in lookup_codes(parse_line_items(text)):
        print(li)
