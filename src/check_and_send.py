"""
GitHub Actions から呼ばれる1回実行スクリプト。
発走が「今から ahead_min〜ahead_max 分後」で未送信のJRAレースを予測してDiscordへ送る。

送信済みレースは data/sent_log.csv に記録され（ワークフローがコミット）、
GitHub Actions の cron 遅延・スキップがあっても重複送信や取りこぼしを防ぐ。

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
SENT_LOG = SRC.parent / 'data' / 'sent_log.csv'


def load_sent():
    if not SENT_LOG.exists():
        return set()
    return {line.split(',')[0].strip() for line in
            SENT_LOG.read_text(encoding='utf-8').splitlines() if line.strip()}


def mark_sent(race_id):
    with open(SENT_LOG, 'a', encoding='utf-8') as f:
        f.write(f'{race_id},{datetime.now(JST).strftime("%Y-%m-%d %H:%M")}\n')


def main():
    ahead_min = int(os.environ.get('AHEAD_MIN', '8'))
    ahead_max = int(os.environ.get('AHEAD_MAX', '40'))
    now = datetime.now(JST)

    print(f'JRA競馬  {now.strftime("%Y-%m-%d")}  チェック時刻: {now.strftime("%H:%M")} JST')
    print(f'送信対象: 発走まで {ahead_min}〜{ahead_max}分 の未送信レース')

    races = get_today_races()
    if not races:
        print('本日のJRA開催なし（または取得失敗）')
        return

    sent_ids = load_sent()
    sent = 0
    for race in races:
        if not race.get('start_time'):
            continue
        if race['race_id'] in sent_ids:
            continue
        h, m = map(int, race['start_time'].split(':'))
        start_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        mins_to_start = (start_dt - now).total_seconds() / 60

        if ahead_min <= mins_to_start < ahead_max:
            print(f'\n→ 送信: {race["start_time"]} {race["venue"]} {race["race_name"]} '
                  f'({race["race_id"]} / あと{mins_to_start:.0f}分)')
            proc = subprocess.run(
                [sys.executable, str(SRC / 'predict.py'),
                 race['race_id'], '--discord', '--no-pedigree'],
                check=False,
            )
            # 予測が失敗した場合は送信済みにしない（次の実行で再試行される）
            if proc.returncode == 0:
                mark_sent(race['race_id'])
                sent += 1
            else:
                print(f'  予測失敗 (exit={proc.returncode}) → 未送信のまま次回再試行')

    if sent == 0:
        print('  このタイミングで送信対象のレースなし')
    else:
        print(f'\n{sent}レース送信完了')


if __name__ == '__main__':
    main()
