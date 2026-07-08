"""
races.csv から馬別統計を構築するスクリプト

使い方:
  python build_horse_stats.py

出力: data/horse_stats.csv
  馬名ごとに通算成績・芝/ダ成績・距離帯成績・近5走を集計
"""
import re
import csv
import pandas as pd
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / 'data'
RACES_CSV    = DATA_DIR / 'races.csv'
HORSE_STATS  = DATA_DIR / 'horse_stats.csv'


def dist_band(d):
    try:
        d = int(d)
        if d <= 1400:   return 'short'
        if d <= 1800:   return 'mile'
        if d <= 2200:   return 'middle'
        return 'long'
    except Exception:
        return None


def main():
    print('races.csv 読み込み中...')
    df = pd.read_csv(RACES_CSV, dtype=str)

    df['pos']  = pd.to_numeric(df['finishing_pos'], errors='coerce')
    df['dist'] = pd.to_numeric(df['distance'], errors='coerce')
    df['date'] = pd.to_numeric(df['race_date'], errors='coerce')

    # 有効な着順データのみ
    df = df.dropna(subset=['pos', 'horse_name'])
    df['pos'] = df['pos'].astype(int)

    print(f'  有効行: {len(df):,}  馬数: {df["horse_name"].nunique():,}')

    records = []
    for horse_name, grp in df.groupby('horse_name'):
        grp = grp.sort_values('date', ascending=False)
        pos = grp['pos'].values

        def rates(sub):
            n = len(sub)
            if n == 0:
                return 0, 0.0, 0.0
            return n, int((sub == 1).sum()) / n, int((sub <= 3).sum()) / n

        # 通算
        cr, cwr, ctr = rates(pos)

        # 芝 / ダート
        is_turf = grp['surface'].values == '芝'
        is_dirt = grp['surface'].values == 'ダ'
        turf_n, turf_wr, turf_tr = rates(pos[is_turf])
        dirt_n, dirt_wr, dirt_tr = rates(pos[is_dirt])

        # 距離帯
        bands = grp['dist'].apply(dist_band).values
        band_stats = {}
        for band in ['short', 'mile', 'middle', 'long']:
            mask = bands == band
            n, wr, tr = rates(pos[mask])
            band_stats[band] = (n, wr, tr)

        # 近5走
        recent = list(pos[:5])
        while len(recent) < 5:
            recent.append('')

        rec = {
            'horse_name':     horse_name,
            'career_races':   cr,
            'career_win_rate': round(cwr, 4),
            'career_top3_rate': round(ctr, 4),
            'turf_races':     turf_n,
            'turf_win_rate':  round(turf_wr, 4),
            'turf_top3_rate': round(turf_tr, 4),
            'dirt_races':     dirt_n,
            'dirt_win_rate':  round(dirt_wr, 4),
            'dirt_top3_rate': round(dirt_tr, 4),
        }
        for band in ['short', 'mile', 'middle', 'long']:
            n, wr, tr = band_stats[band]
            rec[f'{band}_races']     = n
            rec[f'{band}_win_rate']  = round(wr, 4)
            rec[f'{band}_top3_rate'] = round(tr, 4)
        for i, p in enumerate(recent, 1):
            rec[f'r{i}'] = p

        records.append(rec)

    out = pd.DataFrame(records)
    out.to_csv(HORSE_STATS, index=False, encoding='utf-8')
    print(f'保存完了: {HORSE_STATS}  ({len(out):,}頭)')


if __name__ == '__main__':
    main()
