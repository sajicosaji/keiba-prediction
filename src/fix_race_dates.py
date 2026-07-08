"""
races.csv の race_date 修復スクリプト

race_date が「race_idの先頭8桁」の代用値になっている行（2023年以外のほぼ全行）を、
netkeiba から取得した実際の開催日に置き換える。

JRAの race_id は YYYY+会場(2)+回(2)+日(2)+R(2) 構造で、同じ開催キー
（先頭10桁）のレースは全て同一日。開催ごとに1レースだけ db.netkeiba を
取得すれば全行を修復できる（約640リクエスト・5〜10分）。

使い方: python fix_race_dates.py            # 実行
        python fix_race_dates.py --dry-run  # 対象件数の確認のみ
進捗は data/date_cache.json に保存され、中断しても続きから再開できる。
"""
import argparse
import json
import re
import time
from pathlib import Path

import pandas as pd
import requests

DATA_DIR   = Path(__file__).parent.parent / 'data'
RACES_CSV  = DATA_DIR / 'races.csv'
CACHE_PATH = DATA_DIR / 'date_cache.json'
JRA_VENUES = {'01','02','03','04','05','06','07','08','09','10'}
HEADERS = {'User-Agent': 'Mozilla/5.0'}


def fetch_real_date(race_id):
    """db.netkeiba のレースページから実開催日 YYYYMMDD を取得"""
    url = f'https://db.netkeiba.com/race/{race_id}/'
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.encoding = 'EUC-JP'
        m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', r.text)
        if m:
            return f'{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}'
    except Exception as e:
        print(f'  取得エラー {race_id}: {e}')
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--delay', type=float, default=0.5)
    args = ap.parse_args()

    print('races.csv 読み込み中...')
    df = pd.read_csv(RACES_CSV, dtype=str, on_bad_lines='skip')
    jra_mask = df['race_id'].astype(str).str[4:6].isin(JRA_VENUES)

    # fallback判定: race_date（先頭8桁）が race_id の先頭8桁と一致する行
    rd = df['race_date'].astype(str).str[:8]
    rid8 = df['race_id'].astype(str).str[:8]
    bad = jra_mask & (rd == rid8)
    df['_kaisai'] = df['race_id'].astype(str).str[:10]

    kaisai_keys = sorted(df.loc[bad, '_kaisai'].unique())
    print(f'  修復対象: {bad.sum():,}行 / {len(kaisai_keys)}開催')

    if args.dry_run:
        return

    cache = {}
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text(encoding='utf-8'))
        print(f'  キャッシュ済み: {len(cache)}開催')

    todo = [k for k in kaisai_keys if k not in cache]
    print(f'  取得予定: {len(todo)}開催（約{len(todo)*args.delay/60:.0f}分）')

    for i, key in enumerate(todo):
        # この開催の代表レース（第1レースを優先）
        rids = df.loc[df['_kaisai'] == key, 'race_id'].astype(str)
        rep = sorted(rids)[0]
        d = fetch_real_date(rep)
        if d:
            cache[key] = d
        if (i + 1) % 50 == 0:
            CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
            print(f'  {i+1}/{len(todo)}  ({key} → {d})', flush=True)
        time.sleep(args.delay)

    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
    print(f'  日付取得完了: {len(cache)}開催')

    # 置換適用
    mapped = df.loc[bad, '_kaisai'].map(cache)
    n_fix = mapped.notna().sum()
    df.loc[bad & df['_kaisai'].map(cache).notna(), 'race_date'] = mapped.dropna()
    df = df.drop(columns=['_kaisai'])

    # バックアップしてから保存
    backup = DATA_DIR / 'races_before_datefix.csv'
    if not backup.exists():
        RACES_CSV.replace(backup)
        print(f'  元ファイルを退避: {backup.name}')
    df.to_csv(RACES_CSV, index=False, encoding='utf-8')
    print(f'修復完了: {n_fix:,}行の race_date を実開催日に更新 → races.csv 保存')

    # 検証
    df2 = pd.read_csv(RACES_CSV, dtype=str, on_bad_lines='skip', usecols=['race_id', 'race_date'])
    jm = df2['race_id'].astype(str).str[4:6].isin(JRA_VENUES)
    still = (df2.loc[jm, 'race_date'].astype(str).str[:8] == df2.loc[jm, 'race_id'].astype(str).str[:8]).mean()
    print(f'  検証: JRA行のfallback残存率 {still*100:.1f}%（偶然一致を含む）')


if __name__ == '__main__':
    main()
