#!/usr/bin/env python3
"""Checks that nothing private lands in this public repo (CI runs it on every build, before the tests).

  python tools/repo_check.py            checks the files git tracks (git ls-files); exit 1 with the list when not clean

Refused (rules 4 and 5 in CLAUDE.md):
- game files and anything taken from them or from recordings: weapon / legend icon tables (`.npz`), pak and sound bank
  files, textures, video and audio, pickles, archives, programs; scan results (`*.scan.json`, `*.feed.json`);
- sign-ins and keys: `client.json`, `client_secret*.json`, token files, the hub key file, private keys (PEM), and text that
  looks like a real secret (Google API key / OAuth token / client secret, GitHub token, a full hub key or invite code);
- any other binary file, and any file over 1 MB, unless listed in ALLOWED_BINARY (our own icons and derived numbers).
Only the file's path and the kind of finding are printed, never the matched text.
"""
import os
import re
import subprocess
import sys

ALLOWED_BINARY = {            # our own pictures and derived numbers: a new one is added here on purpose, in a reviewed PR
    'app/assets/pcs.ico', 'app/assets/pcs.png', 'app/web/icon.png', 'scanner/rank_rp.npy',
}
MAX_TEXT = 1024 * 1024        # the biggest text file today is ~350 KB (tests/test_recorder.py)
FORBIDDEN_EXT = {
    '.npz': 'icon table (game images)', '.rpak': 'game pak', '.starpak': 'game pak', '.mbnk': 'sound bank',
    '.mstr': 'sound bank', '.bnk': 'sound bank', '.wem': 'game sound', '.dds': 'texture', '.ktx': 'texture',
    '.vpk': 'game pak', '.rson': 'game file', '.mp4': 'video', '.mkv': 'video', '.m4a': 'audio', '.wav': 'audio',
    '.ogg': 'audio', '.opus': 'audio', '.mp3': 'audio', '.flac': 'audio', '.pkl': 'pickle', '.pickle': 'pickle',
    '.zip': 'archive', '.7z': 'archive', '.rar': 'archive', '.exe': 'program', '.dll': 'program', '.pak': 'game pak',
    '.webp': 'picture', '.pem': 'key file', '.p12': 'key file', '.pfx': 'key file', '.key': 'key file',
}
FORBIDDEN_NAMES = [
    (re.compile(r'(^|/)client\.json$'), 'Google sign-in client file'),
    (re.compile(r'(^|/)client_secret[^/]*\.json$'), 'Google sign-in client file'),
    (re.compile(r'(^|/)(google|youtube)_token\.json$'), 'sign-in token file'),
    (re.compile(r'(^|/)hub_key\.dat$'), 'hub key file'),
    (re.compile(r'\.(scan|feed|comms)\.json$'), 'scan result (game data)'),
]
SECRETS = [
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'), 'private key'),
    (re.compile(r'AIza[0-9A-Za-z_-]{35}'), 'Google API key'),
    (re.compile(r'ya29\.[0-9A-Za-z_-]{40,}'), 'Google OAuth token'),
    (re.compile(r'1//0[0-9A-Za-z_-]{40,}'), 'Google refresh token'),
    (re.compile(r'GOCSPX-[0-9A-Za-z_-]{24,}'), 'Google client secret'),
    (re.compile(r'gh[pousr]_[0-9A-Za-z]{36}|github_pat_[0-9A-Za-z_]{50,}'), 'GitHub token'),
    (re.compile(r'(?<![A-Za-z0-9_])pcsi?_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])'), 'hub key or invite code'),
]
BINARY_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.npy', '.bin', '.dat', '.ttf', '.otf', '.woff', '.woff2',
              '.pdf', '.db', '.sqlite'}


def tracked(root):
    out = subprocess.run(['git', 'ls-files', '-z'], cwd=root, capture_output=True, check=True).stdout
    return [p for p in out.decode('utf-8').split('\0') if p]


def is_binary(data):
    return b'\0' in data[:8192]


def check(root, paths):
    """-> [(path, why)] for every file that must not be in the repo."""
    bad = []
    for rel in sorted(paths):
        rel = rel.replace('\\', '/')
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            continue
        ext = os.path.splitext(rel)[1].lower()
        why = FORBIDDEN_EXT.get(ext) or next((w for rx, w in FORBIDDEN_NAMES if rx.search(rel)), None)
        if why:
            bad.append((rel, why))
            continue
        with open(full, 'rb') as f:
            data = f.read()
        if rel in ALLOWED_BINARY:
            continue
        if ext in BINARY_EXT or is_binary(data):
            bad.append((rel, 'binary file not in ALLOWED_BINARY'))
            continue
        if len(data) > MAX_TEXT:
            bad.append((rel, f'over {MAX_TEXT // 1024} KB'))
            continue
        text = data.decode('utf-8', errors='replace')
        for rx, w in SECRETS:
            if rx.search(text):
                bad.append((rel, w))
                break
    return bad


def main(argv=None):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad = check(root, tracked(root))
    for rel, why in bad:
        print(f'NOT ALLOWED: {rel}: {why}')
    print('repo check: ' + ('clean' if not bad else f'{len(bad)} file(s) must not be in this public repo'))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
