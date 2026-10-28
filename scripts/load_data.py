"""Load bills and reference data into Snowflake.

Loads, in order:
  1. CPT_REFERENCE          <- data/cpt_codes.csv
  2. CMS_REFERENCE_PRICING  <- data/raw/cms_physician_by_geo.csv (skipped if absent)
  3. BILLS + BILL_LINE_ITEMS <- data/raw/synthetic_bills.csv and every PDF in sample_bills/

Each table is truncated and reloaded, so the script is safe to re-run. Audit tables are
never touched. Run sql/01_setup.sql first.

    python -m scripts.load_data              # load everything
    python -m scripts.load_data --dry-run    # parse and validate only; no Snowflake
    python -m scripts.load_data --cms-file path/to/other.csv
"""

import argparse
import glob
import os
from datetime import datetime

import pandas as pd

from agent.tools.cms_reference import DEFAULT_CMS_CSV, clean_cms_file
from agent.tools.cpt_lookup import parse_bill_metadata, parse_line_items
from agent.tools.pdf_extractor import extract_bill_text

ROOT = os.path.join(os.path.dirname(__file__), "..")
CPT_CSV = os.path.join(ROOT, "data", "cpt_codes.csv")
SYNTHETIC_CSV = os.path.join(ROOT, "data", "raw", "synthetic_bills.csv")
PDF_DIR = os.path.join(ROOT, "sample_bills")

BILL_COLUMNS = ["bill_id", "source", "source_file", "provider", "provider_type",
                "service_date", "line_item_count", "total_charged"]
LINE_COLUMNS = ["bill_id", "line_no", "code", "description", "quantity", "charge",
                "planted_error"]


def load_cpt_reference() -> pd.DataFrame:
    df = pd.read_csv(CPT_CSV, dtype={"code": str})
    df["code"] = df["code"].str.strip()
    return df


def load_synthetic_bills() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split the flat synthetic CSV into a bills (header) frame and a line-items frame."""
    if not os.path.exists(SYNTHETIC_CSV):
        print(f"  (no {os.path.basename(SYNTHETIC_CSV)} — run scripts.generate_synthetic_bills)")
        return pd.DataFrame(columns=BILL_COLUMNS), pd.DataFrame(columns=LINE_COLUMNS)

    flat = pd.read_csv(SYNTHETIC_CSV, dtype={"code": str})
    flat["planted_error"] = flat["planted_error"].astype("object").where(
        flat["planted_error"].notna(), None
    )
    bills = (
        flat.groupby("bill_id")
        .agg(
            provider=("provider", "first"),
            provider_type=("provider_type", "first"),
            service_date=("service_date", "first"),
            line_item_count=("line_no", "count"),
            total_charged=("charge", "sum"),
        )
        .reset_index()
    )
    bills["source"] = "synthetic"
    bills["source_file"] = os.path.basename(SYNTHETIC_CSV)
    bills["total_charged"] = bills["total_charged"].round(2)
    return bills[BILL_COLUMNS], flat[LINE_COLUMNS]


def _parse_date(text: str):
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def load_pdf_bills() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Parse every PDF in sample_bills/ with the same parser the app uses."""
    bills, lines = [], []
    for path in sorted(glob.glob(os.path.join(PDF_DIR, "*.pdf"))):
        raw_text = extract_bill_text(path)["raw_text"]
        items = parse_line_items(raw_text)
        if not items:
            print(f"  skipped {os.path.basename(path)}: no line items recognized")
            continue
        meta = parse_bill_metadata(raw_text)
        bill_id = f"PDF-{os.path.splitext(os.path.basename(path))[0]}"
        bills.append(
            {
                "bill_id": bill_id,
                "source": "pdf",
                "source_file": os.path.basename(path),
                "provider": meta["provider"] or None,
                "provider_type": None,
                "service_date": _parse_date(meta["date_of_service"]),
                "line_item_count": len(items),
                "total_charged": round(sum(i["charge"] for i in items), 2),
            }
        )
        for n, item in enumerate(items, start=1):
            lines.append({"bill_id": bill_id, "line_no": n, **item, "planted_error": None})
    return (
        pd.DataFrame(bills, columns=BILL_COLUMNS),
        pd.DataFrame(lines, columns=LINE_COLUMNS),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Load bills and reference data into Snowflake.")
    parser.add_argument("--cms-file", default=DEFAULT_CMS_CSV,
                        help="CMS 'by Geography and Service' CSV (default: data/raw/cms_physician_by_geo.csv)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and validate everything, but don't connect to Snowflake.")
    args = parser.parse_args()

    print("Preparing data...")
    cpt = load_cpt_reference()
    print(f"  CPT_REFERENCE: {len(cpt)} codes")

    if os.path.exists(args.cms_file):
        cms = clean_cms_file(args.cms_file)
        print(f"  CMS_REFERENCE_PRICING: {len(cms)} national rows, "
              f"{cms['hcpcs_code'].nunique()} codes from {os.path.basename(args.cms_file)}")
    else:
        cms = None
        print(f"  CMS_REFERENCE_PRICING: skipped — {os.path.normpath(args.cms_file)} not found")

    syn_bills, syn_lines = load_synthetic_bills()
    pdf_bills, pdf_lines = load_pdf_bills()
    bills = pd.concat([syn_bills, pdf_bills], ignore_index=True)
    lines = pd.concat([syn_lines, pdf_lines], ignore_index=True)
    bills["service_date"] = pd.to_datetime(bills["service_date"]).dt.date
    print(f"  BILLS: {len(bills)} ({len(syn_bills)} synthetic, {len(pdf_bills)} from PDFs)")
    print(f"  BILL_LINE_ITEMS: {len(lines)} ({lines['planted_error'].notna().sum()} with planted errors)")

    if bills["bill_id"].duplicated().any():
        raise SystemExit("Duplicate bill_id values — refusing to load.")

    if args.dry_run:
        print("Dry run: nothing written.")
        return

    from agent.db import write_df  # imported here so --dry-run needs no Snowflake setup

    print("Loading into Snowflake...")
    print(f"  CPT_REFERENCE: {write_df(cpt, 'CPT_REFERENCE', overwrite=True)} rows")
    if cms is not None:
        print(f"  CMS_REFERENCE_PRICING: {write_df(cms, 'CMS_REFERENCE_PRICING', overwrite=True)} rows")
    # Line items reference bills, so load the parent table first.
    print(f"  BILLS: {write_df(bills, 'BILLS', overwrite=True)} rows")
    print(f"  BILL_LINE_ITEMS: {write_df(lines, 'BILL_LINE_ITEMS', overwrite=True)} rows")
    print("Done. Next: python -m scripts.run_audits")


if __name__ == "__main__":
    main()
