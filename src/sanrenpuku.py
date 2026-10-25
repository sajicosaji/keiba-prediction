"""
三連複 1頭軸流し(マルチ)の買い目。2026-10-25 ユーザーの指定で追加: 「合成オッズ5倍以上を閾値」。
  ※ 三連複は着順を問わないので、1頭軸の「マルチ」と「流し」は同じ買い目になる。
  - 軸: ◎(呼ぶ側が渡す。無ければ単勝1番人気)
  - 各組の確率: 単勝オッズ → 勝率(大穴補正 α=1.04) → 2・3着補正つき Harville(γ2=0.80, γ3=0.67。α補正後の勝率に掛ける値)
  - 選び方: 当たりやすい組から順に足していき、合成オッズ(1/Σ(1/オッズ))が5倍を割る組は飛ばす。最大10点。相手は上位8頭まで
  - 金額: 合計を 1/オッズ に比例して配る(100円単位)。どの組が当たっても払戻がほぼ同じ(=合計×合成オッズ)
  - 期待回収 = 合成オッズ × 当たる確率。※ 過去の点検(2023〜2025年)では三連複はどの帯も100%を超えなかった。見るだけ・遊び用
使い方: python src/sanrenpuku.py 202608040911 [--axis 8] [--budget 1000]
"""
import argparse
import time
from itertools import combinations

import requests

ALPHA, G2, G3 = 1.04, 0.80, 0.67
SYNTH_MIN, MAX_POINTS, TOP_PARTNERS = 5.0, 10, 8
API = 'https://race.netkeiba.com/api/api_get_jra_odds.html'
HDR = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://race.netkeiba.com/'}


def fetch(race_id, typ):
    r = requests.get(API, params={'race_id': str(race_id), 'type': typ, 'action': 'update'}, headers=HDR, timeout=15)
    d = r.json().get('data') or {}
    odds = d.get('odds') if isinstance(d, dict) else None
    if not isinstance(odds, dict):  # 発売前は空(文字列)で返ってくる
        return {}, ''
    out = {}
    for k, v in (odds.get(typ) or {}).items():
        try:
            o = float(str(v[0]).replace(',', ''))
            if o > 0:
                out[k] = o
        except (ValueError, TypeError, IndexError):
            pass
    return out, d.get('official_datetime', '')


def trio_probs(win):
    """{馬番: 単勝オッズ} → {(a,b,c)昇順: 三連複の確率}"""
    q = {h: (1 / o) ** ALPHA for h, o in win.items() if o > 1}
    s = sum(q.values())
    p = {h: v / s for h, v in q.items()}
    w2 = {h: v ** G2 for h, v in p.items()}
    w3 = {h: v ** G3 for h, v in p.items()}
    S2, S3 = sum(w2.values()), sum(w3.values())
    out = {}
    for i in p:
        for j in p:
            if j == i:
                continue
            pij = p[i] * w2[j] / (S2 - w2[i])
            for k in p:
                if k in (i, j):
                    continue
                key = tuple(sorted((i, j, k)))
                out[key] = out.get(key, 0) + pij * w3[k] / (S3 - w3[i] - w3[j])
    return out, p


def pick(win, trio_odds, axis=None, budget=1000):
    """買い目を選ぶ。戻り値 dict(軸, 点, 合成オッズ, 当たる確率, 期待回収) / 買えない時は reason"""
    probs, p = trio_probs(win)
    if not probs:
        return {'reason': '単勝オッズが無い'}
    axis = int(axis) if axis else max(p, key=p.get)  # G1の行の馬番は文字列のことがある
    partners = [h for h in sorted(p, key=lambda h: -p[h]) if h != axis][:TOP_PARTNERS]
    cands = []
    for a, b in combinations(partners, 2):
        key = tuple(sorted((axis, a, b)))
        o = trio_odds.get(''.join(f'{x:02d}' for x in key))
        if o:
            cands.append((probs.get(key, 0), o, key))
    cands.sort(key=lambda x: -x[0])
    chosen, inv = [], 0.0
    for q, o, key in cands:
        if len(chosen) >= MAX_POINTS:
            break
        if 1 / (inv + 1 / o) < SYNTH_MIN:
            continue
        chosen.append((q, o, key))
        inv += 1 / o
    if not chosen:
        return {'axis': axis, 'reason': f'合成オッズ{SYNTH_MIN:.0f}倍以上になる組がない'}
    synth = 1 / inv
    hit = sum(q for q, _, _ in chosen)
    # 金額: 1/オッズに比例(100円単位・最低100円)
    raw = [budget * synth / o for _, o, _ in chosen]  # = 合計 × (1/o) / Σ(1/o)
    yen = [max(100, int(round(r / 100)) * 100) for r in raw]
    pays = [y * o for y, (_, o, _) in zip(yen, chosen)]
    return {'axis': axis, 'p_axis': p[axis], 'synth': synth, 'hit': hit, 'exp': synth * hit,
            'points': [{'combo': list(k), 'odds': o, 'p': q, 'yen': y, 'pay': round(pay)}
                       for (q, o, k), y, pay in zip(chosen, yen, pays)],
            'total': sum(yen), 'pay_min': round(min(pays)), 'pay_max': round(max(pays))}


def lines(res, names=None):
    names = {int(k): v for k, v in (names or {}).items()}
    nm = lambda h: f"{h}{names.get(h, '')[:6]}"  # noqa: E731
    if res.get('reason'):
        return [f"見送り({res['reason']})"]
    a = res['axis']
    L = [f"軸 ◎{nm(a)} - 相手 {'・'.join(sorted({str(h) for pt in res['points'] for h in pt['combo'] if h != a}, key=int))}"
         f"  {len(res['points'])}点 合計{res['total']:,}円",
         f"合成オッズ {res['synth']:.1f}倍 / 当たる見込み {res['hit'] * 100:.0f}% / 期待回収 {res['exp'] * 100:.0f}%",
         f"当たった時の払戻 {res['pay_min']:,}〜{res['pay_max']:,}円"]
    for pt in res['points']:
        L.append(f"  {'-'.join(str(h) for h in pt['combo']):>8}  {pt['odds']:>6.1f}倍  {pt['yen']:>5,}円  (見込み{pt['p'] * 100:.1f}%)")
    return L


def run(race_id, axis=None, budget=1000):
    win, t1 = fetch(race_id, '1')
    time.sleep(2)
    trio, t7 = fetch(race_id, '7')
    if not win or not trio:
        return {'reason': 'オッズ未発表'}, t7
    return pick({int(k): v for k, v in win.items()}, trio, axis, budget), t7


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('race_id')
    ap.add_argument('--axis', type=int)
    ap.add_argument('--budget', type=int, default=1000)
    a = ap.parse_args()
    res, t = run(a.race_id, a.axis, a.budget)
    print(f'三連複オッズ {t}')
    print('\n'.join(lines(res)))
