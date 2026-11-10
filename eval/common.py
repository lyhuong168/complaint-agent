"""Shared settings and helpers for the evaluation scripts."""
import json
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

SAMPLE = ROOT / "data" / "complaints_sample.csv"
RESULTS = ROOT / "results"
TAXONOMY = json.loads((ROOT / "n8n" / "knowledge" / "taxonomy.json").read_text(encoding="utf-8"))
PRODUCTS = list(TAXONOMY)

# The baseline and the n8n agent must use the same model, or the comparison is
# unfair. Change it in .env (CLAUDE_MODEL=...) and in the n8n workflow together.
MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")

# The three outcomes the agent predicts, and the CFPB label each one maps to.
OUTCOMES = {
    "explanation_only": "Closed with explanation",
    "non_monetary_relief": "Closed with non-monetary relief",
    "monetary_relief": "Closed with monetary relief",
}


def load_sample(limit: int | None = None) -> pd.DataFrame:
    df = pd.read_csv(SAMPLE, dtype={"complaint_id": str}).fillna("")
    if limit:
        # Take the same number from every product so a small run stays balanced.
        per = max(1, limit // df["product"].nunique())
        df = df.groupby("product").head(per).head(limit)
    return df.reset_index(drop=True)


def taxonomy_text() -> str:
    """The product and issue lists, formatted for a prompt."""
    lines = []
    for product, issues in TAXONOMY.items():
        lines.append(f"PRODUCT: {product}")
        lines.extend(f"  - {issue}" for issue in issues)
    return "\n".join(lines)


def done_ids(path: Path) -> set[str]:
    """Complaint ids already answered in a results file, so a rerun can resume.

    Rows that recorded an error are not counted, so a rerun tries them again.
    """
    return set(load_results(path))


def load_results(path: Path) -> dict[str, dict]:
    """Results keyed by complaint id, successful rows only (latest wins)."""
    rows = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    if "error" not in row:
                        rows[str(row["complaint_id"])] = row
    return rows


def append_result(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
