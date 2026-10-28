"""CMS Medicare reference pricing.

Source file (download it yourself from data.cms.gov): "Medicare Physician & Other
Practitioners - by Geography and Service". Save the CSV as
data/raw/cms_physician_by_geo.csv. Columns used:

  Rndrng_Prvdr_Geo_Lvl  "National" or "State" — only National rows are kept
  HCPCS_Cd, HCPCS_Desc  billing code and its description
  Place_Of_Srvc         F (facility) or O (office)
  Tot_Rndrng_Prvdrs     number of providers billing the code (optional)
  Tot_Srvcs             number of services (used to weight the averages)
  Avg_Sbmtd_Chrg        average charge providers submitted
  Avg_Mdcr_Alowd_Amt    average amount Medicare allowed — the fair-price benchmark
  Avg_Mdcr_Pymt_Amt     average amount Medicare paid

`clean_cms_file` turns that CSV into rows for the CMS_REFERENCE_PRICING table.
`load_cms_reference` returns one price per code: from the CMS_REFERENCE_NATIONAL view in
Snowflake mode, or from the local CSV (if present) in local mode. With no CMS data the
audit still works — the CMS fields are simply left empty.
"""

import os
from functools import lru_cache

import pandas as pd

from agent.db import query_df, use_snowflake

DEFAULT_CMS_CSV = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "raw", "cms_physician_by_geo.csv"
)

# CMS column name -> our column name. Matched case-insensitively.
_COLUMN_MAP = {
    "hcpcs_cd": "hcpcs_code",
    "hcpcs_desc": "hcpcs_description",
    "place_of_srvc": "place_of_service",
    "tot_rndrng_prvdrs": "total_providers",
    "tot_srvcs": "total_services",
    "avg_sbmtd_chrg": "avg_submitted_charge",
    "avg_mdcr_alowd_amt": "avg_allowed_amount",
    "avg_mdcr_pymt_amt": "avg_payment_amount",
}
_REQUIRED = {"hcpcs_code", "total_services", "avg_submitted_charge", "avg_allowed_amount"}
# Prefer a code's office price when it has at least this many office services.
_MIN_OFFICE_SERVICES = 1000
_NUMERIC = (
    "total_providers",
    "total_services",
    "avg_submitted_charge",
    "avg_allowed_amount",
    "avg_payment_amount",
)


def _to_number(series: pd.Series) -> pd.Series:
    """Parse numbers that may arrive as text like "$1,234.50"; blanks become NaN."""
    cleaned = series.astype(str).str.replace(r"[$,\s]", "", regex=True)
    return pd.to_numeric(cleaned.replace({"": None, "nan": None}), errors="coerce")


def clean_cms_file(path: str) -> pd.DataFrame:
    """Read the raw CMS CSV and return clean national-level rows.

    Keeps only National rows, renames columns to snake_case, keeps codes as text (so
    leading zeros and letters survive), parses money columns, and drops rows without a
    code or allowed amount. One row per code per place of service.
    """
    # dtype=str: let us parse numbers ourselves and never mangle codes like "0001A".
    raw = pd.read_csv(path, dtype=str, low_memory=False)
    lower = {c: c.strip().lower() for c in raw.columns}
    raw = raw.rename(columns=lower)

    geo_col = "rndrng_prvdr_geo_lvl"
    if geo_col in raw.columns:
        raw = raw[raw[geo_col].str.strip().str.lower() == "national"]

    df = raw[[c for c in _COLUMN_MAP if c in raw.columns]].rename(columns=_COLUMN_MAP)
    missing = _REQUIRED - set(df.columns)
    if missing:
        raise ValueError(
            f"{os.path.basename(path)} is missing expected CMS columns for: "
            f"{', '.join(sorted(missing))}. Is this the 'by Geography and Service' file?"
        )

    df["hcpcs_code"] = df["hcpcs_code"].str.strip().str.upper()
    for col in _NUMERIC:
        if col in df.columns:
            df[col] = _to_number(df[col])
    if "total_providers" in df.columns:
        df["total_providers"] = df["total_providers"].astype("Int64")

    df = df.dropna(subset=["hcpcs_code", "avg_allowed_amount"])
    df = df[df["hcpcs_code"] != ""]
    df["source_file"] = os.path.basename(path)
    return df.reset_index(drop=True)


def national_prices(rows: pd.DataFrame) -> pd.DataFrame:
    """Collapse facility/office rows into one reference price per code.

    Facility rows in this physician dataset often price only the professional part of a
    service (e.g. reading a CT, not the scan), so the office price is preferred when it
    has at least _MIN_OFFICE_SERVICES services; otherwise a service-weighted average
    across settings. `price_basis` says which. Mirrors CMS_REFERENCE_NATIONAL in
    sql/01_setup.sql.
    """
    columns = ["hcpcs_description", "price_basis", "avg_submitted_charge", "avg_allowed_amount"]
    if rows.empty:
        return pd.DataFrame(columns=columns)
    code = rows["hcpcs_code"]
    w = rows["total_services"].fillna(0)
    is_office = (
        rows["place_of_service"].eq("O")
        if "place_of_service" in rows
        else pd.Series(False, index=rows.index)
    )

    def weighted(col: str) -> pd.Series:
        num = (rows[col] * w).groupby(code).sum()
        den = w.groupby(code).sum()
        return num / den.where(den > 0)

    def office(col: str) -> pd.Series:
        return rows[col].where(is_office).groupby(code).max()

    use_office = (w.where(is_office, 0).groupby(code).sum() >= _MIN_OFFICE_SERVICES)
    out = pd.DataFrame(
        {
            "hcpcs_description": rows.groupby(code)["hcpcs_description"].first()
            if "hcpcs_description" in rows.columns
            else None,
            "price_basis": use_office.map({True: "office", False: "all settings"}),
        }
    )
    for col in ("avg_submitted_charge", "avg_allowed_amount"):
        out[col] = office(col).where(use_office, weighted(col)).round(2)
    return out[columns]


@lru_cache(maxsize=1)
def load_cms_reference() -> pd.DataFrame:
    """One CMS reference price per code, indexed by code (cached).

    Columns: hcpcs_description, price_basis, avg_submitted_charge, avg_allowed_amount.
    Empty when no CMS data is available.
    """
    if use_snowflake():
        df = query_df(
            "SELECT HCPCS_CODE, HCPCS_DESCRIPTION, PRICE_BASIS, AVG_SUBMITTED_CHARGE, "
            "AVG_ALLOWED_AMOUNT FROM CMS_REFERENCE_NATIONAL"
        )
        for col in ("avg_submitted_charge", "avg_allowed_amount"):
            df[col] = pd.to_numeric(df[col], errors="coerce")  # NUMBER arrives as Decimal
        return df.set_index("hcpcs_code")
    if os.path.exists(DEFAULT_CMS_CSV):
        return national_prices(clean_cms_file(DEFAULT_CMS_CSV))
    return national_prices(pd.DataFrame())


def get_cms_price(code: str) -> dict | None:
    """CMS reference for one code, or None if CMS has no row for it."""
    cms = load_cms_reference()
    if code not in cms.index:
        return None
    row = cms.loc[code]
    return {
        "description": row["hcpcs_description"],
        "avg_submitted_charge": float(row["avg_submitted_charge"])
        if pd.notna(row["avg_submitted_charge"])
        else None,
        "avg_allowed_amount": float(row["avg_allowed_amount"]),
    }
