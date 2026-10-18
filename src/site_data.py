"""
カミロボ競馬場(https://kamirobo-keiba.keiba-bot-site.workers.dev)用のデータ data/site/data.json を作る。
サイトはこのファイルを GitHub から直接読むので、監視ループが回るたびに最新になる(PCもClaudeも不要)。
BOTの記録(CSV)を読むだけ。netkeibaへは「次の開催」を調べる時だけ、6時間に1回読む。
(2026-10-18 に keiba_bot_site/build_data.py から移した。サイト側での答え合わせは、
 BOTの精算(daily_settlement.py)が取りこぼしも拾うようになったのでやめた)
使い方: python src/site_data.py
"""
import csv
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

DATA = Path(__file__).parent.parent / 'data'
OUT = DATA / 'site'
JST = ZoneInfo('Asia/Tokyo')
VENUE = {'01': '札幌', '02': '函館', '03': '福島', '04': '新潟', '05': '東京', '06': '中山', '07': '中京',
         '08': '京都', '09': '阪神', '10': '小倉'}


def rows(path):
    if not path.exists():
        return []
    with open(path, encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def race_label(rid):
    return f"{VENUE.get(rid[4:6], '?')}{int(rid[-2:])}R"


def next_meeting(now):
    """netkeiba の開催カレンダーから、今日以降で最初の開催日と場を求める(6時間キャッシュ)"""
    cache = OUT / 'next_meeting.json'
    if cache.exists():
        c = json.loads(cache.read_text(encoding='utf-8'))
        if c.get('checked_at', '') >= (now - timedelta(hours=6)).strftime('%Y-%m-%d %H:%M') \
                and c.get('checked_at', '')[:10] == now.strftime('%Y-%m-%d') \
                and (c.get('checked_at', '')[11:13] >= '17') == (now.hour >= 17):
            return c.get('value')
    H = {'User-Agent': 'Mozilla/5.0'}
    # 17時を過ぎたら今日の開催は終わり → 明日以降を探す
    today = (now + timedelta(days=1) if now.hour >= 17 else now).strftime('%Y%m%d')
    days = []
    y, m = now.year, now.month
    for _ in range(2):
        r = requests.get('https://race.netkeiba.com/top/calendar.html', params={'year': y, 'month': m}, headers=H, timeout=20)
        r.encoding = 'UTF-8'
        days += sorted({d for d in re.findall(r'kaisai_date=(\d{8})', r.text) if d >= today})
        time.sleep(3)
        if days:
            break
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    value = None
    if days:
        d = days[0]
        r = requests.get('https://race.netkeiba.com/top/race_list_sub.html', params={'kaisai_date': d}, headers=H, timeout=20)
        r.encoding = 'UTF-8'
        venues = []
        for x in sorted({x for x in re.findall(r'race_id=(\d{12})', r.text) if x[4:6] in VENUE}):
            if VENUE[x[4:6]] not in venues:
                venues.append(VENUE[x[4:6]])
        wd = '月火水木金土日'[datetime.strptime(d, '%Y%m%d').weekday()]
        value = {'date': f'{d[:4]}-{d[4:6]}-{d[6:8]}',
                 'label': f"{int(d[4:6])}/{int(d[6:8])}({wd})" + (f" {'・'.join(venues)}" if venues else '')}
    OUT.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({'checked_at': now.strftime('%Y-%m-%d %H:%M'), 'value': value}, ensure_ascii=False), encoding='utf-8')
    return value


def main():
    now = datetime.now(JST)
    hb = rows(DATA / 'heartbeat_log.csv')
    sent = rows(DATA / 'sent_log.csv')  # 見出し行なし(1行目が見出し扱いになるので値の位置で読む)
    settle = rows(DATA / 'settle_log.csv')

    settled = [{'date': r['date'], 'race_id': r['race_id'], 'race': race_label(r['race_id']),
                'kind': r['kind'], 'num': r['horse_num'], 'name': r['horse_name'],
                'pos': r['pos'], 'odds': float(r['final_odds'] or 0), 'stake': int(r['stake'] or 0),
                'payout': int(r['payout'] or 0), 'source': 'BOT精算'} for r in settle]
    settled.sort(key=lambda x: (x['date'], x['race_id']))

    by_day = defaultdict(lambda: [0, 0, 0, 0])
    for s in settled:
        a = by_day[s['date']]
        a[0] += s['stake']
        a[1] += s['payout']
        a[2] += 1
        a[3] += s['payout'] > 0
    curve, cs, cp = [], 0, 0
    for d in sorted(by_day):
        cs += by_day[d][0]
        cp += by_day[d][1]
        curve.append({'date': d, 'stake': by_day[d][0], 'payout': by_day[d][1], 'bets': by_day[d][2],
                      'hits': by_day[d][3], 'cum_profit': cp - cs, 'cum_roi': round(cp / cs, 3) if cs else 0})
    by_kind = defaultdict(lambda: [0, 0, 0, 0])
    for s in settled:
        a = by_kind[s['kind']]
        a[0] += 1
        a[1] += s['stake']
        a[2] += s['payout']
        a[3] += s['payout'] > 0
    kinds = [{'kind': k, 'bets': v[0], 'stake': v[1], 'payout': v[2], 'hits': v[3]} for k, v in by_kind.items()]

    # 最新開催日の予想(pred_log は今月と先月だけ読む)
    preds = []
    for ym in sorted({now.strftime('%Y-%m'), (now.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')}):
        preds += rows(DATA / 'pred_log' / f'{ym}.csv')
    last_day = max(r['logged_at'][:10] for r in preds) if preds else None
    latest = defaultdict(dict)
    for r in preds:
        if r['logged_at'][:10] == last_day:
            latest[r['race_id']][r['horse_num']] = r
    races = []
    for rid, hs in sorted(latest.items()):
        top = sorted(hs.values(), key=lambda x: int(x['model_rank']))[:5]
        races.append({'race_id': rid, 'race': race_label(rid),
                      'horses': [{'num': h['horse_num'], 'name': h['horse_name'], 'rank': int(h['model_rank']),
                                  'p': float(h['p_bet'] or h['p_win']), 'odds': float(h['live_odds'] or 0),
                                  'ev': float(h['ev'] or 0)} for h in top]})

    picked = {(s['race_id'], s['num']) for s in settled if s['kind'].endswith('単勝')}
    series = defaultdict(list)
    for f in sorted((DATA / 'odds_timeline').glob('*.csv'))[-3:]:
        for r in rows(f):
            if (r['race_id'], r['horse_num']) in picked:
                try:
                    series[(r['race_id'], r['horse_num'])].append(
                        {'m': int(float(r['mins_to_post'] or 0)), 'odds': float(r['odds']), 'phase': r['phase']})
                except ValueError:
                    pass
    odds_moves = []
    for (rid, num), pts in series.items():
        s = next((x for x in settled if x['race_id'] == rid and x['num'] == num), None)
        odds_moves.append({'race_id': rid, 'race': race_label(rid), 'num': num, 'name': s['name'] if s else '',
                           'date': s['date'] if s else '', 'pos': s['pos'] if s else '',
                           'points': sorted(pts, key=lambda p: (p['phase'] == 'final', -p['m']))})
    odds_moves.sort(key=lambda x: x['date'])
    odds_moves = odds_moves[-20:]

    try:
        nm = next_meeting(now)
    except Exception as e:
        print(f'次の開催の取得に失敗: {e}')
        nm = None
    data = {
        'generated_at': now.strftime('%Y-%m-%d %H:%M'),
        'bot': {'monitor_days': len({list(h.values())[0] for h in hb}) if hb else 0,
                'last_heartbeat': (lambda h: f"{list(h.values())[0]} {list(h.values())[1]}")(hb[-1]) if hb else None,
                'races_sent': len(sent), 'last_sent': list(sent[-1].values())[1] if sent else None,
                'schedule': ['土日月 9:05 と 14:05 に監視を自動起動', '開催日の朝に最新の結果を取り込んでから予想',
                             '発走前に全馬の予想を記録し、条件を満たす買い目(記録用)だけDiscordへ',
                             'G1の日は昼までにG1予想をDiscordとこのサイトへ', '夜に答え合わせ・毎月再学習']},
        'next_meeting': nm,
        'totals': {'bets': len(settled), 'stake': cs, 'payout': cp, 'hits': sum(1 for s in settled if s['payout'] > 0)},
        'curve': curve, 'kinds': kinds,
        'recent': [s for s in settled if s['date'] >= '2026-09-01'][-30:],
        'big_hits': sorted([s for s in settled if s['payout'] > 0], key=lambda x: -x['payout'])[:5],
        'latest_day': last_day, 'latest_races': races, 'odds_moves': odds_moves,
        'arc': {'race': '凱旋門賞2026', 'date': '2026-10-04', 'result': '1着①ダリズ・2着⑦ベイシティローラー・3着⑯ダイヤモンドネックレス',
                'hit': '馬連1-7', 'payout_per100': 6930, 'stake': 600, 'payout': 41580, 'budget': 20000,
                'total_stake': 19900},
        'research': [
            {'t': '国内の単勝', 'text': 'どのオッズ帯を全部買っても回収75〜85%。人気でも穴でも、控除率の壁は越えられない(2022〜25年・約6,000R)'},
            {'t': '券種のズレ', 'text': '単勝から計算した確率と、馬連・ワイド・3連複の配当を1,800Rで比べた。毎年100%を超える所はなし'},
            {'t': '海外の日本馬', 'text': 'JRAが売る海外レースでは日本馬が買われすぎ。全部買うと回収45%、外国馬を全部買うと99%(122R)'},
            {'t': '海外ルール', 'text': '日本馬以外で、現地相場より2割以上割安・20倍以下の単勝: 香港178%、別データ72Rで188%。大きい当たり2本を除くと80%。有望だけど未確定→少額で実戦検証'},
            {'t': 'BOTの今', 'text': '国内の買い目は記録用(購入非推奨)、馬連は停止。結果データは毎朝最新を取り込み、古ければ警告'},
            {'t': '監視の強化', 'text': '馬体重・調教評価・取消/乗り替わり・JRA公式の馬場(クッション値)・WIN5・海外発売を記録。効くかは先に決めたやり方で検証'},
        ],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'data.json').write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    print('site data:', len(settled), '点 / 最新', last_day, len(races), 'レース')


if __name__ == '__main__':
    main()
