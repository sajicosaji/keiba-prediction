"""
レース予測スクリプト

使い方:
  python predict.py 202506010801          # 出馬表から予測（レース前）
  python predict.py 202506010801 --result # 結果ページから検証（レース後）
  python predict.py 202506010801 --no-pedigree  # 血統取得をスキップ（高速）
"""
import os
import sys
import json
import joblib
import argparse
import numpy as np
import pandas as pd
from pathlib import Path
from scraper import scrape_race_entry, scrape_race_result, scrape_horse_pedigree
from train_model import (engineer_features, FEATURE_COLS, _dist_band_num, _grade_num,
                         parse_horse_weight, _parse_margin, compute_rolling_entity_top3,
                         _parse_race_time_secs,
                         _RYOYO_SIRES, _STAY_SIRES, _SPEED_SIRES)
from analysis import build_history_df, generate_analysis, VENUE_NAME

DATA_DIR   = Path(__file__).parent.parent / 'data'
MODEL_PATH = DATA_DIR / 'model.pkl'
STATS_PATH = DATA_DIR / 'stats.pkl'
RACES_CSV  = DATA_DIR / 'races.csv'

MARKS      = ['◎', '○', '▲', '△', '★']
JRA_VENUES = {'01','02','03','04','05','06','07','08','09','10'}

# バックテスト(2024-2025, walk-forward)で検証済みの買い条件
# EVは市場ブレンド勝率 p = α*モデル + (1-α)*市場(オッズ逆数正規化) で計算する。
# ブレンドは大穴でのモデル過信を抑え、回収率と安定性の両方を改善した。
EV_MAIN_TH   = 1.2   # ◎: EV>=1.2 & オッズ<=50倍 → 検証回収率 167% (95%CI 135-201%)
EV_LONG_TH   = 2.0   # ◎以外: EV>=2.0 & オッズ<=50倍 → 検証回収率 124% (95%CI 96-154%)
EV_ODDS_CAP  = 50.0
BLEND_ALPHA  = 0.7   # モデル勝率の重み（市場勝率は 1-α）
DEFAULT_TEMP = 0.9   # calibration.json が無い場合のフォールバック温度
KELLY_FRAC   = 0.25  # 1/4ケリー
KELLY_CAP    = 0.02  # 1点あたり資金の2%まで

# 馬連（試験運用: 検証は2日分72レースのみ。EV>=1.5&60倍以下で回収率107-162%）
# ワイド・三連系は同検証で回収率100%未満だったため不採用
UMAREN_EV_TH    = 1.5
UMAREN_ODDS_CAP = 60.0
UMAREN_MAX_BETS = 5     # 1レース最大5点（EV条件を満たす点のみ→実質EVフィルタ付き流し）


def fetch_combo_odds(race_id, odds_type='4'):
    """netkeiba APIから連系オッズを取得 {'0102': オッズ, ...}（4=馬連, 5=ワイド）"""
    try:
        import requests
        r = requests.get(
            'https://race.netkeiba.com/api/api_get_jra_odds.html',
            params={'race_id': str(race_id), 'type': str(odds_type), 'action': 'update'},
            headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://race.netkeiba.com/'},
            timeout=15,
        )
        out = {}
        for combos in r.json()['data']['odds'].values():
            for k, v in combos.items():
                try:
                    out[k] = float(str(v[0]).replace(',', ''))
                except (ValueError, TypeError, IndexError):
                    continue
        return out
    except Exception as e:
        print(f'  馬連オッズ取得エラー: {e}')
        return {}


def _harville_pair(p, i, j):
    """i,j が1・2着（順不同）になる確率（Harville法）"""
    return p[i] * p[j] / (1 - p[i]) + p[j] * p[i] / (1 - p[j])


def compute_umaren(df, race_id):
    """予測上位5頭のペアから期待値の高い馬連候補を返す"""
    from itertools import combinations
    if 'p_bet' not in df.columns or df['p_bet'].isna().all():
        return []
    odds_map = fetch_combo_odds(race_id, '4')
    if not odds_map:
        return []
    p = df['p_bet'].fillna(0.001).clip(lower=1e-4, upper=0.98).values
    nums = pd.to_numeric(df['horse_num'], errors='coerce')
    cands = []
    top_n = min(5, len(df))
    for i, j in combinations(range(top_n), 2):
        ni, nj = nums.iloc[i], nums.iloc[j]
        if pd.isna(ni) or pd.isna(nj):
            continue
        a, b = sorted([int(ni), int(nj)])
        o = odds_map.get(f'{a:02d}{b:02d}')
        if not o or o > UMAREN_ODDS_CAP:
            continue
        prob = _harville_pair(p, i, j)
        ev = prob * o
        if ev >= UMAREN_EV_TH:
            cands.append({
                'kind': '馬連', 'nums': f'{a}-{b}',
                'names': f'{df.iloc[i]["horse_name"]}×{df.iloc[j]["horse_name"]}',
                'odds': o, 'p': prob, 'ev': ev,
            })
    cands.sort(key=lambda x: -x['ev'])
    return cands[:UMAREN_MAX_BETS]


def fetch_live_odds(race_id):
    """netkeiba APIから現在の単勝オッズを取得 {馬番int: (オッズ, 人気)}"""
    try:
        import requests
        r = requests.get(
            'https://race.netkeiba.com/api/api_get_jra_odds.html',
            params={'race_id': str(race_id), 'type': '1', 'action': 'update'},
            headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://race.netkeiba.com/'},
            timeout=15,
        )
        tan = r.json()['data']['odds'].get('1', {})
        out = {}
        for num, v in tan.items():
            try:
                out[int(num)] = (float(v[0]), int(v[2]))
            except (ValueError, TypeError, IndexError):
                continue
        return out
    except Exception as e:
        print(f'  オッズ取得エラー: {e}')
        return {}


def compute_ev(df, race_id):
    """較正済み勝率と現在オッズからEV列を付与し、買い推奨リストを返す"""
    cal_path = DATA_DIR / 'calibration.json'
    T = DEFAULT_TEMP
    if cal_path.exists():
        try:
            T = float(json.loads(cal_path.read_text(encoding='utf-8')).get('T', T))
        except Exception:
            pass

    e = np.exp((df['pred_score'] - df['pred_score'].max()) / T)
    df['p_win'] = e / e.sum()

    df['live_odds'] = np.nan
    df['p_bet'] = df['p_win']
    df['ev'] = np.nan
    odds_map = fetch_live_odds(race_id)
    if odds_map:
        hn = pd.to_numeric(df['horse_num'], errors='coerce')
        df['live_odds'] = hn.map(lambda x: odds_map.get(int(x), (np.nan,))[0]
                                 if pd.notna(x) else np.nan)
        # 市場勝率（オッズ逆数をレース内で正規化）とブレンド
        inv = 1.0 / df['live_odds']
        tot = inv.sum(skipna=True)
        if tot > 0:
            p_mkt = inv / tot
            blended = BLEND_ALPHA * df['p_win'] + (1 - BLEND_ALPHA) * p_mkt
            df['p_bet'] = blended.fillna(df['p_win'])
        df['ev'] = df['p_bet'] * df['live_odds']

    recs = []
    for _, row in df.iterrows():
        ev, o = row.get('ev'), row.get('live_odds')
        if pd.isna(ev) or pd.isna(o) or o > EV_ODDS_CAP or o <= 1.0:
            continue
        if row['final_rank'] == 1 and ev >= EV_MAIN_TH:
            recs.append(('◎単勝', row))
        elif row['final_rank'] != 1 and ev >= EV_LONG_TH:
            recs.append(('穴単勝', row))
    return df, recs, odds_map


def kelly_pct(p, odds):
    """1/4ケリーの推奨賭け金（資金比%）。上限 KELLY_CAP"""
    if pd.isna(p) or pd.isna(odds) or odds <= 1.0:
        return 0.0
    f = p - (1 - p) / (odds - 1)
    return max(0.0, min(f * KELLY_FRAC, KELLY_CAP)) * 100


def log_recommendations(recs, race_id, race_name, combo_recs=None):
    """買い推奨を data/bet_log.csv に追記（実運用成績の検証用）"""
    if not recs and not combo_recs:
        return
    import csv
    from datetime import datetime
    from zoneinfo import ZoneInfo
    path = DATA_DIR / 'bet_log.csv'
    new_file = not path.exists()
    try:
        with open(path, 'a', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(['logged_at', 'race_id', 'race_name', 'kind',
                            'horse_num', 'horse_name', 'odds', 'p_bet', 'ev', 'kelly_pct'])
            # クラウド実行時はランナーがUTCのため、必ずJSTで記録する
            now = datetime.now(ZoneInfo('Asia/Tokyo')).strftime('%Y-%m-%d %H:%M')
            for kind, row in (recs or []):
                w.writerow([now, race_id, race_name, kind,
                            row.get('horse_num', ''), row.get('horse_name', ''),
                            f"{row['live_odds']:.1f}", f"{row['p_bet']:.4f}",
                            f"{row['ev']:.2f}", f"{kelly_pct(row['p_bet'], row['live_odds']):.2f}"])
            for c in (combo_recs or []):
                w.writerow([now, race_id, race_name, c['kind'],
                            c['nums'], c['names'],
                            f"{c['odds']:.1f}", f"{c['p']:.4f}",
                            f"{c['ev']:.2f}", f"{kelly_pct(c['p'], c['odds']):.2f}"])
    except Exception as e:
        print(f'  bet_log 書き込みエラー: {e}')


def load_races_df():
    """JRAレース（venue code 01-10）のみ読み込む"""
    if not RACES_CSV.exists():
        return pd.DataFrame()
    df = pd.read_csv(RACES_CSV, dtype=str, on_bad_lines='skip')
    df['_vc'] = df['race_id'].astype(str).str[4:6]
    df = df[df['_vc'].isin(JRA_VENUES)].copy()
    df['_m'] = df['race_id'].astype(str).str[4:6]
    df['_d'] = df['race_id'].astype(str).str[6:8]
    df = df[df['_m'].between('01','12') & df['_d'].between('01','31')]
    return df.drop(columns=['_vc','_m','_d'])


def attach_horse_history(df, races_df, race_id, surface, distance, cur_race_name='', track_condition=''):
    """
    出走馬に過去成績・前走情報・末脚・乗り替わりフラグを付与（予測時用）。
    races_df から current race_id を除いた履歴を参照する。
    """
    new_cols = [
        'horse_recent3_avg', 'horse_career_top3_rate', 'horse_career_races',
        'career_best_pos', 'career_grade_top3_rate',
        'horse_same_surface_top3_rate', 'horse_same_dist_top3_rate',
        'horse_same_track_top3_rate',
        'running_style_num',
        'prev_pos', 'prev2_pos', 'prev_days_interval', 'prev_margin_num',
        'form_trend', 'field_size_change', 'dist_change',
        'prev_last3f', 'prev2_last3f', 'horse_avg_last3f',
        'prev_last3f_rank_rel',
        'prev_speed_idx', 'horse_avg_speed_idx', 'career_best_speed_idx',
        'jockey_change_flag',
        'class_drop',
        'is_first_surface', 'is_first_venue',
        'horse_weight_avg_change',
        'jockey_recent60_top3', 'trainer_recent60_top3',
        'prev_c4_ratio', 'prev_pos_gain',
        'jockey_horse_pair_top3_rate',
    ]
    for col in new_cols:
        df[col] = np.nan

    if races_df.empty:
        return df

    # パータイム辞書（スピード指数計算用）を事前に1回だけ作成
    par_dict: dict = {}
    try:
        _wr = races_df.copy()
        _wr['_fp'] = pd.to_numeric(_wr.get('finishing_pos', pd.Series(dtype=str)), errors='coerce')
        _wr['_t']  = _wr['time'].apply(_parse_race_time_secs) if 'time' in _wr.columns else pd.Series(np.nan, index=_wr.index)
        _wr['_d']  = pd.to_numeric(_wr.get('distance', pd.Series(dtype=str)), errors='coerce')
        _w_df = _wr[(_wr['_fp'] == 1) & _wr['_t'].notna() & _wr['_d'].notna()]
        _par  = _w_df.groupby(['surface', '_d'])['_t'].median()
        par_dict = {f"{s}_{int(d)}": t for (s, d), t in _par.items() if pd.notna(d)}
    except Exception:
        pass

    hist = races_df[races_df['race_id'].astype(str) != str(race_id)].copy()
    hist['finishing_pos_num'] = pd.to_numeric(hist['finishing_pos'], errors='coerce')
    hist['last3f_num']  = pd.to_numeric(hist.get('last_3f', pd.Series(dtype=float)), errors='coerce')
    hist['dist_band']   = hist['distance'].apply(_dist_band_num)
    hist['race_date_dt'] = pd.to_datetime(
        hist['race_date'].astype(str).str[:8], format='%Y%m%d', errors='coerce'
    )

    cur_dist_band = _dist_band_num(distance)
    today = pd.Timestamp.today()

    for i, row in df.iterrows():
        horse = str(row['horse_name'])
        h = hist[(hist['horse_name'] == horse) & hist['finishing_pos_num'].notna()]
        h = h.sort_values('race_id', ascending=False)   # 直近→古い

        if h.empty:
            continue

        pos = h['finishing_pos_num']

        df.at[i, 'horse_recent3_avg']      = pos.head(3).mean()
        df.at[i, 'horse_career_top3_rate'] = (pos <= 3).mean()
        df.at[i, 'horse_career_races']     = len(pos)
        df.at[i, 'career_best_pos']        = pos.min()
        df.at[i, 'prev_pos']               = pos.iloc[0]
        if len(pos) >= 2:
            df.at[i, 'prev2_pos'] = pos.iloc[1]

        # フォームトレンド (前走 - 3走前: 負=改善)
        if len(pos) >= 3:
            df.at[i, 'form_trend'] = float(pos.iloc[0] - pos.iloc[2])

        # 重賞実績
        if 'race_name' in h.columns:
            gh = h[h['race_name'].apply(_grade_num) >= 4]
            if not gh.empty:
                df.at[i, 'career_grade_top3_rate'] = (gh['finishing_pos_num'] <= 3).mean()

        # 前走着差（数値化）
        if 'margin' in h.columns:
            df.at[i, 'prev_margin_num'] = _parse_margin(str(h.iloc[0]['margin']))

        # 前走からの日数
        last_dates = h['race_date_dt'].dropna()
        if not last_dates.empty:
            delta = (today - last_dates.iloc[0]).days
            df.at[i, 'prev_days_interval'] = delta if 0 < delta < 730 else np.nan

        # 前走の末脚・直近3走の末脚平均
        last3f_vals = h['last3f_num'].dropna()
        if not last3f_vals.empty:
            df.at[i, 'prev_last3f']      = last3f_vals.iloc[0]
            df.at[i, 'horse_avg_last3f'] = last3f_vals.head(3).mean()
            if len(last3f_vals) >= 2:
                df.at[i, 'prev2_last3f'] = last3f_vals.iloc[1]

        # 脚質エンコード（前走）
        if 'running_style' in h.columns:
            _sm = {'逃げ': 0.0, '先行': 1.0, '差し': 2.0, '追込': 3.0}
            rs = h['running_style'].iloc[0] if not h.empty else ''
            rv = _sm.get(str(rs), np.nan)
            if not np.isnan(rv):
                df.at[i, 'running_style_num'] = rv

        # 同馬場3着内率
        surf_h = h[h['surface'] == surface]
        if not surf_h.empty:
            sp = surf_h['finishing_pos_num']
            df.at[i, 'horse_same_surface_top3_rate'] = (sp <= 3).mean()

        # 同距離帯3着内率
        dist_h = h[h['dist_band'] == cur_dist_band]
        if not dist_h.empty:
            dp = dist_h['finishing_pos_num']
            df.at[i, 'horse_same_dist_top3_rate'] = (dp <= 3).mean()

        # 同馬場状態3着内率
        if track_condition and 'track_condition' in h.columns:
            track_h = h[h['track_condition'] == track_condition]
            if not track_h.empty:
                df.at[i, 'horse_same_track_top3_rate'] = (track_h['finishing_pos_num'] <= 3).mean()

        # 初条件フラグ
        df.at[i, 'is_first_surface'] = 0.0 if not surf_h.empty else 1.0
        venue_code_cur = str(race_id)[4:6]
        if 'race_id' in h.columns:
            venue_h = h[h['race_id'].astype(str).str[4:6] == venue_code_cur]
            df.at[i, 'is_first_venue'] = 0.0 if not venue_h.empty else 1.0

        # 体重増減トレンド（直近3走の平均変動）
        if 'horse_weight' in h.columns:
            wc_vals = [parse_horse_weight(str(r))[1]
                       for r in h['horse_weight'].head(3) if str(r) not in ('nan','None','')]
            if wc_vals:
                df.at[i, 'horse_weight_avg_change'] = float(np.mean(wc_vals))

        # 乗り替わりフラグ（前走の騎手と比較）
        prev_jockey = h['jockey'].iloc[0] if 'jockey' in h.columns else None
        cur_jockey  = str(row.get('jockey', ''))
        if prev_jockey and cur_jockey:
            df.at[i, 'jockey_change_flag'] = float(str(prev_jockey) != cur_jockey)

        # 条件クラス変動: 前走グレード - 今走グレード (正=降級、負=昇級)
        if 'race_name' in h.columns and not h.empty:
            prev_grade = _grade_num(h['race_name'].iloc[0])
            cur_grade  = _grade_num(cur_race_name)
            if prev_grade > 0:
                df.at[i, 'class_drop'] = float(prev_grade - cur_grade)

        # スピード指数（直近・直近5走平均・キャリアベスト）
        past_speeds = []
        for _, hr in h.iterrows():
            try:
                h_surf = str(hr.get('surface', ''))
                h_d    = int(float(str(hr['distance'])))
                h_t    = _parse_race_time_secs(str(hr.get('time', '')))
                par_t  = par_dict.get(f"{h_surf}_{h_d}")
                if par_t and par_t > 0 and not np.isnan(h_t):
                    past_speeds.append((par_t - h_t) / par_t * 100 + 80)
            except Exception:
                pass
        if past_speeds:
            df.at[i, 'prev_speed_idx']        = past_speeds[0]
            df.at[i, 'horse_avg_speed_idx']   = float(np.mean(past_speeds[:5]))
            df.at[i, 'career_best_speed_idx'] = float(max(past_speeds))

        # 前走レース内の上がり相対ランク (1.0=最速)
        if not h.empty and 'last_3f' in h.columns and 'race_id' in h.columns:
            prev_rid   = str(h['race_id'].iloc[0])
            prev_race  = races_df[races_df['race_id'].astype(str) == prev_rid]
            if not prev_race.empty:
                l3f_all = pd.to_numeric(prev_race['last_3f'], errors='coerce').dropna()
                h_l3f   = pd.to_numeric(h.iloc[0].get('last_3f', np.nan), errors='coerce')
                if len(l3f_all) > 1 and not np.isnan(h_l3f):
                    n_faster = int((l3f_all < h_l3f).sum())  # 上がりが小さい(速い)馬の数
                    df.at[i, 'prev_last3f_rank_rel'] = 1.0 - n_faster / max(len(l3f_all) - 1, 1)

        # 前走との距離変化・頭数変化
        if not h.empty:
            try:
                prev_d = float(str(h['distance'].iloc[0]))
                cur_d  = float(str(distance))
                df.at[i, 'dist_change'] = cur_d - prev_d
            except Exception:
                pass
            try:
                prev_rid  = str(h['race_id'].iloc[0])
                prev_race = races_df[races_df['race_id'].astype(str) == prev_rid]
                df.at[i, 'field_size_change'] = float(len(df) - len(prev_race))
            except Exception:
                pass

        # コーナー通過順位（前走）
        if not h.empty and 'c4_pos' in h.columns:
            try:
                c4 = float(str(h['c4_pos'].iloc[0]))
                if not np.isnan(c4):
                    prev_rid_c = str(h['race_id'].iloc[0])
                    prev_sz = len(races_df[races_df['race_id'].astype(str) == prev_rid_c])
                    if prev_sz > 1:
                        df.at[i, 'prev_c4_ratio'] = (prev_sz - c4) / max(prev_sz - 1, 1)
                    if 'c1_pos' in h.columns:
                        c1 = float(str(h['c1_pos'].iloc[0]))
                        if not np.isnan(c1):
                            df.at[i, 'prev_pos_gain'] = c1 - c4
            except (ValueError, TypeError):
                pass

        # 騎手×馬コンビ実績（同騎手で2回以上）
        if not h.empty and 'jockey' in h.columns:
            cur_jk = str(row.get('jockey', ''))
            if cur_jk:
                jh_mask = h['jockey'].astype(str) == cur_jk
                if jh_mask.sum() >= 2:
                    df.at[i, 'jockey_horse_pair_top3_rate'] = (
                        h.loc[jh_mask, 'finishing_pos_num'] <= 3
                    ).mean()

    return df


def _get_horse_running_style(horse_name, races_df):
    """races.csv から馬の直近の脚質（running_style）を取得"""
    if races_df.empty or 'running_style' not in races_df.columns:
        return ''
    h = races_df[races_df['horse_name'] == horse_name].copy()
    h = h[h['running_style'].notna() & (h['running_style'] != '')]
    if h.empty:
        return ''
    h = h.sort_values('race_id', ascending=False)
    styles = h['running_style'].head(5).tolist()
    if not styles:
        return ''
    from collections import Counter
    return Counter(styles).most_common(1)[0][0]


def _print_pace_prediction(df, races_df):
    """出走馬の脚質分布からレース展開を予測して表示"""
    styles = {}
    for _, row in df.iterrows():
        hn = str(row.get('horse_name', ''))
        s = _get_horse_running_style(hn, races_df)
        styles[hn] = s if s else '不明'

    counts = {'逃げ': 0, '先行': 0, '差し': 0, '追込': 0, '不明': 0}
    for s in styles.values():
        if s in counts:
            counts[s] += 1
        else:
            counts['不明'] += 1

    known = sum(v for k, v in counts.items() if k != '不明')
    if known < 3:
        return  # データ不足なら表示しない

    W = 75
    print(f'\n{"="*W}')
    print('【展開予測】')

    # 脚質分布表示
    dist_parts = []
    for label in ['逃げ', '先行', '差し', '追込']:
        n = counts[label]
        if n > 0:
            dist_parts.append(f'{label}:{n}頭')
    if counts['不明'] > 0:
        dist_parts.append(f'不明:{counts["不明"]}頭')
    print(f'  脚質分布: {" / ".join(dist_parts)}')

    # 各馬の脚質
    style_lines = []
    for _, row in df.iterrows():
        hn = str(row.get('horse_name', ''))
        s  = styles.get(hn, '不明')
        rank = row['final_rank']
        style_lines.append(f'{rank}着予{hn[:8]}[{s}]')
    print('  ' + '  '.join(style_lines[:8]))

    # ペース予測
    n_front = counts['逃げ'] + counts['先行']
    total_known = known
    front_ratio = n_front / total_known if total_known > 0 else 0

    if counts['逃げ'] >= 3 or front_ratio >= 0.5:
        pace_lbl  = 'ハイペース予想'
        pace_note = '→ 前半から速い流れ。差し・追込有利の展開。'
        favor_lbl = '差し・追込'
    elif counts['逃げ'] == 0 or front_ratio <= 0.2:
        pace_lbl  = 'スローペース予想'
        pace_note = '→ 前半ゆったりした流れ。先行馬が粘りやすい展開。'
        favor_lbl = '逃げ・先行'
    else:
        pace_lbl  = '平均ペース予想'
        pace_note = '→ 標準的な流れ。能力・適性通りの決着が多い。'
        favor_lbl = '実力通り'

    print(f'  ペース: {pace_lbl}  （有利脚質: {favor_lbl}）')
    print(f'  {pace_note}')
    print(f'{"="*W}')


def _fmt(val, fmt, suffix='', na='---'):
    """数値フォーマット用ヘルパー"""
    if pd.isna(val):
        return na
    return format(val, fmt) + suffix


def _build_discord_message(df, race_name, surface, distance, track, venue_name,
                            races_df, pace_info='', ev_recs=None, has_odds=False,
                            umaren_recs=None):
    """Discord に送信するメッセージを組み立てる（印のある5頭のみ）
    Returns: (本文, embeds) のタプル。買い目がある時は緑の埋め込みボックス付き。
    """
    cond = f'{surface}{distance}m {track}'
    if venue_name:
        cond = f'{venue_name} {cond}'

    # 会場＋レース番号（例: 小倉11R）を先頭に出す
    rid = str(df['race_id'].iloc[0]) if 'race_id' in df.columns and len(df) else ''
    rno = f'{int(rid[10:12])}R' if len(rid) == 12 and rid[10:12].isdigit() else ''
    race_label = f'{venue_name}{rno} {race_name}'.strip()

    has_bets = bool(ev_recs or umaren_recs)
    if has_bets:
        title = f'🚨💰 **買い目あり！【{race_label}】** 💰🚨'
    elif has_odds:
        title = f'💤 【見送り】{race_label}'
    else:
        title = f'🏇 **【{race_label}】**'

    lines = [
        title,
        f'📍 {cond} | {len(df)}頭',
        '```',
    ]

    for i, (_, row) in enumerate(df.iterrows()):
        if i >= len(MARKS):
            break  # 印のある5頭のみ

        mark   = MARKS[i]
        name   = str(row['horse_name'])[:10]
        jockey = str(row.get('jockey', ''))[:6]

        # --- 根拠となる数値 ---
        r3    = row.get('horse_recent3_avg')        # 近3走平均着順
        ctr   = row.get('horse_career_top3_rate')   # キャリア3着内率
        si    = row.get('horse_avg_speed_idx')       # 平均スピード指数
        psi   = row.get('prev_speed_idx')            # 前走スピード指数
        jvr   = row.get('jockey_venue_top3_rate')   # 騎手×会場3着内率
        mgn   = row.get('prev_margin_num')           # 前走着差

        r3_s  = f'近走:{r3:.1f}着'               if pd.notna(r3)  else '近走:---'
        ctr_s = f'3着内:{ctr*100:.0f}%'          if pd.notna(ctr) else '3着内:---%'
        si_s  = f'SI:{si:+.1f}'                  if pd.notna(si)  else 'SI:--'
        psi_s = f'前SI:{psi:+.1f}'               if pd.notna(psi) else ''
        jvr_s = f'騎手会場:{jvr*100:.0f}%'       if pd.notna(jvr) else ''
        mgn_s = f'前走{mgn:+.1f}身'              if pd.notna(mgn) else ''

        # 1行目：印・馬名・騎手 + 主要スコア
        stat1 = f'{r3_s} / {ctr_s} / {si_s}'
        lines.append(f'{mark} {name:<10} ({jockey})')
        # 2行目：根拠の詳細（値がある項目のみ）
        detail = '  '.join(x for x in [psi_s, jvr_s, mgn_s] if x)
        if detail:
            lines.append(f'   {stat1}')
            lines.append(f'   📎 {detail}')
        else:
            lines.append(f'   {stat1}')
        lines.append('')  # 馬間の空行

    lines.append('```')
    lines.append('SI=スピード指数(80=基準) / 前SI=前走SI / 騎手会場=当会場3着内率')

    if pace_info:
        lines.append(f'🔀 {pace_info}')

    # 期待値ベースの買い推奨 → 目立つ緑の埋め込みボックスにする
    embeds = []
    if has_odds:
        if has_bets:
            desc = []
            for kind, row in (ev_recs or []):
                kp = kelly_pct(row['p_bet'], row['live_odds'])
                hn = str(row.get('horse_num', '')).strip()
                hn_s = f'{int(hn)}番 ' if hn.isdigit() else ''
                desc.append(f'▶ **{kind}  {hn_s}{row["horse_name"]}**')
                desc.append(f'　 {row["live_odds"]:.1f}倍 / 勝率{row["p_bet"]*100:.0f}% / '
                            f'EV {row["ev"]:.2f} / 賭け金: 資金の{kp:.1f}%')
            for c in (umaren_recs or []):
                desc.append(f'▶ **馬連  {c["nums"]}番**（試験運用・少額）')
                desc.append(f'　 {c["names"]}')
                desc.append(f'　 {c["odds"]:.1f}倍 / 的中率{c["p"]*100:.0f}% / EV {c["ev"]:.2f}')
            embeds.append({
                'title': f'💰 買い目　{race_label}',
                'description': '\n'.join(desc),
                'color': 0x00C853,  # 緑
            })
        else:
            lines.append('💤 期待値条件を満たす馬なし → **見送り推奨**')

    return '\n'.join(lines), embeds


def send_discord(message: str, webhook_url: str, embeds=None) -> bool:
    """Discord ウェブフックにメッセージを送信。成功で True を返す"""
    try:
        import requests
        payload = {'content': message}
        if embeds:
            payload['embeds'] = embeds
        r = requests.post(webhook_url, json=payload, timeout=10)
        return r.status_code in (200, 204)
    except Exception as e:
        print(f'  Discord 送信エラー: {e}')
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('race_id', help='12桁のレースID')
    parser.add_argument('--result',      action='store_true', help='レース後の結果検証モード')
    parser.add_argument('--no-pedigree', action='store_true', help='血統取得をスキップ（高速）')
    parser.add_argument('--discord',     action='store_true', help='予測結果をDiscordに送信')
    args = parser.parse_args()

    WEBHOOK_FILE = DATA_DIR / 'discord_webhook.txt'

    if not MODEL_PATH.exists():
        print('モデルが見つかりません。先に train_model.py を実行してください。')
        sys.exit(1)

    _saved = joblib.load(MODEL_PATH)
    stats  = joblib.load(STATS_PATH)

    # アンサンブル / 単一モデル両対応
    if isinstance(_saved, dict) and _saved.get('type') == 'ensemble':
        _models = _saved['models']
    else:
        _models = [_saved]

    # モデルの特徴量数チェック
    expected = len(FEATURE_COLS)
    actual   = getattr(_models[0], 'n_features_in_', None)
    if actual is not None and actual != expected:
        print(f'Warning: feature count mismatch (model={actual}, current={expected})')
        print('Please re-run 3_model_train.bat.')
        sys.exit(1)

    def _predict(X):
        return np.mean([m.predict(X) for m in _models], axis=0)

    jockey_stats        = stats['jockey']
    trainer_stats       = stats['trainer']
    jockey_venue_stats  = stats.get('jockey_venue')
    trainer_venue_stats = stats.get('trainer_venue')
    frame_bias          = stats.get('frame_bias', {})
    jt_stats            = stats.get('jockey_trainer')

    print('レース履歴データをロード中...')
    races_df = load_races_df()
    if races_df.empty:
        print('  races.csv が見つかりません。')
    else:
        print(f'  {len(races_df):,}行 / {races_df["horse_name"].nunique():,}頭分のデータ')

    print(f'レース {args.race_id} のデータを取得中...')
    rows = scrape_race_result(args.race_id) if args.result else scrape_race_entry(args.race_id)

    if not rows:
        print('データを取得できませんでした。レースIDを確認してください。')
        sys.exit(1)

    df = pd.DataFrame(rows)
    race_name  = df['race_name'].iloc[0]
    surface    = df['surface'].iloc[0]
    distance   = df['distance'].iloc[0]
    weather    = df['weather'].iloc[0]
    track      = df['track_condition'].iloc[0]
    venue_code = args.race_id[4:6]
    venue_name = VENUE_NAME.get(venue_code, '')

    df['finishing_pos_num'] = pd.to_numeric(df.get('finishing_pos', pd.Series()), errors='coerce')

    # 過去成績・末脚・乗り替わり・クラス変動特徴量を付与
    print('馬の過去成績を集計中...')
    df = attach_horse_history(df, races_df, args.race_id, surface, distance, race_name, track)

    # 直近60日騎手・調教師成績（ホットストリーク）
    if not races_df.empty:
        races_df['_fpos_r'] = pd.to_numeric(races_df['finishing_pos'], errors='coerce')
        races_df['_dt_r']   = pd.to_datetime(
            races_df['race_date'].astype(str).str[:8], format='%Y%m%d', errors='coerce'
        )
        cutoff60 = pd.Timestamp.now() - pd.Timedelta(days=60)
        r60 = races_df[(races_df['_dt_r'] >= cutoff60) & races_df['_fpos_r'].notna()].copy()
        if not r60.empty:
            r60['_t3_r'] = (r60['_fpos_r'] <= 3).astype(float)
            j60 = r60.groupby('jockey')['_t3_r'].apply(
                lambda x: x.mean() if len(x) >= 5 else np.nan
            ).to_dict()
            t60 = r60.groupby('trainer')['_t3_r'].apply(
                lambda x: x.mean() if len(x) >= 5 else np.nan
            ).to_dict()
            df['jockey_recent60_top3']  = df['jockey'].map(j60)
            df['trainer_recent60_top3'] = df['trainer'].map(t60)

    # race_month: 全馬共通の季節特徴量
    race_date_str = str(df['race_date'].iloc[0])[:8] if not df.empty else ''
    # スクレイパーが実開催日を取れなかった場合（race_id先頭8桁の代用値）は
    # 当日の日付を使う（予測は当日実行が前提）
    if not race_date_str or race_date_str == str(args.race_id)[:8]:
        race_date_str = pd.Timestamp.now().strftime('%Y%m%d')
        df['race_date'] = race_date_str
    rdt = pd.to_datetime(race_date_str, format='%Y%m%d', errors='coerce')
    df['race_month'] = float(rdt.month) if not pd.isna(rdt) else np.nan

    # 血統取得（予測前に行い、blood特徴量をモデル入力に使う）
    pedigree_map = {}
    if not args.no_pedigree:
        horse_ids = [str(h).strip() for h in df['horse_id'].dropna().unique() if str(h).strip()]
        if horse_ids:
            print(f'{len(horse_ids)}頭の血統を取得中...')
            for hid in horse_ids:
                ped = scrape_horse_pedigree(hid)
                if ped:
                    pedigree_map[hid] = ped

    # 血統特徴量をモデル入力用に設定
    for col in ['sire_ryoyo', 'sire_stay', 'sire_speed', 'dam_sire_ryoyo']:
        df[col] = np.nan
    if pedigree_map:
        for i, row in df.iterrows():
            hid      = str(row.get('horse_id', '')).strip()
            ped      = pedigree_map.get(hid, {})
            sire     = str(ped.get('sire', ''))
            dam_sire = str(ped.get('dam_sire', ''))
            df.at[i, 'sire_ryoyo']     = float(sire in _RYOYO_SIRES)
            df.at[i, 'sire_stay']      = float(sire in _STAY_SIRES)
            df.at[i, 'sire_speed']     = float(sire in _SPEED_SIRES)
            df.at[i, 'dam_sire_ryoyo'] = float(dam_sire in _RYOYO_SIRES)

    df = engineer_features(
        df, jockey_stats, trainer_stats, jockey_venue_stats, trainer_venue_stats,
        frame_bias=frame_bias, jt_stats=jt_stats
    )

    X    = df[FEATURE_COLS].fillna(df[FEATURE_COLS].median()).values
    pred = _predict(X)

    df['pred_score'] = pred
    # LGBMRanker: スコアが高いほど上位 → 降順でランク付け
    df['final_rank'] = df['pred_score'].rank(method='min', ascending=False).astype(int)
    df = df.sort_values('final_rank').reset_index(drop=True)

    # ---- 勝率較正・期待値（EV）計算 ----
    print('現在オッズを取得してEVを計算中...')
    df, ev_recs, live_odds_map = compute_ev(df, args.race_id)
    umaren_recs = compute_umaren(df, args.race_id) if live_odds_map else []

    # 調教データ取得（予測時のみ、結果モードではスキップ）
    training_map = {}
    if not args.result and not args.no_pedigree:
        print('追い切り情報を取得中...')
        from scraper import scrape_training_data
        training_map = scrape_training_data(args.race_id)
        if training_map:
            print(f'  {len(training_map)}頭分の調教データを取得')
        else:
            print('  追い切り情報は取得できませんでした')

    # 詳細分析用の履歴データ（表示用）
    history_map = {}
    for hn in df['horse_name'].unique():
        history_map[str(hn)] = build_history_df(str(hn), races_df)

    def get_ped(row):
        return pedigree_map.get(str(row.get('horse_id', '')).strip(), {})

    def get_hist(row):
        return history_map.get(str(row.get('horse_name', '')), pd.DataFrame())

    # ---- サマリー出力 ----
    cond = '  '.join(p for p in [
        f'{surface}{distance}m' if distance and surface else '',
        f'天候:{weather}' if weather else '',
        f'馬場:{track}' if track else '',
        f'[{venue_name}]' if venue_name else '',
    ] if p)

    W = 75
    print(f'\n{"="*W}')
    print(f'【{race_name}】')
    if cond:
        print(cond)
    print(f'{"="*W}')
    print(f'  {"予想":<4} {"馬名":<20} {"騎手":<12} {"性齢":<4} {"近走":<7} {"3着内":<7} {"末脚":<6} {"前走":<4} {"乗替"}')
    print(f'  {"-"*W}')

    for _, row in df.iterrows():
        fp     = row.get('finishing_pos')
        actual = f" → 実{fp}着" if pd.notna(fp) and str(fp) not in ['', 'None', 'nan'] else ''

        r3     = _fmt(row.get('horse_recent3_avg'),      '.1f', '着')
        ctr    = _fmt(row.get('horse_career_top3_rate'), '.0%')
        l3f    = _fmt(row.get('prev_last3f'),            '.1f')
        prev_p = _fmt(row.get('prev_pos'),               '.0f', '着')
        jchg   = ('↑乗替' if row.get('jockey_change_flag') == 1.0 else '  同騎')  \
                 if pd.notna(row.get('jockey_change_flag')) else '  ---'

        print(f"  {row['final_rank']:>2}着  "
              f"{str(row['horse_name']):<20} "
              f"{str(row.get('jockey','')):<12} "
              f"{str(row.get('sex_age','')):<4} "
              f"{r3:<7} {ctr:<7} {l3f:<6} {prev_p:<4} {jchg}"
              f"{actual}")

    print(f'\n{"="*W}')
    print('★★★ 予想印 ★★★')
    for i, (_, row) in enumerate(df.head(5).iterrows()):
        parts = []
        r3   = row.get('horse_recent3_avg')
        ctr  = row.get('horse_career_top3_rate')
        days = row.get('prev_days_interval')
        jchg = row.get('jockey_change_flag')
        cdrop = row.get('class_drop')
        if pd.notna(r3):   parts.append(f'近走{r3:.1f}着平均')
        if pd.notna(ctr):  parts.append(f'3着内率{ctr:.0%}')
        if pd.notna(days): parts.append(f'中{int(days)}日')
        if jchg == 1.0:    parts.append('乗替')
        if pd.notna(cdrop) and cdrop != 0:
            parts.append(f'{"降" if cdrop > 0 else "昇"}級{"+" if cdrop > 0 else ""}{int(cdrop)}')
        extra = f'  ({", ".join(parts)})' if parts else ''
        print(f'  {MARKS[i]}  {row["horse_name"]}{extra}')
    print(f'{"="*W}')

    # ---- 期待値（EV）と買い推奨 ----
    print(f'\n{"="*W}')
    print('【期待値（EV = ブレンド勝率 × 現在オッズ）】')
    if live_odds_map:
        for i, (_, row) in enumerate(df.head(5).iterrows()):
            pw, o, evv = row.get('p_bet'), row.get('live_odds'), row.get('ev')
            pw_s = f'{pw*100:4.1f}%' if pd.notna(pw) else ' ---'
            o_s  = f'{o:6.1f}倍' if pd.notna(o) else '   ---'
            ev_s = f'EV={evv:.2f}' if pd.notna(evv) else 'EV=---'
            print(f'  {MARKS[i]} {str(row["horse_name"]):<14} 勝率{pw_s}  {o_s}  {ev_s}')
        if ev_recs:
            print(f'\n  💰 買い推奨（検証済み条件: ◎EV≥{EV_MAIN_TH} / 他EV≥{EV_LONG_TH}、オッズ{EV_ODDS_CAP:.0f}倍以下）:')
            for kind, row in ev_recs:
                kp = kelly_pct(row['p_bet'], row['live_odds'])
                print(f'    ▶ {kind}  {row["horse_name"]}  '
                      f'(勝率{row["p_bet"]*100:.1f}% × {row["live_odds"]:.1f}倍 = EV {row["ev"]:.2f}'
                      f' / 推奨賭け金: 資金の{kp:.1f}%)')
        else:
            print('\n  買い推奨なし（期待値の閾値を満たす馬なし → 見送り推奨）')
        if umaren_recs:
            print(f'\n  🎲 馬連（試験運用: EV≥{UMAREN_EV_TH}・{UMAREN_ODDS_CAP:.0f}倍以下）:')
            for c in umaren_recs:
                print(f'    ▶ 馬連 {c["nums"]}  {c["names"]}  '
                      f'({c["p"]*100:.1f}% × {c["odds"]:.1f}倍 = EV {c["ev"]:.2f})')
    else:
        print('  オッズ未取得のためEV計算をスキップ（勝率のみ参考）')
        for i, (_, row) in enumerate(df.head(5).iterrows()):
            pw = row.get('p_win')
            if pd.notna(pw):
                print(f'  {MARKS[i]} {str(row["horse_name"]):<14} 勝率{pw*100:4.1f}%')
    print(f'{"="*W}')

    # 買い推奨を記録（実運用の成績検証用。結果検証モードでは記録しない）
    if not args.result and (ev_recs or umaren_recs):
        log_recommendations(ev_recs, args.race_id, race_name, combo_recs=umaren_recs)

    # ---- 詳細診断 ----
    print('\n\n' + '='*W)
    print('【各馬詳細診断】')
    print('='*W)

    for i, (_, row) in enumerate(df.head(5).iterrows()):
        mark   = MARKS[i]
        hist   = get_hist(row)
        ped    = get_ped(row)
        hw     = row.get('horse_weight_num')
        hw_str = f'{int(hw)}kg' if pd.notna(hw) and hw > 0 else '未計量'
        wc     = row.get('weight_change')
        wc_str = f'({int(wc):+d}kg)' if pd.notna(wc) and wc != 0 else ''

        jt     = row.get('jockey_top3_rate')
        jvt    = row.get('jockey_venue_top3_rate')
        jr60   = row.get('jockey_recent60_top3')
        avg_jt = df['jockey_top3_rate'].mean()
        if pd.notna(jt):
            if jt >= avg_jt * 1.15:
                jockey_eval = f'（全体{jt*100:.1f}%▲'
            elif jt < avg_jt * 0.85:
                jockey_eval = f'（全体{jt*100:.1f}%▽'
            else:
                jockey_eval = f'（全体{jt*100:.1f}%'
            if pd.notna(jvt):
                jockey_eval += f' / {venue_name}{jvt*100:.1f}%'
            if pd.notna(jr60):
                hot = '↑HOT' if jr60 > (jt + 0.05) else ('↓COLD' if jr60 < (jt - 0.05) else '')
                jockey_eval += f' / 直近60日{jr60*100:.1f}%{hot}'
            jockey_eval += '）'
        else:
            jockey_eval = ''

        # 前走情報サマリ
        prev_p  = row.get('prev_pos')
        prev_mg = row.get('prev_margin_num')
        days_i  = row.get('prev_days_interval')
        l3f     = row.get('prev_last3f')
        jchg    = row.get('jockey_change_flag')
        cdrop   = row.get('class_drop')
        tr60    = row.get('trainer_recent60_top3')
        rota    = []
        if pd.notna(prev_p):
            mg_s = ''
            if pd.notna(prev_mg) and prev_p > 1:
                if   prev_mg <= 0.3: mg_s = f'({["ハナ","アタマ","クビ"][int(prev_mg*10)-1]}差)'
                elif prev_mg <= 1.0: mg_s = f'({prev_mg:.1f}馬身差)'
                else:                mg_s = f'({prev_mg:.0f}馬身差)'
            rota.append(f'前走{int(prev_p)}着{mg_s}')
        if pd.notna(days_i): rota.append(f'中{int(days_i)}日')
        if pd.notna(l3f):    rota.append(f'上がり{l3f:.1f}')
        if jchg == 1.0:      rota.append('★乗り替わり')
        if pd.notna(cdrop) and cdrop != 0:
            lbl = f'降級(+{int(cdrop)})' if cdrop > 0 else f'昇級({int(cdrop)})'
            rota.append(lbl)
        rota_s = '  ' + ' / '.join(rota) if rota else ''

        # 調教情報
        train_info = training_map.get(str(row.get('horse_name', '')), {})
        train_s = ''
        if train_info:
            parts_t = []
            if train_info.get('eval'):   parts_t.append(f"評価:{train_info['eval']}")
            if train_info.get('course'): parts_t.append(train_info['course'])
            if train_info.get('time'):   parts_t.append(f"最終1F:{train_info['time']}秒")
            if train_info.get('effort'): parts_t.append(train_info['effort'])
            if parts_t:
                train_s = f'  [調教: {" / ".join(parts_t)}]'

        # 調教師の直近成績
        trainer_note = ''
        if pd.notna(tr60):
            tt = row.get('trainer_top3_rate', np.nan)
            if pd.notna(tt) and tr60 > (tt + 0.05):
                trainer_note = ' ↑HOT'
            elif pd.notna(tt) and tr60 < (tt - 0.05):
                trainer_note = ' ↓COLD'

        print(f'\n{"─"*W}')
        print(f' {mark} {row["horse_name"]}')
        print(f'   {row.get("sex_age","")}  騎手:{row.get("jockey","")}{jockey_eval}')
        print(f'   調教師:{row.get("trainer","")}{trainer_note}  馬体重:{hw_str}{wc_str}{rota_s}')
        if train_s:
            print(f'  {train_s}')
        print(f'{"─"*W}')

        analysis_lines = generate_analysis(
            horse_name=str(row['horse_name']),
            row=row,
            pedigree=ped,
            history_df=hist,
            race_surface=surface,
            race_distance=distance,
            race_venue_code=venue_code,
        )
        for line in analysis_lines:
            print(f'  {line}')

    print(f'\n{"="*W}')
    print('※ LGBMRanker（ランキング学習）+ 騎手/調教師×コース相性 + 馬の過去成績')
    print(f'{"="*W}\n')

    # ---- 展開予測 ----
    _print_pace_prediction(df, races_df)

    if args.result:
        top3 = set(df.head(3)['horse_name'].tolist())
        actual_top3 = set(df[df['finishing_pos_num'] <= 3]['horse_name'].tolist())
        hit = len(top3 & actual_top3)
        win = df.iloc[0]['finishing_pos_num'] == 1
        print(f'検証結果: 1着的中={win}  3着以内的中={hit}/3頭')

    # ---- Discord 送信 ----
    if args.discord:
        # 環境変数優先（GitHub Actions用）、なければローカルのファイルから
        webhook_url = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
        if not webhook_url and WEBHOOK_FILE.exists():
            webhook_url = WEBHOOK_FILE.read_text(encoding='utf-8-sig').strip()
        if not webhook_url:
            print('\nDiscord: webhook URL がありません（discord_webhook.txt か環境変数を設定）。')
        else:
            # ペース情報を文字列で組み立て
            style_counts = {}
            for _, r in df.iterrows():
                s = _get_horse_running_style(str(r.get('horse_name', '')), races_df)
                style_counts[s] = style_counts.get(s, 0) + 1
            n_front = style_counts.get('逃げ', 0) + style_counts.get('先行', 0)
            known = sum(v for k, v in style_counts.items() if k)
            if known >= 3:
                ratio = n_front / known
                if style_counts.get('逃げ', 0) >= 3 or ratio >= 0.5:
                    pace_info = 'ハイペース予想（差し・追込有利）'
                elif style_counts.get('逃げ', 0) == 0 or ratio <= 0.2:
                    pace_info = 'スローペース予想（逃げ・先行有利）'
                else:
                    pace_info = '平均ペース予想（実力通り）'
            else:
                pace_info = ''

            msg, embeds = _build_discord_message(
                df, race_name, surface, distance, track, venue_name, races_df, pace_info,
                ev_recs=ev_recs, has_odds=bool(live_odds_map), umaren_recs=umaren_recs,
            )
            print('\nDiscordに送信中...')
            ok = send_discord(msg, webhook_url, embeds=embeds)
            print('  送信完了！' if ok else '  送信失敗。')


if __name__ == '__main__':
    main()
