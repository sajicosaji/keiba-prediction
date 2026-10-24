"""
G1の日に、詳細予想(血統ON)を自動で作って Discord に送り、サイト用の data/g1/site.json に書く。
カミロボ競馬場の G1 ページ(https://kamirobo-keiba.keiba-bot-site.workers.dev/g1/)は、
このファイルを GitHub から直接読んで表示する(PCもClaudeも不要)。
  - 今日のJRAのG1を netkeiba のレース一覧(G1の印)から探す(1日1回だけ読み、data/g1/today.json に控える)
  - 11:00(JST)を過ぎたら、まだ出していないG1を予想。発走済みのレースは出さない
day_runner の朝と午後のループから毎回呼ばれる(出し済みなら何もしない)。
使い方: python src/g1_auto.py [--race-id 202608050811 --force] [--dry]
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

SRC = Path(__file__).parent
sys.path.insert(0, str(SRC))
import g1_post  # noqa: E402
import g1_research  # noqa: E402

OUT = SRC.parent / 'data' / 'g1'
SITE = OUT / 'site.json'
TODAY = OUT / 'today.json'
JST = ZoneInfo('Asia/Tokyo')
PAGE = 'https://kamirobo-keiba.keiba-bot-site.workers.dev/g1/'
START_HOUR = 11
JRA = {'01', '02', '03', '04', '05', '06', '07', '08', '09', '10'}


def todays_g1(now):
    """今日のJRAのG1 → [{race_id, post}]。1日1回だけnetkeibaを読む"""
    day = now.strftime('%Y%m%d')
    if TODAY.exists():
        t = json.loads(TODAY.read_text(encoding='utf-8'))
        if t.get('date') == day:
            return t['races']
    r = requests.get('https://race.netkeiba.com/top/race_list_sub.html', params={'kaisai_date': day},
                     headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
    r.encoding = 'UTF-8'
    races = []
    for li in re.findall(r'<li class="RaceList_DataItem[^"]*">(.*?)</li>', r.text, re.S):
        if not re.search(r'Icon_GradeType1(?!\d)', li):
            continue
        m = re.search(r'race_id=(\d{12})', li)
        if not m or m.group(1)[4:6] not in JRA:
            continue
        pm = re.search(r'RaceList_Itemtime">\s*(\d{1,2}:\d{2})', li)
        races.append({'race_id': m.group(1), 'post': pm.group(1) if pm else ''})
    races = [dict(t) for t in {tuple(x.items()) for x in races}]
    OUT.mkdir(parents=True, exist_ok=True)
    TODAY.write_text(json.dumps({'date': day, 'races': races}, ensure_ascii=False), encoding='utf-8')
    return races


def make(rid, now):
    """predict(血統ON)を回し、各馬の材料を g1_research でまとめる。
    (2026-10-24 から印はオッズが土台。BOTだけが推す大穴は印に入れず、注意書きに回す = 秋華賞の反省)"""
    jpath = OUT / f'_research_{rid}.json'
    OUT.mkdir(parents=True, exist_ok=True)
    out = g1_post.run_predict(rid, json_out=jpath)
    header, marks, ev, ten = g1_post.extract(out)
    if not jpath.exists():
        return {'race_id': rid, 'marks': []}, ''
    data = json.loads(jpath.read_text(encoding='utf-8'))
    jpath.unlink()
    rows, caution = g1_research.build(data)
    a, b, c = g1_research.lines_for_site(rows)
    ten = [t for t in ten if '脚質分布' in t or 'ペース' in t or '→' in t]
    entry = {'race_id': rid, 'made_at': now.strftime('%Y-%m-%d %H:%M'), 'header': header,
             'marks': a, 'caution': caution, 'ten': ten, 'rows': rows,
             'blocks': [{'title': '印と勝つ見込み(オッズが土台)', 'lines': a},
                        {'title': '調べた材料(○よい / ×気になる / ・メモ)', 'lines': b},
                        {'title': 'BOTの見方(参考・印には使っていない)', 'lines': c, 'cls': 'ev'},
                        {'title': '展開', 'lines': ten}]}
    return entry, g1_research.discord_text(header, rows, caution, ten, PAGE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--race-id')
    ap.add_argument('--force', action='store_true', help='時刻・出し済みに関係なく作る(手動用)')
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--prelim', action='store_true', help='前日版: サイトにだけ載せる(Discordへは送らない。当日の本番で差し替わる)')
    a = ap.parse_args()
    now = datetime.now(JST)
    site = json.loads(SITE.read_text(encoding='utf-8')) if SITE.exists() else []
    done = {x['race_id'] for x in site if not x.get('prelim')}
    if a.race_id:
        todo = [{'race_id': a.race_id, 'post': ''}]
    else:
        if now.hour < START_HOUR and not a.force:
            return
        try:
            todo = todays_g1(now)
        except Exception as e:
            print(f'G1の確認に失敗: {e}')
            return
    for r in todo:
        rid = r['race_id']
        if rid in done and not a.force:
            continue
        if r['post'] and not a.force and now.strftime('%H:%M') >= r['post'].zfill(5):
            print(f'{rid} は発走済み({r["post"]})なので出さない')
            continue
        entry, msg = make(rid, now)
        if not entry['marks']:
            print(f'{rid}: 予想が作れませんでした(印なし)。あとでやり直します')
            continue
        print(msg)
        if a.dry:
            continue
        if a.prelim:
            entry['prelim'] = True
        site = [x for x in site if x['race_id'] != rid]
        site.insert(0, entry)
        site.sort(key=lambda x: x['made_at'], reverse=True)
        OUT.mkdir(parents=True, exist_ok=True)
        SITE.write_text(json.dumps(site[:20], ensure_ascii=False, indent=1), encoding='utf-8')
        wh = g1_post.get_webhook()
        if wh and not a.prelim:
            g1_post.post(wh, msg)
        done.add(rid)
        time.sleep(3)


if __name__ == '__main__':
    main()
