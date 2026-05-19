#!/usr/bin/env python3
"""
BOAT RACE バックテスト — 複合スコアリングモデル

選手能力・モーター成績・スタートタイミング・直近フォーム・コース適性を
加重合算したスコアで出走艇をランク付けし、複数の賭け戦略を比較します。

Usage:
    python backtest.py [--bet AMOUNT] [--history N] [DB_PATH]

    --bet AMOUNT  : 1点あたり賭け金 (円, 省略時: 100)
    --history N   : 直近フォーム参照レース数 (省略時: 30)

スコア構成要素 (B ファイルデータがある場合フル活用):
    当地勝率      × 3.0   -- 最重要: その場での実績
    全国勝率      × 2.0   -- 選手の総合力
    モーター偏差  × 1.5   -- 2連率を基準50%からの偏差で評価
    コース係数    × 1.5   -- 内コース有利の補正
    平均ST        × -12   -- 低いほど踏み込める (フライングリスク考慮)
    F回数ペナルティ× -3   -- フライング歴
    直近1着率     × 2.5   -- 直近N走の調子 (ルックアヘッドなし)
    直近3着内率   × 0.8   -- 安定性

賭け戦略:
    S1  単勝         : スコア1位に単勝
    S2  3連複        : 上位3艇で3連複 (1点)
    S3  3連単BOX     : 上位3艇で3連単BOX (6点)
    S4  2連単流し    : 1位を1着固定, 2-3位を2着流し (2点)
    S5  単勝バリュー : スコア1位 かつ 人気3位以下のみ賭け
"""

import sys
import sqlite3
from collections import defaultdict
from pathlib import Path
from itertools import permutations

try:
    import pandas as pd
except ImportError:
    sys.exit("ERROR: pandas がインストールされていません。  pip install pandas")

DB_PATH = Path("data/boatrace.db")
DEFAULT_BET = 100

# ---------------------------------------------------------------------------
# スコアリング定数
# ---------------------------------------------------------------------------

# コース係数 (全国平均1着率を正規化。コース1=1.00基準)
COURSE_FACTOR = {1: 1.00, 2: 0.42, 3: 0.30, 4: 0.16, 5: 0.12, 6: 0.09}

# 特徴量の重み
W_VENUE_WR    = 3.0    # 当地勝率
W_NAT_WR      = 2.0    # 全国勝率
W_MOTOR       = 1.5    # モーター2連率偏差 (50%基準)
W_COURSE      = 1.5    # コース係数
W_AVG_ST      = -12.0  # 平均ST (秒)
W_FL          = -3.0   # F回数
W_RECENT_WIN  = 2.5    # 直近1着率
W_RECENT_3RD  = 0.8    # 直近3着内率

FALLBACK_AVG_START  = 0.18  # STデータなし時のデフォルト
FALLBACK_MOTOR_2R   = 50.0  # モーターデータなし時


# ---------------------------------------------------------------------------
# メインエンジン
# ---------------------------------------------------------------------------

class BacktestEngine:
    def __init__(self, bet_amount: int = DEFAULT_BET, history_n: int = 30):
        self.bet = bet_amount
        self.history_n = history_n
        # racer_no → deque of (rank_or_None)  (直近結果, 時系列順)
        self._racer_hist: dict[int, list] = defaultdict(list)

    # ------------------------------------------------------------------
    # データロード
    # ------------------------------------------------------------------

    def load(self, conn: sqlite3.Connection):
        """全データを日付順に読み込む。"""
        self.df_races = pd.read_sql(
            "SELECT id, date, venue_code, race_no FROM races ORDER BY date, venue_code, race_no",
            conn,
        )
        self.df_results = pd.read_sql(
            "SELECT race_id, boat_no, course, racer_no, rank FROM results",
            conn,
        )
        self.df_entries = pd.read_sql(
            """SELECT race_id, boat_no, racer_no,
                      avg_start, fl_count, late_count,
                      national_winrate, national_2rate,
                      venue_winrate,    venue_2rate,
                      motor_2rate,      hull_2rate
               FROM entries""",
            conn,
        )
        self.df_payouts = pd.read_sql(
            "SELECT race_id, bet_type, combination, amount, popularity FROM payouts",
            conn,
        )
        self.has_entries = len(self.df_entries) > 0
        self.has_payouts = len(self.df_payouts) > 0

    # ------------------------------------------------------------------
    # スコアリング
    # ------------------------------------------------------------------

    def _recent_stats(self, racer_no: int) -> tuple[float, float]:
        """直近 history_n 走の (1着率, 3着内率)。データなし → (0, 0)。"""
        hist = self._racer_hist.get(racer_no, [])
        if not hist:
            return 0.0, 0.0
        recent = hist[-self.history_n:]
        valid = [r for r in recent if r is not None]
        if not valid:
            return 0.0, 0.0
        n = len(valid)
        win_r  = sum(1 for r in valid if r == 1) / n
        top3_r = sum(1 for r in valid if r <= 3) / n
        return win_r, top3_r

    def _score_one(self, boat_no: int, course: int | None,
                   entry_row, racer_no: int | None) -> float:
        """1艇のスコアを計算する。"""
        score = 0.0
        c = course or boat_no  # コース不明なら艇番をコース代用

        # コース係数
        score += COURSE_FACTOR.get(c, 0.08) * W_COURSE

        if entry_row is not None:
            e = entry_row

            # 当地勝率
            vwr = e.get("venue_winrate") or 0
            score += vwr * W_VENUE_WR

            # 全国勝率
            nwr = e.get("national_winrate") or 0
            score += nwr * W_NAT_WR

            # モーター偏差
            m2r = e.get("motor_2rate") or FALLBACK_MOTOR_2R
            score += (m2r - FALLBACK_MOTOR_2R) / 10.0 * W_MOTOR

            # 平均ST
            avg_st = e.get("avg_start") or FALLBACK_AVG_START
            score += avg_st * W_AVG_ST

            # F回数ペナルティ
            fl = e.get("fl_count") or 0
            score += fl * W_FL

        # 直近フォーム (ルックアヘッドなし — このレース前までの履歴のみ)
        if racer_no:
            win_r, top3_r = self._recent_stats(racer_no)
            score += win_r  * W_RECENT_WIN
            score += top3_r * W_RECENT_3RD

        return score

    def score_race(self, race_id: int) -> dict[int, float]:
        """1レース全艇のスコア辞書 {boat_no: score} を返す。"""
        results  = self.df_results[self.df_results["race_id"] == race_id]
        entries  = self.df_entries[self.df_entries["race_id"] == race_id] if self.has_entries else pd.DataFrame()

        entry_map: dict[int, dict] = {}
        for _, row in entries.iterrows():
            entry_map[int(row["boat_no"])] = row.to_dict()

        scores = {}
        for _, row in results.iterrows():
            bn = int(row["boat_no"])
            course = int(row["course"]) if pd.notna(row.get("course")) else None
            racer_no = int(row["racer_no"]) if pd.notna(row.get("racer_no")) else None
            entry_row = entry_map.get(bn)
            scores[bn] = self._score_one(bn, course, entry_row, racer_no)

        return scores

    def _update_history(self, race_id: int):
        """このレース結果を選手履歴に追記する (次以降のレースから参照)。"""
        results = self.df_results[self.df_results["race_id"] == race_id]
        for _, row in results.iterrows():
            rn = row.get("racer_no")
            if pd.notna(rn):
                rank = int(row["rank"]) if pd.notna(row.get("rank")) else None
                self._racer_hist[int(rn)].append(rank)

    # ------------------------------------------------------------------
    # 払戻ルックアップ
    # ------------------------------------------------------------------

    def _payout(self, race_id: int, bet_type: str, combo: str) -> int | None:
        rows = self.df_payouts[
            (self.df_payouts["race_id"] == race_id)
            & (self.df_payouts["bet_type"] == bet_type)
            & (self.df_payouts["combination"].str.strip() == combo.strip())
        ]
        return int(rows.iloc[0]["amount"]) if not rows.empty else None

    def _popularity(self, race_id: int, bet_type: str, combo: str) -> int | None:
        rows = self.df_payouts[
            (self.df_payouts["race_id"] == race_id)
            & (self.df_payouts["bet_type"] == bet_type)
            & (self.df_payouts["combination"].str.strip() == combo.strip())
        ]
        return int(rows.iloc[0]["popularity"]) if not rows.empty else None

    def _actual_result(self, race_id: int) -> dict[int, int]:
        """{boat_no: rank}。着順なしは除外。"""
        r = self.df_results[
            self.df_results["race_id"] == race_id
        ].dropna(subset=["rank"])
        return {int(row["boat_no"]): int(row["rank"]) for _, row in r.iterrows()}

    def _course_of(self, race_id: int, boat_no: int) -> int | None:
        row = self.df_results[
            (self.df_results["race_id"] == race_id)
            & (self.df_results["boat_no"] == boat_no)
        ]
        if row.empty:
            return None
        c = row.iloc[0].get("course")
        return int(c) if pd.notna(c) else None

    # ------------------------------------------------------------------
    # バックテスト実行
    # ------------------------------------------------------------------

    def run(self) -> dict[str, dict]:
        """全レースを時系列順に処理し、各戦略の損益を集計する。"""
        stats = {
            "S1_単勝":         {"bets": 0, "returns": 0, "hits": 0, "races": 0},
            "S2_3連複":        {"bets": 0, "returns": 0, "hits": 0, "races": 0},
            "S3_3連単BOX":     {"bets": 0, "returns": 0, "hits": 0, "races": 0},
            "S4_2連単流し":    {"bets": 0, "returns": 0, "hits": 0, "races": 0},
            "S5_単勝バリュー": {"bets": 0, "returns": 0, "hits": 0, "races": 0},
        }

        for _, race in self.df_races.iterrows():
            rid = int(race["id"])
            scores = self.score_race(rid)
            if len(scores) < 3:
                self._update_history(rid)
                continue

            ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            top1, top2, top3 = ranked[0][0], ranked[1][0], ranked[2][0]

            actual = self._actual_result(rid)
            if not actual:
                self._update_history(rid)
                continue

            # コース番号に変換 (払戻のキーはコース番号)
            def course(bn):
                c = self._course_of(rid, bn)
                return c or bn

            c1, c2, c3 = course(top1), course(top2), course(top3)

            def winner_course() -> int | None:
                for bn, rk in actual.items():
                    if rk == 1:
                        return course(bn)
                return None

            def finishers_courses() -> list[int]:
                ordered = sorted(actual.items(), key=lambda x: x[1])
                return [course(bn) for bn, _ in ordered[:3]]

            win_c = winner_course()
            fin3  = finishers_courses()

            # ── S1: スコア1位 単勝 ──────────────────────────────
            s = stats["S1_単勝"]
            s["races"] += 1
            s["bets"]  += self.bet
            if win_c == c1:
                p = self._payout(rid, "単勝", str(c1)) or 110
                s["returns"] += p; s["hits"] += 1

            # ── S2: 上位3艇 3連複 (1点) ──────────────────────────
            s = stats["S2_3連複"]
            s["races"] += 1
            s["bets"]  += self.bet
            trio_key = "".join(sorted([str(c1), str(c2), str(c3)]))
            if set(fin3) >= {c1, c2, c3}:
                p = self._payout(rid, "3連複", trio_key) or 0
                if p:
                    s["returns"] += p; s["hits"] += 1

            # ── S3: 上位3艇 3連単BOX (6点) ───────────────────────
            s = stats["S3_3連単BOX"]
            s["races"] += 1
            s["bets"]  += self.bet * 6
            if len(fin3) == 3:
                combo_key = "".join(str(x) for x in fin3)
                if set(fin3) == {c1, c2, c3}:
                    p = self._payout(rid, "3連単", combo_key) or 0
                    if p:
                        s["returns"] += p; s["hits"] += 1

            # ── S4: 1位固定 2連単流し (上位2・3位を2着、2点) ─────
            s = stats["S4_2連単流し"]
            s["races"] += 1
            s["bets"]  += self.bet * 2
            if len(fin3) >= 2:
                actual_1st = fin3[0]
                actual_2nd = fin3[1]
                if actual_1st == c1 and actual_2nd in (c2, c3):
                    key = f"{c1}{actual_2nd}"
                    p = self._payout(rid, "2連単", key) or 0
                    if p:
                        s["returns"] += p; s["hits"] += 1

            # ── S5: 単勝バリュー (1位 かつ 人気3位以下) ──────────
            s = stats["S5_単勝バリュー"]
            pop = self._popularity(rid, "単勝", str(c1))
            if pop is None or pop >= 3:
                s["races"] += 1
                s["bets"]  += self.bet
                if win_c == c1:
                    p = self._payout(rid, "単勝", str(c1)) or 110
                    s["returns"] += p; s["hits"] += 1

            self._update_history(rid)

        return stats


# ---------------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------------

def show_course_stats(df_results: pd.DataFrame):
    valid = df_results.dropna(subset=["rank"])
    wins  = valid[valid["rank"] == 1]
    starts = valid.groupby("course").size().rename("starts")
    w      = wins.groupby("course").size().rename("wins")
    tbl    = pd.concat([starts, w], axis=1).fillna(0).astype({"wins": int}).sort_index()
    tbl["win_rate"] = tbl["wins"] / tbl["starts"] * 100

    print("コース  出走数  1着数   1着率")
    print("-" * 40)
    for course, row in tbl.iterrows():
        bar = "█" * int(row["win_rate"] / 2)
        print(f"  {int(course)}    {row['starts']:5.0f}  {row['wins']:5d}  {row['win_rate']:5.1f}%  {bar}")
    print()


def show_results(stats: dict[str, dict], bet: int):
    header = f"{'戦略':<16}  {'点数':>3}  {'レース':>6}  {'的中':>5}  " \
             f"{'的中率':>6}  {'総賭金':>10}  {'総払戻':>10}  {'損益':>10}  {'回収率':>7}"
    print(header)
    print("-" * len(header))

    for name, s in stats.items():
        if s["races"] == 0:
            continue
        n_bets  = s["bets"] // bet  # 1レースあたりの点数
        roi     = s["returns"] / s["bets"] * 100 if s["bets"] else 0
        hit_r   = s["hits"] / s["races"] * 100 if s["races"] else 0
        net     = s["returns"] - s["bets"]
        label   = name.split("_", 1)[1]
        print(
            f"  {label:<14}  {n_bets:>3}  {s['races']:>6}  {s['hits']:>5}  "
            f"{hit_r:>5.1f}%  ¥{s['bets']:>9,}  ¥{s['returns']:>9,}  "
            f"¥{net:>+10,}  {roi:>6.1f}%"
        )


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def main():
    bet_amount = DEFAULT_BET
    history_n  = 30
    db_path    = DB_PATH

    i, args = 0, sys.argv[1:]
    while i < len(args):
        if args[i] == "--bet" and i + 1 < len(args):
            bet_amount = int(args[i + 1]); i += 2
        elif args[i] == "--history" and i + 1 < len(args):
            history_n = int(args[i + 1]); i += 2
        else:
            db_path = Path(args[i]); i += 1

    if not db_path.exists():
        sys.exit(f"DB が見つかりません: {db_path}\n先に parse.py を実行してください。")

    conn = sqlite3.connect(db_path)

    n_races   = conn.execute("SELECT COUNT(*) FROM races").fetchone()[0]
    n_results = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    n_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    dates     = conn.execute("SELECT MIN(date), MAX(date) FROM races").fetchone()

    print("=" * 70)
    print("  BOAT RACE バックテスト — 複合スコアリングモデル")
    print("=" * 70)
    print(f"  期間       : {dates[0]} ～ {dates[1]}")
    print(f"  レース数   : {n_races:,}")
    print(f"  成績件数   : {n_results:,}")
    has_entries = n_entries > 0
    print(f"  選手データ : {'あり (' + str(n_entries) + '件, B ファイル使用)' if has_entries else 'なし (コース・直近フォームのみ)'}")
    print(f"  賭け金     : ¥{bet_amount}/点  直近フォーム: {history_n}走")
    print()

    engine = BacktestEngine(bet_amount=bet_amount, history_n=history_n)
    engine.load(conn)
    conn.close()

    df_results = engine.df_results

    # ── スコアモデル特徴量 ──
    print("── スコアモデル特徴量 ─────────────────────────────────────")
    feats = [
        ("当地勝率",      f"× {W_VENUE_WR}",  "B ファイル"),
        ("全国勝率",      f"× {W_NAT_WR}",    "B ファイル"),
        ("モーター偏差",  f"× {W_MOTOR}",     "B ファイル"),
        ("コース係数",    f"× {W_COURSE}",    "常時"),
        ("平均ST",        f"× {W_AVG_ST}",    "B ファイル"),
        ("F回数",         f"× {W_FL}",        "B ファイル"),
        (f"直近{history_n}走 1着率", f"× {W_RECENT_WIN}", "K ファイル蓄積"),
        (f"直近{history_n}走 3着内率", f"× {W_RECENT_3RD}", "K ファイル蓄積"),
    ]
    for name, weight, src in feats:
        used = "✓" if src == "常時" or (src == "B ファイル" and has_entries) else "─"
        print(f"  {used}  {name:<20}  {weight:<8}  ({src})")
    print()

    # ── コース別1着率 ──
    print("── コース別 1着率 ────────────────────────────────────────")
    show_course_stats(df_results)

    # ── バックテスト実行 ──
    print("── 戦略別パフォーマンス ──────────────────────────────────")
    stats = engine.run()
    show_results(stats, bet_amount)
    print()

    # ── サマリー ──
    valid = {k: v for k, v in stats.items() if v["bets"] > 0}
    best_roi  = max(valid.items(), key=lambda x: x[1]["returns"] / x[1]["bets"])
    best_name = best_roi[0].split("_", 1)[1]
    best_r    = best_roi[1]["returns"] / best_roi[1]["bets"] * 100

    print(f"── サマリー ──────────────────────────────────────────────")
    print(f"  最高回収率: {best_name}  ({best_r:.1f}%)")
    if not has_entries:
        print("  ※ 選手データ (B ファイル) がないため、コース係数と直近フォームのみで採点しています。")
        print("    B ファイルを含めて parse.py を再実行すると精度が向上します。")
    print()


if __name__ == "__main__":
    main()
