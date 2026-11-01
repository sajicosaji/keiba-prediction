"""
国内の「投票用」買い目を、言い切りの形でDiscordへ送る係。2026-11-01 ユーザーの要望で追加:
  「いちいちサイト見て判断するの疲れる」「国内競馬もそう。断定して買い目の指定して。穴でもいいから」
  - 対象: その日のJRAの重賞(G1・G2・G3。g1_auto.todays_g1 の一覧)
  - 発走の28〜12分前に1回だけ、その時のオッズで作る(day_runner のループは約13分おきなので、この幅で必ず1回入る)
  - 買い目:
      三連複 ◎1頭軸流し(合成オッズ5倍以上) … sanrenpuku.py。合計 trio_yen 円
      ☆穴の単勝 … g1_auto が昼に作った予想の ☆(人気薄で材料がそろい、BOTも市場より高く見ている馬)。今も10倍以上なら ana_yen 円
  - 国内は検証でどの選び方も回収100%を超えていない(期待回収は約8割)。「遊び枠」と1行だけ書く
  - 出した買い目は data/domestic/bets.csv に残す(答え合わせ用)
金額は data/domestic/config.json で変えられる({"trio_yen": 1000, "ana_yen": 500})
使い方: python src/domestic_picks.py [--race-id 202608050911 --force] [--dry]
"""
import argparse
import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SRC = Path(__file__).parent
sys.path.insert(0, str(SRC))
import g1_auto  # noqa: E402
import g1_post  # noqa: E402
import sanrenpuku  # noqa: E402

JST = ZoneInfo('Asia/Tokyo')
OUT = SRC.parent / 'data' / 'domestic'
SENT = OUT / 'sent.json'
BETS = OUT / 'bets.csv'
CONFIG = {'trio_yen': 1000, 'ana_yen': 500}
WINDOW = (12, 28)  # 発走の何分前から何分前まで


def config():
    c = dict(CONFIG)
    f = OUT / 'config.json'
    if f.exists():
        c.update(json.loads(f.read_text(encoding='utf-8')))
    return c


def g1_entry(rid):
    """g1_auto が昼に作った予想(印・☆・馬名)。無ければ None"""
    f = SRC.parent / 'data' / 'g1' / 'site.json'
    if not f.exists():
        return None
    return next((x for x in json.loads(f.read_text(encoding='utf-8')) if x['race_id'] == rid), None)


def make(rid, grade, post_hm, cfg):
    win, _ = sanrenpuku.fetch(rid, '1')
    win = {int(k): v for k, v in win.items()}
    e = g1_entry(rid)
    names = {int(r['num']): r['name'] for r in (e or {}).get('rows', [])}
    title = (e or {}).get('header', [rid])[0].strip('【】') if e else rid
    trio, t = sanrenpuku.run(rid, None, cfg['trio_yen'])
    lines_buy, bets = [], []
    if not trio.get('reason'):
        pts = ' / '.join(f"{'-'.join(str(h) for h in p['combo'])} {p['yen']:,}円" for p in trio['points'])
        lines_buy.append(f"三連複 {pts}(合計{trio['total']:,}円)")
        bets += [('三連複', '-'.join(str(h) for h in p['combo']), p['yen'], p['odds']) for p in trio['points']]
    ana = next((r for r in (e or {}).get('rows', []) if r.get('mark') == '☆'), None)
    if ana and win.get(int(ana['num']), 0) >= 10:
        n = int(ana['num'])
        lines_buy.append(f"単勝 ☆{n}番 {ana['name']} {cfg['ana_yen']:,}円({win[n]:.1f}倍)")
        bets.append(('単勝', str(n), cfg['ana_yen'], win[n]))
    L = [f"🎯 **国内 投票用**【{title}】{grade} 発走 {post_hm}"]
    if lines_buy:
        L.append('👉 **これを買う:**\n' + '\n'.join(f'**{x}**' for x in lines_buy))
    else:
        L.append(f"👉 **今回は買わない**({trio.get('reason', '買い目なし')})")
    if not trio.get('reason'):
        a = trio['axis']
        L.append(f"軸 ◎{a}番 {names.get(a, '')}(1番人気) / 当たる見込み{trio['hit'] * 100:.0f}%・当たれば約{trio['pay_min']:,}〜{trio['pay_max']:,}円")
    if ana and bets and bets[-1][0] == '単勝':
        why = ' / '.join(ana.get('good', [])[:3])
        L.append(f"☆穴の理由: {why}" if why else '☆穴: 人気薄だが材料がそろい、BOTも市場より高く見ている')
    L.append('※国内は遊び枠です(検証ではどの買い方も期待回収は約8割)。' + (f'オッズは{t[11:16]}時点。' if t else ''))
    return '\n'.join(L), bets, title


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--race-id')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()
    now = datetime.now(JST)
    cfg = config()
    sent = json.loads(SENT.read_text(encoding='utf-8')) if SENT.exists() else {}
    if a.race_id:
        todo = [{'race_id': a.race_id, 'post': '', 'grade': '重賞'}]
    else:
        try:
            todo = g1_auto.todays_g1(now)
        except Exception as e:
            print(f'重賞の確認に失敗: {e}')
            return
    for r in todo:
        rid = r['race_id']
        if rid in sent and not a.force:
            continue
        if r['post'] and not a.force:
            post = datetime.strptime(now.strftime('%Y-%m-%d ') + r['post'], '%Y-%m-%d %H:%M').replace(tzinfo=JST)
            if not (post - timedelta(minutes=WINDOW[1]) <= now <= post - timedelta(minutes=WINDOW[0])):
                continue
        msg, bets, title = make(rid, r.get('grade', ''), r.get('post', ''), cfg)
        print(msg)
        if a.dry:
            continue
        wh = g1_post.get_webhook()
        if wh:
            g1_post.post(wh, msg)
        OUT.mkdir(parents=True, exist_ok=True)
        new = not BETS.exists()
        with open(BETS, 'a', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            if new:
                w.writerow(['noticed_at', 'race_id', 'race', 'kind', 'combo', 'yen', 'odds_at_notice'])
            for k, c, y, o in bets:
                w.writerow([now.strftime('%Y-%m-%d %H:%M'), rid, title, k, c, y, o])
        sent[rid] = now.strftime('%Y-%m-%d %H:%M')
        SENT.write_text(json.dumps(sent, ensure_ascii=False, indent=1), encoding='utf-8')


if __name__ == '__main__':
    main()
