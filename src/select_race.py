"""
レース選択スクリプト

使い方:
  python select_race.py          # 日付を対話入力
  python select_race.py 20260628 # 日付指定（YYYYMMDD）
"""
import re
import sys
import subprocess
from pathlib import Path
from datetime import date

import requests
from bs4 import BeautifulSoup

HEADERS_SP = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) '
                  'AppleWebKit/605.1.15 (KHTML, like Gecko) '
                  'Version/15.0 Mobile/15E148 Safari/604.1'
}

JRA_VENUES = {'01', '02', '03', '04', '05', '06', '07', '08', '09', '10'}

VENUE_NAME = {
    '01': '札幌', '02': '函館', '03': '福島', '04': '新潟',
    '05': '東京', '06': '中山', '07': '中京',
    '08': '京都', '09': '阪神', '10': '小倉',
}


def fetch_races_on_date(target_date):
    """netkeiba SPサイトから指定日の全JRAレース一覧を取得"""
    target_str = target_date.strftime('%Y%m%d')

    try:
        res = requests.get(
            'https://race.sp.netkeiba.com/',
            params={'pid': 'race_list', 'kaisai_date': target_str},
            headers=HEADERS_SP,
            timeout=20,
        )
        res.encoding = 'UTF-8'
    except Exception as e:
        print(f'  取得エラー: {e}')
        return []

    soup = BeautifulSoup(res.text, 'lxml')

    # kaisai_date ごとにラップされた div を探す
    target_wrap = None
    for wrap in soup.find_all(class_='RaceListDayWrap'):
        dates_in_wrap = [
            el.get('data-kaisaidate', '')
            for el in wrap.find_all(attrs={'data-kaisaidate': True})
        ]
        if target_str in dates_in_wrap:
            target_wrap = wrap
            break

    if not target_wrap:
        return []

    races = []
    seen = set()

    for box in target_wrap.find_all(class_='RaceList_Main_Box'):
        a = box.find('a', href=re.compile(r'race_id=\d{12}'))
        if not a:
            continue
        m = re.search(r'race_id=(\d{12})', a.get('href', ''))
        if not m:
            continue
        race_id = m.group(1)
        if race_id in seen or race_id[4:6] not in JRA_VENUES:
            continue
        seen.add(race_id)

        text = box.get_text(' ', strip=True)
        # 例: "11R 函館記念 15:20 芝2000m 16頭"
        race_r   = re.search(r'(\d+)R',   text)
        time_m   = re.search(r'(\d+:\d+)', text)
        dist_m   = re.search(r'([芝ダ障]\d+m)', text)
        # レース名: Rと時刻の間
        r_end    = race_r.end() if race_r else 0
        t_start  = time_m.start() if time_m else len(text)
        race_name = text[r_end:t_start].strip()

        venue = VENUE_NAME.get(race_id[4:6], race_id[4:6])
        races.append({
            'race_id': race_id,
            'name':    race_name or f'{venue} {race_r.group(1) if race_r else "?"}R',
            'venue':   venue,
            'race_r':  race_r.group(1) if race_r else race_id[10:12].lstrip('0'),
            'time':    time_m.group(1) if time_m else '',
            'dist':    dist_m.group(1) if dist_m else '',
        })

    races.sort(key=lambda r: r['race_id'])
    return races


def parse_date_input(s):
    """YYYYMMDD / MMDD / 空文字 → date オブジェクト"""
    s = s.strip().replace('/', '').replace('-', '')
    today = date.today()
    if not s:
        return today
    if len(s) == 8:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    if len(s) == 4:
        return date(today.year, int(s[:2]), int(s[2:4]))
    raise ValueError(f'日付形式が不正: {s}')


def display_and_select(races, target_date):
    venues_order = []
    by_venue = {}
    for r in races:
        v = r['venue']
        if v not in by_venue:
            venues_order.append(v)
            by_venue[v] = []
        by_venue[v].append(r)

    print(f'\n{"="*70}')
    print(f'  {target_date.strftime("%Y/%m/%d")} の JRA レース一覧')
    print(f'{"="*70}')
    print(f'  {"#":<4} {"場":<5} {"R":<5} {"レース名":<22} {"発走":<7} {"距離"}')
    print(f'  {"-"*68}')

    idx = 1
    for v in venues_order:
        for r in by_venue[v]:
            r['_idx'] = idx
            race_label = f'{r["race_r"]}R'
            print(f'  {idx:<4} {r["venue"]:<5} {race_label:<5} '
                  f'{r["name"][:22]:<22} {r["time"]:<7} {r["dist"]}')
            idx += 1

    print(f'{"="*70}')
    print(f'\n番号を入力（0で終了）: ', end='', flush=True)
    try:
        choice = int(input().strip())
    except (ValueError, EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if choice == 0:
        sys.exit(0)
    if not (1 <= choice < idx):
        print(f'1〜{idx-1} の番号を入力してください。')
        sys.exit(1)

    return next(r for r in races if r['_idx'] == choice)


def main():
    date_arg = sys.argv[1] if len(sys.argv) > 1 else ''

    if date_arg:
        try:
            target = parse_date_input(date_arg)
        except Exception as e:
            print(f'日付エラー: {e}')
            sys.exit(1)
    else:
        today = date.today()
        print(f'日付を入力してください（例: {today.strftime("%Y%m%d")} / {today.strftime("%m%d")}）')
        print(f'Enterで今日（{today.strftime("%Y/%m/%d")}）: ', end='', flush=True)
        try:
            s = input().strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit(0)
        try:
            target = parse_date_input(s)
        except Exception as e:
            print(f'日付エラー: {e}')
            sys.exit(1)

    print(f'\n{target.strftime("%Y/%m/%d")} のJRAレースを取得中...')
    races = fetch_races_on_date(target)

    if not races:
        print('\nこの日のレースが見つかりませんでした。')
        print('開催日か確認するか、日付を変えてください。')
        print('\nレースIDを直接入力できます（12桁、Enterでスキップ）: ', end='', flush=True)
        try:
            rid = input().strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit(0)
        if rid and re.match(r'^\d{12}$', rid):
            print('\n予測を実行中...\n')
            subprocess.run([sys.executable,
                            str(Path(__file__).parent / 'predict.py'),
                            rid, '--discord'], check=False)
        sys.exit(0)

    selected = display_and_select(races, target)
    print(f'\n選択: 【{selected["name"]}】 {selected["venue"]} {selected["race_r"]}R')
    print('\n予測を実行中...\n')
    subprocess.run([sys.executable,
                    str(Path(__file__).parent / 'predict.py'),
                    selected['race_id'], '--discord'], check=False)


if __name__ == '__main__':
    main()
