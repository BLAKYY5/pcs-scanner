"""What the recording player carries, read from the inventory screen (Tab) when it is open (2560 x 1440, 16:9 only).

The inventory screen (checked on the S31 Ranked and pubs recordings of 7 to 18 Nov 2026): a dark bar across the
bottom (ESC Back ... Game Menu), two weapon cards at the top ('1' and '2' in white boxes at x 901 / 1634, rows 220..252,
the card's weapon drawn white in its middle, four attachment slots under it), the backpack as two rows of up to eight
slots (x 1032 + 130 k, rows 803 / 934, 118 px; slots the backpack does not open show a grey lock), and the equipment
row under it (armour, its core, backpack, survival item). The screen is found by the bottom bar and the two number
boxes only: a few pixels, so the reader costs nothing on other keyframes.

Each backpack slot: the item by its icon (colour cells compared with signs derived from the game's own UI images,
scaled and placed where the game draws them; `tools/inventory_signs.py` builds them from a private RSX export of
ui.rpak, the images are never in this repo), and how many by the stack bar under the icon: one segment per unit the
stack can hold (heals, grenades; ammo stacks show coarse segments, so ammo is counted in stacks, not bullets).
A slot whose frame is not intact (a tooltip or a dragged item covers it) is not read. Weapons by the white shape of
the card's weapon compared with signs of the game's weapon icons. Attachment slots, armour, core and backpack: the tier
colour (1 white, 2 blue, 3 purple, 4 gold, 5 red). No text is read: players' languages differ.
"""
import json, os, sys
import numpy as np

_HERE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))


def _table():
    """pcs_inventory.json: {'weapons': {name: [aspect, hex]}, 'items': {kind: [hex rgb, hex weights]}} (see the tool)."""
    try: t = json.load(open(os.path.join(_HERE, 'pcs_inventory.json'), encoding='utf-8'))
    except Exception: t = {}
    W = {}
    for n, (ar, h) in (t.get('weapons') or {}).items():
        W[n] = (float(ar), np.array([int(c, 16) / 15.0 for c in h], np.float32).reshape(WSH, WSW))
    I = {}
    for n, (rgb, w) in (t.get('items') or {}).items():
        I[n] = (np.array([int(c, 16) * 17 for c in rgb], np.float32).reshape(CN_Y, CN_X, 3),
                np.array([int(c, 16) / 15.0 for c in w], np.float32).reshape(CN_Y, CN_X))
    return W, I


WSW, WSH = 40, 14                   # weapon sign: white shape in its own box, 40 x 14 cells
CN_X, CN_Y = 16, 14                 # item sign: slot rows 0..98 (above the stack bar), 16 x 14 colour cells
W, H = 2560, 1440

BOXES = ((901, 934, 220, 253), (1634, 1667, 220, 253))     # weapon card numbers '1' and '2'
CARDS = (883, 1616)                                         # weapon cards' left edges
SLOT_X0, SLOT_DX, SLOT_Y0, SLOT_DY, SLOT_N = 1032, 130, 803, 131, 118
COLS, ROWS = 8, 2

# What each sign is: heals, grenades, ammo. 'kind' groups them in the output.
KINDS = {'medkit': 'heal', 'syringe': 'heal', 'battery': 'heal', 'cell': 'heal', 'phoenix': 'heal',
         'ultaccel': 'ult', 'frag': 'grenade', 'thermite': 'grenade', 'arcstar': 'grenade',
         'light': 'ammo', 'heavy': 'ammo', 'energy': 'ammo', 'shotgun': 'ammo', 'sniper': 'ammo', 'arrows': 'ammo'}

WEAPONS, ITEMS = _table()


def is_open(a):
    """True when the frame shows the inventory screen (own inventory tab, weapon cards in place)."""
    bot = a[1368:1432:8, 260:2300:16]
    if np.median(bot.max(2)) > 70: return False
    for x0, x1, y0, y1 in BOXES:
        p = a[y0:y1, x0:x1].min(2)
        rim = np.concatenate([p[:3].ravel(), p[-3:].ravel(), p[:, :3].ravel()])
        if np.median(rim) <= 150: return False
        dark = float((p[6:-6, 9:-9] < 100).mean())                  # the number's dark strokes
        if not 0.08 < dark < 0.6: return False
    return True


# ---- weapons ----
WEAPON_ACCEPT, WEAPON_MARGIN = 0.10, 0.06


def _shape(mask):
    ys, xs = np.nonzero(mask)
    if len(xs) < 400: return None, None
    c = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.float32)
    h, w = c.shape
    s = _resample(c, WSH, WSW)
    return s, w / h


def _resample(c, n, m):
    """Box-average a 2-D array to n x m (each output cell = the mean of the pixels that fall in it)."""
    h, w = c.shape
    ys = np.searchsorted(np.arange(h) * n // h, np.arange(n)); xs = np.searchsorted(np.arange(w) * m // w, np.arange(m))
    s = np.add.reduceat(np.add.reduceat(c, ys, 0), xs, 1)
    cnt = np.diff(np.append(ys, h))[:, None] * np.diff(np.append(xs, w))[None, :]
    return (s / cnt).astype(np.float32)


def weapon(a, card):
    """-> weapon name (the game's file name: r97 = R-99, energy_ar = Havoc, 'p2020_akimbo' = two P2020s), or None (empty
    card, covered, or not sure)."""
    x0 = CARDS[card]
    b = a[265:425, x0 + 150:x0 + 620]
    m = np.minimum(np.minimum(b[..., 0], b[..., 1]), b[..., 2]) > 185
    s, ar = _shape(m)
    if s is None or not WEAPONS: return None
    sc = sorted((float(np.abs(s - v).mean()) + 0.1 * abs(float(np.log(ar / a_))), n) for n, (a_, v) in WEAPONS.items())
    if sc[0][0] > WEAPON_ACCEPT or (len(sc) > 1 and sc[1][0] - sc[0][0] < WEAPON_MARGIN): return None
    return sc[0][1]


# ---- tier colours ----
def tier(rgb):
    """Mean colour of a tier-coloured part -> 1 white / grey, 2 blue, 3 purple, 4 gold, 5 red; None when unclear."""
    r, g, b = (float(v) for v in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx < 90: return None
    sat = (mx - mn) / mx
    if sat < 0.15: return 1 if mx > 140 else None
    if sat < 0.35: return None
    if b == mx and r < 0.6 * b and g > 0.45 * b: return 2           # blue (cyan-ish)
    if b >= 0.8 * mx and r > 0.6 * b and g < 0.7 * r: return 3       # purple
    if r == mx and g > 0.6 * r and b < 0.5 * r: return 4             # gold
    if r == mx and g < 0.45 * r and b < 0.45 * r: return 5           # red
    return None


def _slot_frame(a, x0):
    """An attachment slot's two side lines (x0, x0 + 98) are there over rows 530..600 (nothing covers it)."""
    def line(x):
        g = a[530:600, x:x + 2].max(2).max(1)
        return float((g > 85).mean()) >= 0.9
    return line(x0) and line(x0 + 97)


def attachments(a, card):
    """Attachment slots of a card (4, or 5 with a hop-up) -> [tier, 0 = filled without a tier bar, None = empty or not
    read]. A card covered by a tooltip gives None for every slot it covers; a 5th slot is listed only when it is there."""
    out = []
    for j in range(5):
        x0 = CARDS[card] + 10 + 105 * j
        if not _slot_frame(a, x0):
            if j < 4: out.append(None)
            continue
        ic = a[525:590, x0 + 12:x0 + 86]
        bright = float((np.minimum(np.minimum(ic[..., 0], ic[..., 1]), ic[..., 2]) > 200).mean())
        bar = a[600:604, x0 + 12:x0 + 86].reshape(-1, 3).astype(np.float32)
        bar_t = tier(bar.mean(0)) if float(bar.max(1).mean()) > 150 else None
        if bright < 0.04: out.append(None)
        else: out.append(bar_t if bar_t else 0)
    return out


# ---- backpack slots ----
def _slot_xy(r, k): return SLOT_X0 + SLOT_DX * k, SLOT_Y0 + SLOT_DY * r


def frame_ok(a, r, k):
    """The slot's own frame (light grey or white lines) is all there: nothing covers the slot."""
    x0, y0 = _slot_xy(r, k); n = SLOT_N
    b = a[y0:y0 + n, x0:x0 + n]
    g = np.maximum(np.maximum(b[..., 0], b[..., 1]), b[..., 2])     # max: the yellow 'new item' corner counts too
    sides = [g[10:n - 10, 0:3].max(1), g[10:n - 10, n - 3:n].max(1), g[0:3, 10:n - 10].max(0), g[n - 3:n, 10:n - 10].max(0)]
    return all(float((s > 105).mean()) > 0.9 for s in sides)


def cells(a, r, k):
    """The slot above its stack bar (112 x 98 px) as 16 x 14 mean colours."""
    x0, y0 = _slot_xy(r, k)
    p = a[y0:y0 + 98, x0:x0 + 112].astype(np.float32)
    return p.reshape(CN_Y, 7, CN_X, 7, 3).mean((1, 3))


# Largest distance accepted per sign: the dev frames' (17 Nov 2026) largest distance of that item plus a few levels.
# Other things in a slot (attachments, a tooltip's edge) came 40 and more from the nearest ammo sign.
ITEM_ACCEPT = {'arcstar': 47, 'battery': 32, 'cell': 30, 'frag': 14, 'heavy': 27, 'light': 28, 'medkit': 20, 'ultaccel': 32,
               'shotgun': 24, 'syringe': 26, 'thermite': 28}
ITEM_ACCEPT_OTHER = 28          # signs not seen on the dev frames (energy, sniper ammo, arrows)
ITEM_MARGIN = 10.0
DIGITS_CORNER = (slice(9, CN_Y), slice(11, CN_X))        # the count's digits sit bottom right: left out


def _stacked():
    """All item signs as arrays: names, colours (n, 14, 16, 3), weights (n, 14, 16) normalised: cells the image covers
    well (> 0.6), the count's corner left out."""
    names = list(ITEMS)
    if not names: return names, None, None
    rgb = np.stack([ITEMS[n][0] for n in names])
    w = np.stack([ITEMS[n][1] for n in names]).copy()
    w[(slice(None),) + DIGITS_CORNER] = 0
    w = np.where(w > 0.6, w, 0)
    w /= np.maximum(w.sum((1, 2), keepdims=True), 1e-6)
    return names, rgb, w


_NAMES, _RGB, _W = _stacked()


def item(a, r, k):
    """-> (sign name, distance) of the slot's icon, or (None, best distance)."""
    if not _NAMES: return None, None
    c = cells(a, r, k)
    d = (np.abs(_RGB - c[None]).mean(3) * _W).sum((1, 2))
    sc = sorted(zip(d.tolist(), _NAMES))
    if sc[0][0] > ITEM_ACCEPT.get(sc[0][1], ITEM_ACCEPT_OTHER) or (len(sc) > 1 and sc[1][0] - sc[0][0] < ITEM_MARGIN): return None, sc[0][0]
    return sc[0][1], sc[0][0]


def stack(a, r, k):
    """Stack bar under the icon -> (lit segments, segments) or None. Segments are runs of bar pixels split by gaps
    (a gap is darker than the bar on both sides of it: dim segments are only a little brighter than the gaps)."""
    x0, y0 = _slot_xy(r, k)
    p = a[y0 + 103:y0 + 106, x0 + 4:x0 + 114].astype(np.float32).mean(2).mean(0)
    win = np.lib.stride_tricks.sliding_window_view(np.concatenate((np.zeros(5, np.float32), p, np.zeros(5, np.float32))), 5).max(1)
    left = win[:len(p)]; right = win[6:6 + len(p)]           # brightest of the 5 pixels before / after each pixel
    on = (p > 60) & ~((p < left - 15) & (p < right - 15))
    d = np.diff(np.concatenate(([0], on.view(np.int8), [0])))
    st = np.flatnonzero(d == 1); en = np.flatnonzero(d == -1)
    segs = [(s, e) for s, e in zip(st, en) if e - s >= 8]
    if not segs or len(segs) > 8: return None
    lv = np.array([float(np.median(p[s + 2:e - 2])) for s, e in segs])
    top = lv.max()
    if top < 150: return None
    lit = lv > 0.82 * top; dim = lv < 0.72 * top
    if not np.all(lit | dim): return None                     # a segment neither lit nor dim: not sure
    if not lit[0] or (lit[1:] & ~lit[:-1]).any(): return None  # lit segments come first
    return int(lit.sum()), len(segs)


def locked(a, r, k):
    """A grey padlock in the middle of a dark slot (slots the backpack does not open)."""
    x0, y0 = _slot_xy(r, k)
    c = a[y0 + 30:y0 + 90, x0 + 35:x0 + 85].astype(np.int16)
    mx = c.max(2); mn = c.min(2)
    grey = (mx - mn < 18) & (mx > 42) & (mx < 140)
    return float(grey.mean()) > 0.25 and float((mx > 170).mean()) < 0.02


def read(a, t=None):
    """One keyframe -> {t, weapons, attachments, items: [[row, col, sign, lit, segments]], counts, unread, ...} or None
    when the inventory screen is not open."""
    if a.shape[0] != H or a.shape[1] != W or not is_open(a): return None
    out = {'t': round(float(t), 2) if t is not None else None,
           'weapons': [weapon(a, 0), weapon(a, 1)], 'attachments': [attachments(a, 0), attachments(a, 1)],
           'slots': [], 'unread': 0, 'locked': 0, 'open': 0}
    for r in range(ROWS):
        for k in range(COLS):
            if not frame_ok(a, r, k):
                if locked(a, r, k): out['locked'] += 1
                else: out['unread'] += 1
                continue
            out['open'] += 1
            n, _ = item(a, r, k)
            if n is None: continue
            st = stack(a, r, k)
            out['slots'].append([r, k, n, st[0] if st else None, st[1] if st else None])
    out['armor'] = tier(_coloured(a[1125:1205, 1005:1105]))
    out['core'] = _stripe(a, 1295, 1375)
    out['backpack'] = _stripe(a, 1737, 1817)
    return out


def _coloured(b):
    """Mean colour of the saturated or bright pixels of a box (the armour icon)."""
    p = b.reshape(-1, 3).astype(np.float32)
    mx = p.max(1); mn = p.min(1)
    m = (mx > 120) & ((mx - mn) > 60) | (mn > 200)
    return p[m].mean(0) if m.sum() > 60 else np.zeros(3)


def _stripe(a, x0, x1):
    """Tier of an equipment slot by the bright stripe along its bottom (rows 1232..1242: white, blue, purple...)."""
    b = a[1230:1244, x0:x1].astype(np.float32).mean(1)            # rows x RGB
    y = int(b.max(1).argmax())
    if b[y].max() < 150: return None
    return tier(b[y])


def summary(rd):
    """A read -> {heals {kind: n}, grenades {kind: n}, ammoStacks {kind: n}} from slots with a sure count."""
    out = {'heals': {}, 'grenades': {}, 'ammoStacks': {}, 'ultAccel': 0}
    for r, k, n, lit, segs in rd['slots']:
        kind = KINDS.get(n)
        if kind == 'heal' and lit is not None: out['heals'][n] = out['heals'].get(n, 0) + lit
        elif kind == 'grenade' and lit is not None: out['grenades'][n] = out['grenades'].get(n, 0) + lit
        elif kind == 'ammo': out['ammoStacks'][n] = out['ammoStacks'].get(n, 0) + 1
        elif kind == 'ult' and lit is not None: out['ultAccel'] += lit
    return out


OPEN_GAP = 3.5                  # keyframes further apart than this are two different opens


def attach(games, reads):
    """reads: read() results of the recording (inventory open only). Per game (added, nothing changed):
    inventory = [{t, tEnd, reads, weapons [name | None, name | None], attachments [[tier ...], [...]], heals {kind: n},
    grenades {kind: n}, ammoStacks {kind: n}, armor, core, backpack, slotsOpen, slotsUnread}] one per open of the
    screen, from its last keyframe (what the player carried when closing it). A value that differs between the last two
    keyframes of an open (an item moved, a tooltip) is left out (None, or the item kind left out)."""
    reads = sorted((r for r in reads if r and r.get('t') is not None), key=lambda r: r['t'])
    opens = []
    for r in reads:
        if opens and r['t'] - opens[-1][-1]['t'] <= OPEN_GAP: opens[-1].append(r)
        else: opens.append([r])
    for i, g in enumerate(games):
        t0 = g['tStart']; t1 = games[i + 1]['tStart'] if i + 1 < len(games) else float('inf')
        end = g.get('tEndGame')
        L = []
        for op in opens:
            if not (t0 <= op[0]['t'] < t1) or (end is not None and op[0]['t'] > end + 2): continue
            L.append(_entry(op))
        g['inventory'] = L
    return games


def _entry(op):
    last = op[-1]; prev = op[-2] if len(op) > 1 else None
    s = summary(last); sp = summary(prev) if prev else None
    def same(key, sub=None):
        if prev is None: return True
        if sub is None: return last[key] == prev[key]
        return s[key].get(sub) == sp[key].get(sub)
    e = {'t': op[0]['t'], 'tEnd': last['t'], 'reads': len(op),
         'weapons': [w if (prev is None or prev['weapons'][j] == w) else None for j, w in enumerate(last['weapons'])],
         'attachments': [[x if (prev is None or prev['attachments'][j][q] == x) else None for q, x in enumerate(A)]
                         for j, A in enumerate(last['attachments'])]}
    for key in ('heals', 'grenades', 'ammoStacks'):
        e[key] = {k: v for k, v in s[key].items() if same(key, k)}
    e['ultAccel'] = s['ultAccel'] if (prev is None or s['ultAccel'] == sp['ultAccel']) else None
    for key in ('armor', 'core', 'backpack'):
        e[key] = last[key] if same(key) else None
    e['slotsOpen'] = last['open'] + 0; e['slotsLocked'] = last['locked']; e['slotsUnread'] = last['unread']
    return e
