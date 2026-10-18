"""
JRA公式の情報を記録する係(3つ)。どれも記録・お知らせのみで、買い目の判定には使わない(あとで効くかを検証する材料)。
  baba     馬場係   : 全競馬場の芝クッション値・含水率(芝/ダート × ゴール前/4コーナー)を data/jra_info/baba.csv に追記
  win5     WIN5係   : 対象レースを win5_targets.csv に、キャリーオーバーを win5_carry.csv に記録。新しく発生したらDiscordで知らせる
  overseas 海外発売係: JRAが馬券を売る海外レースの一覧を overseas_sales.csv に記録。新しく決まったらDiscordで知らせる
                      (海外発売は「日本馬が買われすぎ・それ以外の20倍以下の単勝が割安」の仮説を検証中。唯一の優位性の候補)
JRAの robots.txt は全ページ許可。同じサイトへは3秒以上あけて、1回の実行で数ページだけ読む。
初回(state.json が無い時)は今ある分を「既に知っている」として記録だけし、通知しない。
使い方: python src/jra_info_logger.py [baba] [win5] [overseas] [--dry]   (省略時は baba と win5。海外発売は src/overseas.py が担当)
"""
import argparse
import csv
import html
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

SRC = Path(__file__).parent
OUT = SRC.parent / 'data' / 'jra_info'
STATE = OUT / 'state.json'
JST = ZoneInfo('Asia/Tokyo')
BASE = 'https://www.jra.go.jp'
UA = {'User-Agent': 'Mozilla/5.0 (keiba-bot; record only)'}
WAIT = 3
_last = [0.0]


def get(path):
    gap = time.time() - _last[0]
    if gap < WAIT:
        time.sleep(WAIT - gap)
    r = requests.get(BASE + path, headers=UA, timeout=20)
    _last[0] = time.time()
    r.raise_for_status()
    return r.content.decode('cp932', errors='replace')


def text(s):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', s))).strip()


def append(path, header, rows):
    if not rows:
        return
    new = not path.exists()
    with open(path, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


def read_keys(path, cols):
    if not path.exists():
        return set()
    with open(path, encoding='utf-8') as f:
        return {tuple(r[c] for c in cols) for r in csv.DictReader(f)}


def jp_time(s, now):
    """'10月18日（日曜）7時00分' → '2026-10-18 07:00'(年は今年。年明けに12月分を読んだら前年)"""
    m = re.search(r'(\d{1,2})月(\d{1,2})日.*?(\d{1,2})時(\d{2})分', s)
    if not m:
        return ''
    mo, d, h, mi = map(int, m.groups())
    y = now.year - 1 if mo > now.month + 1 else now.year
    return f'{y}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}'


def discord(msg, dry):
    print(msg)
    url = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
    if dry or not url:
        return
    try:
        requests.post(url, json={'content': msg[:1990]}, timeout=15)
    except Exception as e:
        print(f'Discord送信エラー: {e}')


# ---------------- 馬場係 ----------------
def cushion_label(v):
    if v >= 12:
        return '硬め'
    if v >= 10:
        return 'やや硬め'
    if v >= 8:
        return '標準'
    if v >= 7:
        return 'やや軟らかめ'
    return '軟らかめ'


def baba(now, dry):
    path = OUT / 'baba.csv'
    seen = read_keys(path, ['venue', 'kind', 'measured_at'])
    rows = []
    for kind, page in (('cushion', '/keiba/baba/_data_cushion.html'), ('moist', '/keiba/baba/_data_moist.html')):
        h = get(page)
        for vm in re.finditer(r'<div id="rc[A-Z]" title="([^"]+)">(.*?)<!-- /\[#rc', h, re.S):
            venue, body = vm.group(1), vm.group(2)
            for um in re.finditer(r'<div class="unit">(.*?)(?=<div class="unit">|$)', body, re.S):
                u = um.group(1)
                tm = re.search(r'<div class="time">(.*?)</div>', u, re.S)
                at = jp_time(text(tm.group(1)), now) if tm else ''
                if not at or (venue, kind, at) in seen:
                    continue
                if kind == 'cushion':
                    cm = re.search(r'<div class="cushion">(.*?)</div>', u, re.S)
                    vals = [text(cm.group(1)) if cm else '', '', '', '', '']
                else:
                    def num(part, cls):
                        pm = re.search(rf'<div class="{part}">(.*?)</div>', u, re.S)
                        vm2 = re.search(rf'class="{cls}"[^>]*>(.*?)</span>', pm.group(1), re.S) if pm else None
                        return text(vm2.group(1)) if vm2 else ''
                    nm = re.search(r'moist_caution">(.*?)</ul>', u, re.S)
                    note = text(nm.group(1)).replace('注記：', '').strip() if nm else ''
                    vals = ['', num('turf', 'mg'), num('turf', 'm4c'), num('dirt', 'mg'), num('dirt', 'm4c')]
                    vals.append(note)
                rows.append([now.strftime('%Y-%m-%d %H:%M'), venue, kind, at] + vals[:5] + [vals[5] if len(vals) > 5 else ''])
                seen.add((venue, kind, at))
    rows.sort(key=lambda r: (r[3], r[1], r[2]))
    print(f'馬場係: 新しい測定 {len(rows)}件')
    if not dry:
        append(path, ['logged_at', 'venue', 'kind', 'measured_at', 'cushion', 'turf_goal', 'turf_4c', 'dirt_goal', 'dirt_4c', 'note'], rows)
    return rows


def baba_today(day):
    """日報用: その日に測った各場の最新クッション値と芝含水率(ゴール前)"""
    path = OUT / 'baba.csv'
    if not path.exists():
        return {}
    out = {}
    with open(path, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            if not r['measured_at'].startswith(day):
                continue
            v = out.setdefault(r['venue'], {})
            if r['kind'] == 'cushion' and r['cushion']:
                v['cushion'] = r['cushion']
            if r['kind'] == 'moist' and r['turf_goal']:
                v['moist'] = r['turf_goal']
    return out


# ---------------- WIN5係 ----------------
def win5(now, state, dry):
    t = text(get('/kouza/win5/info/racelist.html'))
    tpath = OUT / 'win5_targets.csv'
    seen = read_keys(tpath, ['date'])
    trows = []
    for m in re.finditer(r'(\d{1,2})月(\d{1,2})日 （[^）]+） (\d{1,2})時(\d{2})分 ((?:\S+?\d{1,2}R ?){5})', t):
        mo, d = int(m.group(1)), int(m.group(2))
        y = now.year - 1 if mo > now.month + 1 else (now.year + 1 if mo + 10 < now.month else now.year)
        date = f'{y}-{mo:02d}-{d:02d}'
        if (date,) in seen:
            continue
        legs = m.group(5).split()
        trows.append([now.strftime('%Y-%m-%d %H:%M'), date, f'{int(m.group(3)):02d}:{m.group(4)}'] + legs)
        seen.add((date,))
    if not dry:
        append(tpath, ['logged_at', 'date', 'close', 'leg1', 'leg2', 'leg3', 'leg4', 'leg5'], trows)
    h = get('/kouza/win5/result.html')
    carries = []
    for m in re.finditer(r'<div class="carry_over_list_unit" id="carry_(\d{8})">(.*?)<!-- /\[\.carry_over_list_unit\] -->', h, re.S):
        body = m.group(2)
        am = re.search(r'class="carry"><strong[^>]*>(.*?)</strong>', body, re.S)
        nx = re.search(r'<p class="vote">(.*?)</p>.*?<p class="amount">(.*?)</p>', body, re.S)
        carries.append({'date': m.group(1), 'amount': text(am.group(1)) if am else '',
                        'next': text(nx.group(1)) if nx else '', 'next_payout': text(nx.group(2)) if nx else ''})
    cpath = OUT / 'win5_carry.csv'
    known = read_keys(cpath, ['date', 'next'])
    crow = [[now.strftime('%Y-%m-%d %H:%M'), c['date'], c['amount'], c['next'], c['next_payout']]
            for c in carries if (c['date'], c['next']) not in known]
    if not dry:
        append(cpath, ['logged_at', 'date', 'amount', 'next', 'next_payout'], crow)
    first = 'win5_seen' not in state
    seen_c = set(state.get('win5_seen', []))
    for c in carries:
        if c['date'] in seen_c:
            continue
        seen_c.add(c['date'])
        if first:
            continue
        d = c['date']
        discord(f"💰 **WIN5でキャリーオーバー発生: {c['amount']}**({int(d[4:6])}/{int(d[6:8])})\n"
                '次回のWIN5は、払戻金にこの額が上乗せされます。WIN5は普段は控除率30%で不利な券種ですが、'
                '上乗せが大きいほど不利が縮みます(売上が少ない回ほど効果大)。\n'
                '※お知らせのみ。BOTの予想にWIN5の実績はありません。購入の判断はご自身で。', dry)
    state['win5_seen'] = sorted(seen_c)
    pending = [c for c in carries if not c['next']]
    state['win5_pending'] = pending[0] if pending else None
    print(f'WIN5係: 対象レース 新規{len(trows)}日 / キャリーオーバー記録 {len(carries)}件(未消化 {len(pending)})')


# ---------------- 海外発売係 ----------------
def overseas(now, state, dry):
    h = get('/keiba/overseas/racelist/')
    races = []
    for tr in re.findall(r'<tr>(.*?)</tr>', h, re.S):
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)
        if len(tds) < 4 or '年' not in tds[0]:
            continue
        lm = re.search(r'href="([^"]+)"', tds[2])
        fm = re.search(r'alt="([^"]+)"', tds[1])
        races.append({'date': text(tds[0]), 'country': (fm.group(1).replace('国旗', '') if fm else ''), 'course': text(tds[1]),
                      'race': text(tds[2]), 'distance': text(tds[3]), 'winner': text(tds[4]) if len(tds) > 4 else '',
                      'link': lm.group(1) if lm else ''})
    top = get('/keiba/overseas/')
    links = sorted(set(re.findall(r'href="(/keiba/overseas/race/[^"#]+)"', top)))
    path = OUT / 'overseas_sales.csv'
    known = read_keys(path, ['date', 'race', 'winner'])
    rows = [[now.strftime('%Y-%m-%d %H:%M'), r['date'], r['country'], r['course'], r['race'], r['distance'], r['winner'], r['link']]
            for r in races if (r['date'], r['race'], r['winner']) not in known]
    if not dry:
        append(path, ['logged_at', 'date', 'country', 'course', 'race', 'distance', 'winner', 'link'], rows)
    first = 'overseas_seen' not in state
    seen = set(state.get('overseas_seen', []))
    new_races = [r for r in races if r['date'] + r['race'] not in seen]
    new_links = [l for l in links if l.split('/')[4] not in {s.split('/')[4] for s in state.get('overseas_links', [])}]
    if not first and (new_races or new_links):
        lines = ['🌏 **JRAの海外馬券発売に新しい情報**']
        for r in new_races:
            lines.append(f"・{r['date']} {r['country']} {r['course']} {r['race']} {r['distance']}")
        if new_links and not new_races:
            lines.append('・新しい発売レースのページ: ' + '、'.join(BASE + l for l in new_links[:3]))
        lines.append('海外発売は「日本馬が買われすぎ・それ以外の20倍以下の単勝が割安」という仮説を検証中です(唯一の優位性の候補・未確定)。'
                     '凱旋門賞のときと同じ「JRAオッズと現地オッズの比較」はClaudeに頼めば作れます。※お知らせのみ。購入の判断はご自身で。')
        discord('\n'.join(lines), dry)
    state['overseas_seen'] = sorted(seen | {r['date'] + r['race'] for r in races})
    state['overseas_links'] = sorted(set(state.get('overseas_links', [])) | set(links))
    print(f'海外発売係: 一覧 {len(races)}レース(新規記録 {len(rows)}) / 新しいレース {0 if first else len(new_races)}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('parts', nargs='*', default=['baba', 'win5'])
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(JST)
    state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    before = json.dumps(state, ensure_ascii=False, sort_keys=True)
    for part, fn in (('baba', lambda: baba(now, a.dry)), ('win5', lambda: win5(now, state, a.dry)),
                     ('overseas', lambda: overseas(now, state, a.dry))):
        if part not in a.parts:
            continue
        try:
            fn()
        except Exception as e:
            print(f'{part}: 失敗 {e}')
    if not a.dry and json.dumps(state, ensure_ascii=False, sort_keys=True) != before:
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')


if __name__ == '__main__':
    main()
