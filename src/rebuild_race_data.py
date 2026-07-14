"""
races.csv の列ズレ修復・再収集スクリプト

netkeiba のHTML変更（「タイム指数」列の挿入）により、既存 races.csv は
- horse_weight 列に「通過順位」が入る
- last_3f（上がり）・odds が全て「**」で欠損
という列ズレ状態だった。ヘッダー名ベースに修正した scraper で全JRAレースを
再取得し、以下の列だけを正しい値に上書きする（race_date/race_name は保護）。

対象: c4_pos が空 または last_3f が欠損/「**」のJRAレース。
20レースごとに途中保存するので中断・再開が可能。

使い方:
  python rebuild_race_data.py                # 未修復レースを全て処理
  python rebuild_race_data.py --limit 500    # 500レースだけ
  python rebuild_race_data.py --delay 1.0
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
BACKUP_CSV = data_dir / 'races_before_rebuild.csv'
JRA_VENUES = {'01','02','03','04','05','06','07','08','09','10'}

sys.path.insert(0, str(src_dir))
from scraper import scrape_race_result

# race_date / race_name などレース共通情報は触らず、馬ごとの結果列だけ更新する
UPDATE_COLS = ['last_3f', 'odds', 'popularity', 'horse_weight', 'trainer',
               'c1_pos', 'c2_pos', 'c3_pos', 'c4_pos', 'running_style']


def _needs_fix(sub):
    """このレース（同一race_idの行群）が再収集対象か"""
    if 'c4_pos' not in sub.columns:
        return True
    c4_empty = sub['c4_pos'].isna() | (sub['c4_pos'].astype(str).str.strip() == '')
    l3f = sub['last_3f'].astype(str).str.strip()
    l3f_bad = l3f.isin(['', '**', 'nan', 'None'])
    return bool(c4_empty.all() or l3f_bad.all())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--delay', type=float, default=1.0)
    args = ap.parse_args()

    print('races.csv を読み込み中...')
    df = pd.read_csv(RACES_CSV, dtype=str, on_bad_lines='skip')
    print(f'  {len(df):,}行  {df["race_id"].nunique():,}レース')

    for c in ['c1_pos', 'c2_pos', 'c3_pos', 'c4_pos', 'running_style']:
        if c not in df.columns:
            df[c] = ''

    if not BACKUP_CSV.exists():
        shutil.copy(RACES_CSV, BACKUP_CSV)
        print(f'  バックアップ: {BACKUP_CSV.name}')

    df['_vc'] = df['race_id'].astype(str).str[4:6]
    jra = df[df['_vc'].isin(JRA_VENUES)]

    # 再収集が必要なJRAレースIDを抽出（最新から）
    targets = []
    for rid, sub in jra.groupby('race_id'):
        if _needs_fix(sub):
            targets.append(rid)
    targets = sorted(targets, reverse=True)
    print(f'  再収集対象: {len(targets):,}レース')

    if args.limit:
        targets = targets[:args.limit]
        print(f'  今回処理: {len(targets)}レース')

    if not targets:
        print('  すべて修復済みです。')
        df.drop(columns=['_vc'], errors='ignore').to_csv(RACES_CSV, index=False, encoding='utf-8')
        return

    # race_id + horse_num でインデックス化して高速更新
    df['_key'] = df['race_id'].astype(str) + '_' + df['horse_num'].astype(str)
    key_to_idx = {k: i for i, k in zip(df.index, df['_key'])}

    done = 0
    try:
        for rid in targets:
            rows = scrape_race_result(rid, delay=args.delay)
            if not rows:
                continue
            for r in rows:
                key = f"{rid}_{r.get('horse_num', '')}"
                idx = key_to_idx.get(key)
                if idx is None:
                    continue
                for c in UPDATE_COLS:
                    val = r.get(c, '')
                    if val != '' and val is not None:
                        df.at[idx, c] = str(val)
            done += 1
            if done % 20 == 0:
                _save(df)
                print(f'  {done}/{len(targets)} 処理済み（途中保存）', flush=True)
    except KeyboardInterrupt:
        print(f'\n中断（{done}レース処理済み）')

    _save(df)
    l3f_ok = (df[df['_vc'].isin(JRA_VENUES)]['last_3f'].astype(str).str.strip()
              .replace({'**': ''}).str.len() > 0).mean()
    print(f'\n完了: {done}レース再収集  JRA上がり充足率: {l3f_ok:.1%}')


def _save(df):
    df.drop(columns=['_vc', '_key'], errors='ignore').to_csv(
        RACES_CSV, index=False, encoding='utf-8')


if __name__ == '__main__':
    main()
