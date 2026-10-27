"""Run the analysis queries and save each result as a CSV for Tableau Public.

Splits sql/02_analysis.sql on its "-- name: <export_name>" lines, runs each query in
Snowflake, and writes data/exports/<export_name>.csv. In Tableau Public, use
Connect > Text file on any of these CSVs.

    python -m scripts.export_for_tableau
"""

import os
import re

from agent.db import query_df

ROOT = os.path.join(os.path.dirname(__file__), "..")
ANALYSIS_SQL = os.path.join(ROOT, "sql", "02_analysis.sql")
EXPORT_DIR = os.path.join(ROOT, "data", "exports")

_NAME_LINE = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.MULTILINE)


def named_queries(path: str = ANALYSIS_SQL) -> dict[str, str]:
    """{export_name: sql} for every "-- name:" section. Text before the first marker
    (the USE statements) is skipped — the connection already sets warehouse/schema."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    parts = _NAME_LINE.split(text)
    # split() yields [preamble, name1, body1, name2, body2, ...]
    return {
        name: body.strip().rstrip(";").strip()
        for name, body in zip(parts[1::2], parts[2::2])
    }


def main() -> None:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    for name, sql in named_queries().items():
        df = query_df(sql)
        out = os.path.join(EXPORT_DIR, f"{name}.csv")
        df.to_csv(out, index=False)
        print(f"  {name}: {len(df)} rows -> {os.path.normpath(out)}")
    print("Done. Load these CSVs in Tableau Public (Connect > Text file).")


if __name__ == "__main__":
    main()
