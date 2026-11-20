"""Heals, ultimates and tacticals the recording player used (2560 x 1440, 16:9 only).

Heals: while a heal item is in use the game draws a panel right of the screen centre: the item's icon, its name (in
the player's language: not read), a progress bar filling from x = 1800 in the item's colour (blue = shield, red =
health) and the seconds left. The panel's frame lines sit at rows 928 / 1023. The item is told by its icon: shield
cell (narrow blue), shield battery (blue with white stripes under it), syringe (white with a small red cross), med
kit (red), phoenix kit (orange at the bottom). A revive uses the same panel with a blue or green frame: not a heal.
A heal = HEAL_HOLD panel reads of the same item close together; done = the bar would have filled before the next
keyframe (else the panel vanished with time left: cancelled). The own shield / health bars (pcs_bars) can't tell: shields
regenerate on their own in S31 (seen rising during and right after cancelled heals).
"""
import numpy as np

HEAL_TOP, HEAL_BOT = 928, 1023          # the panel's frame lines (white)
BAR_Y = (993, 1007)                     # progress bar rows read (bar 991..1009)
BAR_X = (1800, 2172)                    # progress bar: fill from the left, dark track up to the timer
ICON = (940, 1014, 1636, 1718)          # y0, y1, x0, x1 of the item icon
ITEMS = ('cell', 'battery', 'syringe', 'medkit', 'phoenix')
SHIELD_ITEMS, HEALTH_ITEMS = ('cell', 'battery'), ('syringe', 'medkit')


def _masks(p):
    r, g, b = p[..., 0], p[..., 1], p[..., 2]
    mn = np.minimum(np.minimum(r, g), b)
    return {'blue': (b > 150) & (b - r > 80), 'red': (r > 150) & (g < 80) & (b < 80), 'white': mn > 200,
            'orange': (r > 200) & (g > 100) & (g < 180) & (b < 80), 'green': (g > 150) & (g - r > 80) & (g - b > 60)}


def _frame_rows(a, dy):
    """Fraction of the panel's top and bottom frame lines that is white (dy: rows the panel sits lower)."""
    x0, x1 = BAR_X[0], 2150
    def white(y):
        r = a[y + dy - 1:y + dy + 2, x0:x1]
        return float(((np.minimum(np.minimum(r[..., 0], r[..., 1]), r[..., 2]) > 200).mean(1)).max())
    top = white(HEAL_TOP)
    return (top, white(HEAL_BOT)) if top >= 0.5 else (top, 0.0)    # most frames: no top line, nothing more to look at


def _bar(a, dy):
    """-> (fill colour name or None, progress 0..1) when rows BAR_Y hold a progress bar: a fill of one saturated colour
    from the left (its end is soft: a few columns in between), then a grey see-through track to the end; None otherwise."""
    s = a[BAR_Y[0] + dy:BAR_Y[1] + dy, BAR_X[0]:BAR_X[1]].astype(np.int16)
    m = s.mean(0)                                           # per column mean colour
    mx = m.max(1); mn = m.min(1)
    fill = (mx > 120) & (mx - mn > 80)
    grey = (mx - mn < 60) & (mx < 150)
    n = len(mx)
    k = int(np.argmin(fill)) if not fill.all() else n        # fill run from the left
    rest = grey[min(n, k + 6):]
    if len(rest) and rest.mean() < 0.9: return None
    if k <= 2: return (None, 0.0) if grey.mean() >= 0.95 else None
    c = np.median(m[:max(1, k - 3)], 0); r, g, b = c
    if b - r > 90: col = 'blue'
    elif r - g > 120 and r - b > 120: col = 'red'
    elif g - r > 80 and g - b > 40: col = 'green'
    else: col = 'other'
    if np.median(np.abs(m[:max(1, k - 3)] - c), 0).max() > 30: return None    # not one colour
    return col, round(k / n, 3)


def _item(a, dy):
    """The item from its icon: one of ITEMS, 'revive' (the same panel while reviving someone) or None."""
    y0, y1, x0, x1 = ICON
    p = a[y0 + dy:y1 + dy, x0:x1].astype(np.int16)
    M = _masks(p); n = {k: int(v.sum()) for k, v in M.items()}
    body = M['blue'][:, 19:61]              # x 1655..1697: the cell / battery body (the panel's edge glows blue near the end)
    if body.sum() >= 250:                   # cell: blue body down to row ~997; battery: 951..987 with white stripes under
        ys, _ = np.nonzero(body)
        return ('cell' if ys.max() + y0 >= 993 else 'battery'), n
    fr = _masks(a[935 + dy:1016 + dy, 1580:1626].astype(np.int16))    # the frame left of the icon: coloured = revive
    if fr['blue'].sum() + fr['green'].sum() > 150 or n['green'] > 300: return 'revive', n
    if n['orange'] >= 60: return 'phoenix', n
    if n['red'] >= 600: return 'medkit', n
    if n['white'] >= 1100 and n['red'] < 600 and n['blue'] < 100: return 'syringe', n    # white body, small red cross
    return None, n


def read_heal(a):
    """The heal panel: {'item': one of ITEMS or 'revive' or None, 'bar': fill colour, 'p': progress 0..1, 'dy'} or None."""
    for dy in (0, -2, 2, -4, 4):
        top, bot = _frame_rows(a, dy)
        if top < 0.5 or bot < 0.5: continue
        b = _bar(a, dy)
        if b is None: continue
        item, _ = _item(a, dy)
        col, p = b
        # the bar's colour must suit the item (phoenix: either)
        if item in SHIELD_ITEMS and col not in ('blue', None): item = None
        if item in HEALTH_ITEMS and col not in ('red', None): item = None
        return {'item': item, 'bar': col, 'p': p, 'dy': dy}
    return None


# ---------------- ultimate and tactical (bottom centre) ----------------
# Tactical: icon in x 760..850, y 1300..1370 (white when ready, dimmed while on cooldown); under it the key hint (a
# white box, x 806..833) when ready, or the cooldown text ("19 sec", x ~800..865) while it recharges.
# Ultimate: triangle icon in x 1185..1375, y 1205..1375; ready = a blue glow around it and the key hint (x 1266..1293)
# under it; charging = a grey icon and the charge ("29%", x ~1256..1303). Key hints and words depend on the player's
# keys and language: only their width and fill are used.
TAC_ICON = (1296, 1372, 750, 862)       # y0, y1, x0, x1
TAC_LAB = (1382, 1408, 780, 880)
ULT_ICON = (1205, 1375, 1185, 1375)
ULT_WHITE = (1215, 1370, 1200, 1360)
ULT_LAB = (1380, 1410, 1220, 1340)


def _label(a, box):
    """The white label under an icon: (first column, last column, columns, filled fraction) or None."""
    y0, y1, x0, x1 = box
    p = a[y0:y1, x0:x1]
    mn = np.minimum(np.minimum(p[..., 0], p[..., 1]), p[..., 2])
    m = mn > 200
    cs = m.sum(0) >= 3
    if cs.sum() < 6: return None
    xs = np.nonzero(cs)[0]
    w = xs[-1] - xs[0] + 1
    return int(xs[0] + x0), int(xs[-1] + x0), int(w), round(float(m[:, xs[0]:xs[-1] + 1].mean()), 3)


def _hint(lab, centre):
    """A key hint box: about 28 px wide, mostly white, centred under its icon."""
    return lab is not None and 22 <= lab[2] <= 34 and lab[3] >= 0.45 and abs((lab[0] + lab[1]) / 2 - centre) <= 6


def _text(lab, centre):
    """Charge or cooldown text: wider, sparse white glyphs, centred under its icon."""
    return lab is not None and 20 <= lab[2] <= 110 and lab[3] < 0.45 and abs((lab[0] + lab[1]) / 2 - centre) <= 14


def read_abilities(a):
    """{'tac': 'ready' | 'cooldown' | None, 'ult': 'ready' | 'charging' | None, 'tacMask', 'ultMask'}: the masks (white
    pixels of the ready icons, for the legend signs) only when ready."""
    out = {'tac': None, 'ult': None}
    r = a[1362:1388:6, 600:1440]                  # the full map's black key bar (rows ~1356..1440) hides the icons
    if (np.maximum(np.maximum(r[..., 0], r[..., 1]), r[..., 2]) < 25).mean() > 0.9: return out
    y0, y1, x0, x1 = TAC_ICON
    p = a[y0:y1, x0:x1]
    tw = np.minimum(np.minimum(p[..., 0], p[..., 1]), p[..., 2]) > 225
    lab = _label(a, TAC_LAB)
    if tw.sum() >= 80 and _hint(lab, 819.5): out['tac'] = 'ready'; out['tacMask'] = tw
    elif tw.sum() < 20 and _text(lab, 833): out['tac'] = 'cooldown'; out['tacText'] = True
    elif tw.sum() < 20 and _hint(lab, 819.5): out['tac'] = 'cooldown'    # dimmed, key hint still shown: in use (aiming,
                                                                          # Seer's wind-up), the countdown text follows
    y0, y1, x0, x1 = ULT_ICON
    q = a[y0:y1, x0:x1]
    mx = np.maximum(np.maximum(q[..., 0], q[..., 1]), q[..., 2]); mn = np.minimum(np.minimum(q[..., 0], q[..., 1]), q[..., 2])
    glow = int(((mx > 180) & (mx.astype(np.int16) - mn > 120)).sum())     # the ready / active glow (blue for Seer, red for Sparrow)
    lab = _label(a, ULT_LAB)
    if _hint(lab, 1279.5):
        y0, y1, x0, x1 = ULT_WHITE
        p = a[y0:y1, x0:x1]
        out['ult'] = 'ready'; out['ultMask'] = np.minimum(np.minimum(p[..., 0], p[..., 1]), p[..., 2]) > 225
    elif _text(lab, 1279.5): out['ult'] = 'charging'; out['ultText'] = lab[2]
    elif glow >= 1200 and lab is None: out['ult'] = 'active'          # used, still running (or being aimed)
    return out


# ---------------- legend signs ----------------
# 12 x 12 signs of the ability icons, derived once from the game's UI images (the images are not in this repo): the
# white shape of an icon cropped to its own box. A game's ready reads, averaged, give the shape shown on screen.
G = 12
SIGNS = (            # (kind, legend, sign as hex: G x G bits row by row)
    ('ult', 'alter', '0000000000400d406040010010c272000602'),
    ('ult', 'ash', '0fc07f07f3bf7f8f70f70ff0f707783bc07e'),
    ('ult', 'axiom', '0000600f0cf3d9ff8ff9fc03e071d8060000'),
    ('ult', 'ballistic', '00002006026426496d96d56a56a5fa260204'),
    ('ult', 'bangalore', '00000000000010070437427426006046270e'),
    ('ult', 'bloodhound', '108919901801809198198ff9fffff63f81f8'),
    ('ult', 'bloodhound', '100000000002040070070f7f861c4336c040'),
    ('ult', 'catalyst', '0000000000600600600600700f01f839c606'),
    ('ult', 'caustic', '1f83fc7fe79f70f70ff8efdef9e7fe3fc1f8'),
    ('ult', 'conduit', '1082f40f01f81f8dfbf9f7fe3f81f80000f0'),
    ('ult', 'crypto', '0000000600f010826426426420420479e060'),
    ('ult', 'dummie', 'e1ff1e1361b00e00e00c00e00e01b4f3ef1f'),
    ('ult', 'fuse', '0000000000000000000042f6ea7c01f233fc'),
    ('ult', 'fuse', '0024067181900b00ac359090000000000000'),
    ('ult', 'gibraltar', '0000000000000002042442448f1160204000'),
    ('ult', 'lifeline', '0f03fcfffffffffe07e0770e7fe3fc1f80f0'),
    ('ult', 'lifeline', '0008018618618618f1af3ef78f18016063fc'),
    ('ult', 'loba', '0600000600601f83fc0000003fc1f8070060'),
    ('ult', 'madmaggie', '02009009039c2942f429439c09049222470e'),
    ('ult', 'mirage', '000080400bfe7fedde5fe220200000004002'),
    ('ult', 'nova', '3fc3fc36c1680600f00f01687fefff7fe6f6'),
    ('ult', 'octane', '00700300700c20c1180b00f07fcf0effe3fc'),
    ('ult', 'overdrive', '00006006036c4f20f08f18f1969f6fe67060'),
    ('ult', 'pathfinder', '01c00600f039071080308ff8020080000000'),
    ('ult', 'rampart', '1e00001e03f83f83f83b83183b871cffe3f1'),
    ('ult', 'revenant', '0f0cf39f99f91f81f81681f80f0060090060'),
    ('ult', 'seer', '0701f80f01f879e6e676ef6fd9b5fa4f2060'),
    ('ult', 'sparrow', '060060462c63c63c63c63462060060060060'),
    ('ult', 'valkyrie', '0000600600600600600004622643dc39c36c'),
    ('ult', 'vantage', '0000000f01082042642642641f80f0000000'),
    ('ult', 'wattson', '1080001088f90f006006006006039c402c03'),
    ('ult', 'wraith', '0900600000f0098108108108088696264000'),
    ('tac', 'alter', '000200580c40b80b60b80b809de8f3700100'),
    ('tac', 'ash', '3c00207e0fe0c6086006106307f07604001c'),
    ('tac', 'axiom', '0008f19f9bfdfffffffff6f60600000f0000'),
    ('tac', 'ballistic', '1c020c41c99c9e98d18f107910b102004078'),
    ('tac', 'bangalore', '000060060060e67e67264108090010000108'),
    ('tac', 'bloodhound', '0000f010820446289189146220010c0f0000'),
    ('tac', 'caliber', '1c020c41c99c9e98d18f107914b102004078'),
    ('tac', 'catalyst', '0000000000000000600f00f05fafff7fe3fc'),
    ('tac', 'caustic', '0f01f83fc7fef9f76eee7fff7fe3fc3fc0f0'),
    ('tac', 'conduit', '090198204462df38f18f1cf3462204198090'),
    ('tac', 'crypto', '0600f00f00900901f89f9dfb0900f0060060'),
    ('tac', 'dummie', '0600f01f83fc060c6376e7fe7fe7fe3fc0f0'),
    ('tac', 'fuse', '00060070011008004002007e00e00c000000'),
    ('tac', 'gibraltar', '0f01082644f21f80f10f00f0e63861c0330c'),
    ('tac', 'lifeline', '0f01f87fef9f31cf9f7fefff0f0060264244'),
    ('tac', 'lifeline', '0901f83fc7fe39c79e1f86f6841040040000'),
    ('tac', 'loba', '1081f8040000000000000000801ffffbb3fc'),
    ('tac', 'madmaggie', '0000600600600606666f656acf3c03801801'),
    ('tac', 'mirage', '00000c07c1f817f0fe0f00fc1cc798c18c1c'),
    ('tac', 'nova', '0600700f01f82f40f00f08610000600f03fc'),
    ('tac', 'octane', '400c003803c03e01c001000002800c002001'),
    ('tac', 'overdrive', '00040260639c30c2640600f0198000000060'),
    ('tac', 'pathfinder', '0601e83764730600600f00900f0060060060'),
    ('tac', 'rampart', '801801000000000000fffffffffffffff7fe'),
    ('tac', 'revenant', '80170e50a7de1b81780f81f87fe50e70e801'),
    ('tac', 'revenant', '380f80fc0fe09f007803803e01a000000000'),
    ('tac', 'seer', '0000f030c2e4582d0b90941a274204090060'),
    ('tac', 'sparrow', '0000001982644f28618614f2264198000000'),
    ('tac', 'valkyrie', '1f0600400c93801891891801c910020060f8'),
    ('tac', 'vantage', '1081981f8df97fe3f80f01f80f04200007c0'),
    ('tac', 'wattson', '0fe1fc3f83f07ffffe07c0700e00c0180100'),
    ('tac', 'wraith', '0400e01f0ff0ff81f81f00d80cc046002001'),
)


def sign_of(mask):
    """Boolean mask -> G x G sign of the shape cropped to its own box, or None when too small."""
    ys, xs = np.nonzero(mask)
    if len(ys) < 40: return None
    m = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.float32)
    h, w = m.shape
    yi = (np.arange(G + 1) * h / G).astype(int); xi = (np.arange(G + 1) * w / G).astype(int)
    out = np.zeros((G, G), bool)
    for i in range(G):
        for j in range(G):
            c = m[yi[i]:max(yi[i + 1], yi[i] + 1), xi[j]:max(xi[j + 1], xi[j] + 1)]
            out[i, j] = c.mean() > 0.35
    return out


def _bits(hx):
    return np.array([c == '1' for c in bin(int(hx, 16))[2:].zfill(G * G)]).reshape(G, G)


def legend_of(kind, mask):
    """(legend, IoU, margin to the next legend) of the averaged ready mask, or (None, ...)."""
    s = sign_of(mask)
    if s is None: return None, 0.0, 0.0
    best = {}
    for k, leg, hx in SIGNS:
        if k != kind: continue
        b = _bits(hx)
        iou = (s & b).sum() / max(1, (s | b).sum())
        best[leg] = max(best.get(leg, 0.0), float(iou))
    if not best: return None, 0.0, 0.0
    r = sorted(best.items(), key=lambda kv: -kv[1])
    return r[0][0], round(r[0][1], 3), round(r[0][1] - (r[1][1] if len(r) > 1 else 0.0), 3)


# ---------------- per game ----------------
HEAL_GAP = 3.5          # s between two reads of one heal (a read can be missed)
HEAL_HOLD = 2           # panel reads for a heal; one read needs the bars to confirm it
DUR = {'cell': 2.5, 'battery': 5.0, 'syringe': 4.0, 'medkit': 8.0, 'phoenix': 10.0}    # seconds measured on our recordings (S31)
DONE_WITHIN = 1.2       # s: a heal whose bar would fill within this after its last read is done
TAC_WITHIN = 4.0        # s from the last ready read to the first dimmed read
TAC_TEXT_WITHIN = 20.0  # s from the first dimmed read to a cooldown text read (Seer's wind-up shows the key ~10 s)
ULT_WITHIN = 6.0        # s from the last ready read to the first 'active' read (the use itself)
ULT_RUN = 90.0          # s an ult may run (glow without key hint) before the charge starts again
ULT_EMPTY_W = 38        # px: the charge text right after a use is one digit and % ('1%'), narrower than '29%'
USE_HOLD = 2            # reads of the new state that confirm a use


def heal_runs(reads):
    """reads: [(t, read_heal(...))] (non-empty) in time order -> [{'t0', 't1', 'item', 'reads', 'p0', 'p1', 'rate'}].
    A run ends at a gap, another item, a revive, or a bar that starts again (a queued second heal)."""
    runs = []
    for t, h in reads:
        if not h or h['item'] == 'revive': continue
        r = runs[-1] if runs else None
        it = h['item'] or (r and r['item'])
        slow = 0.5 * (t - r['t1']) / DUR[it] if r is not None and it in DUR else 0.0    # the bar fills at least this much
        same = r is not None and t - r['t1'] <= HEAL_GAP and h['p'] >= r['p1'] + slow - 0.05 and \
            (h['item'] is None or r['item'] is None or h['item'] == r['item']) and (h['bar'] in (None, r['bar']) or r['bar'] is None)
        if same:
            r['t1'] = t; r['p1'] = h['p']; r['reads'] += 1; r['items'].append(h['item'])
            if h['bar']: r['bar'] = h['bar']
            if r['item'] is None: r['item'] = h['item']
        else:
            runs.append({'t0': t, 't1': t, 'p0': h['p'], 'p1': h['p'], 'reads': 1, 'item': h['item'], 'bar': h['bar'],
                         'items': [h['item']]})
    for r in runs:
        named = [x for x in r.pop('items') if x]
        r['item'] = max(set(named), key=named.count) if named else None
        r['rate'] = (r['p1'] - r['p0']) / (r['t1'] - r['t0']) if r['reads'] >= 2 and r['t1'] > r['t0'] and r['p1'] > r['p0'] else None
    return runs


def heals(reads, own, t0, t1):
    """Heals of one game: [{'t', 'item', 'done'}] (+ 'reads'). t = when the heal started (first read minus the part of the
    bar already filled, at the run's own fill rate). own: unused (kept for the call shape)."""
    out = []
    for r in heal_runs([(t, h) for t, h in reads if t0 <= t < t1]):
        if not r['item']: continue
        if r['reads'] < HEAL_HOLD: continue
        rate = r['rate'] or 1.0 / DUR[r['item']]
        start = r['t0'] - min(r['p0'] / max(rate, 1e-3), DUR[r['item']])
        # done: the bar would be full before the next keyframe (about 1 s later), else it vanished with time left
        done = r['p1'] + rate * DONE_WITHIN >= 0.97
        out.append({'t': round(float(start), 2), 'item': r['item'], 'done': bool(done), 'reads': r['reads']})
    return out


def uses(seq, kind):
    """seq: [(t, state, extra)] of one ability per keyframe read (state None = not seen), spectating dropped.
    Tactical (extra: the cooldown text shown): ready, then within TAC_WITHIN a dimmed icon, read USE_HOLD times before it
    is ready again, with the cooldown text among them within TAC_TEXT_WITHIN -> t of the first dimmed read.
    Ult (extra: the charge text's width): ready, then (while it runs) the glow without the key hint ('active') or nothing,
    then the charge text read USE_HOLD times in a row within ULT_RUN of the last ready read, with 'active' seen or the
    first charge text short (one digit: empty again) -> t of the first read after the last ready one (the use)."""
    out = []
    S = [x for x in seq if x[1]]
    if kind == 'tac':
        last_ready = None
        for i, (t, s, w) in enumerate(S):
            if s == 'ready': last_ready = t; continue
            if last_ready is None or s != 'cooldown': continue
            run = []
            for x in S[i:]:
                if x[1] == 'ready': break
                run.append(x)
            if t - last_ready <= TAC_WITHIN and len(run) >= USE_HOLD and any(x[2] and x[0] - t <= TAC_TEXT_WITHIN for x in run):
                out.append(t)
            last_ready = None
        return out
    last_ready = after = None; act = False
    for i, (t, s, w) in enumerate(seq):
        if s == 'ready': last_ready = t; after = None; act = False; continue
        if last_ready is None: continue
        if after is None: after = t
        if s == 'active': act = True; continue
        if s != 'charging': continue
        nxt = [x for x in seq[i:] if x[1]][:USE_HOLD]
        if t - last_ready <= ULT_RUN and (act or (w is not None and w <= ULT_EMPTY_W)) and                 len(nxt) == USE_HOLD and all(x[1] == 'charging' for x in nxt):
            out.append(after if after - last_ready <= ULT_WITHIN else t)
        last_ready = after = None; act = False
    return out


def attach(games, usage_reads, bar_reads=None):
    """usage_reads: [(t, heal read or None, ability read dict or None)] per keyframe; bar_reads: pcs_bars rows
    (t, own, mates, spec). Adds games[].heals [{t, item, done}] and games[].abilities [{t, kind, legend}] (legend from the
    legend select screen, else from the icon signs, else None). Spectating frames (own card 85 px higher, or the ring
    clock's spectator layout) are left out: the HUD is then someone else's."""
    bar_reads = bar_reads or []
    own = [(t, o) for t, o, m, spec in bar_reads]
    spect = sorted(t for t, o, m, spec in bar_reads if spec or (o and o.get('state') == 'spectating'))
    import bisect
    def spectating(t):
        k = bisect.bisect_left(spect, t - 0.01)
        return k < len(spect) and abs(spect[k] - t) < 0.02
    for i, g in enumerate(games):
        t0 = g['tStart']; t1 = games[i + 1]['tStart'] if i + 1 < len(games) else float('inf')
        R = [(t, h, ab) for t, h, ab in usage_reads if t0 <= t < t1 and not spectating(t)]
        g['heals'] = heals([(t, h) for t, h, ab in R if h], own, t0, t1)
        leg = None
        for s in ((g.get('select') or {}).get('squad') or []):
            if s.get('self') and s.get('legend'): leg = s['legend']
        if not leg:
            for kind in ('ult', 'tac'):
                ms = [ab.get(kind + 'Mask') for t, h, ab in R if ab and ab.get(kind + 'Mask') is not None]
                if len(ms) >= 5:
                    name, iou, margin = legend_of(kind, np.mean(ms, 0) > 0.5)
                    if name and iou >= 0.5 and margin >= 0.15: leg = name; break
        ab = []
        for kind in ('tac', 'ult'):
            for t in uses([(t, a.get(kind), a.get('ultText') if kind == 'ult' else a.get('tacText')) for t, h, a in R if a], kind):
                ab.append({'t': round(float(t), 2), 'kind': 'ult' if kind == 'ult' else 'tactical', 'legend': leg})
        g['abilities'] = sorted(ab, key=lambda x: x['t'])
    return games


MASK_EVERY = 10.0       # s: one ready-icon mask per ability kept per this time (enough to average a game's shape)


def read(img, t, perf=None):
    """One keyframe for the scan: (t, heal read or None, ability states + packed icon masks) or None (nothing read).
    perf (the scan's dict) remembers when a mask was last kept, so a long recording keeps a few hundred at most."""
    h = read_heal(img)
    ab = read_abilities(img)
    at = (perf if perf is not None else {}).setdefault('_usageMaskAt', {})
    for kind in ('tac', 'ult'):
        m = ab.pop(kind + 'Mask', None)
        if m is not None and t - at.get(kind, -1e9) >= MASK_EVERY:
            at[kind] = t; ab[kind + 'Mask'] = (m.shape, np.packbits(m))
    if not h and not ab.get('tac') and not ab.get('ult'): return None
    return (round(float(t), 2), h, ab)


def unpack(rows):
    """Scan rows of read() -> the same with the masks as boolean arrays again."""
    out = []
    for t, h, ab in rows:
        ab = dict(ab or {})
        for kind in ('tac', 'ult'):
            v = ab.get(kind + 'Mask')
            if v is not None:
                shp, bits = v
                ab[kind + 'Mask'] = np.unpackbits(bits)[:shp[0] * shp[1]].reshape(shp).astype(bool)
        out.append((t, h, ab))
    return out
