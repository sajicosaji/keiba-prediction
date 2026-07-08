"""
データ収集スクリプト

使い方:
  python collect_data.py                      # 2022〜2024年を収集
  python collect_data.py --years 2023 2024    # 年指定
  python collect_data.py --delay 2.0          # サーバー負荷軽減（秒）
"""
import argparse
import csv
from pathlib import Path
from tqdm import tqdm
from scraper import get_race_ids, scrape_race_result

DATA_DIR = Path(__file__).parent.parent / 'data'
RACES_CSV = DATA_DIR / 'races.csv'

FIELDNAMES = [
    'race_id', 'race_name', 'race_date', 'surface', 'distance', 'weather',
    'track_condition', 'finishing_pos', 'frame', 'horse_num', 'horse_name',
    'sex_age', 'weight_carried', 'jockey', 'time', 'margin', 'popularity',
    'odds', 'last_3f', 'trainer', 'horse_weight', 'horse_id',
    'c1_pos', 'c2_pos', 'c3_pos', 'c4_pos', 'running_style',
]


def load_existing_race_ids():
    if not RACES_CSV.exists():
        return set()
    with open(RACES_CSV, encoding='utf-8') as f:
        return {row['race_id'] for row in csv.DictReader(f)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--years', nargs='+', type=int, default=[2022, 2023, 2024])
    parser.add_argument('--delay', type=float, default=1.5, help='スクレイピング間隔(秒)')
    parser.add_argument('--start-month', type=int, default=None, help='最初の年の開始月(1-12)')
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    existing = load_existing_race_ids()
    write_header = not RACES_CSV.exists()

    with open(RACES_CSV, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction='ignore')
        if write_header:
            writer.writeheader()

        for i, year in enumerate(args.years):
            # 最初の年だけstart_monthを適用（2年目以降は1月から）
            start_mon = args.start_month if (i == 0 and args.start_month) else 1
            mon_label = f'{start_mon}月〜' if start_mon > 1 else ''
            print(f'\n=== {year}年{mon_label} レースID収集中 ===')
            race_ids = get_race_ids(year, delay=args.delay, start_mon=start_mon)
            new_ids = [rid for rid in race_ids if rid not in existing]
            print(f'  合計 {len(race_ids)}件 / 未取得 {len(new_ids)}件')

            for race_id in tqdm(new_ids, desc=str(year)):
                rows = scrape_race_result(race_id, delay=args.delay)
                if rows:
                    writer.writerows(rows)
                    f.flush()
                existing.add(race_id)

    print(f'\n完了: {RACES_CSV}')


if __name__ == '__main__':
    main()
