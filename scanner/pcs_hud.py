"""HUD readers for the PCS scanner: ring clock + label, debug overlay (POS / VEL), timeline building."""
import os, re, io, sys, json, base64, subprocess
import numpy as np
from PIL import Image

HERE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))  # bundled data when frozen (PCS Scanner app)
DEC = []   # decoder options chosen by the scanner (GPU when available)
_F = json.load(open(os.path.join(HERE, 'pcs_fonts.json')))
RING_T = {k: np.array(v) for k, v in _F['ring'].items()}
DBG_T = {k: np.array(v) for k, v in _F['dbg'].items() if k != '.'}
PRE_T = {k: np.array(v) for k, v in _F['pre'].items()}
RSLOTS = [(295, 313), (335, 353), (355, 373)]       # m : s s   (x at 2560 wide)
PSLOTS = [(203, 229), (231, 254), (280, 307), (307, 333)]   # pre-ring clock 'mm:ss' next to the round icon
SPEC_OFF = 85                                        # HUD drops 85 px while spectating
STRETCH = False   # 4:3 stretched recording (decided by the scanner): the 60 fps corner reads unsquash rows too
LOOSE = False   # set by the scanner for 4:3 stretched recordings (softer, rescaled text): looser text thresholds
NUMRX = re.compile(r'(?<![\d])(-?\d{1,6})[.?\-](\d\d)(?!\d)')
# map % = 50 + X/s , 50 - Y/s   (s = world units per 1 % of map); refined from calibration pairs later
# map % = cx + X/sx , cy - Y/sy  (fitted from 220 map opens: own arrow vs POS, Oct 2026)
CAL = {'WE': (914.2, 49.46, 885.1, 49.82), 'ED': (902.9, 49.03, 861.1, 50.39), 'SP': (1024.1, 49.17, 1070.4, 50.63)}

def white(s, lo=180):
    s = s.astype(np.int16)
    if LOOSE: lo = min(lo, 165)
    return (s.min(2) > lo) & ((s.max(2) - s.min(2)) < 50)

# ---------- ring clock ----------
def _glyph(m, T=RING_T, hmin=18, hmax=26):
    ys, xs = np.nonzero(m)
    if len(xs) < 20: return None
    g = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    if not (hmin <= g.shape[0] <= hmax): return None
    g = np.array(Image.fromarray((g * 255).astype(np.uint8)).resize((12, 18), Image.BILINEAR)) / 255.0
    sc = sorted((float(np.abs(g - t).mean()), d) for d, t in T.items())
    if sc[0][0] > (0.2 if LOOSE else 0.15) or sc[1][0] - sc[0][0] < 0.025: return None
    return int(sc[0][1])

def _runs(m, x0, x1, n):
    """Digit columns found from the text itself (stretched HUDs sit a few px off the fixed slots); the colon is dropped."""
    col = m[:, x0:x1].sum(0) > 0; runs = []; st = None
    for i, v in enumerate(list(col) + [False]):
        if v and st is None: st = i
        elif not v and st is not None:
            if i - st >= 5 and m[:, x0 + st:x0 + i].sum() >= 40: runs.append((x0 + st, x0 + i))
            st = None
    return runs if len(runs) == n else [(0, 0)] * n

def read_ring(a):
    for off in (0, SPEC_OFF):
        if white(a[441 + off:442 + off, 70:370]).mean() < 0.8: continue
        m = white(a[449 + off:478 + off])
        d = [_glyph(m[:, x0 - 2:x1 + 2]) for x0, x1 in RSLOTS]
        if None in d: return None
        sep = m[:, 314:336]
        if sep[10:18].sum() > 0:            # colon  -> m:ss (display rounds down)
            if d[1] > 5: return None
            v, fine = d[0] * 60 + d[1] * 10 + d[2], False
        elif sep[20:27].sum() > 0:          # point  -> s.cc (last 10 seconds)
            v, fine = d[0] + (d[1] * 10 + d[2]) / 100, True
        else: return None
        return {'v': v, 'fine': fine, 'spec': off > 0, 'lab': m[3:27, 60:290]}
    for off in (0, SPEC_OFF):               # before the ring is drawn: round icon (orange circle) + 'mm:ss'
        c = a[417 + off:465 + off, 131:179].astype(np.int16)
        orange = (c[..., 0] > 190) & (c[..., 1] > 90) & (c[..., 1] < 180) & (c[..., 2] < 90)
        reddish = (c[..., 0] > 110) & (c[..., 0] - c[..., 2] > 25) & (c[..., 0] - c[..., 1] > 25)   # later rounds: dull red ring
        ring_w = ((c.min(2) > 190) & (np.ptp(c, axis=2) < 40)).sum()
        if orange.sum() < 30 and not (reddish.sum() >= 30 and ring_w >= 40) and not (LOOSE and ring_w >= 150): continue
        m = (a[422 + off:460 + off].astype(np.int16).min(2) > (165 if LOOSE else 190)) & (np.ptp(a[422 + off:460 + off].astype(np.int16), axis=2) < 40)
        d = [_glyph(m[:, x0:x1], PRE_T, 24, 34) for x0, x1 in (_runs(m, 195, 345, 4) if LOOSE else PSLOTS)]
        if None in d or d[2] > 5: return None
        icon = (c.min(2) > 190) & (np.ptp(c, axis=2) < 40)
        return {'v': (d[0] * 10 + d[1]) * 60 + d[2] * 10 + d[3], 'fine': False, 'spec': off > 0, 'lab': icon, 'pre': True}
    return None

class Labels:
    """Clusters the ring label bitmaps ('ROUND 1 - CLOSING', ...) so Claude can read each one once."""
    def __init__(self): self.reps = []; self.n = []
    def id(self, m):
        if m.sum() < 30: return -1
        for i, r in enumerate(self.reps):
            if r.shape != m.shape: continue
            if (r ^ m).sum() / max(1, (r | m).sum()) < 0.18: self.n[i] += 1; return i
        self.reps.append(m.copy()); self.n.append(1); return len(self.reps) - 1
    def export(self):
        out = []
        for r, n in zip(self.reps, self.n):
            b = io.BytesIO(); Image.fromarray(np.where(r, 0, 255).astype(np.uint8)).save(b, 'PNG', optimize=True)
            out.append({'n': n, 'png': base64.b64encode(b.getvalue()).decode()})
        return out

# ---------- debug overlay (cl_showpos style: NAME / POS / ANG / VEL) ----------
def _dbg_line(m, y):
    s = ''
    for c in range(6, 36):
        x = 3 + 8 * c; cell = m[y:y + 9, x:x + 7].astype(np.int16); n = int(cell.sum())
        if n == 0: s += ' '; continue
        best = min((int((t != cell).sum()), k) for k, t in DBG_T.items())
        if best[0] <= 9: s += best[1]; continue
        if 1 <= cell[7:9, 1:5].sum() <= 4 and cell[:6].sum() <= 2: s += '.'; continue
        s += '-' if cell[3:6, 1:6].sum(1).max() >= 4 and cell[7:9].sum() <= 2 else '?'
    return s

def read_dbg(a):
    m = white(a[40:110, 0:300], 170)
    pos = [float(x + '.' + y) for x, y in NUMRX.findall(_dbg_line(m, 22))]
    vel = [float(x + '.' + y) for x, y in NUMRX.findall(_dbg_line(m, 50))]
    return (pos[:3] if len(pos) >= 3 else None), (vel[0] if vel else None)

# ---------- timeline ----------
def _end(pts):
    """pts: [(t, v, fine)]. m:ss shows floor(remaining) -> end in [t+v, t+v+1]; s.cc -> end = t+v (+-0.02)."""
    if not pts: return None, 0
    fine = [t + v for t, v, f in pts if f]
    if len(fine) >= 2: return float(np.median(fine)), 0.02
    lo = np.array([t + v - (0.02 if f else 0) for t, v, f in pts]); hi = np.array([t + v + (0.02 if f else 1) for t, v, f in pts])
    best = (-1, 0.0, 0.0)
    for c in np.unique(np.r_[lo, hi]):
        n = int(((lo <= c + 1e-6) & (hi >= c - 1e-6)).sum())
        if n > best[0]: best = (n, c, c)
        elif n == best[0]: best = (n, best[1], c)
    return (best[1] + best[2]) / 2, (best[2] - best[1]) / 2

def ring_segments(samples):
    """samples: [t, lab, v, spec, x, y, z, vel, fine]. A phase = run of frames whose (t + v) stays constant.
    endsAt uses your own view; spectating is delayed, so it is only used when nothing else was seen."""
    R = [(s[0], s[1], s[2], s[3], s[8], s[9] if len(s) > 9 else False) for s in samples if s[2] is not None]
    segs = []
    for t, lab, v, spec, fine, pre in R:
        k = t + v
        if segs and abs(k - segs[-1]['k']) < 2.5 and t - segs[-1]['last'] < 120 and segs[-1]['pre'] == pre:
            g = segs[-1]
        else:
            g = {'k': k, 'pts': [], 'labs': {}, 'first': t, 'firstV': v, 'last': t, 'pre': pre}; segs.append(g)
        g['pts'].append((t, v, fine, spec)); g['labs'][lab] = g['labs'].get(lab, 0) + 1; g['last'] = t
        g['k'] = float(np.median([a + b for a, b, _, _ in g['pts']]))
    out = []
    for g in segs:
        if len(g['pts']) < 3: continue
        own = [(t, v, f) for t, v, f, sp in g['pts'] if not sp]
        spc = [(t, v, f) for t, v, f, sp in g['pts'] if sp]
        e, err = _end(own) if own else _end(spc)
        es, _ = _end(spc)
        r = {'style': 'roundIcon' if g['pre'] else 'bar', 'label': max(g['labs'], key=g['labs'].get), 'firstSeen': round(g['first'], 2), 'firstV': g['firstV'],
             'lastSeen': round(g['last'], 2), 'endsAt': round(e, 2), 'endsAtErr': round(err, 2), 'samples': len(g['pts']),
             'fromSpectate': not own}
        if own and spc and es is not None: r['spectateLag'] = round(es - e, 2)
        out.append(r)
    return out

def clean_pos(samples):
    """The minus sign is the most fragile glyph (thin line, bright skies). For x and y, take the sign most
    neighbours (+-5 samples, similar magnitude) agree on."""
    P = [s for s in samples if s[4] is not None]
    for k in (4, 5):
        signs = [1 if s[k] >= 0 else -1 for s in P]
        for i in range(len(P)):
            a = abs(P[i][k]); vote = 0
            for j in range(max(0, i - 5), min(len(P), i + 6)):
                if j != i and abs(abs(P[j][k]) - a) < 3000: vote += signs[j]
            if vote and (1 if vote > 0 else -1) != signs[i]: P[i][k] = -P[i][k]
    return samples

def landing(samples, t0):
    """Own view only. On the dropship VEL reads 0.00; jumping makes it > 0; free fall > 600.
    exit = first moving sample of the run that leads into free fall; touchdown = height stops dropping."""
    P = [(s[0], s[4], s[5], s[6], s[7]) for s in samples if s[0] >= t0 and not s[3] and s[4] is not None]
    if len(P) < 4: return None
    fall = next((i for i in range(1, len(P)) if (P[i][4] or 0) > 600), None)
    if fall is None: return None
    ex = fall
    while ex > 0 and (P[ex - 1][4] is None or P[ex - 1][4] > 50): ex -= 1   # unreadable speed: keep walking back
    land = None
    for i in range(fall, len(P) - 1):
        dz = (P[i + 1][3] - P[i][3]) / max(P[i + 1][0] - P[i][0], 0.2)
        if dz > -60 and (P[i][4] or 0) < 600: land = i; break
    if land is None: return None
    return {'exitWindow': [round(P[ex - 1][0], 2) if ex > 0 else None, round(P[ex][0], 2)], 'exitAt': round(P[ex][0], 2),
            'exitZ': P[ex][3], 'landWindow': [round(P[land - 1][0], 2), round(P[land][0], 2)], 'landedAt': round(P[land][0], 2),
            'x': P[land][1], 'y': P[land][2], 'z': P[land][3]}

def _corner(path, a, b, W=2560, H=1440, cw=420, ch=570):
    """Every frame (60 fps) between a and b, top-left corner only (debug overlay + ring clock live there)."""
    cmd = ['ffmpeg', '-v', 'error'] + DEC + ['-ss', f'{max(0, a):.3f}', '-i', path, '-t', f'{b - a:.3f}',
           '-vf', (f'scale={W}:{H},crop={cw}:{round(ch * 0.75)}:0:0,scale={cw}:{ch}:flags=neighbor' if STRETCH else f'scale={W}:{H},crop={cw}:{ch}:0:0'),
           '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    raw = subprocess.run(cmd, capture_output=True, timeout=180).stdout
    n = cw * ch * 3
    for i in range(0, len(raw) - n + 1, n):
        yield max(0, a) + (i // n) / 60.0, np.frombuffer(raw[i:i + n], np.uint8).reshape(ch, cw, 3)

def _topleft(path, a, b):
    out = []
    for t, f in _corner(path, a, b, ch=110, cw=300):
        p, v = read_dbg(f); out.append((t, p, v))
    return out

def refine_phases(path, segs, samples):
    """Frame-exact first appearance of each ring phase (search between the previous keyframe and the first sighting)."""
    ts = [s[0] for s in samples]
    for r in segs[:1]:   # only the R1 timer (game clock anchor); later phases are pinned by their countdown end times
        prev = [t for t in ts if t < r['firstSeen']]
        if not prev or r['firstSeen'] - prev[-1] > 3: continue
        try:
            for t, f in _corner(path, prev[-1], r['firstSeen'] + 0.02):
                rg = read_ring(f)
                if rg and abs((t + rg['v']) - (r['firstSeen'] + r['firstV'])) < 2.5:
                    r['appearedAt'] = round(t, 3); break
        except Exception:
            pass
    return segs

def refine_landing(path, ld):
    """Decode every frame (60 fps) inside the two keyframe windows: frame-exact jump and touchdown."""
    try:
        a, b = ld['exitWindow']
        if a is not None:
            fr = [(t, p, v) for t, p, v in _topleft(path, a, b + 0.05) if p and v is not None]
            for i in range(len(fr) - 6):          # jump = horizontal speed > 0 and stays > 0 (ignores 1-frame spikes)
                if all(fr[k][2] > 0.5 for k in range(i, i + 6)):
                    ld['exitAtExact'] = round(fr[i][0], 3)
                    k = i - 1                      # dropship hovering at the drop-off: height steady before the jump
                    k = i - 3
                    while k > 0 and abs(fr[k][1][2] - fr[k - 1][1][2]) < 1.2: k -= 1
                    if k < i - 6: ld['shipStoppedAt'] = round(fr[k][0], 3)
                    break
        a, b = ld['landWindow']
        fr = [(t, p[2]) for t, p, v in _topleft(path, a, b + 0.6) if p]
        for i in range(len(fr) - 4):
            if all(fr[k + 1][1] - fr[k][1] > -1.5 for k in range(i, i + 4)):
                ld['landedAtExact'] = round(fr[i][0], 3); break
    except Exception as e:
        ld['refineError'] = str(e)[:80]
    return ld

def to_map(mp, x, y):
    c = CAL.get(mp)
    return (round(c[1] + x / c[0], 2), round(c[3] - y / c[2], 2)) if c else None

# ---------- ring schedule (seconds after round 1 starts closing), measured Oct 2026 ----------
_OFF_WE_ED = [('C', 1, 264.3), ('W', 2, 388.4), ('C', 2, 489.9), ('W', 3, 583.8), ('C', 3, 630.4), ('W', 4, 724.9),
              ('C', 4, 776.6), ('W', 5, 855.5), ('C', 5, 912.5), ('W', 6, 975.0)]
_OFF_SP = [('C', 1, 281.6), ('W', 2, 405.5), ('C', 2, 525.1), ('W', 3, 619.0), ('C', 3, 682.8), ('W', 4, 776.8),
           ('C', 4, 828.9), ('W', 5, 907.9)]
SCHED = {'WE': _OFF_WE_ED, 'ED': _OFF_WE_ED, 'SP': _OFF_SP}

def number_rounds(segs, mp):
    """Round number + kind for each ring phase: matched to the map's schedule from the round-1 anchor
    (so a missed phase never shifts the numbering). Falls back to counting when no anchor was seen."""
    off = SCHED.get(mp, _OFF_WE_ED)
    w1 = next((r for r in segs if r['style'] == 'roundIcon' and r['firstV'] >= 60), None)
    rnd = 0
    for r in segs:
        kind = 'wait' if r['style'] == 'roundIcon' else 'closing'
        r['kind'] = kind
        if w1 is not None:
            if r is w1: r['round'] = 1; continue
            rel = r['endsAt'] - w1['endsAt']
            cand = [(abs(rel - o), n) for k, n, o in off if (k == 'W') == (kind == 'wait')]
            best = min(cand) if cand else (99, 0)
            if best[0] <= 6: r['round'] = best[1]; continue
            r['round'] = None; r['offSchedule'] = round(rel, 1); continue
        if kind == 'wait': rnd += 1
        r['round'] = rnd or None
    return segs

def restart_cuts(samples, segs, mp):
    """Times where a new game starts without a drop screen: >25 s without HUD + position jump, and the next
    ring phase does not fit the current game's schedule (respawns keep the same ring clock)."""
    offs = [0.0] + [o for _, _, o in SCHED.get(mp, _OFF_WE_ED)]
    P = [x for x in samples if x[4] is not None]
    anchor = next((r['endsAt'] for r in segs if r['style'] == 'roundIcon' and r['firstV'] >= 60), None)
    cuts = []
    for i in range(1, len(P)):
        if not (P[i][0] - P[i - 1][0] > 25 and abs(abs(P[i][4]) - abs(P[i - 1][4])) + abs(abs(P[i][5]) - abs(P[i - 1][5])) > 5000): continue
        nxt = next((r for r in segs if r['firstSeen'] > P[i][0]), None)
        if nxt is None or anchor is None: continue
        if min(abs(nxt['endsAt'] - anchor - o) for o in offs) > 4:
            cuts.append(P[i - 1][0] + 1); anchor = nxt['endsAt']
    return cuts

# ---------- weapon icon of a kill line ----------
def weapon_file():
    """The private weapon icons file: where PCS_WEAPON_ICONS says, else the app's data folder, else next to the scanner."""
    data = os.environ.get('PCS_DATA_DIR')
    for p in (os.environ.get('PCS_WEAPON_ICONS'), os.path.join(data, 'pcs_weapons.npz') if data else None, os.path.join(HERE, 'pcs_weapons.npz')):
        if p and os.path.exists(p): return p
    return None
WEAPON_NAMES = {'r97': 'R-99', 'r45': 'RE-45', 'r301': 'R-301', 'energy_ar': 'Havoc', 'g7': 'G7 Scout', 'bow': 'Bocek',
                'lstar': 'L-STAR', 'car': 'CAR', '3030repeater': '30-30 Repeater', 'eva8': 'EVA-8', 'hemlock': 'Hemlok',
                'sniper': 'Kraber', 'dragon': 'Rampage', 'p2020': 'P2020', 'p2020_akimbo_active': 'Dual P2020',
                'mozambique_akimbo_active': 'Dual Mozambique', 'wingman_akimbo_active': 'Dual Wingman'}

class Weapons:
    """Names the weapon icon in a kill-feed line by comparing it with the game's own icons.
    The icons come from a private file (pcs_weapons.npz: one small grey picture per weapon, at the size and
    orientation the feed draws them on a 2560-wide frame). The file is built from the game's files on the coach's
    PC and is not part of the public repo; without it no weapon is read and the scan is otherwise unchanged.
    Measured on real lines: a line with a weapon scores >= 0.86, a line without one <= 0.69."""
    FH, FW = 64, 704          # working canvas: one feed line with margin
    MIN = 0.80                # accept above this
    STD = 15.0                # a flat background must not look like a match
    def __init__(self, path=None):
        self.T = []
        try:
            z = np.load(path or weapon_file())
            for n in z.files:
                if n.startswith('legend_'): continue        # legend portraits live in the same file (see Legends)
                t = z[n].astype(np.float32); tz = t - t.mean(); pad = np.zeros((self.FH, self.FW), np.float32)
                if t.shape[0] > self.FH or t.shape[1] > self.FW: continue
                pad[:t.shape[0], :t.shape[1]] = tz
                self.T.append((n, t.shape, float(np.sqrt((tz * tz).sum())) + 1e-6, np.conj(np.fft.rfft2(pad))))
        except Exception: self.T = []
    @staticmethod
    def _box(a, h, w):
        c = np.pad(a, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
        return c[h:, w:] - c[:-h, w:] - c[h:, :-w] + c[:-h, :-w]
    def match(self, rgb):
        """rgb: one feed line at full size (rows x cols x 3). -> None or {'weapon', 'score', 'x', 'y', 'w', 'h'}."""
        if not self.T: return None
        g = np.asarray(rgb, np.float32).min(2)[:self.FH, :self.FW]     # white icon stays bright, coloured names drop
        H, Wd = g.shape; c = np.zeros((self.FH, self.FW), np.float64); c[:H, :Wd] = g
        G = np.fft.rfft2(c); best = None
        for n, (th, tw), tn, F in self.T:
            if th > H or tw > Wd: continue
            num = np.fft.irfft2(G * F, s=(self.FH, self.FW))[:H - th + 1, :Wd - tw + 1]
            k = th * tw; S = self._box(c[:H, :Wd], th, tw); S2 = self._box(c[:H, :Wd] ** 2, th, tw)
            sc = num / (np.sqrt(np.maximum(S2 - S * S / k, k * self.STD ** 2)) * tn)
            i = int(sc.argmax()); y, x = divmod(i, sc.shape[1])
            if best is None or sc[y, x] > best['score']:
                best = {'weapon': n, 'score': float(sc[y, x]), 'x': x, 'y': y, 'w': tw, 'h': th}
        return best if best and best['score'] >= self.MIN else None

def red_arrow(rgb, x0, x1):
    """The red down arrow the feed draws between the weapon and the victim (columns x0..x1 of a line)."""
    p = np.asarray(rgb, np.int16)[:, max(0, x0):max(0, x1)]
    return int(((p[..., 0] > 170) & (p[..., 1] < 110) & (p[..., 2] < 110)).sum()) >= 25

# ---------- who is on each side of a kill line ----------
# Around the weapon icon a kill line reads:  killer  [weapon]  marker  victim.
# Marker: red arrow = knock, gold skull = kill whose last bullet was a headshot, nothing = kill (Blakey, Nov 2026);
# in ranked, kills that give RP show the victim's rank badge there, with '+N RP' after the name (kept as 'rank').
# Names of our squad are drawn in the squad colours (orange, green, teal...), everyone else in white: that tells
# "ours" from "theirs" without reading the name, in any alphabet.
def _feed_masks(a):
    r, g, b = a[..., 0], a[..., 1], a[..., 2]; mx = a.max(2); mn = a.min(2)
    white = (mn > 195) & (mx - mn < 50)
    red = (r > 170) & (g < 110) & (b < 110)
    col = (mx > 165) & ((mx - mn) > 0.38 * mx) & ~red
    gold = (r > 190) & (g > 130) & (g < 215) & (b < 120)
    return white, red, col, gold

def _gap_runs(c, gap):
    out = []; s = l = None
    for x in np.nonzero(c)[0]:
        if s is None: s = l = x
        elif x - l > gap: out.append((int(s), int(l) + 1)); s = l = x
        else: l = x
    if s is not None: out.append((int(s), int(l) + 1))
    return out

def _hue(p):
    """Median hue in degrees of the pixels p (n x 3)."""
    p = p.astype(np.float32); mx = p.max(1); mn = p.min(1); d = np.maximum(mx - mn, 1e-3)
    r, g, b = p[:, 0], p[:, 1], p[:, 2]
    h = np.where(mx == r, ((g - b) / d) % 6, np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4)) * 60
    return float(np.median(h))

SQUAD_HUES = (('orange', 20, 58), ('green', 58, 105), ('teal', 150, 195), ('blue', 195, 250), ('purple', 250, 320))
def _name(seg, white, col, run):
    """One word of a name -> None (too little to say), {'side': 'other'} or {'side': 'squad', 'hue', 'color'}."""
    s, e = run; w = int(white[:, s:e].sum()); c = int(col[:, s:e].sum())
    if w + c < 40: return None
    if c > 0.6 * (w + c):
        h = _hue(seg[:, s:e][col[:, s:e]])
        return {'side': 'squad', 'hue': round(h), 'color': next((n for n, lo, hi in SQUAD_HUES if lo <= h < hi), 'other')}
    if w > 0.6 * (w + c): return {'side': 'other'}
    return None

def kill_parts(rgb, wp):
    """rgb: a feed line at full size; wp: the weapon found in it (Weapons.match).
    -> {'marker': 'arrow' | 'skull' | 'other' | None, 'killer': ..., 'victim': ...} (names as in _name)."""
    a = np.asarray(rgb, np.int16); y0 = max(0, wp['y'] - 6); y1 = min(a.shape[0], wp['y'] + wp['h'] + 6)
    out = {'marker': None, 'killer': None, 'victim': None}
    seg = a[y0:y1, wp['x'] + wp['w'] - 2:]; white, red, col, gold = _feed_masks(seg); anyt = white | red | col
    R = _gap_runs(anyt.sum(0) >= 2, 9); k = 0
    if R and R[0][0] <= 45:
        s, e = R[0]; n = max(1, int(anyt[:, s:e].sum())); w = e - s
        ys = np.nonzero(anyt[:, s:e].sum(1) >= 2)[0]; h = int(ys[-1] - ys[0] + 1) if len(ys) else 0
        if w <= 22 and red[:, s:e].sum() >= 0.5 * n: out['marker'] = 'arrow'; k = 1
        elif 22 <= w <= 40 and h >= 28 and gold[:, s:e].sum() >= 0.45 * n: out['marker'] = 'skull'; k = 1
        elif w <= 22 and white[:, s:e].sum() < 0.3 * n: out['marker'] = 'rank'; k = 1
    if out['marker'] is None:      # a line that is fading out: the arrow is only a pale red smear before the name
        z = seg[:, 16:52]      # red, not the orange of a squad name: green well under red
        soft = (z[..., 0] > 120) & (z[..., 0] - np.maximum(z[..., 1], z[..., 2]) > 60) & (z[..., 1] < 0.56 * z[..., 0])
        cols = soft.sum(0) >= 3
        if soft.sum() >= 40 and 5 <= int(cols.sum()) <= 24 and not (white[:, 16:52] & ~soft).sum() > 60:
            out['marker'] = 'arrow'; R = [r for r in R if r[0] >= 46] if R and R[0][0] < 46 else R; k = 0
    if len(R) > k and R[k][0] <= 115:
        out['victim'] = _name(seg, white, col, R[k]); out['victimSig'] = _sig(white | col, R[k:], +1)
    seg = a[y0:y1, :max(0, wp['x'] + 2)]; white, red, col, gold = _feed_masks(seg)
    R = _gap_runs((white | col).sum(0) >= 2, 9)
    if R and seg.shape[1] - R[-1][1] <= 45:
        out['killer'] = _name(seg, white, col, R[-1]); out['killerSig'] = _sig(white | col, R, -1)
    return out

def _sig(mask, R, direction):
    """Shape of a whole name (its words, up to the first wide gap) as 36 hex digits: the same name gives nearly the
    same digits, so one line captured twice (the feed scrolls and fades) can be recognised. Not readable text."""
    R = list(R) if direction > 0 else list(R)[::-1]; x0, x1 = R[0]
    for s, e in R[1:]:
        if (s - x1 if direction > 0 else x0 - e) > 26 or (max(e, x1) - min(s, x0)) > 300: break
        x0, x1 = min(x0, s), max(x1, e)
    m = mask[:, x0:x1]; ys = np.nonzero(m.sum(1) >= 2)[0]
    if len(ys) < 4 or x1 - x0 < 8: return None
    m = m[ys[0]:ys[-1] + 1]
    bits = np.array(Image.fromarray((m * 255).astype(np.uint8)).resize((24, 6), Image.BILINEAR)) > 60
    return '%036x' % int(''.join('1' if b else '0' for b in bits.ravel()), 2)

def _same_sig(a, b):
    if not a or not b: return a == b
    return bin(int(a, 16) ^ int(b, 16)).count('1') <= 26          # of 144 points

def feed_stats(lines, t0, t1, squad=None):
    """What the kill lines between t0 and t1 say about our squad: knocks and kills we made, times we went down.
    squad (the slots read on the game's legend select screen, each with the hue of its colour): the feed writes
    each squad member's name in that same colour, so the counts are also given per player in 'players'."""
    s = {'squadKnocks': 0, 'squadKills': 0, 'squadKnocked': 0, 'squadKilled': 0, 'headshotKills': 0}
    P = [{'slot': q['slot'], 'legend': q.get('legend'), 'self': bool(q.get('self')), 'knocks': 0, 'kills': 0, 'knocked': 0,
          'killed': 0, 'headshotKills': 0, '_h': q['hue']} for q in (squad or []) if q.get('hue') is not None]
    def who(side):
        h = (side or {}).get('hue')
        if h is None or not P: return None
        d = [min(abs(h - q['_h']), 360 - abs(h - q['_h'])) for q in P]
        return P[int(np.argmin(d))] if min(d) <= 20 else None
    seen = []
    for l in lines:
        if not (t0 <= l['t'] < t1) or 'event' not in l: continue
        k = (l.get('killer') or {}).get('side'); v = (l.get('victim') or {}).get('side')
        if k == 'squad' and v == 'squad': continue          # firing range / friendly fire: not a fight
        if k != 'squad' and v != 'squad': continue
        # the feed scrolls and fades, so one line is often captured two or three times: count it once
        if any(l['t'] - p['t'] < 25 and p['event'] == l['event'] and p.get('weapon') == l.get('weapon')
               and _same_sig(p.get('killerSig'), l.get('killerSig')) and _same_sig(p.get('victimSig'), l.get('victimSig')) for p in seen):
            continue
        seen.append(l)
        knock = l['event'] == 'knock'
        if k == 'squad':
            s['squadKnocks' if knock else 'squadKills'] += 1; q = who(l.get('killer'))
            if l.get('headshot'): s['headshotKills'] += 1
            if q:
                q['knocks' if knock else 'kills'] += 1
                if l.get('headshot'): q['headshotKills'] += 1
        if v == 'squad':
            s['squadKnocked' if knock else 'squadKilled'] += 1; q = who(l.get('victim'))
            if q: q['knocked' if knock else 'killed'] += 1
    if P: s['players'] = [{k_: v_ for k_, v_ in q.items() if k_ != '_h'} for q in P]
    return s

# ---------- kill feed / event feed (top right): every new line saved once, read by Claude at import ----------
class Feed:
    """Lines of the feed under 'squads left' (kills, knocks, shield breaks, pings, squad eliminations,
    kill leader). Each new line is kept once as a small colour JPEG + time; text is read at import.
    A line that shows a weapon icon also gets 'weapon' (the game's name for it), 'weaponName', 'weaponScore',
    'arrow' (red arrow after the icon) and 'event' when the weapon icons file is present. The red arrow means a
    knock; a weapon line without it is a kill (Blakey, 14 Nov 2026). Such a line also gets, when they can be told:
    'marker' (arrow / skull / rank), 'headshot' (gold skull: the kill's last bullet was a headshot), and 'killer' /
    'victim' = {'side': 'squad' or 'other'} with the squad colour ('hue', 'color') for our own squad's names."""
    X0, X1, Y0, Y1 = 1820, 2470, 260, 640
    def __init__(self, weapons=None): self.recent = []; self.lines = []; self.weapons = weapons if weapons is not None else Weapons()
    @staticmethod
    def _text(p):
        r, g, b = p[..., 0], p[..., 1], p[..., 2]
        white = (p.min(2) > 222) & (np.ptp(p, axis=2) < 40)
        warm = (r > 210) & (g > 90) & (b < 110) & (r - b > 120)          # orange / yellow highlights
        mx = p.max(2); mn = p.min(2)
        squad = (mx > 170) & ((mx - mn) > 0.45 * mx) & ~((r > 150) & (g < 100) & (b < 100))   # squad-coloured names (teal, blue, green, purple...), not the red knock arrow
        return white | warm | squad
    @staticmethod
    def _texty(mm):
        """Text-shaped band: many short vertical strokes (letters), not a smooth bright stripe."""
        col = mm.any(0); runs = int(np.count_nonzero(col[1:] & ~col[:-1]))
        fill = mm.sum() / max(1, mm.shape[0] * max(1, col.sum()))
        return runs >= 8 and 0.12 <= fill <= 0.6
    def read(self, a, t):
        reg = a[self.Y0:self.Y1, self.X0:self.X1]
        m = self._text(reg.astype(np.int16)); rows = m.sum(1)
        bands = []; cur = None
        for i, v in enumerate(list(rows) + [0]):
            if v > 5 and cur is None: cur = i
            elif v <= 5 and cur is not None:
                if 12 <= i - cur <= 40: bands.append((cur, i))
                cur = None
        self.recent = [r for r in self.recent if t - r[0] < 25]
        for y0, y1 in bands:
            mm = m[y0:y1]; cols = np.nonzero(mm.sum(0))[0]
            if len(cols) < 20 or cols[-1] < mm.shape[1] - 60 or not self._texty(mm): continue   # right-aligned text only
            x0 = max(0, cols[0] - 6); w = mm.shape[1] - x0
            sig = np.array(Image.fromarray((mm[:, x0:] * 255).astype(np.uint8)).resize((64, 6), Image.BILINEAR)) > 50
            dup = False
            for r in self.recent:
                if abs(r[2] - w) < 14 and (r[1] ^ sig).mean() < 0.14: r[0] = t; dup = True; break
            if dup: continue
            self.recent.append([t, sig, w])
            crop = Image.fromarray(reg[max(0, y0 - 5):y1 + 5, x0:].astype(np.uint8))
            crop = crop.resize((max(1, crop.width * 2 // 3), max(1, crop.height * 2 // 3)), Image.LANCZOS)
            b = io.BytesIO(); crop.save(b, 'JPEG', quality=80)
            line = {'t': round(t, 2), 'y': int(self.Y0 + y0), 'jpg': base64.b64encode(b.getvalue()).decode()}
            try:
                full = reg[max(0, y0 - 12):y1 + 12, x0:]
                wp = self.weapons.match(full)
                if wp:
                    arrow = red_arrow(full, wp['x'] + wp['w'], wp['x'] + wp['w'] + 80)
                    line.update(weapon=wp['weapon'], weaponName=WEAPON_NAMES.get(wp['weapon'], wp['weapon'].replace('_', ' ').title()),
                                weaponScore=round(wp['score'], 2), arrow=arrow, event='knock' if arrow else 'kill')
                    try:
                        kp = kill_parts(full, wp)
                        if kp['marker'] == 'arrow' and not arrow: line.update(arrow=True, event='knock')   # arrow fading out
                        if kp['marker'] == 'skull' and not line['arrow']: line['headshot'] = True
                        if kp['marker']: line['marker'] = kp['marker']
                        for key in ('killer', 'victim', 'killerSig', 'victimSig'):
                            if kp.get(key): line[key] = kp[key]
                    except Exception: pass
            except Exception: pass
            self.lines.append(line)


# ---------- legend select screen: start of a game + each squad member's legend ----------
LEGEND_NAMES = {'madmaggie': 'Mad Maggie', 'dummie': 'DUMMIE'}
class Legends:
    """The legend select screen before a game: the squad roster at the top left has one slot per player (a portrait
    in a coloured frame, numbered in pick order). Seeing it marks the start of a game; the portrait in each slot is
    compared with the game's own legend tiles to name the legend.
    The tiles come from the same private file as the weapon icons (keys 'legend_<name>': the middle of each tile,
    grey, at the size the roster draws it on a 2560-wide frame; 0 = transparent). Without them the screen is still
    recognised by its frames, only the legends stay unnamed.
    Measured on real frames: the right legend scores 0.53 to 0.90, the best wrong one 0.49 at most (usually < 0.45)."""
    TOP, STEP = 172, 106.5        # top of slot 1's portrait, distance between slots (px at 1440 high)
    X0, CW, CH = 170, 256, 128    # search window per slot: x from X0, canvas CW x CH from 22 px above the portrait
    MIN, GAP = 0.52, 0.06         # accept: score and lead over the second best
    SHIFTS = (0, -114)            # ranked: a rank badge left of each slot; other modes: none, the roster sits 114 px further left
    ROW = 31                      # row of the window where the portrait sits (the lobby's party list draws them elsewhere)
    def __init__(self, path=None):
        self.T = []
        try:
            z = np.load(path or weapon_file())
            for n in z.files:
                if not n.startswith('legend_'): continue
                t = z[n].astype(np.float64); m = (t > 0).astype(np.float64); k = m.sum()
                if k < 200 or t.shape[0] > self.CH or t.shape[1] > self.CW: continue
                tz = (t - (t * m).sum() / k) * m
                pz = np.zeros((self.CH, self.CW)); pz[:t.shape[0], :t.shape[1]] = tz
                pm = np.zeros((self.CH, self.CW)); pm[:t.shape[0], :t.shape[1]] = m
                self.T.append((n[7:], t.shape, k, float(np.sqrt((tz * tz).sum())) + 1e-6, np.conj(np.fft.rfft2(pz)), np.conj(np.fft.rfft2(pm))))
        except Exception: self.T = []
    @classmethod
    def frames(cls, a):
        """Cheap test (well under 1 ms): which roster slots are on screen. Each slot has a small coloured number
        badge at its lower right corner (always), and its frame takes the same colour once that player is picking
        or has locked in (grey before). -> list of (slot, hue of the badge, frame coloured) for slots 1..3,
        empty when this is not the legend select screen."""
        for dx in cls.SHIFTS:
            out = []
            for k in range(3):
                y0 = int(round(cls.TOP + cls.STEP * k))
                B = a[y0 + 56:y0 + 80, 296 + dx:328 + dx].reshape(-1, 3); bs = B[(B.max(1) - B.min(1) > 100) & (B.max(1) > 140)]
                if len(bs) < 60: break
                hue = _hue(bs)
                L = a[y0 + 35:y0 + 42, 188 + dx:208 + dx].reshape(-1, 3); ls = L[(L.max(1) - L.min(1) > 100) & (L.max(1) > 140)]
                lit = len(ls) >= 6 and min(abs(_hue(ls) - hue), 360 - abs(_hue(ls) - hue)) < 25
                if not lit and (L.min(1) > 150).sum() < 6: break          # grey frame: its bright edge must be there
                out.append((k + 1, int(round(hue)), bool(lit)))
            if out: return out, dx
        return [], 0
    def name(self, a, slot, dx=0):
        """-> (legend, score) for the portrait in this slot, or (None, best score)."""
        if not self.T: return None, 0.0
        y0 = int(round(self.TOP + self.STEP * (slot - 1))) - 22
        g = a[y0:y0 + self.CH, self.X0 + dx:self.X0 + dx + self.CW].astype(np.float64)
        g = 0.299 * g[..., 0] + 0.587 * g[..., 1] + 0.114 * g[..., 2]
        if g.shape != (self.CH, self.CW): return None, 0.0
        G = np.fft.rfft2(g); G2 = np.fft.rfft2(g * g); res = []
        for n, (th, tw), k, tn, F, Fm in self.T:
            s = (self.CH, self.CW); v = (slice(0, self.CH - th + 1), slice(0, self.CW - tw + 1))
            num = np.fft.irfft2(G * F, s=s)[v]; S = np.fft.irfft2(G * Fm, s=s)[v]; S2 = np.fft.irfft2(G2 * Fm, s=s)[v]
            sc = num / (np.sqrt(np.maximum(S2 - S * S / k, k * 25.0)) * tn); i = int(sc.argmax())
            res.append((float(sc.flat[i]), n, i // sc.shape[1], i % sc.shape[1] + tw))
        res.sort(reverse=True)
        ok = res[0][0] >= self.MIN and (len(res) < 2 or res[0][0] - res[1][0] >= self.GAP) and abs(res[0][2] - self.ROW) <= 3
        return (res[0][1] if ok else None), round(res[0][0], 3)
    def read(self, a):
        """a: full frame (1440 x 2560 x 3, 16:9). -> None, or {'squad': [{'slot', 'hue', 'locked', 'self', 'legend', 'score'}]}.
        Without the portraits file the screen needs three slots with three different colours to count."""
        fr, dx = self.frames(a)
        if not fr: return None
        squad = []; whites = []
        for slot, hue, lit in fr:
            y0 = int(round(self.TOP + self.STEP * (slot - 1)))
            nm = a[y0 + 4:y0 + 40, 318 + dx:560 + dx].reshape(-1, 3)
            col = nm[(nm.max(1) > 150) & (nm.max(1) - nm.min(1) > 80)]              # letters in the slot's own colour
            mine = len(col) >= 60 and min(abs(_hue(col) - hue), 360 - abs(_hue(col) - hue)) < 25
            whites.append(int((nm.min(1) > 190).sum()) if mine else 999)            # white name = someone else
            legend, score = self.name(a, slot, dx)
            squad.append({'slot': slot, 'hue': hue, 'locked': lit, 'self': False, 'legend': legend, 'score': score})
        if self.T:
            if not any(s['legend'] for s in squad): return None
        elif len(squad) < 3 or min(min(abs(p['hue'] - q['hue']), 360 - abs(p['hue'] - q['hue']))
                                   for i, p in enumerate(squad) for q in squad[i + 1:]) < 25: return None
        low = [i for i, w in enumerate(whites) if w < 40]       # the player's own name is coloured, not white
        if len(low) == 1 and (len(whites) == 1 or sorted(whites)[1] > 100): squad[low[0]]['self'] = True
        b = a[self.TOP - 2:self.TOP + 82, 72:156].astype(np.float32).mean(2)     # ranked: a rank badge left of each slot
        return {'squad': squad, 'ranked': bool(dx == 0 and b.std() > 35)}

def select_runs(events, gap=5.0, min_s=8.0, min_frames=5, max_s=120.0):
    """Keep only the legend select reads that form a real screen: seen for 8 s to 2 min without a hole
    (the screen lasts about 25 s; a look-alike in the lobby or one odd frame in a game must not start a game)."""
    runs = []
    for e in events:
        if e['kind'] != 'select': continue
        if runs and e['t'] - runs[-1][-1]['t'] <= gap: runs[-1].append(e)
        else: runs.append([e])
    bad = {id(e) for r in runs if not (min_s <= r[-1]['t'] - r[0]['t'] <= max_s and len(r) >= min_frames) for e in r}
    return [e for e in events if id(e) not in bad]

def new_select(d, t):
    """First frame of a legend select screen."""
    sel = {'tFirst': t, 'tLast': t, 'frames': 1, 'squad': [dict(x) for x in d['squad']], 'ranked': bool(d.get('ranked')),
           '_rk': int(bool(d.get('ranked'))), '_own': {x['slot']: int(x['self']) for x in d['squad']}}
    return sel

def merge_select(sel, d, t):
    """Add one more frame of the same legend select screen. Before a player's turn their slot shows a legend they
    may not end up with, so a read with the coloured frame (picking or locked in) beats one without, and among
    equals the latest wins. 'self' (which slot is the recording player) and 'ranked' are votes over all frames."""
    sel['tLast'] = t; sel['frames'] += 1; sel['_rk'] += int(bool(d.get('ranked')))
    have = {x['slot']: x for x in sel['squad']}
    for x in d['squad']:
        sel['_own'][x['slot']] = sel['_own'].get(x['slot'], 0) + int(x['self'])
        o = have.get(x['slot'])
        if o is None: sel['squad'].append(dict(x)); continue
        if x['legend'] and (x['locked'] or not o['locked'] or not o['legend']): o.update(x)
    sel['squad'].sort(key=lambda x: x['slot'])
    top = max(sel['_own'].values())
    for x in sel['squad']: x['self'] = top >= 2 and sel['_own'].get(x['slot'], 0) == top and list(sel['_own'].values()).count(top) == 1
    sel['ranked'] = sel['_rk'] * 2 >= sel['frames']
    return sel


# ---------- loading banner before a game: the mode and the map in big white letters, top left ----------
try: BANNER_T = json.load(open(os.path.join(HERE, 'pcs_banner.json')))
except Exception: BANNER_T = {'mode': {}, 'map': {}}
class Banner:
    """While players connect (the 20 to 40 s before the legend select screen) a banner at the top left gives the
    mode ("RANKED", "WILDCARD") and under it the map ("STORM POINT"). Each word is recognised by its shape: the
    white letters, squeezed to 10 x 60 cells, against the shapes in pcs_banner.json (a table we built from our own
    recordings, in English; a word not in the table is returned as its shape so it can be named later)."""
    ROWS = {'mode': (138, 198), 'map': (273, 298)}
    X0 = 180
    @staticmethod
    def shape(m):
        """White letters (rows x cols of True/False) -> (150 hex digits, width / height) or None."""
        ys = np.where(m.sum(1) > 1)[0]; xs = np.where(m.sum(0) > 0)[0]
        if len(ys) < 8 or len(xs) < 40: return None
        m = m[ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1].astype(np.uint8) * 255
        g = np.asarray(Image.fromarray(m).resize((60, 10), Image.BOX)) > 110
        return ''.join('%x' % int(''.join('1' if v else '0' for v in g.flat[i:i + 4]), 2) for i in range(0, 600, 4)), round(m.shape[1] / m.shape[0], 2)
    @staticmethod
    def near(sig, ratio, table):
        best = None
        for word, L in table.items():
            for s2, r2 in L:
                if abs(r2 - ratio) > 0.08 * r2: continue
                d = bin(int(sig, 16) ^ int(s2, 16)).count('1')
                if best is None or d < best[1]: best = (word, d)
        return best[0] if best and best[1] <= 60 else None       # 600 cells: a tenth may differ
    @classmethod
    def read(cls, a):
        """a: full 16:9 frame. -> None or {'mode', 'modeSig', 'map', 'mapSig'} (a name is None when the word is unknown)."""
        if (a[150:190:4, 20:60:4].max(2) > 215).mean() > 0.2: return None       # the plate left of the icon is never white
        out = {}
        for key, (y0, y1) in cls.ROWS.items():
            m = a[y0:y1, cls.X0:1100].min(2) > 215; col = m.sum(0) > 0; xs = np.where(col)[0]
            if not len(xs) or not (6 <= xs[0] <= 22): return None                # both words start at the same column
            end = xs[0]; gap = 0
            for x in range(xs[0], len(col)):                                     # the word ends at the first wide gap
                if col[x]: end = x; gap = 0
                else:
                    gap += 1
                    if gap >= 30: break
            m = m[:, xs[0]:end + 1]
            if m.sum(1).min() == 0 and key == 'mode': return None                # capital letters fill every row
            if m.mean() < 0.25 or m.mean() > 0.75: return None
            sh = cls.shape(m)
            if not sh: return None
            out[key] = cls.near(sh[0], sh[1], BANNER_T.get(key, {})); out[key + 'Sig'] = [sh[0], sh[1]]
        return out


# ---------- "N SQUADS LEFT" (top right during a game) ----------
class SquadsLeft:
    """The number in front of "SQUADS LEFT": white digits 22 px high, right-aligned just left of the words.
    The words must be there (their box is 17 to 33 % white, starting and ending where they always do), then each
    digit is compared with the shapes in pcs_banner.json ('digits': 12 x 16 cells each, from our own recordings)."""
    T = [(d, int(h, 16)) for d, L in BANNER_T.get('digits', {}).items() for h in L]
    @classmethod
    def read(cls, a):
        """a: full 16:9 frame. -> the number (1..60) or None."""
        if not cls.T: return None
        w = a[77:99, 2138:2294].min(2) > 232; c = w.sum(0) > 0
        if not (0.17 < w.mean() < 0.33) or c[:2].any() or not c[2:4].any() or not c[-4:].any(): return None
        m = a[77:99, 2040:2134].min(2) > 232; col = m.sum(0) > 0; x = len(col) - 1; out = ''
        while x >= 0 and len(out) < 2:              # from the right: at most two digits, then nothing for 12 columns
            if not col[x]:
                if out and not col[max(0, x - 12):x + 1].any(): break
                x -= 1; continue
            e = x
            while x >= 0 and col[x]: x -= 1
            g = m[:, x + 1:e + 1]; ys = np.where(g.sum(1) > 0)[0]; g = g[ys[0]:ys[-1] + 1]
            if not (19 <= g.shape[0] <= 22 and 4 <= g.shape[1] <= 18): return None
            n = np.asarray(Image.fromarray(g.astype(np.uint8) * 255).resize((12, 16), Image.BOX)) > 110
            v = int(''.join('1' if b else '0' for b in n.flat), 2)
            d, dist = min(((d, bin(v ^ t).count('1')) for d, t in cls.T), key=lambda r: r[1])
            if dist > 26: return None
            out = d + out
        if not out or col[max(0, x - 12):x + 1].any(): return None       # something else left of the number: not a clean read
        n = int(out)
        return n if 1 <= n <= 60 else None

def squads_timeline(reads, t0, t1):
    """reads: [(t, n)] over the recording. -> [[t, n]]: when each new, lower count was first seen in this game.
    The count only goes down during a game; a value counts once it was read twice within 30 s, so one bad read
    never moves it. The first entry is the number of squads at the start."""
    R = [(t, n) for t, n in reads if t0 <= t < t1]; out = []
    for i, (t, n) in enumerate(R):
        if out and n >= out[-1][1]: continue
        if any(n2 == n and 0 < t2 - t <= 30 for t2, n2 in R[i + 1:i + 40]): out.append([round(t, 2), n])
    return out


# ---------- game mode: ranked badge (RP panel, top right) ----------
try: RANK_T = np.load(os.path.join(HERE, 'rank_rp.npy')).astype(np.float32)
except Exception: RANK_T = None
def ranked(a):
    """True when the ranked RP panel (top-right, under the rank badge) is on screen."""
    if RANK_T is None: return False
    g = a[120:190, 2480:2545].astype(np.float32).mean(2); T = RANK_T - RANK_T.mean(); tn = np.sqrt((T * T).sum())
    best = -1
    for dy in (0, 4, 8):
        for dx in (0, 4, 8):
            x = g[dy:dy + T.shape[0], dx:dx + T.shape[1]]
            if x.shape != T.shape: continue
            x = x - x.mean(); best = max(best, float((x * T).sum() / (np.sqrt((x * x).sum()) * tn + 1e-9)))
    return best > 0.5
