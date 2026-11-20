"""The app's languages: one string table per language in web/i18n/<code>.json (en.json is the source).

Keys are the English text itself, with {name} for the parts that change ("Scanning {name}…"). A language file
only needs the keys it translates: anything missing falls back to English. Adding a language = adding one file
(its "_language" entry is the name shown in Settings). Game terms stay in English in every language: see
web/i18n/glossary.md.

Python code keeps writing English (app.log stays English for debugging). Text reaches the player through `tx()`,
which finds the English sentence in the table, also when it carries values ("Scan of a.mp4 done" matches
"Scan of {name} done"), and gives the translation with the same values.
"""
import json, os, re, sys

HERE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(HERE, 'web', 'i18n')
DEFAULT = 'en'

_tables: dict = {}
_patterns: list | None = None
_lang = None            # set by set_language (the app's settings); None = not set yet


def _load(code: str) -> dict:
    if code not in _tables:
        try:
            with open(os.path.join(DIR, f'{code}.json'), encoding='utf-8') as f:
                d = json.load(f)
            _tables[code] = {k: v for k, v in d.items() if not k.startswith('_') and isinstance(v, str)}
            _tables[code + ':meta'] = {k: v for k, v in d.items() if k.startswith('_')}
        except (OSError, ValueError):
            _tables[code], _tables[code + ':meta'] = {}, {}
    return _tables[code]


def languages() -> list[dict]:
    """[{code, name}] of every table in the folder, English first."""
    out = []
    try:
        codes = sorted(f[:-5] for f in os.listdir(DIR) if re.fullmatch(r'[a-z]{2}(-[A-Z]{2})?\.json', f))
    except OSError:
        codes = [DEFAULT]
    for c in sorted(codes, key=lambda c: (c != DEFAULT, c)):
        _load(c)
        out.append({'code': c, 'name': _tables[c + ':meta'].get('_language', c)})
    return out


def windows_language() -> str:
    """The Windows display language when we have a table for it, else English."""
    code = ''
    if os.name == 'nt':
        try:
            import ctypes, locale
            code = locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), '')
        except Exception:
            code = ''
    code = code.split('_')[0].lower()
    return code if code and any(l['code'] == code for l in languages()) else DEFAULT


def pick(settings: dict) -> str:
    """The language in the settings; none saved yet (new install or settings from an older version) = Windows'."""
    v = settings.get('language') if isinstance(settings, dict) else None
    if isinstance(v, str) and any(l['code'] == v for l in languages()):
        return v
    return windows_language()


def set_language(code: str) -> None:
    global _lang
    _lang = code if any(l['code'] == code for l in languages()) else DEFAULT


def language() -> str:
    if _lang is None:
        try:
            import pcs_core
            set_language(pick(pcs_core.SETTINGS))
        except Exception:
            set_language(DEFAULT)
    return _lang


def tables(code: str | None = None) -> dict:
    """What the web page needs: the language, the list, English and the chosen table."""
    code = code or language()
    return {'lang': code, 'languages': languages(), 'en': _load(DEFAULT), 'tr': _load(code) if code != DEFAULT else {}}


def t(key: str, **vars) -> str:
    """The text for a key in the current language (English when it has none), {name} filled in."""
    code = language()
    s = (_load(code).get(key) if code != DEFAULT else None) or _load(DEFAULT).get(key) or key
    if vars:
        s = re.sub(r'\{(\w+)\}', lambda m: str(vars[m.group(1)]) if m.group(1) in vars else m.group(0), s)
    return s


def _compiled():
    global _patterns
    if _patterns is None:
        pats = []
        for key in _load(DEFAULT):
            if '{' not in key:
                continue
            parts = re.split(r'\{(\w+)\}', key)
            if not any(p.strip() for p in parts[0::2]):
                continue                                    # nothing but values: matches anything
            rx = ''.join(re.escape(p) if i % 2 == 0 else f'(?P<{p}>.+?)' for i, p in enumerate(parts))
            try:
                pats.append((len(''.join(parts[0::2])), re.compile(rx + r'\Z', re.S), key))
            except re.error:
                pass
        pats.sort(key=lambda p: -p[0])                      # the most specific sentence first
        _patterns = pats
    return _patterns


def tx(text, depth: int = 0) -> str:
    """English text written by the app, in the player's language: a key as it is, or a key with values in it
    (the values translated too when they are table sentences). Unknown text comes back unchanged."""
    if not isinstance(text, str) or not text or language() == DEFAULT:
        return text
    tr = _load(language())
    if text in tr:
        return tr[text]
    if depth > 2:
        return text
    for _, rx, key in _compiled():
        m = rx.match(text)
        if m and key in tr:
            vals = {k: tx(v, depth + 1) for k, v in m.groupdict().items()}
            return re.sub(r'\{(\w+)\}', lambda x: vals.get(x.group(1), x.group(0)), tr[key])
    return text


def reset() -> None:
    """Forget the tables read (tests, a language file changed)."""
    global _patterns, _lang
    _tables.clear()
    _patterns = None
    _lang = None
