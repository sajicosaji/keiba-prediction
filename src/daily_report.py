"""
本日の監視レポート(日報)。その日に集めた記録だけを数えてDiscordに1日1回送る(netkeibaへは読みに行かない)。
  - 見張ったレース数、予想を記録した頭数、オッズの記録回数・確定オッズ
  - 出馬表(朝)・調教評価・馬体重・直前チェックを記録できたレース数
  - 取消・除外、騎手の乗り替わり、馬場の変化、大きな馬体重の増減、調教A評価の馬
  - 記録用の買い目の数
day_runner の最後(午後のループのあと)に呼ばれる。送った日は data/daily_report_log.csv に記録して二重送信しない。
使い方: python src/daily_report.py [--date 2026-10-12] [--dry] [--info-dir 別の記録フォルダ]
"""
import argparse
import csv
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SRC = Path(__file__).parent
DATA = SRC.parent / 'data'
JST = ZoneInfo('Asia/Tokyo')
MARK = DATA / 'daily_report_log.csv'
VENUE = {'01': '札幌', '02': '函館', '03': '福島', '04': '新潟', '05': '東京', '06': '中山', '07': '中京',
         '08': '京都', '09': '阪神', '10': '小倉'}


def rows(path):
    if not path.exists():
        return []
    with open(path, encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def label(rid):
    return f"{VENUE.get(rid[4:6], '?')}{int(rid[-2:])}R"


def jockey_key(s):
    return re.sub(r'^[▲△☆★◇◆]+', '', (s or '').strip())


def build(day, info_dir):
    ym = day[:7]
    preds = [r for r in rows(DATA / 'pred_log' / f'{ym}.csv') if r['logged_at'].startswith(day)]
    odds = [r for r in rows(DATA / 'odds_timeline' / f'{ym}.csv') if r['logged_at'].startswith(day)]
    info = [r for r in rows(info_dir / f'{ym}.csv') if r['logged_at'].startswith(day)]
    oik = [r for r in rows(info_dir / f'oikiri_{ym}.csv') if r['logged_at'].startswith(day)]
    bets = [r for r in rows(DATA / 'bet_log.csv') if r['logged_at'].startswith(day)]
    races = {r['race_id'] for r in preds} | {r['race_id'] for r in odds} | {r['race_id'] for r in info}
    if not races:
        return None
    snaps = {(r['race_id'], r['logged_at']) for r in odds if r['phase'] == 'pre'}
    finals = {r['race_id'] for r in odds if r['phase'] == 'final'}
    by_phase = defaultdict(set)
    for r in info:
        by_phase[r['phase']].add(r['race_id'])
    # 取消・除外(最後に見た状態)、乗り替わり(朝→直前)、馬場の変化(朝→直前、場ごと)
    last = {}
    morning = {}
    for r in info:
        key = (r['race_id'], r['horse_num'])
        last[key] = r
        if r['phase'] == 'morning':
            morning[key] = r
    scratch = sorted({(k[0], r['horse_num'], r['horse_name'], r['status']) for k, r in last.items() if r['status'] in ('取消', '除外')})
    changes = []
    for k, r in last.items():
        m = morning.get(k)
        if m and r['phase'] == 'final' and jockey_key(m['jockey']) and jockey_key(r['jockey']) != jockey_key(m['jockey']):
            changes.append((k[0], r['horse_num'], r['horse_name'], m['jockey'], r['jockey']))
    track = defaultdict(list)
    for r in sorted(info, key=lambda x: x['logged_at']):
        v = VENUE.get(r['race_id'][4:6], '?')
        if r['track'] and (not track[v] or track[v][-1] != r['track']):
            track[v].append(r['track'])
    big = []
    for k, r in last.items():
        try:
            d = int(r['weight_diff'])
        except (TypeError, ValueError):
            continue
        if abs(d) >= 14:
            big.append((abs(d), k[0], r['horse_num'], r['horse_name'], r['body_weight'], d))
    big.sort(reverse=True)
    grade_a = [(r['race_id'], r['horse_num'], r['horse_name'], r['comment']) for r in oik if r['grade'] == 'A']
    return {
        'races': len(races), 'venues': sorted({VENUE.get(x[4:6], '?') for x in races}),
        'preds': len(preds), 'snaps': len(snaps), 'finals': len(finals),
        'morning': len(by_phase['morning']), 'oikiri': len({r['race_id'] for r in oik}), 'oikiri_horses': len(oik),
        'weights': len(by_phase['weights']), 'final': len(by_phase['final']),
        'scratch': scratch, 'changes': changes, 'track': dict(track), 'big': big[:5], 'grade_a': grade_a,
        'bets': len(bets),
    }


def message(day, s):
    d = datetime.strptime(day, '%Y-%m-%d')
    head = f"📋 **競馬BOT 本日の監視レポート({d.month}/{d.day})** {'・'.join(s['venues'])} {s['races']}レース"
    lines = [head,
             f"🔭 予想を記録 {s['preds']}頭 / オッズの記録 {s['snaps']}回 / 確定オッズ {s['finals']}レース",
             f"📝 出馬表(朝) {s['morning']}R / 調教評価 {s['oikiri']}R・{s['oikiri_horses']}頭 / 馬体重 {s['weights']}R / 直前チェック {s['final']}R"]
    if s['track']:
        lines.append('🌱 馬場: ' + ' / '.join(f"{v} {'→'.join(t)}" for v, t in s['track'].items()))
    if s['scratch']:
        lines.append('🚫 取消・除外: ' + '、'.join(f"{label(r)} {n}番{nm}({st})" for r, n, nm, st in s['scratch'][:8]))
    if s['changes']:
        lines.append('🔁 乗り替わり: ' + '、'.join(f"{label(r)} {n}番{nm} {a}→{b}" for r, n, nm, a, b in s['changes'][:6]))
    if s['big']:
        lines.append('⚖ 馬体重の大きな増減: ' + '、'.join(f"{label(r)} {n}番{nm} {w}kg({d:+d})" for _, r, n, nm, w, d in s['big']))
    if s['grade_a']:
        lines.append(f"💪 調教A評価 {len(s['grade_a'])}頭: " + '、'.join(f"{label(r)} {n}番{nm}" for r, n, nm, _ in s['grade_a'][:6]))
    lines.append(f"📮 記録用の買い目 {s['bets']}点(検証中・購入非推奨)")
    lines.append('※集めた情報は記録のみ。買い目の判定には使っていません(あとで効くかを検証する材料)。')
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date')
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--info-dir')
    a = ap.parse_args()
    day = a.date or datetime.now(JST).strftime('%Y-%m-%d')
    if not a.dry and MARK.exists() and any(r.get('date') == day for r in rows(MARK)):
        print(f'{day} の日報は送信済み')
        return
    s = build(day, Path(a.info_dir) if a.info_dir else DATA / 'race_info')
    if not s:
        print(f'{day} は記録なし(非開催)。送りません')
        return
    msg = message(day, s)
    print(msg)
    if a.dry:
        return
    url = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
    if url:
        import requests
        requests.post(url, json={'content': msg[:1990]}, timeout=15)
    new = not MARK.exists()
    with open(MARK, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['date', 'sent_at', 'summary'])
        w.writerow([day, datetime.now(JST).strftime('%H:%M'), json.dumps({k: s[k] for k in ('races', 'preds', 'snaps', 'weights', 'final')})])


if __name__ == '__main__':
    main()
