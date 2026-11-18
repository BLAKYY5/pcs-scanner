"""Shield and health bars (the recording player and his teammates) and the player's own knocks, from the squad
cards at the bottom left (2560 x 1440, 16:9 only).

The player's own card: the health bar is 16 rows high from x = 236 (rows 1344..1359 in the usual layout), white, or
light pink after a revive; the shield bar sits 16 rows above it, cut in segments of 25 shield points (63 px apart),
filled in the colour of the armour tier (the white tier is a light grey). How many segments show depends on the
armour, not only on its colour. Knocked: the health bar becomes a thinner yellow bleed-out bar that shrinks, the name
turns red and the shield segments are empty.
Teammates' cards (rows found by pcs_respawn.read_cards): the same bars, smaller (segments 42 px apart from x = 185,
health bar 216 px wide).

attach() combines the reads per game: a knock needs KNOCK_HOLD knocked reads (menus in between are allowed, an alive
read ends it); a bar value enters the timeline only when the next read agrees with it.
"""
import numpy as np

# reference colours of a shield fill (as recorded): white, blue, purple measured on our recordings (7, 13, 15 Nov);
# red and gold not seen yet (values from the game's usual tier colours, unchecked)
TIERS = (('white', (198, 196, 200)), ('blue', (40, 140, 240)), ('purple', (175, 45, 235)),
         ('red', (235, 45, 50)), ('gold', (240, 190, 50)))

OWN_X0, OWN_PITCH, OWN_SEG, OWN_HW = 236, 63, 58, 317        # own: bar start, segment pitch / fill width, health width
OWN_Y = (1235, 1385)                                         # rows searched for the health / bleed-out bar
OWN_TOP = (1336, 1352)                                       # the own health bar's top row (1344)
SPEC_TOP = (1251, 1267)                                      # 85 px higher: the card of the player one spectates
MATE_X0, MATE_PITCH, MATE_SEG, MATE_HW = 185, 42, 39, 216


def _tier_of(px):
    """px: N x 3 int array of filled pixels -> tier name by the nearest reference colour (median colour)."""
    m = np.median(px, 0)
    d = [((m - np.array(c)) ** 2).sum() for _, c in TIERS]
    k = int(np.argmin(d))
    return TIERS[k][0] if d[k] < 60 ** 2 * 3 else None


def _filled(s):
    """s: rows x cols x 3 int16 (the middle rows of a shield bar) -> per column: filled (a tier colour) or not."""
    r, g, b = s[..., 0], s[..., 1], s[..., 2]
    mn = np.minimum(np.minimum(r, g), b); mx = np.maximum(np.maximum(r, g), b)
    # a tier colour, or the white tier's light grey (about 196, 195, 198; an empty segment is dark and see-through)
    col = ((mx - mn > 90) & (mx > 170)) | ((mn > 175) & (mx - mn < 35))
    return col.mean(0) >= 0.6, col


def _shield(s, pitch, seg):
    """Shield points from the middle rows of the shield bar (5 segments wide) and the tier from their colour."""
    f, col = _filled(s)
    pts = 0.0
    for k in range(5):
        part = f[k * pitch:k * pitch + seg]
        if not len(part): break
        pts += 25.0 * min(1.0, part.sum() / (seg - 2))
    px = s[:, f][col[:, f]]
    tier = _tier_of(px) if len(px) >= 20 else None
    return tier, int(round(pts)), int(f.sum())


def _white_run(m):
    """Length of the run of True from the start of a 1-D mask."""
    return int(np.argmin(m)) if not m.all() else len(m)


def _health(px):
    """Health bar fill: white, or light pink (after a revive / at low health: about 249, 207, 208). Not the red of
    health just lost, the tan of health still to come from a med kit or the green of health regenerating."""
    return (px[..., 0] > 215) & (px[..., 1] > 175) & (px[..., 2] > 130)


def _edge(row):
    """Index of the first health-coloured pixel (the bar's left edge) in a short row of pixels, 8 if none (a mode's
    coloured card frame can sit just left of the bar)."""
    m = _health(row.astype(np.int16))
    return int(np.argmax(m)) if m.any() else 8


def _bleed(rows):
    """Bleed-out bar mask (yellow / orange, as drawn while knocked) over rows x cols x 3 int16."""
    r, g, b = rows[..., 0], rows[..., 1], rows[..., 2]
    return (r > 170) & (g > 110) & (g < 225) & (b < 110) & (r - b > 110)


def _runs(m, lo, hi):
    """[(start, end)] of runs of True with lo <= length <= hi."""
    out = []; k = 0
    while k < len(m):
        if not m[k]: k += 1; continue
        j = k
        while j < len(m) and m[j]: j += 1
        if lo <= j - k <= hi: out.append((k, j))
        k = j
    return out


def read_own(a):
    """The recording player's card: {'state': 'alive', 'tier', 'shield', 'health', 'y'}, {'state': 'knocked', 'y'}
    (bleeding out, also while being revived), {'state': 'spectating', 'y'} (a card 85 px higher: the player is dead and
    watches someone, whose card that is), or None when no card shows (menus, inventory, the map, death screens)."""
    y0, y1 = OWN_Y
    col = a[y0:y1, OWN_X0 + 6:OWN_X0 + 16].astype(np.int16)     # inside the bar: the card shakes a few px when hit
    # the health bar: 15..17 health-coloured rows (white, or pink after a revive)
    for k, j in _runs(_health(col).mean(1) > 0.9, 15, 17):
        top = y0 + k
        if SPEC_TOP[0] <= top <= SPEC_TOP[1]: return {'state': 'spectating', 'y': top}
        if not OWN_TOP[0] <= top <= OWN_TOP[1]: continue
        x0 = _edge(a[top + 8, OWN_X0 - 6:OWN_X0 + 10]) + OWN_X0 - 6
        hrow = a[top + 3:top + 13, x0:x0 + OWN_HW].astype(np.int16)
        health = min(100, int(round(100.0 * np.mean([_white_run(r) for r in _health(hrow)]) / OWN_HW)))
        tier, pts, _ = _shield(a[top - 13:top - 7, x0:x0 + 5 * OWN_PITCH].astype(np.int16), OWN_PITCH, OWN_SEG)
        return {'state': 'alive', 'y': top, 'tier': tier, 'shield': pts, 'health': health}
    # knocked: a yellow bleed-out bar 7..9 rows high, the name above it in red (gold while being revived)
    for k, j in _runs(_bleed(col).mean(1) > 0.8, 6, 10):
        top = y0 + k
        spec = SPEC_TOP[0] + 4 <= top <= SPEC_TOP[1] + 4
        if not (spec or OWN_TOP[0] + 4 <= top <= OWN_TOP[1] + 4): continue
        nm = a[top - 58:top - 30, OWN_X0:OWN_X0 + 200].astype(np.int16)
        r, g, b = nm[..., 0], nm[..., 1], nm[..., 2]
        if ((r > 170) & (g < 90) & (b < 90)).sum() + ((r > 190) & (g > 140) & (b < 80)).sum() >= 40:
            return {'state': 'spectating' if spec else 'knocked', 'y': top}
    return None


def read_mates(a, cards):
    """Teammates' bars on the alive cards pcs_respawn.read_cards found: [{'y', 'tier', 'shield', 'health'}]. The bar's
    own top row is looked for near the card's row (the card row read jitters by a few px between decoders)."""
    out = []
    for c in cards:
        if c['state'] != 'alive': continue
        y = c['y']; top = y + 33
        col = a[y + 26:y + 50, MATE_X0 + 6:MATE_X0 + 16].astype(np.int16)
        rr = _runs(_health(col).mean(1) > 0.9, 8, 12)
        if rr: top = y + 26 + rr[0][0]
        s = a[top - 7:top - 3, MATE_X0:MATE_X0 + 5 * MATE_PITCH].astype(np.int16)
        tier, pts, _ = _shield(s, MATE_PITCH, MATE_SEG)
        hrow = a[top + 2:top + 8, MATE_X0:MATE_X0 + MATE_HW].astype(np.int16)
        out.append({'y': y, 'tier': tier, 'shield': pts,
                    'health': min(100, int(round(100.0 * np.mean([_white_run(r) for r in _health(hrow)]) / MATE_HW)))})
    return out


KNOCK_HOLD = 2          # knocked reads needed (Wildcard knocks often last 1-3 s)
KNOCK_GAP = 15.0        # s without a card (menus, inventory) inside one knock
REVIVE_WITHIN = 5.0    # s: an alive own card this soon after the last knocked read = revived; a death shows a recap or spectating first
AGREE = 3               # shield points / health % two reads in a row may differ by and still agree


def own_knocks(reads, t0, t1):
    """reads: [(t, own)] in time order (own = read_own(...), {'state': 'spectating'} or None). Returns
    [{'t', 'end', 'reads', 'outcome': 'revived' | 'died'}] inside [t0, t1)."""
    R = [(t, o) for t, o in reads if t0 <= t < t1]
    out = []; cur = None
    def close(c, nxt):
        if c and c['reads'] >= KNOCK_HOLD:
            c['outcome'] = 'revived' if nxt is not None and nxt - c['end'] <= REVIVE_WITHIN else 'died'
            out.append(c)
    for t, o in R:
        st = o['state'] if o else None
        if st == 'knocked':
            if cur and t - cur['end'] <= KNOCK_GAP: cur['end'] = t; cur['reads'] += 1
            else:
                close(cur, None); cur = {'t': t, 'end': t, 'reads': 1}
        elif st == 'alive' and cur:
            close(cur, t); cur = None
        elif st == 'spectating' and cur:                     # dead: watching someone else
            close(cur, None); cur = None
    close(cur, None)
    return [{'t': round(float(k['t']), 2), 'end': round(float(k['end']), 2), 'reads': k['reads'], 'outcome': k['outcome']}
            for k in out]


def _key(b):
    return (b.get('tier'), b.get('shield'), b.get('health'))


def _agree(a, b):
    return a[0] == b[0] and abs((a[1] or 0) - (b[1] or 0)) <= AGREE and abs((a[2] or 0) - (b[2] or 0)) <= AGREE


def timeline(seq):
    """seq: [(t, bar dict or None)] -> compact [[t, tier, shield, health], ...]: a value is kept when the next read
    agrees with it (AGREE), and only when it differs from the last value kept; [t, 'knocked'] rows for knocks."""
    out = []
    for i in range(len(seq) - 1):
        (t, a), (_, b) = seq[i], seq[i + 1]
        if not a or not b or 'spectating' in (a.get('state'), b.get('state')): continue
        if a.get('state') == 'knocked' or b.get('state') == 'knocked':
            if a.get('state') == b.get('state') == 'knocked' and (not out or out[-1][1] != 'knocked'):
                out.append([round(float(t), 2), 'knocked'])
            continue
        ka, kb = _key(a), _key(b)
        if not _agree(ka, kb): continue
        if out and len(out[-1]) == 4 and _agree(tuple(out[-1][1:]), ka): continue
        out.append([round(float(t), 2), ka[0], ka[1], ka[2]])
    return out


def attach(games, bar_reads, samples):
    """bar_reads: [(t, own, mates, spec)] per keyframe (spec: the ring clock's spectator layout, or None). Adds to each
    game ownKnocks [{t, end, reads, outcome}] and shieldTimeline {'cols', 'self', 'mates': [slot 1, slot 2]}, from the
    game's start to the next game's start. The own card is told from a spectated player's by its place (read_own);
    teammates' cards only from own-view frames (pcs_respawn.own_view: while spectating they are someone else's)."""
    import pcs_respawn as RS
    bar_reads = bar_reads or []
    own = [(t, {'state': 'spectating'} if spec else o) for t, o, m, spec in bar_reads]
    mine = RS.own_view([(t, m, spec) for t, o, m, spec in bar_reads], samples)
    for i, g in enumerate(games):
        t0 = g['tStart']; t1 = games[i + 1]['tStart'] if i + 1 < len(games) else float('inf')
        O = [(t, o) for t, o in own if t0 <= t < t1]
        M = [(t, m) for t, m in mine if t0 <= t < t1]
        g['ownKnocks'] = own_knocks(O, t0, t1)
        rows = RS.slot_rows([m['y'] for _, ms in M for m in ms])
        mates = []
        for k in range(len(rows)):
            seq = []
            for t, ms in M:
                m = [x for x in ms if RS.slot_of(x['y'], rows) == k]
                seq.append((t, m[0] if m else None))
            mates.append(timeline(seq))
        g['shieldTimeline'] = {'cols': ['t', 'tier', 'shield', 'health'], 'self': timeline(O), 'mates': mates}
    return games
