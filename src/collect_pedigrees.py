"""
血統データ一括収集スクリプト

races.csv に記録されているすべての馬の父（sire）・母父（dam_sire）を取得し
data/pedigrees.csv に保存する。増分更新なので途中停止後の再実行が可能。

収集後は 3_モデル訓練.bat を再実行してモデルを再訓練してください。

使い方:
  python collect_pedigrees.py                # 全馬を収集
  python collect_pedigrees.py --limit 500    # 最初の500頭のみ（テスト用）
  python collect_pedigrees.py --delay 1.5    # スクレイピング間隔（秒）
"""
import argparse
import csv
import sys
from pathlib import Path

import pandas as pd

src_dir  = Path(__file__).parent
data_dir = src_dir.parent / 'data'
RACES_CSV     = data_dir / 'races.csv'
PEDIGREES_CSV = data_dir / 'pedigrees.csv'

sys.path.insert(0, str(src_dir))
from scraper import scrape_horse_pedigree

FIELDNAMES = ['horse_id', 'sire', 'dam_sire']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=None, help='収集頭数の上限（省略=全馬）')
    parser.add_argument('--delay', type=float, default=1.2, help='スクレイピング間隔（秒）')
    args = parser.parse_args()

    if not RACES_CSV.exists():
        print('races.csv が見つかりません。先にデータ収集を実行してください。')
        sys.exit(1)

    print('races.csv から馬IDを読み込み中...')
    df = pd.read_csv(RACES_CSV, dtype=str, on_bad_lines='skip')
    if 'horse_id' not in df.columns:
        print('races.csv に horse_id 列がありません。データを再収集してください。')
        sys.exit(1)

    all_ids = df['horse_id'].dropna().unique().tolist()
    all_ids = [h.strip() for h in all_ids if h.strip()]
    print(f'  ユニーク馬ID: {len(all_ids):,}頭')

    # 既存データを読み込み（増分更新）
    existing: set = set()
    if PEDIGREES_CSV.exists():
        ex = pd.read_csv(PEDIGREES_CSV, dtype=str)
        existing = set(ex['horse_id'].dropna().str.strip())
        print(f'  既存データ: {len(existing):,}頭')

    new_ids = [h for h in all_ids if h not in existing]
    if args.limit:
        new_ids = new_ids[:args.limit]

    if not new_ids:
        print('  新規収集対象はありません。pedigrees.csv は最新です。')
        return

    print(f'  新規収集: {len(new_ids):,}頭  (間隔: {args.delay}s)')
    print('  Ctrl+C で中断可能（再実行時は続きから収集）\n')

    write_header = not PEDIGREES_CSV.exists()
    done = 0
    try:
        with open(PEDIGREES_CSV, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction='ignore')
            if write_header:
                writer.writeheader()

            for hid in new_ids:
                ped = scrape_horse_pedigree(hid, delay=args.delay)
                writer.writerow({
                    'horse_id': hid,
                    'sire':     ped.get('sire', '') if ped else '',
                    'dam_sire': ped.get('dam_sire', '') if ped else '',
                })
                f.flush()
                done += 1
                if done % 50 == 0:
                    print(f'  {done:,}/{len(new_ids):,} 完了...')
    except KeyboardInterrupt:
        print(f'\n中断しました（{done}頭収集済み）。再実行で続きから収集されます。')
        return

    print(f'\n完了: {done:,}頭を収集  -> {PEDIGREES_CSV}')
    print('次のステップ: 3_モデル訓練.bat を実行してモデルを再訓練してください。')


if __name__ == '__main__':
    main()
