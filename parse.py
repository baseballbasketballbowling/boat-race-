#!/usr/bin/env python3
"""
BOAT RACE 公式データ パーサー

K ファイル (競走成績) の LZH を展開し、固定長テキストをパースして SQLite に保存します。

Usage:
    python parse.py [--debug] [LZH_DIR]

    --debug  : 各レコードの生データを表示
    LZH_DIR  : LZH ファイルのディレクトリ (省略時: data/lzh)

K ファイルレコード形式 (Shift-JIS 固定長):
    T0  : ファイルヘッダ (場コード, 日付)
    TH  : レースヘッダ (場, R番, 天候, 風向, 風速, 水温, 波高)
    T1-T6 : 艇別成績 (艇番順, コース・選手番号・着順・タイム)
    T7  : 払戻金 (単勝・複勝・拡連複・2連複・2連単・3連複・3連単)
"""

import sys
import sqlite3
from pathlib import Path

try:
    import lhafile
except ImportError:
    sys.exit("ERROR: lhafile がインストールされていません。  pip install lhafile")

LZH_DIR = Path("data/lzh")
DB_PATH = Path("data/boatrace.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS races (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,        -- YYYYMMDD
    venue_code  TEXT NOT NULL,        -- 01-24
    race_no     INTEGER NOT NULL,     -- 1-12
    weather     TEXT,                 -- 天候
    wind_dir    INTEGER,              -- 風向 (1-16)
    wind_speed  INTEGER,              -- 風速 (m/s)
    water_temp  REAL,                 -- 水温 (℃)
    wave_height INTEGER,              -- 波高 (cm)
    UNIQUE(date, venue_code, race_no)
);

CREATE TABLE IF NOT EXISTS results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id      INTEGER NOT NULL REFERENCES races(id),
    boat_no      INTEGER NOT NULL,   -- 艇番 (1-6, 登録順)
    course       INTEGER,            -- 実コース (1-6)
    racer_no     INTEGER,            -- 選手登録番号
    rank         INTEGER,            -- 着順 (NULL=失格/欠場)
    race_time    TEXT,               -- タイム (生文字列)
    start_timing TEXT,               -- スタートタイミング (生文字列)
    UNIQUE(race_id, boat_no)
);

CREATE TABLE IF NOT EXISTS payouts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id     INTEGER NOT NULL REFERENCES races(id),
    bet_type    TEXT NOT NULL,       -- 単勝/複勝/拡連複/2連複/2連単/3連複/3連単
    combination TEXT NOT NULL,       -- 艇番組合 (例: "1", "12", "123")
    amount      INTEGER NOT NULL,    -- 払戻金額 (円)
    popularity  INTEGER,             -- 人気順
    UNIQUE(race_id, bet_type, combination)
);
"""

WEATHER_MAP = {"1": "晴", "2": "曇", "3": "雨", "4": "霧", "5": "雪"}

VENUE_NAMES = {
    "01": "桐生", "02": "戸田", "03": "江戸川", "04": "平和島",
    "05": "多摩川", "06": "浜名湖", "07": "蒲郡", "08": "常滑",
    "09": "津", "10": "三国", "11": "びわこ", "12": "住之江",
    "13": "尼崎", "14": "鳴門", "15": "丸亀", "16": "児島",
    "17": "宮島", "18": "徳山", "19": "下関", "20": "若松",
    "21": "芦屋", "22": "福岡", "23": "唐津", "24": "大村",
}


def safe_int(s: str, default=None):
    try:
        v = int(s.strip())
        return v
    except (ValueError, AttributeError):
        return default


class KParser:
    """K ファイル (競走成績) のパーサー。"""

    # フィールドオフセット定数 (0-indexed, 実データで要検証)
    # TH: レースヘッダ
    TH_VENUE     = slice(2, 4)
    TH_RACE_NO   = slice(4, 6)
    TH_STATUS    = slice(6, 7)
    TH_WEATHER   = slice(7, 8)
    TH_WIND_DIR  = slice(8, 10)
    TH_WIND_SPD  = slice(10, 12)
    TH_WATER_TMP = slice(12, 16)   # ×0.1℃
    TH_WAVE      = slice(16, 20)   # cm

    # T1-T6: 艇別成績 (艇番 = レコード番号の数字部)
    BOAT_COURSE  = slice(2, 3)
    BOAT_RACER   = slice(3, 8)
    BOAT_RANK    = slice(8, 9)
    BOAT_TIME    = slice(9, 14)
    BOAT_START   = slice(14, 17)

    def __init__(self, date: str, debug: bool = False):
        self.date = date
        self.debug = debug
        self.venue_code: str = ""
        self.race_id: int | None = None
        # T7 払戻解析用に着順保持
        self._t7_boats: list[str] = []

        self.races_in = 0
        self.results_in = 0
        self.payouts_in = 0

    def parse_file(self, text: str, conn: sqlite3.Connection):
        for raw_line in text.splitlines():
            line = raw_line.rstrip("\r\n")
            if len(line) < 2:
                continue
            rtype = line[:2]
            if self.debug:
                print(f"  [{rtype}] {repr(line[:50])}")
            match rtype:
                case "T0":
                    self._parse_t0(line)
                case "TH":
                    self._parse_th(line, conn)
                case _ if rtype[0] == "T" and rtype[1] in "123456":
                    self._parse_boat(line, conn)
                case "T7":
                    self._parse_payout(line, conn)

    # ------------------------------------------------------------------
    def _parse_t0(self, line: str):
        # T0: ファイルヘッダから場コードを取得
        if len(line) >= 4:
            self.venue_code = line[2:4].strip()

    def _parse_th(self, line: str, conn: sqlite3.Connection):
        if len(line) < 20:
            return

        venue = line[self.TH_VENUE].strip() or self.venue_code
        race_no = safe_int(line[self.TH_RACE_NO])
        weather_code = line[self.TH_WEATHER]
        wind_dir = safe_int(line[self.TH_WIND_DIR])
        wind_spd = safe_int(line[self.TH_WIND_SPD])
        water_raw = safe_int(line[self.TH_WATER_TMP])
        wave = safe_int(line[self.TH_WAVE])

        if not venue or race_no is None:
            return

        self.venue_code = venue
        weather = WEATHER_MAP.get(weather_code, weather_code)
        water_temp = water_raw / 10.0 if water_raw is not None else None

        cur = conn.execute(
            """INSERT OR IGNORE INTO races
               (date, venue_code, race_no, weather, wind_dir, wind_speed, water_temp, wave_height)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.date, venue, race_no, weather, wind_dir, wind_spd, water_temp, wave),
        )
        if cur.lastrowid:
            self.race_id = cur.lastrowid
            self.races_in += 1
        else:
            row = conn.execute(
                "SELECT id FROM races WHERE date=? AND venue_code=? AND race_no=?",
                (self.date, venue, race_no),
            ).fetchone()
            self.race_id = row[0] if row else None
        self._t7_boats = []

    def _parse_boat(self, line: str, conn: sqlite3.Connection):
        if self.race_id is None or len(line) < 14:
            return

        boat_no = safe_int(line[1:2])
        course = safe_int(line[self.BOAT_COURSE])
        racer_no = safe_int(line[self.BOAT_RACER])
        rank_str = line[self.BOAT_RANK]
        race_time = line[self.BOAT_TIME].strip()
        start_timing = line[self.BOAT_START].strip() if len(line) >= 17 else None

        # 着順: 数字のみ有効。F/L/K/0 等は None (失格扱い)
        rank = safe_int(rank_str) if rank_str.strip().isdigit() else None

        # 着順順に艇番を記録 (T7 払戻解析用)
        if rank == 1 and str(course):
            self._t7_boats.insert(0, str(course))
        elif rank and str(course):
            while len(self._t7_boats) < rank:
                self._t7_boats.append("")
            if len(self._t7_boats) > rank - 1:
                self._t7_boats[rank - 1] = str(course)

        conn.execute(
            """INSERT OR IGNORE INTO results
               (race_id, boat_no, course, racer_no, rank, race_time, start_timing)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (self.race_id, boat_no, course, racer_no, rank, race_time, start_timing),
        )
        self.results_in += 1

    def _parse_payout(self, line: str, conn: sqlite3.Connection):
        """T7 払戻レコードをパースする。

        フォーマット (公式仕様書に基づく推定・0-indexed):
          2:3  - 1着艇番
          3:4  - 2着艇番
          4:5  - 3着艇番
          単勝 (1件)  : payout[5] + pop[2]
          複勝 (3件)  : payout[5] + pop[2] × 3
          拡連複 (3件): payout[5] + pop[2] × 3
          2連複 (1件) : payout[5] + pop[2]
          2連単 (1件) : payout[5] + pop[2]
          3連複 (1件) : payout[5] + pop[2]
          3連単 (1件) : payout[5] + pop[2]
        """
        if self.race_id is None or len(line) < 12:
            return

        pos = 2
        first  = line[pos:pos+1].strip(); pos += 1
        second = line[pos:pos+1].strip(); pos += 1
        third  = line[pos:pos+1].strip(); pos += 1

        def insert(bet_type: str, combo: str, p: int) -> int:
            if p + 7 > len(line):
                return p
            amount = safe_int(line[p:p+5])
            pop    = safe_int(line[p+5:p+7])
            if amount and amount > 0 and combo:
                conn.execute(
                    """INSERT OR IGNORE INTO payouts
                       (race_id, bet_type, combination, amount, popularity)
                       VALUES (?, ?, ?, ?, ?)""",
                    (self.race_id, bet_type, combo.strip(), amount * 10, pop),
                    # 公式データは ¥10 単位で格納されている場合があるため×10
                    # 実データ確認後に要調整
                )
                self.payouts_in += 1
            return p + 7

        # 単勝
        pos = insert("単勝", first, pos)

        # 複勝 (1~3着各1件)
        for b in (first, second, third):
            pos = insert("複勝", b, pos)

        # 拡連複 (1-2, 1-3, 2-3)
        for combo in (
            "".join(sorted([first, second])),
            "".join(sorted([first, third])),
            "".join(sorted([second, third])),
        ):
            pos = insert("拡連複", combo, pos)

        # 2連複
        pos = insert("2連複", "".join(sorted([first, second])), pos)

        # 2連単
        pos = insert("2連単", f"{first}{second}", pos)

        # 3連複
        pos = insert("3連複", "".join(sorted([first, second, third])), pos)

        # 3連単
        pos = insert("3連単", f"{first}{second}{third}", pos)


# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)
    conn.commit()


def extract_k_text(lzh_path: Path) -> list[tuple[str, bytes]]:
    """LZH を展開して (ファイル名, バイト列) のリストを返す。"""
    try:
        lhf = lhafile.Lhafile(str(lzh_path))
        return [(name, lhf.read(name)) for name in lhf.namelist()]
    except Exception as e:
        print(f"  [ERR] 展開失敗 {lzh_path.name}: {e}")
        return []


def date_from_filename(filename: str) -> str:
    """k260101.lzh → "20260101" へ変換 (2000年代前提)。"""
    stem = Path(filename).stem  # "k260101"
    yymmdd = stem[1:]           # "260101"
    yy = int(yymmdd[:2])
    mm = yymmdd[2:4]
    dd = yymmdd[4:6]
    yyyy = 2000 + yy
    return f"{yyyy}{mm}{dd}"


def parse_all(lzh_dir: Path, db_path: Path, debug: bool = False):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    init_db(conn)

    k_files = sorted(lzh_dir.glob("k*.lzh"))
    if not k_files:
        print(f"K ファイルが見つかりません: {lzh_dir}/k*.lzh")
        conn.close()
        return

    total_races = total_results = total_payouts = 0

    for lzh_path in k_files:
        date = date_from_filename(lzh_path.name)
        print(f"\n[{date}] {lzh_path.name}")

        for inner_name, raw_bytes in extract_k_text(lzh_path):
            text = raw_bytes.decode("cp932", errors="replace")
            parser = KParser(date, debug=debug)
            parser.parse_file(text, conn)
            conn.commit()

            venue = VENUE_NAMES.get(parser.venue_code, parser.venue_code)
            print(
                f"  {inner_name}: {venue} | "
                f"races={parser.races_in} results={parser.results_in} payouts={parser.payouts_in}"
            )
            total_races   += parser.races_in
            total_results += parser.results_in
            total_payouts += parser.payouts_in

    conn.close()
    print(
        f"\n保存完了: races={total_races}, results={total_results}, payouts={total_payouts}"
        f"\nDB: {db_path}"
    )


if __name__ == "__main__":
    debug = "--debug" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    lzh_dir = Path(args[0]) if args else LZH_DIR

    parse_all(lzh_dir, DB_PATH, debug=debug)
