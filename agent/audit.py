"""Deterministic bill audit and conversion of its findings into AUDIT_RESULTS rows.

This is the reproducible half of the system: no LLM, same input -> same findings. The
batch script (scripts/run_audits.py) and the app's "Save findings" button both use it,
so what lands in Snowflake is never decided by the model.
"""

import uuid
from collections import defaultdict

import pandas as pd

from agent.tools.anomaly_detector import detect_anomalies
from agent.tools.cpt_lookup import lookup_codes
from agent.tools.unbundling import check_unbundling


def audit_line_items(raw_items: list[dict], overcharge_ratio: float = 1.5) -> dict:
    """Price and audit parsed line items ({code, description, quantity, charge}).

    Returns {line_items (enriched), result (detect_anomalies output), unbundling}.
    """
    line_items = lookup_codes(raw_items)
    return {
        "line_items": line_items,
        "result": detect_anomalies(line_items, overcharge_ratio),
        "unbundling": check_unbundling([li["code"] for li in line_items]),
    }


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


def findings_to_rows(bill_id: str, run_id: str, audit: dict) -> pd.DataFrame:
    """Flatten one bill's audit into AUDIT_RESULTS rows.

    AMOUNT_FLAGGED is the dollars in question for each finding:
      - overcharge      : the estimated excess over the typical price
      - excess_quantity : the units above the per-day limit, at the billed unit price
      - duplicate       : the charges on every repeat line beyond the first
      - unbundling      : the charge of the component code that is already included
                          ("includes"), or of the cheaper code ("mutually_exclusive")
      - unknown_code    : 0 — it can't be priced, only questioned
    Findings on the same line can overlap (an overcharged line can also be duplicated).
    """
    charges_by_code: dict[str, list[float]] = defaultdict(list)
    units_by_code: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for li in audit["line_items"]:
        qty = max(li.get("quantity") or 1, 1)
        charges_by_code[li["code"]].append(li["charge"])
        units_by_code[li["code"]].append((qty, li["charge"] / qty))

    rows = []
    for f in audit["result"]["findings"]:
        code, kind = f["code"], f["type"]
        if kind == "overcharge":
            amount = f["estimated_overcharge"]
        elif kind == "excess_quantity":
            limit = next(
                (li["max_units_per_day"] for li in audit["line_items"] if li["code"] == code),
                None,
            ) or 0
            amount = sum(max(q - limit, 0) * unit for q, unit in units_by_code[code])
        elif kind == "duplicate":
            amount = sum(sorted(charges_by_code[code], reverse=True)[1:])
        else:
            amount = 0.0
        rows.append(
            {
                "run_id": run_id,
                "bill_id": bill_id,
                "code": code,
                "related_code": None,
                "issue_type": kind,
                "severity": f["severity"],
                "description": f["message"],
                "amount_flagged": round(amount, 2),
            }
        )

    for u in audit["unbundling"]["findings"]:
        a_total = sum(charges_by_code.get(u["code_a"], []))
        b_total = sum(charges_by_code.get(u["code_b"], []))
        amount = b_total if u["relationship"] == "includes" else min(a_total, b_total)
        rows.append(
            {
                "run_id": run_id,
                "bill_id": bill_id,
                "code": u["code_a"],
                "related_code": u["code_b"],
                "issue_type": "unbundling",
                "severity": u["severity"],
                "description": u["message"],
                "amount_flagged": round(amount, 2),
            }
        )

    return pd.DataFrame(
        rows,
        columns=[
            "run_id", "bill_id", "code", "related_code", "issue_type",
            "severity", "description", "amount_flagged",
        ],
    )
