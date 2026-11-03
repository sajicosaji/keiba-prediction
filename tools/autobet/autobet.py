"""
海外発売レースの単勝を、即PATで自動で買う係(2026-11-03・ユーザーの指示「PCで作る」「やれ」)。このPCで動く。
  plan : 30分ごと(タスクスケジューラ)。GitHub の data/overseas/site.json から、これから発走する発売レースを読み、
         「発走7分前に bet を動かす」予約をタスクスケジューラに入れる(スリープ中なら起こす。電源が切れていて過ぎた分は動かない)
  bet  : 発走7分前。本番の見張り(src/overseas.py)と同じ計算で、その時のJRA公式オッズ・現地のオッズから買い目を決めて即PATで買う。
         締切は発走2分前なので、発走10〜3分前の間しか動かない
  test : 試し運転。即PATに入り、購入予定リストに入れて合計金額を入れたところで止める(購入はしない)
安全:
  - 最初は試し運転だけ(config.json の live が false)。本番は「本番にする.bat」で、本人が「はい」と入れた時だけ
  - 1レース・1日の上限額。止めるファイル(STOP)があれば何もしない。自動の入金はしない(即PATに入れてある分までしか買えない)
  - ログイン情報は cred.py(このPCだけ・暗号化)。Discord と記録には、レース・馬番・金額・結果・止まった段階だけ
置き場所: %LOCALAPPDATA%\\keiba_autobet(config.json / cred.bin / bets.csv / autobet.log / STOP)
使い方: python tools/autobet/autobet.py {setup|plan|bet --key K|test|live on|live off|stop|resume|status}
"""
import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

HERE = Path(__file__).resolve().parent
BASE = HERE.parents[1]  # netkeiba_prediction
sys.path.insert(0, str(BASE / 'src'))
sys.path.insert(0, str(HERE))
import cred as C  # noqa: E402

JST = ZoneInfo('Asia/Tokyo')
HOME = C.HOME
SITE = 'https://raw.githubusercontent.com/sajicosaji/keiba-prediction/main/data/overseas/site.json'
DEFAULT = {'live': False, 'stake_per_pick': 1000, 'max_per_race': 3000, 'max_per_day': 6000, 'start_min': 7}
TASK = 'keiba_autobet_'
BELOW = '⚠ 基準未満(検証では回収95%)'  # 2026-11-03 5年90Rの検証。全部屋の決まりで買い目の1行目に書く
WINDOW = (3, 10)  # 発走の何分前から何分前までしか買わないか


def config():
    c = dict(DEFAULT)
    f = HOME / 'config.json'
    if f.exists():
        c.update(json.loads(f.read_text(encoding='utf-8')))
    return c


def save_config(c):
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / 'config.json').write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding='utf-8')


def log(msg):
    line = f"{datetime.now(JST):%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    HOME.mkdir(parents=True, exist_ok=True)
    with open(HOME / 'autobet.log', 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def discord(msg, quiet=False):
    if quiet:
        return
    try:
        import g1_post
        wh = g1_post.get_webhook()
        if wh:
            g1_post.post(wh, msg)
    except Exception as e:
        log(f'Discordに送れず: {type(e).__name__}')


def key(slug):
    return hashlib.md5(slug.encode('utf-8')).hexdigest()[:8]


def races():
    return requests.get(SITE, params={'t': int(datetime.now().timestamp() // 60)}, timeout=20).json().get('races', [])


def post_dt(r):
    return datetime.strptime(r['post'], '%Y-%m-%d %H:%M').replace(tzinfo=JST)


# ---------------------------------------------------------------- タスクスケジューラ
def ps(script):
    p = subprocess.run(['powershell.exe', '-NoProfile', '-Command', script], capture_output=True, text=True, encoding='utf-8', errors='replace')
    return p.returncode, (p.stdout + p.stderr).strip()


def register(name, at, args, every_min=None):
    py, me = sys.executable, str(HERE / 'autobet.py')
    trig = (f"New-ScheduledTaskTrigger -Once -At '{at:%Y-%m-%dT%H:%M:%S}'" +
            (f" -RepetitionInterval (New-TimeSpan -Minutes {every_min})" if every_min else ''))
    code, out = ps(f"""
$a = New-ScheduledTaskAction -Execute '{py}' -Argument '"{me}" {args}' -WorkingDirectory '{BASE}'
$t = {trig}
$s = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
Register-ScheduledTask -TaskName '{name}' -Action $a -Trigger $t -Settings $s -Force | Out-Null
""")
    if code:
        raise RuntimeError(f'予約に失敗: {out[-300:]}')


def tasks():
    code, out = ps(f"Get-ScheduledTask -TaskName '{TASK}*' -ErrorAction SilentlyContinue | "
                   "ForEach-Object { $_.TaskName + '|' + ($_.Triggers | Select-Object -First 1).StartBoundary }")
    return dict(line.split('|', 1) for line in out.splitlines() if '|' in line)


def unregister(name):
    ps(f"Unregister-ScheduledTask -TaskName '{name}' -Confirm:$false -ErrorAction SilentlyContinue")


def plan(now):
    """これから48時間に発走する発売レースの「発走7分前」に bet を予約する。発走時刻が変わったら入れ直す"""
    cfg = config()
    have = tasks()
    want = {}
    for r in races():
        if not r.get('post'):
            continue
        post = post_dt(r)
        if now + timedelta(minutes=cfg['start_min'] + 1) < post <= now + timedelta(hours=48):
            want[TASK + key(r['slug'])] = (post - timedelta(minutes=cfg['start_min']), r)
    for name, (at, r) in want.items():
        if not have.get(name, '').startswith(f'{at:%Y-%m-%dT%H:%M}'):
            register(name, at, f'bet --key {name[len(TASK):]}')
            log(f"予約: {r['race']} 発走 {r['post'][5:]} → {at:%m-%d %H:%M} に自動購入を動かす")
    for name, start in have.items():  # 終わったレースの予約を消す(plan 自身は残す)
        if name != TASK + 'plan' and name not in want and start and start[:16] < f'{now:%Y-%m-%dT%H:%M}':
            unregister(name)
    return want


# ---------------------------------------------------------------- 買い目(本番の見張りと同じ計算)
def snapshot_now(slug, now):
    import overseas as ov
    work = HOME / 'ov'
    (work / 'races').mkdir(parents=True, exist_ok=True)
    for f in ('schedule.json', 'config.json'):
        src = BASE / 'data' / 'overseas' / f
        if src.exists():
            shutil.copyfile(src, work / f)
    ov.OUT = work  # 記録はこのPCの中だけに書く(GitHub の記録は本番の見張りが書く)
    st = BASE / 'data' / 'overseas' / 'state.json'
    state = json.loads(st.read_text(encoding='utf-8')) if st.exists() else {}
    d = next((x for x in ov.decided(now) if x['slug'] == slug), None)
    if not d:
        raise LookupError('JRAの発売一覧にレースが無い')
    return ov.snapshot(d, now, state), state, ov


def race_number(r, state, ov):
    """即PATのレース番号(=現地のレース番号)。PMUのレース番号と netkeiba の番号の末尾で照らし合わせる。食い違えば None"""
    nums = set()
    m = re.search(r'C(\d+)', r.get('pmu_race') or '')
    if m:
        nums.add(int(m.group(1)))
    try:
        nk = state.get('nk_ids', {}).get(r['slug']) or ov.nk_race_id(r, state)
        if nk:
            nums.add(int(nk[-2:]))
    except Exception as e:
        log(f'netkeibaの番号を探せず: {type(e).__name__}')
    return nums.pop() if len(nums) == 1 else None


def spent_today(now):
    f = HOME / 'bets.csv'
    if not f.exists():
        return 0
    return sum(int(x['amount']) for x in csv.DictReader(open(f, encoding='utf-8'))
               if x['at'][:10] == f'{now:%Y-%m-%d}' and x['mode'] == '本番' and x['bought'] in ('True', 'None'))


def record(now, r, race_no, orders, mode, res):
    f = HOME / 'bets.csv'
    new = not f.exists()
    with open(f, 'a', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(['at', 'slug', 'race', 'race_no', 'num', 'amount', 'mode', 'bought', 'failed'])
        for n, a in orders:
            w.writerow([f'{now:%Y-%m-%d %H:%M}', r['slug'], r['race'], race_no, n, a, mode, res.get('bought'), res.get('failed') or ''])


def bet(k, now, force_dry=False, no_browser=False, ignore_time=False, quiet=False):
    cfg = config()
    if (HOME / 'STOP').exists():
        log('止めてあるので買いません(STOP)')
        return
    r0 = next((r for r in races() if key(r['slug']) == k), None)
    if not r0:
        log(f'レースが見つからない: {k}')
        return
    post = post_dt(r0)
    mins = (post - now).total_seconds() / 60
    if not ignore_time and not (WINDOW[0] <= mins <= WINDOW[1]):
        log(f"{r0['race']}: 発走{mins:.0f}分前なので動かない(発走{WINDOW[1]}〜{WINDOW[0]}分前だけ)")
        return
    lock = HOME / 'bet.lock'
    if lock.exists() and (datetime.now().timestamp() - lock.stat().st_mtime) < 900:
        log('別の自動購入が動いているので待たずに終わる')
        return
    lock.write_text(str(now))
    try:
        return _bet(r0, post, now, cfg, force_dry, no_browser, quiet)
    finally:
        lock.unlink(missing_ok=True)


def _bet(r0, post, now, cfg, force_dry, no_browser, quiet):
    head = f"{BELOW} 🤖 **自動購入**【{r0['race']}】発走 {post:%H:%M}"
    manual = ''
    try:
        r, state, ov = snapshot_now(r0['slug'], now)
    except Exception as e:
        log(f'買い目の計算に失敗: {type(e).__name__}: {e}')
        discord(f'{head}\n❌ 買い目の計算ができませんでした({type(e).__name__})。Discordの最終版を見て、締切(発走2分前・{post - timedelta(minutes=2):%H:%M})までに手で買ってください。', quiet)
        return
    plan_ = [e for e in sorted(r['entries'], key=lambda e: -(e['ev'] or 0)) if e['pick']]
    stake = int(cfg['stake_per_pick'])
    orders = [(e['num'], stake) for e in plan_]
    names = {e['num']: e for e in plan_}
    if not r['local']['ok'] or not any(e['jra_odds'] for e in r['entries']):
        log('現地かJRAのオッズが無いので判定できない')
        discord(f'{head}\n今回は買いません(現地かJRAのオッズが取れず、判定できない)。', quiet)
        return
    if not orders:
        log('買い目なし')
        discord(f'{head}\n👉 今回は買いません(日本馬以外で、現地より2割以上割安・20倍以下の単勝がない)。', quiet)
        return
    while orders and sum(a for _, a in orders) > cfg['max_per_race']:
        orders.pop()  # 期待値の低い方から外す
    room = cfg['max_per_day'] - spent_today(now)
    while orders and sum(a for _, a in orders) > room:
        orders.pop()
    if not orders:
        discord(f'{head}\n今日の上限({cfg["max_per_day"]:,}円)に達しているので買いません。', quiet)
        return
    manual = ' / '.join(f"単勝 {n}番 {names[n]['name']} {a:,}円" for n, a in orders)
    race_no = race_number(r, state, ov)
    if not race_no:
        log('レース番号が決められない(PMUと netkeiba が食い違うか、どちらも無い)')
        discord(f'{head}\n❌ 即PATのレース番号が確かめられないので、自動では買いません。手で買ってください: **{manual}**', quiet)
        return
    venues = [r.get('track', ''), r.get('country', ''), '海外']
    dry = force_dry or not cfg['live']
    mode = '試し運転' if dry else '本番'
    lines = '\n'.join(f"単勝 {n}番 {names[n]['name']} {a:,}円(JRA {names[n]['jra_odds']:.1f}倍・期待値{names[n]['ev']:.2f})" for n, a in orders)
    log(f"{mode}: {r['race']} {race_no}R {manual}")
    if no_browser:
        log('(即PATは開かない指定)')
        return {'race_no': race_no, 'orders': orders, 'venues': venues}
    cr = C.load()
    if not cr:
        discord(f'{head}\n❌ ログイン情報が入っていないので買えません(「1_ログイン情報を入れる.bat」)。手で買ってください: **{manual}**', quiet)
        return
    import ipat
    res = ipat.buy(cr, venues, race_no, orders, dry=dry, log=log)
    record(now, r, race_no, orders, mode, res)
    total = sum(a for _, a in orders)
    if res['ok'] and res['bought']:
        discord(f"{head}\n✅ **購入しました**({race_no}R・合計{total:,}円)\n{lines}\n即PATの購入履歴でも確かめてください。手で同じ馬券を買わないでください。", quiet)
    elif res['ok'] and dry:
        discord(f"{head}\n🧪 試し運転: 即PATで購入予定リストに入れ、合計{total:,}円を入れるところまで進みました(購入はしていません)。\n{lines}\n"
                f"本番は「3_本番にする.bat」。このレースを買うなら、締切(発走2分前・{post - timedelta(minutes=2):%H:%M})までに手で買ってください。", quiet)
    elif res['bought'] is None:
        discord(f"{head}\n⚠ 「購入」を押した後の確認ができませんでした。**即PATの購入履歴を見て**、買えていなければ手で買ってください: **{manual}**", quiet)
    else:
        discord(f"{head}\n❌ 自動購入が「{res['failed']}」の段階で止まりました。締切(発走2分前・{post - timedelta(minutes=2):%H:%M})までに手で買ってください: **{manual}**", quiet)


# ---------------------------------------------------------------- 試し運転
def test(now, quiet=False):
    """試し運転。いま発売中の海外レースがあればそれで、無ければ今日のJRAのレースで、購入予定リストまで進む(購入はしない)"""
    cr = C.load()
    if not cr:
        print('先に「1_ログイン情報を入れる.bat」でログイン情報を入れてください。')
        return
    import ipat
    soon = [r for r in races() if r.get('post') and now + timedelta(minutes=3) < post_dt(r) <= now + timedelta(hours=12)]
    if soon:
        r = min(soon, key=post_dt)
        try:
            out = bet(key(r['slug']), now, force_dry=True, no_browser=True, ignore_time=True, quiet=True) or {}
        except Exception as e:
            out = {}
            log(f'海外レースの準備に失敗: {type(e).__name__}')
        if out.get('race_no'):
            num = out['orders'][0][0] if out.get('orders') else 1
            res = ipat.buy(cr, out['venues'], out['race_no'], [(num, 100)], dry=True, log=log)
            _report_test(f"{r['race']} {out['race_no']}R 単勝{num}番 100円", res, quiet)
            return
    venue, race_no = todays_jra(now)
    if not venue:
        print('いま発売中のレースが見つかりません(即PATの受付時間に、もう一度試してください)。')
        return
    res = ipat.buy(cr, [venue], race_no, [(1, 100)], dry=True, log=log)
    _report_test(f'{venue} {race_no}R 単勝1番 100円', res, quiet)


def _report_test(what, res, quiet):
    if res['ok']:
        msg = f'🧪 自動購入の試し運転: 成功({what}を購入予定リストに入れ、合計金額を入れるところまで。購入はしていません)'
    else:
        msg = f"🧪 自動購入の試し運転: 「{res['failed']}」の段階で止まりました({what})。この段階の名前を Claude に伝えてください。"
    print('\n' + msg)
    discord(msg, quiet)


def todays_jra(now):
    """JRAのレースから、まだ発走していない一番遅いレース(競馬場名, レース番号)。今日に無ければ明日(金曜の夜は土曜の前日発売)。
    netkeiba の公開ページから"""
    names = {'01': '札幌', '02': '函館', '03': '福島', '04': '新潟', '05': '東京', '06': '中山', '07': '中京', '08': '京都', '09': '阪神', '10': '小倉'}
    for day in (now, now + timedelta(days=1)):
        try:
            t = requests.get('https://race.netkeiba.com/top/race_list_sub.html', params={'kaisai_date': f'{day:%Y%m%d}'},
                             headers={'User-Agent': 'Mozilla/5.0 (keiba-bot; record only)'}, timeout=20).text
        except requests.RequestException:
            return None, None
        best = None
        for m in re.finditer(r'race_id=(\d{4})(\d{2})(\d{4})(\d{2}).*?(\d{1,2}):(\d{2})', t, re.S):
            if m.group(2) not in names:
                continue
            at = day.replace(hour=int(m.group(5)), minute=int(m.group(6)), second=0, microsecond=0)
            if at > now + timedelta(minutes=5) and (best is None or at > best[0]):
                best = (at, names[m.group(2)], int(m.group(4)))
        if best:
            return best[1], best[2]
    return None, None


# ---------------------------------------------------------------- 設定・状態
def status(now):
    cfg = config()
    print(f"モード: {'本番(実際に買う)' if cfg['live'] else '試し運転だけ(買わない)'}")
    print(f"1点 {cfg['stake_per_pick']:,}円 / 1レースの上限 {cfg['max_per_race']:,}円 / 1日の上限 {cfg['max_per_day']:,}円 / 発走{cfg['start_min']}分前に動く")
    print(f"止める: {'止めてある' if (HOME / 'STOP').exists() else 'なし'} / ログイン情報: {'入っている' if C.CRED.exists() else '入っていない'} / 今日の購入 {spent_today(now):,}円")
    t = tasks()
    print('予約:', '、'.join(f'{n[len(TASK):]}({s[:16]})' for n, s in sorted(t.items())) or 'なし')
    f = HOME / 'bets.csv'
    if f.exists():
        rows = list(csv.DictReader(open(f, encoding='utf-8')))[-6:]
        print('最近の記録:')
        for x in rows:
            print(f"  {x['at']} {x['race'][:16]} {x['race_no']}R 単勝{x['num']}番 {int(x['amount']):,}円 {x['mode']} 購入={x['bought']} {x['failed']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['setup', 'plan', 'bet', 'test', 'live', 'stop', 'resume', 'status'])
    ap.add_argument('arg', nargs='?')
    ap.add_argument('--key')
    ap.add_argument('--now', help='試験用(YYYY-MM-DD HH:MM)。--no-browser と一緒にだけ')
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--quiet', action='store_true', help='Discordに送らない')
    a = ap.parse_args()
    now = datetime.strptime(a.now, '%Y-%m-%d %H:%M').replace(tzinfo=JST) if a.now else datetime.now(JST)
    if a.now and not a.no_browser:
        sys.exit('--now は --no-browser と一緒にだけ使えます')
    if a.cmd == 'setup':
        if C.setup():
            register(TASK + 'plan', datetime.now() + timedelta(minutes=1), 'plan', every_min=30)
            print('30分ごとに発売レースを確かめて、発走7分前に自動購入を動かす予約を入れました。')
            print('いまは「試し運転だけ」です。「2_試し運転.bat」を即PATの受付時間に開いて、うまく進むか確かめてください。')
    elif a.cmd == 'plan':
        plan(now)
    elif a.cmd == 'bet':
        bet(a.key, now, no_browser=a.no_browser, ignore_time=bool(a.now) and a.no_browser, quiet=a.quiet)
    elif a.cmd == 'test':
        test(now, quiet=a.quiet)
    elif a.cmd == 'live':
        cfg = config()
        if a.arg == 'on':
            print('本番にすると、発売レースの発走7分前に、即PATで実際に馬券を買います。')
            print(f"(1点 {cfg['stake_per_pick']:,}円・1レース {cfg['max_per_race']:,}円まで・1日 {cfg['max_per_day']:,}円まで。{BELOW})")
            if input('本番にするなら「はい」と入れてください: ').strip() == 'はい':
                cfg['live'] = True
                save_config(cfg)
                print('本番にしました。止めたい時は「止める.bat」。')
            else:
                print('本番にはしませんでした。')
        else:
            cfg['live'] = False
            save_config(cfg)
            print('試し運転だけに戻しました(買いません)。')
    elif a.cmd == 'stop':
        HOME.mkdir(parents=True, exist_ok=True)
        (HOME / 'STOP').write_text('stop', encoding='utf-8')
        print('自動購入を止めました(「再開する.bat」で戻せます)。')
    elif a.cmd == 'resume':
        (HOME / 'STOP').unlink(missing_ok=True)
        print('自動購入を再開しました。')
    elif a.cmd == 'status':
        status(now)


if __name__ == '__main__':
    main()
