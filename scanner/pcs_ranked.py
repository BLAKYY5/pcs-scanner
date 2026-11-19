"""Ranked: the RP the game shows during a Ranked game, and the rank badge of the players our squad kills.

What the screen shows (checked on the coach's Ranked recordings, Nov 2026):
- Kill feed (top right): a kill by our squad that gives RP is drawn as
      [killer's badge] killer  [weapon | Bleed Out | ability]  [victim's badge] victim  +N RP
  with "+N RP" in green at the right end. The same green "+N RP" ends two other lines: "Team Eliminated +N RP"
  (placement points as squads go out) and "All Previous Kill Points Increased +N RP" (earlier kills worth more).
  A kill line's number can grow while it is on screen (+8 -> +10 after such an increase): the last value counts.
  The badge in the feed shows the tier only (no division), at about 28 x 28 px.
- RP counter (top right, under the player's own rank emblem): the game's RP so far, "-48 RP" (the entry cost, red)
  at the start, rising with kills and placement ("+237 RP", white). It does not include what the match summary adds
  after the game (the win bonus: a won game showed +328 in game and +392 on the summary).

Reading:
- Digits are compared with shapes measured on our own recordings (pcs_ranked.json: 12 x 18 grey cells, each digit
  cut at its own contrast), the nearest shape must be close AND clearly closer than any other digit, and its width
  must fit; one doubtful digit = no number.
- Badges: the 28 x 28 shape of each tier seen so far (diamond, platinum, gold) is matched along the line; the victim's
  badge is the rightmost one followed by a white name (our own names are drawn in the squad colours), its tier = the
  closest shape, and the badge's colour must agree (blue / teal / gold). Tiers not seen yet in our recordings
  (rookie, bronze, silver, master, predator) are not read.
- Never one read: a value counts when read on two keyframes in a row (feed lines are seen on several keyframes as
  they scroll; the counter is read once per keyframe), and the RP counter never goes down inside a game.
Only 16:9 recordings (the boxes are at 2560 x 1440).
"""
import os, json
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    _T = json.load(open(os.path.join(HERE, 'pcs_ranked.json'), encoding='utf-8'))
except Exception:
    _T = {}

TIER_NAMES = {'rookie': 'Rookie', 'bronze': 'Bronze', 'silver': 'Silver', 'gold': 'Gold', 'platinum': 'Platinum',
              'diamond': 'Diamond', 'master': 'Master', 'predator': 'Predator'}
TIER_HUE = {'diamond': (195, 225), 'platinum': (150, 200), 'gold': (20, 60)}   # median hue of the badge's colour


def _hex(s):
    return np.array([int(c, 16) / 15.0 for c in s], np.float32)


def _z(a):
    a = a - a.mean(-1, keepdims=True)
    return a / (np.linalg.norm(a, axis=-1, keepdims=True) + 1e-6)


_LIN = {}
def _lin(n, m):
    """n x m matrix that resamples m values to n (linear, pixel centres aligned)."""
    if (n, m) not in _LIN:
        R = np.zeros((n, m), np.float32); x = (np.arange(n) + 0.5) * m / n - 0.5
        x0 = np.clip(np.floor(x).astype(int), 0, m - 1); x1 = np.clip(x0 + 1, 0, m - 1); f = np.clip(x - x0, 0, 1)
        np.add.at(R, (np.arange(n), x0), 1 - f); np.add.at(R, (np.arange(n), x1), f)
        _LIN[(n, m)] = R
    return _LIN[(n, m)]


def _cells(v, b):
    """Ink values v (2-D), box b = (x0, x1, y0, y1): the box at its own contrast as 12 x 18 cells, None if faint."""
    x0, x1, y0, y1 = b
    box = v[max(0, y0 - 2):y1 + 2, max(0, x0 - 2):x1 + 2]
    s = np.sort(box.ravel()); lo = s[int(.25 * (len(s) - 1))]; hi = s[int(.97 * (len(s) - 1))]
    if hi - lo < 40: return None
    g = np.clip((v[y0:y1, x0:x1] - lo) / (hi - lo), 0, 1)
    return (_lin(18, g.shape[0]) @ g @ _lin(12, g.shape[1]).T).ravel()


def _runs(m):
    """Runs of columns with ink -> [(x0, x1, y0, y1)] (end excluded)."""
    col = m.any(0); out = []; s = None
    for i, c in enumerate(list(col) + [False]):
        if c and s is None: s = i
        elif not c and s is not None:
            ys = np.nonzero(m[:, s:i].any(1))[0]; out.append((s, i, int(ys[0]), int(ys[-1]) + 1)); s = None
    return out


class _Digits:
    def __init__(self, rows, widths=None):
        self.rows = rows; self.labs = [r[0] for r in rows]
        self.M = _z(np.array([_hex(r[1]) for r in rows]))
        W = widths if widths is not None else [(r[2], r[3]) for r in rows]
        self.W = np.array(W, np.float32)
    def read(self, p, w, acc=0.35, margin=0.04):
        if p is None: return None
        d = 1 - _z(p) @ self.M.T + 1.0 * ((w < self.W[:, 0]) | (w > self.W[:, 1]))
        o = np.argsort(d)
        if d[o[0]] > acc or d[o[1]] - d[o[0]] < margin: return None
        return self.labs[o[0]]


FEED_D = _Digits(_T['feedDigits']) if _T.get('feedDigits') else None
_PW = [(r[1], r[2]) for r in _T.get('panelWidths', [])]
PANEL_D = {k: _Digits(_T['panelDigits'][k], _PW) for k in ('white', 'red')} if _T.get('panelDigits') else {}
BADGES = {}
for _n, _h in (_T.get('badges') or {}).items():
    _b = _hex(_h).reshape(28, 28); _b = _b - _b.mean(); BADGES[_n] = (_b / (np.linalg.norm(_b) + 1e-6)).astype(np.float32)


# ---------- "+N RP" at the end of a feed line ----------
def _green(a):
    a = a.astype(np.int16); r, g, b = a[..., 0], a[..., 1], a[..., 2]
    return (g > 140) & (g - r > 55) & (g - b > 55)


def _split_wide(m, D):
    """Two digits that touch: cut where the ink is thinnest."""
    out = []
    for b in D:
        if b[1] - b[0] >= 24:
            x = b[0] + 4 + int(m[b[2]:b[3], b[0] + 4:b[1] - 4].sum(0).argmin())
            for a, c in ((b[0], x), (x + 1, b[1])):
                ys = np.nonzero(m[:, a:c].any(1))[0]
                if len(ys): out.append((a, c, int(ys[0]), int(ys[-1]) + 1))
        else: out.append(b)
    return out


def feed_rp(band):
    """band: rgb of one feed line (about 20 rows above and below its text, right end included).
    -> N of a green '+N RP' at its right end, or None."""
    if FEED_D is None: return None
    m = _green(band)
    if m.sum() < 60: return None
    R = [r for r in _runs(m) if (r[1] - r[0]) * (r[3] - r[2]) >= 12]
    if len(R) < 3: return None
    p, q = R[-2], R[-1]                                   # 'R', 'P'
    if q[0] - p[1] > 4 or not all(17 <= r[3] - r[2] <= 24 for r in (p, q)): return None
    k = len(R) - 3                                        # the '+' and the digits: runs close together left of 'RP'
    if k < 0 or p[0] - R[k][1] < 6: return None
    j = k
    while j > 0 and R[j][0] - R[j - 1][1] <= 6: j -= 1
    x0, x1 = R[j][0], R[k][1]
    col = m[:, x0:x1].any(0)
    top = np.where(col, m[:, x0:x1].argmax(0), 99); bot = np.where(col, m.shape[0] - m[::-1, x0:x1].argmax(0), 0)
    x = 0
    while x < x1 - x0 and (not col[x] or bot[x] - top[x] <= 14): x += 1     # the '+': short columns
    if x < 6: return None
    D = [(x0 + x + a, x0 + x + b, c, d) for a, b, c, d in _runs(m[:, x0 + x:x1]) if (b - a) * (d - c) >= 12]
    D = _split_wide(m, D)
    if not D or len(D) > 3 or not all(18 <= b[3] - b[2] <= 23 for b in D): return None
    ink = (band[..., 1].astype(np.int16) - np.maximum(band[..., 0], band[..., 2]).astype(np.int16)).astype(np.float32)
    s = ''
    for b in D:
        c = FEED_D.read(_cells(ink, b), b[1] - b[0])
        if c is None: return None
        s += c
    return None if s[0] == '0' else int(s)


# ---------- the victim's rank badge ----------
def _ncc(g, T):
    th, tw = T.shape; H, W = g.shape
    if H < th or W < tw: return np.zeros((0, 0))
    g = g.astype(np.float64)
    num = np.fft.irfft2(np.fft.rfft2(g, (H, W)) * np.conj(np.fft.rfft2(T, (H, W))), (H, W))[:H - th + 1, :W - tw + 1]
    I = np.pad(g, ((1, 0), (1, 0))).cumsum(0).cumsum(1); I2 = np.pad(g * g, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    box = lambda A: A[th:, tw:] - A[:-th, tw:] - A[th:, :-tw] + A[:-th, :-tw]
    s = box(I); n = th * tw; var = box(I2) - s * s / n
    return np.where(var > n * 150, num / np.sqrt(np.maximum(var, 1e-6)), 0)    # flat areas never match


def _hue(p):
    mx = p.max(2); mn = p.min(2); sel = ((mx - mn) / (mx + 1e-3) > 0.35) & (mx > 80)
    if sel.sum() < 15: return None
    q = p[sel]; r, g, b = q[:, 0], q[:, 1], q[:, 2]; m = q.max(1); d = np.maximum(m - q.min(1), 1e-3)
    h = np.where(m == r, ((g - b) / d) % 6, np.where(m == g, (b - r) / d + 2, (r - g) / d + 4)) * 60
    return float(np.median(h))


def victim_badge(band, thr=0.6):
    """band: rgb of one feed line, its text centred in the rows. -> (x, y) of the victim's badge, or None.
    The victim's badge is the rightmost badge-shaped spot followed by a white name (our squad's names, after the
    killer's own badge, are in colour)."""
    if not BADGES: return None
    g = band.astype(np.float32).mean(2); H = band.shape[0]
    m = np.max([_ncc(g, T) for T in BADGES.values()], 0)
    if m.size == 0: return None
    cy = H // 2 - 14; rows = slice(max(0, cy - 4), min(m.shape[0], cy + 5)); mm = m[rows]
    if mm.size == 0: return None
    col = mm.max(0); xs = np.nonzero(col > thr)[0]
    if not len(xs): return None
    peaks = []; s = p = xs[0]
    for x in list(xs[1:]) + [10 ** 9]:
        if x - p > 3: peaks.append(s + int(col[s:p + 1].argmax())); s = x
        p = x
    for xb in reversed(peaks):
        yb = rows.start + int(mm[:, xb].argmax())
        a = band[yb + 4:yb + 24, xb + 30:xb + 90].astype(np.int16); mx = a.max(2); mn = a.min(2)
        white = int(((mn > 190) & (mx - mn < 50)).sum()); colr = int(((mx > 165) & ((mx - mn) > 0.38 * mx)).sum())
        if white >= 20 and white >= 2 * colr: return xb, yb
    return None


def victim_rank(band, loc=None):
    """-> 'diamond' / 'platinum' / 'gold' (the tiers whose badge we know), or None."""
    loc = loc or victim_badge(band)
    if not loc: return None
    x, y = loc; p = band[y:y + 28, x:x + 28].astype(np.float32)
    if p.shape[:2] != (28, 28): return None
    gz = p.mean(2); gz = gz - gz.mean(); gz /= np.linalg.norm(gz) + 1e-6
    sc = sorted(((float((gz * T).sum()), n) for n, T in BADGES.items()), reverse=True)
    if len(sc) > 1 and sc[0][0] - sc[1][0] < 0.05: return None
    h = _hue(p); lo, hi = TIER_HUE.get(sc[0][1], (0, -1))
    return sc[0][1] if h is not None and lo <= h <= hi else None


def last_twice(reads):
    """The last value read on two captures in a row (None reads break a pair) -> (value, times read) or (None, 0)."""
    v = None
    for a, b in zip(reads, reads[1:]):
        if a is not None and a == b: v = a
    return v, sum(1 for x in reads if x == v) if v is not None else 0


SQUAD_HUES = (('orange', 20, 58), ('green', 58, 105), ('teal', 150, 195), ('blue', 195, 250), ('purple', 250, 320))


def killer_hue(band, x_badge):
    """Median hue (degrees) of the squad-coloured name left of the victim's badge (our killer), or None."""
    a = band[12:30, :max(0, x_badge - 60)].astype(np.float32)
    if a.size == 0: return None
    mx = a.max(2); mn = a.min(2); sel = (mx > 165) & ((mx - mn) > 0.38 * mx) & ~((a[..., 0] > 170) & (a[..., 1] < 110))
    if sel.sum() < 40: return None
    q = a[sel]; r, g, b = q[:, 0], q[:, 1], q[:, 2]; m = q.max(1); d = np.maximum(m - q.min(1), 1e-3)
    return float(np.median(np.where(m == r, ((g - b) / d) % 6, np.where(m == g, (b - r) / d + 2, (r - g) / d + 4)) * 60))


def color_of(h):
    return None if h is None else next((n for n, lo, hi in SQUAD_HUES if lo <= h < hi), None)


def killer_color(band, x_badge):
    """The squad colour name of our killer's name ('orange', 'teal'...), or None."""
    return color_of(killer_hue(band, x_badge))


class Tracker:
    """Follows the kill feed's lines that end in a green '+N RP' from keyframe to keyframe and reads each capture.
    A line is the same line on the next keyframe (<= 2.6 s later) when its '+N RP' starts at the same place, it moved
    at most three places up or down (lines come and go above it), and its words look alike: captures go to the lines
    they look most like, one to one. finish() also joins two pieces of one line (never seen at the same moment, same
    words, within 12 s). finish() -> one entry per line:
    {'t' (first seen), 'y', 'rp', 'reads' (captures that gave that value),
     'kind': 'kill' (a victim's badge was read) | 'team' (Team Eliminated) | 'bonus' (All Previous Kill Points
     Increased) | 'unknown' (a kill whose badge was not read, or a line not told apart),
     'victimRank' (kill lines, when read), 'color' and 'hue' (our killer's squad colour and its hue in degrees, as
     the legend select screen's slot hues, when read)}."""
    X0, X1, Y0, Y1 = 1820, 2470, 260, 640
    DY, SAME, JOIN = 130, 0.12, 0.10     # largest move between keyframes (px); look-alike limits to follow / join a line
    def __init__(self):
        self.live = []; self.done = []; self.seen = []       # seen: keyframes with a green '+N RP' line
    def read(self, a, t):
        reg = a[self.Y0:self.Y1, self.X0:self.X1]
        sub = reg[:, 400:].astype(np.int16)
        gm = (sub[..., 1] > 150) & (sub[..., 1] - sub[..., 0] > 60) & (sub[..., 1] - sub[..., 2] > 60)
        ys = np.nonzero(gm.sum(1) >= 2)[0]
        grp = []
        if len(ys):
            s = p = ys[0]
            for y in list(ys[1:]) + [10 ** 6]:
                if y - p > 3: grp.append((s, p)); s = y
                p = y
        caps = []
        for s, e in grp:
            if not (14 <= e - s <= 26 and gm[s:e + 1].sum() >= 60) or s < 10 or e + 11 > reg.shape[0]: continue
            band = reg[s - 10:e + 11]
            g2 = _green(band); xs = np.nonzero(g2.sum(0) >= 2)[0]
            if not len(xs) or not 556 <= xs[-1] <= 566: continue          # right-aligned at the feed's edge
            f = band.astype(np.int16); mn = f.min(2); mx = f.max(2)
            wm = ((mn > 200) & (mx - mn < 40)) | g2
            sig = _shrink(wm[:, 200:570])
            caps.append({'t': round(t, 2), 'y': self.Y0 + int(s), 'x0': int(xs[0]), 'sig': sig, 'band': band,
                         'what': _what(((mn > 200) & (mx - mn < 40))[8:-8], int(xs[0]))})
        if caps: self.seen.append(round(t, 2))
        pairs = []                       # each capture to the line it continues: closest looks first, one to one
        for ci, c in enumerate(caps):
            for L in self.live:
                p = L['last']
                if 0 < c['t'] - p['t'] <= 2.6 and abs(c['x0'] - p['x0']) <= 3 and abs(c['y'] - p['y']) <= self.DY:
                    d = float((c['sig'] ^ p['sig']).mean())
                    if d < self.SAME: pairs.append((d, ci, id(L), L))
        to = {}; used = set()
        for d, ci, lid, L in sorted(pairs, key=lambda x: x[:3]):
            if ci not in to and lid not in used: to[ci] = L; used.add(lid)
        for ci, c in enumerate(caps):
            L = to.get(ci)
            if L is None: L = {'t': c['t'], 'y': c['y'], 'x0': c['x0'], 'rp': [], 'rank': [], 'color': [], 'what': [], 'sig0': c['sig']}; self.live.append(L)
            L['what'].append(c['what']); L.setdefault('ts', []).append(c['t'])
            rp = feed_rp(c['band']); rk = None; col = None
            if rp is not None:
                loc = victim_badge(c['band'])
                if loc: rk = victim_rank(c['band'], loc); col = killer_hue(c['band'], loc[0])
            L['rp'].append(rp); L['rank'].append(rk); L['color'].append(col); L['last'] = {k: c[k] for k in ('t', 'y', 'x0', 'sig')}
        old = [L for L in self.live if t - L['last']['t'] > 2.6]
        self.live = [L for L in self.live if t - L['last']['t'] <= 2.6]
        self.done += old
    def finish(self):
        out = []; Ls = sorted(self.done + self.live, key=lambda L: L['t'])
        for i, L in enumerate(Ls):      # one line followed as two (a bright frame, a jump of two places): never seen
            if L.get('merged'): continue  # at the same moment, same words, within 12 s = one line, reads in time order
            for M in Ls[i + 1:]:
                if M['t'] - L['ts'][-1] > 12: break
                if M.get('merged') or set(M['ts']) & set(L['ts']): continue
                same = abs(M['x0'] - L['x0']) <= 3 and float((M['sig0'] ^ L['sig0']).mean()) < self.JOIN
                # a kill line whose number grew (+8 -> +10: wider, so it looks like a new line): the same victim's
                # badge and our same killer, taking over within 3 s where the old one stopped
                grown = (0 < M['ts'][0] - L['ts'][-1] <= 3 and abs(M['y'] - L['last']['y']) <= 50 and
                         last_twice(L['rp'])[0] is not None and (last_twice(M['rp'])[0] or 0) > last_twice(L['rp'])[0] and
                         _one(L['rank']) and _one(L['rank']) == _one(M['rank']) and
                         _one([color_of(h) for h in L['color'] if h is not None]) == _one([color_of(h) for h in M['color'] if h is not None]))
                # a line lost for a keyframe (bright background): the same number and killer colour going on within
                # 3 s, at most one place from where it was, with words that look alike (two kills by one player with
                # the same RP can follow each other: egoyebli then [HARU] Reders, +9 each, 18 Nov).
                lc = _one([color_of(h) for h in L['color'] if h is not None]); mc = _one([color_of(h) for h in M['color'] if h is not None])
                twin = (0 < M['ts'][0] - L['ts'][-1] <= 3 and abs(M['y'] - L['last']['y']) <= 50 and
                        _one(L['rp']) is not None and _one(L['rp']) == _one(M['rp']) and lc == mc and
                        float((M['sig0'] ^ L['last']['sig']).mean()) < 0.15)
                if same or grown or twin:
                    rows = sorted(zip(L['ts'] + M['ts'], L['rp'] + M['rp'], L['rank'] + M['rank'], L['color'] + M['color'], L['what'] + M['what']), key=lambda r: r[0])
                    L['ts'], L['rp'], L['rank'], L['color'], L['what'] = (list(x) for x in zip(*rows))
                    if M['last']['t'] > L['last']['t']: L['last'] = M['last']
                    M['merged'] = True
        for L in Ls:
            if L.get('merged'): continue
            rp, n = last_twice(L['rp'])
            if rp is None: continue
            rk, _ = last_twice(L['rank'])
            what, _ = last_twice(L['what'])
            e = {'t': L['t'], 'y': L['y'], 'rp': rp, 'reads': n,
                 'kind': 'kill' if rk or any(L['rank']) else (what or 'unknown')}
            if rk: e['victimRank'] = rk
            hs = [h for h in L['color'] if h is not None]
            if hs:
                names = [color_of(h) for h in hs]; name = max(set(names), key=names.count)
                if name: e['color'] = name; e['hue'] = round(float(np.median([h for h, n in zip(hs, names) if n == name])))
            out.append(e)
        self.done = []; self.live = []
        return out


def _one(xs):
    """The value read most among the reads that gave one (None if none)."""
    v = [x for x in xs if x]
    return max(set(v), key=v.count) if v else None


def _what(white, x0):
    """The white words left of '+N RP' as one block: about 193 px wide = 'Team Eliminated' ('team'), about 383 px =
    'All Previous Kill Points Increased' ('bonus'); anything else None (a kill line, or not clear)."""
    cols = np.nonzero(white.sum(0) >= 2)[0]; xl = x0
    for v in reversed(cols[cols < x0]):
        if xl - v <= 14: xl = v
        else: break
    w = x0 - xl
    return 'team' if 185 <= w <= 200 else 'bonus' if 372 <= w <= 398 else None


def _shrink(m):
    """A text mask as 96 x 6 cells (mean > 0.2): the line's look, to know it again on the next keyframe."""
    h, w = m.shape
    r = _lin(6, h) @ m.astype(np.float32) @ _lin(96, w).T
    return r > 0.2


# ---------- the RP counter (top right) ----------
P_Y0, P_Y1, P_X0, P_X1 = 146, 164, 2360, 2560      # text rows of the counter; the box


def panel_rp(a):
    """a: full 16:9 frame -> the game's RP shown under the rank emblem ('-48 RP' red, '+237 RP' white), or None."""
    if not PANEL_D: return None
    crop = a[P_Y0 - 8:P_Y1 + 8, P_X0:P_X1].astype(np.int16)
    r, g, b = crop[..., 0], crop[..., 1], crop[..., 2]
    for red, m, key in ((False, crop.min(2) > 150, 'white'), (True, (r > 140) & (r - np.maximum(g, b) > 70), 'red')):
        band = m[8:-8]; above = m[2:7].any(0); below = m[-7:-2].any(0)
        R = []
        for s, e, y0, y1 in _runs(band):
            if e - s <= 4 and above[s:e].any() and below[s:e].any(): continue      # the box's border
            if (e - s) * (y1 - y0) < 6: continue
            R.append((s, e, y0 + 8, y1 + 8))
        if len(R) < 4: continue
        p, q = R[-2], R[-1]
        if q[0] - p[1] > 4 or not all(17 <= x[3] - x[2] <= 20 for x in (p, q)): continue
        h = R[0][3] - R[0][2]
        sign = '-' if h <= 5 else '+' if 8 <= h <= 14 else None
        if sign is None or (sign == '-') != red: continue      # negative = red, positive = white
        D = R[1:-2]
        if not D or len(D) > 3 or p[0] - D[-1][1] < 5: continue
        D2 = []
        for d in D:
            if d[1] - d[0] >= 19: c = (d[0] + d[1]) // 2; D2 += [(d[0], c, d[2], d[3]), (c, d[1], d[2], d[3])]
            else: D2.append(d)
        if len(D2) > 3: return None
        ink = (r - np.maximum(g, b)).astype(np.float32) if red else crop.min(2).astype(np.float32)
        s = ''
        for d in D2:
            c = PANEL_D[key].read(_cells(ink, d), d[1] - d[0], 0.35, 0.06)
            if c is None: return None
            s += c
        if len(s) > 1 and s[0] == '0': return None
        return int(s) * (-1 if red else 1)
    return None


def game_rp(reads, t0, t1, seen=None):
    """reads: [(t, value)] of the RP counter; seen: times of keyframes with a green '+N RP' line in the feed.
    -> {'start', 'last', 'lastAt', 'reads'} for the game [t0, t1), or None.
    A value counts when read on two keyframes in a row. The counter only moves when RP is earned, and every RP earned
    in a game is announced by a green feed line, so a new value counts only when such a line was seen from 20 s
    before to 5 s after it (a red 8 misread as 6, -48 -> -46, has none), and never when lower than the value before.
    'start' = the value read most before the first green line (the entry cost)."""
    R = [(t, v) for t, v in reads if t0 <= t < t1]
    ok = [(tb, a) for (ta, a), (tb, b) in zip(R, R[1:]) if a == b]
    if not ok: return None
    S = sorted(x for x in (seen or []) if t0 - 10 <= x < t1)
    first = S[0] if S else float('inf')
    pre = [v for t, v in ok if t < first] or [v for t, v in ok]
    cur = max(set(pre), key=pre.count); at = None
    for t, v in ok:
        if v == cur:
            if at is None or t >= first: at = t
            continue
        if v < cur: continue
        if seen is not None and not any(t - 20 <= x <= t + 5 for x in S): continue
        cur, at = v, t
    if at is None: at = ok[-1][0]
    return {'start': max(set(pre), key=pre.count), 'last': cur, 'lastAt': round(at, 2), 'reads': len(R)}


def game_feed(lines, t0, t1):
    """The Tracker's lines of one game -> {'kills': [{t, rp, victimRank?, color?, hue?}] (our kills that gave RP, as the
    line showed it last), 'killRp' (their sum), 'lines' (green lines read), 'unknownLines' (kills whose badge was not
    read, or lines not told apart)} or None. Not a game total: a kill line's number grows when 'All Previous Kill
    Points Increased' shows, so lines overlap; the game's total is the RP counter (game_rp)."""
    L = [l for l in lines if t0 <= l['t'] < t1 and l.get('rp') is not None]
    if not L: return None
    kills = [{k: l[k] for k in ('t', 'rp', 'victimRank', 'color', 'hue') if k in l} for l in L if l.get('kind') == 'kill']
    return {'kills': kills, 'killRp': sum(k['rp'] for k in kills), 'lines': len(L),
            'unknownLines': sum(1 for l in L if l.get('kind') == 'unknown')}
