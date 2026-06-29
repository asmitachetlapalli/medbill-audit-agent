"""Detect unbundling: code pairs that should not be billed together.

When two codes have an "includes" or "mutually_exclusive" relationship and both appear on
a bill, the payer is likely being double-charged. The rules in data/unbundling_pairs.csv
are illustrative — they are not the official CMS/NCCI edit tables — so findings should be
treated as items to verify, not proven errors.
"""

import os
from functools import lru_cache

import pandas as pd

_RULES_CSV = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "unbundling_pairs.csv"
)

_DISCLAIMER = (
    "These are illustrative bundling rules, not official CMS/NCCI edits. "
    "Confirm against the provider's coding before disputing."
)


@lru_cache(maxsize=1)
def load_unbundling_rules() -> pd.DataFrame:
    """Load the code-pair rules (cached)."""
    df = pd.read_csv(_RULES_CSV)
    for col in ("code_a", "code_b"):
        df[col] = df[col].astype(str).str.strip()
    return df


def check_unbundling(codes: list[str]) -> dict:
    """Return unbundling findings for the codes present on a bill.

    A rule fires only when *both* of its codes appear in `codes`. Returns
    {findings, checked_pairs, disclaimer}.
    """
    present = {str(c).strip() for c in codes}
    rules = load_unbundling_rules()

    findings = []
    for _, r in rules.iterrows():
        if r["code_a"] in present and r["code_b"] in present:
            findings.append(
                {
                    "code_a": r["code_a"],
                    "code_b": r["code_b"],
                    "relationship": r["relationship"],
                    "severity": "medium",
                    "message": r["note"],
                }
            )

    return {
        "findings": findings,
        "checked_pairs": int(len(rules)),
        "disclaimer": _DISCLAIMER,
    }


if __name__ == "__main__":
    import json

    # A bill with a lipid panel billed alongside its components should flag.
    print(json.dumps(check_unbundling(["80061", "82465", "99213"]), indent=2))
