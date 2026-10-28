"""Data-source switch and Snowflake connection helpers.

`DATA_SOURCE` (env / .env) picks where reference data and bills come from:
  - "local"     (default): CSV files in data/ and uploaded PDFs. No Snowflake needed.
  - "snowflake": reference tables, bills, and audit results live in Snowflake.

Credentials are read only from the environment (SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER,
SNOWFLAKE_WAREHOUSE, SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA, the optional SNOWFLAKE_ROLE,
and either SNOWFLAKE_PRIVATE_KEY_FILE or SNOWFLAKE_PASSWORD) — never hardcoded. The connector is imported lazily so local
mode works without it installed.
"""

import os
from functools import lru_cache

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

_REQUIRED_VARS = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_DATABASE",
    "SNOWFLAKE_SCHEMA",
)


def data_source() -> str:
    """The configured data source: "local" or "snowflake"."""
    source = os.getenv("DATA_SOURCE", "local").strip().lower()
    if source not in ("local", "snowflake"):
        raise ValueError(f"DATA_SOURCE must be 'local' or 'snowflake', got {source!r}.")
    return source


def use_snowflake() -> bool:
    return data_source() == "snowflake"


@lru_cache(maxsize=1)
def get_connection():
    """Open (once per process) and return a Snowflake connection from env credentials."""
    missing = [v for v in _REQUIRED_VARS if not os.getenv(v)]
    key_file = os.getenv("SNOWFLAKE_PRIVATE_KEY_FILE")
    if not key_file and not os.getenv("SNOWFLAKE_PASSWORD"):
        missing.append("SNOWFLAKE_PASSWORD or SNOWFLAKE_PRIVATE_KEY_FILE")
    if missing:
        raise RuntimeError(
            f"Missing Snowflake settings: {', '.join(missing)}. Add them to your .env file."
        )

    import snowflake.connector  # lazy: only needed in Snowflake mode

    # Key-pair auth (a private key file) is preferred when set: no MFA prompts or token
    # expiry. Otherwise a password or a programmatic access token (PAT) both work.
    auth = (
        {"private_key_file": os.path.expanduser(key_file)}
        if key_file
        else {"password": os.environ["SNOWFLAKE_PASSWORD"]}
    )
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        **auth,
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        database=os.environ["SNOWFLAKE_DATABASE"],
        schema=os.environ["SNOWFLAKE_SCHEMA"],
        role=os.getenv("SNOWFLAKE_ROLE") or None,
    )


def query_df(sql: str, params=None) -> pd.DataFrame:
    """Run a query and return the result as a DataFrame with lower-case column names
    (Snowflake returns unquoted identifiers upper-cased)."""
    cur = get_connection().cursor()
    try:
        cur.execute(sql, params)
        df = cur.fetch_pandas_all()
    finally:
        cur.close()
    df.columns = [c.lower() for c in df.columns]
    return df


def write_df(df: pd.DataFrame, table: str, overwrite: bool = False) -> int:
    """Bulk-load a DataFrame into an existing table (columns matched by name).

    Uses write_pandas, which stages the data as Parquet and runs a single COPY INTO —
    much faster than row-by-row INSERTs. `overwrite=True` truncates the table first.
    Columns left out of `df` get their table defaults (e.g. LOADED_AT timestamps).
    Returns the number of rows written.
    """
    if df.empty:
        return 0

    from snowflake.connector.pandas_tools import write_pandas

    upper = df.rename(columns=str.upper)
    ok, _, nrows, _ = write_pandas(
        get_connection(),
        upper,
        table.upper(),
        quote_identifiers=False,
        overwrite=overwrite,
        use_logical_type=True,
    )
    if not ok:
        raise RuntimeError(f"write_pandas into {table} reported failure.")
    return nrows


def execute(sql: str, params=None) -> None:
    """Run a statement that returns no result we need (DELETE, etc.)."""
    cur = get_connection().cursor()
    try:
        cur.execute(sql, params)
    finally:
        cur.close()
