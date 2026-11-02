"""
出す前の点検(2026-11-02)。本番と同じ Actions の環境で、各係を「送らない試し走り」で動かし、壊れていたら Discord に知らせる。
ユーザーの指摘「検証をしてから納品してくれ」を受けて作った。
過去の事故(10/25 pandas 3.1 で予想が全停止 / 10/25 三連複の書き間違い / 11/1 BCの出走馬0頭)を、本番の前に見つけるためのもの。
  - 全ファイルの文法(py_compile)
  - predict.py: 過去の決まったレース(血統なし)で ◎ が出るか
  - domestic_picks.py: 過去の決まったレースで「これを買う」か「買わない」が出るか
  - g1_research / sanrenpuku: 決まった入力で計算が通るか(ネット不要)
  - overseas.py: 海外発売の一覧・日程が読めるか(daily --dry。通知はしない)
問題がなければ何も送らない。使い方: python tools/preflight.py [--no-discord]
"""
import py_compile
import subprocess
import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / 'src'
sys.path.insert(0, str(SRC))
PAST_RACE = '202608040911'  # 2026 菊花賞(確定オッズで試す)
results = []


def check(name, fn):
    try:
        msg = fn()
        results.append((name, True, msg or ''))
    except Exception as e:  # 点検なので、何が起きても続ける
        results.append((name, False, f'{type(e).__name__}: {e}'))
        traceback.print_exc()


def run(args, need, timeout=600, bad=()):
    p = subprocess.run([sys.executable] + args, cwd=BASE, capture_output=True, text=True, timeout=timeout,
                       encoding='utf-8', errors='replace', env={**__import__('os').environ, 'PYTHONIOENCODING': 'utf-8'})
    out = p.stdout + p.stderr
    if 'Traceback' in out:
        raise RuntimeError(out[out.find('Traceback'):][-600:])
    hit = [x for x in bad if x in out]
    if hit:  # 例外で止まらずに印字だけする失敗(海外の取得失敗など)も拾う
        i = out.find(hit[0])
        raise RuntimeError(out[max(0, i - 200):i + 300])
    if not any(n in out for n in need):
        raise RuntimeError(f"出力に {' / '.join(need)} が無い: {out[-400:]}")
    return ''


def syntax():
    bad = []
    for f in sorted(SRC.glob('*.py')) + sorted((BASE / 'tools').glob('*.py')):
        try:
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError as e:
            bad.append(f'{f.name}: {e.msg[:120]}')
    if bad:
        raise RuntimeError(' / '.join(bad))
    return f'{len(list(SRC.glob("*.py")))}ファイル'


def calc():
    import g1_research
    import sanrenpuku
    win = {1: 1.8, 2: 4.0, 3: 8.0, 4: 15.0, 5: 30.0, 6: 60.0}
    trio = {''.join(f'{x:02d}' for x in k): 20.0 for k in [(1, 2, 3), (1, 2, 4), (1, 3, 4), (1, 2, 5), (1, 3, 5)]}
    r = sanrenpuku.pick(win, trio, axis=None, budget=1000)
    assert r.get('points') and r['synth'] >= 5, r
    horses = [{'num': n, 'name': f'馬{n}', 'jockey': 'x', 'odds': o, 'p_mkt': 1 / o, 'p_model': 1 / o, 'runs': []} for n, o in win.items()]
    rows, _ = g1_research.build({'horses': horses, 'distance': '2000', 'surface': '芝'})
    assert rows[0]['mark'] == '◎'
    return ''


check('文法', syntax)
check('計算(三連複・G1の印)', calc)
check('予想(predict.py)', lambda: run(['src/predict.py', PAST_RACE, '--no-pedigree'], ['◎']))
check('国内の言い切り配信', lambda: run(['src/domestic_picks.py', '--race-id', PAST_RACE, '--dry'], ['これを買う', '今回は買わない']))
check('海外の見張り', lambda: run(['src/overseas.py', 'daily', '--dry'], [''], bad=('取得失敗', 'PMU失敗', 'JRA公式オッズ失敗', '読めず')))

ng = [r for r in results if not r[1]]
for name, ok, msg in results:
    print(('OK ' if ok else 'NG ') + name + (f' … {msg}' if msg else ''))
if ng and '--no-discord' not in sys.argv:
    import g1_post
    wh = g1_post.get_webhook()
    if wh:
        g1_post.post(wh, '🛠 **競馬BOTの点検で不具合が見つかりました**(本番の前に直します)\n' +
                     '\n'.join(f'・{n}: {m[:300]}' for n, _, m in ng))
sys.exit(1 if ng else 0)
