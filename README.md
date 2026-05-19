# BOAT RACE バックテストツール

競艇（ボートレース）公式データを使ったバックテストプロジェクトです。

## 構成

| ファイル | 役割 |
|---|---|
| `download.py` | LZH ファイル一括ダウンロード (3秒インターバル) |
| `parse.py` | 固定長テキスト → SQLite 保存 |
| `backtest.py` | コース別1着率ベース戦略シミュレーション |

## セットアップ

```bash
pip install -r requirements.txt
```

## 使い方

### 1. データダウンロード

```bash
# 直近7日分 (引数省略時)
python download.py

# 日付範囲指定 (YYYYMMDD)
python download.py 20260101 20260107
```

- `data/lzh/` に K ファイル (競走成績) と B ファイル (番組表) を保存
- 競走非開催日は HTTP 404 でスキップ

### 2. データパース

```bash
python parse.py

# デバッグ出力 (生レコード確認)
python parse.py --debug
```

- `data/boatrace.db` (SQLite) に保存
- テーブル: `races`, `results`, `payouts`

### 3. バックテスト

```bash
python backtest.py

# 賭け金変更
python backtest.py --bet 200
```

## データソース

- 競走成績: `http://www1.mbrace.or.jp/od2/K/{YYYYMM}/k{YYMMDD}.lzh`
- 番組表:   `http://www1.mbrace.or.jp/od2/B/{YYYYMM}/b{YYMMDD}.lzh`

## データベーススキーマ

```sql
races   (date, venue_code, race_no, weather, wind_dir, wind_speed, water_temp, wave_height)
results (race_id, boat_no, course, racer_no, rank, race_time, start_timing)
payouts (race_id, bet_type, combination, amount, popularity)
```

## 戦略概要

全戦略とも1レース当たり一定額の単勝賭けをシミュレート。

| 戦略 | 内容 |
|---|---|
| コース1 単勝 | 毎レース1コースへ単勝 (全国平均1着率 ~43%) |
| コース2 単勝 | 毎レース2コースへ単勝 (~18%) |
| コース3 単勝 | 毎レース3コースへ単勝 (~13%) |
| 最高勝率コース | データ内で最も1着率が高いコースへ単勝 |

> 公営競技の控除率は約25%のため、単純全賭け戦略の期待回収率は理論上75%前後になります。

## 注意事項

- 本ツールは教育・研究目的です
- 公式サイトへの過剰アクセスを避けるため3秒インターバルを設けています
- 固定長フォーマットのオフセット定数は `parse.py` 内の `KParser` クラスで管理しています。実データと照合して必要に応じて調整してください
