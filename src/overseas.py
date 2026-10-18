"""
海外発売チーム: JRAが馬券を売る海外レースの「日程 → 1週間前からの情報集め → 当日の買い目 → 答え合わせ」。
情報はすべて JRA 公式ページから読む(robots.txt は全許可。3秒以上あけて読む)。自動購入はしない(買うのは本人)。

買い目のルール(2016年以降の検証で決めたもの。変えない):
  - 日本馬(調教師が JPN)は買わない … JRAでは日本馬が買われすぎ(全部買うと回収45%)
  - 現地の参考オッズから出した勝率 × JRA単勝オッズ = 期待値 が 1.2以上(=現地より2割以上割安)
  - JRA単勝が20倍以下(大穴の割安は当てにならない)
  検証: 香港51R 178%、香港以外72R 188%。ただし大きい当たり2本を除くと80%で、未確定。→ 少額で実戦検証する。
  勝率の出し方: 参考オッズが公営(PMU・HKJCなどのトート)なら (1/オッズ)^1.03 を合計1に、
               ブックメーカーなら Shin 法で控除を除く。日本馬も含めた全馬で計算する。

データ: data/overseas/
  schedule.json … 指定競走(年116レース)のうち今後のものと、発売が決まったか
  races/<slug>.json … 発売レースごとの最新の状態(出走馬・JRAオッズ・現地オッズ・期待値・買い目)
  odds.csv … 取るたびに全馬のJRAオッズと現地オッズを追記(あとで検証する材料)
  bets.csv / results.csv … 最終版で出した買い目と答え合わせ
  site.json … サイト(カミロボ競馬場 /overseas/)が GitHub から直接読む
  state.json … 通知済みの印

使い方:
  python src/overseas.py daily        日程の更新、発売決定・1週間前からの毎日のまとめ、答え合わせ
  python src/overseas.py watch        daily ＋ 当日なら締切まで約8分おきに見張る(overseas.yml から)
  python src/overseas.py show <link>  1レースを読んで表示だけする(試運転)
"""
import argparse
import csv
import html
import json
import math
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

DATA = Path(__file__).parent.parent / 'data'
OUT = DATA / 'overseas'
JST = ZoneInfo('Asia/Tokyo')
BASE = 'https://www.jra.go.jp'
UA = {'User-Agent': 'Mozilla/5.0 (keiba-bot; record only)'}
SITE_PAGE = 'https://kamirobo-keiba.keiba-bot-site.workers.dev/overseas/'
EV_MIN, ODDS_MAX, TOTE_A = 1.2, 20.0, 1.03
CONFIG = {'stake_per_pick': 1000, 'final_notice_min': 15}  # data/overseas/config.json で上書きできる
WAIT = 3.0
_last = [0.0]


# ---------------------------------------------------------------- 取得
def get(path):
    gap = time.time() - _last[0]
    if gap < WAIT:
        time.sleep(WAIT - gap)
    r = requests.get(path if path.startswith('http') else BASE + path, headers=UA, timeout=25)
    _last[0] = time.time()
    r.raise_for_status()
    return r.content.decode('cp932', errors='replace')


def text(s):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', s or ''))).strip()


def num(s):
    try:
        return float(str(s).replace(',', '').strip())
    except (TypeError, ValueError):
        return None


def md_date(s, now):
    """'10月31日（土曜）' → date(年は今年。今日より4か月以上前なら来年)"""
    m = re.search(r'(\d{1,2})月(\d{1,2})日', s or '')
    if not m:
        return None
    d = datetime(now.year, int(m.group(1)), int(m.group(2)), tzinfo=JST)
    if d < now - timedelta(days=120):
        d = d.replace(year=now.year + 1)
    return d


def jst_time(s, now):
    """'10月4日（日曜）23時05分' → datetime(JST)"""
    d = md_date(s, now)
    m = re.search(r'(\d{1,2})時(\d{2})分', s or '')
    if not d or not m:
        return None
    return d.replace(hour=int(m.group(1)), minute=int(m.group(2)))


def designated(now):
    """指定競走一覧(年116レース)→ 今日以降のもの"""
    h = get('/keiba/overseas/racelist/release.html')
    out = []
    for tr in re.findall(r'<tr>(.*?)</tr>', h, re.S):
        tds = [text(x) for x in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', tr, re.S)]
        if len(tds) < 6 or not tds[0].isdigit():
            continue
        m = re.search(r'(\d{1,2})月(\d{1,2})日', tds[5])
        if not m:
            continue
        # この一覧は今年の日程(来年分は年明けにJRAが出し直す)。終わったものは除く
        d = datetime(now.year, int(m.group(1)), int(m.group(2)), tzinfo=JST)
        if d < now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1):
            continue
        name = tds[1]
        en = re.search(r'[A-Z][A-Za-z0-9\.\'&ⅡⅢ\- ]+$', name)
        out.append({'date': d.strftime('%Y-%m-%d'), 'name': name[:en.start()].strip() if en else name,
                    'en': en.group(0).strip() if en else '', 'course': tds[2], 'country': tds[3], 'track': tds[4]})
    return sorted(out, key=lambda x: x['date'])


def decided(now):
    """発売レース一覧(今年)。行がある = JRAが発売を決めたレース"""
    h = get('/keiba/overseas/racelist/')
    out = []
    for tr in re.findall(r'<tr>(.*?)</tr>', h, re.S):
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)
        if len(tds) < 4 or '年' not in tds[0]:
            continue
        lm = re.search(r'href="([^"]+)"', tds[2])
        fm = re.search(r'alt="([^"]+)"', tds[1])
        dm = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', text(tds[0]))
        if not dm or not lm:
            continue
        link = lm.group(1)
        link = link[:-len('index.html')] if link.endswith('index.html') else link
        link = link if link.endswith('/') else link + '/'
        out.append({'date': f'{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}', 'race': text(tds[2]),
                    'country': fm.group(1).replace('国旗', '') if fm else '', 'track': text(tds[1]),
                    'distance': text(tds[3]), 'winner': text(tds[4]) if len(tds) > 4 else '', 'link': link,
                    'slug': link.strip('/').split('/race/')[-1].replace('/', '_')})
    return out


def get_opt(path):
    """無いページ(404など)は空文字で返す"""
    try:
        return get(path)
    except requests.RequestException as e:
        print(f'  {path} 読めず: {e}')
        return ''


def race_page(link, now):
    """レースのページ(トップ・出馬表・海外参考オッズ)を読む。
    レース後しばらくするとトップは「回顧」ページへ自動で切り替わるので、出馬表と参考オッズはトップに頼らず直接読む"""
    top = get_opt(link)
    sale = post = None
    sm = re.search(r'発売開始時刻.*?日本時間\s*<strong>(.*?)</strong>.*?ネット馬券購入：\s*<strong>(.*?)</strong>', top, re.S)
    if sm:
        sale = jst_time(text(sm.group(1)) + text(sm.group(2)), now)
    pm = re.search(r'発走予定時刻.*?日本時間\s*<strong>(.*?)</strong>', top, re.S)
    if pm:
        post = jst_time(text(pm.group(1)), now)
    r = {'link': link, 'sale_start': sale.strftime('%Y-%m-%d %H:%M') if sale else '',
         'post': post.strftime('%Y-%m-%d %H:%M') if post else '', 'entries': [], 'scratched': [], 'ref': {}, 'ref_time': '',
         'ref_source': '', 'jra_time': ''}
    h = get_opt(link + 'syutsuba.html')
    if h:
        if not post:  # トップが回顧ページになった後は、出馬表の見出しから(日本時間の発走時刻)
            dm = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', h)
            tm = re.search(r'発走時刻：\s*(?:<[^>]+>\s*)*(\d{1,2})時(\d{2})分', h)
            if dm and tm:
                r['post'] = f'{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d} {int(tm.group(1)):02d}:{tm.group(2)}'
        body = h[h.find('<tbody'):]
        for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', body, re.S):
            nm = re.search(r'<td class="num[^"]*">\s*(\d+)', tr)   # 馬番の後ろにブリンカー等のマークが付くことがある
            nmn = re.search(r'<div class="txt">(.*?)</div>', tr, re.S)
            if not nmn:
                continue
            if not nm:  # 馬番の代わりに「取消」「除外」
                if re.search(r'<td class="num[^"]*">', tr):
                    r['scratched'].append(text(nmn.group(1)))
                continue
            om = re.search(r'<span class="num">(.*?)</span>', tr, re.S)
            dv = re.search(r'<p class="trainer">.*?<span class="division">\((.*?)\)</span>', tr, re.S)
            odds = num(text(om.group(1))) if om else None
            r['entries'].append({'num': int(nm.group(1)), 'name': text(nmn.group(1)), 'jra_odds': odds,
                                 'trainer_country': dv.group(1) if dv else '',
                                 'jpn': bool(dv and dv.group(1) == 'JPN')})
        um = re.search(r'(\d{1,2})時(\d{2})分\s*現在', text(h))
        r['jra_time'] = (now.strftime('%Y-%m-%d ') + f'{int(um.group(1)):02d}:{um.group(2)}') if um else now.strftime('%Y-%m-%d %H:%M')
    h = get_opt(link + 'morning.html')
    if h:
        blocks = re.split(r'<div class="update_area">', h)[1:]
        best = None
        for b in blocks:
            tm = re.search(r'<p class="update_time">(.*?)</p>', b, re.S)
            t = jst_time(text(tm.group(1)), now) if tm else None
            cm = re.search(r'<div class="caution[^"]*">(.*?)</div>', b, re.S)
            odds = {}
            body = b[b.find('<tbody'):b.find('</tbody>')]
            for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', body, re.S):
                n = re.search(r'<th[^>]*>\s*(\d+)\s*</th>', tr)
                hm = re.search(r'<td class="horse">(.*?)<span>(.*?)</span>', tr, re.S)
                om = re.findall(r'<td>(.*?)</td>', tr, re.S)
                if n and om:
                    o = num(text(om[-1]))
                    if o and o >= 1:  # 香港の断然人気は1.0倍もある
                        odds[int(n.group(1))] = {'odds': o, 'en': text(hm.group(2)) if hm else ''}
            if odds and (best is None or (t and (best[0] is None or t > best[0]))):
                best = (t, odds, text(cm.group(1)) if cm else '')
        if best:
            r['ref'] = best[1]
            r['ref_time'] = best[0].strftime('%Y-%m-%d %H:%M') if best[0] else ''
            r['ref_source'] = best[2]
    return r


# ---------------------------------------------------------------- 計算
def shin(odds):
    """ブックメーカーのオッズから控除を除いた勝率(Shin法)"""
    pi = {k: 1 / v for k, v in odds.items()}
    s = sum(pi.values())
    if s <= 1:
        return {k: v / s for k, v in pi.items()}
    lo, hi = 0.0, 0.5
    for _ in range(60):
        z = (lo + hi) / 2
        p = {k: (math.sqrt(z * z + 4 * (1 - z) * v * v / s) - z) / (2 * (1 - z)) for k, v in pi.items()}
        if sum(p.values()) > 1:
            lo = z
        else:
            hi = z
    t = sum(p.values())
    return {k: v / t for k, v in p.items()}


def tote(odds, a=TOTE_A):
    w = {k: (1 / v) ** a for k, v in odds.items()}
    s = sum(w.values())
    return {k: v / s for k, v in w.items()}


# ---------------------------------------------------------------- 現地の実オッズ(PMU)
# JRAの「海外参考オッズ」は国によってはモーニングライン(現地の予想担当が事前に付けた想定オッズ)で、
# 市場の値ではない。また香港・フランスでも発走の数時間前の値。検証は「現地の締切に近い実オッズ」で行ったので、
# 締切前の実オッズを PMU(フランス公営)の公開データから取る。PMUは凱旋門賞などの仏レースのほか、
# ブリーダーズカップ・香港国際競走など海外の大レースも売っている(香港は香港ジョッキークラブと同じ集計)。
# 馬番は現地の番号=JRAの馬番。英語名でも照合して確かめる。
PMU = 'https://online.turfinfo.api.pmu.fr/rest/client/1/programme'
UTC, PARIS = ZoneInfo('UTC'), ZoneInfo('Europe/Paris')
_pmu_prog = {}


def get_json(url):
    gap = time.time() - _last[0]
    if gap < WAIT:
        time.sleep(WAIT - gap)
    r = requests.get(url, headers=UA, timeout=25)
    _last[0] = time.time()
    if r.status_code != 200:
        return None
    return r.json()


def norm_en(s):
    # JRAは「NYSOS（USA）」のように全角かっこで国名を付けることがある
    return re.sub(r'[^A-Z0-9]', '', re.sub(r'[\(（].*?[\)）]', '', str(s).upper()))


def pmu_lookup(r):
    """JRAの発走時刻(±15分)と馬名で PMU のレースを探し、{馬番: {odds, en, out, pos}} を返す。見つからなければ {}"""
    if not r.get('post'):
        return {}
    post = datetime.strptime(r['post'], '%Y-%m-%d %H:%M').replace(tzinfo=JST)
    jra_en = {e['num']: norm_en(e.get('en') or r['ref'].get(e['num'], {}).get('en', '')) for e in r['entries']}
    best = None
    for d in sorted({post.astimezone(PARIS).date(), (post.astimezone(PARIS) - timedelta(days=1)).date()}):
        key = d.strftime('%d%m%Y')
        if key not in _pmu_prog:
            try:
                _pmu_prog[key] = get_json(f'{PMU}/{key}') or {}
            except Exception as e:
                print(f'PMU {key} 読めず: {e}')
                _pmu_prog[key] = {}
        for reu in (_pmu_prog[key].get('programme') or {}).get('reunions', []):
            for c in reu.get('courses', []):
                hd = c.get('heureDepart')
                if not hd:
                    continue
                gap = abs(datetime.fromtimestamp(hd / 1000, UTC) - post).total_seconds() / 60
                if gap > 15:
                    continue
                try:
                    ps = (get_json(f"{PMU}/{key}/R{reu['numOfficiel']}/C{c['numOrdre']}/participants") or {}).get('participants', [])
                except Exception as e:
                    print(f'PMU 出走馬 読めず: {e}')
                    continue
                by_num = {p.get('numPmu'): p for p in ps}
                same = sum(1 for n, en in jra_en.items() if en and n in by_num and norm_en(by_num[n].get('nom')) == en)
                named = sum(1 for en in jra_en.values() if en)
                # 英語名が分かる馬の7割以上が同じ番号で一致したら同じレース(英語名が無ければ頭数と時刻で判断)
                ok = (same >= 0.7 * named) if named else (abs(len(ps) - len(r['entries']) - len(r.get('scratched', []))) <= 1)
                if ok and (best is None or gap < best[0]):
                    best = (gap, ps, f"R{reu['numOfficiel']}C{c['numOrdre']} {c.get('libelle', '')[:40]}")
    if not best:
        return {}
    out = {}
    for p in best[1]:
        o = (p.get('dernierRapportDirect') or {}).get('rapport')
        out[p.get('numPmu')] = {'odds': o, 'en': p.get('nom', ''), 'out': p.get('statut') == 'NON_PARTANT',
                                'pos': p.get('ordreArrivee')}
    r['pmu_race'] = best[2]
    return out


def evaluate(r, pmu=None):
    """各馬の現地勝率・期待値と買い目。日本馬は買わない。
    現地の勝率は ① PMUの締切前の実オッズ ② JRAの参考オッズ(市場の値の時だけ) の順。どちらも無ければ買い目なし"""
    live = {e['num'] for e in r['entries'] if e['jra_odds']}
    pmu_odds = {k: v['odds'] for k, v in (pmu or {}).items() if v['odds'] and not v['out'] and (not live or k in live)}
    ml = bool(re.search(r'モーニングライン|Morning ?Line', r.get('ref_source', ''), re.I))
    if len(pmu_odds) >= 2:
        ref, is_tote = pmu_odds, True
        r['local'] = {'source': 'PMU(仏公営)の実オッズ ' + r.get('pmu_race', ''), 'time': datetime.now(JST).strftime('%Y-%m-%d %H:%M'), 'ok': True}
    elif r['ref'] and not ml:
        ref = {k: v['odds'] for k, v in r['ref'].items() if not live or k in live}
        is_tote = bool(re.search(r'PMU|HKJC|香港ジョッキークラブ|トート|パリミュチュエル|Tote', r.get('ref_source', '')))
        r['local'] = {'source': 'JRAの海外参考オッズ(' + re.sub(r'^下記は|から提供.*$', '', r.get('ref_source', ''))[:20] + ')',
                      'time': r.get('ref_time', ''), 'ok': True}
    else:
        ref, is_tote = {}, False
        r['local'] = {'source': 'モーニングライン(予想担当の想定オッズ)しか無い' if ml else '現地のオッズなし',
                      'time': r.get('ref_time', ''), 'ok': False}
    p = (tote(ref) if is_tote else shin(ref)) if len(ref) >= 2 else {}
    r['method'] = ('公営(トート)a=1.03' if is_tote else 'Shin法') if p else '判定できず'
    for e in r['entries']:
        e['en'] = r['ref'].get(e['num'], {}).get('en', '') or (pmu or {}).get(e['num'], {}).get('en', '')
        e['ref_odds'] = ref.get(e['num'])
        e['jra_ref_odds'] = r['ref'].get(e['num'], {}).get('odds')
        e['p'] = round(p[e['num']], 4) if e['num'] in p else None
        e['ev'] = round(e['p'] * e['jra_odds'], 3) if (e['p'] and e['jra_odds']) else None
        e['pick'] = bool(e['ev'] and not e['jpn'] and e['jra_odds'] <= ODDS_MAX and e['ev'] >= EV_MIN)
    r['picks'] = [e['num'] for e in sorted(r['entries'], key=lambda e: -(e['ev'] or 0)) if e['pick']]
    return r


# ---------------------------------------------------------------- 記録・通知
def load(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')


def append(path, header, rows):
    if not rows:
        return
    new = not path.exists()
    with open(path, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


def discord(msg, dry=False):
    print(msg)
    print('-' * 40)
    url = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
    if not url:
        f = DATA / 'discord_webhook.txt'
        url = f.read_text(encoding='utf-8-sig').strip() if f.exists() else ''
    if dry or not url:
        return
    for i in range(0, len(msg), 1900):
        try:
            requests.post(url, json={'content': msg[i:i + 1900]}, timeout=15)
        except Exception as e:
            print(f'Discord送信エラー: {e}')


def hm(s):
    return f'{int(s[5:7])}/{int(s[8:10])} {s[11:16]}' if s and len(s) >= 16 else '?'


def table(r, only=None, limit=8):
    rows = [e for e in r['entries'] if (only is None or e['num'] in only)]
    rows.sort(key=lambda e: -(e['p'] or 0))
    out = []
    for e in rows[:limit]:
        jra = f"{e['jra_odds']:.1f}" if e['jra_odds'] else '--'
        ref = f"{e['ref_odds']:.1f}" if e['ref_odds'] else '--'
        ev = f"{e['ev']:.2f}" if e['ev'] else '--'
        mark = '🇯🇵' if e['jpn'] else ('✅' if e['pick'] else '・')
        out.append(f"{mark}{e['num']:>2} {e['name'][:10]:<10} JRA{jra:>6}倍 現地{ref:>6}倍 期待値{ev}")
    return '\n'.join(out)


def snapshot(d, now, state):
    """発売レース1つを読んで記録し、状態を返す"""
    r = race_page(d['link'], now)
    r.update({k: d[k] for k in ('date', 'race', 'country', 'track', 'distance', 'winner', 'slug')})
    pmu = {}
    if r['post']:
        post = datetime.strptime(r['post'], '%Y-%m-%d %H:%M').replace(tzinfo=JST)
        if post - timedelta(hours=36) <= now <= post + timedelta(days=3):  # PMUの出走表は前日ごろに出る
            try:
                pmu = pmu_lookup(r)
            except Exception as e:
                print(f"{d['slug']} PMU失敗: {e}")
    evaluate(r, pmu)
    r['checked_at'] = now.strftime('%Y-%m-%d %H:%M')
    save(OUT / 'races' / f"{d['slug']}.json", r)
    append(OUT / 'odds.csv', ['logged_at', 'slug', 'num', 'name', 'jpn', 'jra_odds', 'local_odds', 'local_source',
                              'jra_ref_odds', 'jra_ref_time', 'p', 'ev', 'pick'],
           [[r['checked_at'], d['slug'], e['num'], e['name'], int(e['jpn']), e['jra_odds'] or '', e['ref_odds'] or '',
             r['local']['source'][:30], e['jra_ref_odds'] or '', r['ref_time'], e['p'] or '', e['ev'] or '', int(e['pick'])]
            for e in r['entries']])
    return r


def thresholds(r, limit=6):
    """日本馬以外で、現地の勝率から「JRAの単勝がこの倍率以上なら買い」の目安(20倍以下で届く馬だけ)。
    締切直前のJRAオッズの動きに、買う本人がその場で合わせられるようにする"""
    out = []
    for e in sorted(r['entries'], key=lambda e: -(e['p'] or 0)):
        if e['jpn'] or not e['p']:
            continue
        need = EV_MIN / e['p']
        if need <= ODDS_MAX:
            now = f"今{e['jra_odds']:.1f}倍" if e['jra_odds'] else 'JRAオッズ未取得'
            out.append(f"{e['num']:>2}番 {e['name'][:10]}: JRAが {need:.1f}〜{ODDS_MAX:.0f}倍なら買い({now})")
    return out[:limit]


def stake_plan(r):
    s = int(load(OUT / 'config.json', {}).get('stake_per_pick', CONFIG['stake_per_pick']))
    return [(e, s) for e in sorted(r['entries'], key=lambda e: -(e['ev'] or 0)) if e['pick']]


def final_notice(r, now, dry):
    plan = stake_plan(r)
    lines = [f"🌏🎯 **海外発売 投票用 最終版**【{r['race']}】発走 {hm(r['post'])}(日本時間)",
             f"JRAオッズ {hm(r['jra_time'])} / 現地のオッズ: {r['local']['source']} {hm(r['local']['time'])}"]
    if not r['local']['ok']:
        lines.append(f"**判定できません → 見送り**({r['local']['source']}。現地の実際のオッズと比べられないため)")
    elif plan:
        lines.append('```')
        for e, s in plan:
            lines.append(f"単勝 {e['num']:>2}番 {e['name']}  {s:,}円  (JRA{e['jra_odds']:.1f}倍・現地{e['ref_odds']:.1f}倍・期待値{e['ev']:.2f})")
        lines.append(f"合計 {sum(s for _, s in plan):,}円")
        lines.append('```')
    elif not any(e['jra_odds'] for e in r['entries']):
        lines.append('**JRAのオッズが読めませんでした** → 下の目安を見て、JRAの画面の倍率で判断してください')
    else:
        lines.append('**買い目なし**(日本馬以外で、現地より2割以上割安・20倍以下の単勝がありません)→ 見送り')
    th = thresholds(r) if r['local']['ok'] else []
    if th:
        lines.append('買いの目安(締切直前にJRAの倍率がこの範囲に入った馬だけ買う):\n```\n' + '\n'.join(th) + '\n```')
    lines.append('```\n' + table(r) + '\n```')
    lines.append('ルール: 日本馬は買わない / 期待値(現地の勝率×JRA単勝)1.2以上 / JRA20倍以下。'
                 '過去の検証で有望(香港178%・他188%)だけど未確定なので少額で。締切直前にオッズが動くので、買う前にJRAの倍率が20倍以下かだけ確認を。')
    lines.append(f'🌐 {SITE_PAGE}')
    discord('\n'.join(lines), dry)
    if not dry:
        append(OUT / 'bets.csv', ['noticed_at', 'slug', 'race', 'date', 'num', 'name', 'jra_odds', 'ref_odds', 'p', 'ev', 'stake'],
               [[now.strftime('%Y-%m-%d %H:%M'), r['slug'], r['race'], r['date'], e['num'], e['name'], e['jra_odds'],
                 e['ref_odds'], e['p'], e['ev'], s] for e, s in plan])


def settle(decs, now, state, dry):
    """結果(優勝馬)が出たら、最終版の買い目を答え合わせ。払戻 = 金額×確定単勝オッズ"""
    bets = []
    if (OUT / 'bets.csv').exists():
        with open(OUT / 'bets.csv', encoding='utf-8') as f:
            bets = list(csv.DictReader(f))
    done = set(state.get('settled', []))
    by_slug = {d['slug']: d for d in decs}
    rows, msgs = [], []
    for slug in sorted({b['slug'] for b in bets} - done):
        d = by_slug.get(slug)
        if not d or not d['winner']:
            continue
        try:
            fin = race_page(d['link'], now)
        except Exception as e:
            print(f'{slug} 結果の取得失敗: {e}')
            continue
        final = {e['num']: e['jra_odds'] for e in fin['entries']}
        tot_s = tot_p = 0
        for b in [b for b in bets if b['slug'] == slug]:
            n = int(b['num'])
            fo = final.get(n)
            s = int(b['stake'])
            if fo is None:
                pay, res = s, '返還(取消)'
            elif b['name'].replace(' ', '') == d['winner'].replace(' ', ''):
                pay, res = int(round(s * fo)), '1着'
            else:
                pay, res = 0, '外れ'
            tot_s += s
            tot_p += pay
            rows.append([now.strftime('%Y-%m-%d %H:%M'), slug, b['race'], b['date'], n, b['name'], fo or '', s, pay, res])
        msgs.append(f"{d['race']}: 勝ち馬 {d['winner']} / 買い目{len([b for b in bets if b['slug'] == slug])}点 {tot_s:,}円 → 払戻 {tot_p:,}円")
        done.add(slug)
    if rows and not dry:
        append(OUT / 'results.csv', ['settled_at', 'slug', 'race', 'date', 'num', 'name', 'final_odds', 'stake', 'payout', 'result'], rows)
    state['settled'] = sorted(done)
    if msgs:
        rec = record()
        discord('🌏 **海外発売の答え合わせ**\n' + '\n'.join(msgs)
                + f"\n通算: {rec['bets']}点 {rec['stake']:,}円 → {rec['payout']:,}円(回収率{rec['roi']*100:.0f}%)", dry)


def record():
    rows = []
    if (OUT / 'results.csv').exists():
        with open(OUT / 'results.csv', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
    s = sum(int(r['stake']) for r in rows)
    p = sum(int(r['payout']) for r in rows)
    return {'bets': len(rows), 'stake': s, 'payout': p, 'hits': sum(1 for r in rows if r['result'] == '1着'),
            'roi': round(p / s, 3) if s else 0, 'list': rows[-30:]}


# ---------------------------------------------------------------- まとめ役
def daily(now, state, dry):
    try:
        des = designated(now)
    except Exception as e:
        print(f'指定競走一覧の取得失敗: {e}')
        des = load(OUT / 'schedule.json', {}).get('designated', [])
    decs = decided(now)
    today = now.strftime('%Y-%m-%d')
    upcoming = [d for d in decs if d['date'] >= (now - timedelta(days=2)).strftime('%Y-%m-%d')]
    for x in des:
        x['sale'] = any(d['date'] == x['date'] and (x['name'][:4] in d['race'] or d['race'][:4] in x['name']) for d in decs)
    save(OUT / 'schedule.json', {'checked_at': now.strftime('%Y-%m-%d %H:%M'), 'designated': des[:40], 'decided': upcoming})

    # 発売決定の知らせ(初回は既知として記録だけ)
    first = 'seen' not in state
    seen = set(state.get('seen', []))
    new = [d for d in decs if d['slug'] not in seen and d['date'] >= today]
    if new and not first:
        lines = ['🌏 **JRAが海外レースの馬券発売を決めました**']
        for d in new:
            lines.append(f"・{d['date'][5:].replace('-', '/')} {d['country']} {d['track']} **{d['race']}** {d['distance']}")
        lines.append('→ 今日から毎日、出走馬・日本馬・現地オッズを集めて送ります。当日は締切15分前に「投票用 最終版」を送ります。'
                     '\nルール: 日本馬は買わない / 現地より2割以上割安・JRA20倍以下の単勝だけ。')
        discord('\n'.join(lines), dry)
    state['seen'] = sorted(seen | {d['slug'] for d in decs})

    # 1週間前からの毎日のまとめ(1日1回・18時以降)
    races = []
    for d in upcoming:
        if d['date'] > (now + timedelta(days=8)).strftime('%Y-%m-%d'):
            continue
        try:
            r = snapshot(d, now, state)
        except Exception as e:
            print(f"{d['slug']} 取得失敗: {e}")
            continue
        races.append(r)
    ahead = [r for r in races if (r.get('post') or r['date'] + ' 23:59') > now.strftime('%Y-%m-%d %H:%M')]  # まだ走っていないレースだけ
    if ahead and now.hour >= 18 and state.get('digest_on') != today:
        lines = ['🌏 **海外発売 毎日のまとめ**(日本馬🇯🇵は買わない / ✅=いま買い条件)']
        for r in ahead:
            jp = [e['name'] for e in r['entries'] if e['jpn']]
            lines.append(f"\n**{r['race']}** 発走 {hm(r['post'])} / JRA発売開始 {hm(r['sale_start'])}")
            lines.append(f"出走{len(r['entries'])}頭・日本馬{len(jp)}頭({'、'.join(jp)[:60]})" if r['entries'] else '出馬表はまだ(枠順確定後に出ます)')
            lines.append(f"現地のオッズ: {r['local']['source']} {hm(r['local']['time'])} / JRAオッズ"
                         f"{'あり' if any(e['jra_odds'] for e in r['entries']) else 'まだ(発売前)'}")
            if not r['local']['ok']:
                lines.append('→ 当日、締切前の現地の実オッズ(PMU)が取れたら判定します')
            if r['entries']:
                lines.append('```\n' + table(r) + '\n```')
            if r['picks']:
                lines.append(f"いまの買い条件: {'、'.join(str(n) + '番' for n in r['picks'])}(最終判断は当日の締切15分前)")
        discord('\n'.join(lines), dry)
        state['digest_on'] = today
    settle(decs, now, state, dry)
    return decs, races


def write_site(now):
    races = [load(p, {}) for p in sorted((OUT / 'races').glob('*.json'))] if (OUT / 'races').exists() else []
    races = [r for r in races if r.get('date', '') >= (now - timedelta(days=10)).strftime('%Y-%m-%d')]
    save(OUT / 'site.json', {'generated_at': now.strftime('%Y-%m-%d %H:%M'), 'schedule': load(OUT / 'schedule.json', {}),
                             'races': races, 'record': record(),
                             'rule': '日本馬は買わない / 現地の勝率×JRA単勝=期待値1.2以上 / JRA20倍以下'})


def watch(now, state, dry, budget_min=340):
    """daily のあと、締切が近いレースがあれば締切まで約8分おきに見張る。時間切れなら次の当番を起動"""
    t0 = time.time()
    decs, _ = daily(now, state, dry)
    save(OUT / 'state.json', state)
    write_site(now)
    push()
    while (time.time() - t0) / 60 < budget_min:
        now = datetime.now(JST)
        soon = []
        for d in decs:
            r = load(OUT / 'races' / f"{d['slug']}.json", {})
            if not r.get('post'):
                continue
            post = datetime.strptime(r['post'], '%Y-%m-%d %H:%M').replace(tzinfo=JST)
            if now - timedelta(minutes=5) < post <= now + timedelta(hours=30):
                soon.append((post, d))
        if not soon:
            print('30時間以内の発売レースなし → 終了')
            return
        soon.sort(key=lambda x: x[0])
        active = [(p, d) for p, d in soon if p <= now + timedelta(hours=3)]
        for post, d in active:
            try:
                r = snapshot(d, now, state)
            except Exception as e:
                print(f"{d['slug']} 取得失敗: {e}")
                continue
            mins = (post - now).total_seconds() / 60
            key = d['slug']
            sent = state.setdefault('race_sent', {}).setdefault(key, {})
            fn = int(load(OUT / 'config.json', {}).get('final_notice_min', CONFIG['final_notice_min']))
            if 3 < mins <= fn + 6 and not sent.get('final'):
                final_notice(r, now, dry)
                sent['final'] = now.strftime('%H:%M')
                sent['final_picks'] = r['picks']
            elif sent.get('final') and r['picks'] != sent.get('final_picks') and mins > 3 and not sent.get('changed'):
                was = set(sent.get('final_picks') or [])
                drop = [n for n in was if n not in r['picks']]
                add = [n for n in r['picks'] if n not in was]
                discord(f"⚠ **{r['race']} 最終版のあとで買い目が変わりました**(締切直前のオッズ変動・あと{mins:.0f}分)\n"
                        + (f"条件から外れた馬: {'、'.join(str(n) + '番' for n in drop)}(まだ買っていなければ買わなくてOK)\n" if drop else '')
                        + (f"新しく条件に入った馬: {'、'.join(str(n) + '番' for n in add)}\n" if add else '')
                        + f"```\n{table(r)}\n```", dry)
                sent['changed'] = now.strftime('%H:%M')
            elif not sent.get('final') and r['picks'] != sent.get('picks') and any(e['jra_odds'] for e in r['entries']):
                discord(f"🌏 **{r['race']}** 途中経過 {now.strftime('%H:%M')}(発走 {hm(r['post'])}・あと{mins:.0f}分)\n"
                        f"いまの買い条件: {'、'.join(str(n) + '番' for n in r['picks']) or 'なし'}\n```\n{table(r)}\n```", dry)
                sent['picks'] = r['picks']
        save(OUT / 'state.json', state)
        write_site(now)
        push()
        nxt = soon[0][0]
        wait = 8 * 60 if active else min(30 * 60, max(60, (nxt - timedelta(hours=3) - now).total_seconds()))
        if (time.time() - t0 + wait) / 60 >= budget_min:
            break
        time.sleep(wait)
    if not dry and os.environ.get('GITHUB_ACTIONS'):
        print('時間切れ → 次の見張りを起動')
        subprocess.run(['gh', 'workflow', 'run', 'overseas.yml', '-f', 'mode=watch'], check=False)


def push():
    if os.environ.get('GITHUB_ACTIONS'):
        subprocess.run(['bash', 'tools/push_logs.sh', 'log: overseas watch', 'data/overseas'], check=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=['daily', 'watch', 'show'])
    ap.add_argument('link', nargs='?')
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()
    now = datetime.now(JST)
    OUT.mkdir(parents=True, exist_ok=True)
    state = load(OUT / 'state.json', {})
    if a.mode == 'show':
        r = race_page(a.link, now)
        r.update({'race': a.link, 'slug': 'test', 'date': ''})
        evaluate(r, pmu_lookup(r))
        print(json.dumps({k: v for k, v in r.items() if k not in ('entries', 'ref')}, ensure_ascii=False))
        print(table(r, limit=30))
        return
    if a.mode == 'daily':
        daily(now, state, a.dry)
        if not a.dry:
            save(OUT / 'state.json', state)
            write_site(now)
        return
    watch(now, state, a.dry)


if __name__ == '__main__':
    main()
