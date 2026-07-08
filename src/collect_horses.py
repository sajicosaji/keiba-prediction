"""
馬プロフィール収集スクリプト（血統・過去成績・距離適性）

使い方:
  python collect_horses.py             # races.csv から馬IDを抽出して収集
  python collect_horses.py --delay 1.5 # サーバー負荷軽減（秒）

※ collect_data.py で race データを収集した後に実行してください。
"""
import argparse
import csv
import time
from pathlib import Path
from tqdm import tqdm
from scraper import scrape_horse_profile

DATA_DIR = Path(__file__).parent.parent / 'data'
RACES_CSV = DATA_DIR / 'races.csv'
HORSES_CSV = DATA_DIR / 'horses.csv'

FIELDNAMES = [
    'horse_id',
    'sire', 'dam_sire',
    'career_races', 'career_wins', 'career_top3',
    'turf_races', 'turf_wins', 'turf_top3',
    'dirt_races', 'dirt_wins', 'dirt_top3',
    'short_races', 'short_wins', 'short_top3',
    'mile_races', 'mile_wins', 'mile_top3',
    'middle_races', 'middle_wins', 'middle_top3',
    'long_races', 'long_wins', 'long_top3',
    'r1', 'r2', 'r3', 'r4', 'r5',
]


def load_existing_horse_ids():
    if not HORSES_CSV.exists():
        return set()
    with open(HORSES_CSV, encoding='utf-8') as f:
        return {row['horse_id'] for row in csv.DictReader(f)}


def get_horse_ids_from_races():
    if not RACES_CSV.exists():
        return set()
    ids = set()
    with open(RACES_CSV, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            hid = row.get('horse_id', '').strip()
            if hid:
                ids.add(hid)
    return ids


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--delay', type=float, default=1.2, help='スクレイピング間隔(秒)')
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)

    all_ids = get_horse_ids_from_races()
    existing = load_existing_horse_ids()
    new_ids = sorted(all_ids - existing)

    print(f'馬ID総数: {len(all_ids)}  収集済み: {len(existing)}  未取得: {len(new_ids)}')

    if not new_ids:
        print('収集する馬データはありません。')
        return

    write_header = not HORSES_CSV.exists()
    with open(HORSES_CSV, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction='ignore')
        if write_header:
            writer.writeheader()

        for horse_id in tqdm(new_ids, desc='馬プロフィール収集'):
            profile = scrape_horse_profile(horse_id, delay=args.delay)
            if profile:
                writer.writerow(profile)
                f.flush()

    print(f'\n完了: {HORSES_CSV}')


if __name__ == '__main__':
    main()
