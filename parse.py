#!/usr/bin/env python3
"""
BOAT RACE 公式データ パーサー

K ファイル (競走成績) と B ファイル (番組表) の LZH を展開し、
固定長テキストをパースして SQLite に保存します。

Usage:
    python parse.py [--debug] [LZH_DIR]

K ファイルレコード形式 (Shift-JIS 固定長):
    T0  : ファイルヘッダ (場コード, 日付)
    TH  : レースヘッダ (場, R番, 天候, 風向, 風速, 水温, 波高)
    T1-T6 : 艇別成績 (艇番順, コース・選手番号・着順・タイム)
    T7  : 払戻金 (単勝・複勝・拡連複・2連複・2連単・3連複・3連単)

B ファイルレコード形式 (Shift-JIS 固定長バイト):
    BB  : ファイルヘッダ
    BH  : レースヘッダ (場, R番)
    B1-B6 : 選手・モーター情報 (全国/当地勝率, 平均ST, モーター2連率 など)
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
    date        TEXT NOT NULL,
    venue_code  TEXT NOT NULL,
    race_no     INTEGER NOT NULL,
    weather     TEXT,
    wind_dir    INTEGER,
    wind_speed  INTEGER,
    water_temp  REAL,
    wave_height INTEGER,
    UNIQUE(date, venue_code, race_no)
);

CREATE TABLE IF NOT EXISTS results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id      INTEGER NOT NULL REFERENCES races(id),
    boat_no      INTEGER NOT NULL,
    course       INTEGER,
    racer_no     INTEGER,
    rank         INTEGER,
    race_time    TEXT,
    start_timing TEXT,
    UNIQUE(race_id, boat_no)
);

CREATE TABLE IF NOT EXISTS payouts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id     INTEGER NOT NULL REFERENCES races(id),
    bet_type    TEXT NOT NULL,
    combination TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    popularity  INTEGER,
    UNIQUE(race_id, bet_type, combination)
);

CREATE TABLE IF NOT EXISTS entries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id          INTEGER NOT NULL REFERENCES races(id),
    boat_no          INTEGER NOT NULL,   -- 艇番 (1-6)
    racer_no         INTEGER,            -- 選手登録番号
    racer_name       TEXT,               -- 氏名
    branch_code      TEXT,               -- 支部コード
    age              INTEGER,
    weight           REAL,               -- 体重 (kg)
    fl_count         INTEGER,            -- F回数
    late_count       INTEGER,            -- L回数
    avg_start        REAL,               -- 平均ST (秒)
    national_winrate REAL,               -- 全国勝率
    national_2rate   REAL,               -- 全国2連率 (%)
    venue_winrate    REAL,               -- 当地勝率
    venue_2rate      REAL,               -- 当地2連率 (%)
    motor_no         INTEGER,            -- モーター番号
    motor_2rate      REAL,               -- モーター2連率 (%)
    hull_no          INTEGER,            -- ボート番号
    hull_2rate       REAL,               -- ボート2連率 (%)
    UNIQUE(race_id, boat_no)
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
        return int(s.strip())
    except (ValueError, AttributeError):
        return default


# ===========================================================================
# K ファイルパーサー (競走成績)
# ===========================================================================

class KParser:
    """K ファイル (競走成績) のパーサー。"""

    TH_VENUE     = slice(2, 4)
    TH_RACE_NO   = slice(4, 6)
    TH_WEATHER   = slice(7, 8)
    TH_WIND_DIR  = slice(8, 10)
    TH_WIND_SPD  = slice(10, 12)
    TH_WATER_TMP = slice(12, 16)   # ×0.1℃
    TH_WAVE      = slice(16, 20)   # cm

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
        self.races_in = self.results_in = self.payouts_in = 0

    def parse_file(self, text: str, conn: sqlite3.Connection):
        for raw_line in text.splitlines():
            line = raw_line.rstrip("\r\n")
            if len(line) < 2:
                continue
            rtype = line[:2]
            if self.debug:
                print(f"  K[{rtype}] {repr(line[:60])}")
            match rtype:
                case "T0":
                    if len(line) >= 4:
                        self.venue_code = line[2:4].strip()
                case "TH":
                    self._parse_th(line, conn)
                case _ if rtype[0] == "T" and rtype[1] in "123456":
                    self._parse_boat(line, conn)
                case "T7":
                    self._parse_payout(line, conn)

    def _parse_th(self, line: str, conn: sqlite3.Connection):
        if len(line) < 20:
            return
        venue = line[self.TH_VENUE].strip() or self.venue_code
        race_no = safe_int(line[self.TH_RACE_NO])
        if not venue or race_no is None:
            return
        self.venue_code = venue
        weather = WEATHER_MAP.get(line[self.TH_WEATHER], line[self.TH_WEATHER])
        wind_dir = safe_int(line[self.TH_WIND_DIR])
        wind_spd = safe_int(line[self.TH_WIND_SPD])
        water_raw = safe_int(line[self.TH_WATER_TMP])
        wave = safe_int(line[self.TH_WAVE])
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

    def _parse_boat(self, line: str, conn: sqlite3.Connection):
        if self.race_id is None or len(line) < 14:
            return
        boat_no = safe_int(line[1:2])
        course  = safe_int(line[self.BOAT_COURSE])
        racer_no = safe_int(line[self.BOAT_RACER])
        rank_str = line[self.BOAT_RANK]
        race_time = line[self.BOAT_TIME].strip()
        start_timing = line[self.BOAT_START].strip() if len(line) >= 17 else None
        rank = safe_int(rank_str) if rank_str.strip().isdigit() else None
        conn.execute(
            """INSERT OR IGNORE INTO results
               (race_id, boat_no, course, racer_no, rank, race_time, start_timing)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (self.race_id, boat_no, course, racer_no, rank, race_time, start_timing),
        )
        self.results_in += 1

    def _parse_payout(self, line: str, conn: sqlite3.Connection):
        """T7 払戻レコード。フォーマット (0-indexed, 公式仕様推定):
          2:5 - 1着/2着/3着 艇番
          その後 [payout(5桁) + pop(2桁)] × 7種
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
            if amount and amount > 0 and combo.strip():
                conn.execute(
                    """INSERT OR IGNORE INTO payouts
                       (race_id, bet_type, combination, amount, popularity)
                       VALUES (?, ?, ?, ?, ?)""",
                    (self.race_id, bet_type, combo.strip(), amount * 10, pop),
                )
                self.payouts_in += 1
            return p + 7

        pos = insert("単勝", first, pos)
        for b in (first, second, third):
            pos = insert("複勝", b, pos)
        for combo in (
            "".join(sorted([first, second])),
            "".join(sorted([first, third])),
            "".join(sorted([second, third])),
        ):
            pos = insert("拡連複", combo, pos)
        pos = insert("2連複", "".join(sorted([first, second])), pos)
        pos = insert("2連単", f"{first}{second}", pos)
        pos = insert("3連複", "".join(sorted([first, second, third])), pos)
        pos = insert("3連単", f"{first}{second}{third}", pos)


# ===========================================================================
# B ファイルパーサー (番組表) — 選手・モーターデータ
# ===========================================================================

class BParser:
    """B ファイル (番組表) パーサー。バイト列として処理する (Shift-JIS 名前対応)。

    B1-B6 バイトオフセット定数 (公式仕様書に基づく推定値):
        2- 6: 選手番号 (5)
        7-14: 氏名 (8 bytes = 全角4文字)
       15   : 支部コード (1)
       16-17: 年齢 (2)
       18-20: 体重×0.1 kg (3)
       21-22: F回数 (2)
       23-24: L回数 (2)
       25-28: 平均ST×0.01秒 (4)
       29-32: 全国勝率×0.01 (4)
       33-36: 全国2連率×0.01 (4)
       37-40: 当地勝率×0.01 (4)
       41-44: 当地2連率×0.01 (4)
       45-47: モーター番号 (3)
       48-51: モーター2連率×0.01 (4)
       52-54: ボート番号 (3)
       55-58: ボート2連率×0.01 (4)
    """

    def __init__(self, date: str, debug: bool = False):
        self.date = date
        self.debug = debug
        self.venue_code: str = ""
        self.race_id: int | None = None
        self.entries_in = 0

    def parse_file(self, raw_bytes: bytes, conn: sqlite3.Connection):
        for raw_line in raw_bytes.split(b"\r\n") or raw_bytes.split(b"\n"):
            if len(raw_line) < 2:
                continue
            try:
                rtype = raw_line[:2].decode("ascii")
            except Exception:
                continue
            if self.debug:
                print(f"  B[{rtype}] {raw_line[:60]}")
            match rtype:
                case "BB":
                    if len(raw_line) >= 4:
                        try:
                            self.venue_code = raw_line[2:4].decode("ascii").strip()
                        except Exception:
                            pass
                case "BH":
                    self._parse_bh(raw_line, conn)
                case _ if rtype[0] == "B" and rtype[1] in "123456":
                    self._parse_entry(raw_line, conn)

    def _parse_bh(self, raw: bytes, conn: sqlite3.Connection):
        if len(raw) < 6:
            return
        try:
            venue   = raw[2:4].decode("ascii").strip() or self.venue_code
            race_no = int(raw[4:6])
        except Exception:
            return
        self.venue_code = venue
        row = conn.execute(
            "SELECT id FROM races WHERE date=? AND venue_code=? AND race_no=?",
            (self.date, venue, race_no),
        ).fetchone()
        self.race_id = row[0] if row else None

    def _parse_entry(self, raw: bytes, conn: sqlite3.Connection):
        if self.race_id is None or len(raw) < 45:
            return

        def bi(s, e, default=None):
            try:
                return int(raw[s:e].decode("ascii").strip())
            except Exception:
                return default

        def bf(s, e, scale=0.01, default=None):
            v = bi(s, e)
            return round(v * scale, 4) if v is not None else default

        try:
            boat_no = int(chr(raw[1]))
        except Exception:
            return

        racer_no = bi(2, 7)
        try:
            racer_name = raw[7:15].decode("cp932", errors="replace").strip()
        except Exception:
            racer_name = ""
        try:
            branch = raw[15:16].decode("ascii").strip()
        except Exception:
            branch = ""

        try:
            conn.execute(
                """INSERT OR IGNORE INTO entries
                   (race_id, boat_no, racer_no, racer_name, branch_code,
                    age, weight, fl_count, late_count, avg_start,
                    national_winrate, national_2rate, venue_winrate, venue_2rate,
                    motor_no, motor_2rate, hull_no, hull_2rate)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.race_id, boat_no, racer_no, racer_name, branch,
                    bi(16, 18),           # age
                    bf(18, 21, 0.1),      # weight
                    bi(21, 23, default=0),
                    bi(23, 25, default=0),
                    bf(25, 29),           # avg_start (秒)
                    bf(29, 33),           # national_winrate
                    bf(33, 37),           # national_2rate
                    bf(37, 41),           # venue_winrate
                    bf(41, 45),           # venue_2rate
                    bi(45, 48),           # motor_no
                    bf(48, 52),           # motor_2rate
                    bi(52, 55),           # hull_no
                    bf(55, 59),           # hull_2rate
                ),
            )
            self.entries_in += 1
        except sqlite3.Error as e:
            if self.debug:
                print(f"  [DB ERR] B{boat_no}: {e}")


# ===========================================================================
# ユーティリティ
# ===========================================================================

def init_db(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)
    conn.commit()


def extract_lzh(lzh_path: Path) -> list[tuple[str, bytes]]:
    try:
        lhf = lhafile.Lhafile(str(lzh_path))
        return [(name, lhf.read(name)) for name in lhf.namelist()]
    except Exception as e:
        print(f"  [ERR] 展開失敗 {lzh_path.name}: {e}")
        return []


def date_from_filename(filename: str) -> str:
    """k260101.lzh → "20260101"。"""
    stem = Path(filename).stem
    yymmdd = stem[1:]
    yy, mm, dd = int(yymmdd[:2]), yymmdd[2:4], yymmdd[4:6]
    return f"{2000 + yy}{mm}{dd}"


def parse_all(lzh_dir: Path, db_path: Path, debug: bool = False):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    init_db(conn)

    k_files = sorted(lzh_dir.glob("k*.lzh"))
    b_files = {p.stem[1:]: p for p in lzh_dir.glob("b*.lzh")}

    if not k_files:
        print(f"K ファイルが見つかりません: {lzh_dir}/k*.lzh")
        conn.close()
        return

    tr = te = tp = tb = 0

    for lzh_path in k_files:
        date = date_from_filename(lzh_path.name)
        yymmdd = lzh_path.stem[1:]
        print(f"\n[{date}] {lzh_path.name}")

        # K ファイル (競走成績)
        for inner_name, raw_bytes in extract_lzh(lzh_path):
            text = raw_bytes.decode("cp932", errors="replace")
            kp = KParser(date, debug=debug)
            kp.parse_file(text, conn)
            conn.commit()
            venue = VENUE_NAMES.get(kp.venue_code, kp.venue_code)
            print(
                f"  K {inner_name}: {venue} | "
                f"races={kp.races_in} results={kp.results_in} payouts={kp.payouts_in}"
            )
            tr += kp.races_in; te += kp.results_in; tp += kp.payouts_in

        # B ファイル (番組表) — 同日付があれば処理
        b_path = b_files.get(yymmdd)
        if b_path:
            for inner_name, raw_bytes in extract_lzh(b_path):
                bp = BParser(date, debug=debug)
                bp.parse_file(raw_bytes, conn)
                conn.commit()
                print(f"  B {inner_name}: entries={bp.entries_in}")
                tb += bp.entries_in
        else:
            print(f"  B ファイルなし → 選手データはスキップ")

    conn.close()
    print(
        f"\n保存完了: races={tr}, results={te}, payouts={tp}, entries={tb}"
        f"\nDB: {db_path}"
    )


if __name__ == "__main__":
    debug = "--debug" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    lzh_dir = Path(args[0]) if args else LZH_DIR
    parse_all(lzh_dir, DB_PATH, debug=debug)
