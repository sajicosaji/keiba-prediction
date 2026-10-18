"""
【検証データ蓄積・記録のみ】当日の全JRAレースについて、オッズ以外の当日情報を記録する。
day_runner のループ(約13分おき)から毎回呼ばれ、レースごとに次の3回だけ記録する:
  - morning : その日最初に見たとき(発走400分前以内) … 出馬表(騎手・斤量・天候・馬場)＋調教評価
  - weights : 発走70分前以降で、馬体重が発表されたら1回 … 馬体重(増減)
  - final   : 発走20〜2分前 … 取消・除外、騎手の乗り替わり、馬場・天候の最終
出力: data/race_info/YYYY-MM.csv(出馬表) と data/race_info/oikiri_YYYY-MM.csv(調教評価)
買い判定には一切関与しない。目的は「市場が十分に使っていない当日情報があるか」を、
あとで事前登録した方法で検証するための材料集め(2026-10 点検の結論: 勝つには新しい情報が要る)。
netkeiba へは3秒以上あけ、1回の呼び出しで読むページ数に上限を設ける(監視ループを遅らせない)。
試運転: python src/race_info_logger.py --date 20261012 --now 14:20 --out /tmp/x  (過去日の動作確認)
"""
import argparse
import csv
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

SRC = Path(__file__).parent
JST = ZoneInfo('Asia/Tokyo')
OUT_DIR = SRC.parent / 'data' / 'race_info'
H = {'User-Agent': 'Mozilla/5.0'}
JRA_VENUES = {'01', '02', '03', '04', '05', '06', '07', '08', '09', '10'}
# 直前は発走20〜2分前(2026-10-18に15〜3分前から拡大。ループは約16分おきなので、12分幅だと1日5〜6レース取りこぼしていた)
MORNING_MAX, WEIGHT_FROM, FINAL_FROM, FINAL_TO = 400, 70, 20, 2
REQ_INTERVAL, MAX_REQ = 3.0, 24
HEADER = ['logged_at', 'race_id', 'start_time', 'mins_to_post', 'phase', 'weather', 'track',
          'horse_num', 'horse_name', 'status', 'jockey', 'weight_carried', 'body_weight', 'weight_diff']
OIKIRI_HEADER = ['logged_at', 'race_id', 'horse_num', 'horse_name', 'comment', 'grade']
_last = [0.0]
_n_req = [0]


def get(url):
    if _n_req[0] >= MAX_REQ:
        raise RuntimeError('今回の上限')
    wait = REQ_INTERVAL - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    r = requests.get(url, headers=H, timeout=20)
    _last[0] = time.time()
    _n_req[0] += 1
    r.encoding = 'UTF-8'
    return r.text


def races_of(date_s):
    t = get(f'https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={date_s}')
    s = BeautifulSoup(t, 'html.parser')
    out = []
    for item in s.find_all('li', class_=re.compile(r'RaceList_DataItem')):
        a = item.find('a', href=re.compile(r'race_id=\d{12}'))
        m = re.search(r'race_id=(\d{12})', a.get('href', '')) if a else None
        if not m or m.group(1)[4:6] not in JRA_VENUES:
            continue
        tt = item.find(class_=re.compile(r'RaceList_Itemtime|ItemTime', re.IGNORECASE))
        st = tt.get_text(strip=True) if tt else ''
        if re.fullmatch(r'\d{1,2}:\d{2}', st):
            out.append({'race_id': m.group(1), 'start_time': st})
    return out


def parse_shutuba(rid):
    s = BeautifulSoup(get(f'https://race.netkeiba.com/race/shutuba.html?race_id={rid}'), 'html.parser')
    d1 = s.select_one('.RaceData01')
    d1t = d1.get_text(' ', strip=True) if d1 else ''
    weather = (re.search(r'天候:(\S+)', d1t) or [None, ''])[1]
    track = (re.search(r'馬場:(\S+)', d1t) or [None, ''])[1]
    rows = []
    for tr in s.select('tr.HorseList'):
        tds = tr.select('td')
        if len(tds) < 9:
            continue
        num = tds[1].get_text(strip=True)
        name = tds[3].get_text(' ', strip=True).split(' ')[0]
        cls = ' '.join(tr.get('class', []))
        txt = tr.get_text(' ', strip=True)
        status = '取消' if ('Cancel' in cls or '取消' in txt) else '除外' if '除外' in txt else '出走'
        bw = tds[8].get_text(' ', strip=True)
        m = re.match(r'(\d+)\s*\(([-+]?\d+|前計不|計不)\)', bw)
        rows.append({'horse_num': num, 'horse_name': name, 'status': status,
                     'jockey': tds[6].get_text(' ', strip=True), 'weight_carried': tds[5].get_text(strip=True),
                     'body_weight': m.group(1) if m else '', 'weight_diff': m.group(2) if m else ''})
    return weather, track, rows


def parse_oikiri(rid):
    s = BeautifulSoup(get(f'https://race.netkeiba.com/race/oikiri.html?race_id={rid}'), 'html.parser')
    out = []
    for tr in s.select('tr'):
        tds = tr.select('td')
        if len(tds) < 6:
            continue
        num = tds[1].get_text(strip=True)
        if not num.isdigit():
            continue
        out.append({'horse_num': num, 'horse_name': tds[3].get_text(' ', strip=True).replace('前走', '').strip(),
                    'comment': tds[4].get_text(' ', strip=True), 'grade': tds[5].get_text(strip=True)})
    return out


def load_done(path):
    done = {}
    if path.exists():
        with open(path, encoding='utf-8') as f:
            for r in csv.DictReader(f):
                done.setdefault(r['race_id'], set()).add(r['phase'])
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date')   # 試運転用 YYYYMMDD
    ap.add_argument('--now')    # 試運転用 HH:MM
    ap.add_argument('--out')    # 試運転用の出力先
    a = ap.parse_args()
    now = datetime.now(JST)
    if a.date:
        d = datetime.strptime(a.date + (a.now or now.strftime('%H:%M')), '%Y%m%d%H:%M')
        now = d.replace(tzinfo=JST)
    out_dir = Path(a.out) if a.out else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f'{now.strftime("%Y-%m")}.csv'
    opath = out_dir / f'oikiri_{now.strftime("%Y-%m")}.csv'
    races = races_of(now.strftime('%Y%m%d'))
    if not races:
        print('race_info_logger: 本日開催なし')
        return
    done = load_done(path)
    todo = []
    for r in races:
        h, m = map(int, r['start_time'].split(':'))
        mins = (now.replace(hour=h, minute=m, second=0, microsecond=0) - now).total_seconds() / 60
        ph = done.get(r['race_id'], set())
        if FINAL_TO <= mins <= FINAL_FROM and 'final' not in ph:
            todo.append((0, r, mins, 'final'))      # 直前を最優先
        elif FINAL_FROM < mins <= WEIGHT_FROM and 'weights' not in ph:
            todo.append((1, r, mins, 'weights'))
        elif mins <= MORNING_MAX and mins > FINAL_TO and 'morning' not in ph:
            todo.append((2, r, mins, 'morning'))
    todo.sort(key=lambda x: (x[0], x[2]))
    new_file, new_o = not path.exists(), not opath.exists()
    n = {'morning': 0, 'weights': 0, 'final': 0, 'oikiri': 0}
    with open(path, 'a', newline='', encoding='utf-8') as f, open(opath, 'a', newline='', encoding='utf-8') as fo:
        w, wo = csv.writer(f), csv.writer(fo)
        if new_file:
            w.writerow(HEADER)
        if new_o:
            wo.writerow(OIKIRI_HEADER)
        ts = now.strftime('%Y-%m-%d %H:%M')
        for _, r, mins, phase in todo:
            rid = r['race_id']
            try:
                weather, track, rows = parse_shutuba(rid)
            except Exception as e:
                print(f'  {rid} {phase} 取得できず: {e}')
                if str(e) == '今回の上限':
                    break
                continue
            if phase == 'weights' and not any(x['body_weight'] for x in rows):
                continue  # まだ発表されていない→次回
            for x in rows:
                w.writerow([ts, rid, r['start_time'], f'{mins:.0f}', phase, weather, track, x['horse_num'], x['horse_name'],
                            x['status'], x['jockey'], x['weight_carried'], x['body_weight'], x['weight_diff']])
            n[phase] += 1
            if phase == 'morning':
                try:
                    for x in parse_oikiri(rid):
                        wo.writerow([ts, rid, x['horse_num'], x['horse_name'], x['comment'], x['grade']])
                    n['oikiri'] += 1
                except Exception as e:
                    print(f'  {rid} 調教 取得できず: {e}')
                    if str(e) == '今回の上限':
                        break
    print(f"race_info_logger: 朝{n['morning']} 調教{n['oikiri']} 馬体重{n['weights']} 直前{n['final']}レース (読んだページ{_n_req[0]})")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'race_info_logger エラー(無視): {e}')  # 記録係の失敗で監視ループを止めない
