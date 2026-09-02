"""
モデル訓練スクリプト

使い方:
  python train_model.py           # 通常訓練
  python train_model.py --tune    # Optuna ハイパーパラメータ最適化（30〜60分）
"""
import re
import argparse
import joblib
import numpy as np
import pandas as pd
from pathlib import Path
from sklearn.model_selection import GroupKFold
import lightgbm as lgb

DATA_DIR   = Path(__file__).parent.parent / 'data'
RACES_CSV  = DATA_DIR / 'races.csv'
MODEL_PATH = DATA_DIR / 'model.pkl'
STATS_PATH = DATA_DIR / 'stats.pkl'

# 予測対象はJRA（中央）のみ。races.csv にはNAR（地方）も混在しているが、
# バックテストで検証済みの構成に合わせ、訓練もJRA限定とする。
JRA_VENUES = {'01','02','03','04','05','06','07','08','09','10'}

FEATURE_COLS = [
    # 基本情報（オッズ・人気は市場予想のため除外）
    'frame_num', 'horse_num_num', 'sex', 'age', 'weight_carried_num',
    'surface_num', 'distance', 'track_num',
    'horse_weight_num', 'weight_change', 'field_size',
    # 季節性
    'race_month',
    # 枠番×会場バイアス
    'frame_venue_top3_rate',
    # 騎手・調教師（全体統計）
    'jockey_win_rate', 'jockey_top3_rate', 'trainer_win_rate', 'trainer_top3_rate',
    # 騎手・調教師（コース別統計）
    'jockey_venue_top3_rate', 'trainer_venue_top3_rate',
    # 直近60日ホットストリーク
    'jockey_recent60_top3', 'trainer_recent60_top3',
    # 騎手×調教師コンビ実績（信頼コンビは勝率が高い）
    'jockey_trainer_top3_rate',
    # 乗り替わり
    'jockey_change_flag',
    # 馬のキャリア成績
    'horse_recent3_avg', 'horse_career_top3_rate', 'horse_career_races',
    'career_best_pos', 'career_grade_top3_rate',
    'horse_same_surface_top3_rate', 'horse_same_dist_top3_rate',
    'horse_same_track_top3_rate',
    # 脚質
    'running_style_num',
    # 前走・ローテーション
    'prev_pos', 'prev2_pos', 'prev_days_interval',
    'prev_margin_num', 'form_trend', 'field_size_change',
    'dist_change',             # 前走からの距離変化（m）
    # 末脚
    'prev_last3f', 'prev2_last3f', 'horse_avg_last3f',
    'prev_last3f_rank_rel',
    # スピード指数
    'prev_speed_idx', 'horse_avg_speed_idx',
    'career_best_speed_idx',   # キャリアベストスピード指数（天井値）
    'speed_idx_vs_field',      # 自馬の平均スピード指数 − 出走メンバー平均
    # 斤量の相対値
    'weight_vs_field_avg',     # 自馬の斤量 − 出走メンバー平均斤量
    # 条件クラス変動
    'class_drop',
    # 初条件フラグ
    'is_first_surface', 'is_first_venue',
    # 体重トレンド
    'horse_weight_avg_change',
    # 血統特徴量（pedigrees.csv があれば有効、なければ NaN→LGBMがNaN分岐で処理）
    'sire_ryoyo', 'sire_stay', 'sire_speed', 'dam_sire_ryoyo',
    # コーナー通過順位（c1-c4データがあるレースのみ有効）
    'prev_c4_ratio',   # 前走4コーナーの相対位置（1.0=先頭、0.0=最後方）
    'prev_pos_gain',   # 前走1→4コーナーの位置変化（正=追い上げ、負=失速）
    # 騎手×馬コンビ実績
    'jockey_horse_pair_top3_rate',
]

# ---- 血統適性分類 -------------------------------------------------------
# 道悪・重馬場に強いサイヤーライン（良→重での成績維持率が高い系統）
_RYOYO_SIRES = frozenset({
    'ハービンジャー', 'キングカメハメハ', 'ルーラーシップ', 'ブラックタイド',
    'スクリーンヒーロー', 'モーリス', 'サトノクラウン', 'エフフォーリア',
    'フィエールマン', 'ジャスタウェイ', 'オルフェーヴル', 'マンハッタンカフェ',
    'Harbinger', 'King Kamehameha', 'Rulership',
})
# 長距離・ステイヤー適性が高いサイヤーライン
_STAY_SIRES = frozenset({
    'ディープインパクト', 'ステイゴールド', 'マンハッタンカフェ', 'フィエールマン',
    'サトノクラウン', 'オルフェーヴル', 'ゴールドシップ', 'キタサンブラック',
    'ブラックタイド', 'ドリームジャーニー', 'ワールドプレミア',
    'Deep Impact', 'Stay Gold',
})
# 短距離・スプリント適性が高いサイヤーライン
_SPEED_SIRES = frozenset({
    'ダイワメジャー', 'ロードカナロア', 'ドレフォン', 'カフェファラオ',
    'ニューイヤーズデイ', 'グレナディアガーズ', 'サウスヴィグラス', 'ヘニーヒューズ',
    'モーニン', 'カネヒキリ', 'アメリカンファラオ', 'シニスターミニスター',
    'Lord Kanaloa', 'Drefong', 'American Pharoah',
})

# グレード数値マップ: 高いほど格上
_GRADE_RANK = {'G1': 6, 'G2': 5, 'G3': 4, 'L': 3, 'OP': 2, '条件': 1}


def _grade_num(race_name):
    """レース名からグレードを数値化 (G1=6 ... 条件=1, 不明=0)"""
    n = str(race_name)
    if not n or n in ('nan', 'None'):
        return 0
    if re.search(r'G1|GI(?!I)|グランプリ', n): return 6
    if re.search(r'G2|GII(?!I)', n):           return 5
    if re.search(r'G3|GIII', n):               return 4
    if re.search(r'\(L\)|リステッド', n):       return 3
    if re.search(r'オープン|OP|特別', n):       return 2
    return 1  # 条件戦


def _dist_band_num(d):
    """距離帯を数値に変換 (0=短距離, 1=マイル, 2=中距離, 3=長距離, -1=不明)"""
    try:
        d = int(d)
        if d <= 1400: return 0
        if d <= 1800: return 1
        if d <= 2200: return 2
        return 3
    except Exception:
        return -1


def _parse_race_time_secs(s):
    """レースタイムを秒数に変換 (1:23.4 → 83.4、欠損→NaN)"""
    s = str(s).strip()
    if not s or s in ('nan', 'None', '---', '--', ''):
        return np.nan
    m = re.match(r'(\d+):(\d+\.\d+)', s)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    m = re.match(r'(\d+\.\d+)', s)
    if m:
        return float(m.group(1))
    return np.nan


def parse_sex_age(s):
    m = re.match(r'([牡牝セ騸])(\d+)', str(s))
    if m:
        return {'牡': 0, '牝': 1, 'セ': 2, '騸': 2}.get(m.group(1), -1), int(m.group(2))
    return -1, -1


def parse_horse_weight(s):
    m = re.match(r'(\d+)\(([+-]?\d+)\)', str(s))
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r'(\d+)', str(s))
    if m:
        return int(m.group(1)), 0
    return np.nan, 0


def _parse_margin(s):
    """着差テキストを馬身数値に変換（勝ち馬 / 同着 = 0.0）"""
    s = str(s).strip()
    if not s or s in ('nan', 'None', '---', '--', '同着', ''):
        return 0.0
    d = {'ハナ': 0.1, 'アタマ': 0.2, 'クビ': 0.3, '大差': 10.0}
    if s in d:
        return d[s]
    # "1.1/2" → 1.5
    m = re.match(r'^(\d+)\.(\d+)/(\d+)$', s)
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3))
    # "1/2" → 0.5
    m = re.match(r'^(\d+)/(\d+)$', s)
    if m:
        return int(m.group(1)) / int(m.group(2))
    try:
        return float(s)
    except Exception:
        return np.nan


def compute_rolling_entity_top3(df, entity_col, days=60, min_races=5):
    """
    各レース日時点での直近 days 日間の複勝率をリーク無しで計算する。
    entity_col: 'jockey' または 'trainer'
    """
    df = df.copy()
    df['_dt_rol'] = pd.to_datetime(
        df['race_date'].astype(str).str[:8], format='%Y%m%d', errors='coerce'
    )
    df['_t3_rol'] = (pd.to_numeric(df['finishing_pos'], errors='coerce') <= 3).astype(float)
    df_v = df.dropna(subset=['_dt_rol'])

    result = pd.Series(np.nan, index=df.index)
    for date in sorted(df_v['_dt_rol'].unique()):
        cutoff  = date - pd.Timedelta(days=days)
        past    = df_v[(df_v['_dt_rol'] >= cutoff) & (df_v['_dt_rol'] < date)]
        if past.empty:
            continue
        agg  = past.groupby(entity_col)['_t3_rol'].agg(['mean', 'count'])
        agg  = agg.loc[agg['count'] >= min_races, 'mean']
        idxs = df_v[df_v['_dt_rol'] == date].index
        result.loc[idxs] = df_v.loc[idxs, entity_col].map(agg).values

    return result


def compute_stats(df):
    """騎手・調教師の全体成績を集計"""
    valid = df[df['finishing_pos_num'] > 0].copy()

    def agg(col):
        g = valid.groupby(col)['finishing_pos_num'].agg(
            total='count',
            wins=lambda x: (x == 1).sum(),
            top3=lambda x: (x <= 3).sum(),
        )
        g['win_rate']  = g['wins']  / g['total']
        g['top3_rate'] = g['top3'] / g['total']
        return g[['win_rate', 'top3_rate']]

    return agg('jockey'), agg('trainer')


def compute_venue_stats(df, min_count=5):
    """騎手・調教師のコース別3着内率（サンプル数 min_count 以上のみ採用）"""
    valid = df[df['finishing_pos_num'] > 0].copy()
    valid['venue_code'] = valid['race_id'].astype(str).str[4:6]

    def agg_venue(col):
        g = valid.groupby([col, 'venue_code'])['finishing_pos_num'].agg(
            total='count',
            top3=lambda x: (x <= 3).sum(),
        )
        g['top3_rate'] = g['top3'] / g['total']
        return g.loc[g['total'] >= min_count, 'top3_rate']

    return agg_venue('jockey'), agg_venue('trainer')


def compute_frame_bias(df, min_count=30):
    """枠番×会場×馬場の3着内率（ポジションバイアス）"""
    valid = df[df['finishing_pos_num'] > 0].copy()
    valid['_vc'] = valid['race_id'].astype(str).str[4:6]
    valid['_fn'] = pd.to_numeric(valid['frame'], errors='coerce')
    valid = valid.dropna(subset=['_fn'])
    valid['_fn'] = valid['_fn'].astype(int)
    g = valid.groupby(['_vc', 'surface', '_fn'])['finishing_pos_num'].agg(
        total='count', top3=lambda x: (x <= 3).sum()
    )
    g['top3_rate'] = g['top3'] / g['total']
    result = g.loc[g['total'] >= min_count, 'top3_rate']
    # Build string-key dict for fast lookup
    fb_dict = {f"{vc}_{surf}_{int(fn)}": v for (vc, surf, fn), v in result.items()}
    return fb_dict


def compute_jockey_trainer_stats(df, min_count=10):
    """
    騎手×調教師コンビの過去3着内率を集計。
    信頼コンビ（ホームトレーナー × エース騎手）は勝率が統計的に高い。
    min_count件以上のコンビのみ（それ未満は NaN → モデルが未知扱い）
    """
    valid = df[df['finishing_pos_num'] > 0].copy()
    g = valid.groupby(['jockey', 'trainer'])['finishing_pos_num'].agg(
        total='count', top3=lambda x: (x <= 3).sum()
    )
    g['top3_rate'] = g['top3'] / g['total']
    return g.loc[g['total'] >= min_count, 'top3_rate']


def add_horse_history_features(df):
    """
    各行の時点での馬の過去成績・前走情報を特徴量として計算（訓練用）。
    horse_name + race_id 昇順でソートし shift(1) ベースで計算するため
    データリークは発生しない。
    """
    df = df.copy()
    df['_rid_s']      = df['race_id'].astype(str)
    df['_dist_b']     = df['distance'].apply(_dist_band_num)
    df['_date_dt']    = pd.to_datetime(
        df['race_date'].astype(str).str[:8], format='%Y%m%d', errors='coerce'
    )
    df['_last3f_num'] = pd.to_numeric(df.get('last_3f', pd.Series(dtype=float)), errors='coerce')

    # ---- ソート前計算 -------------------------------------------------------

    # グレード
    df['_grade'] = df['race_name'].apply(_grade_num)

    # スピード指数: (par - race_time) / par * 100 + 80  (80=パー)
    df['_t_sec']  = df['time'].apply(_parse_race_time_secs)
    df['_dist_n'] = pd.to_numeric(df['distance'], errors='coerce')
    df['_fpos_n'] = pd.to_numeric(df['finishing_pos'], errors='coerce')
    _w   = df[(df['_fpos_n'] == 1) & df['_t_sec'].notna() & df['_dist_n'].notna()]
    _par = _w.groupby(['surface', '_dist_n'])['_t_sec'].median()
    _pd  = {f"{s}_{int(d)}": t for (s, d), t in _par.items()}
    df['_par_key']  = df['surface'] + '_' + df['_dist_n'].apply(
        lambda x: str(int(x)) if pd.notna(x) else ''
    )
    df['_par_val']  = df['_par_key'].map(_pd)
    df['_speed_idx'] = np.where(
        df['_par_val'].notna() & df['_t_sec'].notna() & (df['_par_val'] > 0),
        (df['_par_val'] - df['_t_sec']) / df['_par_val'] * 100 + 80,
        np.nan
    )

    # レース内上がり相対ランク (1.0=最速, 0.0=最遅)
    df['_l3f_rank'] = df.groupby('race_id')['_last3f_num'].rank(
        method='min', ascending=True, na_option='keep'
    )
    df['_race_sz'] = df.groupby('race_id')['horse_name'].transform('count')
    df['_l3f_rel'] = 1.0 - (df['_l3f_rank'] - 1) / (df['_race_sz'] - 1).clip(lower=1)

    # 頭数・着差・脚質・月
    df['_field_cnt'] = df['_race_sz']
    df['_margin_n']  = df['margin'].apply(_parse_margin)
    _smap = {'逃げ': 0.0, '先行': 1.0, '差し': 2.0, '追込': 3.0}
    df['_style_n'] = df['running_style'].map(_smap) if 'running_style' in df.columns else np.nan
    df['race_month'] = df['_date_dt'].dt.month.astype(float)

    # コーナー通過順位
    df['_c4'] = pd.to_numeric(df.get('c4_pos', pd.Series(dtype=float)), errors='coerce')
    df['_c1'] = pd.to_numeric(df.get('c1_pos', pd.Series(dtype=float)), errors='coerce')

    # ---- 時系列順ソート -----------------------------------------------------
    df = df.sort_values(['horse_name', '_rid_s']).reset_index(drop=True)

    grp     = df.groupby('horse_name', sort=False)
    grp_pos = grp['finishing_pos_num']

    # ---- ベクトル化できる特徴量 ---------------------------------------------
    df['prev_pos']          = grp_pos.shift(1)
    df['prev2_pos']         = grp_pos.shift(2)
    df['prev_margin_num']   = grp['_margin_n'].shift(1)
    df['running_style_num'] = grp['_style_n'].shift(1)

    df['horse_recent3_avg'] = grp_pos.transform(
        lambda x: x.shift(1).rolling(3, min_periods=1).mean()
    )

    # フォームトレンド: 前走 - 3走前 (負=改善, 正=悪化)
    df['form_trend'] = grp_pos.transform(lambda x: x.shift(1) - x.shift(3))

    # 通算3着内率（NaN-aware）
    def _top3_rate(x):
        s    = x.shift(1)
        cnt  = s.expanding().count().replace(0, np.nan)
        top3 = (s <= 3).where(s.notna()).expanding().sum()
        return top3 / cnt

    df['horse_career_top3_rate'] = grp_pos.transform(_top3_rate)
    df['horse_career_races']     = grp_pos.transform(
        lambda x: x.shift(1).expanding().count()
    )
    df['career_best_pos'] = grp_pos.transform(
        lambda x: x.shift(1).expanding().min()
    )

    # 前走からの日数
    date_s1  = grp['_date_dt'].shift(1)
    raw_days = (df['_date_dt'] - date_s1).dt.days.astype(float)
    raw_days[(raw_days <= 0) | (raw_days > 730)] = np.nan
    df['prev_days_interval'] = raw_days

    # 末脚系
    df['prev_last3f']          = grp['_last3f_num'].shift(1)
    df['prev2_last3f']         = grp['_last3f_num'].shift(2)
    df['horse_avg_last3f']     = grp['_last3f_num'].transform(
        lambda x: x.shift(1).rolling(3, min_periods=1).mean()
    )
    df['prev_last3f_rank_rel'] = grp['_l3f_rel'].shift(1)

    # スピード指数系
    df['prev_speed_idx']      = grp['_speed_idx'].shift(1)
    df['horse_avg_speed_idx'] = grp['_speed_idx'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean()
    )

    # 頭数変化（前走比）
    df['field_size_change'] = grp['_field_cnt'].transform(lambda x: x - x.shift(1))

    # 距離変化（前走との比較、m単位）
    df['dist_change'] = grp['_dist_n'].transform(lambda x: x - x.shift(1))

    # キャリアベストスピード指数（過去最高値 = 天井能力の指標）
    df['career_best_speed_idx'] = grp['_speed_idx'].transform(
        lambda x: x.shift(1).expanding().max()
    )

    # コーナー通過順位（前走）
    prev_field      = grp['_field_cnt'].shift(1)
    prev_c4         = grp['_c4'].shift(1)
    prev_c1         = grp['_c1'].shift(1)
    df['prev_c4_ratio'] = (prev_field - prev_c4) / (prev_field - 1).clip(lower=1)
    df['prev_pos_gain'] = prev_c1 - prev_c4  # 正=追い上げ、負=失速

    # 乗り替わりフラグ
    prev_jockey     = grp['jockey'].shift(1)
    jockey_changed  = (df['jockey'] != prev_jockey).astype(float)
    jockey_changed[prev_jockey.isna()] = np.nan
    df['jockey_change_flag'] = jockey_changed

    # ---- 条件付き: 馬単位ループ （同馬場・同距帯・同馬場状態・重賞実績） ------
    surf_rates  = np.full(len(df), np.nan)
    dist_rates  = np.full(len(df), np.nan)
    track_rates = np.full(len(df), np.nan)
    grade_top3  = np.full(len(df), np.nan)
    jh_rates    = np.full(len(df), np.nan)

    for _, grp_df in df.groupby('horse_name', sort=False):
        idxs      = grp_df.index.values
        pos_arr   = grp_df['finishing_pos_num'].values.astype(float)
        surf_arr  = grp_df['surface'].values
        dist_arr  = grp_df['_dist_b'].values
        track_arr = grp_df['track_condition'].values
        grd_arr   = grp_df['_grade'].values
        jky_arr   = grp_df['jockey'].values

        for i in range(1, len(idxs)):
            past_pos = pos_arr[:i]
            valid    = ~np.isnan(past_pos)

            s_mask = (surf_arr[:i] == surf_arr[i]) & valid
            if s_mask.sum() >= 1:
                surf_rates[idxs[i]] = (past_pos[s_mask] <= 3).mean()

            d_mask = (dist_arr[:i] == dist_arr[i]) & valid
            if d_mask.sum() >= 1:
                dist_rates[idxs[i]] = (past_pos[d_mask] <= 3).mean()

            t_mask = (track_arr[:i] == track_arr[i]) & valid
            if t_mask.sum() >= 1:
                track_rates[idxs[i]] = (past_pos[t_mask] <= 3).mean()

            g_mask = (grd_arr[:i] >= 4) & valid  # G3=4, G2=5, G1=6
            if g_mask.sum() >= 1:
                grade_top3[idxs[i]] = (past_pos[g_mask] <= 3).mean()

            # 騎手×馬コンビ実績（同じ騎手で2回以上）
            jh_mask = (jky_arr[:i] == jky_arr[i]) & valid
            if jh_mask.sum() >= 2:
                jh_rates[idxs[i]] = (past_pos[jh_mask] <= 3).mean()

    df['horse_same_surface_top3_rate']  = surf_rates
    df['horse_same_dist_top3_rate']     = dist_rates
    df['horse_same_track_top3_rate']    = track_rates
    df['career_grade_top3_rate']        = grade_top3
    df['jockey_horse_pair_top3_rate']   = jh_rates

    # 条件クラス変動
    prev_grade = grp['_grade'].shift(1)
    df['class_drop'] = (prev_grade - df['_grade']).where(prev_grade.notna())

    # 初条件フラグ
    df['_surf_cnt'] = df.groupby(['horse_name', 'surface'], sort=False).cumcount()
    df['is_first_surface'] = (df['_surf_cnt'] == 0).astype(float)
    df['_venue_c2'] = df['race_id'].astype(str).str[4:6]
    df['_venu_cnt'] = df.groupby(['horse_name', '_venue_c2'], sort=False).cumcount()
    df['is_first_venue'] = (df['_venu_cnt'] == 0).astype(float)

    # 体重増減トレンド
    df['_wchg'] = df['horse_weight'].apply(lambda x: parse_horse_weight(str(x))[1])
    df['horse_weight_avg_change'] = grp['_wchg'].transform(
        lambda x: x.shift(1).rolling(3, min_periods=1).mean()
    )

    return df.drop(columns=[
        '_rid_s', '_dist_b', '_date_dt', '_last3f_num', '_grade',
        '_t_sec', '_dist_n', '_fpos_n', '_par_key', '_par_val', '_speed_idx',
        '_l3f_rank', '_race_sz', '_l3f_rel', '_field_cnt', '_c4', '_c1',
        '_margin_n', '_style_n',
        '_surf_cnt', '_venue_c2', '_venu_cnt', '_wchg',
    ])


def engineer_features(df, jockey_stats=None, trainer_stats=None,
                      jockey_venue_stats=None, trainer_venue_stats=None,
                      frame_bias=None, jt_stats=None):
    df = df.copy()

    sa = df['sex_age'].apply(lambda x: pd.Series(parse_sex_age(x), index=['sex', 'age']))
    df[['sex', 'age']] = sa

    hw = df['horse_weight'].apply(
        lambda x: pd.Series(parse_horse_weight(x), index=['horse_weight_num', 'weight_change'])
    )
    df[['horse_weight_num', 'weight_change']] = hw

    df['surface_num'] = df['surface'].map({'芝': 0, 'ダ': 1, '障': 2}).fillna(-1)
    df['track_num']   = df['track_condition'].map(
        {'良': 0, '稍重': 1, '重': 2, '不良': 3}
    ).fillna(-1)

    for col in ['frame', 'horse_num', 'weight_carried', 'popularity']:
        df[f'{col}_num'] = pd.to_numeric(df[col], errors='coerce')
    df['odds_num'] = pd.to_numeric(df['odds'], errors='coerce')
    df['distance'] = pd.to_numeric(df['distance'], errors='coerce')

    field_size = df.groupby('race_id')['horse_num'].count().rename('field_size')
    df = df.join(field_size, on='race_id')

    df['venue_code'] = df['race_id'].astype(str).str[4:6]

    # ---- 全体成績を付与 ----
    def attach_global_stats(stats, col, prefix):
        if stats is not None:
            df2 = df.merge(
                stats.rename(columns={
                    'win_rate':  f'{prefix}_win_rate',
                    'top3_rate': f'{prefix}_top3_rate',
                }),
                left_on=col, right_index=True, how='left'
            )
            df2[f'{prefix}_win_rate']  = df2[f'{prefix}_win_rate'].fillna(stats['win_rate'].mean())
            df2[f'{prefix}_top3_rate'] = df2[f'{prefix}_top3_rate'].fillna(stats['top3_rate'].mean())
            return df2
        df[f'{prefix}_win_rate']  = 0.0
        df[f'{prefix}_top3_rate'] = 0.0
        return df

    df = attach_global_stats(jockey_stats,  'jockey',  'jockey')
    df = attach_global_stats(trainer_stats, 'trainer', 'trainer')

    # ---- コース別成績を付与（不足はフォールバック） ----
    def attach_venue_stats(venue_stats, entity_col, out_col, fallback_stats):
        if venue_stats is not None:
            v = venue_stats.reset_index()
            v.columns = [entity_col, 'venue_code', out_col]
            merged = df.merge(v, on=[entity_col, 'venue_code'], how='left')
            # フォールバック: 全体成績 → 平均
            if fallback_stats is not None:
                fb_mean = fallback_stats['top3_rate'].mean()
                fb_map  = fallback_stats['top3_rate'].to_dict()
                mask = merged[out_col].isna()
                merged.loc[mask, out_col] = merged.loc[mask, entity_col].map(fb_map).fillna(fb_mean)
            return merged
        df[out_col] = np.nan
        return df

    df = attach_venue_stats(jockey_venue_stats,  'jockey',  'jockey_venue_top3_rate',  jockey_stats)
    df = attach_venue_stats(trainer_venue_stats, 'trainer', 'trainer_venue_top3_rate', trainer_stats)

    # 枠番×会場×馬場バイアス
    if frame_bias:
        df['_fn'] = pd.to_numeric(df['frame'], errors='coerce').fillna(-1).astype(int)
        df['_fb_k'] = df['venue_code'] + '_' + df['surface'] + '_' + df['_fn'].astype(str)
        df['frame_venue_top3_rate'] = df['_fb_k'].map(frame_bias)
        df = df.drop(columns=['_fn', '_fb_k'])
    else:
        df['frame_venue_top3_rate'] = np.nan

    # 騎手×調教師コンビ実績
    if jt_stats is not None and not jt_stats.empty:
        jt_df = jt_stats.reset_index()
        jt_df.columns = ['jockey', 'trainer', 'jockey_trainer_top3_rate']
        df = df.merge(jt_df, on=['jockey', 'trainer'], how='left')
    else:
        df['jockey_trainer_top3_rate'] = np.nan

    # レース内相対値（同一レース全出走馬を基準とした偏差）
    if 'weight_carried_num' in df.columns:
        fld_avg_w = df.groupby('race_id')['weight_carried_num'].transform('mean')
        df['weight_vs_field_avg'] = df['weight_carried_num'] - fld_avg_w
    else:
        df['weight_vs_field_avg'] = np.nan

    if 'horse_avg_speed_idx' in df.columns:
        fld_avg_s = df.groupby('race_id')['horse_avg_speed_idx'].transform('mean')
        df['speed_idx_vs_field'] = df['horse_avg_speed_idx'] - fld_avg_s
    else:
        df['speed_idx_vs_field'] = np.nan

    # 血統フラグ（pedigrees.csv 由来の列が df に存在する場合のみ計算）
    for col in ['sire_ryoyo', 'sire_stay', 'sire_speed', 'dam_sire_ryoyo']:
        if col not in df.columns:
            df[col] = np.nan

    return df


def _compute_group_sizes(df_sorted, race_id_col='race_id'):
    """race_id でソート済みの DataFrame からグループサイズ配列を返す"""
    return df_sorted.groupby(race_id_col, sort=False)[race_id_col].count().values


def _groups_from_race_ids(race_ids_arr):
    """ソート済み race_id 配列から LGBMRanker 用グループサイズリストを生成"""
    if len(race_ids_arr) == 0:
        return []
    groups = []
    cur = race_ids_arr[0]
    cnt = 1
    for rid in race_ids_arr[1:]:
        if rid != cur:
            groups.append(cnt)
            cur = rid
            cnt = 1
        else:
            cnt += 1
    groups.append(cnt)
    return groups


def run_optuna(X, y_label, race_ids, n_trials=50):
    """
    Optuna でハイパーパラメータを最適化し、最適パラメータ dict を返す。
    GroupKFold(4) × NDCG@1 を最大化する。
    """
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    except ImportError:
        print('  Optuna 未インストール: pip install optuna')
        return {}

    kf = GroupKFold(n_splits=4)

    def objective(trial):
        params = dict(
            objective='lambdarank', metric='ndcg',
            n_jobs=-1, verbose=-1, random_state=42,
            n_estimators=trial.suggest_int('n_estimators', 300, 2000),
            learning_rate=trial.suggest_float('learning_rate', 0.005, 0.3, log=True),
            max_depth=trial.suggest_int('max_depth', 4, 9),
            num_leaves=trial.suggest_int('num_leaves', 20, 200),
            min_child_samples=trial.suggest_int('min_child_samples', 5, 50),
            subsample=trial.suggest_float('subsample', 0.4, 1.0),
            colsample_bytree=trial.suggest_float('colsample_bytree', 0.4, 1.0),
            reg_alpha=trial.suggest_float('reg_alpha', 1e-5, 10.0, log=True),
            reg_lambda=trial.suggest_float('reg_lambda', 1e-5, 10.0, log=True),
        )
        ndcg_scores = []
        for tr_idx, va_idx in kf.split(X, y_label, groups=race_ids):
            tr_rids = race_ids[tr_idx]
            va_rids = race_ids[va_idx]
            ts = np.argsort(tr_rids, kind='stable')
            vs = np.argsort(va_rids, kind='stable')
            X_tr = X[tr_idx][ts]; y_tr = y_label[tr_idx][ts]
            X_va = X[va_idx][vs]; y_va = y_label[va_idx][vs]
            g_tr = _groups_from_race_ids(tr_rids[ts])
            g_va = _groups_from_race_ids(va_rids[vs])
            m = lgb.LGBMRanker(**params)
            m.fit(X_tr, y_tr, group=g_tr,
                  eval_set=[(X_va, y_va)], eval_group=[g_va],
                  callbacks=[lgb.early_stopping(30, verbose=False),
                              lgb.log_evaluation(-1)])
            best = m.best_score_.get('valid_0', {}).get('ndcg@1', 0.0)
            ndcg_scores.append(best)
        return float(np.mean(ndcg_scores))

    print(f'  Optuna: {n_trials}トライアル実行中（4-fold CV / NDCG@1 最大化）...')
    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)
    print(f'  Best NDCG@1 : {study.best_value:.4f}')
    print(f'  Best params : {study.best_params}')
    return study.best_params


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tune', action='store_true',
                        help='Optuna でハイパーパラメータ最適化（30〜60分）')
    parser.add_argument('--tune-trials', type=int, default=50,
                        help='Optuna トライアル数（デフォルト50）')
    args = parser.parse_args()

    print('データ読み込み中...')
    df = pd.read_csv(RACES_CSV, dtype=str)

    n_all = len(df)
    df = df[df['race_id'].astype(str).str[4:6].isin(JRA_VENUES)].copy()
    print(f'  JRAレースに限定: {n_all:,}行 → {len(df):,}行（NAR除外）')

    df['finishing_pos_num'] = pd.to_numeric(df['finishing_pos'], errors='coerce')
    df = df.dropna(subset=['finishing_pos_num'])
    df['finishing_pos_num'] = df['finishing_pos_num'].astype(int)
    print(f'  有効データ: {len(df):,}行  {df["race_id"].nunique():,}レース')

    print('騎手・調教師成績を計算中...')
    jockey_stats, trainer_stats = compute_stats(df)
    jockey_venue_stats, trainer_venue_stats = compute_venue_stats(df)
    frame_bias = compute_frame_bias(df)

    print('騎手×調教師コンビ実績を計算中...')
    jt_stats = compute_jockey_trainer_stats(df)
    print(f'  有効コンビ: {len(jt_stats):,}組')

    print('馬の過去成績特徴量を計算中（数分かかる場合があります）...')
    df = add_horse_history_features(df)

    print('直近60日騎手・調教師実績を計算中...')
    df['jockey_recent60_top3']  = compute_rolling_entity_top3(df, 'jockey')
    df['trainer_recent60_top3'] = compute_rolling_entity_top3(df, 'trainer')

    # 血統特徴量（pedigrees.csv がある場合）
    PEDIGREE_CSV = DATA_DIR / 'pedigrees.csv'
    if PEDIGREE_CSV.exists():
        print('血統データを読み込み中...')
        ped_df = pd.read_csv(PEDIGREE_CSV, dtype=str)
        df = df.merge(ped_df[['horse_id', 'sire', 'dam_sire']], on='horse_id', how='left')
        df['sire_ryoyo']     = df['sire'].apply(lambda s: float(str(s) in _RYOYO_SIRES))
        df['sire_stay']      = df['sire'].apply(lambda s: float(str(s) in _STAY_SIRES))
        df['sire_speed']     = df['sire'].apply(lambda s: float(str(s) in _SPEED_SIRES))
        df['dam_sire_ryoyo'] = df['dam_sire'].apply(lambda s: float(str(s) in _RYOYO_SIRES))
        n_matched = df['sire'].notna().sum()
        print(f'  血統マッチ: {n_matched:,}行')
        df = df.drop(columns=['sire', 'dam_sire'], errors='ignore')
    else:
        print('  pedigrees.csv 未作成 → 血統特徴量は NaN（10_血統収集.bat で収集可能）')
        for col in ['sire_ryoyo', 'sire_stay', 'sire_speed', 'dam_sire_ryoyo']:
            df[col] = np.nan

    print('特徴量エンジニアリング中...')
    df = engineer_features(df, jockey_stats, trainer_stats,
                           jockey_venue_stats, trainer_venue_stats, frame_bias,
                           jt_stats=jt_stats)

    # race_id でソート（LGBMRanker の group 引数に必要）
    df = df.sort_values('race_id').reset_index(drop=True)

    # ランキング用ラベル: 高いほど良い着順
    #   1着 → max_pos-1（最大）、最下位 → 0
    df['_max_pos'] = df.groupby('race_id')['finishing_pos_num'].transform('max')
    y_label  = (df['_max_pos'] - df['finishing_pos_num']).astype(int).values
    race_ids = df['race_id'].values

    X = df[FEATURE_COLS].fillna(df[FEATURE_COLS].median()).values

    # 2026-09 3年バックテスト(2023/24/25)で、正則化を強めた設定が現行より
    # ◎勝率・ベタ買いROIとも一貫して上回ると検証済み（過学習の抑制）。
    ranker_params = dict(
        objective='lambdarank',
        metric='ndcg',
        n_estimators=1500,
        learning_rate=0.03,
        max_depth=5,
        num_leaves=31,
        min_child_samples=40,
        subsample=0.7,
        colsample_bytree=0.7,
        random_state=42,
        n_jobs=-1,
        verbose=-1,
    )

    if args.tune:
        print(f'\nOptuna ハイパーパラメータ最適化 ({args.tune_trials}トライアル)...')
        best = run_optuna(X, y_label, race_ids, n_trials=args.tune_trials)
        if best:
            keep = {'objective', 'metric', 'n_jobs', 'verbose'}
            ranker_params.update({k: v for k, v in best.items() if k not in keep})
            print('  最適パラメータでモデルを訓練します。')

    print('\n交差検証中（Top-3的中率で評価）...')
    hit_rates = []
    for fold, (tr_idx, va_idx) in enumerate(GroupKFold(5).split(X, y_label, race_ids)):
        X_tr, y_tr = X[tr_idx], y_label[tr_idx]
        X_va, y_va = X[va_idx], y_label[va_idx]

        tr_groups = _compute_group_sizes(df.iloc[tr_idx])
        va_groups = _compute_group_sizes(df.iloc[va_idx])

        m = lgb.LGBMRanker(**ranker_params)
        m.fit(
            X_tr, y_tr, group=tr_groups,
            eval_set=[(X_va, y_va)], eval_group=[va_groups],
            callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(False)],
        )

        # Top-3的中率: 予測上位3頭が実際に3着以内に入った割合
        val_df = df.iloc[va_idx].copy()
        val_df['score']     = m.predict(X_va)
        val_df['pred_rank'] = val_df.groupby('race_id')['score'].rank(
            ascending=False, method='min'
        ).astype(int)
        pred_top3 = val_df[val_df['pred_rank'] <= 3]
        hit_rate  = (pred_top3['finishing_pos_num'] <= 3).mean()
        hit_rates.append(hit_rate)
        print(f'  Fold {fold+1}/5  Top3的中率={hit_rate:.3f}')

    mean_hr = np.mean(hit_rates)
    print(f'\nCV Top3的中率: {mean_hr:.3f} ± {np.std(hit_rates):.3f}')
    print(f'  ※ランダム予測の理論値: {3 / 9:.3f}（平均9頭想定）')

    print('\n全データでアンサンブルモデルを訓練中（3シード）...')
    all_groups = _compute_group_sizes(df)
    ensemble   = []
    for seed in [42, 123, 456]:
        m = lgb.LGBMRanker(**{**ranker_params, 'random_state': seed})
        m.fit(X, y_label, group=all_groups)
        ensemble.append(m)
        print(f'  seed={seed} 完了')

    final_model = ensemble[0]  # 重要度表示用

    DATA_DIR.mkdir(exist_ok=True)
    joblib.dump({'models': ensemble, 'type': 'ensemble', 'feature_cols': FEATURE_COLS}, MODEL_PATH)
    joblib.dump({
        'jockey':           jockey_stats,
        'trainer':          trainer_stats,
        'jockey_venue':     jockey_venue_stats,
        'trainer_venue':    trainer_venue_stats,
        'frame_bias':       frame_bias,
        'jockey_trainer':   jt_stats,
    }, STATS_PATH)

    print(f'\nモデル保存完了: {MODEL_PATH}')

    print('\n特徴量重要度 (上位15):')
    imp = pd.Series(final_model.feature_importances_, index=FEATURE_COLS)
    print(imp.sort_values(ascending=False).head(15).to_string())


if __name__ == '__main__':
    main()
