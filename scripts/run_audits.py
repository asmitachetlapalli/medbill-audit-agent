"""Audit every bill in Snowflake and write the findings back.

Reads BILL_LINE_ITEMS, runs the deterministic audit (pricing, overcharges, quantities,
duplicates, unbundling — no LLM), and writes one AUDIT_RUNS row per bill plus one
AUDIT_RESULTS row per finding under a single run_id. Re-running adds a new run; the
LATEST_AUDIT_* views always show each bill's most recent one.

Requires DATA_SOURCE=snowflake so reference prices come from Snowflake too.

    python -m scripts.run_audits                 # all bills, default sensitivity 1.5x
    python -m scripts.run_audits --ratio 2.0
    python -m scripts.run_audits --bill-id SYN-0001
"""

import argparse
from collections import Counter

from agent.audit import audit_line_items
from agent.bill_store import get_all_line_items, get_line_items, save_audits
from agent.db import use_snowflake


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit bills in Snowflake.")
    parser.add_argument("--ratio", type=float, default=1.5,
                        help="Flag charges above this multiple of the typical price.")
    parser.add_argument("--bill-id", help="Audit only this bill.")
    args = parser.parse_args()

    if not use_snowflake():
        raise SystemExit("Set DATA_SOURCE=snowflake in .env to audit bills stored in Snowflake.")

    bills = (
        {args.bill_id: get_line_items(args.bill_id)} if args.bill_id else get_all_line_items()
    )
    bills = {k: v for k, v in bills.items() if v}
    if not bills:
        raise SystemExit("No bills found. Run python -m scripts.load_data first.")

    audits = {bill_id: audit_line_items(items, args.ratio) for bill_id, items in bills.items()}
    summary = save_audits(audits, args.ratio)

    by_type = Counter(
        f["type"] for a in audits.values() for f in a["result"]["findings"]
    ) + Counter("unbundling" for a in audits.values() for _ in a["unbundling"]["findings"])
    print(f"Run {summary['run_id']}: audited {summary['bills']} bills, "
          f"wrote {summary['findings']} findings.")
    for kind, n in by_type.most_common():
        print(f"  {kind}: {n}")


if __name__ == "__main__":
    main()
