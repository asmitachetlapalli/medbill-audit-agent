"""Read bills from and write audits to Snowflake (DATA_SOURCE=snowflake only)."""

import pandas as pd

from agent.audit import findings_to_rows, new_run_id
from agent.db import query_df, write_df


def list_bills() -> pd.DataFrame:
    """All bills, newest service date first: bill_id, provider, service_date, ..."""
    return query_df(
        "SELECT BILL_ID, SOURCE, PROVIDER, PROVIDER_TYPE, SERVICE_DATE, "
        "LINE_ITEM_COUNT, TOTAL_CHARGED FROM BILLS ORDER BY SERVICE_DATE DESC, BILL_ID"
    )


def get_line_items(bill_id: str) -> list[dict]:
    """A bill's line items in the parser's shape: {code, description, quantity, charge}."""
    df = query_df(
        "SELECT CODE, DESCRIPTION, QUANTITY, CHARGE FROM BILL_LINE_ITEMS "
        "WHERE BILL_ID = %s ORDER BY LINE_NO",
        (bill_id,),
    )
    return [
        {
            "code": str(r.code),
            "description": r.description or "",
            "quantity": int(r.quantity or 1),
            "charge": float(r.charge),
        }
        for r in df.itertuples()
    ]


def get_all_line_items() -> dict[str, list[dict]]:
    """Every bill's line items in one query, keyed by bill_id (for batch audits)."""
    df = query_df(
        "SELECT BILL_ID, CODE, DESCRIPTION, QUANTITY, CHARGE FROM BILL_LINE_ITEMS "
        "ORDER BY BILL_ID, LINE_NO"
    )
    bills: dict[str, list[dict]] = {}
    for r in df.itertuples():
        bills.setdefault(r.bill_id, []).append(
            {
                "code": str(r.code),
                "description": r.description or "",
                "quantity": int(r.quantity or 1),
                "charge": float(r.charge),
            }
        )
    return bills


def save_audits(audits: dict[str, dict], overcharge_ratio: float, run_id: str | None = None) -> dict:
    """Write audits ({bill_id: audit_line_items(...) output}) as one run.

    Adds one AUDIT_RUNS row per bill and one AUDIT_RESULTS row per finding. Returns
    {run_id, bills, findings}.
    """
    run_id = run_id or new_run_id()
    results = [findings_to_rows(bill_id, run_id, a) for bill_id, a in audits.items()]
    results = [r for r in results if not r.empty]
    results_df = pd.concat(results, ignore_index=True) if results else pd.DataFrame()
    runs_df = pd.DataFrame(
        [
            {
                "run_id": run_id,
                "bill_id": bill_id,
                "overcharge_ratio": overcharge_ratio,
                "line_item_count": len(a["line_items"]),
                "finding_count": int((results_df["bill_id"] == bill_id).sum())
                if not results_df.empty
                else 0,
            }
            for bill_id, a in audits.items()
        ]
    )
    # Results first, then runs: the LATEST_AUDIT_* views key off AUDIT_RUNS, so a run
    # only becomes "latest" once its findings are already in place.
    write_df(results_df, "AUDIT_RESULTS")
    write_df(runs_df, "AUDIT_RUNS")
    return {"run_id": run_id, "bills": len(runs_df), "findings": len(results_df)}
