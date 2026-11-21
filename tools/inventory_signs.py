#!/usr/bin/env python3
"""Build scanner/pcs_inventory.json (signs for pcs_inventory) from the game's own UI images.

A developer tool for the coach's PC, not part of the app (not bundled in the exe).

The images come from a private RSX export of ui.rpak, named by their UI paths (`ui_named/rui/...`); they are never in
this repo. Only numbers derived from them go into the table:
- weapons: the white shape of each `weapon_icons/r5/weapon_*` image (alpha and light pixels), cut to its own box and
  averaged into 40 x 14 cells (4 bits), plus the box's width / height;
- backpack items (heals, grenades, ammo): the image scaled and placed where the inventory screen draws it in a slot
  (height and centre below, fitted once on our own recordings of 17 Nov 2026: the median slot picture of each item
  against the image at every size and place, least colour difference), then cut into the slot's 16 x 14 colour cells
  (4 bits per channel) and how much of each cell the image covers (4 bits).

The frag grenade's image holds only the white of the icon (the game adds the orange): its colours come from our
own recordings instead (`--recorded`: a private list of slots showing it; the median of their colour cells), its
place and coverage still from the image.

  python tools/inventory_signs.py <ui_named/rui folder> [--recorded <slots.json>] [--write]

slots.json: {"frag": [["<full-size frame .png>", row, col], ...]} (frames from our recordings, never in the repo).
"""
import os, sys, json, argparse, glob
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = os.path.join(HERE, '..', 'scanner', 'pcs_inventory.json')
WSW, WSH = 40, 14
CN_X, CN_Y, CELL = 16, 14, 7          # slot picture 112 x 98 (above the stack bar) in 7 x 7 cells

AMMO = (84, 60, 54)                   # every ammo icon: same canvas, same place
# sign: (image under ui_named/rui, height in px at 1440p, centre x, centre y in the slot)
ITEMS = {
    'medkit': ('hud/loot/loot_stim_health_large', 66, 60, 52),
    'syringe': ('hud/loot/loot_stim_health_small', 94, 59, 54),
    'battery': ('hud/loot/loot_stim_shield_large', 86, 59, 56),
    'cell': ('hud/loot/loot_stim_shield_small', 66, 60, 53),
    # phoenix kit: never on the dev frames; its image is the battery's size (72 x 139), drawn at the battery's scale
    'phoenix': ('hud/loot/loot_stim_combo_full', 86, 59, 56),
    'ultaccel': ('hud/loot/loot_health_ultimate', 64, 60, 56),        # ultimate accelerant
    'frag': ('ordnance_icons/grenade_frag', 64, 59, 56),
    'thermite': ('ordnance_icons/grenade_incendiary', 84, 59, 55),
    'arcstar': ('ordnance_icons/grenade_arc', 86, 60, 55),
    'light': ('hud/gametype_icons/survival/sur_ammo_bullet',) + AMMO,
    'heavy': ('hud/gametype_icons/survival/sur_ammo_bullet_large',) + AMMO,
    'energy': ('hud/gametype_icons/survival/sur_ammo_special',) + AMMO,
    'shotgun': ('hud/gametype_icons/survival/sur_ammo_shotgun',) + AMMO,
    'sniper': ('hud/gametype_icons/survival/sur_ammo_sniper',) + AMMO,
    'arrows': ('hud/gametype_icons/survival/sur_ammo_arrows',) + AMMO,
}


def hexs(v, scale):
    return ''.join('%x' % int(x) for x in np.clip(np.round(np.asarray(v, np.float64).ravel() * scale), 0, 15))


def weapon_sign(path):
    a = np.asarray(Image.open(path).convert('RGBA'))
    m = (a[..., 3] > 128) & (a[..., :3].min(2) > 128)
    ys, xs = np.nonzero(m)
    c = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.float32)
    h, w = c.shape
    yi = np.arange(h) * WSH // h; xi = np.arange(w) * WSW // w
    out = np.zeros((WSH, WSW)); cnt = np.zeros((WSH, WSW))
    np.add.at(out, (yi[:, None], xi[None, :]), c); np.add.at(cnt, (yi[:, None], xi[None, :]), 1)
    return [round(w / h, 3), hexs(out / np.maximum(cnt, 1), 15)]


def item_sign(path, h, cx, cy):
    im = Image.open(path).convert('RGBA'); W0, H0 = im.size
    w = round(W0 * h / H0)
    R = np.asarray(im.resize((w, h), Image.LANCZOS)).astype(np.float32)
    can = np.zeros((CN_Y * CELL, CN_X * CELL, 4), np.float32)
    ys, xs = cy - h // 2, cx - w // 2
    y0, x0 = max(ys, 0), max(xs, 0); y1, x1 = min(ys + h, can.shape[0]), min(xs + w, can.shape[1])
    can[y0:y1, x0:x1] = R[y0 - ys:y1 - ys, x0 - xs:x1 - xs]
    al = can[..., 3:] / 255
    rgb = (can[..., :3] * al).reshape(CN_Y, CELL, CN_X, CELL, 3).sum((1, 3))
    cov = al.reshape(CN_Y, CELL, CN_X, CELL).sum((1, 3))
    col = rgb / np.maximum(cov[..., None], 1e-6)
    return [hexs(col, 1 / 17.0), hexs(cov / (CELL * CELL), 15)]


def recorded_colours(slots):
    """Median colour cells of slots seen on our recordings -> 16 x 14 x 3."""
    C = []
    for f, r, k in slots:
        a = np.asarray(Image.open(f).convert('RGB')).astype(np.float32)
        x0, y0 = 1032 + 130 * k, 803 + 131 * r
        C.append(a[y0:y0 + CN_Y * CELL, x0:x0 + CN_X * CELL].reshape(CN_Y, CELL, CN_X, CELL, 3).mean((1, 3)))
    return np.median(np.array(C), 0)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('rui'); ap.add_argument('--write', action='store_true')
    ap.add_argument('--recorded')
    a = ap.parse_args()
    weapons = {}
    for f in sorted(glob.glob(os.path.join(a.rui, 'weapon_icons', 'r5', 'weapon_*.png'))):
        n = os.path.basename(f)[7:-4]
        if 'akimbo' in n and not n.endswith('_active'): continue      # two pistols: the card shows the 'active' pair
        weapons[n.replace('_active', '')] = weapon_sign(f)
    items = {k: item_sign(os.path.join(a.rui, p + '.png'), h, cx, cy) for k, (p, h, cx, cy) in ITEMS.items()}
    if a.recorded:
        for k, slots in json.load(open(a.recorded, encoding='utf-8')).items():
            items[k][0] = hexs(recorded_colours(slots), 1 / 17.0)
    t = {'about': 'Derived from the game UI images by tools/inventory_signs.py (numbers only, no images).',
         'weapons': weapons, 'items': items}
    print(len(weapons), 'weapons,', len(items), 'items')
    if a.write:
        with open(TABLE, 'w', encoding='utf-8', newline='\n') as fh: json.dump(t, fh, indent=0, sort_keys=True); fh.write('\n')
        print('written', TABLE)


if __name__ == '__main__':
    main()
