"""
Loads a query,count CSV into the SQLite query_counts table.
Run once before starting the server: `python -m app.core.load_dataset <csv_path>`

Swap data/sample_queries.csv for a real open-source dataset before submission
(e.g. AOL search query logs, Amazon product titles + review counts, or
Wikipedia page-view counts) -- aggregate to query,count if it isn't already.
"""
import csv
import sys

from app.core.db import engine, query_counts, init_db


def load_csv(path: str):
    init_db()
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            q = row["query"].strip().lower()
            c = int(row["count"])
            if q:
                rows.append({"query": q, "count": c})

    with engine.begin() as conn:
        conn.execute(query_counts.insert().prefix_with("OR REPLACE"), rows)
    print(f"Loaded {len(rows)} queries into {engine.url}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "../data/sample_queries.csv"
    load_csv(path)
