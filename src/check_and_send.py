"""
GitHub Actions から呼ばれる1回実行スクリプト。
発走時刻が「今から ahead〜ahead+interval 分後」のJRAレースだけ予測してDiscordへ送る。

例: ahead=15, interval=15 の場合
  → 発走まで15〜30分のレースを送信
  → GitHub Actions で15分おきに実行すれば各レースを1回だけ送信できる

時刻は必ず日本時間（JST）で扱う。GitHub Actions のランナーは UTC のため、
datetime.now() をそのまま使うと発走時刻と9時間ズレて一切送信されない。
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SRC = Path(__file__).parent
sys.path.insert(0, str(SRC))
from scraper import get_today_races

JST = ZoneInfo('Asia/Tokyo')


def main():
    ahead    = int(os.environ.get('AHEAD_MINUTES', '15'))
    interval = int(os.environ.get('CHECK_INTERVAL', '15'))
    now      = datetime.now(JST)

    print(f'JRA競馬  {now.strftime("%Y-%m-%d")}  チェック時刻: {now.strftime("%H:%M")} JST')
    print(f'送信対象: 発走まで {ahead}〜{ahead + interval}分 のレース')

    races = get_today_races()
    if not races:
        print('本日のJRA開催なし（または取得失敗）')
        return

    sent = 0
    for race in races:
        if not race.get('start_time'):
            continue
        h, m = map(int, race['start_time'].split(':'))
        start_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        mins_to_start = (start_dt - now).total_seconds() / 60

        if ahead <= mins_to_start < ahead + interval:
            print(f'\n→ 送信: {race["start_time"]} {race["venue"]} {race["race_name"]} '
                  f'({race["race_id"]} / あと{mins_to_start:.0f}分)')
            subprocess.run(
                [sys.executable, str(SRC / 'predict.py'),
                 race['race_id'], '--discord', '--no-pedigree'],
                check=False,
            )
            sent += 1

    if sent == 0:
        print('  このタイミングで送信対象のレースなし')
    else:
        print(f'\n{sent}レース送信完了')


if __name__ == '__main__':
    main()
