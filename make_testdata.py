#!/usr/bin/env python3
"""
BOAT RACE テストデータ生成スクリプト

公式フォーマット準拠の合成データを生成し parse.py / backtest.py を
ダウンロード不要で動作確認できます。

Usage:
    python make_testdata.py [--days N] [--seed S]

    --days N : 生成日数 (省略時: 14)
    --seed S : 乱数シード (省略時: 42)

生成物:
    data/lzh/k{YYMMDD}.txt  K ファイル相当 (競走成績)
    data/lzh/b{YYMMDD}.txt  B ファイル相当 (番組表)

parse.py は .lzh と .txt の両方に対応しています。
"""

import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

OUT_DIR = Path("data/lzh")
DEFAULT_DAYS = 14
START_DATE = datetime(2026, 5, 5)  # 直近に見せかけた基準日

VENUES = ["01", "02", "12"]        # 桐生, 戸田, 住之江
N_RACES = 12

# コース別 1着確率 (全国平均ベース)
COURSE_WIN_PROB = [0.43, 0.18, 0.13, 0.10, 0.09, 0.07]

# ---------------------------------------------------------------------------
# 選手プール生成
# ---------------------------------------------------------------------------

def make_racers(n: int = 100, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    racers = []
    for i in range(n):
        grade = rng.choices(["A1", "A2", "B1", "B2"], weights=[20, 30, 35, 15])[0]
        if grade == "A1":
            nwr = rng.uniform(6.5, 8.5);  n2r = rng.uniform(55, 75); st = rng.uniform(0.10, 0.17); fl = rng.randint(0, 2)
        elif grade == "A2":
            nwr = rng.uniform(5.0, 6.5);  n2r = rng.uniform(42, 58); st = rng.uniform(0.14, 0.20); fl = rng.randint(0, 1)
        elif grade == "B1":
            nwr = rng.uniform(3.5, 5.0);  n2r = rng.uniform(32, 46); st = rng.uniform(0.16, 0.22); fl = rng.randint(0, 1)
        else:
            nwr = rng.uniform(1.0, 3.5);  n2r = rng.uniform(20, 34); st = rng.uniform(0.18, 0.25); fl = 0

        racers.append({
            "racer_no":        4000 + i,
            "grade":           grade,
            "national_winrate": round(nwr, 2),
            "national_2rate":   round(n2r, 1),
            "venue_winrate":    round(nwr * rng.uniform(0.7, 1.3), 2),
            "venue_2rate":      round(n2r * rng.uniform(0.8, 1.2), 1),
            "avg_start":        round(st, 2),
            "fl_count":         fl,
            "late_count":       rng.randint(0, 2),
            "weight":           round(rng.uniform(48, 58), 1),
            "age":              rng.randint(22, 55),
            "branch":           str(rng.randint(1, 9)),
        })
    return racers


def make_motors(n: int = 120, seed: int = 42) -> list[dict]:
    rng = random.Random(seed + 1)
    return [
        {"motor_no": 100 + i, "motor_2rate": round(rng.uniform(25, 72), 1)}
        for i in range(n)
    ]


def make_hulls(n: int = 120, seed: int = 42) -> list[dict]:
    rng = random.Random(seed + 2)
    return [
        {"hull_no": 200 + i, "hull_2rate": round(rng.uniform(30, 68), 1)}
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# レースシミュレーション
# ---------------------------------------------------------------------------

def score_boat(racer: dict, course: int, motor_2r: float) -> float:
    """スコアリングモデルと同じ計算式で艇スコアを計算。"""
    cf = [1.00, 0.42, 0.30, 0.16, 0.12, 0.09]
    s = cf[course - 1] * 1.5
    s += racer["venue_winrate"] * 3.0
    s += racer["national_winrate"] * 2.0
    s += (motor_2r - 50) / 10 * 1.5
    s += racer["avg_start"] * -12.0
    s += racer["fl_count"] * -3.0
    return s


def simulate_finish(scores: list[float], rng: random.Random) -> list[int]:
    """スコアをソフトマックスに変換して着順をサンプリング。"""
    import math
    exp_s = [math.exp(min(s, 20)) for s in scores]
    remaining = list(range(6))
    rem_exp = list(exp_s)
    order = []
    while remaining:
        total = sum(rem_exp)
        r = rng.random() * total
        cum = 0
        for i, (idx, e) in enumerate(zip(remaining, rem_exp)):
            cum += e
            if r <= cum:
                order.append(idx)
                remaining.pop(i)
                rem_exp.pop(i)
                break
    return order  # order[0] = インデックス(0-5)の1着, order[1] = 2着, …


# ---------------------------------------------------------------------------
# K ファイル生成
# ---------------------------------------------------------------------------

def payout_stored(yen: int) -> str:
    """払戻金を T7 格納形式 (÷10) の5桁文字列に変換。"""
    return f"{max(11, yen // 10):05d}"


def estimate_payout(win_prob: float, scale: float = 1.0) -> int:
    """勝率から控除率75%前提の払戻金を推算し、10円単位に丸める。"""
    raw = int(100 / max(win_prob, 0.01) * 0.75 * scale)
    return max(110, (raw // 10) * 10)


def make_k_file(venue: str, date_str: str, races: list[dict]) -> str:
    lines = [f"T0{venue}{date_str}"]
    for race in races:
        r = race["race_no"]
        wx = race["weather"]
        wd = race["wind_dir"]
        ws = race["wind_speed"]
        wt = race["water_temp"]
        wh = race["wave"]
        lines.append(f"TH{venue}{r:02d}1{wx}{wd:02d}{ws:02d}{wt:04d}{wh:04d}")

        for bn, boat in enumerate(race["boats"], 1):
            course = boat["course"]
            rno    = boat["racer_no"]
            rank   = boat["rank"]
            time   = boat["race_time"]
            start  = boat["start_str"]
            lines.append(f"T{bn}{course}{rno:05d}{rank}{time}{start}")

        # T7 払戻レコード
        fin = race["finish_order"]   # [(course, win_prob), ...]
        c1, c2, c3 = fin[0][0], fin[1][0], fin[2][0]
        p1, p2, p3 = fin[0][1], fin[1][1], fin[2][1]

        tansho   = estimate_payout(p1)
        fuku1    = estimate_payout(p1 * 3)           # 複勝: 3着内確率≈3×勝率
        fuku2    = estimate_payout(p2 * 3)
        fuku3    = estimate_payout(p3 * 3)
        kaku12   = estimate_payout(p1 * p2 * 5)     # 拡連複
        kaku13   = estimate_payout(p1 * p3 * 6)
        kaku23   = estimate_payout(p2 * p3 * 7)
        renpu    = estimate_payout(p1 * p2 * 2)     # 2連複 (順不同)
        rentan   = estimate_payout(p1 * p2)          # 2連単
        sanpu    = estimate_payout(p1 * p2 * p3 * 6) # 3連複 (6通り)
        santan   = estimate_payout(p1 * p2 * p3)     # 3連単

        def pp(amt):  return payout_stored(amt)
        def pop(n):   return f"{n:02d}"

        t7 = (
            f"T7{c1}{c2}{c3}"
            f"{pp(tansho)}{pop(1)}"
            f"{pp(fuku1)}{pop(2)}{pp(fuku2)}{pop(3)}{pp(fuku3)}{pop(4)}"
            f"{pp(kaku12)}{pop(2)}{pp(kaku13)}{pop(3)}{pp(kaku23)}{pop(4)}"
            f"{pp(renpu)}{pop(3)}"
            f"{pp(rentan)}{pop(4)}"
            f"{pp(sanpu)}{pop(5)}"
            f"{pp(santan)}{pop(6)}"
        )
        lines.append(t7)

    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# B ファイル生成 (バイト列)
# ---------------------------------------------------------------------------

def make_b_file(venue: str, date_str: str, races: list[dict]) -> bytes:
    buf = bytearray()

    def line(s: str):
        buf.extend(s.encode("ascii") + b"\r\n")

    def bline(b: bytes):
        buf.extend(b + b"\r\n")

    line(f"BB{venue}{date_str}")

    for race in races:
        r = race["race_no"]
        line(f"BH{venue}{r:02d}")

        for bn, boat in enumerate(race["boats"], 1):
            ra = boat["racer"]
            mo = boat["motor"]
            hu = boat["hull"]

            # 名前フィールド 8バイト (全角4文字相当、ASCII空白で代替)
            name_b = b"        "

            record = (
                f"B{bn}".encode("ascii") +
                f"{ra['racer_no']:05d}".encode("ascii") +
                name_b +
                f"{ra['branch']:1}".encode("ascii") +
                f"{ra['age']:02d}".encode("ascii") +
                f"{int(ra['weight'] * 10):03d}".encode("ascii") +     # weight × 0.1
                f"{ra['fl_count']:02d}".encode("ascii") +
                f"{ra['late_count']:02d}".encode("ascii") +
                f"{int(ra['avg_start'] / 0.01):04d}".encode("ascii") +       # avg_start × 100
                f"{int(ra['national_winrate'] / 0.01):04d}".encode("ascii") +  # ×100
                f"{int(ra['national_2rate'] / 0.01):04d}".encode("ascii") +    # ×100
                f"{int(ra['venue_winrate'] / 0.01):04d}".encode("ascii") +
                f"{int(ra['venue_2rate'] / 0.01):04d}".encode("ascii") +
                f"{mo['motor_no']:03d}".encode("ascii") +
                f"{int(mo['motor_2rate'] / 0.01):04d}".encode("ascii") +
                f"{hu['hull_no']:03d}".encode("ascii") +
                f"{int(hu['hull_2rate'] / 0.01):04d}".encode("ascii")
            )
            bline(record)

    return bytes(buf)


# ---------------------------------------------------------------------------
# メインジェネレーター
# ---------------------------------------------------------------------------

def generate(n_days: int = DEFAULT_DAYS, seed: int = 42):
    rng = random.Random(seed)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    racers = make_racers(100, seed)
    motors = make_motors(60, seed)
    hulls  = make_hulls(60, seed)

    total_races = 0

    for day in range(n_days):
        dt = START_DATE + timedelta(days=day)
        yymmdd   = dt.strftime("%y%m%d")
        date_str = dt.strftime("%Y%m%d")

        k_lines_all: list[str] = []
        b_bytes_all  = bytearray()

        for venue in VENUES:
            k_lines_all.append(f"T0{venue}{date_str}")
            b_bytes_all += f"BB{venue}{date_str}\r\n".encode("ascii")

            # モーター/ボートは会期中固定 (6艇分を各レースで使い回す)
            venue_motors = rng.sample(motors, 6)
            venue_hulls  = rng.sample(hulls,  6)

            for race_idx in range(N_RACES):
                race_no = race_idx + 1
                boats_racers = rng.sample(racers, 6)
                race_motors  = venue_motors
                race_hulls   = venue_hulls

                # コース = 艇番 (テスト用は course change なし)
                courses = list(range(1, 7))

                # 各艇スコア
                scores = [
                    score_boat(boats_racers[i], courses[i], race_motors[i]["motor_2rate"])
                    for i in range(6)
                ]

                # 着順サンプリング
                finish_idx = simulate_finish(scores, rng)

                # 勝率推定 (softmax を win prob 代用)
                import math
                exp_s = [math.exp(min(s, 20)) for s in scores]
                total_exp = sum(exp_s)
                win_probs = [e / total_exp for e in exp_s]

                # 着順付与
                ranks = [0] * 6
                for rank, idx in enumerate(finish_idx, 1):
                    ranks[idx] = rank

                # ランダムな天候・水面
                wx    = rng.choice(["1", "1", "1", "2", "2", "3"])
                wd    = rng.randint(1, 16)
                ws    = rng.randint(0, 8)
                wt    = rng.randint(200, 300)    # ×0.1 ℃
                wave  = rng.randint(0, 20)       # cm

                boats_data = []
                for i in range(6):
                    st = boats_racers[i]["avg_start"] + rng.gauss(0, 0.02)
                    st = max(0.01, st)
                    start_str = f"{int(st * 100):03d}"
                    # F判定 (0.01秒以下はフライング扱い)
                    if st < 0.01:
                        rank_display = "F"
                    else:
                        rank_display = str(ranks[i])

                    # タイム (簡易: 1着 + オフセット)
                    base_time = 9600 + rng.randint(-200, 200)  # 96.xx秒
                    race_time = f"{base_time + ranks[i] * 50:05d}"

                    boats_data.append({
                        "course":   courses[i],
                        "racer_no": boats_racers[i]["racer_no"],
                        "rank":     rank_display,
                        "race_time": race_time,
                        "start_str": start_str,
                        "racer":    boats_racers[i],
                        "motor":    race_motors[i],
                        "hull":     race_hulls[i],
                    })

                # 着順コースと勝率 (払戻計算用)
                finish_order = [
                    (courses[finish_idx[r]], win_probs[finish_idx[r]])
                    for r in range(3)
                ]

                race_data = {
                    "race_no": race_no,
                    "weather": wx, "wind_dir": wd, "wind_speed": ws,
                    "water_temp": wt, "wave": wave,
                    "boats": boats_data,
                    "finish_order": finish_order,
                }

                # K ファイルレコード追加
                r = race_no
                k_lines_all.append(
                    f"TH{venue}{r:02d}1{wx}{wd:02d}{ws:02d}{wt:04d}{wave:04d}"
                )
                for bn, boat in enumerate(boats_data, 1):
                    k_lines_all.append(
                        f"T{bn}{boat['course']}{boat['racer_no']:05d}"
                        f"{boat['rank']}{boat['race_time']}{boat['start_str']}"
                    )

                # T7 払戻
                c1, c2, c3 = finish_order[0][0], finish_order[1][0], finish_order[2][0]
                p1, p2, p3 = finish_order[0][1], finish_order[1][1], finish_order[2][1]

                def pp(yen): return payout_stored(yen)
                def po(n):   return f"{n:02d}"

                # 単勝人気: 勝率の高い順に1位から付与 (win_probs は softmax 値)
                sorted_by_prob = sorted(range(6), key=lambda i: -win_probs[i])
                winner_idx = finish_idx[0]   # 1着 (0-indexed)
                pop_map = {idx: rank + 1 for rank, idx in enumerate(sorted_by_prob)}
                winner_pop = pop_map[winner_idx]

                # 2・3着も同様
                sec_pop = pop_map[finish_idx[1]]
                thi_pop = pop_map[finish_idx[2]]

                tansho  = estimate_payout(p1)
                k_lines_all.append(
                    f"T7{c1}{c2}{c3}"
                    f"{pp(tansho)}{po(winner_pop)}"
                    f"{pp(estimate_payout(p1 * 3))}{po(winner_pop)}"
                    f"{pp(estimate_payout(p2 * 3))}{po(sec_pop)}"
                    f"{pp(estimate_payout(p3 * 3))}{po(thi_pop)}"
                    f"{pp(estimate_payout(p1 * p2 * 5))}{po(max(1,winner_pop-1))}"
                    f"{pp(estimate_payout(p1 * p3 * 6))}{po(winner_pop)}"
                    f"{pp(estimate_payout(p2 * p3 * 7))}{po(sec_pop)}"
                    f"{pp(estimate_payout(p1 * p2 * 2))}{po(max(1,winner_pop-1))}"
                    f"{pp(estimate_payout(p1 * p2))}{po(winner_pop)}"
                    f"{pp(estimate_payout(p1 * p2 * p3 * 6))}{po(max(1,winner_pop-2))}"
                    f"{pp(estimate_payout(p1 * p2 * p3))}{po(winner_pop)}"
                )
                total_races += 1

                # B ファイルレコード追加
                b_bytes_all += f"BH{venue}{race_no:02d}\r\n".encode("ascii")
                for bn, boat in enumerate(boats_data, 1):
                    ra, mo, hu = boat["racer"], boat["motor"], boat["hull"]
                    b_bytes_all += (
                        f"B{bn}".encode("ascii") +
                        f"{ra['racer_no']:05d}".encode("ascii") +
                        b"        " +   # 名前 8バイト
                        f"{ra['branch']:1}".encode("ascii") +
                        f"{ra['age']:02d}".encode("ascii") +
                        f"{int(ra['weight'] * 10):03d}".encode("ascii") +
                        f"{ra['fl_count']:02d}".encode("ascii") +
                        f"{ra['late_count']:02d}".encode("ascii") +
                        f"{int(ra['avg_start'] / 0.01):04d}".encode("ascii") +
                        f"{int(ra['national_winrate'] / 0.01):04d}".encode("ascii") +
                        f"{int(ra['national_2rate'] / 0.01):04d}".encode("ascii") +
                        f"{int(ra['venue_winrate'] / 0.01):04d}".encode("ascii") +
                        f"{int(ra['venue_2rate'] / 0.01):04d}".encode("ascii") +
                        f"{mo['motor_no']:03d}".encode("ascii") +
                        f"{int(mo['motor_2rate'] / 0.01):04d}".encode("ascii") +
                        f"{hu['hull_no']:03d}".encode("ascii") +
                        f"{int(hu['hull_2rate'] / 0.01):04d}".encode("ascii") +
                        b"\r\n"
                    )

        # 書き出し
        k_text = "\r\n".join(k_lines_all) + "\r\n"
        k_path = OUT_DIR / f"k{yymmdd}.txt"
        b_path = OUT_DIR / f"b{yymmdd}.txt"
        k_path.write_text(k_text, encoding="ascii")
        b_path.write_bytes(bytes(b_bytes_all))
        print(f"  [{dt.strftime('%Y-%m-%d')}] k{yymmdd}.txt  b{yymmdd}.txt")

    print(f"\n生成完了: {n_days}日 × {len(VENUES)}場 × {N_RACES}R = {total_races}レース → {OUT_DIR}/")


if __name__ == "__main__":
    n_days = DEFAULT_DAYS
    seed   = 42
    i, args = 0, sys.argv[1:]
    while i < len(args):
        if args[i] == "--days" and i + 1 < len(args):
            n_days = int(args[i + 1]); i += 2
        elif args[i] == "--seed" and i + 1 < len(args):
            seed = int(args[i + 1]); i += 2
        else:
            i += 1

    print(f"テストデータ生成: {n_days}日分 seed={seed}")
    generate(n_days, seed)
