"""
買い推奨の実運用成績を確認するスクリプト

predict.py が data/bet_log.csv に記録した買い推奨と、races.csv の実結果を
突合して的中率・回収率を集計する。レース結果は 3_データ更新.bat 実行後に反映される。

使い方: python check_results.py            # 画面表示
        python check_results.py --discord  # Discordにも送信
※回収額は「記録時点のオッズ×100円」で計算した参考値。実際の払戻（確定オッズ）とは
  多少ズレる（発走までのオッズ変動分）。
"""
import os
import argparse
import pandas as pd
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / 'data'
BET_LOG  = DATA_DIR / 'bet_log.csv'
RACES    = DATA_DIR / 'races.csv'
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--discord', action='store_true', help='結果をDiscordにも送信')
    args = ap.parse_args()

    lines = []

    def out(s=''):
        lines.append(s)
        print(s)

    if not BET_LOG.exists():
        out('bet_log.csv がまだありません。予測実行時に買い推奨が出ると記録されます。')
        if args.discord:
            send_discord('📊 **買い推奨の成績**\nまだ買い推奨の記録がありません。')
        return

    bets = pd.read_csv(BET_LOG, dtype=str, encoding='utf-8-sig')
    bets = bets.drop_duplicates(subset=['race_id', 'horse_num'], keep='last')

    SETTLE_LOG = DATA_DIR / 'settle_log.csv'
    if not SETTLE_LOG.exists():
        out(f'精算記録がまだありません（買い推奨 {len(bets)}件は当日17:30に精算されます）。')
        if args.discord:
            send_discord('📊 **買い推奨の成績**\n' + '\n'.join(lines))
        return

    st = pd.read_csv(SETTLE_LOG, encoding='utf-8-sig')
    st = st.drop_duplicates(subset=['race_id', 'horse_num'], keep='last')
    settled_keys = set(zip(st['race_id'].astype(str), st['horse_num'].astype(str)))
    pending = sum(1 for _, b in bets.iterrows()
                  if (str(b['race_id']), str(b['horse_num'])) not in settled_keys)

    out('=' * 64)
    out('  買い推奨の実運用成績（100円/点・確定オッズで精算）')
    out('=' * 64)

    def block(sub, label):
        n = len(sub)
        if n == 0:
            return
        inv = sub['stake'].sum()
        ret = sub['payout'].sum()
        hit = (sub['payout'] > 0).sum()
        out(f'  {label:<12} {n:>4}点  的中{hit:>3}回 ({hit/n*100:4.1f}%)  '
            f'投資{inv:>8,.0f}円  回収{ret:>9,.0f}円  回収率{ret/inv*100:6.1f}%')

    block(st, '全体')
    for kind in st['kind'].unique():
        block(st[st['kind'] == kind], str(kind))
    if pending:
        out(f'  未精算: {pending}件')
    out('=' * 64)
    out('  ※検証時の期待回収率: ◎単勝167% / 穴単勝124% / 馬連は試験運用')

    if args.discord:
        body = '\n'.join(l for l in lines if not set(l.strip()) == {'='})
        send_discord('📊 **買い推奨の成績（週次サマリー）**\n```\n' + body + '\n```')


if __name__ == '__main__':
    main()
