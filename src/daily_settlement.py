"""
当日の買い目精算スクリプト（GitHub Actions から夕方に自動実行）

bet_log.csv からその日の買い推奨を取り出し、レース結果と確定オッズを
netkeiba から取得して「的中・配当・回収率」を計算し、Discordへ通知する。
精算結果は data/settle_log.csv に蓄積され、累計成績も併せて表示する。
前日以前の買い目で settle_log に無いもの（結果未確定・取りこぼし）も毎回拾い直して
精算する（settle_log にある分は二重計上しない）。

使い方: python daily_settlement.py                    # 本日分を精算（表示のみ）
        python daily_settlement.py --discord          # Discordにも送信
        python daily_settlement.py --date 2026-07-11  # 日付指定
※金額は bet_log の推奨金額（stake列）。競走中止・失格は外れ、出走取消・競走除外は返還。
"""
import os
import re
import time
import argparse
import csv
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import sys
SRC = Path(__file__).parent
sys.path.insert(0, str(SRC))
from scraper import scrape_race_result
from predict import fetch_live_odds, fetch_combo_odds
from analysis import VENUE_NAME


REFUND_POS = ('取消', '除外')   # 出走取消・競走除外 → 返還（払戻=賭金）
LOSS_POS   = ('中止', '失格')   # 競走中止・失格 → 外れ（払戻0）
REQ_INTERVAL = 3.0              # netkeiba へのリクエスト間隔（秒）


def _norm_pos(txt):
    """着順の表記 → 数値 / '中止'・'取消'・'除外'・'失格' / 不明は None"""
    s = str(txt or '').strip()
    m = re.match(r'\d+', s)
    if m:
        return int(m.group())
    return next((w for w in REFUND_POS + LOSS_POS if w in s), None)


def _pos_s(p):
    return f'{p}着' if isinstance(p, int) else (p or '?')


def _umaren_winners(res):
    """着順から馬連の的中組 {'3-7', ...} を返す（払戻表が取れない時の代替。同着も考慮）"""
    first  = sorted((n for n, p in res.items() if p == 1), key=int)
    second = sorted((n for n, p in res.items() if p == 2), key=int)
    if len(first) >= 2:  # 1着同着 → 1着馬同士
        return {f'{x}-{y}' for i, x in enumerate(first) for y in first[i + 1:]}
    return {'-'.join(sorted((f, s), key=int)) for f in first for s in second}


def fetch_result_live(race_id):
    """race.netkeiba の結果ページから ({馬番: 着順}, 払戻) を取得。

    着順は数値か '中止'/'取消'/'除外'/'失格'。払戻は100円あたりの金額で
    {'単勝': {'5': 590}, '馬連': {'4-5': 2380}}（同着なら複数の組が入る）。
    db.netkeiba のレースページは当日中に更新されないことがあるため、
    当日精算では必ずこちら（レース確定直後から見られる）を先に使う。
    """
    import requests
    from bs4 import BeautifulSoup
    try:
        r = requests.get('https://race.netkeiba.com/race/result.html',
                         params={'race_id': str(race_id)},
                         headers={'User-Agent': 'Mozilla/5.0',
                                  'Referer': 'https://race.netkeiba.com/'},
                         timeout=15)
        r.encoding = 'UTF-8'
        soup = BeautifulSoup(r.text, 'lxml')
        wrap = soup.find(class_=re.compile('ResultTableWrap'))
        out = {}
        if wrap:
            table = wrap.find('table')
            if table:
                for tr in table.find_all('tr')[1:]:
                    tds = tr.find_all('td')
                    if len(tds) < 4:
                        continue
                    pos = _norm_pos(tds[0].get_text(strip=True))
                    num_txt = tds[2].get_text(strip=True)
                    if pos is not None and num_txt.isdigit():
                        out[num_txt] = pos
        # 払戻表（同着時は「馬番が組数ぶん・金額が複数」並ぶ）
        pay = {'単勝': {}, '馬連': {}}
        for tr in soup.find_all('tr'):
            cells = tr.find_all(['th', 'td'])
            if len(cells) < 3 or cells[0].get_text(strip=True) not in pay:
                continue
            kind = cells[0].get_text(strip=True)
            k = 1 if kind == '単勝' else 2
            nums = [int(x) for x in re.findall(r'\d+', cells[1].get_text(' ', strip=True))]
            yens = [int(x.replace(',', '')) for x in
                    re.findall(r'([\d,]+)円', cells[2].get_text(' ', strip=True))]
            if not yens or len(nums) != k * len(yens):
                continue
            for i, y in enumerate(yens):
                pay[kind]['-'.join(str(n) for n in sorted(nums[i * k:(i + 1) * k]))] = y
        return out, pay
    except Exception as e:
        print(f'  結果取得エラー {race_id}: {e}')
        return {}, {}

JST = ZoneInfo('Asia/Tokyo')
DATA_DIR   = SRC.parent / 'data'
BET_LOG    = DATA_DIR / 'bet_log.csv'
SETTLE_LOG = DATA_DIR / 'settle_log.csv'
WEBHOOK_FILE = DATA_DIR / 'discord_webhook.txt'


def send_discord(text):
    url = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
    if not url and WEBHOOK_FILE.exists():
        url = WEBHOOK_FILE.read_text(encoding='utf-8-sig').strip()
    if not url:
        print('Discord: webhook URL がありません。')
        return
    try:
        import requests
        r = requests.post(url, json={'content': text}, timeout=10)
        print('Discord送信完了' if r.status_code in (200, 204) else f'Discord送信失敗: {r.status_code}')
    except Exception as e:
        print(f'Discord送信エラー: {e}')


def append_settle_log(rows):
    """精算結果を settle_log.csv に追記（同一 race_id+horse_num は上書きしない）"""
    existing = set()
    if SETTLE_LOG.exists():
        old = pd.read_csv(SETTLE_LOG, dtype=str, encoding='utf-8-sig')
        existing = set(zip(old['race_id'], old['horse_num']))
    new_file = not SETTLE_LOG.exists()
    with open(SETTLE_LOG, 'a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(['date', 'race_id', 'horse_num', 'horse_name', 'kind',
                        'pos', 'final_odds', 'stake', 'payout'])
        for r in rows:
            if (str(r['race_id']), str(r['horse_num'])) in existing:
                continue
            w.writerow([r['date'], r['race_id'], r['horse_num'], r['horse_name'],
                        r['kind'], r['pos'], r['final_odds'], r['stake'], r['payout']])


def settled_keys():
    """settle_log にある (race_id, horse_num) の集合"""
    if not SETTLE_LOG.exists():
        return set()
    old = pd.read_csv(SETTLE_LOG, dtype=str, encoding='utf-8-sig')
    return set(zip(old['race_id'], old['horse_num']))


def cumulative_summary(upto):
    if not SETTLE_LOG.exists():
        return ''
    df = pd.read_csv(SETTLE_LOG, encoding='utf-8-sig')
    inv = df['stake'].sum()
    ret = df['payout'].sum()
    # 返還（取消・除外）は的中に数えない
    hit = ((df['payout'] > 0) & ~df['pos'].astype(str).isin(REFUND_POS)).sum()
    if inv <= 0:
        return ''
    # 未精算 = upto 日までの買い目で settle_log に無いもの
    unsettled = 0
    if BET_LOG.exists():
        bets = pd.read_csv(BET_LOG, dtype=str, encoding='utf-8-sig')
        bets = bets[bets['logged_at'].str[:10] <= upto]
        done = settled_keys()
        unsettled = len({k for k in zip(bets['race_id'], bets['horse_num']) if k not in done})
    return (f'累計: {len(df)}点 的中{hit}回 投資{inv:,.0f}円 '
            f'回収{ret:,.0f}円 回収率{ret/inv*100:.1f}% 未精算{unsettled}点')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--discord', action='store_true')
    ap.add_argument('--date', default=None, help='YYYY-MM-DD（デフォルト: 本日JST）')
    args = ap.parse_args()

    target = args.date or datetime.now(JST).strftime('%Y-%m-%d')
    label  = f'{int(target[5:7])}/{int(target[8:10])}'
    print(f'精算対象日: {target}（それ以前の未精算分も含む）')

    bets = pd.DataFrame()
    if BET_LOG.exists():
        bets = pd.read_csv(BET_LOG, dtype=str, encoding='utf-8-sig')
        bets = bets.drop_duplicates(subset=['race_id', 'horse_num'], keep='last')
        bets['bet_date'] = bets['logged_at'].str[:10]
        # 本日分は全部（再実行しても同じ通知になる）。前日以前は settle_log に無い分だけ拾い直す
        done = settled_keys()
        unsettled = pd.Series([(r, h) not in done for r, h in zip(bets['race_id'], bets['horse_num'])],
                              index=bets.index, dtype=bool)
        bets = bets[(bets['bet_date'] == target) | ((bets['bet_date'] < target) & unsettled)]
    n_today = int((bets['bet_date'] == target).sum()) if not bets.empty else 0
    if not bets.empty:
        print(f'  本日の買い目: {n_today}点 / 前日以前の未精算: {len(bets) - n_today}点')

    # レースごとに結果・払戻と確定オッズを取得（netkeiba へは REQ_INTERVAL 秒おき）
    results, payouts, odds_final, umaren_final = {}, {}, {}, {}
    for i, rid in enumerate(bets['race_id'].unique() if not bets.empty else []):
        if i:
            time.sleep(REQ_INTERVAL)
        # 当日反映される結果ページを優先、ダメならdbページ
        res_map, pay = fetch_result_live(str(rid))
        if not res_map:
            time.sleep(REQ_INTERVAL)
            rows = scrape_race_result(str(rid))
            res_map = {str(r.get('horse_num')): _norm_pos(r.get('finishing_pos'))
                       for r in rows} if rows else {}
            res_map = {k: v for k, v in res_map.items() if v is not None}
        if res_map:
            results[rid] = res_map
            payouts[rid] = pay
        time.sleep(REQ_INTERVAL)
        odds_final[rid] = fetch_live_odds(str(rid))
        if (bets[(bets['race_id'] == rid) & (bets['kind'] == '馬連')]).shape[0]:
            time.sleep(REQ_INTERVAL)
            umaren_final[rid] = fetch_combo_odds(str(rid), '4')

    lines, past_lines, settle_rows = [], [], []
    tot = {True: [0, 0, 0, 0], False: [0, 0, 0, 0]}  # 本日分/前日以前分の [投資, 回収, 精算点数, 未確定]

    def book(is_today, line, row=None):
        (lines if is_today else past_lines).append(line)
        t = tot[is_today]
        if row is None:  # 結果未確定 → 次回の実行で拾い直す
            t[3] += 1
            return
        t[0] += row['stake']
        t[1] += row['payout']
        t[2] += 1
        settle_rows.append(row)

    for _, b in bets.iterrows():
        rid, hn = str(b['race_id']), str(b['horse_num'])
        name = str(b.get('horse_name', ''))
        kind = str(b.get('kind', ''))
        venue = VENUE_NAME.get(rid[4:6], '')
        rno   = int(rid[10:12]) if rid[10:12].isdigit() else '?'
        race_res = results.get(rid, {})
        pay = payouts.get(rid, {})
        bdate = b['bet_date']
        is_today = bdate == target
        # 推奨金額（bet_logのstake列。旧形式の記録は100円扱い）
        try:
            stake = int(float(b.get('stake') or 100))
        except (ValueError, TypeError):
            stake = 100

        if '-' in hn:  # 馬連（馬番 "3-7" 形式）
            a, b2 = hn.split('-')
            pa, pb = race_res.get(a), race_res.get(b2)
            # レース結果が取れていない場合は未確定
            if not race_res:
                book(is_today, f'▶ {kind} {hn} {name} ({venue}{rno}R) → 結果未確定')
                continue
            fo = umaren_final.get(rid, {}).get(
                f'{int(a):02d}{int(b2):02d}')
            if fo is None:
                fo = float(b.get('odds', 0) or 0)
            refund = next((p_ for p_ in (pa, pb) if p_ in REFUND_POS), None)
            if refund:  # 片方でも出走取消・競走除外なら返還
                payout, pos_rec, mark = stake, refund, '返還'
            else:
                key = '-'.join(str(n) for n in sorted((int(a), int(b2))))
                y = pay.get('馬連', {}).get(key)  # 払戻表（2着同着なら2組とも載る）
                hit = y is not None or key in _umaren_winners(race_res)
                if y is not None:
                    fo = y / 100
                payout = round(fo * stake) if hit else 0
                pos_rec, mark = (1 if hit else 0), ('🎯' if hit else '✗')
            res_s = f'{_pos_s(pa)}/{_pos_s(pb)}'
            book(is_today,
                 f'▶ {kind} {hn} ({venue}{rno}R) {fo:.1f}倍 → {res_s} {mark} {payout-stake:+,}円',
                 dict(date=bdate, race_id=rid, horse_num=hn, horse_name=name,
                      kind=kind, pos=pos_rec, final_odds=fo, stake=stake, payout=payout))
            continue

        pos = race_res.get(hn)
        if pos is None:
            book(is_today, f'▶ {kind} {name} ({venue}{rno}R) → 結果未確定')
            continue
        fo = odds_final.get(rid, {}).get(int(hn), (None,))[0]
        if fo is None:
            fo = float(b.get('odds', 0) or 0)  # 取得失敗時は記録時オッズで代用
        if pos in REFUND_POS:  # 出走取消・競走除外 → 返還
            payout, mark = stake, '返還'
        else:  # 競走中止・失格は pos が文字列のまま → 外れ
            y = pay.get('単勝', {}).get(str(int(hn)))  # 払戻表（1着同着なら2頭とも載る）
            hit = y is not None or pos == 1
            if y is not None:
                fo = y / 100
            payout = round(fo * stake) if hit else 0
            mark = '🎯' if hit else '✗'
        pl = payout - stake
        hn_s = f'{int(hn)}番 ' if str(hn).isdigit() else ''
        book(is_today,
             f'▶ {kind} {hn_s}{name} ({venue}{rno}R) {fo:.1f}倍 → {_pos_s(pos)} {mark} {pl:+,}円',
             dict(date=bdate, race_id=rid, horse_num=hn, horse_name=name,
                  kind=kind, pos=pos, final_odds=fo, stake=stake, payout=payout))

    if settle_rows:
        append_settle_log(settle_rows)

    inv, ret, _, pending = tot[True]
    p_inv, p_ret, p_n, _ = tot[False]
    if past_lines:
        print('【前日以前の未精算分】')
        print('\n'.join(past_lines))
    extra = (f'📝 追加精算 {p_n}点（前日以前の未精算分：投資 {p_inv:,}円 / 回収 {p_ret:,}円）'
             if p_n else '')
    cum = cumulative_summary(target)

    if n_today == 0:
        # 開催日でなければ、追加精算があった時だけ通知（非開催日に空のメッセージを送らない）
        from scraper import get_today_races
        if get_today_races():
            print('本日の買い推奨はありませんでした。')
            msg_lines = [f'🏁 **{label} の買い目結果**',
                         '本日は期待値条件を満たすレースがなく、全て見送りでした。']
        elif p_n:
            msg_lines = ['🏁 **前日以前の買い目の追加精算**']
        else:
            print('本日は開催なし。通知しません。')
            if cum:
                print(cum)
            return
    else:
        summary = ''
        if inv > 0:
            roi = ret / inv * 100
            emoji = '🎉' if roi >= 100 else '📉'
            summary = f'{emoji} 投資 {inv:,}円 / 回収 {ret:,}円 / **回収率 {roi:.0f}%**'
        msg_lines = [f'🏁 **{label} の記録用の買い目の答え合わせ**（記録上の金額・購入非推奨）'] + lines
        if summary:
            msg_lines.append('―――――――――――')
            msg_lines.append(summary)
        if pending:
            msg_lines.append(f'（結果未確定 {pending}点は次回集計）')
    if extra:
        msg_lines.append(extra)
    if cum and (n_today or p_n):
        msg_lines.append(f'📈 {cum}')
    msg = '\n'.join(msg_lines)

    print()
    print(msg)
    if args.discord:
        send_discord(msg)


if __name__ == '__main__':
    main()
