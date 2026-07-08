"""
バックテスト・回収率検証スクリプト

オッズは予想ロジックには使わないが、バックテストでは回収率算出に使用する。

使い方:
  python backtest.py                    # 最新年をテスト年として検証
  python backtest.py --test-year 2024   # 2024年を検証
  python backtest.py --grade g1g2       # G1/G2のみ
"""
import argparse
import joblib
import numpy as np
import pandas as pd
from pathlib import Path

from train_model import (
    FEATURE_COLS, add_horse_history_features, engineer_features,
    compute_stats, compute_venue_stats, compute_rolling_entity_top3, compute_frame_bias,
    compute_jockey_trainer_stats, _RYOYO_SIRES, _STAY_SIRES, _SPEED_SIRES,
)
from analysis import parse_grade, VENUE_NAME

DATA_DIR   = Path(__file__).parent.parent / 'data'
RACES_CSV  = DATA_DIR / 'races.csv'
MODEL_PATH = DATA_DIR / 'model.pkl'
STATS_PATH = DATA_DIR / 'stats.pkl'
JRA_VENUES = {'01','02','03','04','05','06','07','08','09','10'}


def _load_models():
    obj = joblib.load(MODEL_PATH)
    if isinstance(obj, dict) and obj.get('type') == 'ensemble':
        return obj['models']
    return [obj]


def _predict(models, X):
    return np.mean([m.predict(X) for m in models], axis=0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--test-year', type=int, default=None)
    parser.add_argument('--grade', default='all',
                        choices=['all', 'g1g2', 'g3l', 'op', 'cond'])
    args = parser.parse_args()

    print('Loading data...')
    df = pd.read_csv(RACES_CSV, dtype=str, on_bad_lines='skip')
    df['_vc'] = df['race_id'].astype(str).str[4:6]
    df = df[df['_vc'].isin(JRA_VENUES)].copy()
    df['finishing_pos_num'] = pd.to_numeric(df['finishing_pos'], errors='coerce')
    df = df.dropna(subset=['finishing_pos_num'])
    df['finishing_pos_num'] = df['finishing_pos_num'].astype(int)
    df['_year'] = df['race_id'].astype(str).str[:4].astype(int)
    print(f'  Total: {len(df):,} rows / {df["_year"].unique().tolist()}')

    test_year = args.test_year or int(df['_year'].max())
    train_df  = df[df['_year'] < test_year].copy()
    test_rows = df[df['_year'] == test_year]
    if test_rows.empty:
        print(f'No data for {test_year}.')
        return
    print(f'  Train: up to {test_year-1} | Test: {test_year} ({len(test_rows):,} rows)')

    # --- Feature engineering ---
    print('Computing features (may take a few minutes)...')
    # Horse history uses shift(1), so processing full df is leakage-free
    df_feat = add_horse_history_features(df)

    # Rolling recent form (leakage-free: each row only sees past dates)
    print('  Computing rolling 60-day jockey/trainer stats...')
    df_feat['jockey_recent60_top3']  = compute_rolling_entity_top3(df_feat, 'jockey')
    df_feat['trainer_recent60_top3'] = compute_rolling_entity_top3(df_feat, 'trainer')

    # Stats from train data only (leakage-free)
    jockey_stats, trainer_stats = compute_stats(train_df)
    jockey_venue, trainer_venue = compute_venue_stats(train_df)
    frame_bias = compute_frame_bias(train_df)
    jt_stats   = compute_jockey_trainer_stats(train_df)
    print(f'  騎手×調教師コンビ: {len(jt_stats):,}組')

    # 血統特徴量（pedigrees.csv がある場合）
    PEDIGREE_CSV = DATA_DIR / 'pedigrees.csv'
    if PEDIGREE_CSV.exists():
        ped_df = pd.read_csv(PEDIGREE_CSV, dtype=str)
        df_feat = df_feat.merge(ped_df[['horse_id', 'sire', 'dam_sire']], on='horse_id', how='left')
        df_feat['sire_ryoyo']     = df_feat['sire'].apply(lambda s: float(str(s) in _RYOYO_SIRES))
        df_feat['sire_stay']      = df_feat['sire'].apply(lambda s: float(str(s) in _STAY_SIRES))
        df_feat['sire_speed']     = df_feat['sire'].apply(lambda s: float(str(s) in _SPEED_SIRES))
        df_feat['dam_sire_ryoyo'] = df_feat['dam_sire'].apply(lambda s: float(str(s) in _RYOYO_SIRES))
        df_feat = df_feat.drop(columns=['sire', 'dam_sire'], errors='ignore')
    else:
        for col in ['sire_ryoyo', 'sire_stay', 'sire_speed', 'dam_sire_ryoyo']:
            df_feat[col] = np.nan

    df_feat = engineer_features(df_feat, jockey_stats, trainer_stats,
                                jockey_venue, trainer_venue, frame_bias,
                                jt_stats=jt_stats)

    test = df_feat[df_feat['_year'] == test_year].copy()
    test['_grade_lbl'] = test['race_name'].apply(parse_grade)

    # Grade filter
    grade_sets = {
        'g1g2': {'G1', 'G2'}, 'g3l': {'G3', 'L'},
        'op': {'OP'}, 'cond': {'条件'},
    }
    if args.grade != 'all':
        test = test[test['_grade_lbl'].isin(grade_sets[args.grade])]

    n_races = test['race_id'].nunique()
    print(f'  Test set: {n_races:,} races / {len(test):,} rows')

    # --- Predict ---
    print('Loading model and predicting...')
    models = _load_models()
    X = test[FEATURE_COLS].fillna(test[FEATURE_COLS].median()).values
    test['pred_score'] = _predict(models, X)
    test['pred_rank'] = (
        test.groupby('race_id')['pred_score']
        .rank(ascending=False, method='min')
        .astype(int)
    )

    test['odds_num'] = pd.to_numeric(test['odds'], errors='coerce')

    # ---- Report ----
    W = 72
    grade_label = {'all': '全クラス', 'g1g2': 'G1/G2', 'g3l': 'G3/L',
                   'op': 'OP特別', 'cond': '条件戦'}[args.grade]

    print(f'\n{"="*W}')
    print(f'  バックテスト結果: {test_year}年 / {grade_label}')
    print(f'  {n_races:,}レース  {len(test):,}頭')
    print(f'{"="*W}')

    avg_field = test.groupby('race_id').size().mean()

    # Accuracy
    top1 = test[test['pred_rank'] == 1]
    top3 = test[test['pred_rank'] <= 3]
    win_hit  = (top1['finishing_pos_num'] == 1).mean()
    top3_hit = (top3['finishing_pos_num'] <= 3).mean()
    per_race_hit = (top3.groupby('race_id')['finishing_pos_num']
                    .apply(lambda x: (x <= 3).sum()).mean())

    print('\n【予測精度】')
    print(f'  1着的中率  : {win_hit:6.1%}  (理論値: {1/avg_field:.1%})')
    print(f'  top3的中率 : {top3_hit:6.1%}  (理論値: {3/avg_field:.1%})')
    print(f'  3着内平均  : {per_race_hit:.2f}頭 / レース (予測top3のうち)')

    # ROI simulation
    budget = 100
    print('\n【単勝回収率シミュレーション (◎=予測1位)】')
    won = top1[top1['finishing_pos_num'] == 1]['odds_num'].dropna()
    total_races = len(top1)
    revenue = won.sum() * budget
    invest  = total_races * budget
    roi = revenue / invest * 100 if invest > 0 else 0.0
    print(f'  投資: {invest:,}円  的中: {len(won)}回 ({len(won)/total_races:.1%})')
    print(f'  回収: {revenue:,.0f}円  回収率: {roi:.1f}%')

    # Grade breakdown
    print('\n【グレード別内訳】')
    print(f'  {"グレード":<6} {"R数":>5} {"1着的中":>8} {"単勝回収率":>10}')
    print(f'  {"-"*34}')
    for g in ['G1', 'G2', 'G3', 'L', 'OP', '条件']:
        gdf = test[test['_grade_lbl'] == g]
        if gdf.empty:
            continue
        g1 = gdf[gdf['pred_rank'] == 1]
        hit = (g1['finishing_pos_num'] == 1).mean() if not g1.empty else 0
        g_won = g1[g1['finishing_pos_num'] == 1]['odds_num'].dropna()
        g_roi = g_won.sum() * budget / (len(g1) * budget) * 100 if len(g1) > 0 else 0
        print(f'  {g:<6} {gdf["race_id"].nunique():>5,}R  {hit:>7.1%}  {g_roi:>8.1f}%')

    # Venue breakdown
    print('\n【会場別内訳 (top5)】')
    test['_vn'] = test['race_id'].astype(str).str[4:6].map(VENUE_NAME).fillna('?')
    venue_stats = []
    for vn, vdf in test.groupby('_vn'):
        v1 = vdf[vdf['pred_rank'] == 1]
        if v1.empty:
            continue
        hit = (v1['finishing_pos_num'] == 1).mean()
        n   = vdf['race_id'].nunique()
        venue_stats.append((n, vn, hit))
    for n, vn, hit in sorted(venue_stats, reverse=True)[:5]:
        print(f'  {vn}: {n}R  1着的中{hit:.1%}')

    # Feature importance
    if hasattr(models[0], 'feature_importances_'):
        print('\n【特徴量重要度 top12】')
        imp = pd.Series(models[0].feature_importances_, index=FEATURE_COLS)
        for feat, val in imp.sort_values(ascending=False).head(12).items():
            bar = '#' * (val * 30 // (imp.max() + 1))
            print(f'  {feat:<35s} {val:>5d} {bar}')

    print(f'\n{"="*W}\n')


if __name__ == '__main__':
    main()
