"""
海外発売レースで「発走の何分前に買うのが良いか」を、見張りの記録(data/overseas/odds.csv)から出す。2026-11-03。
  - 発走の T 分前の時点で記録されていた買い目(その時のJRAオッズ・現地オッズで判定したもの)を、T ごとに集める
  - 払戻は確定のJRA単勝(パリミュチュエルなので、いつ買っても払戻は確定オッズで決まる)
  - あわせて、T 分前の買い目が「締切の時点(最後の記録)の買い目」とどれだけ一致するか、買い目の確定オッズ ÷ T 分前のオッズ を出す
人が買う時は、締切(発走2分前)の3分以上前に知らせが要る → T ≥ 6 が目安。自動で買うなら T = 3 前後。
使い方: python tools/overseas_timing.py
"""
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OV = BASE / 'data' / 'overseas'
TS = [30, 20, 15, 10, 8, 6, 5, 4, 3]


def main():
    rows = list(csv.DictReader(open(OV / 'odds.csv', encoding='utf-8')))
    res = {r['slug']: r for r in csv.DictReader(open(OV / 'results.csv', encoding='utf-8'))} if (OV / 'results.csv').exists() else {}
    by = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by[r['slug']][r['logged_at']].append(r)
    stat = {t: {'bets': 0, 'pay': 0.0, 'races': 0, 'same': 0, 'ratio': []} for t in TS}
    used = []
    for slug, snaps in by.items():
        f = OV / 'races' / f'{slug}.json'
        if not f.exists():
            continue
        race = json.loads(f.read_text(encoding='utf-8'))
        if not race.get('post') or not race.get('winner'):
            continue
        post = datetime.strptime(race['post'], '%Y-%m-%d %H:%M')
        after = sorted(t for t in snaps if datetime.strptime(t, '%Y-%m-%d %H:%M') > post)
        # 発走前の記録のうち、JRAのオッズが入っているものだけ(2026年BCは発走前にJRAのオッズが読めていなかった)
        before = sorted(t for t in snaps if datetime.strptime(t, '%Y-%m-%d %H:%M') <= post and any(r['jra_odds'] for r in snaps[t]))
        if not after or not before:
            continue
        final = {r['num']: float(r['jra_odds']) for r in snaps[after[-1]] if r['jra_odds']}
        winners = {r['num'] for r in snaps[after[-1]] if r['name'].replace(' ', '') == race['winner'].replace(' ', '')}
        if not final or not winners:
            continue
        close = {r['num'] for r in snaps[before[-1]] if r['pick'] in ('1', 'True')}
        used.append(f"{slug}(記録{len(before)}回・最後は発走{(post - datetime.strptime(before[-1], '%Y-%m-%d %H:%M')).total_seconds() / 60:.0f}分前)")
        for t in TS:
            cand = [s for s in before if (post - datetime.strptime(s, '%Y-%m-%d %H:%M')).total_seconds() / 60 >= t]
            if not cand:
                continue
            picks = [r for r in snaps[cand[-1]] if r['pick'] in ('1', 'True')]
            st = stat[t]
            st['races'] += 1
            st['bets'] += len(picks)
            st['pay'] += sum(final[r['num']] for r in picks if r['num'] in winners and r['num'] in final)
            st['same'] += {r['num'] for r in picks} == close
            st['ratio'] += [final[r['num']] / float(r['jra_odds']) for r in picks if r['num'] in final and r['jra_odds']]
    print('使った記録:', ' / '.join(used) or 'なし')
    print(f"{'発走前':>6} {'レース':>4} {'点数':>4} {'回収':>6} {'締切時と同じ買い目':>10} {'確定÷その時のオッズ(中央値)':>16}")
    for t in TS:
        s = stat[t]
        if not s['races']:
            continue
        rr = sorted(s['ratio'])
        med = f'{rr[len(rr) // 2]:.2f}' if rr else '--'
        roi = f"{s['pay'] / s['bets'] * 100:.0f}%" if s['bets'] else '--'
        print(f"{t:>4}分前 {s['races']:>4} {s['bets']:>4} {roi:>6} {s['same']:>6}/{s['races']:<4} {med:>12}")


if __name__ == '__main__':
    main()
