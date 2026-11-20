#!/usr/bin/env python3
"""Build the mode emblem shapes of scanner/pcs_banner.json ('emblems') from the game's own emblem images.

A developer tool for the coach's PC, not part of the app (not bundled in the exe).

The emblem at the top right of the HUD (and of the "waiting for players" screen) tells the playlist: the red
"unranked" emblem in pubs, the teal emblem in Wildcard, a rank badge in ranked. The images come from an RSX export
of ui.rpak (private, never in this repo). Only numbers derived from them go into the table: per emblem 16 x 16
colour cells (4 bits per channel) and the cells the emblem fully covers. That is what pcs_hud.ModeEmblem compares
with the screen.

  python tools/mode_emblems.py <ui_image folder> [--write]

The keys below are the images' asset GUIDs (names as RSX exports them). 'mode' is the scanner's mode value for the
emblem, or null for an emblem of a playlist we have not seen on screen yet (it is still read, so a game in that
playlist is not taken for pubs or Wildcard).
"""
import os, sys, json, argparse
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = os.path.join(HERE, '..', 'scanner', 'pcs_banner.json')
N = 16

# Seen on our own recordings (S31): 0x3917D11A174C728A on every pubs trios game (15 Nov 2026), 0x89ACA90DA52C6EF4
# on every Wildcard game (13 Nov 2026). The others are the same kind of round playlist emblem (ui.rpak, about 580 px,
# found next to them): their playlist is not known yet. Emblems that are mostly see-through (fewer than 100 of
# the 256 cells fully covered) are left out: their look on screen depends on what is behind them.
EMBLEMS = {
    '0x3917D11A174C728A': 'pubs',
    '0x89ACA90DA52C6EF4': 'wildcard',
    '0xA8E12252': None, '0xB51C4D2E': None, '0xB93D022C': None, '0xC07B1396': None, '0xCBE63EB0': None,
    '0xD293A122': None, '0xDB14EC62': None,
}


def derive(path):
    """Emblem image (RGBA) -> (768 hex digits: 16 x 16 cells x RGB at 4 bits, 64 hex digits: fully covered cells)."""
    im = Image.open(path).convert('RGBA'); a = np.asarray(im)
    ys, xs = np.where(a[..., 3] > 40)
    im = im.crop((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))
    a = np.asarray(im.resize((N * 8, N * 8), Image.BOX)).astype(np.float32) / 255
    al = a[..., 3:]
    rgb = (a[..., :3] * al).reshape(N, 8, N, 8, 3).mean((1, 3))
    cov = al.reshape(N, 8, N, 8).mean((1, 3))
    col = np.clip(rgb / np.maximum(cov[..., None], 1e-6), 0, 1)
    q = np.round(col * 15).astype(int)
    mask = (cov > 0.97).astype(int).ravel()
    return (''.join('%x' % v for v in q.ravel()),
            ''.join('%x' % int(''.join(map(str, mask[i:i + 4])), 2) for i in range(0, N * N, 4)))


def find(folder, key):
    for f in os.listdir(folder):
        if f.upper().startswith(key.upper()) and f.lower().endswith('.png'): return os.path.join(folder, f)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('folder', help='RSX export folder of ui.rpak images (ui_image)')
    ap.add_argument('--write', action='store_true', help='put the shapes into scanner/pcs_banner.json')
    A = ap.parse_args()
    out = {}
    for key, mode in EMBLEMS.items():
        p = find(A.folder, key)
        if not p: print('missing', key); continue
        rgb, mask = derive(p)
        out[os.path.splitext(os.path.basename(p))[0]] = {'mode': mode, 'rgb': rgb, 'mask': mask}
        print(f'{key:20s} {mode or "-":9s} {bin(int(mask, 16)).count("1")} full cells')
    if not A.write: return
    s = open(TABLE, 'rb').read().decode('utf-8')
    d = json.loads(s)
    if 'emblems' in d:      # replace the old entry: it is the last key of the file
        s = s[:s.rindex(',\r\n"emblems":')] + '\r\n}\r\n'
    body = json.dumps(out, indent=0).replace('\n', '\r\n')
    s = s[:s.rstrip().rindex('}')].rstrip() + ',\r\n"emblems": ' + body + '\r\n}\r\n'
    json.loads(s)
    open(TABLE, 'wb').write(s.encode('utf-8'))
    print('written', TABLE)


if __name__ == '__main__':
    main()
