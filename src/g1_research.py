"""
G1予想の「調べた材料」をまとめる(g1_auto.py から呼ぶ)。2026-10-24 作り直し。

印の付け方:
  - 土台は市場(オッズ)。国内の点検(2023・2025年 3,930R)で、BOTを混ぜても市場だけより当たらなかった
    (条件付きロジットで BOT の重み b≈0.01〜0.06、対数尤度の差 −0.0004/R)。
    なので「勝つ見込み」は、オッズ(大穴を少し割り引く α=1.04)に BOT をほんの少し(0.05)だけ混ぜたもの。
  - 調教・距離実績・血統・前走・騎手・間隔・馬体重は、点数にせず「材料(✓よい / ✗気になる)」として並べる。
    重みを検証していないので、印は動かさない(勝手な重みで◎を穴馬にしない = 秋華賞の反省)。
  - 「穴の注目」は、6番手以下・10倍以上で、よい材料が気になる材料より2つ以上多く、BOTも市場より高く見ている馬(最大1頭)。
"""
import math
import re

MARKS = ['◎', '○', '▲', '△', '△']
ALPHA, BETA = 1.04, 0.05


def _int(s):
    m = re.match(r'\s*(\d+)', str(s))
    return int(m.group(1)) if m else None


def _short(race):
    r = re.sub(r'^第\d+回', '', str(race))
    return r.replace('(GIII)', '(G3)').replace('(GII)', '(G2)').replace('(GI)', '(G1)')


def probs(horses):
    """市場(α補正)× BOT^0.05 → 勝つ見込み。オッズが無い時はBOTだけ"""
    pm = {h['num']: (1 / h['odds']) ** ALPHA for h in horses if h.get('odds') and h['odds'] > 1}
    tm = sum(pm.values())
    out = {}
    for h in horses:
        b = max(h.get('p_model') or 1e-4, 1e-4)
        out[h['num']] = math.log(pm[h['num']] / tm) + BETA * math.log(b) if h['num'] in pm and tm else (
            math.log(b) if not pm else None)
    ok = {k: v for k, v in out.items() if v is not None}
    z = sum(math.exp(v) for v in ok.values()) or 1
    return {k: math.exp(v) / z for k, v in ok.items()}


def facts(h, dist, surface):
    """(よい材料, 気になる材料, ふつうのメモ)"""
    good, bad, memo = [], [], []
    runs = [r for r in h.get('runs', []) if _int(r.get('pos'))]
    if runs:
        r = runs[0]
        pos, pop = _int(r['pos']), _int(r.get('pop'))
        s = f"前走 {_short(r['race'])} {pos}着" + (f'({pop}番人気)' if pop else '')
        g1 = '(GI)' in r['race']
        (good if pos <= 3 or (g1 and pos <= 5) else bad if pos >= 10 else memo).append(s)
    else:
        memo.append('JRAの出走記録なし')
    # 距離実績(同じ芝/ダートで)
    if dist >= 2400:
        lo, hi, lab = 2200, 9999, '2200m以上'
    else:
        lo, hi, lab = dist - 200, dist + 200, f'{dist - 200}〜{dist + 200}m'
    same = [r for r in runs if r.get('surface', '')[:1] == surface[:1] and lo <= (_int(r.get('distance')) or 0) <= hi]
    if same:
        top3 = sum(_int(r['pos']) <= 3 for r in same)
        wins = sum(_int(r['pos']) == 1 for r in same)
        s = f'{lab}は{len(same)}戦{wins}勝(3着内{top3}回)'
        (good if top3 else bad).append(s)
    elif runs:
        memo.append(f'{lab}は初めて')
    # 調教
    ev = str((h.get('training') or {}).get('eval', '')).strip()
    if ev[:1] == 'A':
        good.append('調教A')
    elif ev[:1] in ('C', 'D'):
        bad.append(f'調教{ev[:1]}')
    # 血統
    if h.get('sire'):
        if h.get('stay') and dist >= 1800:
            good.append(f"父{h['sire']}(スタミナ型)")
        elif h.get('speed') and dist >= 2000:
            bad.append(f"父{h['sire']}(スピード型)")
    # 騎手
    j = h.get('jockey_top3')
    if j is not None and j >= 0.35:
        good.append(f"騎手{h['jockey']}(3着内{j * 100:.0f}%)")
    if h.get('jockey_change') == 1.0:
        memo.append('乗り替わり')
    # 間隔
    d = h.get('days')
    if d is not None and d >= 120:
        bad.append(f'休み明け({int(d)}日ぶり)')
    # 馬体重(当日)
    m = re.search(r'\(([+-]\d+)\)', str(h.get('horse_weight', '')))
    if m and abs(int(m.group(1))) >= 10:
        bad.append(f'馬体重{m.group(1)}kg')
    return good, bad, memo


def build(data):
    horses = [h for h in data['horses'] if h.get('name')]
    dist = _int(data.get('distance')) or 0
    surface = str(data.get('surface', ''))
    p = probs(horses)
    order = sorted(horses, key=lambda h: -p.get(h['num'], 0))
    pops = {h['num']: i + 1 for i, h in enumerate(sorted([h for h in horses if h.get('odds')], key=lambda h: h['odds']))}
    rows = []
    for i, h in enumerate(order):
        g, b, m = facts(h, dist, surface)
        rows.append({'mark': MARKS[i] if i < len(MARKS) else '', 'num': h['num'], 'name': h['name'],
                     'jockey': h['jockey'], 'odds': h.get('odds'), 'pop': pops.get(h['num']),
                     'p': round(p.get(h['num'], 0), 4), 'p_mkt': h.get('p_mkt'), 'p_model': h.get('p_model'),
                     'good': g, 'bad': b, 'memo': m})
    # 穴の注目
    ana = [r for r in rows[5:] if (r['odds'] or 0) >= 10 and len(r['good']) - len(r['bad']) >= 2
           and (r['p_model'] or 0) > (r['p_mkt'] or 1)]
    ana.sort(key=lambda r: (-(len(r['good']) - len(r['bad'])), -r['p']))
    if ana:
        ana[0]['mark'] = '☆'
    caution = []
    only_bot = [r for r in rows if r['p_mkt'] is not None and r['p_model'] is not None
                and r['p_mkt'] < 0.03 and r['p_model'] >= 0.05 and r['p_model'] >= 4 * r['p_mkt']]
    for r in sorted(only_bot, key=lambda r: -r['p_model'])[:2]:
        caution.append(f"{r['name']}({r['odds']:.1f}倍)はBOTだけが高く見ている大穴(BOT{r['p_model'] * 100:.0f}%・市場{r['p_mkt'] * 100:.1f}%)。"
                       'BOTは市場より当たる証拠がないので、印には入れていません。')
    return rows, caution


def fmt_odds(r):
    return f"{r['odds']:.1f}倍({r['pop']}番人気)" if r.get('odds') else 'オッズ未発表'


def lines_for_site(rows):
    """サイト用の文字ブロック: (印と見込み, 調べた材料, BOTの見方)"""
    marked = [r for r in rows if r['mark']]
    a = [f"{r['mark']} {r['num']:>2} {r['name']}  {fmt_odds(r)}  勝つ見込み{r['p'] * 100:.0f}%" for r in marked]
    b = []
    for r in marked:
        b.append(f"{r['mark']} {r['name']}")
        b += [f'   ✓ {x}' for x in r['good']] + [f'   ✗ {x}' for x in r['bad']] + [f'   ・{x}' for x in r['memo']]
    c = [f"{r['num']:>2} {r['name']}  BOT{(r['p_model'] or 0) * 100:.0f}% / 市場{(r['p_mkt'] or 0) * 100:.0f}%"
         + ('  ← BOTの方が高い' if (r['p_model'] or 0) > 1.5 * (r['p_mkt'] or 1) else '')
         for r in sorted(rows, key=lambda r: -(r['p_model'] or 0))[:6]]
    return a, b, c


def discord_text(header, rows, caution, ten, page, trio=None):
    L = ['🏆 **G1予想**(調べた材料つき・見るだけ)']
    if header:
        L.append('**' + header[0].strip('【】') + '**  ' + (header[1] if len(header) > 1 else ''))
    for r in [r for r in rows if r['mark']]:
        L.append(f"{r['mark']} **{r['num']} {r['name']}** {fmt_odds(r)} 見込み{r['p'] * 100:.0f}%")
        if r['good']:
            L.append('　✓ ' + ' / '.join(r['good'][:3]))
        if r['bad']:
            L.append('　✗ ' + ' / '.join(r['bad'][:2]))
    pace = [t for t in ten if 'ペース' in t]
    if pace:
        L.append('🏇 ' + pace[0].strip())
    L += [f'⚠ {c}' for c in caution]
    if trio:
        L.append('🎯 **三連複 ◎1頭軸流し**(合成オッズ5倍以上・遊び用)
```
' + '
'.join(trio) + '
```')
    L.append('印はオッズ(みんなの評価)が土台です。過去の点検で、BOTはオッズより当たる証拠がないため。'
             '✓✗は調べた材料で、印は動かしていません。')
    L.append(f'🌐 くわしくはサイトで: {page}')
    s = '\n'.join(L)
    return s if len(s) <= 1950 else s[:1940] + '…'
