"""
データ自動更新スクリプト

races.csv の最新日付を確認し、今日までの不足分を自動収集する。
毎週〜毎月実行することで常に最新状態を維持できる。

使い方:
  python update_data.py           # 不足分を自動収集
  python update_data.py --dry-run # 何を収集するか確認だけ（実行しない）
"""
import argparse
import csv
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

DATA_DIR  = Path(__file__).parent.parent / 'data'
RACES_CSV = DATA_DIR / 'races.csv'
LOG_FILE  = DATA_DIR / 'update_log.txt'


JRA_VENUES = {'01','02','03','04','05','06','07','08','09','10'}

def get_latest_collected_date():
    """races.csvに収録されているJRAレースの最新日付（YYYYMMDD）を返す"""
    if not RACES_CSV.exists():
        return None
    latest = '20220101'
    with open(RACES_CSV, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rid = str(row.get('race_id', '') or '')
            # JRAレースのみ（venue code 01-10）
            if len(rid) >= 6 and rid[4:6] not in JRA_VENUES:
                continue
            d = rid[:8]  # race_idの先頭8桁がYYYYMMDD相当
            # 有効な日付チェック（月01-12、日01-31）
            if (d.isdigit() and len(d) == 8
                    and '01' <= d[4:6] <= '12'
                    and '01' <= d[6:8] <= '31'
                    and d > latest):
                latest = d
    return latest


def years_to_collect(latest_date_str):
    """収集が必要な年のリストを返す"""
    today = date.today()
    current_year = today.year

    if not latest_date_str:
        # races.csv が存在しない場合は直近2年分
        return list(range(current_year - 1, current_year + 1))

    latest_year = int(latest_date_str[:4])
    # 最新収集年〜今年 をすべて対象（既存IDはスキップされるので重複なし）
    return list(range(latest_year, current_year + 1))


def get_collected_count():
    """現在のraces.csvの行数（レース数ではなく馬数）を返す"""
    if not RACES_CSV.exists():
        return 0
    with open(RACES_CSV, encoding='utf-8') as f:
        return sum(1 for _ in f) - 1  # ヘッダー除く


def write_log(message):
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        ts = datetime.now().strftime('%Y-%m-%d %H:%M')
        f.write(f'[{ts}] {message}\n')


_SRC = Path(__file__).parent

def rebuild_stats():
    """horse_stats.csv を再構築"""
    print('\n[自動] horse_stats.csv を再構築中...')
    result = subprocess.run([sys.executable, str(_SRC / 'build_horse_stats.py')], capture_output=True, text=True)
    if result.returncode == 0:
        print('  完了')
    else:
        print(f'  エラー: {result.stderr[:200]}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='確認のみ（実行しない）')
    parser.add_argument('--skip-stats', action='store_true', help='horse_stats再構築をスキップ')
    args = parser.parse_args()

    print('=' * 55)
    print('  netkeiba データ自動更新')
    print('=' * 55)

    # 現在の状況確認
    latest = get_latest_collected_date()
    count  = get_collected_count()
    today  = date.today().strftime('%Y/%m/%d')

    print(f'  races.csv 現在の最新日付: {latest[:4]}/{latest[4:6]}/{latest[6:8]}' if latest else '  races.csv なし（初回）')
    print(f'  現在の収録データ数: {count:,}行')
    print(f'  本日の日付: {today}')

    years = years_to_collect(latest)
    print(f'\n  収集対象年: {years}')
    print(f'  ※ 既収集のレースは自動スキップされます')

    if args.dry_run:
        print('\n[dry-run] 実際の収集はしません。')
        return

    print('\n収集開始...')
    before = get_collected_count()

    # 最新データの月を渡して不要な再スキャンを省略
    start_month = int(latest[4:6]) if latest else 1

    # collect_data.py を呼び出し
    cmd = ([sys.executable, str(_SRC / 'collect_data.py'), '--years'] + [str(y) for y in years]
           + ['--start-month', str(start_month)])
    proc = subprocess.run(cmd)

    after = get_collected_count()
    added = after - before

    print(f'\n収集完了: {added:,}行 追加 → 合計 {after:,}行')
    write_log(f'更新完了: {added}行追加, 合計{after}行, 対象年={years}')

    if added > 0 and not args.skip_stats:
        rebuild_stats()
        print('\n=== 更新完了 ===')
        print('次は 3_モデル訓練.bat でモデルを再訓練してください。')
    elif added == 0:
        print('\n新しいレースはありませんでした（最新の状態です）。')

    print('=' * 55)


if __name__ == '__main__':
    main()
