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
    # 同一レース・同一馬は最後の記録だけ採用（再実行の重複除去）
    bets = bets.drop_duplicates(subset=['race_id', 'horse_num'], keep='last').copy()
    bets['odds'] = pd.to_numeric(bets['odds'], errors='coerce')

    races = pd.read_csv(RACES, dtype=str, on_bad_lines='skip',
                        usecols=['race_id', 'horse_num', 'finishing_pos'])
    races['pos'] = pd.to_numeric(races['finishing_pos'], errors='coerce')
    res = races.dropna(subset=['pos']).set_index(['race_id', 'horse_num'])['pos']

    bets['pos'] = bets.set_index(['race_id', 'horse_num']).index.map(res)
    done    = bets[bets['pos'].notna()].copy()
    pending = len(bets) - len(done)

    out('=' * 64)
    out('  買い推奨の実運用成績（100円/点・記録時オッズで計算）')
    out('=' * 64)
    if done.empty:
        out(f'  結果待ち: {pending}件（データ更新後に反映されます）')
        if args.discord:
            send_discord('📊 **買い推奨の成績**\n' + '\n'.join(lines[2:]))
        return

    def block(sub, label):
        n = len(sub)
        if n == 0:
            return
        win = sub[sub['pos'] == 1]
        ret = win['odds'].sum() * 100
        roi = ret / (n * 100) * 100
        out(f'  {label:<12} {n:>4}点  的中{len(win):>3}回 ({len(win)/n*100:4.1f}%)  '
            f'投資{n*100:>8,}円  回収{ret:>9,.0f}円  回収率{roi:6.1f}%')

    block(done, '全体')
    for kind in done['kind'].unique():
        block(done[done['kind'] == kind], kind)
    if pending:
        out(f'  結果待ち: {pending}件')
    out('=' * 64)
    out('  ※検証時の期待回収率: ◎単勝168% / 穴単勝132%（2024-2025バックテスト）')

    if args.discord:
        body = '\n'.join(l for l in lines if not set(l.strip()) == {'='})
        send_discord('📊 **買い推奨の成績（週次サマリー）**\n```\n' + body + '\n```')


if __name__ == '__main__':
    main()
