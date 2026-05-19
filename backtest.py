#!/usr/bin/env python3
"""
BOAT RACE バックテスト — 複合スコアリングモデル + 荒れ指数フィルター

Usage:
    python backtest.py [--bet AMOUNT] [--history N] [DB_PATH]

スコア構成要素:
    当地勝率 ×3.0 / 全国勝率 ×2.0 / モーター偏差 ×1.5 / コース係数 ×1.5
    平均ST ×-12 / F回数 ×-3 / 直近1着率 ×2.5 / 直近3着内率 ×0.8

荒れ指数 (0-1):
    スコアエントロピー ×0.50  全艇の点差が小さいほど高い
    1コース弱体度     ×0.20  内枠に格下選手がいるほど高い
    水面条件          ×0.20  風速・波高が高いほど高い
    外枠強さ          ×0.10  アウト勢が相対的に強いほど高い

賭け戦略:
    S1  単勝              スコア1位に単勝 (全レース)
    S2  3連複             上位3艇で3連複 (全レース)
    S3  3連単BOX          上位3艇で3連単BOX 6点 (全レース)
    S4  2連単流し          1位固定×2-3位を2着 2点 (全レース)
    S5  単勝バリュー       1位 かつ 市場人気3位以下のみ
    S6  荒れ絞り3連単BOX  荒れ指数≥閾値のレースのみ3連単BOX 6点
    S7  堅い絞り単勝      荒れ指数≤閾値のレースのみ単勝
    S8  荒れ絞り拡連複    荒れ指数≥閾値のレースで上位2艇の拡連複 3点
"""

import sys
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    sys.exit("ERROR: pandas がインストールされていません。  pip install pandas")

DB_PATH = Path("data/boatrace.db")
DEFAULT_BET = 100

# ---------------------------------------------------------------------------
# スコアリング定数
# ---------------------------------------------------------------------------

COURSE_FACTOR = {1: 1.00, 2: 0.42, 3: 0.30, 4: 0.16, 5: 0.12, 6: 0.09}

W_VENUE_WR   = 3.0
W_NAT_WR     = 2.0
W_MOTOR      = 1.5
W_COURSE     = 1.5
W_AVG_ST     = -12.0
W_FL         = -3.0
W_RECENT_WIN = 2.5
W_RECENT_3RD = 0.8

FALLBACK_AVG_START = 0.18
FALLBACK_MOTOR_2R  = 50.0

# ---------------------------------------------------------------------------
# 荒れ指数定数
# ---------------------------------------------------------------------------

CHOPPY_HIGH = 0.65   # これ以上 → 高荒れ判定
CHOPPY_LOW  = 0.45   # これ以下 → 堅いレース判定

# 荒れ指数コンポーネントの重み
CW_ENTROPY = 0.50
CW_C1_WEAK = 0.20
CW_WATER   = 0.20
CW_OUTER   = 0.10


# ---------------------------------------------------------------------------
# エンジン
# ---------------------------------------------------------------------------

class BacktestEngine:
    def __init__(self, bet_amount: int = DEFAULT_BET, history_n: int = 30):
        self.bet = bet_amount
        self.history_n = history_n
        self._racer_hist: dict[int, list] = defaultdict(list)

    # ------------------------------------------------------------------
    def load(self, conn: sqlite3.Connection):
        self.df_races = pd.read_sql(
            """SELECT id, date, venue_code, race_no,
                      wind_speed, wave_height
               FROM races ORDER BY date, venue_code, race_no""",
            conn,
        )
        self.df_results = pd.read_sql(
            "SELECT race_id, boat_no, course, racer_no, rank FROM results",
            conn,
        )
        self.df_entries = pd.read_sql(
            """SELECT race_id, boat_no, racer_no,
                      avg_start, fl_count,
                      national_winrate, national_2rate,
                      venue_winrate, venue_2rate,
                      motor_2rate, hull_2rate
               FROM entries""",
            conn,
        )
        self.df_payouts = pd.read_sql(
            "SELECT race_id, bet_type, combination, amount, popularity FROM payouts",
            conn,
        )
        self.has_entries = len(self.df_entries) > 0

        # race_id → results rows のキャッシュ (速度最適化)
        self._res_by_race  = {rid: grp for rid, grp in self.df_results.groupby("race_id")}
        self._ent_by_race  = {rid: grp for rid, grp in self.df_entries.groupby("race_id")} \
                              if self.has_entries else {}
        self._pay_by_race  = {rid: grp for rid, grp in self.df_payouts.groupby("race_id")}

    # ------------------------------------------------------------------
    # スコアリング
    # ------------------------------------------------------------------

    def _recent_stats(self, racer_no: int) -> tuple[float, float]:
        hist = self._racer_hist.get(racer_no, [])
        if not hist:
            return 0.0, 0.0
        recent = [r for r in hist[-self.history_n:] if r is not None]
        if not recent:
            return 0.0, 0.0
        n = len(recent)
        return (sum(1 for r in recent if r == 1) / n,
                sum(1 for r in recent if r <= 3) / n)

    def _score_one(self, boat_no, course, entry_row, racer_no) -> float:
        c = course or boat_no
        s = COURSE_FACTOR.get(c, 0.08) * W_COURSE
        if entry_row is not None:
            s += (entry_row.get("venue_winrate") or 0) * W_VENUE_WR
            s += (entry_row.get("national_winrate") or 0) * W_NAT_WR
            s += ((entry_row.get("motor_2rate") or FALLBACK_MOTOR_2R) - FALLBACK_MOTOR_2R) / 10 * W_MOTOR
            s += (entry_row.get("avg_start") or FALLBACK_AVG_START) * W_AVG_ST
            s += (entry_row.get("fl_count") or 0) * W_FL
        if racer_no:
            wr, t3 = self._recent_stats(racer_no)
            s += wr * W_RECENT_WIN + t3 * W_RECENT_3RD
        return s

    def score_race(self, race_id: int) -> dict[int, float]:
        results = self._res_by_race.get(race_id, pd.DataFrame())
        ent_grp = self._ent_by_race.get(race_id, pd.DataFrame())
        entry_map = {int(r["boat_no"]): r.to_dict() for _, r in ent_grp.iterrows()}
        scores = {}
        for _, row in results.iterrows():
            bn      = int(row["boat_no"])
            course  = int(row["course"]) if pd.notna(row.get("course")) else None
            racer   = int(row["racer_no"]) if pd.notna(row.get("racer_no")) else None
            scores[bn] = self._score_one(bn, course, entry_map.get(bn), racer)
        return scores

    def _update_history(self, race_id: int):
        for _, row in self._res_by_race.get(race_id, pd.DataFrame()).iterrows():
            rn = row.get("racer_no")
            if pd.notna(rn):
                rank = int(row["rank"]) if pd.notna(row.get("rank")) else None
                self._racer_hist[int(rn)].append(rank)

    # ------------------------------------------------------------------
    # 荒れ指数
    # ------------------------------------------------------------------

    def calc_choppiness(self, race_row: pd.Series,
                        scores: dict[int, float]) -> dict:
        """荒れ指数 (0-1) と各コンポーネントを返す。

        Returns:
            dict with keys: total, entropy, c1_weak, water, outer
        """
        vals = list(scores.values())
        n = len(vals)
        if n < 2:
            return {"total": 0.5, "entropy": 0.5,
                    "c1_weak": 0.5, "water": 0.0, "outer": 0.5}

        # ── 1. スコアエントロピー ────────────────────────────────
        shift   = max(vals)
        exp_v   = [math.exp(v - shift) for v in vals]
        tot_exp = sum(exp_v)
        probs   = [e / tot_exp for e in exp_v]
        entropy = -sum(p * math.log(max(p, 1e-10)) for p in probs)
        entropy_norm = entropy / math.log(n)   # 0=確定, 1=完全一様

        # ── 2. 1コース弱体度 ─────────────────────────────────────
        results = self._res_by_race.get(int(race_row["id"]), pd.DataFrame())
        c1_boats = results[results["course"] == 1]
        if not c1_boats.empty:
            c1_bn    = int(c1_boats.iloc[0]["boat_no"])
            c1_score = scores.get(c1_bn, min(vals))
            rank_0   = sorted(vals, reverse=True).index(c1_score)
            c1_weak  = rank_0 / (n - 1)   # 0=最強, 1=最弱
        else:
            c1_weak = 0.5

        # ── 3. 水面荒れ ──────────────────────────────────────────
        ws    = float(race_row.get("wind_speed")  or 0)
        wh    = float(race_row.get("wave_height") or 0)
        water = min(1.0, ws / 8.0 + wh / 15.0)

        # ── 4. 外枠（4-6コース）の相対強度 ───────────────────────
        cs_pairs = []
        for _, row in results.iterrows():
            c = row.get("course")
            if pd.notna(c):
                cs_pairs.append((int(c), scores.get(int(row["boat_no"]), 0)))
        inner = [s for c, s in cs_pairs if c <= 3]
        outer = [s for c, s in cs_pairs if c >= 4]
        if inner and outer:
            mean_all = sum(vals) / n
            std_all  = (sum((v - mean_all) ** 2 for v in vals) / n) ** 0.5 or 1
            outer_adv = (sum(outer) / len(outer) - sum(inner) / len(inner)) / std_all
            outer_norm = min(1.0, max(0.0, outer_adv * 0.5 + 0.5))
        else:
            outer_norm = 0.5

        # ── 総合 ─────────────────────────────────────────────────
        total = (CW_ENTROPY * entropy_norm
                 + CW_C1_WEAK * c1_weak
                 + CW_WATER   * water
                 + CW_OUTER   * outer_norm)

        return {
            "total":   round(min(1.0, max(0.0, total)), 3),
            "entropy": round(entropy_norm, 3),
            "c1_weak": round(c1_weak, 3),
            "water":   round(water, 3),
            "outer":   round(outer_norm, 3),
        }

    # ------------------------------------------------------------------
    # 払戻ルックアップ
    # ------------------------------------------------------------------

    def _payout(self, race_id, bet_type, combo) -> int | None:
        pays = self._pay_by_race.get(race_id, pd.DataFrame())
        rows = pays[(pays["bet_type"] == bet_type)
                    & (pays["combination"].str.strip() == str(combo).strip())]
        return int(rows.iloc[0]["amount"]) if not rows.empty else None

    def _popularity(self, race_id, bet_type, combo) -> int | None:
        pays = self._pay_by_race.get(race_id, pd.DataFrame())
        rows = pays[(pays["bet_type"] == bet_type)
                    & (pays["combination"].str.strip() == str(combo).strip())]
        return int(rows.iloc[0]["popularity"]) if not rows.empty else None

    def _course_of(self, race_id, boat_no) -> int | None:
        results = self._res_by_race.get(race_id, pd.DataFrame())
        row = results[results["boat_no"] == boat_no]
        if row.empty:
            return None
        c = row.iloc[0].get("course")
        return int(c) if pd.notna(c) else None

    def _actual_result(self, race_id) -> dict[int, int]:
        results = self._res_by_race.get(race_id, pd.DataFrame())
        return {int(r["boat_no"]): int(r["rank"])
                for _, r in results.dropna(subset=["rank"]).iterrows()}

    # ------------------------------------------------------------------
    # バックテスト実行
    # ------------------------------------------------------------------

    def run(self) -> tuple[dict[str, dict], list[dict]]:
        """全レースを時系列順に処理して (戦略集計, 荒れ指数ログ) を返す。"""
        stats = {
            "S1_単勝":            {"bets": 0, "returns": 0, "hits": 0, "races": 0},
            "S2_3連複":           {"bets": 0, "returns": 0, "hits": 0, "races": 0},
            "S3_3連単BOX":        {"bets": 0, "returns": 0, "hits": 0, "races": 0},
            "S4_2連単流し":       {"bets": 0, "returns": 0, "hits": 0, "races": 0},
            "S5_単勝バリュー":    {"bets": 0, "returns": 0, "hits": 0, "races": 0},
            "S6_荒れ3連単BOX":   {"bets": 0, "returns": 0, "hits": 0, "races": 0},
            "S7_堅い単勝":        {"bets": 0, "returns": 0, "hits": 0, "races": 0},
            "S8_荒れ拡連複":     {"bets": 0, "returns": 0, "hits": 0, "races": 0},
        }
        choppy_log = []

        for _, race in self.df_races.iterrows():
            rid    = int(race["id"])
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

            def course(bn):
                return self._course_of(rid, bn) or bn

            c1, c2, c3 = course(top1), course(top2), course(top3)

            ordered = sorted(actual.items(), key=lambda x: x[1])
            fin3    = [course(bn) for bn, _ in ordered[:3]]
            win_c   = fin3[0] if fin3 else None

            trio_key   = "".join(sorted([str(c1), str(c2), str(c3)]))
            combo_key  = "".join(str(x) for x in fin3[:3]) if len(fin3) == 3 else ""

            # ── 荒れ指数 ──────────────────────────────────────────
            choppy = self.calc_choppiness(race, scores)
            cv     = choppy["total"]

            choppy_log.append({
                "race_id":    rid,
                "date":       race["date"],
                "venue_code": race["venue_code"],
                "race_no":    int(race["race_no"]),
                "choppiness": cv,
                **{k: v for k, v in choppy.items() if k != "total"},
                "win_course": win_c,
                "top_course": c1,
                "model_hit":  int(win_c == c1) if win_c else 0,
            })

            # ── S1: 単勝 ─────────────────────────────────────────
            s = stats["S1_単勝"]
            s["races"] += 1; s["bets"] += self.bet
            if win_c == c1:
                p = self._payout(rid, "単勝", str(c1)) or 110
                s["returns"] += p; s["hits"] += 1

            # ── S2: 3連複 ─────────────────────────────────────────
            s = stats["S2_3連複"]
            s["races"] += 1; s["bets"] += self.bet
            if set(fin3) >= {c1, c2, c3}:
                p = self._payout(rid, "3連複", trio_key) or 0
                if p: s["returns"] += p; s["hits"] += 1

            # ── S3: 3連単BOX ──────────────────────────────────────
            s = stats["S3_3連単BOX"]
            s["races"] += 1; s["bets"] += self.bet * 6
            if combo_key and set(fin3) == {c1, c2, c3}:
                p = self._payout(rid, "3連単", combo_key) or 0
                if p: s["returns"] += p; s["hits"] += 1

            # ── S4: 2連単流し ─────────────────────────────────────
            s = stats["S4_2連単流し"]
            s["races"] += 1; s["bets"] += self.bet * 2
            if len(fin3) >= 2 and fin3[0] == c1 and fin3[1] in (c2, c3):
                p = self._payout(rid, "2連単", f"{c1}{fin3[1]}") or 0
                if p: s["returns"] += p; s["hits"] += 1

            # ── S5: 単勝バリュー ──────────────────────────────────
            s = stats["S5_単勝バリュー"]
            pop = self._popularity(rid, "単勝", str(c1))
            if pop is None or pop >= 3:
                s["races"] += 1; s["bets"] += self.bet
                if win_c == c1:
                    p = self._payout(rid, "単勝", str(c1)) or 110
                    s["returns"] += p; s["hits"] += 1

            # ── S6: 荒れ絞り 3連単BOX ────────────────────────────
            s = stats["S6_荒れ3連単BOX"]
            if cv >= CHOPPY_HIGH:
                s["races"] += 1; s["bets"] += self.bet * 6
                if combo_key and set(fin3) == {c1, c2, c3}:
                    p = self._payout(rid, "3連単", combo_key) or 0
                    if p: s["returns"] += p; s["hits"] += 1

            # ── S7: 堅いレース 単勝 ───────────────────────────────
            s = stats["S7_堅い単勝"]
            if cv <= CHOPPY_LOW:
                s["races"] += 1; s["bets"] += self.bet
                if win_c == c1:
                    p = self._payout(rid, "単勝", str(c1)) or 110
                    s["returns"] += p; s["hits"] += 1

            # ── S8: 荒れ絞り 拡連複 (上位2艇の3通り) ────────────
            s = stats["S8_荒れ拡連複"]
            if cv >= CHOPPY_HIGH:
                # c1 が絡む拡連複 3点: (c1,c2), (c1,c3), (c2,c3)
                combos_kaku = [
                    "".join(sorted([str(c1), str(c2)])),
                    "".join(sorted([str(c1), str(c3)])),
                    "".join(sorted([str(c2), str(c3)])),
                ]
                s["races"] += 1; s["bets"] += self.bet * 3
                for kk in combos_kaku:
                    p = self._payout(rid, "拡連複", kk) or 0
                    if p:
                        s["returns"] += p; s["hits"] += 1
                        break  # 1回の的中で複数は発生しない

            self._update_history(rid)

        return stats, choppy_log


# ---------------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------------

def show_course_stats(df_results: pd.DataFrame):
    valid  = df_results.dropna(subset=["rank"])
    wins   = valid[valid["rank"] == 1]
    starts = valid.groupby("course").size().rename("starts")
    w      = wins.groupby("course").size().rename("wins")
    tbl    = pd.concat([starts, w], axis=1).fillna(0).astype({"wins": int}).sort_index()
    tbl["win_rate"] = tbl["wins"] / tbl["starts"] * 100

    print("コース  出走数  1着数   1着率")
    print("-" * 40)
    for course, row in tbl.iterrows():
        bar = "█" * int(row["win_rate"] / 2)
        print(f"  {int(course)}    {row['starts']:5.0f}  {int(row['wins']):5d}  "
              f"{row['win_rate']:5.1f}%  {bar}")
    print()


def show_results(stats: dict[str, dict], bet: int):
    sep = "─" * 94
    header = (f"  {'戦略':<14}  {'点':>2}  {'レース':>6}  {'的中':>5}  "
              f"{'的中率':>6}  {'総賭金':>10}  {'総払戻':>10}  {'損益':>10}  {'回収率':>7}")
    print(header)
    print(sep)

    groups = [
        ("── 全レース戦略 ──", ["S1_単勝", "S2_3連複", "S3_3連単BOX",
                                 "S4_2連単流し", "S5_単勝バリュー"]),
        ("── 荒れ指数フィルター戦略 ──",
         ["S6_荒れ3連単BOX", "S7_堅い単勝", "S8_荒れ拡連複"]),
    ]

    for group_label, keys in groups:
        print(f"\n  {group_label}")
        for key in keys:
            s = stats.get(key, {})
            if not s or s["races"] == 0:
                continue
            n_pts  = s["bets"] // (bet * s["races"])
            roi    = s["returns"] / s["bets"] * 100
            hit_r  = s["hits"] / s["races"] * 100
            net    = s["returns"] - s["bets"]
            label  = key.split("_", 1)[1]
            print(f"  {label:<14}  {n_pts:>2}  {s['races']:>6}  {s['hits']:>5}  "
                  f"{hit_r:>5.1f}%  ¥{s['bets']:>9,}  ¥{s['returns']:>9,}  "
                  f"¥{net:>+10,}  {roi:>6.1f}%")


def show_choppiness_analysis(choppy_log: list[dict], bet: int):
    """荒れ指数の分布・ティア別パフォーマンスを表示する。"""
    df = pd.DataFrame(choppy_log)
    if df.empty:
        return

    total = len(df)
    high  = df[df["choppiness"] >= CHOPPY_HIGH]
    mid   = df[(df["choppiness"] >= CHOPPY_LOW) & (df["choppiness"] < CHOPPY_HIGH)]
    low   = df[df["choppiness"] < CHOPPY_LOW]

    print(f"  平均荒れ指数: {df['choppiness'].mean():.3f}  "
          f"(最小: {df['choppiness'].min():.3f}  最大: {df['choppiness'].max():.3f})")
    print()
    print(f"  {'ティア':<14}  {'件数':>5}  {'割合':>6}  "
          f"{'モデル1位的中率':>14}  {'荒れ指数(平均)':>14}")
    print("  " + "─" * 60)

    def row(label, sub):
        if len(sub) == 0:
            return
        hit_r   = sub["model_hit"].mean() * 100
        chop_m  = sub["choppiness"].mean()
        pct     = len(sub) / total * 100
        print(f"  {label:<14}  {len(sub):>5}  {pct:>5.1f}%  "
              f"{hit_r:>13.1f}%  {chop_m:>14.3f}")

    row(f"高荒れ (≥{CHOPPY_HIGH})",  high)
    row(f"中程度",                    mid)
    row(f"堅い   (<{CHOPPY_LOW})",   low)
    print()

    # ティア別の払戻率を単勝ベースで試算
    print(f"  ── 荒れ度ティア × 単勝 回収率試算 (払戻データなし時は¥110固定) ──")
    print(f"  {'ティア':<14}  {'賭回数':>6}  {'的中':>5}  {'推定回収率':>10}")
    print("  " + "─" * 44)

    def roi_row(label, sub):
        if len(sub) == 0:
            return
        n   = len(sub)
        hits = sub["model_hit"].sum()
        # 平均払戻が分からないのでここでは的中率だけ表示
        hr  = hits / n * 100
        print(f"  {label:<14}  {n:>6}  {hits:>5}  {hr:>9.1f}%  ← 払戻データあれば自動計算")

    roi_row(f"高荒れ (≥{CHOPPY_HIGH})", high)
    roi_row("中程度",                    mid)
    roi_row(f"堅い (<{CHOPPY_LOW})",    low)
    print()

    # 荒れ指数 上位10レース
    print("  ── 荒れ指数 上位10レース ──────────────────────────────────────")
    print(f"  {'日付':>10}  {'場':>4}  {'R':>2}  {'荒れ指数':>8}  "
          f"{'エントロピー':>10}  {'1コース弱':>8}  {'水面':>6}  {'モデル的中':>8}")
    print("  " + "─" * 68)

    for _, r in df.nlargest(10, "choppiness").iterrows():
        hit_mark = "✓" if r["model_hit"] else "─"
        print(f"  {r['date']:>10}  {r['venue_code']:>4}  {int(r['race_no']):>2}  "
              f"{r['choppiness']:>8.3f}  {r['entropy']:>10.3f}  "
              f"{r['c1_weak']:>8.3f}  {r['water']:>6.3f}  {hit_mark:>8}")
    print()

    # コンポーネント別平均
    print("  ── 荒れ指数 コンポーネント平均 ──────────────────────────────")
    for col, label in [
        ("entropy", f"スコアエントロピー (重み{CW_ENTROPY})"),
        ("c1_weak", f"1コース弱体度     (重み{CW_C1_WEAK})"),
        ("water",   f"水面荒れ          (重み{CW_WATER})"),
        ("outer",   f"外枠強さ          (重み{CW_OUTER})"),
    ]:
        bar_len = int(df[col].mean() * 20)
        print(f"  {label:<30}  {df[col].mean():.3f}  {'█' * bar_len}")
    print()


# ---------------------------------------------------------------------------
# メイン
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
        sys.exit(f"DB が見つかりません: {db_path}\npython parse.py を先に実行してください。")

    conn = sqlite3.connect(db_path)
    n_races   = conn.execute("SELECT COUNT(*) FROM races").fetchone()[0]
    n_results = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    n_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    dates     = conn.execute("SELECT MIN(date), MAX(date) FROM races").fetchone()

    print("=" * 70)
    print("  BOAT RACE バックテスト — 複合スコアリング + 荒れ指数フィルター")
    print("=" * 70)
    print(f"  期間       : {dates[0]} ～ {dates[1]}")
    print(f"  レース数   : {n_races:,}")
    print(f"  成績件数   : {n_results:,}")
    has_entries = n_entries > 0
    print(f"  選手データ : {'あり (' + str(n_entries) + '件)' if has_entries else 'なし (コース・直近フォームのみ)'}")
    print(f"  賭け金     : ¥{bet_amount}/点  直近フォーム: {history_n}走")
    print(f"  荒れ閾値   : 高荒れ≥{CHOPPY_HIGH}  堅い≤{CHOPPY_LOW}")
    print()

    engine = BacktestEngine(bet_amount=bet_amount, history_n=history_n)
    engine.load(conn)
    conn.close()

    # モデル特徴量
    print("── スコアモデル特徴量 ─────────────────────────────────────")
    for name, weight, src in [
        ("当地勝率",       f"× {W_VENUE_WR}",   "B ファイル"),
        ("全国勝率",       f"× {W_NAT_WR}",     "B ファイル"),
        ("モーター偏差",   f"× {W_MOTOR}",      "B ファイル"),
        ("コース係数",     f"× {W_COURSE}",     "常時"),
        ("平均ST",         f"× {W_AVG_ST}",     "B ファイル"),
        ("F回数",          f"× {W_FL}",         "B ファイル"),
        (f"直近{history_n}走 1着率", f"× {W_RECENT_WIN}", "K ファイル蓄積"),
        (f"直近{history_n}走 3着内率", f"× {W_RECENT_3RD}", "K ファイル蓄積"),
    ]:
        ok = "✓" if src in ("常時", "K ファイル蓄積") or (src == "B ファイル" and has_entries) else "─"
        print(f"  {ok}  {name:<20}  {weight:<8}  ({src})")
    print()

    # コース別勝率
    print("── コース別 1着率 ────────────────────────────────────────")
    show_course_stats(engine.df_results)

    # バックテスト実行
    stats, choppy_log = engine.run()

    # 戦略結果
    print("── 戦略別パフォーマンス ──────────────────────────────────")
    show_results(stats, bet_amount)
    print()

    # 荒れ指数分析
    print("── 荒れ指数 分析 ─────────────────────────────────────────")
    show_choppiness_analysis(choppy_log, bet_amount)

    # サマリー
    valid  = {k: v for k, v in stats.items() if v["bets"] > 0}
    best   = max(valid.items(), key=lambda x: x[1]["returns"] / x[1]["bets"])
    best_n = best[0].split("_", 1)[1]
    best_r = best[1]["returns"] / best[1]["bets"] * 100
    print(f"── サマリー ──────────────────────────────────────────────")
    print(f"  最高回収率: {best_n}  ({best_r:.1f}%)")

    # 荒れ絞り vs 全レースの比較メモ
    s6 = stats.get("S6_荒れ3連単BOX", {})
    s3 = stats.get("S3_3連単BOX", {})
    if s6.get("bets") and s3.get("bets"):
        roi6 = s6["returns"] / s6["bets"] * 100
        roi3 = s3["returns"] / s3["bets"] * 100
        diff = roi6 - roi3
        sign = "↑" if diff > 0 else "↓"
        print(f"\n  荒れ絞り3連単BOX vs 全レース3連単BOX: "
              f"{diff:+.1f}% {sign}  "
              f"(対象{s6['races']}レース / 全{s3['races']}レース, "
              f"{s6['races']/s3['races']*100:.0f}%に絞込)")

    s7 = stats.get("S7_堅い単勝", {})
    s1 = stats.get("S1_単勝", {})
    if s7.get("bets") and s1.get("bets"):
        roi7 = s7["returns"] / s7["bets"] * 100
        roi1 = s1["returns"] / s1["bets"] * 100
        diff = roi7 - roi1
        sign = "↑" if diff > 0 else "↓"
        print(f"  堅い絞り単勝     vs 全レース単勝    : "
              f"{diff:+.1f}% {sign}  "
              f"(対象{s7['races']}レース / 全{s1['races']}レース, "
              f"{s7['races']/s1['races']*100:.0f}%に絞込)")
    print()


if __name__ == "__main__":
    main()
