"""Ring circles on the full map (M key): the current ring and the next ring, as centre + radius in map %.

What the full map draws (Season 31, checked on our recordings): the area outside the current ring has a red tint, with
a dark line on the ring itself; the next ring is a see-through white disc (everything under it brighter by the same
fraction of the way to white). Both circles are found the same way:
  1. edges that look like the circle's edge: for the current ring, edges of the "untinted" measure; for the next
     ring, edges where the bright side is the dark side moved towards white by one fraction alike in red, green and
     blue, over a wide step (map art, labels and icons fail this test: coloured, or thin);
  2. each edge votes along its gradient for the centre it points to, then the radius (a two-step Hough transform);
  3. a local search keeps the circle with the most of its visible arc showing the right contrast.
A circle counts only when that holds on most of its visible arc (support), so a ring partly off the map or under
icons is still read, and lookalike discs of other map features (coloured overlays) are refused.

Coordinates: the scanner's map % (pcs_maps.json space) through the map frame's own transform M (screen px -> map %),
so circles line up with the harvesters, the landing and the drop spots. World units: map % x the map's scale in
pcs_hud.CAL (world units per 1 % of the map).
"""
import numpy as np
from scipy import ndimage

# the unzoomed full map at 2560 x 1440: a square of about 1353 px from x = 604, y = -4 (MapReader's M0)
MAP_X0, MAP_X1, MAP_Y1 = 604, 1957, 1340
DS = 2                    # work at half size (circles are hundreds of px: no precision lost, 4x less work)
UNZOOMED = 0.0739         # map % per screen px of the unzoomed full map (MapReader M0); zoomed maps are skipped
M_DEFAULT = np.array([[UNZOOMED, 0], [0, UNZOOMED], [-44.64, 0.27]])
ANG = np.linspace(0, 2 * np.pi, 180, endpoint=False)


def half(a):
    """Map area at half size, float32 RGB, lightly smoothed."""
    c = a[0:MAP_Y1, MAP_X0:MAP_X1].astype(np.float32)
    h, w = (c.shape[0] // DS) * DS, (c.shape[1] // DS) * DS
    c = c[:h, :w].reshape(h // DS, DS, w // DS, DS, 3).mean((1, 3))
    return ndimage.gaussian_filter(c, (1.5, 1.5, 0))


def tint(c):
    """High inside the current ring (no red tint), low outside."""
    return np.log((c[..., 1] + c[..., 2]) / 2 + 12) - np.log(c[..., 0] + 12)


def _samp(img, x, y):
    if img.ndim == 2: return ndimage.map_coordinates(img, [y, x], order=1)
    return np.stack([ndimage.map_coordinates(img[..., k], [y, x], order=1) for k in range(img.shape[2])], -1)


def _grad(F, thr):
    gx = ndimage.sobel(F, 1); gy = ndimage.sobel(F, 0); mag = np.hypot(gx, gy)
    ys, xs = np.nonzero(mag > thr)
    m = mag[ys, xs]
    return xs.astype(np.float32), ys.astype(np.float32), gx[ys, xs] / m, gy[ys, xs] / m, m


def _alpha(c, xi, yi, xo, yo):
    """How far the inside colour is moved towards white from the outside colour, per channel."""
    ci, co = _samp(c, xi, yi), _samp(c, xo, yo)
    return (ci - co) / np.maximum(255 - co, 20)


def _white_ok(al, am=None):
    am = al.mean(-1) if am is None else am
    return (al.min(-1) > 0.035) & (al.max(-1) < 0.32) & (al.max(-1) - al.min(-1) < 0.5 * am)


def edges_cur(c, keep=15000):
    T = tint(c)
    xs, ys, ux, uy, m = _grad(T, 0.0)
    thr = max(np.percentile(m, 96), 0.25 * np.percentile(m, 99.9))
    k = m > thr
    xs, ys, ux, uy, m = xs[k], ys[k], ux[k], uy[k], m[k]
    if len(xs) > keep:
        k = np.argsort(m)[-keep:]; xs, ys, ux, uy = xs[k], ys[k], ux[k], uy[k]
    return xs, ys, ux, uy


def edges_next(c, keep=20000):
    """Edges of a white see-through overlay: a step of one consistent whitening fraction, wide (still there 7 and
    12 px away, half size), not a thin line or an icon."""
    xs, ys, ux, uy, m = _grad(c.mean(2), 8.0)
    a3 = _alpha(c, xs + 3 * ux, ys + 3 * uy, xs - 3 * ux, ys - 3 * uy); am = a3.mean(1)
    ok = _white_ok(a3, am)
    for d in (7, 12):
        al = _alpha(c, xs + d * ux, ys + d * uy, xs - d * ux, ys - d * uy)
        ok &= (al.min(1) > 0.6 * am) & (al.max(1) < 1.6 * am + 0.03)
    xs, ys, ux, uy = xs[ok], ys[ok], ux[ok], uy[ok]
    if len(xs) > keep:
        k = np.random.default_rng(0).choice(len(xs), keep, replace=False); xs, ys, ux, uy = xs[k], ys[k], ux[k], uy[k]
    return xs, ys, ux, uy


def _hough(xs, ys, ux, uy, shape, rmin, rmax, pad, step=2, cb=3, n=6):
    """Centre votes (each edge votes along its gradient for every radius), then the best centres."""
    h, w = shape
    H = (h + 2 * pad) // cb + 1; Wd = (w + 2 * pad) // cb + 1
    acc = np.zeros(H * Wd, np.float32)
    for r in range(rmin, rmax + 1, step):
        cx = ((xs + r * ux + pad) / cb).astype(np.int64); cy = ((ys + r * uy + pad) / cb).astype(np.int64)
        ok = (cx >= 0) & (cx < Wd) & (cy >= 0) & (cy < H)
        acc += np.bincount(cy[ok] * Wd + cx[ok], minlength=H * Wd).astype(np.float32)
    acc = ndimage.gaussian_filter(acc.reshape(H, Wd), 1.0)
    mx = ndimage.maximum_filter(acc, size=9)
    pk = np.argwhere((acc == mx) & (acc > 0))
    pk = sorted(pk, key=lambda p: -acc[p[0], p[1]])[:n]
    return [((p[1] * cb + cb / 2 - pad), (p[0] * cb + cb / 2 - pad)) for p in pk]


def _radius(xs, ys, ux, uy, cx, cy, rmin, rmax, tol=0.94, n=3):
    """Radius histogram of the edges whose gradient points to (cx, cy): best radii."""
    dx, dy = cx - xs, cy - ys; d = np.hypot(dx, dy) + 1e-6
    ok = (dx * ux + dy * uy) / d > tol
    h = np.bincount(np.clip(d[ok], 0, rmax + 2).astype(int), minlength=rmax + 3)[:rmax + 3].astype(np.float32)
    h[:rmin] = 0
    hs = ndimage.uniform_filter1d(h, 5) / np.sqrt(np.arange(len(h)) + 20)    # big circles collect more edges
    return [int(i) for i in np.argsort(hs)[::-1][:n] if hs[i] > 0]


def _arc(shape, cx, cy, r, d):
    h, w = shape
    ca, sa = np.cos(ANG), np.sin(ANG)
    xi, yi, xo, yo = cx + (r - d) * ca, cy + (r - d) * sa, cx + (r + d) * ca, cy + (r + d) * sa
    vis = (np.minimum(xi, xo) >= 1) & (np.maximum(xi, xo) < w - 2) & (np.minimum(yi, yo) >= 1) & (np.maximum(yi, yo) < h - 2)
    return xi[vis], yi[vis], xo[vis], yo[vis], float(vis.mean())


def measure_cur(c, T, cx, cy, r, d=4):
    """Across the visible arc: (median contrast, support = fraction where the inside is clearly less red-tinted than
    the outside, visible fraction, samples)."""
    xi, yi, xo, yo, vis = _arc(T.shape, cx, cy, r, d)
    if len(xi) < 12: return 0.0, 0.0, vis, len(xi)
    v = _samp(T, xi, yi) - _samp(T, xo, yo)
    return float(np.median(v)), float((v > 0.08).mean()), vis, len(xi)


def measure_next(c, T, cx, cy, r, d=3):
    """Across the visible arc: (median whitening fraction, support = fraction where the inside is the outside moved
    towards white by that same fraction in R, G and B, visible fraction, samples). The overlay has one fraction."""
    xi, yi, xo, yo, vis = _arc(c.shape[:2], cx, cy, r, d)
    if len(xi) < 12: return 0.0, 0.0, vis, len(xi)
    al = _alpha(c, xi, yi, xo, yo)
    a0 = float(np.median(al.mean(1)))
    if not 0.09 < a0 < 0.24: return a0, 0.0, vis, len(xi)      # the overlay's fraction: 0.14..0.17 measured
    am = al.mean(1)
    return a0, float(((am > 0.4 * a0) & (am < 2 * a0) & (al.min(1) > 0.25 * a0)).mean()), vis, len(xi)


def _refine(measure, c, T, cx, cy, r):
    """Local search on the median contrast: first across a wide band (finds the edge from a rough Hough guess),
    then a narrow one (sits on it)."""
    for d, steps in ((10, (8, 4)), (3, (2, 1, 0.5))):
        def f(x, y, rr):
            m = measure(c, T, x, y, rr, d)
            return m[0] * min(1.0, m[3] / 45.0)
        best = (f(cx, cy, r), cx, cy, r)
        for stp in steps:
            moved = True
            while moved:
                moved = False
                _, bx, by, br = best
                for dx, dy, dr in ((stp, 0, 0), (-stp, 0, 0), (0, stp, 0), (0, -stp, 0), (0, 0, stp), (0, 0, -stp)):
                    if br + dr < 8: continue
                    s = f(bx + dx, by + dy, br + dr)
                    if s > best[0] + 1e-6: best = (s, bx + dx, by + dy, br + dr); moved = True
        _, cx, cy, r = best
    return cx, cy, r


def find_circle(edges, measure, c, T, rmin, rmax, pad, inside=None):
    """Best circle from an edge set and a measure. inside: (cx, cy, r) the circle must lie in."""
    xs, ys, ux, uy = edges
    if inside is not None:      # only edges inside the outer circle can belong to the inner one
        k = np.hypot(xs - inside[0], ys - inside[1]) < inside[2] - 2
        xs, ys, ux, uy = xs[k], ys[k], ux[k], uy[k]
    if len(xs) < 40: return None
    best = None; seen = []
    for cx, cy in _hough(xs, ys, ux, uy, T.shape, rmin, rmax, pad):
        for r in _radius(xs, ys, ux, uy, cx, cy, rmin, rmax):
            if any(abs(cx - a) + abs(cy - b) + abs(r - q) < 6 for a, b, q in seen): continue
            seen.append((cx, cy, r))
            cx2, cy2, r2 = _refine(measure, c, T, cx, cy, r)
            if inside is not None and np.hypot(cx2 - inside[0], cy2 - inside[1]) + r2 > inside[2] * 1.03 + 4: continue
            con, sup, vis, n = measure(c, T, cx2, cy2, r2)
            s = sup * min(1.0, n / 45.0)
            if best is None or s > best[0]: best = (s, cx2, cy2, r2, sup, vis)
    if best is None: return None
    return {'cx': best[1], 'cy': best[2], 'r': best[3], 'support': best[4], 'visible': best[5]}


MIN_SUPPORT = 0.6


def read(a, M=None):
    """Ring circles on one full-map frame (2560 x 1440 RGB, 16:9 layout or rebuilt by the scanner's view_map).
    M: the map transform found by MapReader (screen px -> map %); None = the unzoomed default.
    Returns {'cur': circle or None, 'next': circle or None} (circle: cx, cy, r in map %, support, visible, px), or None
    when the map is zoomed."""
    M = M_DEFAULT if M is None else np.asarray(M)
    if abs(M[0, 0] - UNZOOMED) > 0.03 * UNZOOMED: return None      # zoomed map: circles cut off, skip
    c = half(a); T = tint(c)
    cur = find_circle(edges_cur(c), measure_cur, c, T, 40, 560, 480)
    if not (cur and cur['support'] >= MIN_SUPPORT and cur['visible'] * 2 * np.pi * cur['r'] >= 250): cur = None
    inside = (cur['cx'], cur['cy'], cur['r']) if cur else None
    nxt = find_circle(edges_next(c), measure_next, c, T, 15, 520, 420, inside)
    if not (nxt and nxt['support'] >= MIN_SUPPORT and nxt['visible'] * 2 * np.pi * nxt['r'] >= 150): nxt = None

    def to_map(d):
        if d is None: return None
        px, py = MAP_X0 + d['cx'] * DS + (DS - 1) / 2, d['cy'] * DS + (DS - 1) / 2
        X, Y = np.array([px, py, 1.0]) @ M
        return {'cx': round(float(X), 2), 'cy': round(float(Y), 2), 'r': round(float(d['r'] * DS * M[0, 0]), 2),
                'support': round(d['support'], 2), 'visible': round(d['visible'], 2),
                'px': [round(float(px), 1), round(float(py), 1), round(float(d['r'] * DS), 1)]}
    return {'cur': to_map(cur), 'next': to_map(nxt)}


# ---------- per game: circles per ring stage, ring ends, closing speed ----------
def units_per_pct(mp):
    """World units per 1 % of the map (pcs_hud.CAL: mean of the x and y scales), or None for a map without one."""
    import pcs_hud
    c = pcs_hud.CAL.get(mp)
    return (c[0] + c[2]) / 2 if c else None


def phase_at(ring, t):
    """(round, kind) of the ring phase running at time t, from the game's ring phases (pcs_hud.number_rounds):
    the phase with the earliest countdown end at or after t. (None, None) when unknown."""
    P = sorted([r for r in ring or [] if r.get('round') and r.get('endsAt') is not None and r.get('kind')],
               key=lambda r: r['endsAt'])
    for r in P:
        if r['endsAt'] >= t - 0.5:
            # a phase lasts at most ~ 300 s: further ahead means phases in between were not seen
            return (r['round'], r['kind']) if r['endsAt'] - t <= 320 else (None, None)
    return None, None


def _agree(C, tol=0.6):
    """The circles (cx, cy, r in map %) that agree with their median within tol % : (median circle, n agreeing)."""
    A = np.array(C)
    m = np.median(A, 0)
    ok = np.abs(A - m).max(1) <= tol
    if ok.sum() >= 1: m = np.median(A[ok], 0)
    return [round(float(v), 2) for v in m], int(ok.sum())


def game_circles(reads, ring, mp, t0, t1):
    """reads: [(t, map, {'cur', 'next'})] map-frame reads. Per (round, kind) group inside [t0, t1): the current circle
    and the next ring (median of the reads that agree), conf = share of the group's reads that agree."""
    groups = {}
    for t, m, r in reads:
        if not (t0 <= t < t1) or not r or (mp not in (None, '?') and m != mp): continue
        k = phase_at(ring, t)
        groups.setdefault(k, []).append((t, r))
    out = []
    for (rnd, kind), L in sorted(groups.items(), key=lambda kv: kv[1][0][0]):
        e = {'stage': rnd, 'phase': kind, 't': L[0][0], 'tLast': L[-1][0], 'reads': len(L)}
        cur = [(x['cur']['cx'], x['cur']['cy'], x['cur']['r']) for _, x in L if x.get('cur')]
        nxt = [(x['next']['cx'], x['next']['cy'], x['next']['r']) for _, x in L if x.get('next')]
        conf = []
        if cur and kind != 'closing':       # while closing the current circle moves: kept as a single read only
            (e['cx'], e['cy'], e['r']), n = _agree(cur); e['curReads'] = len(cur); conf.append(n / len(L))
        elif cur:
            t_, x = next((t_, x) for t_, x in L if x.get('cur'))
            e.update(cx=x['cur']['cx'], cy=x['cur']['cy'], r=x['cur']['r'], curAt=t_, curReads=len(cur)); conf.append(len(cur) / len(L))
        if nxt:
            (cx, cy, r), n = _agree(nxt); e['next'] = {'cx': cx, 'cy': cy, 'r': r, 'reads': len(nxt)}; conf.append(n / len(L))
        if not cur and not nxt: continue
        if e.get('r') is not None:      # the shape the review player's map reads ({t, mapX, mapY, mapR}, round)
            e['mapX'], e['mapY'], e['mapR'], e['round'] = e['cx'], e['cy'], e['r'], rnd
        e['conf'] = round(float(np.mean(conf)), 2)
        out.append(e)
    return out


def ring_ends(circles, mp):
    """End circle of each ring stage k: the next ring shown during round k, or the current circle during the wait of
    round k + 1 (both when seen: they must agree). rUnits = radius in world units (map scale from pcs_hud.CAL)."""
    k_u = units_per_pct(mp)
    cand = {}
    for e in circles:
        if e['stage'] is None: continue
        if e.get('next'): cand.setdefault(e['stage'], []).append((e['next']['cx'], e['next']['cy'], e['next']['r'], 'next', e['next']['reads']))
        if e.get('r') is not None and e['phase'] == 'wait' and e['stage'] >= 2:
            cand.setdefault(e['stage'] - 1, []).append((e['cx'], e['cy'], e['r'], 'current', e.get('curReads', 1)))
    out = []
    for k in sorted(cand):
        C = cand[k]
        (cx, cy, r), n = _agree([c[:3] for c in C])
        d = {'ring': k, 'cx': cx, 'cy': cy, 'r': r, 'from': sorted(set(c[3] for c in C)), 'reads': sum(c[4] for c in C),
             'agree': n == len(C)}
        if k_u: d['rUnits'] = int(round(r * k_u))
        out.append(d)
    return out


def closing_speed(ends, ring):
    """Radius drop / closing time for each stage whose own end and the previous end are both read:
    [{ring, drop (units), seconds, unitsPerS}]. Closing time = the round's closing countdown end - its wait end."""
    E = {e['ring']: e for e in ends if e.get('rUnits')}
    W = {r['round']: r['endsAt'] for r in ring or [] if r.get('kind') == 'wait' and r.get('round') and not r.get('fromSpectate')}
    C = {r['round']: r['endsAt'] for r in ring or [] if r.get('kind') == 'closing' and r.get('round') and not r.get('fromSpectate')}
    out = []
    for k in sorted(E):
        if k - 1 in E and k in W and k in C and C[k] > W[k]:
            drop = E[k - 1]['rUnits'] - E[k]['rUnits']; secs = C[k] - W[k]
            out.append({'ring': k, 'drop': drop, 'seconds': round(secs, 1), 'unitsPerS': round(drop / secs, 1)})
    return out


def attach(games, reads):
    """Adds ringCircles (per stage and phase), ringEnds (end circle per ring) and ringSpeed to each game."""
    for i, g in enumerate(games):
        t1 = games[i + 1]['tStart'] if i + 1 < len(games) else float('inf')
        circles = game_circles(reads, g.get('ring'), g.get('map'), g['tStart'] - 120, t1)
        if not circles: continue
        g['ringCircles'] = circles
        g['ringEnds'] = ring_ends(circles, g.get('map'))
        sp = closing_speed(g['ringEnds'], g.get('ring'))
        if sp: g['ringSpeed'] = sp
    return games
