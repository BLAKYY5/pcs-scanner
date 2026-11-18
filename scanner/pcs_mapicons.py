"""Care packages and strongholds on the full map (M key).

What the full map draws (Season 31, checked on our recordings, unzoomed map at 2560 x 1440, icons about 45 px):
- care package: a diamond with a light border and a helmet inside. Three looks: white border on a dark fill, yellow
  border on a dark fill, and yellow with a line across it and a see-through fill (seen on packages already opened).
  In scrims the packages are on the map as soon as they are announced; in ranked only once revealed (in our ranked
  recordings of 17 Nov: one package in 17 games).
- stronghold (new in Season 31, a locked vault airdropped during the game): a red octagon with a door inside while it
  can be captured, a dark octagon with a light border afterwards.

How they are found (no picture of the game is kept: only numbers derived from the game's icon files):
  1. the light parts of the map (the smaller of red and green above 150: white and yellow both pass, the red ring tint
     does not), at half size;
  2. a diamond outline score: light on the outline, dark just outside it (map labels, lines and the white circles
     fail this);
  3. the inside of each diamond, 12 x 12 samples, compared with the helmet of the game's care package icon
     (CARE_SIGNS: the sign of each sample, derived once from the icon file);
  4. red strongholds: blobs of the icon's red (pink-red, bluer than the red ring tint), octagon-sized; dark ones:
     the door of the game's stronghold icon (DOOR_SIGNS, derived the same way), light on black.
Positions in map % through MapReader's transform M (screen px -> map %), like the harvesters and the ring circles.

Per game (attach): reads grouped by place (each icon is seen on many map opens), the timing windows of the game's
ring phases (the coach's rules, Ranked / BR, 18 Nov 2026: 4 care packages, 1 and 2 dropped together a few seconds
after ring 1 starts closing, landing inside ring 2; 3 announced during ring 2, inside ring 3; 4 announced during
ring 3, inside ring 4) number the packages and drop reads at times no package can be on the map.
"""
import numpy as np
from scipy import ndimage, signal

X0, X1, Y1 = 560, 1960, 1340      # the full map's area on screen (MapReader's crop)
LIGHT = 150                       # min(red, green) of the icons' white / yellow outline
R_DIAMOND = 23.5                  # care package outline: |dx| + |dy| at the middle of its 3-4 px line (tips rounded), full size px
G = 12
SCALE_43 = 1264 / 1353         # icon size in a 4:3 stretched recording after the scanner's view_map (its map is that much smaller)
# The helmet of rui/hud/gametype_icons/survival/care_package_icon (RSX export, Season 31): sign of each of 12 x 12
# samples of min(red, green) inside the diamond, minus their mean (# = lighter). Built by tools in the private notes.
CARE_SIGNS = ('.#........#.'
              '#...####...#'
              '..########..'
              '.....##.....'
              '..###..###..'
              '...######...'
              '....####.#..'
              '...#.#####..'
              '...#....#...'
              '....#####...'
              '#...####...#'
              '.#...##...#.')
_S = np.array([1.0 if ch == '#' else -1.0 for ch in CARE_SIGNS]).reshape(G, G)
# The door of rui/hud/gametype_icons/survival/stronghold_map_available_icon, the same way on the brightness (mean of
# R, G, B): # = the red fill, . = the black door and outline. A taken stronghold draws it the other way round (light
# grey door on a black fill). On screen the 12 x 12 cells cover 25 px (half size: 12 px, one cell a pixel).
DOOR_SIGNS = ('#..........#'
              '############'
              '..#......#..'
              '..#......#..'
              '..#.####.#..'
              '..#.#..#.#..'
              '..#.#..#.#..'
              '..#.#..#.#..'
              '..#.####.#..'
              '####....####'
              '##.######.##'
              '###......###')
_D = np.array([1.0 if ch == '#' else -1.0 for ch in DOOR_SIGNS]).reshape(G, G)
DOOR_CELL = 1.03                  # px per cell at full size (the door: 25 px across)
DOOR_MIN = 0.5                   # taken strongholds -0.57..-0.64 on our recordings, nothing else below -0.4
CARE_MIN = 0.6                    # glyph match (care packages 0.75-0.85 on our recordings, other diamonds <= 0.35)
OPENED_MIN = 0.4                  # opened ones (a line across the helmet): 0.45-0.58
SHAPE_MIN = 0.25                  # outline score of a candidate diamond (care packages 0.4-0.6, labels and lines lower)


def _ring_kernel(dist, r, w):
    """Weights on a shape's outline (+) and on a band just outside it (-), half size."""
    n = int(r + w + 4); y, x = np.mgrid[-n:n + 1, -n:n + 1].astype(np.float32)
    d = dist(x, y)
    on = (np.abs(d - r) <= w / 2).astype(np.float32)
    off = (np.abs(d - (r + w + 1)) <= 0.75).astype(np.float32)     # inside: the helmet / door can reach the line
    return on / on.sum() - off / off.sum()


_DIAM = lambda x, y: np.abs(x) + np.abs(y)
_cache = {}


def _kernels(scale):
    k = round(scale, 3)
    if k not in _cache:
        n = max(13, int(round(2 * G * DOOR_CELL * scale))) | 1   # the door template, full size, odd
        u = ((np.arange(n) + 0.5) / n * G).astype(int)
        T = _D[np.ix_(u, u)]; T = T - T.mean()
        _cache[k] = (_ring_kernel(_DIAM, R_DIAMOND * scale / 2, 1.6 * scale), T / np.linalg.norm(T))
    return _cache[k]


def door_ncc(B, T):
    """Normalised correlation of the brightness B with the door template T at every place (-1..1), 0 near the border."""
    n = T.shape[0]
    s = signal.fftconvolve(B, T[::-1, ::-1], mode='same')
    m = ndimage.uniform_filter(B, n); m2 = ndimage.uniform_filter(B * B, n)
    out = s / np.sqrt(np.maximum(m2 - m * m, 1.0) * n * n)
    out[:n] = 0; out[-n:] = 0; out[:, :n] = 0; out[:, -n:] = 0
    return out


def _peaks(score, thr, size):
    mx = ndimage.maximum_filter(score, size=size)
    ys, xs = np.nonzero((score == mx) & (score > thr))
    return sorted(zip(xs.tolist(), ys.tolist()), key=lambda p: -score[p[1], p[0]])[:40]


def _glyph(f, cx, cy, r):
    """12 x 12 samples inside a diamond (half diagonal r), minus their mean, unit length."""
    u = (np.arange(G) + 0.5) / G * 2 - 1
    U, V = np.meshgrid(u, u)
    k = 0.58 * r                                     # the helmet sits within 0.58 of the outline
    v = ndimage.map_coordinates(f, [cy + V * k, cx + U * k], order=1)
    v = v - v.mean()
    return v / (np.linalg.norm(v) + 1e-6)


def care_match(f, cx, cy, r):
    """How much the inside of the diamond at (cx, cy) looks like the care package's helmet (-1..1), with the centre
    moved by up to 2 px to the best place. -> (score, cx, cy)."""
    best = (-1.0, cx, cy)
    for dy in (-2, -1, 0, 1, 2):
        for dx in (-2, -1, 0, 1, 2):
            s = float((_glyph(f, cx + dx, cy + dy, r) * _S).sum() / G)
            if s > best[0]: best = (s, cx + dx, cy + dy)
    return best


def _style(c, f, cx, cy, r):
    """white / yellow (outline colour) / opened (yellow with a line across a see-through fill)."""
    h, w = f.shape
    x0, x1 = int(cx - r), int(cx + r) + 1; yc = int(round(cy))
    if 0 <= yc < h and x0 >= 0 and x1 <= w:
        mid = f[yc - 1:yc + 2, int(cx - 0.6 * r):int(cx + 0.6 * r)].max(0)
        q = int(0.35 * r)                    # above and below the line: the helmet on a see-through fill, not a filled
        rest = np.r_[f[yc - q:yc - 3, int(cx - q):int(cx + q)].ravel(), f[yc + 4:yc + q, int(cx - q):int(cx + q)].ravel()]
        if (mid > LIGHT).mean() > 0.8 and rest.size and (rest > LIGHT).mean() < 0.3: return 'opened'   # (ping marker)
    y, x = np.mgrid[-int(r) - 3:int(r) + 4, -int(r) - 3:int(r) + 4]
    on = np.abs(np.abs(x) + np.abs(y) - r) <= 1.5
    ys, xs = (y[on] + int(round(cy))), (x[on] + int(round(cx)))
    ok = (ys >= 0) & (ys < h) & (xs >= 0) & (xs < w)
    px = c[ys[ok], xs[ok]].astype(np.float32)
    px = px[np.minimum(px[:, 0], px[:, 1]) > LIGHT]
    if not len(px): return 'white'
    m = px.mean(0)
    return 'yellow' if m[2] < 0.8 * min(m[0], m[1]) else 'white'


def read(a, M, scale=1.0):
    """Care packages and strongholds on one full-map frame (2560 x 1440 RGB, 16:9 or rebuilt by the scanner's view_map).
    M: MapReader's transform (screen px -> map %). scale: icon size against a 16:9 recording (4:3 stretched: 4 / 3).
    -> {'care': [{x, y, px, py, score, style}], 'strongholds': [{x, y, px, py, state}]} (x, y in map %)."""
    M = np.asarray(M, float)
    c = a[:Y1, X0:X1]
    f = np.minimum(c[..., 0], c[..., 1]).astype(np.float32)
    h2, w2 = f.shape[0] // 2, f.shape[1] // 2
    L = (f[:2 * h2, :2 * w2] > LIGHT).reshape(h2, 2, w2, 2).mean((1, 3)).astype(np.float32)
    kd, ko = _kernels(scale)
    to_map = lambda px, py: [round(float(v), 2) for v in np.array([px + X0, py, 1.0]) @ M]
    inside = lambda p: 0 <= p[0] <= 100 and 0 <= p[1] <= 100
    care = []
    sd = signal.fftconvolve(L, kd[::-1, ::-1], mode='same')
    for x, y in _peaks(sd, SHAPE_MIN, 9):
        s, cx, cy = care_match(f, 2 * x + 0.5, 2 * y + 0.5, R_DIAMOND * scale)
        if s < OPENED_MIN: continue
        st = _style(c, f, cx, cy, R_DIAMOND * scale)
        if s < CARE_MIN and st != 'opened': continue        # the line across an opened one spoils its helmet a little
        p = to_map(cx, cy)
        if not inside(p) or any(abs(cx + X0 - q['px']) < 10 and abs(cy - q['py']) < 10 for q in care): continue
        care.append({'x': p[0], 'y': p[1], 'px': round(cx + X0, 1), 'py': round(cy, 1), 'score': round(s, 2), 'style': st})
    sh = []
    # red (can be captured): the icon's pink-red fill
    ci = c.astype(np.int16); r_, g_, b_ = ci[..., 0], ci[..., 1], ci[..., 2]
    red = (r_ > 190) & (g_ < 100) & (b_ > 60) & (b_ - g_ > 15) & (r_ - b_ > 80)
    lab, n = ndimage.label(ndimage.binary_closing(red, np.ones((5, 5), bool)))     # the map's crosshair can cut it in two
    area = (250 * scale ** 2, 2800 * scale ** 2)      # drawn larger for a while (just landed or hovered)
    for i, sl in enumerate(ndimage.find_objects(lab)):
        hh = sl[0].stop - sl[0].start; ww = sl[1].stop - sl[1].start
        if not (26 * scale <= ww <= 72 * scale and 26 * scale <= hh <= 72 * scale and abs(ww - hh) <= 12 * scale): continue
        k = int((lab[sl] == i + 1).sum())
        if not area[0] <= k <= area[1]: continue
        cy, cx = ndimage.center_of_mass(lab[sl] == i + 1)
        cx += sl[1].start; cy += sl[0].start
        p = to_map(cx, cy)
        if inside(p): sh.append({'x': p[0], 'y': p[1], 'px': round(cx + X0, 1), 'py': round(cy, 1), 'state': 'available'})
    # dark (taken): the door the other way round (light on black), by its shape
    B = c.astype(np.float32).mean(2)
    n = ko.shape[0]
    for x, y in _peaks(-door_ncc(B, ko), DOOR_MIN, 15):
        cx, cy = float(x), float(y)
        if any(abs(cx + X0 - q['px']) < 24 and abs(cy - q['py']) < 24 for q in sh + care): continue
        # light door on a black fill: the fill cells dark, the door cells clearly lighter
        win = B[y - n // 2:y + n // 2 + 1, x - n // 2:x + n // 2 + 1]
        if win.shape != ko.shape: continue
        fill = win[ko > 0]; door = win[ko < 0]
        if np.median(fill) > 70 or np.median(door) < np.median(fill) + 60: continue
        p = to_map(cx, cy)
        if inside(p): sh.append({'x': p[0], 'y': p[1], 'px': round(cx + X0, 1), 'py': round(cy, 1), 'state': 'unavailable'})
    return {'care': care, 'strongholds': sh}


# ---------- per game: care packages numbered by the ring phases, strongholds, the next-ring check ----------
NEAR_CARE = 1.5       # map %: reads of one care package (it never moves once announced)
NEAR_SH = 2.0         # map %: reads of one stronghold
EARLY_S = 15          # s: no care package is on the map before ring 1 starts closing (reads before = not packages)
LAST_ROUND = {1: 2, 2: 3, 3: 4}   # first seen in round k: at most package n (1-2 drop in round 1, 3 in round 2, 4 in round 3)


def _groups(R, near):
    """[(t, x, y, extra)] -> lists of reads at one place (greedy, in time order)."""
    G = []
    for r in sorted(R, key=lambda r: r[0]):
        for g in G:
            mx, my = np.median([q[1] for q in g]), np.median([q[2] for q in g])
            if abs(r[1] - mx) <= near and abs(r[2] - my) <= near: g.append(r); break
        else: G.append([r])
    return G


def _ring_round(ring, t):
    import pcs_ring
    return pcs_ring.phase_at(ring, t)[0]


def care_packages(reads, g):
    """reads: [(t, care read)] of one game. -> games[].carePackages [{t, tLast, n, nSure, mapX, mapY, seenCount, styles,
    openedAt?, nextRing, inNextRing, nextRingDist}], first seen first."""
    ring = g.get('ring') or []
    w1 = next((r['endsAt'] for r in sorted(ring, key=lambda r: r.get('endsAt') or 0)
               if r.get('round') == 1 and r.get('kind') == 'wait' and r.get('endsAt') is not None), None)
    R = [(t, q['x'], q['y'], q) for t, q in reads if w1 is None or t >= w1 - EARLY_S]
    out = []
    for grp in _groups(R, NEAR_CARE):
        st = [q['style'] for _, _, _, q in grp]
        e = {'t': grp[0][0], 'tLast': grp[-1][0], 'mapX': round(float(np.median([r[1] for r in grp])), 2),
             'mapY': round(float(np.median([r[2] for r in grp])), 2), 'seenCount': len(grp),
             'score': round(float(np.median([q['score'] for _, _, _, q in grp])), 2),
             'styles': sorted(set(st), key=st.index)}
        op = [r[0] for r in grp if r[3]['style'] == 'opened']
        if op: e['openedAt'] = op[0]
        out.append(e)
    out.sort(key=lambda e: (e['t'], 'openedAt' not in e))      # first seen together: an opened one is older
    # numbers by timing: 1 and 2 first, then 3, then 4; a package first seen in round k can't be a later one
    used = 0
    for e in out:
        rnd = _ring_round(ring, e['t'])
        cap = LAST_ROUND.get(rnd, 4) if rnd else 4
        n = used + 1
        if n > 4 or n > cap: e['n'] = None; e['nSure'] = False; continue
        e['n'] = n; used = n
        # sure: first seen in its own announcement round (1-2 in round 1, 3 in round 2, 4 in round 3)
        e['nSure'] = rnd == {1: 1, 2: 1, 3: 2, 4: 3}[n]
    ends = {e['ring']: e for e in g.get('ringEnds') or [] if e.get('r')}
    for e in out:
        # inside each end circle that was read ({ring: bool}): the coach's rule is checked with the next ring,
        # the circle the ring was closing to when the package dropped is ring n - 1 (1 for packages 1-2)
        if ends:
            e['inRings'] = {str(k): bool(np.hypot(e['mapX'] - c['cx'], e['mapY'] - c['cy']) <= c['r'])
                            for k, c in sorted(ends.items())}
        if not e.get('n'): continue
        k = 2 if e['n'] <= 2 else e['n']
        e['nextRing'] = k
        c = ends.get(k)
        if c:
            d = float(np.hypot(e['mapX'] - c['cx'], e['mapY'] - c['cy']))
            e['nextRingDist'] = round(d / c['r'], 2)
            e['inNextRing'] = bool(d <= c['r'])
    return out


def strongholds(reads, g):
    """reads: [(t, stronghold read)] of one game. -> games[].strongholds [{t, tLast, mapX, mapY, seenCount, state,
    availableLast?, unavailableFirst?}] (state: as last seen; 'available' = red icon, 'unavailable' = dark icon).
"""
    out = []
    for grp in _groups([(t, q['x'], q['y'], q) for t, q in reads], NEAR_SH):
        e = {'t': grp[0][0], 'tLast': grp[-1][0], 'mapX': round(float(np.median([r[1] for r in grp])), 2),
             'mapY': round(float(np.median([r[2] for r in grp])), 2), 'seenCount': len(grp), 'state': grp[-1][3]['state']}
        av = [r[0] for r in grp if r[3]['state'] == 'available']; un = [r[0] for r in grp if r[3]['state'] == 'unavailable']
        if av: e['availableLast'] = av[-1]
        if un: e['unavailableFirst'] = un[0]
        if not av and len(un) < 2: continue          # a taken one seen once only: not enough
        out.append(e)
    return sorted(out, key=lambda e: e['t'])


def attach(games, reads):
    """reads: [(t, map, {'care', 'strongholds'})] full-map reads. Adds carePackages, strongholds and
    carePackageRingCheck ({checked, inside}: packages whose next ring was read (ring 2 for packages 1-2, n for 3-4)
    and how many lay inside it; {checkedClosing, insideClosing}: the same with the ring before it, the circle the ring
    was closing to when the package dropped)."""
    for i, g in enumerate(games):
        t0 = g['tStart'] - 120; t1 = games[i + 1]['tStart'] if i + 1 < len(games) else float('inf')
        R = [(t, r) for t, m, r in reads if t0 <= t < t1 and r and (g.get('map') in (None, '?') or m == g.get('map'))]
        if not R: continue
        g['carePackages'] = care_packages([(t, q) for t, r in R for q in r.get('care', [])], g)
        g['strongholds'] = strongholds([(t, q) for t, r in R for q in r.get('strongholds', [])], g)
        chk = [e for e in g['carePackages'] if 'inNextRing' in e]
        prev = [e for e in g['carePackages'] if e.get('nextRing') and str(e['nextRing'] - 1) in e.get('inRings', {})]
        if chk or prev:
            g['carePackageRingCheck'] = {'checked': len(chk), 'inside': sum(e['inNextRing'] for e in chk),
                                         'checkedClosing': len(prev),
                                         'insideClosing': sum(e['inRings'][str(e['nextRing'] - 1)] for e in prev)}
    return games
