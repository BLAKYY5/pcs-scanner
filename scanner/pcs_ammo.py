"""Ammo and heals the recording player carries, from the HUD (2560 x 1440, 16:9 only).

Weapon panel, bottom right: the magazine as two big digits (x 2306..2376, rows 1285..1326) and the reserve as three
smaller ones under it (x 2305..2374, rows 1337..1362). The places are fixed: the game pads both with dim leading zeros
('04', '076'), so each digit sits in its own cell and is compared there. An empty magazine shows '00' with the last
zero in a red box. Weapons without a magazine count (L-STAR) or with no reserve limit (some modes show a sign for
'no limit' there) give shapes that match no digit: no read.
Heal slot, bottom left of the centre: the selected heal item's icon (x 652..692, rows 1318..1362) and how many of it
the player carries, 1 or 2 white digits right-aligned at x 717 (rows 1346..1365). Only the selected kind is shown.

Digits are cut at their own contrast, squeezed to 12 x 18 grey cells and compared with shapes from our own recordings
(pcs_banner.json 'ammo': per font 'M' magazine, 'R' reserve, 'H' heal count; 'X' = not a digit). A digit counts only
when the nearest shape is close and clearly closer than any other digit's; one doubtful digit = no number.
"""
import bisect, json, os, sys
import numpy as np

_HERE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))


def _table():
    """pcs_banner.json 'ammo': {font: [[label, 216 hex digits (12 x 18 grey cells, 0..f)], ...]}."""
    try: t = json.load(open(os.path.join(_HERE, 'pcs_banner.json'), encoding='utf-8')).get('ammo') or {}
    except Exception: t = {}
    return {f: (np.array([r[0] for r in L]), np.array([[int(c, 16) / 15.0 for c in r[1]] for r in L], np.float32))
            for f, L in t.items() if isinstance(L, list)}


T = _table()

MAG_Y = (1283, 1328)
MAG_X = ((2306, 2340), (2342, 2376))
RES_Y = (1335, 1364)
RES_X = ((2305, 2328), (2328, 2351), (2351, 2374))
HEAL_ICON = (1316, 1364, 648, 696)       # y0, y1, x0, x1


HEAL_ROWS = (1346, 1366)                   # the count's digits (18 px high, rows 1347..1364)
HEAL_COLS = (691, 721)                     # searched for them (right-aligned at x 716; the icon ends at x ~700 above row 1352)
HEAL_ICON_X1 = 702                         # the icon may reach this far right above row 1352


def heal_digits(a):
    """Boxes (x0, x1, y0, y1) of the heal count's white digits, left to right. Columns are found in rows 1347..1364 (1352..1364
    left of x 702, below where the item's icon can reach)."""
    x0, x1 = HEAL_COLS
    b = a[1347:1365, x0:x1]
    m = np.minimum(np.minimum(b[..., 0], b[..., 1]), b[..., 2]) > 185
    m[:5, :HEAL_ICON_X1 - x0] = False                # rows 1347..1351 left of x 702: the icon reaches there
    col = m.sum(0) >= 2
    d = np.diff(np.concatenate(([0], col.view(np.int8), [0])))
    st = np.flatnonzero(d == 1); en = np.flatnonzero(d == -1)
    runs = []
    for s, e in zip(st, en):                         # a one-column gap inside a digit (the bars of 4 and 0 are thin)
        if runs and s - runs[-1][1] <= 1: runs[-1][1] = e
        else: runs.append([s, e])
    return [(x0 + int(s) - 1, x0 + int(e) + 1) + HEAL_ROWS for s, e in runs]


def cell_boxes(a):
    """{font: [(x0, x1, y0, y1), ...]} of the digit cells (left to right)."""
    return {'M': [(x0, x1) + MAG_Y for x0, x1 in MAG_X], 'R': [(x0, x1) + RES_Y for x0, x1 in RES_X],
            'H': heal_digits(a)}


_LIN = {}
def _lin(n, m):
    """n x m matrix that resamples m values to n (linear, pixel centres aligned)."""
    if (n, m) not in _LIN:
        R = np.zeros((n, m), np.float32); x = (np.arange(n) + 0.5) * m / n - 0.5
        x0 = np.clip(np.floor(x).astype(int), 0, m - 1); x1 = np.clip(x0 + 1, 0, m - 1); f = np.clip(x - x0, 0, 1)
        np.add.at(R, (np.arange(n), x0), 1 - f); np.add.at(R, (np.arange(n), x1), f)
        _LIN[(n, m)] = R
    return _LIN[(n, m)]


MIN_CONTRAST = 40                       # a digit's strokes against what is behind them (grey levels)
DIM_CONTRAST = 15                       # the dim leading zeros ('04', '076'): read only as '0'
def patch(a, box, min_contrast=MIN_CONTRAST):
    """The cell as 12 x 18 grey values from 0 (behind) to 1 (stroke), at its own contrast; None when flat."""
    x0, x1, y0, y1 = box
    b = a[y0:y1, x0:x1]
    v = (b[..., 0].astype(np.uint16) + b[..., 1] + b[..., 2]).astype(np.float32) * (1 / 3.0)
    s = v.ravel(); k1, k2 = int(0.25 * (s.size - 1)), int(0.97 * (s.size - 1))
    s = np.partition(s, (k1, k2)); lo = float(s[k1]); hi = float(s[k2])
    if hi - lo < min_contrast: return None
    g = np.clip((v - lo) / (hi - lo), 0, 1)
    return (_lin(18, g.shape[0]) @ g @ _lin(12, g.shape[1]).T).ravel()


ACCEPT, MARGIN = 0.13, 0.02            # mean grey difference to the nearest shape; lead over another label


def digit(a, box, font, min_contrast=MIN_CONTRAST):
    """-> label ('0'..'9', 'X' = not a digit) or None (flat cell, or doubtful)."""
    if font not in T: return None
    p = patch(a, box, min_contrast)
    if p is None: return None
    labels, S = T[font]
    d = np.abs(S - p).mean(1)
    i = int(d.argmin()); lab = str(labels[i]); di = float(d[i])
    rest = d[labels != labels[i]]
    if di > ACCEPT or (len(rest) and float(rest.min()) - di < MARGIN): return None
    return lab


def number(a, font, boxes):
    """All cells must read as digits -> int, else None. Magazine and reserve: a leading cell too faint for a digit
    may be one of the dim padding zeros, read at a lower contrast and only as '0' (the units cell never)."""
    out = ''
    for k, b in enumerate(boxes):
        lab = digit(a, b, font)
        if lab is None and font in ('M', 'R') and k < len(boxes) - 1 and out.strip('0') == '' and patch(a, b) is None:
            lab = digit(a, b, font, DIM_CONTRAST)
            if lab != '0': return None
        if lab is None or lab == 'X': return None
        out += lab
    return int(out) if out else None


HINT = (1378, 1409, 660, 715)          # the heal slot's key hint (a white box ~28 px wide, centred at x ~687)


def _hint(a):
    y0, y1, x0, x1 = HINT
    p = a[y0:y1, x0:x1]
    m = np.minimum(np.minimum(p[..., 0], p[..., 1]), p[..., 2]) > 200
    cs = np.flatnonzero(m.sum(0) >= 3)
    if len(cs) < 6: return False
    w = cs[-1] - cs[0] + 1
    return 22 <= w <= 34 and m[:, cs[0]:cs[-1] + 1].mean() >= 0.45 and abs((cs[0] + cs[-1]) / 2 + x0 - 687) <= 6


def heal_item(a):
    """The selected heal item by its icon's colours: 'shield' (cell or battery: blue), 'syringe' (white, small red
    cross), 'medkit' (red), 'phoenix' (orange), or None."""
    y0, y1, x0, x1 = HEAL_ICON
    p = a[y0:y1, x0:x1].astype(np.int16)
    r, g, b = p[..., 0], p[..., 1], p[..., 2]
    blue = int(((b > 150) & (b - r > 80)).sum())
    if blue >= 150: return 'shield'
    red = int(((r > 150) & (g < 90) & (b < 90)).sum())
    white = int((np.minimum(np.minimum(r, g), b) > 200).sum())
    orange = int(((r > 200) & (g > 100) & (g < 180) & (b < 80)).sum())
    if white >= 1000: return 'syringe'
    if red >= 800: return 'medkit'
    if orange >= 300: return 'phoenix'
    return None


def read_heal(a):
    """-> (item, count) when the heal slot shows a count, ('none', 0) when the slot is there (key hint) but empty
    (no count, no icon), else None."""
    boxes = heal_digits(a)
    if boxes and len(boxes) <= 2:
        n = number(a, 'H', boxes)
        if n is not None and n > 0: return heal_item(a), n
        return None
    if not boxes and _hint(a) and heal_item(a) is None:
        y0, y1, x0, x1 = HEAL_ICON
        p = a[y0:y1, x0:x1]
        if (np.minimum(np.minimum(p[..., 0], p[..., 1]), p[..., 2]) > 200).sum() < 40: return 'none', 0
    return None


SLOT_ROWS = (1386, 1406)               # the weapon names under the panel: the weapon in hand's name is white
SLOT_X = ((2075, 2240), (2290, 2470))  # slot 1 (with its white key box '1': ~120 px), slot 2 (no key box inside)


def slot(a):
    """1 or 2: which weapon slot is in hand (its name is drawn white, the other grey), or None."""
    w = [int((np.minimum(np.minimum(p[..., 0], p[..., 1]), p[..., 2]) > 200).sum())
         for p in (a[SLOT_ROWS[0]:SLOT_ROWS[1], x0:x1] for x0, x1 in SLOT_X)]
    if w[1] >= 100: return 2
    if w[1] < 15 and w[0] >= 150: return 1
    return None


def read(a, t=None):
    """One keyframe: (t, mag, reserve, heal item, heal count, slot) with None for what could not be read, or None when
    nothing was read. mag / reserve are the weapon in hand (slot 1 or 2); heal item 'none' with count 0 = the heal
    slot is empty: no heals left."""
    B = cell_boxes(a)
    mag = number(a, 'M', B['M'])
    res = number(a, 'R', B['R']) if mag is not None else None     # the reserve only with a magazine read
    h = read_heal(a)
    if h is not None and h[0] == 'none' and mag is None: h = None    # empty slot: only in the full HUD (ammo read)
    if mag is None and h is None: return None
    return (None if t is None else round(float(t), 2), mag, res, h[0] if h else None, h[1] if h else None,
            slot(a) if mag is not None else None)


# ---------------- per game ----------------
FIGHT_BEFORE = 20.0     # s: the last ammo read up to this long before a fight's start (and 1 s after) tells its ammo
MAG_WINDOW = 180.0      # s: a weapon's full magazine = the largest magazine read in its slot over this time before
EMPTY_HOLD = 2          # empty heal slot reads in a row (no heal count read in between) for 'out of heals'
EMPTY_GAP = 15.0        # s: at most this between two of those reads


def _spect_filter(bar_reads):
    spect = sorted(t for t, o, m, spec in (bar_reads or []) if spec or (o and o.get('state') == 'spectating'))
    def spectating(t):
        k = bisect.bisect_left(spect, t - 0.01)
        return k < len(spect) and abs(spect[k] - t) < 0.02
    return spectating


def _timeline(rows, key):
    """Consecutive rows with the same key(row) are kept once (the first)."""
    out = []; last = object()
    for r in rows:
        k = key(r)
        if k != last: out.append(r); last = k
    return out


def fight_ammo(R, t0):
    """Ammo of the weapon in hand when a fight started: {mag, reserve, slot, magFull, at, low} or None.
    low = less than one full magazine left in reserve (reserve < magFull), or nothing left at all."""
    before = [r for r in R if t0 - FIGHT_BEFORE <= r[0] <= t0 + 1.0 and r[1] is not None and r[2] is not None]
    if not before: return None
    t, mag, res, _, _, sl = before[-1]
    full = [r[1] for r in R if t - MAG_WINDOW <= r[0] <= t and r[1] is not None and r[5] == sl]
    mfull = max(full) if full else mag
    low = (res == 0 and mag == 0) or (mfull >= 2 and res < mfull)
    return {'mag': mag, 'reserve': res, 'slot': sl, 'magFull': mfull, 'at': t, 'low': bool(low)}


def out_of_heals(R):
    """Times the heal slot went empty after the player had heals: [{t, end, reads}] (end None: still empty when the
    reads stop). t = the first empty read; end = the first heal count read after."""
    H = [(r[0], r[3], r[4]) for r in R if r[3] is not None]
    out = []; had = False; i = 0
    while i < len(H):
        t, item, n = H[i]
        if item != 'none': had = True; i += 1; continue
        j = i
        while j + 1 < len(H) and H[j + 1][1] == 'none' and H[j + 1][0] - H[j][0] <= EMPTY_GAP: j += 1
        if had and j - i + 1 >= EMPTY_HOLD:
            end = next((x[0] for x in H[j + 1:] if x[1] != 'none'), None)
            out.append({'t': round(float(t), 2), 'end': None if end is None else round(float(end), 2), 'reads': j - i + 1})
            had = False
        i = j + 1
    return out


def attach(games, ammo_reads, bar_reads=None):
    """ammo_reads: rows of read() for the recording. Per game (fields added, none changed):
    ammoTimeline {cols, rows [t, slot, mag, reserve]}: the reads, a row kept when it differs from the one before;
    healTimeline {cols, rows [t, item, count]}: the same for the selected heal item and its count ('none', 0 = empty);
    ammoEvents [{t, kind: lowAmmoFight, mag, reserve, magFull, fightEnd} | {t, kind: outOfHeals, end}];
    ammoSummary {reads, healReads, fightsChecked, lowAmmoFights, outOfHeals, outOfHealsSeconds};
    each fight (pcs_fights) gets ammoAtStart {mag, reserve, slot, magFull, at, low} when the ammo was read before it.
    Frames taken while spectating (the HUD is someone else's) are left out."""
    spectating = _spect_filter(bar_reads)
    for i, g in enumerate(games):
        t0 = g['tStart']; t1 = games[i + 1]['tStart'] if i + 1 < len(games) else float('inf')
        end = g.get('tEndGame')
        R = [r for r in ammo_reads if t0 <= r[0] < t1 and (end is None or r[0] <= end + 2) and not spectating(r[0])]
        A_ = [r for r in R if r[1] is not None]
        g['ammoTimeline'] = {'cols': ['t', 'slot', 'mag', 'reserve'],
                             'rows': [[r[0], r[5], r[1], r[2]] for r in _timeline(A_, lambda r: (r[5], r[1], r[2]))]}
        H = [r for r in R if r[3] is not None]
        g['healTimeline'] = {'cols': ['t', 'item', 'count'],
                             'rows': [[r[0], r[3], r[4]] for r in _timeline(H, lambda r: (r[3], r[4]))]}
        ev = []; checked = low = 0
        for f in g.get('fights') or []:
            fa = fight_ammo(R, f['t0'])
            if fa is None: continue
            f['ammoAtStart'] = fa; checked += 1
            if fa['low']:
                low += 1
                ev.append({'t': round(float(f['t0']), 2), 'kind': 'lowAmmoFight', 'mag': fa['mag'], 'reserve': fa['reserve'],
                           'magFull': fa['magFull'], 'fightEnd': f.get('t1')})
        oh = out_of_heals(R)
        stop = end if end is not None else (R[-1][0] if R else t0)
        for o in oh: ev.append({'t': o['t'], 'kind': 'outOfHeals', 'end': o['end']})
        g['ammoEvents'] = sorted(ev, key=lambda e: e['t'])
        g['ammoSummary'] = {'reads': len(A_), 'healReads': len(H), 'fightsChecked': checked, 'lowAmmoFights': low,
                            'outOfHeals': len(oh),
                            'outOfHealsSeconds': round(sum(max(0.0, (o['end'] if o['end'] is not None else stop) - o['t'])
                                                           for o in oh), 1)}
    return games
