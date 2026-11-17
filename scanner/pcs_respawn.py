"""Respawns (and the recording player's own deaths) from the HUD.

Teammates: the squad cards at the bottom left. Each teammate card shows the player's name in white at x = 184 (2560 x
1440), and under it either the health / shield bar (alive), a red bleed-out bar (knocked), or, once the player is dead,
a status line in lime letters ("DEATHBOX RECOVERY", "DELIVER TO BEACON", "AWAITING RESPAWN": the banner's way back),
with a lime frame. A teammate whose card goes from that dead line back to a bar, while the game goes on, was respawned
(respawn beacon, mobile respawn beacon or the mode's own respawn: the card does not say which).
The recording player: dead = the HUD moves into the spectator layout (the ring clock 85 px lower, pcs_hud.SPEC_OFF);
back in his own view later in the same game = respawned (a fall from the respawn dropship follows in the debug overlay).

Cards are read on own-view frames only: while spectating, the cards are the spectated squad's.
"""
import numpy as np

NAME_X0, NAME_X1 = 180, 340          # names start at x = 184 (all layouts: normal, map open, spectating)
STAT_X1 = 430                        # the lime status line ends before the card's right edge (x ~ 400)
Y0, Y1 = 860, 1330                   # the teammate cards' rows in every layout (spectator layout is 85 px higher)


def _runs(v, lo):
    """[(start, end)] of consecutive True rows."""
    out = []; st = None
    for i, x in enumerate(list(v) + [False]):
        if x and st is None: st = i
        elif not x and st is not None: out.append((st, i)); st = None
    return [(a, b) for a, b in out if b - a >= lo]


def _trans(m):
    """Colour changes along each row (text has many, a bar a few)."""
    return np.abs(np.diff(m.astype(np.int8), axis=1)).sum(1)


def masks(a):
    """White (names, bars), lime (dead status line) over the card column."""
    c = a[Y0:Y1, NAME_X0:STAT_X1]
    r, g, b = (c[..., k].astype(np.int16) for k in range(3))
    mn = np.minimum(np.minimum(r, g), b); mx = np.maximum(np.maximum(r, g), b)
    white = (mn > 185) & (mx - mn < 45)
    lime = (g > 150) & (g - b > 70) & (g >= r) & (r > 90)
    return white, lime


def _left(m):
    return np.where(m.any(1), m.argmax(1), 999)


def read_cards(a):
    """Teammate cards on one frame: [{'y': card row, 'state': 'alive' | 'dead', 'hue': frame hue}] top to bottom.
    A card is found by what sits under the player's name (names can be one letter long): the health bar (white, solid,
    starting at the name column x 183..192) = alive, or the lime status line starting there = dead. 'y' = that line's
    top row - 30 (about the name's top row). Empty when no card shows (menus, inventory, death screens)."""
    white, lime = masks(a)
    cards = []
    # dead: lime text rows starting near the name column
    ll = _left(lime)
    lrow = (lime.sum(1) >= 20) & (_trans(lime) >= 6) & (ll <= 22)
    for y0, y1 in _runs(lrow, 8):
        if y1 - y0 <= 26: cards.append((y0 - 27, 'dead'))
    # alive: a solid white bar from the name column (a knocked player's bar is red, a low one may be too short)
    wl = _left(white)
    run = np.array([(np.argmin(white[y, wl[y]:]) if not white[y, wl[y]:].all() else white.shape[1] - wl[y])
                    if wl[y] < 999 else 0 for y in range(white.shape[0])])
    brow = (wl >= 3) & (wl <= 12) & (run >= 8) & (_trans(white) <= 3)
    for y0, y1 in _runs(brow, 4):
        if y1 - y0 <= 14 and not any(abs((y0 - 33) - y) < 14 for y, _ in cards): cards.append((y0 - 33, 'alive'))
    return [{'y': int(Y0 + y), 'state': s, 'hue': frame_hue(a, Y0 + y)} for y, s in sorted(cards)]


def frame_hue(a, y):
    """Hue (degrees) of the card's coloured left frame next to the portrait, or None."""
    c = a[max(0, y - 14):y + 40, 60:120].astype(np.float32) / 255
    mx = c.max(2); mn = c.min(2); s = (mx - mn) / np.maximum(mx, 1e-3)
    k = (s > 0.5) & (mx > 0.45)
    if k.sum() < 25: return None
    p = c[k]; r, g, b = p[:, 0], p[:, 1], p[:, 2]
    h = np.degrees(np.arctan2(np.sqrt(3) * (g - b), 2 * r - g - b)) % 360
    return int(np.median(h))


HUES = (('orange', 15, 55), ('green', 55, 110), ('teal', 150, 200), ('blue', 200, 250), ('purple', 250, 320))


def hue_name(h):
    if h is None: return None
    return next((n for n, a, b in HUES if a <= h < b), None)


def teammate_respawns(reads, t0, t1, hold=3):
    """reads: [(t, cards)] own-view frames, time order. A respawn = a card slot read dead at least `hold` times, then
    alive at least `hold` times in a row (other frames in between are ignored), inside [t0, t1).
    Slots are the cards' order top to bottom on frames that show the usual number of teammate cards."""
    R = [(t, c) for t, c in reads if t0 <= t < t1 and c]
    if not R: return []
    rows = slot_rows([x['y'] for _, c in R for x in c])
    out = []
    for k in range(len(rows)):
        seq = [(t, x['state'], x['hue']) for t, c in R for x in c
               if slot_of(x['y'], rows) == k and x['state'] in ('alive', 'dead')]
        dead = []; i = 0
        while i < len(seq):
            t, st, _ = seq[i]
            if st == 'dead': dead.append(t); i += 1; continue
            j = i
            while j < len(seq) and seq[j][1] == 'alive': j += 1
            if j - i < hold: i = j; continue                 # a short run of 'alive' reads: not trusted, keep waiting
            if len(dead) >= hold and t - dead[-1] <= MAX_GAP:
                hues = [h for _, s, h in seq[i:j] if h is not None]
                h = int(np.median(hues)) if hues else None
                out.append({'t': round(float(t), 2), 'who': 'teammate', 'slot': k + 1, 'hue': h, 'color': hue_name(h),
                            'how': 'card', 'deadSince': round(float(dead[0]), 2), 'lastDeadRead': round(float(dead[-1]), 2)})
            dead = []; i = j
    return sorted(out, key=lambda e: e['t'])


MAX_GAP = 60.0            # s from the last 'dead' read to the first 'alive' one (more: cards unseen, maybe another game)
SLOTS = 2                 # trios: two teammate cards (a third small card, the player's own, shows in some layouts)
SLOT_TOL = 12             # px: one card's rows across frames (map view, bar vs status line)


def slot_rows(ys):
    """The rows of the teammate cards in a game: the SLOTS rows read most often (ranked / pubs: about 1058 and 1146;
    Wildcard: 1035 and 1149), top to bottom."""
    cl = []
    for y in sorted(ys):
        if cl and y - cl[-1][-1] <= SLOT_TOL: cl[-1].append(y)
        else: cl.append([y])
    top = sorted(cl, key=len, reverse=True)[:SLOTS]
    return sorted(float(np.median(c)) for c in top if len(c) >= 3)


def slot_of(y, rows):
    """Index of the card row y belongs to, or None."""
    d = [abs(y - r) for r in rows]
    return int(np.argmin(d)) if d and min(d) <= SLOT_TOL else None


def own_deaths(samples, t0, t1, min_spec=3, min_back=3):
    """The recording player's deaths and respawns in [t0, t1) from the HUD samples [t, lab, v, spec, x, y, z, vel, ...]:
    a run of spectator-layout ring reads = dead; own-view ring reads after it in the same game = respawned.
    Returns (deaths [t], respawns [{t, who: 'self', how}])."""
    S = [s for s in samples if t0 <= s[0] < t1 and s[2] is not None]
    deaths, resp = [], []
    i = 0
    while i < len(S):
        if not S[i][3]: i += 1; continue
        j = i
        while j < len(S) and S[j][3]: j += 1
        if j - i >= min_spec:
            deaths.append(round(float(S[i][0]), 2))
            back = S[j:j + min_back]
            if len(back) >= min_back and not any(s[3] for s in back) and back[-1][0] - S[j - 1][0] < 120:
                tb = float(S[j][0])
                fall = [s for s in samples if tb - 2 <= s[0] <= tb + 40 and not s[3] and (s[7] or 0) > 600]
                resp.append({'t': round(tb, 2), 'who': 'self', 'how': 'dropship' if fall else 'unknown',
                             'deadSince': round(float(S[i][0]), 2)})
        i = j
    return deaths, resp


def spectating(samples, gap=20.0, pad=3.0):
    """Spectating intervals [(t0, t1)]: runs of spectator-layout ring reads (gaps under `gap` s joined), padded."""
    out = []
    for t in sorted(s[0] for s in samples if s[2] is not None and s[3]):
        if out and t - out[-1][1] <= gap: out[-1][1] = t
        else: out.append([t, t])
    return [(a - pad, b + pad) for a, b in out]


def own_view(card_reads, samples):
    """The card reads taken in the player's own view: [(t, cards)]. A frame's view comes from its own ring-clock read
    (spec = spectator layout); a frame without one is own view unless it lies in a spectating interval (Wildcard
    shows no ring clock most of the time)."""
    import bisect
    iv = spectating(samples); starts = [a for a, _ in iv]
    out = []
    for t, cards, spec in card_reads:
        if spec is None:
            i = bisect.bisect_right(starts, t) - 1
            spec = i >= 0 and t <= iv[i][1]
        if not spec: out.append((t, cards))
    return out


def attach(games, card_reads, samples):
    """Adds to each game: respawns [{t, who: 'teammate' | 'self', how, ...}] and ownDeaths [t] (the recording player's
    deaths: start of each spectating run), from its start to the next game's start."""
    mine = own_view(card_reads or [], samples)
    for i, g in enumerate(games):
        t0 = g['tStart']; t1 = games[i + 1]['tStart'] if i + 1 < len(games) else float('inf')
        deaths, own = own_deaths(samples, t0, t1)
        resp = teammate_respawns(mine, t0, t1) + own
        g['respawns'] = sorted(resp, key=lambda e: e['t'])
        if deaths: g['ownDeaths'] = deaths
    return games
