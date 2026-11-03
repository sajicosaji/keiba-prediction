"""
即PAT(パソコン版 https://www.ipat.jra.go.jp/)で単勝を買う係(2026-11-03)。
  画面の操作の手順と目印は、公開されている keiba-auto-bet(MIT License, KeibaAI-developer, 2026-08 更新)の
  keiba_auto_bet/auto_bet.py を元にした(Selenium → Playwright に書き換え)。
  海外のレース向けに 2 か所へ予備の手順を足した:
    - 購入画面への移動: 元は「12R」のボタンを押す。海外の日は 12R が無いことがあるので、どのレースのボタンでもよいことにした
    - 競馬場の選び方: JRA の一覧の競馬場名(例: シャティン)や国名で探す
  ログイン後の画面の中身(残高・口座など)は、記録にも Discord にも残さない(即PATの約定 第27条(4))。段階の名前と成否だけを返す。
  dry=True(試し運転)は、購入予定リストに入れて合計金額を入れたところで止め、「購入」は押さない。
  合計金額の入力は即PAT側の確認でもある(入れた合計と予定リストの合計が違うと買えない)。
"""
import re
import time

from playwright.sync_api import sync_playwright

URL = 'https://www.ipat.jra.go.jp/'
T = 15000  # 1つの段階で待つ最長(ミリ秒)


class StepError(Exception):
    def __init__(self, step, why):
        super().__init__(f'{step}: {why}')
        self.step = step


def _announce(pg):
    """ログイン直後の「お知らせ」が出ていたら OK で閉じる。出ていなければ何もしない"""
    pg.wait_for_timeout(1500)
    if pg.locator("xpath=//h1[contains(text(),'お知らせ')]").count():
        pg.locator('button.btn-ok').first.click()
        pg.wait_for_timeout(1500)


def _race_button(pg, race_no):
    """通常投票の画面で、どこかのレースのボタンを押して購入画面へ(競馬場とレースは次の段階で選び直す)"""
    btns = pg.locator('button').filter(has_text=re.compile(r'^\s*\d{1,2}R'))
    btns.first.wait_for(state='visible')
    mine = pg.locator('button').filter(has_text=re.compile(rf'^\s*{race_no}R(\D|$)'))
    (mine.first if mine.count() else btns.first).click()
    pg.locator('#bet-basic-type').wait_for(state='visible')


def _select(pg, venues, race_no, show_options):
    sel = pg.locator('#select-course-race-course')
    sel.wait_for(state='visible')
    opts = [o.strip() for o in sel.locator('option').all_inner_texts()]
    pick = next((o for v in venues if v for o in opts if v in o), None)
    if not pick:
        show_options('競馬場の選択肢', opts)  # 画面(本人のコンソール)にだけ出す
        raise LookupError('競馬場が選択肢に無い')
    sel.select_option(label=pick)
    race = pg.locator('#select-course-race-race')
    end = time.time() + T / 1000
    while time.time() < end:
        texts = [o.strip() for o in race.locator('option').all_inner_texts()]
        hit = [t for t in texts if re.match(rf'^{race_no}R(\D|$)', t)]
        if hit:
            race.select_option(label=hit[0])
            break
        pg.wait_for_timeout(300)
    else:
        show_options('レースの選択肢', texts)
        raise LookupError('レースが選択肢に無い')
    pg.locator('#bet-basic-type').wait_for(state='visible')
    pg.wait_for_timeout(1000)  # 画面の作り直し(AngularJS)が落ち着くのを待つ


def _win(pg, num, amount):
    for i in range(3):  # 画面の作り直しで要素が入れ替わることがあるので、券種の選択はやり直しあり
        try:
            pg.locator('#bet-basic-type').select_option(label='単勝')
            break
        except Exception:
            if i == 2:
                raise
            pg.wait_for_timeout(1000)
    lab = pg.locator(f"xpath=//label[@for='no{num}']")
    lab.wait_for()
    lab.locator('.check').first.evaluate('e => e.click()')
    unit = pg.locator("xpath=//input[@maxlength='4' and @ng-model='vm.nUnit']")
    unit.fill(str(amount // 100))
    pg.locator("button.btn.btn-lg.btn-set.btn-primary[ng-click='vm.onSet()']").click()
    pg.locator('#bet-basic-type').wait_for(state='visible')


def _ok(pg):
    ok = pg.locator("xpath=//button[contains(@class,'btn-ok') and contains(text(),'OK')]").first
    ok.wait_for(state='visible')
    ok.evaluate('e => e.click()')
    ok.wait_for(state='hidden')


def buy(cred, venues, race_no, orders, dry=True, log=print, show_options=None, headless=False):
    """orders = [(馬番, 金額)] の単勝。戻り値 {'ok', 'bought', 'steps': [(段階, 'OK'|'NG')], 'failed': 段階名 or None}"""
    show_options = show_options or (lambda title, xs: print(f'  ({title}: {" / ".join(xs)})'))
    total = sum(a for _, a in orders)
    steps = []

    def step(name, fn):
        try:
            fn()
        except Exception as e:
            steps.append((name, 'NG'))
            log(f'段階{len(steps)} {name}: NG({type(e).__name__})')
            raise StepError(name, type(e).__name__)
        steps.append((name, 'OK'))
        log(f'段階{len(steps)} {name}: OK')

    res = {'ok': False, 'bought': False, 'steps': steps, 'failed': None}
    with sync_playwright() as p:
        br = p.chromium.launch(channel='chrome', headless=headless)
        pg = br.new_page(viewport={'width': 1280, 'height': 900})
        pg.set_default_timeout(T)
        try:
            step('即PATを開く', lambda: pg.goto(URL, wait_until='domcontentloaded', timeout=30000))
            step('ログイン画面(受付時間内か)', lambda: pg.locator("input[name='inetid']").wait_for(state='visible'))

            def login1():
                pg.fill("input[name='inetid']", cred['inet_id'])
                pg.locator("xpath=//a[@title='ログイン']").first.click()
            step('INET-IDを入れる', login1)

            def login2():
                pg.locator("input[name='i']").wait_for(state='visible')
                pg.fill("input[name='i']", cred['user_number'])
                pg.fill("input[name='p']", cred['password'])
                pg.fill("input[name='r']", cred['p_ars'])
                pg.locator("xpath=//a[@title='ネット投票メニューへ']").first.click()
                pg.wait_for_load_state('domcontentloaded')
            step('加入者番号・暗証番号・P-ARS番号を入れる', login2)
            step('お知らせを閉じる', lambda: _announce(pg))
            step('通常投票を開く', lambda: pg.locator("xpath=//button[@title='出馬表から馬を選択する方式です。']").first.click())
            step('購入画面へ', lambda: _race_button(pg, race_no))
            step(f'競馬場とレース({race_no}R)を選ぶ', lambda: _select(pg, venues, race_no, show_options))
            for n, a in orders:
                step(f'単勝 {n}番 {a:,}円を入れる', lambda n=n, a=a: _win(pg, n, a))
            step('購入予定リストを開く', lambda: pg.locator("xpath=//button[contains(@class,'btn-vote-list')]").first.click())
            step(f'合計金額 {total:,}円を入れる', lambda: pg.fill("xpath=//input[@ng-model='vm.cAmountTotal']", str(total)))
            if dry:
                log('試し運転なので「購入」は押さずに止めます')
                pg.wait_for_timeout(3000)
                res['ok'] = True
                return res
            step('購入を押す', lambda: pg.locator("xpath=//button[contains(text(),'購入')]").first.click())
            step('確認のOKを押す', lambda: _ok(pg))
            res['ok'] = res['bought'] = True
            return res
        except StepError as e:
            res['failed'] = e.step
            # 「購入を押す」より後で失敗した時は、買えたかどうか分からない
            res['bought'] = None if e.step == '確認のOKを押す' else False
            return res
        finally:
            br.close()
