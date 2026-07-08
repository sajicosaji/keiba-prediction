"""
各テスト年のリークなし予測結果を1つのCSVに保存する。
列: year, race_id, horse_num, pred_rank, pos, odds, pop, grade, field
これを使えば人気帯フィルタ等の分析を特徴量再生成なしで高速に回せる。
使い方: python save_picks.py 2023 2024 2025
出力: <scratch>/picks.csv
"""
import sys, json
import numpy as np, pandas as pd
import lightgbm as lgb
from pathlib import Path
from train_model import (
    FEATURE_COLS, add_horse_history_features, engineer_features,
    compute_stats, compute_venue_stats, compute_rolling_entity_top3, compute_frame_bias,
    compute_jockey_trainer_stats, _compute_group_sizes,
    _RYOYO_SIRES, _STAY_SIRES, _SPEED_SIRES,
)
from analysis import parse_grade

DATA_DIR = Path(__file__).parent.parent / 'data'
RACES_CSV = DATA_DIR / 'races.csv'
SCRATCH = DATA_DIR / 'odds'
JRA_VENUES = {'01','02','03','04','05','06','07','08','09','10'}


def build_features():
    print('Loading data...', flush=True)
    df = pd.read_csv(RACES_CSV, dtype=str, on_bad_lines='skip')
    df['_vc'] = df['race_id'].astype(str).str[4:6]
    df = df[df['_vc'].isin(JRA_VENUES)].copy()
    df['finishing_pos_num'] = pd.to_numeric(df['finishing_pos'], errors='coerce')
    df = df.dropna(subset=['finishing_pos_num'])
    df['finishing_pos_num'] = df['finishing_pos_num'].astype(int)
    df['_year'] = df['race_id'].astype(str).str[:4].astype(int)
    print('Computing features (leak-free, once)...', flush=True)
    df_feat = add_horse_history_features(df)
    df_feat['jockey_recent60_top3'] = compute_rolling_entity_top3(df_feat, 'jockey')
    df_feat['trainer_recent60_top3'] = compute_rolling_entity_top3(df_feat, 'trainer')
    ped = DATA_DIR / 'pedigrees.csv'
    if ped.exists():
        pdf = pd.read_csv(ped, dtype=str)
        df_feat = df_feat.merge(pdf[['horse_id', 'sire', 'dam_sire']], on='horse_id', how='left')
        df_feat['sire_ryoyo'] = df_feat['sire'].apply(lambda s: float(str(s) in _RYOYO_SIRES))
        df_feat['sire_stay'] = df_feat['sire'].apply(lambda s: float(str(s) in _STAY_SIRES))
        df_feat['sire_speed'] = df_feat['sire'].apply(lambda s: float(str(s) in _SPEED_SIRES))
        df_feat['dam_sire_ryoyo'] = df_feat['dam_sire'].apply(lambda s: float(str(s) in _RYOYO_SIRES))
        df_feat = df_feat.drop(columns=['sire', 'dam_sire'], errors='ignore')
    else:
        for c in ['sire_ryoyo', 'sire_stay', 'sire_speed', 'dam_sire_ryoyo']:
            df_feat[c] = np.nan
    return df, df_feat


def year_rows(df, df_feat, ty):
    train_df = df[df['_year'] < ty].copy()
    if train_df.empty:
        return None
    oj = SCRATCH / f'odds_{ty}.json'
    if not oj.exists():
        return None
    js, ts = compute_stats(train_df)
    jv, tv = compute_venue_stats(train_df)
    fb = compute_frame_bias(train_df)
    jt = compute_jockey_trainer_stats(train_df)
    feat = engineer_features(df_feat.copy(), js, ts, jv, tv, fb, jt_stats=jt)
    tr = feat[feat['_year'] < ty].sort_values('race_id').reset_index(drop=True)
    med = tr[FEATURE_COLS].median()
    Xtr = tr[FEATURE_COLS].fillna(med).values
    tr['_mx'] = tr.groupby('race_id')['finishing_pos_num'].transform('max')
    ytr = (tr['_mx'] - tr['finishing_pos_num']).astype(int).values
    gtr = _compute_group_sizes(tr)
    m = lgb.LGBMRanker(objective='lambdarank', metric='ndcg', n_estimators=500,
                       learning_rate=0.05, max_depth=6, num_leaves=63,
                       n_jobs=-1, verbose=-1, random_state=42)
    m.fit(Xtr, ytr, group=gtr)
    test = feat[feat['_year'] == ty].copy()
    test['pred_score'] = m.predict(test[FEATURE_COLS].fillna(med).values)
    test['pred_rank'] = test.groupby('race_id')['pred_score'].rank(ascending=False, method='min').astype(int)
    odds_map = json.load(open(oj, encoding='utf-8'))
    def _o(row, idx):
        d = odds_map.get(str(row['race_id']), {})
        hn = str(row['horse_num'])
        if not hn.isdigit():
            return np.nan
        v = d.get(f'{int(hn):02d}')
        try:
            return float(v[idx])
        except Exception:
            return np.nan
    test['odds'] = test.apply(lambda r: _o(r, 0), axis=1)
    test['pop'] = test.apply(lambda r: _o(r, 1), axis=1)
    test['grade'] = test['race_name'].apply(parse_grade)
    test['field'] = test.groupby('race_id')['horse_num'].transform('count')
    out = test[['race_id', 'horse_num', 'pred_rank', 'pred_score', 'finishing_pos_num', 'odds', 'pop', 'grade', 'field']].copy()
    out['year'] = ty
    out = out.rename(columns={'finishing_pos_num': 'pos'})
    return out


def main():
    years = [int(a) for a in sys.argv[1:]] if len(sys.argv) > 1 else [2023, 2024, 2025]
    df, df_feat = build_features()
    frames = []
    for ty in years:
        r = year_rows(df, df_feat, ty)
        if r is not None:
            frames.append(r)
            print(f'{ty}: {r["race_id"].nunique()} races, {len(r)} rows', flush=True)
    allrows = pd.concat(frames, ignore_index=True)
    outpath = SCRATCH / 'picks.csv'
    allrows.to_csv(outpath, index=False, encoding='utf-8')
    print(f'SAVED {outpath} ({len(allrows)} rows)', flush=True)


if __name__ == '__main__':
    main()
