"""
結果の帳簿(data/races.csv)が最新かを見張る。2026-09〜10月に帳簿が8/30で止まったまま
5週間誰も気づかず、その間の予想がすべて古い情報で作られた反省から追加。
  - 帳簿のJRA最新開催日 と、netkeiba の開催カレンダー上の「今日より前の最後の開催日」を比べる
  - 結果を data/ledger_status.json に書く(predict.py の通知と daily_report.py の日報が読む)
  - 古ければ Discord に警告(1日1回)
day_runner の朝、update_data.py の直後に呼ばれる。
使い方: python src/data_guard.py [--dry]
"""
import argparse
import csv
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

SRC = Path(__file__).parent
DATA = SRC.parent / 'data'
STATUS = DATA / 'ledger_status.json'
JST = ZoneInfo('Asia/Tokyo')
JRA = {'01', '02', '03', '04', '05', '06', '07', '08', '09', '10'}


def ledger_latest():
    latest = ''
    with open(DATA / 'races.csv', encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            rid = r.get('race_id', '')
            d = (r.get('race_date') or '')[:8]
            if len(rid) == 12 and rid[4:6] in JRA and d.isdigit() and len(d) == 8 and d > latest:
                latest = d
    return latest


LAG_DAYS = 3  # netkeibaの過去データページは1〜2日遅れて載るので、3日以上前の開催日までを「入っているべき」とする


def last_race_day(today):
    """「入っているべき」最後のJRA開催日 = 今日の LAG_DAYS 日以上前で最後の開催日(カレンダーから)。
    馬は普通1週間以上あけて走るので、1〜2日の遅れは予想にほぼ影響しない。止まったままを見逃さないのが目的"""
    days = []
    y, m = today.year, today.month
    for _ in range(2):
        r = requests.get('https://race.netkeiba.com/top/calendar.html', params={'year': y, 'month': m},
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
        r.encoding = 'UTF-8'
        from datetime import timedelta
        limit = (today - timedelta(days=LAG_DAYS)).strftime('%Y%m%d')
        days += [d for d in re.findall(r'kaisai_date=(\d{8})', r.text) if d <= limit]
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        time.sleep(2)
    return max(days) if days else ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()
    now = datetime.now(JST)
    latest = ledger_latest()
    try:
        expected = last_race_day(now)
    except Exception as e:
        print(f'カレンダー取得失敗: {e}')
        expected = ''
    stale = bool(expected) and latest < expected
    st = {'checked_at': now.strftime('%Y-%m-%d %H:%M'), 'ledger_latest': latest, 'expected': expected, 'stale': stale}
    prev = json.loads(STATUS.read_text(encoding='utf-8')) if STATUS.exists() else {}
    print(json.dumps(st, ensure_ascii=False))
    if a.dry:
        return
    STATUS.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding='utf-8')
    if stale and prev.get('alerted_on') != now.strftime('%Y-%m-%d'):
        url = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
        f = lambda d: f'{int(d[4:6])}/{int(d[6:8])}' if d else '?'  # noqa: E731
        msg = (f'⚠ **結果の帳簿が古いままです**(帳簿の最新 {f(latest)} / 実際は {f(expected)} まで開催あり)\n'
               '今日の予想は古い情報(近走・前走・中◯日など)で作られます。結果の取り込みが壊れている可能性があります。')
        if url:
            try:
                requests.post(url, json={'content': msg}, timeout=15)
            except Exception as e:
                print(f'Discord送信エラー: {e}')
        st['alerted_on'] = now.strftime('%Y-%m-%d')
        STATUS.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding='utf-8')


if __name__ == '__main__':
    main()
