"""Flag billing anomalies on enriched bill line items.

Consumes the output of `cpt_lookup.lookup_codes` (line items enriched with reference
pricing) and returns structured findings. Detected anomaly types:

  - overcharge      : charge per unit is well above the typical fair price.
  - excess_quantity : units billed exceed the normal per-day maximum.
  - duplicate       : the same code appears on more than one line item.
  - unknown_code    : the code is not in the reference dataset and can't be verified.

Each finding carries a severity (high/medium/low) and a human-readable explanation.
The summary totals the estimated potential overcharge across all findings.
"""

from collections import Counter

from langchain_core.tools import tool

# A charge above this multiple of the typical fair price is flagged as an overcharge.
_OVERCHARGE_RATIO = 1.5
# Above this multiple it is treated as a high-severity overcharge.
_HIGH_OVERCHARGE_RATIO = 3.0


def detect_anomalies(
    line_items: list[dict],
    overcharge_ratio: float = _OVERCHARGE_RATIO,
    high_overcharge_ratio: float = _HIGH_OVERCHARGE_RATIO,
) -> dict:
    """Inspect enriched line items and return {findings, summary}.

    `line_items` is the output of cpt_lookup.lookup_codes. Each finding is a dict with
    code, type, severity, message, and (where applicable) the estimated overcharge.

    `overcharge_ratio` is the multiple of the typical fair price above which a charge is
    flagged; `high_overcharge_ratio` is the multiple above which it is high-severity.
    """
    findings: list[dict] = []
    total_overcharge = 0.0

    # Count code occurrences to detect duplicate billing.
    code_counts = Counter(item["code"] for item in line_items)

    seen_duplicates: set[str] = set()
    for item in line_items:
        code = item["code"]
        charge = item["charge"]
        quantity = max(item.get("quantity") or 1, 1)
        per_unit = charge / quantity

        # Unknown code: nothing to compare against.
        if not item.get("known"):
            findings.append(
                {
                    "code": code,
                    "type": "unknown_code",
                    "severity": "medium",
                    "message": (
                        f"Code {code} ('{item['description']}') is not in the reference "
                        f"dataset and could not be verified. Charged ${charge:,.2f}."
                    ),
                    "estimated_overcharge": 0.0,
                }
            )
            continue

        typical = item["typical_charge"]
        medicare = item["medicare_rate"]

        # Overcharge: per-unit charge well above the typical fair price.
        if typical and per_unit > typical * overcharge_ratio:
            overcharge = (per_unit - typical) * quantity
            total_overcharge += overcharge
            ratio = per_unit / typical
            medicare_mult = (per_unit / medicare) if medicare else None
            severity = "high" if ratio >= high_overcharge_ratio else "medium"
            msg = (
                f"Code {code} ('{item['reference_description']}') charged "
                f"${per_unit:,.2f}/unit vs a typical ${typical:,.2f} "
                f"({ratio:.1f}x)."
            )
            if medicare_mult:
                msg += f" That is {medicare_mult:.0f}x the Medicare rate of ${medicare:,.2f}."
            findings.append(
                {
                    "code": code,
                    "type": "overcharge",
                    "severity": severity,
                    "message": msg,
                    "estimated_overcharge": round(overcharge, 2),
                }
            )

        # Excess quantity: more units than normally billable per day.
        max_units = item.get("max_units_per_day")
        if max_units and quantity > max_units:
            findings.append(
                {
                    "code": code,
                    "type": "excess_quantity",
                    "severity": "high",
                    "message": (
                        f"Code {code} billed {quantity} units, but the normal maximum is "
                        f"{max_units} per day. Verify the extra units are justified."
                    ),
                    "estimated_overcharge": 0.0,
                }
            )

        # Duplicate: same code on multiple line items (report once per code).
        if code_counts[code] > 1 and code not in seen_duplicates:
            seen_duplicates.add(code)
            findings.append(
                {
                    "code": code,
                    "type": "duplicate",
                    "severity": "medium",
                    "message": (
                        f"Code {code} appears on {code_counts[code]} separate line items. "
                        f"Confirm these are distinct services and not duplicate billing."
                    ),
                    "estimated_overcharge": 0.0,
                }
            )

    summary = {
        "line_item_count": len(line_items),
        "finding_count": len(findings),
        "total_estimated_overcharge": round(total_overcharge, 2),
        "has_high_severity": any(f["severity"] == "high" for f in findings),
    }

    return {"findings": findings, "summary": summary}


def make_find_anomalies_tool(
    overcharge_ratio: float = _OVERCHARGE_RATIO,
    high_overcharge_ratio: float = _HIGH_OVERCHARGE_RATIO,
):
    """Build a `find_anomalies` agent tool bound to a specific overcharge sensitivity.

    The thresholds are closed over here rather than passed by the LLM, so the UI's
    sensitivity slider deterministically controls what the agent flags.
    """

    @tool
    def find_anomalies(line_items: list[dict]) -> dict:
        """Check enriched bill line items for billing anomalies: overcharges, excess
        quantities, duplicate charges, and unverifiable codes. Returns findings with
        severity levels and an estimated total overcharge. Call this after lookup_bill."""
        return detect_anomalies(line_items, overcharge_ratio, high_overcharge_ratio)

    return find_anomalies


# Default-sensitivity tool instance, for callers that don't customize the threshold.
find_anomalies = make_find_anomalies_tool()


if __name__ == "__main__":
    from agent.tools.cpt_lookup import lookup_codes, parse_line_items
    from agent.tools.pdf_extractor import extract_bill_text

    text = extract_bill_text("sample_bills/sample.pdf")["raw_text"]
    result = detect_anomalies(lookup_codes(parse_line_items(text)))
    import json

    print(json.dumps(result, indent=2))
