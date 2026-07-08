"""
コーナー通過順位のバックフィルスクリプト

既存の races.csv に c1_pos〜c4_pos / running_style 列を追加し、
再スクレイピングで欠損データを埋める。

初回実行時: 列の追加 + 直近レースから順にバックフィル開始
再実行時:   未処理のレースを続きから処理（増分更新）

使い方:
  python update_corner_data.py                # 最新のレースから順に処理
  python update_corner_data.py --limit 200    # 今日は200レースだけ
  python update_corner_data.py --delay 1.5    # スクレイピング間隔
"""
import argparse
import shutil
import sys
import time
from pathlib import Path

import pandas as pd

src_dir  = Path(__file__).parent
data_dir = src_dir.parent / 'data'
RACES_CSV  = data_dir / 'races.csv'
BACKUP_CSV = data_dir / 'races_backup_no_corners.csv'

sys.path.insert(0, str(src_dir))
from scraper import scrape_race_result

CORNER_COLS = ['c1_pos', 'c2_pos', 'c3_pos', 'c4_pos', 'running_style']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=None, help='今回処理するレース数の上限')
    parser.add_argument('--delay', type=float, default=1.5)
    args = parser.parse_args()

    print('races.csv を読み込み中...')
    df = pd.read_csv(RACES_CSV, dtype=str, on_bad_lines='skip')
    print(f'  {len(df):,}行  {df["race_id"].nunique():,}レース')

    # 列が存在しない場合は追加
    added = False
    for col in CORNER_COLS:
        if col not in df.columns:
            df[col] = ''
            added = True

    if added:
        print('  c1〜c4 / running_style 列を追加しました')
        if not BACKUP_CSV.exists():
            shutil.copy(RACES_CSV, BACKUP_CSV)
            print(f'  バックアップ: {BACKUP_CSV.name}')

    # c4_pos が空のレースIDを抽出（直近のレースを優先）
    df['_c4_empty'] = df['c4_pos'].isna() | (df['c4_pos'].astype(str).str.strip() == '')
    empty_races = (
        df[df['_c4_empty']]
        .groupby('race_id')['_c4_empty'].all()
    )
    empty_race_ids = empty_races[empty_races].index.tolist()
    # 最新順にソート
    empty_race_ids = sorted(empty_race_ids, reverse=True)

    total_empty = len(empty_race_ids)
    print(f'  c4_pos未充填レース: {total_empty:,}件')

    if args.limit:
        empty_race_ids = empty_race_ids[:args.limit]
        print(f'  今回処理: {len(empty_race_ids)}件 (--limit {args.limit})')

    if not empty_race_ids:
        print('  すべてのレースにコーナーデータがあります。')
        _save(df, RACES_CSV)
        return

    print('  Ctrl+C で中断可能（再実行時は続きから処理）\n')

    done = 0
    try:
        for race_id in empty_race_ids:
            rows = scrape_race_result(race_id, delay=args.delay)
            if not rows:
                continue

            # 取得したコーナーデータを既存dfに反映
            for r in rows:
                mask = (df['race_id'] == race_id) & \
                       (df['horse_num'].astype(str) == str(r.get('horse_num', '')))
                if mask.any():
                    for col in CORNER_COLS:
                        df.loc[mask, col] = str(r.get(col, ''))

            done += 1
            if done % 20 == 0:
                print(f'  {done}/{len(empty_race_ids)} 処理済み... (途中保存)')
                _save(df, RACES_CSV)

    except KeyboardInterrupt:
        print(f'\n中断しました（{done}レース処理済み）')

    _save(df, RACES_CSV)
    has_c4 = (df['c4_pos'].notna() & (df['c4_pos'].str.strip() != '')).mean()
    print(f'\n完了: {done}レース更新  c4充填率: {has_c4:.1%}')
    print('次のステップ: 3_モデル訓練.bat を再実行してください。')


def _save(df, path):
    save_df = df.drop(columns=['_c4_empty'], errors='ignore')
    save_df.to_csv(path, index=False, encoding='utf-8')
    print(f'  保存完了: {path.name}')


if __name__ == '__main__':
    main()
