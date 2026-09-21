"""
G1詳細予想をDiscordへ配信。predict.py(血統ON)を回し、
印・期待値・展開の要点を抜き出して投稿する。
使い方: python src/g1_post.py --race-id 202606040911 [--dry-run]
webhookは環境変数 DISCORD_WEBHOOK_URL、無ければ data/discord_webhook.txt。
"""
import argparse, subprocess, sys, os, re, json
from pathlib import Path
try:
    import requests
except Exception:
    requests = None

DATA = Path(__file__).parent.parent / 'data'

def get_webhook():
    wh = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
    if wh:
        return wh
    f = DATA / 'discord_webhook.txt'
    if f.exists():
        return f.read_text(encoding='utf-8-sig').strip()
    return ''

def run_predict(race_id):
    # 血統ON(= --no-pedigree を付けない)で詳細予想
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    p = subprocess.run([sys.executable, str(Path(__file__).parent/'predict.py'), race_id],
                       capture_output=True, encoding='utf-8', errors='replace', env=env, timeout=300)
    return (p.stdout or '') + '\n' + (p.stderr or '')

def extract(text):
    """predict出力から ヘッダ / 予想印 / 期待値 / 展開 を抜く。"""
    lines = text.splitlines()
    def block(start_pat, stop_pats):
        out=[]; on=False
        for l in lines:
            if not on and re.search(start_pat, l):
                on=True
            if on:
                if out and any(re.search(sp,l) for sp in stop_pats):
                    break
                out.append(l)
        return [x for x in out if x.strip() and not re.match(r'^=+$', x.strip())]
    # レース名(【...】＋条件行)
    header=[]
    for i,l in enumerate(lines):
        if re.match(r'^【.+】$', l.strip()) and i+1<len(lines):
            header=[l.strip(), lines[i+1].strip()]; break
    marks = block(r'予想印', [r'期待値', r'各馬詳細', r'展開予測'])
    ev    = block(r'期待値', [r'各馬詳細', r'展開予測', r'※ LGBMRanker'])
    ten   = block(r'展開予測', [r'^=+', r'※'])
    return header, marks, ev, ten

def build_message(race_id, header, marks, ev, ten):
    L=[]
    L.append('🏆 **G1予想** 🏆')
    if header:
        L.append('**'+header[0].strip('【】')+'**  '+(header[1] if len(header)>1 else ''))
    if marks:
        L.append('')
        L += [m for m in marks if '★★★' not in m]
    if ev:
        L.append('')
        L += ev
    if ten:
        L.append('')
        L += [t for t in ten if '脚質分布' in t or 'ペース' in t or '→' in t]
    L.append('')
    L.append('※モデル予想＋市場ブレンド。的中は保証できませんが情報を整理して出しています。')
    return '\n'.join(L)

def post(webhook, msg):
    # 2000字制限で分割
    chunks=[]; cur=''
    for line in msg.split('\n'):
        if len(cur)+len(line)+1 > 1800:
            chunks.append(cur); cur=''
        cur += line+'\n'
    if cur.strip(): chunks.append(cur)
    for c in chunks:
        payload = json.dumps({'content': c}, ensure_ascii=False).encode('utf-8')
        if requests:
            requests.post(webhook, data=payload, headers={'Content-Type':'application/json'}, timeout=20)
        else:
            import urllib.request
            req=urllib.request.Request(webhook, data=payload, headers={'Content-Type':'application/json'})
            urllib.request.urlopen(req, timeout=20)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--race-id',required=True); ap.add_argument('--dry-run',action='store_true')
    a=ap.parse_args()
    out=run_predict(a.race_id)
    header,marks,ev,ten=extract(out)
    msg=build_message(a.race_id, header, marks, ev, ten)
    if a.dry_run:
        print('----- DRY RUN (投稿しない) -----'); print(msg); return
    wh=get_webhook()
    if not wh:
        print('webhook無し'); return
    post(wh, msg); print('Discord投稿完了')

if __name__=='__main__':
    sys.path.insert(0,'src'); main()
