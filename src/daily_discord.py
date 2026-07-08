"""
当日のレース予測を自動でDiscordに送信するスクリプト

バッチファイルから起動すると、本日のJRAレース全レースを取得し、
発走30分前になったら自動で予測→Discord送信を繰り返す。

使い方:
  python daily_discord.py           # 発走30分前に自動送信
  python daily_discord.py --ahead 20  # 発走20分前に送信
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

src_dir = Path(__file__).parent
sys.path.insert(0, str(src_dir))
from scraper import get_today_races


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ahead', type=int, default=30,
                        help='発走何分前に送信するか（デフォルト30）')
    args = parser.parse_args()

    print('本日のレース一覧を取得中...')
    races = get_today_races()

    if not races:
        print('本日のJRAレースが見つかりませんでした。')
        print('ヒント: 開催日以外はレースがありません。')
        return

    # 発走時刻でソート（時刻が取れていないものは末尾へ）
    races.sort(key=lambda r: r['start_time'] or '99:99')

    print(f'\n本日 {len(races)}レース を検出:')
    for r in races:
        t = r['start_time'] or '--:--'
        print(f'  {t}  {r["venue"]}  {r["race_name"]}  ({r["race_id"]})')

    print(f'\n発走 {args.ahead}分前 に予測→Discord送信します。')
    print('Ctrl+C で中断できます。\n')

    now = datetime.now()
    skipped = 0

    for race in races:
        if not race['start_time']:
            print(f'スキップ（時刻不明）: {race["race_name"]}')
            skipped += 1
            continue

        h, m = map(int, race['start_time'].split(':'))
        start_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        send_dt  = start_dt - timedelta(minutes=args.ahead)

        remaining = (send_dt - datetime.now()).total_seconds()

        if remaining < -60:
            print(f'スキップ（時間切れ）: {race["start_time"]}  {race["race_name"]}')
            skipped += 1
            continue

        if remaining > 0:
            mins = int(remaining // 60)
            secs = int(remaining % 60)
            print(f'待機中... {race["start_time"]} {race["venue"]} {race["race_name"]}')
            print(f'  送信予定: {send_dt.strftime("%H:%M")}  (あと {mins}分{secs:02d}秒)')
            try:
                time.sleep(remaining)
            except KeyboardInterrupt:
                print('\n中断しました。')
                return

        print(f'\n→ 予測送信: {race["race_id"]}  {race["race_name"]}')
        subprocess.run(
            [sys.executable, str(src_dir / 'predict.py'),
             race['race_id'], '--discord', '--no-pedigree'],
            check=False
        )
        print('送信完了\n')

    done = len(races) - skipped
    print(f'\n本日の処理完了: {done}レース送信 / {skipped}レーススキップ')


if __name__ == '__main__':
    main()
