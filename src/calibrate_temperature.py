"""
スコア→勝率変換の温度較正スクリプト（リークなし）

デプロイと同一構成（3シードアンサンブル・同一ハイパーパラメータ）のモデルを
「直近180日を除いたデータ」で訓練し、直近180日のレースをホールドアウトとして
softmax温度Tを対数尤度最大化で較正する。結果は data/calibration.json に保存され、
predict.py が勝率・期待値（EV）計算に使用する。

使い方: python calibrate_temperature.py
        python calibrate_temperature.py --holdout-days 180
"""
import argparse
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb

from train_model import (
    FEATURE_COLS, JRA_VENUES, add_horse_history_features, engineer_features,
    compute_stats, compute_venue_stats, compute_rolling_entity_top3,
    compute_frame_bias, compute_jockey_trainer_stats, _compute_group_sizes,
    _RYOYO_SIRES, _STAY_SIRES, _SPEED_SIRES,
)

DATA_DIR  = Path(__file__).parent.parent / 'data'
RACES_CSV = DATA_DIR / 'races.csv'
CAL_PATH  = DATA_DIR / 'calibration.json'

# train_model.py のデプロイ構成と揃えること
RANKER_PARAMS = dict(
    objective='lambdarank', metric='ndcg',
    n_estimators=1000, learning_rate=0.05, max_depth=6, num_leaves=63,
    min_child_samples=20, subsample=0.8, colsample_bytree=0.8,
    n_jobs=-1, verbose=-1,
)
SEEDS = [42, 123, 456]


def fit_temperature(races):
    """レースごとの (スコア配列, 勝ち馬index) から最適温度Tを求める"""
    best_T, best_ll = None, -1e18
    for T in np.arange(0.1, 3.01, 0.05):
        ll = 0.0
        for s, wi in races:
            e = np.exp((s - s.max()) / T)
            p = e / e.sum()
            ll += math.log(max(p[wi], 1e-12))
        if ll > best_ll:
            best_ll, best_T = ll, T
    return float(round(best_T, 2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--holdout-days', type=int, default=180)
    args = ap.parse_args()

    print('データ読み込み中...')
    df = pd.read_csv(RACES_CSV, dtype=str, on_bad_lines='skip')
    df = df[df['race_id'].astype(str).str[4:6].isin(JRA_VENUES)].copy()
    df['finishing_pos_num'] = pd.to_numeric(df['finishing_pos'], errors='coerce')
    df = df.dropna(subset=['finishing_pos_num'])
    df['finishing_pos_num'] = df['finishing_pos_num'].astype(int)
    df['_dt'] = pd.to_datetime(df['race_date'].astype(str).str[:8],
                               format='%Y%m%d', errors='coerce')
    df = df.dropna(subset=['_dt'])

    cutoff = df['_dt'].max() - pd.Timedelta(days=args.holdout_days)
    print(f'  ホールドアウト境界: {cutoff.date()} 以降 (直近{args.holdout_days}日)')

    print('特徴量計算中（リークなし・数分かかります）...')
    df_feat = add_horse_history_features(df)
    df_feat['jockey_recent60_top3']  = compute_rolling_entity_top3(df_feat, 'jockey')
    df_feat['trainer_recent60_top3'] = compute_rolling_entity_top3(df_feat, 'trainer')

    train_raw = df[df['_dt'] < cutoff]
    jockey_stats, trainer_stats = compute_stats(train_raw)
    jockey_venue, trainer_venue = compute_venue_stats(train_raw)
    frame_bias = compute_frame_bias(train_raw)
    jt_stats   = compute_jockey_trainer_stats(train_raw)

    ped = DATA_DIR / 'pedigrees.csv'
    if ped.exists():
        pdf = pd.read_csv(ped, dtype=str)
        df_feat = df_feat.merge(pdf[['horse_id', 'sire', 'dam_sire']], on='horse_id', how='left')
        df_feat['sire_ryoyo']     = df_feat['sire'].apply(lambda s: float(str(s) in _RYOYO_SIRES))
        df_feat['sire_stay']      = df_feat['sire'].apply(lambda s: float(str(s) in _STAY_SIRES))
        df_feat['sire_speed']     = df_feat['sire'].apply(lambda s: float(str(s) in _SPEED_SIRES))
        df_feat['dam_sire_ryoyo'] = df_feat['dam_sire'].apply(lambda s: float(str(s) in _RYOYO_SIRES))
        df_feat = df_feat.drop(columns=['sire', 'dam_sire'], errors='ignore')
    else:
        for c in ['sire_ryoyo', 'sire_stay', 'sire_speed', 'dam_sire_ryoyo']:
            df_feat[c] = np.nan

    df_feat = engineer_features(df_feat, jockey_stats, trainer_stats,
                                jockey_venue, trainer_venue, frame_bias,
                                jt_stats=jt_stats)

    tr = df_feat[df_feat['_dt'] < cutoff].sort_values('race_id').reset_index(drop=True)
    ho = df_feat[df_feat['_dt'] >= cutoff].copy()
    print(f'  訓練: {tr["race_id"].nunique():,}R / ホールドアウト: {ho["race_id"].nunique():,}R')

    med = tr[FEATURE_COLS].median()
    Xtr = tr[FEATURE_COLS].fillna(med).values
    tr['_mx'] = tr.groupby('race_id')['finishing_pos_num'].transform('max')
    ytr = (tr['_mx'] - tr['finishing_pos_num']).astype(int).values
    gtr = _compute_group_sizes(tr)

    print('デプロイ構成でアンサンブル訓練中（3シード）...')
    models = []
    for seed in SEEDS:
        m = lgb.LGBMRanker(**{**RANKER_PARAMS, 'random_state': seed})
        m.fit(Xtr, ytr, group=gtr)
        models.append(m)
        print(f'  seed={seed} 完了')

    Xho = ho[FEATURE_COLS].fillna(med).values
    ho['score'] = np.mean([m.predict(Xho) for m in models], axis=0)

    races = []
    for rid, g in ho.groupby('race_id'):
        s = g['score'].values
        w = np.where(g['finishing_pos_num'].values == 1)[0]
        if len(w) == 0 or len(s) < 5:
            continue
        races.append((s, w[0]))

    T = fit_temperature(races)
    print(f'\n最適温度 T = {T}  (較正 {len(races):,}レース)')

    # 較正の質チェック: ◎の平均予測勝率 vs 実勝率
    p_sum, hit, n = 0.0, 0, 0
    for s, wi in races:
        e = np.exp((s - s.max()) / T)
        p = e / e.sum()
        top = int(np.argmax(s))
        p_sum += p[top]
        n += 1
        if wi == top:
            hit += 1
    print(f'  ◎の平均予測勝率: {p_sum/n*100:.1f}%  /  実勝率: {hit/n*100:.1f}%')

    CAL_PATH.write_text(json.dumps({
        'T': T,
        'fitted_on': datetime.now().strftime('%Y-%m-%d'),
        'holdout_days': args.holdout_days,
        'holdout_races': len(races),
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n保存完了: {CAL_PATH}')


if __name__ == '__main__':
    main()
