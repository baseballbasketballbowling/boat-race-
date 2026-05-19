#!/usr/bin/env python3
"""
BOAT RACE バックテスト シミュレーター

コース別1着率をベースにした単純戦略で損益を試算します。

Usage:
    python backtest.py [--bet AMOUNT] [DB_PATH]

    --bet AMOUNT : 1レースあたりの賭け金 (円, 省略時: 100)
    DB_PATH      : SQLite DB ファイル (省略時: data/boatrace.db)

戦略一覧:
    1. コース1 単勝 全賭け (最も基本的な戦略)
    2. コース2 単勝 全賭け (比較用)
    3. コース別 全1着率ランキング表示
    4. コース勝率上位コース × 拡連複 (払戻データあり時)
"""

import sys
import sqlite3
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    sys.exit("ERROR: pandas がインストールされていません。  pip install pandas")

DB_PATH = Path("data/boatrace.db")
DEFAULT_BET = 100  # 円


# ---------------------------------------------------------------------------
# データロード
# ---------------------------------------------------------------------------

def load_results(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT
            r.date, r.venue_code, r.race_no,
            res.race_id, res.boat_no,
            res.course, res.racer_no, res.rank
        FROM results res
        JOIN races r ON r.id = res.race_id
        WHERE res.course IS NOT NULL
        ORDER BY r.date, r.venue_code, r.race_no, res.course
        """,
        conn,
    )


def load_payouts(conn: sqlite3.Connection, bet_type: str) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT race_id, bet_type, combination, amount FROM payouts WHERE bet_type = ?",
        conn,
        params=(bet_type,),
    )


# ---------------------------------------------------------------------------
# 統計表示
# ---------------------------------------------------------------------------

def show_course_stats(df: pd.DataFrame):
    df_valid = df[df["rank"].notna()]
    df_win   = df_valid[df_valid["rank"] == 1]

    starts = df_valid.groupby("course").size().rename("starts")
    wins   = df_win.groupby("course").size().rename("wins")
    stats  = pd.concat([starts, wins], axis=1).fillna(0).astype({"wins": int})
    stats["win_rate"] = stats["wins"] / stats["starts"] * 100
    stats = stats.sort_index()

    print("コース  出走数   1着数   1着率")
    print("-" * 38)
    for course, row in stats.iterrows():
        bar = "█" * int(row["win_rate"] / 2)
        print(
            f"  {int(course)}    {row['starts']:5.0f}  {row['wins']:5d}  "
            f"{row['win_rate']:5.1f}%  {bar}"
        )
    print()
    return stats


# ---------------------------------------------------------------------------
# バックテスト共通
# ---------------------------------------------------------------------------

def run_backtest(
    df: pd.DataFrame,
    df_payouts: pd.DataFrame,
    bet_course: int,
    bet_amount: int,
    bet_type: str = "単勝",
    label: str = "",
):
    """指定コースへの単純全賭け戦略をシミュレートする。"""
    races = (
        df[["race_id", "date", "venue_code", "race_no"]]
        .drop_duplicates("race_id")
    )

    total_bets    = 0
    total_returns = 0
    wins          = 0
    no_payout_data = 0

    for _, race in races.iterrows():
        rid = race["race_id"]

        # 対象コースの艇を探す
        slot = df[(df["race_id"] == rid) & (df["course"] == bet_course)]
        if slot.empty:
            continue  # このレースに指定コース艇なし

        rank = slot.iloc[0]["rank"]
        total_bets += bet_amount

        if pd.notna(rank) and int(rank) == 1:
            # 勝ち → 払戻取得
            prow = df_payouts[
                (df_payouts["race_id"] == rid)
                & (df_payouts["combination"].str.strip() == str(bet_course))
            ]
            if not prow.empty:
                total_returns += int(prow.iloc[0]["amount"])
                wins += 1
            else:
                # 払戻データ未取得時は最低払戻 ¥110 でカウント
                total_returns += 110
                wins += 1
                no_payout_data += 1

    n_races = len(races)
    roi     = total_returns / total_bets * 100 if total_bets > 0 else 0
    wr      = wins / n_races * 100 if n_races > 0 else 0
    net     = total_returns - total_bets

    title = label or f"コース{bet_course} {bet_type} 全賭け (¥{bet_amount}/race)"
    print(f"【{title}】")
    print(f"  対象レース数  : {n_races:,}")
    print(f"  総賭け金      : ¥{total_bets:,}")
    print(f"  的中数        : {wins} ({wr:.1f}%)")
    print(f"  総払戻金      : ¥{total_returns:,}")
    print(f"  損益          : ¥{net:+,}")
    print(f"  回収率        : {roi:.1f}%")
    if no_payout_data:
        print(f"  ※ 払戻データ未取得 {no_payout_data} 件 → ¥110 で代替集計")
    print()


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    # 引数パース
    bet_amount = DEFAULT_BET
    db_path    = DB_PATH
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--bet" and i + 1 < len(args):
            bet_amount = int(args[i + 1]); i += 2
        else:
            db_path = Path(args[i]); i += 1

    if not db_path.exists():
        sys.exit(f"DB が見つかりません: {db_path}\n先に parse.py を実行してください。")

    conn = sqlite3.connect(db_path)

    # データ件数確認
    n_races = conn.execute("SELECT COUNT(*) FROM races").fetchone()[0]
    n_results = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    date_range = conn.execute(
        "SELECT MIN(date), MAX(date) FROM races"
    ).fetchone()

    print("=" * 55)
    print("  BOAT RACE バックテスト結果")
    print("=" * 55)
    print(f"  期間   : {date_range[0]} ～ {date_range[1]}")
    print(f"  レース数: {n_races:,}")
    print(f"  成績件数: {n_results:,}")
    print()

    df = load_results(conn)
    if df.empty:
        sys.exit("成績データがありません。parse.py を再実行してください。")

    # コース別勝率表示
    print("── コース別 1着率 ─────────────────────────────")
    stats = show_course_stats(df)

    # 単勝払戻データ
    df_tansho = load_payouts(conn, "単勝")
    has_payout = not df_tansho.empty
    if not has_payout:
        print("  ※ 払戻データなし (T7 形式不一致の可能性) → 最低払戻 ¥110 で代替\n")

    print("── 戦略シミュレーション ────────────────────────")

    # 戦略1: コース1 単勝
    run_backtest(df, df_tansho, bet_course=1, bet_amount=bet_amount)

    # 戦略2: コース2 単勝
    run_backtest(df, df_tansho, bet_course=2, bet_amount=bet_amount)

    # 戦略3: コース3 単勝
    run_backtest(df, df_tansho, bet_course=3, bet_amount=bet_amount)

    # 戦略4: 最高勝率コース
    best_course = int(stats["win_rate"].idxmax())
    if best_course not in (1, 2, 3):
        run_backtest(
            df, df_tansho,
            bet_course=best_course,
            bet_amount=bet_amount,
            label=f"コース{best_course} 単勝 (データ内最高勝率)",
        )

    conn.close()


if __name__ == "__main__":
    main()
