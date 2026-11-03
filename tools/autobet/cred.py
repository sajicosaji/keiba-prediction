"""
即PATのログイン情報を、このPCの中だけに暗号化して置く係(2026-11-03)。
  - Windows の DPAPI(CryptProtectData)で暗号化する。同じ Windows のユーザーでしか元に戻せない(ファイルを持ち出しても読めない)
  - 置き場所: %LOCALAPPDATA%\\keiba_autobet\\cred.bin(OneDrive やGitの外)
  - 即PATの約定 第29条「加入者番号・暗証番号・P-ARS番号・INET-IDを第三者に漏らさない」を守るため、
    画面にも記録にもDiscordにも出さない。入れるのは本人だけ(ログイン情報を入れる.bat)
"""
import ctypes
import ctypes.wintypes as w
import json
import os
import re
from pathlib import Path

HOME = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'keiba_autobet'
CRED = HOME / 'cred.bin'
FIELDS = [('inet_id', 'INET-ID(英数字8けた)', r'[A-Za-z0-9]{8}'),
          ('user_number', '加入者番号(数字8けた)', r'\d{8}'),
          ('password', '暗証番号(数字4けた)', r'\d{4}'),
          ('p_ars', 'P-ARS番号(数字4けた)', r'\d{4}')]


class _Blob(ctypes.Structure):
    _fields_ = [('cbData', w.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]


def _call(fn, data):
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):  # 0x1 = 画面を出さない
        raise OSError('DPAPI の処理に失敗しました')
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(data: bytes) -> bytes:
    return _call(ctypes.windll.crypt32.CryptProtectData, data)


def unprotect(data: bytes) -> bytes:
    return _call(ctypes.windll.crypt32.CryptUnprotectData, data)


def valid(c):
    return all(re.fullmatch(pat, str(c.get(k, ''))) for k, _, pat in FIELDS)


def save(c):
    if not valid(c):
        raise ValueError('ログイン情報の形が違います')
    HOME.mkdir(parents=True, exist_ok=True)
    CRED.write_bytes(protect(json.dumps(c).encode('utf-8')))


def load():
    """保存したログイン情報。無ければ None"""
    if not CRED.exists():
        return None
    c = json.loads(unprotect(CRED.read_bytes()).decode('utf-8'))
    return c if valid(c) else None


def setup():
    """本人が入力する。入れた文字は画面に出さない"""
    import getpass
    print('即PATのログイン情報を、このPCの中だけに暗号化して保存します。')
    print('入力した文字は画面に出ません(打ち間違えたら、もう一度このファイルを開いてやり直してください)。\n')
    c = {}
    for k, label, pat in FIELDS:
        while True:
            v = getpass.getpass(f'{label}: ').strip()
            if re.fullmatch(pat, v):
                c[k] = v
                break
            print('  形が違います。もう一度入れてください')
    save(c)
    ok = load() == c
    print('\n保存しました(このPCのあなたのWindowsアカウントでしか読めません)。' if ok else '\n保存に失敗しました。')
    return ok


if __name__ == '__main__':
    setup()
