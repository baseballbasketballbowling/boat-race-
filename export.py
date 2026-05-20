#!/usr/bin/env python3
"""
SQLite → Parquet エクスポーター

backtest.py / ML モデルがこのセッション (クラウド) から読めるよう
races / results / payouts / entries を data/exports/ に保存して git push する。

Usage:
    python export.py
"""

import sqlite3
import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    sys.exit("pandas が必要です: pip install pandas")

DB_PATH      = Path("data/boatrace.db")
EXPORTS_DIR  = Path("data/exports")
TABLES       = ["races", "results", "payouts", "entries"]


def export_all():
    if not DB_PATH.exists():
        sys.exit(f"DB が見つかりません: {DB_PATH}  (先に parse.py を実行してください)")

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    try:
        for table in TABLES:
            df = pd.read_sql(f"SELECT * FROM {table}", conn)
            out = EXPORTS_DIR / f"{table}.parquet"
            df.to_parquet(out, index=False)
            print(f"  {table:10s}: {len(df):,} 行 → {out}")
    finally:
        conn.close()

    print(f"\nエクスポート完了 → {EXPORTS_DIR}/")


if __name__ == "__main__":
    export_all()
