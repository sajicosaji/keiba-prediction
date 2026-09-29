"""
【検証データ蓄積・記録のみ】当日の全JRAレースの単勝オッズ推移を記録する。
day_runner のループ(約13分おき)から毎回呼ばれ、
  - 発走150〜5分前のレース: 前回記録から25分以上空いていればスナップショット
  - 発走後10分以上経ったレース: 確定オッズを1回だけ記録(phase=final)
を data/odds_timeline/YYYY-MM.csv に追記する。買い判定には一切関与しない。

目的: 「締切前に人気を落としている馬(ドリフト)は負けやすいか」を
先読みバイアス無しで検証するため(判定ルールは memory keiba_odds_logging 参照)。
"""
import csv
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

SRC = Path(__file__).parent
sys.path.insert(0, str(SRC))
from scraper import get_today_races

JST = ZoneInfo('Asia/Tokyo')
OUT_DIR = SRC.parent / 'data' / 'odds_timeline'
PRE_MAX, PRE_MIN = 150, 5     # 発走何分前の範囲を記録するか
GAP_MIN = 25                  # 同一レースの記録間隔(分)
FINAL_AFTER = 10              # 発走後何分で確定オッズを取りに行くか
HEADER = ['logged_at', 'race_id', 'start_time', 'mins_to_post', 'phase',
          'official_datetime', 'horse_num', 'odds', 'popularity']


def fetch_odds(race_id):
    r = requests.get(
        'https://race.netkeiba.com/api/api_get_jra_odds.html',
        params={'race_id': race_id, 'type': '1', 'action': 'update'},
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://race.netkeiba.com/'},
        timeout=15,
    )
    j = r.json()
    d = j.get('data') or {}
    if not isinstance(d, dict):
        return j.get('status'), '', {}
    return j.get('status'), d.get('official_datetime', ''), (d.get('odds') or {}).get('1', {})


def load_state(path):
    """race_id -> (最終pre記録時刻, final記録済みか)"""
    last_pre, has_final = {}, set()
    if not path.exists():
        return last_pre, has_final
    with open(path, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            rid = row['race_id']
            if row['phase'] == 'final':
                has_final.add(rid)
            else:
                t = datetime.strptime(row['logged_at'], '%Y-%m-%d %H:%M').replace(tzinfo=JST)
                if rid not in last_pre or t > last_pre[rid]:
                    last_pre[rid] = t
    return last_pre, has_final


def main():
    now = datetime.now(JST)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f'{now.strftime("%Y-%m")}.csv'
    races = get_today_races()
    if not races:
        print('odds_logger: 本日開催なし')
        return
    last_pre, has_final = load_state(path)
    new_file = not path.exists()
    n_snap = 0
    with open(path, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(HEADER)
        for race in races:
            st = race.get('start_time')
            if not st:
                continue
            rid = race['race_id']
            h, m = map(int, st.split(':'))
            start_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            mins = (start_dt - now).total_seconds() / 60
            if PRE_MIN <= mins <= PRE_MAX:
                prev = last_pre.get(rid)
                if prev and (now - prev).total_seconds() / 60 < GAP_MIN:
                    continue
                phase = 'pre'
            elif mins <= -FINAL_AFTER and rid not in has_final:
                phase = 'final'
            else:
                continue
            try:
                status, off, tan = fetch_odds(rid)
            except Exception as e:
                print(f'  {rid} 取得失敗: {e}')
                continue
            if phase == 'final' and status != 'result':
                continue  # まだ確定していない→次回
            if not tan:
                continue
            ts = now.strftime('%Y-%m-%d %H:%M')
            for num, v in tan.items():
                try:
                    w.writerow([ts, rid, st, f'{mins:.0f}', phase, off,
                                int(num), float(v[0]), int(v[2])])
                except (ValueError, TypeError, IndexError):
                    continue
            n_snap += 1
            time.sleep(0.5)
    print(f'odds_logger: {n_snap}レース記録')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        # 記録係の失敗で監視ループを止めない
        print(f'odds_logger エラー(無視): {e}')
