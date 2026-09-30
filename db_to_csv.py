"""
db_to_csv.py
============
Exports every table in a PetMaxi batch SQLite db to individual CSVs.
Needed because Claude can't read .db files directly, only text/CSV.

Usage:
    python db_to_csv.py --db petmaxi_cache_new.db --out db_export
"""

import argparse
import os
import sqlite3

import pandas as pd


def export_db_to_csv(db_path: str, out_dir: str) -> None:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")

    os.makedirs(out_dir, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        tables = pd.read_sql(
            "SELECT name FROM sqlite_master WHERE type='table'", conn
        )["name"].tolist()

        if not tables:
            print("  No tables found in DB.")
            return

        print(f"  Found {len(tables)} tables: {tables}")

        for table in tables:
            df = pd.read_sql(f"SELECT * FROM {table}", conn)
            out_path = os.path.join(out_dir, f"{table}.csv")
            df.to_csv(out_path, index=False)
            print(f"  {table:20s} -> {out_path}  ({len(df):,} rows, {len(df.columns)} cols)")

    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export SQLite db tables to CSV")
    parser.add_argument("--db",  required=True, help="path to .db file")
    parser.add_argument("--out", default="db_export", help="output folder for CSVs")
    args = parser.parse_args()

    print(f"Exporting {args.db} -> {args.out}/")
    export_db_to_csv(args.db, args.out)
    print("Done.")
