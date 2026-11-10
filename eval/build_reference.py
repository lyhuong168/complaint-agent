"""Build the reference data the agent's tools look things up in.

Reads the same archive shard as build_sample.py and writes two files:

    n8n/knowledge/taxonomy.json
        The CFPB product list and, for each product, its list of issues.
        The agent must pick its labels from these lists.

    n8n/knowledge/company_history.json
        For each company: how its complaints were closed BEFORE 2023-09-01.
        The test sample only contains complaints from 2023-09-01 onward, so
        this history never includes a complaint the agent is being tested on.

Usage:
    python eval/build_reference.py
"""
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "bee_spoke_has_text" / "train-00002-of-00003.parquet"
OUT_DIR = ROOT / "n8n" / "knowledge"
CUTOFF = "2023-09-01"       # must match MIN_DATE in build_sample.py
MIN_ISSUE_COUNT = 5         # drop issues that are mislabeled strays under a product
MIN_HISTORY = 20            # companies with fewer past complaints get no history


def main() -> None:
    df = pd.read_parquet(
        RAW,
        columns=["Date received", "Product", "Issue", "Company",
                 "Company response to consumer", "Timely response?"],
    )
    df.columns = ["date", "product", "issue", "company", "response", "timely"]
    df["date"] = df["date"].astype(str)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # --- taxonomy: products and issues as used from the cutoff onward ---------
    new = df[df["date"] >= CUTOFF]
    counts = new.groupby(["product", "issue"]).size()
    taxonomy = {}
    for product in sorted(new["product"].unique()):
        issues = counts[product]
        taxonomy[product] = sorted(issues[issues >= MIN_ISSUE_COUNT].index)
    (OUT_DIR / "taxonomy.json").write_text(json.dumps(taxonomy, indent=2), encoding="utf-8")
    print(f"taxonomy: {len(taxonomy)} products, "
          f"{sum(len(v) for v in taxonomy.values())} product/issue pairs")

    # --- company history: outcomes before the cutoff --------------------------
    old = df[df["date"] < CUTOFF]
    history = {}
    for company, g in old.groupby("company"):
        n = len(g)
        if n < MIN_HISTORY:
            continue
        resp = g["response"].value_counts()
        history[company] = {
            "past_complaints": n,
            "pct_monetary_relief": round(100 * resp.get("Closed with monetary relief", 0) / n, 1),
            "pct_non_monetary_relief": round(100 * resp.get("Closed with non-monetary relief", 0) / n, 1),
            "pct_explanation_only": round(100 * resp.get("Closed with explanation", 0) / n, 1),
            "pct_late_response": round(100 * (g["timely"] == "No").mean(), 1),
        }
    (OUT_DIR / "company_history.json").write_text(json.dumps(history, indent=1), encoding="utf-8")
    print(f"company history: {len(history):,} companies "
          f"(from {len(old):,} complaints before {CUTOFF})")

    sample = pd.read_csv(ROOT / "data" / "complaints_sample.csv")
    covered = sample["company"].isin(history).mean()
    ok = (sample.apply(lambda r: r["issue"] in taxonomy.get(r["product"], []), axis=1)).mean()
    print(f"sample complaints whose company has history: {covered:.0%}")
    print(f"sample complaints whose true issue is in the taxonomy: {ok:.0%}")


if __name__ == "__main__":
    main()
