"""
海外発売チーム: JRAが馬券を売る海外レースの「日程 → 1週間前からの情報集め → 当日の買い目 → 答え合わせ」。
情報はすべて JRA 公式ページから読む(robots.txt は全許可。3秒以上あけて読む)。自動購入はしない(買うのは本人)。

買い目のルール(2016年以降の検証で決めたもの。変えない):
  - 日本馬(調教師が JPN)は買わない … JRAでは日本馬が買われすぎ(全部買うと回収45%)
  - 現地の参考オッズから出した勝率 × JRA単勝オッズ = 期待値 が 1.2以上(=現地より2割以上割安)
  - JRA単勝が20倍以下(大穴の割安は当てにならない)
  検証(2026-11-03 やり直し): 2021-11〜2026-10 にJRAが発売した90レースで 57点・6的中・回収95%(基準未満)。
  10月の「香港178%・香港以外188%」は、データの漏れと発売の無いレースの混ざりで甘く出ていた。ユーザーの判断で単勝は買い続ける。
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
import gzip
import html
import json
import math
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from itertools import combinations
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
FINE_MIN = 40  # 発走の何分前から約1分おきに記録するか(2026-11-03)
# 2026-11-03 過去5年の検証(arc2026/research/bt5y_*.py): 2021-11〜2026-10 にJRAが発売した90レースで 57点・6的中・回収95%。
# 基準(回収100%以上)に届かないが、ユーザーの判断で単勝は買い続ける。全部屋の決まりで、買い目を出す通知の1行目に必ず書く
BELOW = '⚠ 基準未満(検証では回収95%)'
VERIFY = '過去5年(2021〜2026年・JRA発売90レース)の検証では57点・6的中・回収95%で、基準の100%に届いていません'
# 2026-11-03 ユーザー「ワイドを買いとして出して」。過去5年(2021-11〜2026-10・JRA発売)の検証で、
# 日本馬以外で現地の人気(勝率)上位5頭のワイドボックス(10点)が回収150%(87R・毎年103%以上・大きい配当3本を除いても116%)。
# 結果を見る前に決めた19通りの1つで、決めておいた合格の決まり(全体・前半・後半とも100%超、100%超の確率90%以上)を満たした。
# arc2026/research/bt5y_exotics.py。外国馬が5頭に満たないレースは検証でも買っていないので出さない
WIDE_N = 5
WIDE_OK = '✅ ワイドは検証で回収150%'
WIDE_VERIFY = 'ワイドは過去5年(2021〜2026年・JRA発売87レース)の検証で回収150%(毎年プラス)。将来の儲けを約束するものではありません'
CONFIG = {'stake_per_pick': 1000, 'final_notice_min': 15, 'wide_yen': 100}  # data/overseas/config.json で上書きできる(wide_yen はワイド1点の金額)
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


def post(url, cname):
    """JRA公式データベース(JRADB)は cname を POST して読む。間隔は get と同じ"""
    gap = time.time() - _last[0]
    if gap < WAIT:
        time.sleep(WAIT - gap)
    r = requests.post(url, data={'cname': cname}, headers=UA, timeout=25)
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
    # JRAは深夜を前の日の「24時15分」のように書くことがある(2025年 愛チャンピオンS)。24時以上は翌日にする
    return d.replace(hour=0, minute=int(m.group(2))) + timedelta(hours=int(m.group(1)))


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


def race_norm(name):
    """レース名の比較用: 「（G1）」やかっこ・空白・英語の部分を除く"""
    return re.sub(r'[（\(].*?[）\)]|\s|[A-Za-z][A-Za-z0-9\.\'&ⅡⅢ\- ]*$', '', html.unescape(name or ''))


def decided(now):
    """発売レース一覧(今年)。行がある = JRAが発売を決めたレース。
    決まったばかりの時はリンク先が「発売決定のお知らせ」(1つのお知らせを複数レースで共有)で、
    レースのページは後から作られる。なので各レースは「日付_レース名」で区別し、リンクは race(レースのページ) と
    news(お知らせ)に分けて持つ"""
    h = get('/keiba/overseas/racelist/')
    out = []
    for tr in re.findall(r'<tr>(.*?)</tr>', h, re.S):
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)
        if len(tds) < 4 or '年' not in tds[0]:
            continue
        lm = re.search(r'href="([^"]+)"', tds[2])
        fm = re.search(r'alt="([^"]+)"', tds[1])
        dm = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', text(tds[0]))
        if not dm:
            continue
        link = lm.group(1) if lm else ''
        race_link = news = ''
        if '/keiba/overseas/race/' in link:
            race_link = link[:-len('index.html')] if link.endswith('index.html') else link
            race_link = race_link if race_link.endswith('/') else race_link + '/'
        elif link:
            news = link
        date = f'{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}'
        out.append({'date': date, 'race': text(tds[2]),
                    'country': fm.group(1).replace('国旗', '') if fm else '', 'track': text(tds[1]),
                    'distance': text(tds[3]), 'winner': text(tds[4]) if len(tds) > 4 else '', 'link': race_link,
                    'news': news, 'slug': date + '_' + race_norm(text(tds[2]))})
    return out


_news = {}


def news_posts(news):
    """発売決定のお知らせから {レース名(比較用): 発走(日本時間)} を読む。
    例:「第12レース ブリーダーズカップクラシック（G1） … 11月1日（日曜）7時16分（現地時間 …）発走予定」"""
    if news in _news:
        return _news[news]
    t = text(get_opt(news))
    out = {}
    for m in re.finditer(r'第\d+レース\s*(\S+?（G\d）|\S+?)\s.*?(\d{4}年)?(\d{1,2})月(\d{1,2})日（[^）]*）\s*(\d{1,2})時(\d{2})分', t):
        y = int(m.group(2)[:4]) if m.group(2) else datetime.now(JST).year
        # 「24時15分」は翌日の0時15分
        t0 = datetime(y, int(m.group(3)), int(m.group(4)), 0, int(m.group(6))) + timedelta(hours=int(m.group(5)))
        out[race_norm(m.group(1))] = t0.strftime('%Y-%m-%d %H:%M')
    _news[news] = out
    return out


def find_race_page(d, state):
    """レースのページの場所。一覧のリンク → 前に見つけた場所 → 海外発売トップから探す(ページの題名で照合)"""
    if d.get('link'):
        return d['link']
    pages = state.setdefault('race_pages', {})
    if d['slug'] in pages:
        return pages[d['slug']]
    want = race_norm(d['race'])
    seen, queue = set(), sorted(set(re.findall(r'href="(/keiba/overseas/race/[^"#]+?/)(?:index\.html)?"', get_opt('/keiba/overseas/'))))
    while queue and len(seen) < 25:
        link = queue.pop(0)
        if link in seen or 'kaiko' in link:
            continue
        seen.add(link)
        h = get_opt(link)
        tm = re.search(r'<title>(.*?)</title>', h, re.S)
        if tm and want and want in race_norm(text(tm.group(1)).replace('海外競馬発売', '').replace('JRA', '')):
            if re.search(r'href="syutsuba\.html"|href="morning\.html"|出馬表', h):
                pages[d['slug']] = link
                return link
        # 「2026bc/」のような入口ページなら、その下のレースのページも見る
        for sub in re.findall(r'href="((?:/keiba/overseas/race/[^"#]+?/)|(?:[a-z0-9_]+/))(?:index\.html)?"', h):
            full = sub if sub.startswith('/') else link + sub
            if full.startswith(link) and full not in seen:
                queue.append(full)
    return ''


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
    # コメントで隠した古いひな形(BCでは「11月2日（日曜）7時25分」)が残っているので、先に取り除く(2026-10-24)
    top = re.sub(r'<!--.*?-->', '', get_opt(link), flags=re.S)
    sale = post = None
    sm = re.search(r'発売開始時刻.*?日本時間\s*<strong>(.*?)</strong>.*?ネット馬券購入：\s*<strong>(.*?)</strong>', top, re.S)
    if sm:
        sale = jst_time(text(sm.group(1)) + text(sm.group(2)), now)
    # 旧: 日本時間<strong>10月4日（日曜）23時05分</strong> / 新(BC): <div class="line">日本時間11月1日（日曜）7時16分</div>
    pm = re.search(r'発走予定時刻.*?日本時間\s*(?:<strong>)?([^<]*?\d{1,2}時\d{2}分)', top, re.S)
    if pm:
        post = jst_time(text(pm.group(1)), now)
    r = {'link': link, 'sale_start': sale.strftime('%Y-%m-%d %H:%M') if sale else '',
         'post': post.strftime('%Y-%m-%d %H:%M') if post else '', 'entries': [], 'scratched': [], 'ref': {}, 'ref_time': '',
         'ref_source': '', 'jra_time': ''}
    h = sy = get_opt(link + 'syutsuba.html')
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
                        odds[int(n.group(1))] = {'odds': o, 'en': text(hm.group(2)) if hm else '', 'ja': text(hm.group(1)) if hm else ''}
            if odds and (best is None or (t and (best[0] is None or t > best[0]))):
                best = (t, odds, text(cm.group(1)) if cm else '')
        if best:
            r['ref'] = best[1]
            r['ref_time'] = best[0].strftime('%Y-%m-%d %H:%M') if best[0] else ''
            r['ref_source'] = best[2]
    # 2026-11-01 BCでは出馬表(syutsuba.html)がお知らせ「出走予定馬」へ転送されて中身が無かった。
    # その時は参考オッズの表(馬番・日本名・英語名)から出走馬を作り、日本馬はお知らせの「JRA所属馬」の名前で決める。
    # JRAの単勝オッズは取れないので、最終版は「JRAが何倍なら買い」の目安で出る
    if not r['entries'] and r['ref']:
        jp = jra_horses(sy)
        r['entries'] = [{'num': n, 'name': v.get('ja') or v.get('en', ''), 'jra_odds': None, 'trainer_country': '',
                         'jpn': (v.get('ja') or '') in jp} for n, v in sorted(r['ref'].items())]
        r['entries_from'] = '参考オッズの表(出馬表ページが無いため)'
    return r


_jradb = {'at': 0.0, 'menu': [], 'names': {}}
SP_MENU = 'pk01pde01/5F'  # 海外情報メニュー(各レースのページの「出馬表｜オッズ｜開催情報」と同じ)


def jradb_odds(r, now):
    """JRA公式データベースの海外オッズ(単勝・複勝・調教国)。2026-11-01 から。
    BC・メルボルンカップでは出馬表ページ(syutsuba.html)が使えなかったので、こちらでJRAの単勝と日本馬(調教国JPN)を読む。
    戻り値 {馬番: {'name', 'odds', 'code'}}, 'HH:MM' か '最終' / 見つからなければ {}, ''"""
    if not r.get('post'):
        return {}, ''
    if time.time() - _jradb['at'] > 1800:  # メニューは30分ごとに読み直す(発売が始まるとレースが増える)
        try:
            h = post('https://jra.jp/JRADB/accessSP.html', SP_MENU)
            _jradb['menu'] = sorted(set(re.findall(r"doAction\('/JRADB/accessSO\.html'\s*,\s*'([^']+)'\)", h)))
            _jradb['at'] = time.time()
        except requests.RequestException as e:
            print(f'JRA公式の海外メニュー 読めず: {e}')
    want = (r['post'][:10], race_norm(r['race']))
    for cname in _jradb['menu']:
        known = _jradb['names'].get(cname)
        if known and known != want:
            continue
        try:
            h = post('https://jra.jp/JRADB/accessSO.html', cname)
        except requests.RequestException as e:
            print(f'JRA公式オッズ {cname} 読めず: {e}')
            continue
        t = text(re.sub(r'<script.*?</script>', '', h, flags=re.S))
        m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日（[^）]*）\s*(\S+?)\s*海外情報メニューへ戻る', t)
        if not m:
            continue
        got = (f'{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}', race_norm(m.group(4)))
        _jradb['names'][cname] = got
        if got != want:
            continue
        out = {}
        body = h[h.find('<tbody'):h.find('</tbody>')]
        for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', body, re.S):
            n = re.search(r'<td class="num">\s*(\d+)', tr)
            nm = re.search(r'<td class="horse">(.*?)</td>', tr, re.S)
            o = re.search(r'<td class="odds_tan">(.*?)</td>', tr, re.S)
            c = re.search(r'<td class="code">(.*?)</td>', tr, re.S)
            if n and nm:
                out[int(n.group(1))] = {'name': text(nm.group(1)), 'odds': num(text(o.group(1))) if o else None,
                                        'code': text(c.group(1)) if c else ''}
        um = re.search(r'(\d{1,2})時(\d{2})分\s*現在', t)
        when = f'{int(um.group(1)):02d}:{um.group(2)}' if um else ('最終' if '最終オッズ' in t else '')
        return out, when
    return {}, ''


_jra_horses = {}


def jra_horses(page):
    """出馬表の代わりに出ている「出走予定馬」のお知らせから、JRA所属馬の名前を集める"""
    m = re.search(r'http-equiv="refresh"[^>]*url=([^"\s>]+)', page or '')
    if not m:
        return set()
    url = m.group(1)
    if url not in _jra_horses:
        t = text(get_opt(url))
        _jra_horses[url] = set(re.findall(r'([ァ-ヶー・]{2,})\s*（[牡牝セ]\d+歳\s*(?:栗東|美浦)', t))
    return _jra_horses[url]


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
                if not r['entries']:  # 2026-11-01 JRAの出馬表も参考オッズも無い時: 発走時刻(10分以内)とレースの英語名で探す
                    words = re.findall(r'[A-Z]{5,}', (r.get('en_race') or '').upper().replace("'", ''))
                    ok = gap <= 10 and bool(words) and all(w in c.get('libelle', '').upper().replace("'", '') for w in words)
                if ok and (best is None or gap < best[0]):
                    best = (gap, ps, f"R{reu['numOfficiel']}C{c['numOrdre']} {c.get('libelle', '')[:40]}")
    if not best:
        return {}
    out = {}
    for p in best[1]:
        o = (p.get('dernierRapportDirect') or {}).get('rapport')
        out[p.get('numPmu')] = {'odds': o, 'en': p.get('nom', ''), 'out': p.get('statut') == 'NON_PARTANT',
                                'pos': p.get('ordreArrivee'), 'pays': p.get('pays', '')}
    r['pmu_race'] = best[2]
    return out


# ---------------------------------------------------------------- 組み合わせ馬券のJRAオッズの記録(2026-11-03)
# ユーザー「オッズのゆがみに注力して」。過去の海外レースの組み合わせのJRAオッズはどこにも残っていない(netkeiba も数日で消える)ので、
# これから発売の海外レースは、発走40分前〜発走数分後に5分おきで、全券種・全組み合わせのJRAオッズを記録する(買い目には使わない)。
# 取り元は netkeiba のオッズのデータ。2026年BC5レース・メルボルンカップで、JRA公式の確定単勝・全券種の当たり組の払戻と一致を確認した。
NK_API = 'https://race.netkeiba.com/api/api_get_jra_odds.html'
NK_TYPES = ('1', '4', '5', '6', '7', '8')  # 単勝・馬連・ワイド・馬単・3連複・3連単
COMBO_EVERY = 5


def nk_get(url, params=None, enc=None):
    gap = time.time() - _last[0]
    if gap < WAIT:
        time.sleep(WAIT - gap)
    r = requests.get(url, params=params, headers={'User-Agent': UA['User-Agent'], 'Referer': 'https://race.netkeiba.com/'}, timeout=25)
    _last[0] = time.time()
    if enc:
        r.encoding = enc
    return r


def nk_odds(rid, typ):
    """netkeiba のJRAオッズ {組番: [オッズ, …]}。発売前・無い時は {}"""
    d = (nk_get(NK_API, {'race_id': rid, 'type': typ, 'action': 'update'}).json() or {}).get('data') or {}
    odds = d.get('odds') if isinstance(d, dict) else None
    return (odds.get(typ) or {}) if isinstance(odds, dict) else {}


def nk_race_id(r, state):
    """netkeiba の海外レースの番号。その日の海外レースの中から、単勝のオッズが JRA 公式とほぼ同じレースを探す。見つかれば state に覚える"""
    ids = state.setdefault('nk_ids', {})
    if r['slug'] in ids:
        return ids[r['slug']]
    jra = {e['num']: e['jra_odds'] for e in r['entries'] if e.get('jra_odds')}
    if len(jra) < 3 or not r.get('post'):
        return None
    post = datetime.strptime(r['post'], '%Y-%m-%d %H:%M')
    days = sorted({(post - timedelta(days=k)).strftime('%Y%m%d') for k in (0, 1)} | {r['date'].replace('-', '')})
    words = set(re.sub(r'[^ァ-ヶー]', '', r['race']))
    for day in days:
        page = nk_get(f'https://db.netkeiba.com/race/list/{day}/', enc='EUC-JP').text
        cands = {}
        for m in re.finditer(r'href="[^"]*/race/(\d{4}[A-Z0-9]{8})/"[^>]*>([^<]*)<', page):
            if not m.group(1)[4:6].isdigit():
                cands.setdefault(m.group(1), m.group(2))
        # レース名の文字が多く重なるものから試す(最大8つ)
        for rid in sorted(cands, key=lambda k: -len(words & set(cands[k])))[:8]:
            win = nk_odds(rid, '1')
            same = sum(1 for k, v in win.items() if int(k) in jra and abs(float(str(v[0]).replace(',', '')) - jra[int(k)]) <= max(0.3, 0.1 * jra[int(k)]))
            if win and same >= 0.7 * len(jra):
                ids[r['slug']] = rid
                return rid
    return None


def record_combos(r, rid, now):
    """全券種・全組み合わせのJRAオッズを data/overseas/combo/<slug>/<日時>.json.gz に残す"""
    out = {'at': now.strftime('%Y-%m-%d %H:%M'), 'post': r['post'], 'nk_id': rid}
    for typ in NK_TYPES:
        out[typ] = {k: (v[:2] if typ == '5' else v[0]) for k, v in nk_odds(rid, typ).items()}  # ワイドは下限と上限
    f = OUT / 'combo' / r['slug'] / (now.strftime('%Y%m%d-%H%M') + '.json.gz')
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(gzip.compress(json.dumps(out, ensure_ascii=False).encode('utf-8')))
    return sum(len(out[t]) for t in NK_TYPES)


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
        e['need'] = round(EV_MIN / e['p'], 1) if (e['p'] and not e['jpn'] and EV_MIN / e['p'] <= ODDS_MAX) else None
    r['picks'] = [e['num'] for e in sorted(r['entries'], key=lambda e: -(e['ev'] or 0)) if e['pick']]
    # ワイド: 日本馬以外で現地の勝率が高い順に5頭(JRAのオッズは使わない)。5頭に満たなければ出さない
    fo = [e for e in sorted(r['entries'], key=lambda e: -(e['p'] or 0)) if not e['jpn'] and e['p']]
    r['wide'] = [e['num'] for e in fo[:WIDE_N]] if len(fo) >= WIDE_N else []
    return r


def wide_yen():
    return int(load(OUT / 'config.json', {}).get('wide_yen', CONFIG['wide_yen']))


def wide_text(r, nums=None):
    """「ワイド 20・2・24・12・11 ボックス 各100円(10点・1,000円)」"""
    nums = r['wide'] if nums is None else nums
    if not nums:
        return ''
    y, pts = wide_yen(), len(nums) * (len(nums) - 1) // 2
    return f"ワイド {'・'.join(str(n) for n in nums)} ボックス 各{y:,}円({pts}点・{pts * y:,}円)"


def race_no(r, state):
    """即PATのレース番号(=現地のレース番号)。PMUのレース番号と netkeiba の番号の末尾が一致した時だけ。分からなければ None"""
    nums = set()
    m = re.search(r'C(\d+)', r.get('pmu_race') or '')
    if m:
        nums.add(int(m.group(1)))
    nk = state.get('nk_ids', {}).get(r.get('slug', ''))
    if nk:
        nums.add(int(nk[-2:]))
    return nums.pop() if len(nums) == 1 else None


def buy_data(r, state, notice, now):
    """ユーザーの自動購入の仕組み向けの1行(2026-11-03 ユーザー「自動で買う仕組みを作っている。ディスコードに投げてくれればいい」)。
    「🤖BUY 」+ JSON。ワイドがメイン(role=main)、単勝はおまけ(role=extra)。買わない時は bets が空。
    最終版のあとで買い目が変わった時は notice=changed で、その時点の買い目を全部入れ直して送る(前のものと置き換える)"""
    bets = []
    wide = r.get('wide') or []
    if wide:
        pts, y = len(wide) * (len(wide) - 1) // 2, wide_yen()
        bets.append({'role': 'main', 'type': 'ワイド', 'method': 'BOX', 'horses': wide, 'yen_per_point': y, 'points': pts, 'yen': pts * y})
    for e, s in stake_plan(r):
        bets.append({'role': 'extra', 'type': '単勝', 'method': 'NORMAL', 'horses': [e['num']], 'yen_per_point': s, 'points': 1, 'yen': s})
    post = datetime.strptime(r['post'], '%Y-%m-%d %H:%M')
    d = {'v': 1, 'notice': notice, 'sent_at': now.strftime('%Y-%m-%d %H:%M'), 'race_id': r.get('slug', ''), 'race': r['race'],
         'venue': r.get('track', ''), 'country': r.get('country', ''), 'race_no': race_no(r, state),
         'post': r['post'], 'close': (post - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M'),
         'bets': bets, 'total': sum(b['yen'] for b in bets),
         'note': ' / '.join(x for x in ('ワイドは検証で回収150%' if wide else '', '⚠ 単勝は基準未満(検証では回収95%)' if any(b['type'] == '単勝' for b in bets) else '') if x)}
    return '🤖BUY ' + json.dumps(d, ensure_ascii=False, separators=(',', ':'))


def wide_payouts(link):
    """JRA公式の結果ページ(kaiko.html)のワイドの払戻 {(若番, 大番): 100円あたりの円}。まだ無ければ {}"""
    page = re.sub(r'(index\.html|kaiko\.html)?$', 'kaiko.html', link)
    x = text(re.sub(r'<!--.*?-->', '', get_opt(page), flags=re.S))
    m = re.search(r'払戻金.*?ワイド\s+(.*?)\s+(?:馬単|3連複|3連単)', x)
    if not m:
        return {}
    return {tuple(sorted(int(n) for n in c.split('-'))): int(y.replace(',', ''))
            for c, y in re.findall(r'(\d+-\d+)\s+([\d,]+)\s*円', m.group(1))}


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
    link = find_race_page(d, state)
    if link:
        r = race_page(link, now)
    else:  # レースのページがまだ無い(発売決定直後)。発走時刻はお知らせから
        r = {'link': '', 'sale_start': '', 'post': news_posts(d['news']).get(race_norm(d['race']), '') if d.get('news') else '',
             'entries': [], 'scratched': [], 'ref': {}, 'ref_time': '', 'ref_source': '', 'jra_time': ''}
    r.update({k: d.get(k, '') for k in ('date', 'race', 'country', 'track', 'distance', 'winner', 'slug', 'news')})
    r['link'] = link
    en = [x.get('en', '') for x in load(OUT / 'schedule.json', {}).get('designated', [])
          if x.get('date') == d.get('date') and race_norm(x.get('name', '')) == race_norm(d.get('race', ''))]
    r['en_race'] = en[0] if en else ''
    try:
        jra, jra_when = jradb_odds(r, now)
    except Exception as e:
        print(f"{d['slug']} JRA公式オッズ失敗: {e}")
        jra, jra_when = {}, ''
    if jra:
        if not r['entries']:
            r['entries'] = [{'num': n, 'name': v['name'], 'jra_odds': None, 'trainer_country': '', 'jpn': False}
                            for n, v in sorted(jra.items())]
            r['entries_from'] = 'JRA公式のオッズ表'
        for e in r['entries']:
            v = jra.get(e['num'])
            if v:
                e['jra_odds'] = v['odds']
                if v['code']:
                    e['trainer_country'] = v['code']
                    e['jpn'] = v['code'] == 'JPN'  # 調教国で判定(いちばん確か)
        # 単勝が「取消」等で数字でない馬は出走しない
        r['scratched'] = sorted(set(r.get('scratched', [])) | {v['name'] for v in jra.values() if v['odds'] is None})
        r['entries'] = [e for e in r['entries'] if not (e['num'] in jra and jra[e['num']]['odds'] is None)]
        r['jra_time'] = now.strftime('%Y-%m-%d ') + (jra_when if ':' in jra_when else now.strftime('%H:%M'))
        r['jra_source'] = 'JRA公式オッズ' + (f'({jra_when}現在)' if ':' in jra_when else '')
    pmu = {}
    if r['post']:
        post = datetime.strptime(r['post'], '%Y-%m-%d %H:%M').replace(tzinfo=JST)
        if post - timedelta(hours=36) <= now <= post + timedelta(days=3):  # PMUの出走表は前日ごろに出る
            try:
                pmu = pmu_lookup(r)
            except Exception as e:
                print(f"{d['slug']} PMU失敗: {e}")
    if not r['entries'] and pmu:  # JRAの出馬表も参考オッズも無い時は、PMUの出走表から(日本馬は生産国=日本で判定)
        jp = jra_horses(get_opt(link + 'syutsuba.html')) if link else set()
        r['entries'] = [{'num': n, 'name': v['en'], 'jra_odds': None, 'trainer_country': '', 'jpn': v.get('pays') == 'Japon', 'en': v['en']}
                        for n, v in sorted(pmu.items()) if n and not v['out']]
        r['entries_from'] = 'PMUの出走表(JRAの出馬表・参考オッズが無いため。日本馬は生産国で判定' + (f'。JRA所属馬: {"、".join(sorted(jp))}' if jp else '') + ')'
    evaluate(r, pmu)
    r['checked_at'] = now.strftime('%Y-%m-%d %H:%M')
    old = load(OUT / 'races' / f"{d['slug']}.json", {})
    if old.get('final'):
        r['final'] = old['final']
    r['stake_per_pick'] = int(load(OUT / 'config.json', {}).get('stake_per_pick', CONFIG['stake_per_pick']))
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
    has_jra = any(e['jra_odds'] for e in r['entries'])
    wide = r.get('wide') or []
    # 2026-11-01 ユーザー「いちいち判断するの疲れる。断定して」→ 2行目で「買う/買わない」を言い切る
    # 2026-11-03 ワイド(検証150%)を先に。単勝(基準未満)を出す時は、全部屋の決まりで1行目に「基準未満」
    # 2026-11-03 ユーザー「ワイド メイン 単勝おまけ」
    buys = ([f'【メイン】{wide_text(r)}'] if wide else []) + [f"【おまけ】単勝 {e['num']}番 {e['name']} {s:,}円" for e, s in plan]
    lab = ' / '.join(x for x in (WIDE_OK if wide else '', BELOW if plan else '') if x)
    if buys:
        head = '👉 **これを買う: ' + ' / '.join(buys) + '**'
    elif r['local']['ok'] and has_jra:
        head = '👉 **今回は買わない**(割安な馬がいない)'
    elif not r['local']['ok']:
        head = '👉 **今回は買わない**(現地の実際のオッズが取れず、判定できない)'
    else:
        head = '👉 **JRAのオッズが読めなかった**ので、下の目安で判断が必要です'
    lines = [f"{lab + ' ' if lab else ''}🌏🎯 **海外発売 投票用 最終版**【{r['race']}】発走 {hm(r['post'])}(日本時間)", head,
             f"JRAオッズ {hm(r['jra_time'])} / 現地のオッズ: {r['local']['source']} {hm(r['local']['time'])}"]
    if wide:
        ents = {e['num']: e for e in r['entries']}
        lines.append('【メイン】ワイド ボックス(日本馬以外で、現地の人気の上位5頭。JRAのオッズは使わない):\n```\n' + '\n'.join(
            f"{n:>2}番 {ents[n]['name'][:12]}  現地{(ents[n]['ref_odds'] or 0):.1f}倍・勝率{(ents[n]['p'] or 0) * 100:.1f}%" for n in wide)
            + f"\n{wide_text(r)}\n```")
    if not r['local']['ok']:
        lines.append(f"**判定できません → 見送り**({r['local']['source']}。現地の実際のオッズと比べられないため)")
    elif plan:
        lines.append('【おまけ】単勝:\n```')
        for e, s in plan:
            lines.append(f"単勝 {e['num']:>2}番 {e['name']}  {s:,}円  (JRA{e['jra_odds']:.1f}倍・現地{e['ref_odds']:.1f}倍・期待値{e['ev']:.2f})")
        lines.append(f"合計 {sum(s for _, s in plan):,}円")
        lines.append('```')
    elif not any(e['jra_odds'] for e in r['entries']):
        lines.append('**JRAのオッズが読めませんでした** → 下の目安を見て、JRAの画面の倍率で判断してください')
    else:
        lines.append('**単勝は買い目なし**(日本馬以外で、現地より2割以上割安・20倍以下の単勝がありません)')
    th = thresholds(r) if r['local']['ok'] and not has_jra else []  # JRAのオッズがある時は目安を出さない(判断を求めない)
    if th:
        lines.append('買いの目安(締切直前にJRAの倍率がこの範囲に入った馬だけ買う):\n```\n' + '\n'.join(th) + '\n```')
    if r['local']['ok']:  # 2026-11-01 ユーザーの要望「どういうロジックで買わないかまで」
        jp = [e['name'] for e in r['entries'] if e['jpn']]
        far = [e for e in r['entries'] if not e['jpn'] and e['p'] and EV_MIN / e['p'] > ODDS_MAX]
        why = [f"日本馬{len(jp)}頭({'、'.join(n[:8] for n in jp)})は対象外(JRAでは買われすぎ)" if jp else '',
               f"外国馬{len(far)}頭は勝率が低く、期待値1.2に必要なJRAの倍率が20倍を超える" if far else '']
        if any(why):
            lines.append('買わない理由: ' + ' / '.join(x for x in why if x))
        lines.append(f"計算: 「JRAが○倍なら買い」の○ = 1.2 ÷ 現地の勝率(現地のオッズから控除を除いた値。{r.get('method', '')})")
    lines.append('```\n' + table(r) + '\n```')
    lines.append(f'{WIDE_VERIFY}。')
    lines.append('単勝のルール: 日本馬は買わない / 期待値(現地の勝率×JRA単勝)1.2以上 / JRA20倍以下。'
                 f'{VERIFY}。この後オッズが動いて買い目が変わったら、すぐ別に知らせます。')
    lines.append(f'🌐 {SITE_PAGE}')
    discord('\n'.join(lines), dry)
    if not dry and wide:
        append(OUT / 'wide_bets.csv', ['noticed_at', 'slug', 'race', 'date', 'nums', 'yen_per_point', 'points', 'stake'],
               [[now.strftime('%Y-%m-%d %H:%M'), r['slug'], r['race'], r['date'], '-'.join(str(n) for n in wide), wide_yen(),
                 len(wide) * (len(wide) - 1) // 2, wide_yen() * len(wide) * (len(wide) - 1) // 2]])
    if not dry:
        append(OUT / 'bets.csv', ['noticed_at', 'slug', 'race', 'date', 'num', 'name', 'jra_odds', 'ref_odds', 'p', 'ev', 'stake'],
               [[now.strftime('%Y-%m-%d %H:%M'), r['slug'], r['race'], r['date'], e['num'], e['name'], e['jra_odds'],
                 e['ref_odds'], e['p'], e['ev'], s] for e, s in plan])


def bedtime_notice(rs, now, dry):
    """前の晩23時台の「寝る前の目安」。払戻は締切時のオッズで決まるので、最終版より確度は下がる(記録はしない)"""
    rs = sorted(rs, key=lambda r: r.get('post', ''))
    lines = [f"{WIDE_OK} / {BELOW} 🌙 **海外発売 寝る前の目安**({now.strftime('%H:%M')}時点・発走が深夜〜朝のレース)",
             f"最終版は各レースの締切15分前に届きます({hm(rs[0]['post'])}〜)。起きていられない時は、ここで先に買う手もあります。"
             'ただしJRAの払戻は締切時のオッズで決まるので、いまの倍率から動きます(確度は最終版より下がる)。']
    for r in rs:
        lines.append(f"\n**{r['race']}** 発走 {hm(r['post'])}")
        if not r['local']['ok']:
            lines.append(f"→ 現地の実際のオッズがまだ取れていません({r['local']['source']})。判定は最終版で")
            continue
        if r.get('wide'):
            lines.append(f"いまのワイド(メイン): **{wide_text(r)}**")
        plan = stake_plan(r)
        if plan:
            lines.append('いまの単勝の買い条件(おまけ): ' + ' / '.join(
                f"単勝{e['num']}番 {e['name'][:10]} {s:,}円(JRA{e['jra_odds']:.1f}倍・現地{e['ref_odds']:.1f}倍・期待値{e['ev']:.2f})" for e, s in plan))
        elif any(e['jra_odds'] for e in r['entries']):
            lines.append('いまの単勝の買い条件: なし')
        else:
            lines.append('JRAのオッズはまだ(発売前)')
        th = thresholds(r, limit=4)
        if th:
            lines.append('```\n' + '\n'.join(th) + '\n```')
    lines.append(f'{WIDE_VERIFY}。')
    lines.append(f'単勝のルール: 日本馬は買わない / 期待値1.2以上 / JRA20倍以下。{VERIFY}。')
    lines.append(f'🌐 {SITE_PAGE}')
    discord('\n'.join(lines), dry)


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
            link = find_race_page(d, state)
            if not link:
                continue
            fin = race_page(link, now)
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
    # ワイドの答え合わせ(2026-11-03)。払戻は JRA 公式の結果ページ(kaiko.html)のワイドの払戻。取消の馬がからむ組は返還
    wbets = []
    if (OUT / 'wide_bets.csv').exists():
        with open(OUT / 'wide_bets.csv', encoding='utf-8') as f:
            wbets = list(csv.DictReader(f))
    wdone = set(state.get('settled_wide', []))
    wrows = []
    for b in wbets:
        d = by_slug.get(b['slug'])
        if b['slug'] in wdone or not d or not d['winner']:
            continue
        try:
            link = find_race_page(d, state)
            pays = wide_payouts(link) if link else {}
            fin = race_page(link, now) if pays else None
        except Exception as e:
            print(f"{b['slug']} ワイドの結果の取得失敗: {e}")
            continue
        if not pays:  # 結果ページの払戻がまだ出ていない
            continue
        nums = [int(n) for n in b['nums'].split('-')]
        ran = {e['num'] for e in fin['entries'] if e.get('jra_odds')} if fin and fin.get('entries') else set(nums)
        y = int(b['yen_per_point'])
        pay = hits = 0
        for c in combinations(sorted(nums), 2):
            if not set(c) <= ran:
                pay += y  # 取消の馬がからむ組は返還
            elif c in pays:
                pay += pays[c] * y // 100
                hits += 1
        hit_txt = '、'.join(f'{a}-{c}' for a, c in combinations(sorted(nums), 2) if (a, c) in pays and {a, c} <= ran)
        wrows.append([now.strftime('%Y-%m-%d %H:%M'), b['slug'], b['race'], b['date'], b['nums'], int(b['stake']), pay, hits, hit_txt])
        msgs.append(f"{b['race']}: ワイド {b['nums'].replace('-', '・')} ボックス {int(b['stake']):,}円 → 払戻 {pay:,}円"
                    + (f"(当たり {hit_txt})" if hit_txt else '(外れ)'))
        wdone.add(b['slug'])
    if wrows and not dry:
        append(OUT / 'wide_results.csv', ['settled_at', 'slug', 'race', 'date', 'nums', 'stake', 'payout', 'hits', 'hit_pairs'], wrows)
    state['settled_wide'] = sorted(wdone)
    if msgs:
        rec, wr = record(), wide_record()
        discord('🌏 **海外発売の答え合わせ**\n' + '\n'.join(msgs)
                + (f"\nワイド通算: {wr['races']}レース {wr['stake']:,}円 → {wr['payout']:,}円(回収率{wr['roi']*100:.0f}%)" if wr['races'] else '')
                + f"\n単勝通算: {rec['bets']}点 {rec['stake']:,}円 → {rec['payout']:,}円(回収率{rec['roi']*100:.0f}%)", dry)


def record():
    rows = []
    if (OUT / 'results.csv').exists():
        with open(OUT / 'results.csv', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
    s = sum(int(r['stake']) for r in rows)
    p = sum(int(r['payout']) for r in rows)
    return {'bets': len(rows), 'stake': s, 'payout': p, 'hits': sum(1 for r in rows if r['result'] == '1着'),
            'roi': round(p / s, 3) if s else 0, 'list': rows[-30:]}


def wide_record():
    rows = []
    if (OUT / 'wide_results.csv').exists():
        with open(OUT / 'wide_results.csv', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
    s = sum(int(r['stake']) for r in rows)
    p = sum(int(r['payout']) for r in rows)
    return {'races': len(rows), 'stake': s, 'payout': p, 'hits': sum(1 for r in rows if int(r['hits'] or 0) > 0),
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
        x['sale'] = any(d['date'] == x['date'] and race_norm(d['race']) == race_norm(x['name']) for d in decs)
    save(OUT / 'schedule.json', {'checked_at': now.strftime('%Y-%m-%d %H:%M'), 'designated': des[:40], 'decided': upcoming})

    # 発売決定の知らせ(初回は既知として記録だけ)
    # 2026-10-24 区別の仕方を「日付_レース名」に変えた。初回は今ある分を既知にする(同じ知らせを二度送らない)
    first = 'seen_v2' not in state
    seen = set(state.get('seen_v2', []))
    new = [d for d in decs if d['slug'] not in seen and d['date'] >= today]
    if new and not first:
        lines = [f'{WIDE_OK} / {BELOW} 🌏 **JRAが海外レースの馬券発売を決めました**']
        for d in new:
            lines.append(f"・{d['date'][5:].replace('-', '/')} {d['country']} {d['track']} **{d['race']}** {d['distance']}")
        lines.append('→ 今日から毎日、出走馬・日本馬・現地オッズを集めて送ります。当日は締切15分前に「投票用 最終版」を送ります。'
                     '\n買い方: ワイド=日本馬以外で現地の人気の上位5頭のボックス / 単勝=日本馬以外で現地より2割以上割安・JRA20倍以下')
        discord('\n'.join(lines), dry)
    state['seen_v2'] = sorted(seen | {d['slug'] for d in decs})

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
        lines = [f'{WIDE_OK} / {BELOW} 🌏 **海外発売 毎日のまとめ**(日本馬🇯🇵は買わない / ✅=いま単勝の買い条件)']
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
            if r.get('wide'):
                lines.append(f"いまのワイド(メイン): {wide_text(r)}(最終判断は当日の締切15分前)")
            if r['picks']:
                lines.append(f"いまの単勝の買い条件(おまけ): {'、'.join(str(n) + '番' for n in r['picks'])}(最終判断は当日の締切15分前)")
        discord('\n'.join(lines), dry)
        state['digest_on'] = today
    settle(decs, now, state, dry)
    return decs, races


def write_site(now):
    races = [load(p, {}) for p in sorted((OUT / 'races').glob('*.json'))] if (OUT / 'races').exists() else []
    races = [r for r in races if r.get('date', '') >= (now - timedelta(days=10)).strftime('%Y-%m-%d')]
    save(OUT / 'site.json', {'generated_at': now.strftime('%Y-%m-%d %H:%M'), 'schedule': load(OUT / 'schedule.json', {}),
                             'races': races, 'record': record(), 'wide_record': wide_record(),
                             'rule': '日本馬は買わない / 現地の勝率×JRA単勝=期待値1.2以上 / JRA20倍以下',
                             'wide_rule': '日本馬以外で現地の人気の上位5頭のワイドボックス(10点)'})


def watch(now, state, dry, budget_min=340):
    """daily のあと、締切が近いレースがあれば締切まで約8分おきに見張る。時間切れなら次の当番を起動"""
    t0 = time.time()
    decs, _ = daily(now, state, dry)
    save(OUT / 'state.json', state)
    write_site(now)
    push()
    last_push = time.time()
    while (time.time() - t0) / 60 < budget_min:
        loop_t = time.time()
        now = datetime.now(JST)
        noticed = False
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
        # 寝る前の目安(2026-10-24): 発走が深夜〜朝(0〜9時)のレースは、最終版が寝ている間に届くので、前の晩23時台に一度だけ送る
        bed = [d for p, d in soon if p.date() == (now + timedelta(days=1)).date() and p.hour < 9]
        if bed and now.hour == 23 and state.get('bedtime_on') != now.strftime('%Y-%m-%d'):
            rs = []
            for d in bed:
                try:
                    rs.append(snapshot(d, now, state))
                except Exception as e:
                    print(f"{d['slug']} 取得失敗: {e}")
            if rs:
                bedtime_notice(rs, now, dry)
            state['bedtime_on'] = now.strftime('%Y-%m-%d')
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
                discord(buy_data(r, state, 'final', now), dry)  # ユーザーの自動購入の仕組み向けの1行
                noticed = True
                sent['final'] = now.strftime('%H:%M')
                sent['final_picks'] = r['picks']
                sent['final_wide'] = r.get('wide') or []
                r['final'] = {'at': now.strftime('%Y-%m-%d %H:%M'), 'picks': r['picks'],
                              'stake': int(load(OUT / 'config.json', {}).get('stake_per_pick', CONFIG['stake_per_pick'])),
                              'wide': r.get('wide') or [], 'wide_yen': wide_yen()}
                save(OUT / 'races' / f"{d['slug']}.json", r)
            elif sent.get('final') and (r['picks'] != sent.get('final_picks') or sorted(r.get('wide') or []) != sorted(sent.get('final_wide') or [])) \
                    and mins > 3 and not sent.get('changed'):
                was = set(sent.get('final_picks') or [])
                drop = [n for n in was if n not in r['picks']]
                add = [n for n in r['picks'] if n not in was]
                wchg = sorted(r.get('wide') or []) != sorted(sent.get('final_wide') or [])
                discord(f"{WIDE_OK if r.get('wide') else ''}{' / ' if r.get('wide') and r['picks'] else ''}{BELOW if r['picks'] else ''} "
                        f"**{r['race']} 最終版のあとで買い目が変わりました**(締切直前のオッズ変動・あと{mins:.0f}分)\n"
                        + (f"ワイドの5頭が変わりました → **{wide_text(r) or 'ワイドは見送り'}**(まだ買っていなければこちら)\n" if wchg else '')
                        + (f"単勝の条件から外れた馬: {'、'.join(str(n) + '番' for n in drop)}(まだ買っていなければ買わなくてOK)\n" if drop else '')
                        + (f"単勝の条件に新しく入った馬: {'、'.join(str(n) + '番' for n in add)}\n" if add else '')
                        + f"```\n{table(r)}\n```", dry)
                discord(buy_data(r, state, 'changed', now), dry)  # 変わった後の買い目を全部入れ直して送る
                noticed = True
                sent['changed'] = now.strftime('%H:%M')
            elif not sent.get('final') and r['picks'] != sent.get('picks') and any(e['jra_odds'] for e in r['entries']):
                discord(f"{BELOW} 🌏 **{r['race']}** 途中経過 {now.strftime('%H:%M')}(発走 {hm(r['post'])}・あと{mins:.0f}分)\n"
                        f"いまの買い条件: {'、'.join(str(n) + '番' for n in r['picks']) or 'なし'}\n```\n{table(r)}\n```", dry)
                sent['picks'] = r['picks']
                noticed = True
            # 組み合わせのJRAオッズの記録(2026-11-03・買い目には使わない)。番号探しは発走25分前より前だけ・1レース4回まで(最終版の邪魔をしない)。
            # 記録は発走40分前〜発走5分後に5分おき(最後の1回は締切後=確定)。失敗しても通知には関係しない
            try:
                tries = state.setdefault('nk_tries', {})
                rid = state.get('nk_ids', {}).get(key)
                if not rid and mins > 25 and tries.get(key, 0) < 4 and any(e['jra_odds'] for e in r['entries']):
                    tries[key] = tries.get(key, 0) + 1
                    rid = nk_race_id(r, state)
                    print(f"{key} netkeibaの番号: {rid or '見つからない'}")
                ct = state.setdefault('combo_at', {})
                last = datetime.strptime(ct[key], '%Y-%m-%d %H:%M').replace(tzinfo=JST) if key in ct else None
                due = last is None or now - last >= timedelta(minutes=COMBO_EVERY) - timedelta(seconds=30)
                final_due = last is None or last < post + timedelta(minutes=2)  # 締切後の確定を、発走3〜5分後にもう1回
                if rid and ((-5 < mins <= FINE_MIN and due) or (-5 < mins <= -3 and final_due)):
                    n = record_combos(r, rid, now)
                    ct[key] = now.strftime('%Y-%m-%d %H:%M')
                    print(f"{key} 組み合わせのオッズを記録 {n}件")
            except Exception as e:
                print(f"{key} 組み合わせのオッズ記録失敗: {e}")
        save(OUT / 'state.json', state)
        # 2026-11-03 発走40分前〜発走5分後は約1分おきに記録する(締切直前のオッズの動きを残し、何分前に買うのが良いかを後で決めるため)。
        # その間の GitHub への保存は8分おきか、通知を送った時だけ(1分ごとの保存で履歴が埋まらないように)
        fine = any(now - timedelta(minutes=5) < p <= now + timedelta(minutes=FINE_MIN) for p, _ in active)
        if not fine or noticed or time.time() - last_push >= 8 * 60:
            write_site(now)
            push()
            last_push = time.time()
        nxt = soon[0][0]
        if fine:
            wait = max(10, 60 - (time.time() - loop_t))
        else:
            wait = 8 * 60 if active else min(30 * 60, max(60, (nxt - timedelta(hours=3) - now).total_seconds()))
        # 当番の交代(時間切れ→次の見張りを起動)が、最終版〜締切の時間帯(発走30分前〜3分後)に重ならないようにする。
        # 交代は時間切れの最大30分前に起きるので、その幅で重なりを見る。重なりそうで、まだ発走45分より前なら今のうちに交代する
        end = now + timedelta(minutes=budget_min - (time.time() - t0) / 60)
        if not fine and (time.time() - t0) / 60 > 20 and any(
                end - timedelta(minutes=30) < p + timedelta(minutes=3) and p - timedelta(minutes=30) < end and p - now > timedelta(minutes=45)
                for p, _ in soon):
            print('交代が締切の時間帯に重なるので、早めに交代する')
            write_site(now)
            push()
            break
        if (time.time() - t0 + wait) / 60 >= budget_min:
            write_site(now)
            push()
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
