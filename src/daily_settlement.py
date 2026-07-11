"""
当日の買い目精算スクリプト（GitHub Actions から夕方に自動実行）

bet_log.csv からその日の買い推奨を取り出し、レース結果と確定オッズを
netkeiba から取得して「的中・配当・回収率」を計算し、Discordへ通知する。
精算結果は data/settle_log.csv に蓄積され、累計成績も併せて表示する。

使い方: python daily_settlement.py                    # 本日分を精算（表示のみ）
        python daily_settlement.py --discord          # Discordにも送信
        python daily_settlement.py --date 2026-07-11  # 日付指定
※金額は1点100円のフラット賭け換算。
"""
import os
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


def fetch_result_live(race_id):
    """race.netkeiba の結果ページから {馬番: 着順} を取得。

    db.netkeiba のレースページは当日中に更新されないことがあるため、
    当日精算では必ずこちら（レース確定直後から見られる）を先に使う。
    """
    import re
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
                    pos_txt = tds[0].get_text(strip=True)
                    num_txt = tds[2].get_text(strip=True)
                    if pos_txt.isdigit() and num_txt.isdigit():
                        out[num_txt] = int(pos_txt)
        return out
    except Exception as e:
        print(f'  結果取得エラー {race_id}: {e}')
        return {}

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


def cumulative_summary():
    if not SETTLE_LOG.exists():
        return ''
    df = pd.read_csv(SETTLE_LOG, encoding='utf-8-sig')
    inv = df['stake'].sum()
    ret = df['payout'].sum()
    hit = (df['payout'] > 0).sum()
    if inv <= 0:
        return ''
    return (f'累計: {len(df)}点 的中{hit}回 投資{inv:,.0f}円 '
            f'回収{ret:,.0f}円 回収率{ret/inv*100:.1f}%')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--discord', action='store_true')
    ap.add_argument('--date', default=None, help='YYYY-MM-DD（デフォルト: 本日JST）')
    args = ap.parse_args()

    target = args.date or datetime.now(JST).strftime('%Y-%m-%d')
    label  = f'{int(target[5:7])}/{int(target[8:10])}'
    print(f'精算対象日: {target}')

    bets = pd.DataFrame()
    if BET_LOG.exists():
        bets = pd.read_csv(BET_LOG, dtype=str, encoding='utf-8-sig')
        bets = bets[bets['logged_at'].str.startswith(target)]
        bets = bets.drop_duplicates(subset=['race_id', 'horse_num'], keep='last')
    if bets.empty:
        # 開催日でなければ黙って終了（非開催日にメッセージを送らない）
        from scraper import get_today_races
        if not get_today_races():
            print('本日は開催なし。通知しません。')
            return
        print('本日の買い推奨はありませんでした。')
        if args.discord:
            send_discord(f'🏁 **{label} の買い目結果**\n'
                         f'本日は期待値条件を満たすレースがなく、全て見送りでした。')
        return

    print(f'  本日の買い目: {len(bets)}点')

    # レースごとに結果と確定オッズを取得
    results, odds_final, umaren_final = {}, {}, {}
    has_umaren = (bets['kind'] == '馬連').any()
    for rid in bets['race_id'].unique():
        # 当日反映される結果ページを優先、ダメならdbページ
        res_map = fetch_result_live(str(rid))
        if not res_map:
            rows = scrape_race_result(str(rid))
            res_map = {str(r.get('horse_num')): r.get('finishing_pos')
                       for r in rows} if rows else {}
        if res_map:
            results[rid] = res_map
        odds_final[rid] = fetch_live_odds(str(rid))
        if has_umaren and (bets[(bets['race_id'] == rid) & (bets['kind'] == '馬連')]).shape[0]:
            umaren_final[rid] = fetch_combo_odds(str(rid), '4')

    lines, settle_rows = [], []
    inv = ret = 0
    pending = 0
    for _, b in bets.iterrows():
        rid, hn = str(b['race_id']), str(b['horse_num'])
        name = str(b.get('horse_name', ''))
        kind = str(b.get('kind', ''))
        venue = VENUE_NAME.get(rid[4:6], '')
        rno   = int(rid[10:12]) if rid[10:12].isdigit() else '?'
        race_res = results.get(rid, {})
        stake = 100

        if '-' in hn:  # 馬連（馬番 "3-7" 形式）
            a, b2 = hn.split('-')
            pa = pd.to_numeric(race_res.get(a), errors='coerce')
            pb = pd.to_numeric(race_res.get(b2), errors='coerce')
            # レース結果が取れていない場合は未確定
            if not race_res:
                pending += 1
                lines.append(f'▶ {kind} {hn} {name} ({venue}{rno}R) → 結果未確定')
                continue
            top2 = {n for n, p_ in race_res.items()
                    if pd.to_numeric(p_, errors='coerce') in (1, 2)}
            hit = {a, b2} == top2
            fo = umaren_final.get(rid, {}).get(
                f'{int(a):02d}{int(b2):02d}')
            if fo is None:
                fo = float(b.get('odds', 0) or 0)
            payout = round(fo * 100) if hit else 0
            inv += stake
            ret += payout
            mark = '🎯' if hit else '✗'
            res_s = f'{int(pa)}着/{int(pb)}着' if pd.notna(pa) and pd.notna(pb) else '着外含む'
            lines.append(f'▶ {kind} {hn} ({venue}{rno}R) {fo:.1f}倍 → {res_s} {mark} {payout-stake:+,}円')
            settle_rows.append(dict(date=target, race_id=rid, horse_num=hn, horse_name=name,
                                    kind=kind, pos=(1 if hit else 0), final_odds=fo,
                                    stake=stake, payout=payout))
            continue

        pos_raw = race_res.get(hn)
        pos = pd.to_numeric(pos_raw, errors='coerce')
        if pd.isna(pos):
            pending += 1
            lines.append(f'▶ {kind} {name} ({venue}{rno}R) → 結果未確定')
            continue
        pos = int(pos)
        fo = odds_final.get(rid, {}).get(int(hn), (None,))[0]
        if fo is None:
            fo = float(b.get('odds', 0) or 0)  # 取得失敗時は記録時オッズで代用
        payout = round(fo * 100) if pos == 1 else 0
        inv += stake
        ret += payout
        mark = '🎯' if pos == 1 else '✗'
        pl = payout - stake
        hn_s = f'{int(hn)}番 ' if str(hn).isdigit() else ''
        lines.append(f'▶ {kind} {hn_s}{name} ({venue}{rno}R) {fo:.1f}倍 → {pos}着 {mark} {pl:+,}円')
        settle_rows.append(dict(date=target, race_id=rid, horse_num=hn, horse_name=name,
                                kind=kind, pos=pos, final_odds=fo, stake=stake, payout=payout))

    if settle_rows:
        append_settle_log(settle_rows)

    summary = ''
    if inv > 0:
        roi = ret / inv * 100
        emoji = '🎉' if roi >= 100 else '📉'
        summary = f'{emoji} 投資 {inv:,}円 / 回収 {ret:,}円 / **回収率 {roi:.0f}%**'
    cum = cumulative_summary()

    msg_lines = [f'🏁 **{label} の買い目結果**（1点100円換算）'] + lines
    if summary:
        msg_lines.append('―――――――――――')
        msg_lines.append(summary)
    if pending:
        msg_lines.append(f'（結果未確定 {pending}点は次回集計）')
    if cum:
        msg_lines.append(f'📈 {cum}')
    msg = '\n'.join(msg_lines)

    print()
    print(msg)
    if args.discord:
        send_discord(msg)


if __name__ == '__main__':
    main()
