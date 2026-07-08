import re
import time
import requests
from bs4 import BeautifulSoup

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}
BASE_DB = 'https://db.netkeiba.com'
BASE_RACE = 'https://race.netkeiba.com'


def _get(url, params=None, encoding='EUC-JP'):
    res = requests.get(url, params=params, headers=HEADERS, timeout=20)
    res.encoding = encoding
    return res


JRA_VENUES = {'01','02','03','04','05','06','07','08','09','10'}

def get_race_ids(year, delay=1.5, jra_only=True, start_mon=1, end_mon=12):
    """指定年・月範囲のレースIDを取得（デフォルトはJRAのみ）"""
    ids = set()
    page = 1
    while True:
        params = dict(pid='race_list', start_year=year, start_mon=start_mon,
                      end_year=year, end_mon=end_mon, action='OK', page=page)
        try:
            res = _get(BASE_DB + '/', params=params)
        except Exception as e:
            print(f'  ID収集エラー(p{page}): {e}')
            time.sleep(delay * 3)
            break
        found = set(re.findall(r'/race/(\d{12})/', res.text))
        if not found:
            break
        if jra_only:
            found = {r for r in found if r[4:6] in JRA_VENUES}
        ids |= found
        soup = BeautifulSoup(res.text, 'lxml')
        has_next = any(a.get_text(strip=True) == '次'
                       for a in soup.find_all('a', href=True))
        if not has_next:
            break
        page += 1
        time.sleep(delay)
    return sorted(ids)


def get_today_races(delay=1.0):
    """
    本日のJRAレース一覧と発走時刻を取得。
    Returns: list of {'race_id': str, 'start_time': 'HH:MM', 'race_name': str, 'venue': str}
    """
    from datetime import date
    today = date.today().strftime('%Y%m%d')
    url = f'{BASE_RACE}/top/race_list_sub.html?kaisai_date={today}'
    try:
        res = _get(url, encoding='UTF-8')
    except Exception as e:
        print(f'  レース一覧取得エラー: {e}')
        return []

    soup = BeautifulSoup(res.text, 'lxml')
    races = []

    for item in soup.find_all('li', class_=re.compile(r'RaceList_DataItem')):
        a = item.find('a', href=re.compile(r'race_id=\d{12}'))
        if not a:
            continue
        m = re.search(r'race_id=(\d{12})', a.get('href', ''))
        if not m:
            continue
        race_id = m.group(1)
        if race_id[4:6] not in JRA_VENUES:
            continue

        # 発走時刻（クラス名: RaceList_Itemtime）
        time_tag = item.find(class_=re.compile(r'RaceList_Itemtime|ItemTime', re.IGNORECASE))
        start_time = time_tag.get_text(strip=True) if time_tag else ''
        if not re.match(r'\d{1,2}:\d{2}', start_time):
            start_time = ''

        # レース名（クラス名: ItemTitle）
        name_tag = item.find(class_=re.compile(r'ItemTitle', re.IGNORECASE))
        race_name = name_tag.get_text(strip=True) if name_tag else ''

        # 会場名（race_idの会場コードから）
        venue_map = {
            '01':'札幌','02':'函館','03':'福島','04':'新潟','05':'東京',
            '06':'中山','07':'中京','08':'京都','09':'阪神','10':'小倉'
        }
        venue = venue_map.get(race_id[4:6], '')

        races.append({'race_id': race_id, 'start_time': start_time,
                      'race_name': race_name, 'venue': venue})

    time.sleep(delay)
    return races


def scrape_race_result(race_id, delay=1.5):
    """レース結果ページをスクレイピング → list of dict"""
    try:
        res = _get(f'{BASE_DB}/race/{race_id}/')
    except Exception as e:
        print(f'  Error {race_id}: {e}')
        return []

    soup = BeautifulSoup(res.text, 'lxml')
    info = _parse_info_db(soup, race_id)

    table = soup.find('table', class_='race_table_01')
    if not table:
        time.sleep(delay)
        return []

    rows = []
    for tr in table.find_all('tr')[1:]:
        td = tr.find_all('td')
        if len(td) < 11:
            continue

        # 馬IDを取得（horse/XXXXXXXXXX/ 形式のリンクから）
        horse_id = ''
        a = td[3].find('a', href=re.compile(r'/horse/\d+/'))
        if a:
            m = re.search(r'/horse/(\d+)/', a.get('href', ''))
            if m:
                horse_id = m.group(1)

        row = dict(info)
        row.update({
            'horse_id':       horse_id,
            'finishing_pos':  td[0].get_text(strip=True),
            'frame':          td[1].get_text(strip=True),
            'horse_num':      td[2].get_text(strip=True),
            'horse_name':     td[3].get_text(strip=True),
            'sex_age':        td[4].get_text(strip=True),
            'weight_carried': td[5].get_text(strip=True),
            'jockey':         td[6].get_text(strip=True),
            'time':           td[7].get_text(strip=True),
            'margin':         td[8].get_text(strip=True),
            'popularity':     td[9].get_text(strip=True) if len(td) > 9 else '',
            'odds':           td[10].get_text(strip=True) if len(td) > 10 else '',
            'last_3f':        td[11].get_text(strip=True) if len(td) > 11 else '',
            'trainer':        td[13].get_text(strip=True) if len(td) > 13 else '',
            'horse_weight':   td[14].get_text(strip=True) if len(td) > 14 else '',
            # コーナー（後でfill）
            'c1_pos': '', 'c2_pos': '', 'c3_pos': '', 'c4_pos': '', 'running_style': '',
        })
        rows.append(row)

    # コーナー通過順位を解析して各馬に付与
    field_size = len(rows)
    corner_data = _parse_corner_positions(soup)
    if corner_data:
        for row in rows:
            try:
                hnum = int(row.get('horse_num', 0))
            except (ValueError, TypeError):
                hnum = 0
            cd = corner_data.get(hnum, {})
            row['c1_pos'] = cd.get('c1', '')
            row['c2_pos'] = cd.get('c2', '')
            row['c3_pos'] = cd.get('c3', '')
            row['c4_pos'] = cd.get('c4', '')
            c1 = cd.get('c1')
            row['running_style'] = _corner_running_style(c1, field_size) if c1 else ''

    time.sleep(delay)
    return rows


def scrape_race_entry(race_id, delay=1.5):
    """出馬表をスクレイピング（予測用）→ list of dict"""
    url = f'{BASE_RACE}/race/shutuba.html?race_id={race_id}'
    try:
        res = _get(url, encoding='UTF-8')
    except Exception as e:
        print(f'  Error {race_id}: {e}')
        return []

    soup = BeautifulSoup(res.text, 'lxml')
    info = _parse_info_race(soup, race_id)

    table = soup.find('table', class_='Shutuba_Table')
    if not table:
        time.sleep(delay)
        return []

    entries = []
    for tr in table.find_all('tr', class_=re.compile('HorseList')):
        td = tr.find_all('td')
        if len(td) < 5:
            continue

        # 馬名取得
        hn = tr.find('span', class_='HorseName') or tr.find('a', attrs={'data-horse_id': True})
        horse_name = hn.get_text(strip=True) if hn else ''

        # 馬IDを取得（data-horse_id属性 or /horse/XXXXXXXXXX リンク）
        horse_id = ''
        el = tr.find(attrs={'data-horse_id': True})
        if el:
            horse_id = el.get('data-horse_id', '')
        if not horse_id:
            a = tr.find('a', href=re.compile(r'/horse/\d+'))
            if a:
                m = re.search(r'/horse/(\d+)', a.get('href', ''))
                if m:
                    horse_id = m.group(1)

        row = dict(info)
        row.update({
            'horse_id':       horse_id,
            'finishing_pos':  None,
            'frame':          _find(tr, 'td', 'Waku'),
            'horse_num':      _find(tr, 'td', 'Umaban'),
            'horse_name':     horse_name,
            'sex_age':        _find(tr, 'td', 'Barei'),
            'weight_carried': _find(tr, 'td', 'Futan'),
            'jockey':         _find(tr, 'td', 'Jockey').split()[0] if _find(tr, 'td', 'Jockey') else '',
            'trainer':        _find(tr, 'td', 'Trainer').split()[0] if _find(tr, 'td', 'Trainer') else '',
            'horse_weight':   _find(tr, 'td', 'Weight'),
            'odds':           _find(tr, 'span', 'OddsVal') or _find(tr, 'td', 'Odds'),
            'popularity':     _find(tr, 'td', 'Ninki') or _find(tr, 'td', 'Popular'),
            'time': '', 'margin': '', 'last_3f': '',
        })
        entries.append(row)

    time.sleep(delay)
    return entries


def scrape_training_data(race_id, delay=1.0):
    """
    追い切り情報を取得 → {horse_name: {'eval': 'A', 'course': '坂路', 'time': '12.1', 'effort': '強め'}}
    netkeiba の oikiri ページをスクレイピング。取得できない場合は {}。
    """
    url = f'{BASE_RACE}/race/oikiri.html?race_id={race_id}'
    try:
        res = _get(url, encoding='UTF-8')
    except Exception as e:
        print(f'  Training scrape error: {e}')
        return {}

    soup = BeautifulSoup(res.text, 'lxml')
    result = {}

    # 馬名ごとのブロックを探索（div/section 形式）
    for horse_div in soup.find_all(class_=re.compile(r'Horse_Training|TrainingHorse|oikiri_horse', re.I)):
        name_el = horse_div.find(class_=re.compile(r'HorseName|horse_name', re.I))
        if not name_el:
            name_el = horse_div.find('a', href=re.compile(r'/horse/'))
        if not name_el:
            continue
        horse_name = name_el.get_text(strip=True)
        if not horse_name:
            continue
        block_text = horse_div.get_text(' ', strip=True)
        result[horse_name] = _parse_training_block(block_text, horse_div)

    # テーブル形式にフォールバック
    if not result:
        for table in soup.find_all('table'):
            ths = [th.get_text(strip=True) for th in table.find_all('th')]
            if not any(k in ' '.join(ths) for k in ['馬名', '評価', '調教']):
                continue
            for tr in table.find_all('tr')[1:]:
                tds = tr.find_all('td')
                if len(tds) < 2:
                    continue
                horse_name = ''
                for td in tds:
                    a = td.find('a', href=re.compile(r'/horse/'))
                    if a:
                        horse_name = a.get_text(strip=True)
                        break
                if not horse_name:
                    continue
                row_text = ' '.join(td.get_text(strip=True) for td in tds)
                result[horse_name] = _parse_training_block(row_text, tr)
            if result:
                break

    time.sleep(delay)
    return result


def _parse_training_block(text, tag):
    """テキストブロックから評価・コース・タイム・追い方を抽出"""
    info = {'eval': '', 'course': '', 'time': '', 'effort': ''}
    # 評価: S/A/B/C/D 単独
    m = re.search(r'\b([SABCDsabcd])\b', text)
    if m:
        info['eval'] = m.group(1).upper()
    # コース: 坂路/W/ウッド/芝/ダ/ポリ
    for course in ['坂路', 'ウッドチップ', 'ウッド', 'W', '芝', 'ダート', 'ポリトラック', 'ポリ']:
        if course in text:
            info['course'] = course
            break
    # タイム: 数字.数字 (最終1Fタイム想定)
    times = re.findall(r'\d{2}\.\d', text)
    if times:
        info['time'] = times[-1]  # 最後が最終1Fに近い
    # 追い方
    for effort in ['馬なり', '強め', '一杯', '手ぶら', 'G前仕掛', '末強め']:
        if effort in text:
            info['effort'] = effort
            break
    return info


def scrape_horse_pedigree(horse_id, delay=1.0):
    """馬の血統情報（父・母父）を取得（/horse/ped/ ページから）"""
    try:
        res = _get(f'{BASE_DB}/horse/ped/{horse_id}/')
    except Exception as e:
        print(f'  Error ped {horse_id}: {e}')
        return {'horse_id': horse_id, 'sire': '', 'dam_sire': ''}

    soup = BeautifulSoup(res.text, 'lxml')
    result = {'horse_id': horse_id, 'sire': '', 'dam_sire': ''}

    blood = soup.find('table', class_='blood_table')
    if blood:
        all_tds = blood.find_all('td')
        # 最大rowspan = 父と母が持つ値（世代数によって8 or 16 or 32）
        rowspans = [int(td.get('rowspan', 0)) for td in all_tds if td.get('rowspan')]
        if not rowspans:
            return result
        max_rs = max(rowspans)
        half_rs = max_rs // 2

        # 父: max_rs を持つ最初のセル
        rs_max = [td for td in all_tds if td.get('rowspan') == str(max_rs)]
        if rs_max:
            result['sire'] = _clean_pedigree_name(rs_max[0].get_text(strip=True))

        # 母父: half_rs を持つセルのうち3番目（父父・父母・母父・母母の順）
        rs_half = [td for td in all_tds if td.get('rowspan') == str(half_rs)]
        if len(rs_half) >= 3:
            result['dam_sire'] = _clean_pedigree_name(rs_half[2].get_text(strip=True))

    time.sleep(delay)
    return result


def _parse_corner_order(text):
    """
    コーナー通過テキスト '3-5,1=12-8' → {horse_num: position}
    '-' でグループ分け、',' '=' は同グループ扱い
    """
    positions = {}
    pos = 1
    for group in re.split(r'-', text.strip()):
        for token in re.split(r'[,=]', group.strip()):
            token = token.strip()
            try:
                horse_num = int(token)
                positions[horse_num] = pos
            except ValueError:
                pass
        pos += 1
    return positions


def _parse_corner_positions(soup):
    """
    レース結果ページのコーナー通過順位を解析。
    戻り値: {horse_num: {'c1': pos, 'c2': pos, 'c3': pos, 'c4': pos}} （取得できない場合 {}）
    """
    corner_text = ''

    # 方法1: result_order 系のクラス名
    for tag in soup.find_all(['div', 'p', 'td', 'section']):
        cls = ' '.join(tag.get('class', []))
        if 'result_order' in cls or 'corner' in cls.lower():
            t = tag.get_text(' ', strip=True)
            if re.search(r'[1234]コーナー|[1234]C', t):
                corner_text = t
                break

    # 方法2: "コーナー通過順位" テキストを含む要素
    if not corner_text:
        for tag in soup.find_all(['div', 'p', 'td']):
            t = tag.get_text(' ', strip=True)
            if 'コーナー通過' in t and re.search(r'\d+-\d+', t):
                corner_text = t
                break

    if not corner_text:
        return {}

    result = {}
    for i in range(1, 5):
        pattern = rf'{i}\s*(?:コーナー|C)[:\s]*([0-9,=\-\s]+?)(?=\s*[1-4]\s*(?:コーナー|C)|$)'
        m = re.search(pattern, corner_text)
        if not m:
            continue
        order_str = re.sub(r'\s+', '', m.group(1))
        positions = _parse_corner_order(order_str)
        for horse_num, p in positions.items():
            if horse_num not in result:
                result[horse_num] = {}
            result[horse_num][f'c{i}'] = p

    return result


def _corner_running_style(c1_pos, field_size):
    """1コーナー位置と頭数から脚質を判定"""
    if c1_pos is None or field_size <= 0:
        return ''
    ratio = c1_pos / field_size
    if ratio <= 0.12: return '逃げ'
    if ratio <= 0.38: return '先行'
    if ratio <= 0.68: return '差し'
    return '追込'


def _clean_pedigree_name(raw):
    """血統名から年・毛色・タグを除去してクリーンな馬名を返す"""
    name = re.sub(r'\d{4}.*', '', raw).strip()
    name = re.sub(r'\[.*', '', name).strip()
    name = re.sub(r'\(.*?\)', '', name).strip()
    # 日本語名+英語名が連結している場合: "ジェネラスGenerous" → "ジェネラス"
    # 日本語(非ASCII)で始まる名前の末尾ASCII部分を除去
    if re.match(r'[^\x00-\x7F]', name):
        name = re.sub(r'[A-Za-z\s]+$', '', name).strip()
    return name


def _parse_info_db(soup, race_id):
    info = {'race_id': race_id, 'race_name': '', 'race_date': race_id[:8],
            'surface': '', 'distance': None, 'weather': '', 'track_condition': ''}
    div = soup.find('div', class_='data_intro')
    if div:
        text = div.get_text(' ', strip=True)
        _fill_conditions(text, info)
        m = re.search(r'\d+\s*R\s*(.+?)\s*[芝ダ障]', text)
        if m:
            info['race_name'] = m.group(1).strip()
        # 実際の開催日を抽出（"2025年10月5日" 形式）
        m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', text)
        if m:
            info['race_date'] = f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    return info


def _parse_info_race(soup, race_id):
    info = {'race_id': race_id, 'race_name': '', 'race_date': race_id[:8],
            'surface': '', 'distance': None, 'weather': '', 'track_condition': ''}
    for cls in ['RaceName', 'race_name']:
        el = soup.find(class_=cls)
        if el:
            info['race_name'] = el.get_text(strip=True)
            break
    for cls in ['RaceData01', 'RaceData02']:
        el = soup.find(class_=cls)
        if el:
            _fill_conditions(el.get_text(' ', strip=True), info)
            break
    return info


def _fill_conditions(text, info):
    # 芝右1800m / ダ左1400m / 障2450m など方向文字付きに対応
    m = re.search(r'([芝ダ障])[右左外内]?(\d+)m', text)
    if m:
        info['surface'] = m.group(1)
        info['distance'] = int(m.group(2))
    m = re.search(r'天候\s*[:/]\s*(\S+)', text)
    if m:
        info['weather'] = m.group(1)
    # "芝 : 良" または "ダート : 重" または "馬場 : 良" 形式
    m = re.search(r'(?:芝|ダート|馬場)\s*:\s*([良稍重不][良重]?)', text)
    if m:
        info['track_condition'] = m.group(1)


def _find(tag, el_type, cls):
    el = tag.find(el_type, class_=cls)
    return el.get_text(strip=True) if el else ''
