"""Generate synthetic medical bills with known, deliberately planted errors.

The repo ships one real sample bill, which is too little data for SQL analysis. This
script writes data/raw/synthetic_bills.csv: a few hundred bills from fictional
providers, priced around the curated typical charges, with errors planted at rates that
vary by provider. Each planted error is recorded in `planted_error`, so after auditing we
can measure how many the audit actually caught (sql/02_analysis.sql, query 6).

All providers and bills are fictional. Seeded, so every run produces the same file.

    python -m scripts.generate_synthetic_bills [--bills 300] [--seed 42]
"""

import argparse
import os
import random
from datetime import date, timedelta

import pandas as pd

ROOT = os.path.join(os.path.dirname(__file__), "..")
OUT_CSV = os.path.join(ROOT, "data", "raw", "synthetic_bills.csv")

# Fictional providers: (name, type, chance a bill gets a planted error, price markup).
PROVIDERS = [
    ("Riverside General Hospital", "Hospital", 0.35, 1.15),
    ("St. Aurelia Medical Center", "Hospital", 0.25, 1.25),
    ("Harborview Emergency Group", "Emergency", 0.30, 1.20),
    ("Lakeside Urgent Care", "Urgent Care", 0.15, 1.00),
    ("Northgate Family Practice", "Clinic", 0.08, 0.95),
    ("Pinecrest Internal Medicine", "Clinic", 0.12, 1.00),
    ("Summit Imaging Center", "Imaging", 0.20, 1.10),
    ("Valley Infusion Clinic", "Infusion", 0.18, 1.05),
]

# Every bill starts with one visit code, then 1-5 extra services from its type's pool.
VISIT_CODES = {
    "Hospital": ["99283", "99284", "99285"],
    "Emergency": ["99282", "99283", "99284", "99285"],
    "Urgent Care": ["99212", "99213", "99214", "99203"],
    "Clinic": ["99212", "99213", "99214", "99215", "99203", "99204"],
    "Imaging": ["99212", "99213"],
    "Infusion": ["99212", "99213"],
}
EXTRA_CODES = {
    "Hospital": ["36415", "80053", "85025", "71046", "74176", "70450", "93000", "96374",
                 "J1885", "J2270", "J7030", "J1100"],
    "Emergency": ["36415", "80053", "85025", "81001", "71045", "70450", "93000", "96374",
                  "J1885", "J3010", "J7030"],
    "Urgent Care": ["36415", "81001", "85025", "71046", "73610", "96372", "J1885", "J1100"],
    "Clinic": ["36415", "80053", "80061", "85025", "83036", "81001", "93000"],
    "Imaging": ["71045", "71046", "73610", "74176", "70450", "72148"],
    "Infusion": ["36415", "85025", "96365", "96372", "J0744", "J7030", "J1100", "J2270"],
}
# Codes with no real reference price — planted as "unknown_code" errors.
FAKE_CODES = [("Z9901", "Miscellaneous supply"), ("Z9902", "Facility convenience item")]

ERROR_TYPES = ["overcharge", "duplicate", "excess_quantity", "unbundling", "unknown_code"]
ERROR_WEIGHTS = [0.40, 0.20, 0.15, 0.15, 0.10]


def _load_reference():
    data = os.path.join(ROOT, "data")
    ref = pd.read_csv(os.path.join(data, "cpt_codes.csv"), dtype={"code": str})
    rules = pd.read_csv(os.path.join(data, "unbundling_pairs.csv"), dtype=str)
    ref = ref.set_index("code")
    # Only rules whose codes we can price are useful for planting.
    rules = rules[rules["code_a"].isin(ref.index) & rules["code_b"].isin(ref.index)]
    return ref, list(rules[["code_a", "code_b"]].itertuples(index=False, name=None))


def _price(ref, code, qty, markup, rng) -> float:
    """A normal-looking line charge: typical price x provider markup x small noise."""
    unit = ref.loc[code, "typical_charge"] * markup * rng.uniform(0.80, 1.25)
    return round(unit * qty, 2)


def _normal_qty(ref, code, rng) -> int:
    limit = int(ref.loc[code, "max_units_per_day"])
    return rng.randint(1, min(limit, 3)) if limit > 1 else 1


def _forms_pair(code, codes, pairs) -> bool:
    return any((code, c) in pairs or (c, code) in pairs for c in codes)


def generate(n_bills: int, seed: int) -> pd.DataFrame:
    rng = random.Random(seed)
    ref, pairs = _load_reference()
    pair_set = set(pairs)
    start = date(2025, 1, 1)
    rows = []

    for i in range(1, n_bills + 1):
        name, ptype, error_rate, markup = rng.choice(PROVIDERS)
        bill_id = f"SYN-{i:04d}"
        service_date = start + timedelta(days=rng.randrange(365))

        # Build a clean bill: a visit plus distinct extras that don't accidentally form
        # an unbundling pair (so every issue on the bill is either planted or pricing).
        codes = [rng.choice(VISIT_CODES[ptype])]
        for code in rng.sample(EXTRA_CODES[ptype], k=min(rng.randint(1, 5), len(EXTRA_CODES[ptype]))):
            if not _forms_pair(code, codes, pair_set):
                codes.append(code)
        lines = []
        for code in codes:
            qty = _normal_qty(ref, code, rng)
            lines.append([code, ref.loc[code, "description"], qty,
                          _price(ref, code, qty, markup, rng), None])

        if rng.random() < error_rate:
            kind = rng.choices(ERROR_TYPES, ERROR_WEIGHTS)[0]
            if kind == "overcharge":
                line = rng.choice(lines)
                # Some planted overcharges are subtle (1.3-1.5x) and fall under the default
                # 1.5x threshold, so detection rate reflects a real sensitivity trade-off.
                unit = ref.loc[line[0], "typical_charge"] * rng.uniform(1.3, 4.5)
                line[3], line[4] = round(unit * line[2], 2), "overcharge"
            elif kind == "duplicate":
                lines.append(list(rng.choice(lines)[:4]) + ["duplicate"])
            elif kind == "excess_quantity":
                line = rng.choice(lines)
                limit = int(ref.loc[line[0], "max_units_per_day"])
                unit = line[3] / line[2]
                line[2] = limit + rng.randint(1, 3)
                line[3], line[4] = round(unit * line[2], 2), "excess_quantity"
            elif kind == "unbundling":
                code_a, code_b = rng.choice(pairs)
                present = {l[0] for l in lines}
                if code_a not in present:
                    qty = _normal_qty(ref, code_a, rng)
                    lines.append([code_a, ref.loc[code_a, "description"], qty,
                                  _price(ref, code_a, qty, markup, rng), None])
                if code_b in present:  # already there: mark it rather than add a duplicate
                    next(l for l in lines if l[0] == code_b)[4] = "unbundling"
                else:
                    qty = _normal_qty(ref, code_b, rng)
                    lines.append([code_b, ref.loc[code_b, "description"], qty,
                                  _price(ref, code_b, qty, markup, rng), "unbundling"])
            else:  # unknown_code
                code, desc = rng.choice(FAKE_CODES)
                lines.append([code, desc, 1, round(rng.uniform(40, 400), 2), "unknown_code"])

        for line_no, (code, desc, qty, charge, planted) in enumerate(lines, start=1):
            rows.append(
                {
                    "bill_id": bill_id,
                    "provider": name,
                    "provider_type": ptype,
                    "service_date": service_date.isoformat(),
                    "line_no": line_no,
                    "code": code,
                    "description": desc,
                    "quantity": qty,
                    "charge": charge,
                    "planted_error": planted,
                }
            )

    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bills", type=int, default=300)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    df = generate(args.bills, args.seed)
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    df.to_csv(OUT_CSV, index=False)
    planted = df["planted_error"].value_counts()
    print(f"Wrote {df['bill_id'].nunique()} bills / {len(df)} line items to {os.path.normpath(OUT_CSV)}")
    print("Planted errors:", planted.to_dict())


if __name__ == "__main__":
    main()
