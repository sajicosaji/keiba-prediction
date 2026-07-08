"""
リークなし回収率バックテスト＋マイニング
- テスト年を除いて再訓練 → テスト年で予測（リークフリー）
- odds_{year}.json（API取得の正しいオッズ・人気）で回収率を算出
- クラス/頭数/◎の人気帯（モデルと市場の一致度）で条件別マイニング
使い方: python backtest_roi.py --test-year 2025
"""
import argparse, json
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--test-year', type=int, default=2025)
    args = ap.parse_args()
    ty = args.test_year

    print('Loading data...')
    df = pd.read_csv(RACES_CSV, dtype=str, on_bad_lines='skip')
    df['_vc'] = df['race_id'].astype(str).str[4:6]
    df = df[df['_vc'].isin(JRA_VENUES)].copy()
    df['finishing_pos_num'] = pd.to_numeric(df['finishing_pos'], errors='coerce')
    df = df.dropna(subset=['finishing_pos_num'])
    df['finishing_pos_num'] = df['finishing_pos_num'].astype(int)
    df['_year'] = df['race_id'].astype(str).str[:4].astype(int)

    print('Computing features (leak-free)...')
    df_feat = add_horse_history_features(df)
    df_feat['jockey_recent60_top3'] = compute_rolling_entity_top3(df_feat, 'jockey')
    df_feat['trainer_recent60_top3'] = compute_rolling_entity_top3(df_feat, 'trainer')
    train_df = df[df['_year'] < ty].copy()
    jockey_stats, trainer_stats = compute_stats(train_df)
    jockey_venue, trainer_venue = compute_venue_stats(train_df)
    frame_bias = compute_frame_bias(train_df)
    jt_stats = compute_jockey_trainer_stats(train_df)
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
    df_feat = engineer_features(df_feat, jockey_stats, trainer_stats,
                                jockey_venue, trainer_venue, frame_bias, jt_stats=jt_stats)

    # --- リークなし再訓練（テスト年より前だけで学習）---
    print(f'Retraining on <{ty} (leak-free)...')
    tr = df_feat[df_feat['_year'] < ty].sort_values('race_id').reset_index(drop=True)
    med = tr[FEATURE_COLS].median()
    Xtr = tr[FEATURE_COLS].fillna(med).values
    tr['_mx'] = tr.groupby('race_id')['finishing_pos_num'].transform('max')
    ytr = (tr['_mx'] - tr['finishing_pos_num']).astype(int).values
    gtr = _compute_group_sizes(tr)
    m = lgb.LGBMRanker(objective='lambdarank', metric='ndcg', n_estimators=500,
                       learning_rate=0.05, max_depth=6, num_leaves=63,
                       n_jobs=-1, verbose=-1, random_state=42)
    m.fit(Xtr, ytr, group=gtr)

    test = df_feat[df_feat['_year'] == ty].copy()
    test['pred_score'] = m.predict(test[FEATURE_COLS].fillna(med).values)
    test['pred_rank'] = test.groupby('race_id')['pred_score'].rank(ascending=False, method='min').astype(int)

    # --- 正しいオッズ・人気を結合（API取得分）---
    odds_map = json.load(open(SCRATCH / f'odds_{ty}.json', encoding='utf-8'))
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
    test['odds_num'] = test.apply(lambda r: _o(r, 0), axis=1)
    test['pop_num'] = test.apply(lambda r: _o(r, 1), axis=1)
    test['_grade'] = test['race_name'].apply(parse_grade)
    test['_field'] = test.groupby('race_id')['horse_num'].transform('count')
    test = test.dropna(subset=['odds_num'])
    nR = test['race_id'].nunique()

    print(f'\n{"="*66}\n  リークなし回収率バックテスト {ty}年  {nR:,}レース\n{"="*66}')

    def block(sub, label):
        pick = sub[sub['pred_rank'] == 1]
        n = len(pick)
        if n == 0:
            print(f'  {label:<20} R数0'); return
        win = pick[pick['finishing_pos_num'] == 1]
        show = pick[pick['finishing_pos_num'] <= 3]
        roi = win['odds_num'].sum() * 100 / (n * 100) * 100
        print(f'  {label:<20}{n:>6}R  勝率{len(win)/n*100:4.1f}%  複勝率{len(show)/n*100:4.1f}%  単勝回収{roi:6.1f}%')

    print('\n【全体】◎=モデル予測1位を単勝100円')
    block(test, '全レース')
    print('\n【クラス別】')
    for g in ['G1', 'G2', 'G3', 'L', 'OP', '条件']:
        block(test[test['_grade'] == g], g)
    print('\n【頭数別】')
    for lo, hi, lb in [(1,9,'〜8頭'),(9,13,'9-12頭'),(13,17,'13-16頭'),(17,30,'17頭〜')]:
        block(test[(test['_field'] >= lo) & (test['_field'] < hi)], lb)
    print('\n【◎が何番人気か（モデルと市場の一致度）】')
    pick = test[test['pred_rank'] == 1]
    for lo, hi, lb in [(1,2,'◎=1番人気'),(2,4,'◎=2-3番人気'),(4,7,'◎=4-6番人気'),(7,99,'◎=7番人気以下')]:
        sub = pick[(pick['pop_num'] >= lo) & (pick['pop_num'] < hi)]
        n = len(sub)
        if n == 0:
            print(f'  {lb:<20} R数0'); continue
        win = sub[sub['finishing_pos_num'] == 1]
        show = sub[sub['finishing_pos_num'] <= 3]
        roi = win['odds_num'].sum() * 100 / (n * 100) * 100
        print(f'  {lb:<20}{n:>6}R  勝率{len(win)/n*100:4.1f}%  複勝率{len(show)/n*100:4.1f}%  単勝回収{roi:6.1f}%')
    print(f'\n{"="*66}')


if __name__ == '__main__':
    main()
