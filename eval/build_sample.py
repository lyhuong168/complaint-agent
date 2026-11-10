"""Build a balanced evaluation sample of real CFPB consumer complaints.

Source: the CFPB Consumer Complaint Database. The CFPB no longer publishes the
complaint narratives through its API or bulk download (checked 2026-11-10), so
we use a public-domain (CC0) archived snapshot that still contains them:
    https://huggingface.co/datasets/BEE-spoke-data/consumer-finance-complaints
    (config "has-text": only complaints with a narrative)

Usage:
    python eval/build_sample.py [--per-product 50] [--seed 42]

Output:
    data/complaints_sample.csv   balanced sample used for all evaluation runs
"""
import argparse
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "bee_spoke_has_text"
OUT = ROOT / "data" / "complaints_sample.csv"
SHARD = "has-text/train-00002-of-00003.parquet"
URL = (
    "https://huggingface.co/datasets/BEE-spoke-data/consumer-finance-complaints"
    f"/resolve/main/{SHARD}"
)

COLUMNS = {
    "Complaint ID": "complaint_id",
    "Date received": "date_received",
    "Product": "product",
    "Sub-product": "sub_product",
    "Issue": "issue",
    "Sub-issue": "sub_issue",
    "Consumer complaint narrative": "narrative",
    "Company": "company",
    "State": "state",
    "Tags": "tags",
    "Company response to consumer": "company_response",
    "Timely response?": "timely",
}
# Only complaints the company has finished handling have a real outcome label.
CLOSED = {
    "Closed with explanation",
    "Closed with monetary relief",
    "Closed with non-monetary relief",
}
# The CFPB changed its product list in August 2023; keep one consistent taxonomy.
MIN_DATE = "2023-09-01"
MIN_CHARS, MAX_CHARS = 300, 3000


def download(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    with requests.get(URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total and done % (50 << 20) < (1 << 20):
                    print(f"  downloaded {done >> 20} / {total >> 20} MB")
    tmp.rename(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-product", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    raw = RAW_DIR / Path(SHARD).name
    print("Downloading archive shard (skipped if already present)...")
    download(raw)

    df = pd.read_parquet(raw, columns=list(COLUMNS)).rename(columns=COLUMNS)
    print(f"Rows in shard: {len(df):,}")
    print(f"Date range:    {df.date_received.min()} .. {df.date_received.max()}")

    df["narrative"] = df["narrative"].fillna("").str.strip()
    n_chars = df["narrative"].str.len()
    df = df[
        (df["date_received"].astype(str) >= MIN_DATE)
        & df["company_response"].isin(CLOSED)
        & n_chars.between(MIN_CHARS, MAX_CHARS)
    ].drop_duplicates("narrative")
    print(f"After filters: {len(df):,}")

    sample = (
        df.sort_values("complaint_id")
        .sample(frac=1, random_state=args.seed)  # shuffle, then take N per product
        .groupby("product")
        .head(args.per_product)
        .reset_index(drop=True)
    )
    sample.to_csv(OUT, index=False, encoding="utf-8")

    print(f"\nWrote {len(sample)} complaints to {OUT}")
    print("\nPer product:")
    print(sample["product"].value_counts().to_string())
    print("\nCompany response:")
    print(sample["company_response"].value_counts().to_string())
    print(f"\nMedian narrative length: {int(sample.narrative.str.len().median())} chars")


if __name__ == "__main__":
    main()
