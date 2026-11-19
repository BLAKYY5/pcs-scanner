"""The app's string tables (app/web/i18n): which keys the web UI uses, which are missing from a table.

    python tools/i18n_keys.py              list keys used in the JS but missing from en.json / the other tables
    python tools/i18n_keys.py --update     add the missing keys to en.json (English text = key), sorted
    python tools/i18n_keys.py --list       every key the JS uses

Keys are the English text with {name} for values. Python-side sentences (Activity lines, corner messages, reasons)
are in en.json too: they are matched at display time (pcs_i18n.tx / tx() in i18n.js), so they never show up as
"used in the JS"; keep them. A new language = a new <code>.json with "_language" and the keys it translates.
"""
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'app' / 'web'
I18N = WEB / 'i18n'
JS = ['i18n.js', 'app.js', 'session.js', 'review-extras.js', 'minimap.js', 'clip.js']
_ESC = {'n': '\n', 't': '\t', '\\': '\\', "'": "'", '"': '"', '`': '`', '0': '\0'}


def unescape(raw: str) -> str:
    return re.sub(r'\\u([0-9a-fA-F]{4})|\\(.)', lambda m: chr(int(m.group(1), 16)) if m.group(1)
                  else _ESC.get(m.group(2), m.group(2)), raw)


def js_keys(files=None) -> dict:
    """{key: [file, ...]} of every t('...') / t("...") / data-i18n="..." in the web UI."""
    out: dict = {}
    for name in files or JS:
        src = (WEB / name).read_text(encoding='utf-8')
        for m in re.finditer(r"""\bt\(\s*(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")""", src):
            out.setdefault(unescape(m.group(1) if m.group(1) is not None else m.group(2)), []).append(name)
    html = (WEB / 'index.html').read_text(encoding='utf-8')
    for m in re.finditer(r'data-i18n(?:-title|-aria)?="([^"]*)"', html):
        out.setdefault(m.group(1).replace('&amp;', '&'), []).append('index.html')
    return out


def table(code: str) -> dict:
    return json.loads((I18N / f'{code}.json').read_text(encoding='utf-8'))


def save(code: str, d: dict) -> None:
    meta = {k: v for k, v in d.items() if k.startswith('_')}
    rest = dict(sorted(((k, v) for k, v in d.items() if not k.startswith('_')), key=lambda kv: kv[0].lower()))
    (I18N / f'{code}.json').write_text(json.dumps({**meta, **rest}, ensure_ascii=False, indent=1) + '\n',
                                       encoding='utf-8', newline='\n')


def main(argv):
    used = js_keys()
    if '--list' in argv:
        for k in sorted(used, key=str.lower):
            print(k)
        return 0
    en = table('en')
    missing = [k for k in used if k not in en]
    if '--update' in argv:
        for k in missing:
            en[k] = k
        save('en', en)
        print(f'en.json: {len(missing)} keys added, {len([k for k in en if not k.startswith("_")])} in all')
        return 0
    print(f'{len(used)} keys used in the web UI; {len(missing)} missing from en.json')
    for k in missing:
        print('  missing:', k)
    for f in sorted(I18N.glob('*.json')):
        if f.stem == 'en':
            continue
        tr = table(f.stem)
        extra = [k for k in tr if not k.startswith('_') and k not in en]
        gap = [k for k in en if not k.startswith('_') and k not in tr]
        print(f'{f.name}: {len(gap)} keys fall back to English, {len(extra)} keys not in en.json')
        for k in extra:
            print('  not in en.json:', k)
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
