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

# ---------- kill feed / event feed (top right): every new line saved once, read by Claude at import ----------
class Feed:
    """Lines of the feed under 'squads left' (kills, knocks, shield breaks, pings, squad eliminations,
    kill leader). Each new line is kept once as a small colour JPEG + time; text is read at import."""
    X0, X1, Y0, Y1 = 1820, 2470, 260, 640
    def __init__(self): self.recent = []; self.lines = []
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
            self.lines.append({'t': round(t, 2), 'y': int(self.Y0 + y0), 'jpg': base64.b64encode(b.getvalue()).decode()})


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
