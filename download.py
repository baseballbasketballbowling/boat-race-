#!/usr/bin/env python3
"""
BOAT RACE 公式データ ダウンローダー

競走成績 (K ファイル) と 番組表 (B ファイル) の LZH を日付範囲指定で取得します。

Usage:
    python download.py [START_DATE] [END_DATE]
    日付は YYYYMMDD 形式。省略時は直近7日分。

Example:
    python download.py 20260101 20260107
"""

import requests
import time
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 公式データ配信URL
BASE_URL = "http://www1.mbrace.or.jp/od2"
# K: 競走成績, B: 番組表
FILE_TYPES = ("K", "B")

INTERVAL_SECS = 3   # サーバー負荷配慮のリクエスト間隔
TIMEOUT_SECS = 30
OUT_DIR = Path("data/lzh")


def build_url(file_type: str, date: datetime) -> str:
    yyyymm = date.strftime("%Y%m")
    yymmdd = date.strftime("%y%m%d")
    return f"{BASE_URL}/{file_type.upper()}/{yyyymm}/{file_type.lower()}{yymmdd}.lzh"


def download_file(url: str, dest: Path) -> bool:
    """1ファイルをダウンロードして保存。成功なら True を返す。"""
    if dest.exists():
        print(f"  [SKIP] {dest.name} (既存)")
        return True

    try:
        resp = requests.get(url, timeout=TIMEOUT_SECS)
        if resp.status_code == 200:
            dest.write_bytes(resp.content)
            print(f"  [OK]   {dest.name} ({len(resp.content):,} bytes)")
            return True
        elif resp.status_code == 404:
            print(f"  [404]  {dest.name} (開催なし)")
            return False
        else:
            print(f"  [ERR]  HTTP {resp.status_code}: {dest.name}")
            return False
    except requests.RequestException as e:
        print(f"  [ERR]  {dest.name}: {e}")
        return False


def download_range(start_date: str, end_date: str):
    """指定日付範囲の全K・Bファイルをダウンロードする。"""
    start = datetime.strptime(start_date, "%Y%m%d")
    end = datetime.strptime(end_date, "%Y%m%d")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    current = start
    total = 0
    success = 0

    while current <= end:
        print(f"\n[{current.strftime('%Y-%m-%d (%a)')}]")
        yymmdd = current.strftime("%y%m%d")

        for file_type in FILE_TYPES:
            url = build_url(file_type, current)
            dest = OUT_DIR / f"{file_type.lower()}{yymmdd}.lzh"
            total += 1
            if download_file(url, dest):
                success += 1
            time.sleep(INTERVAL_SECS)

        current += timedelta(days=1)

    print(f"\n完了: {success}/{total} ファイル → {OUT_DIR}/")


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        start_date = sys.argv[1]
        end_date = sys.argv[2]
    else:
        end = datetime.now()
        start = end - timedelta(days=7)
        start_date = start.strftime("%Y%m%d")
        end_date = end.strftime("%Y%m%d")

    print(f"BOAT RACE データ取得: {start_date} ～ {end_date}")
    download_range(start_date, end_date)
