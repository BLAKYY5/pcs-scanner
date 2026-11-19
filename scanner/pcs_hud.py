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
CAL = {'WE': (914.2, 49.46, 885.1, 49.82), 'SP': (1024.1, 49.17, 1070.4, 50.63)}   # ED: below (game transform)

def _game_tx(pos_x, pos_y, scale):
    """The game's own minimap transform (px = (x - pos_x) / scale on its 4096 px minimap) as map %. The app's map
    pictures are framed like the game's minimap (checked Nov 2026: own-arrow / POS pairs on BM and ED, POI centres
    on the minimap pictures of BM, KC, SP)."""
    s = scale * 40.96
    return (s, -pos_x / s, s, pos_y / s)

# Broken Moon: the game transform, checked on 12 own-arrow / POS pairs from 3 games (Nov 2026): residual rms 0.75 % (x)
# and 0.48 % (y), mean offset under 0.3 %. A free fit on the same pairs moves the 9 known landings by 0.1 to 1.3 %.
CAL['BM'] = _game_tx(-45000, 43500, 21)
# E-District: the game transform since 16 Nov 2026 (coach's decision). On 13 own-arrow / POS pairs from 6 games
# (3, 4 Nov scans; 19 pairs built from the 3, 4, 15 Nov scans, 6 arrow outliers rejected) it fits better than the Oct fit: rms 0.33 % (x) / 0.49 % (y) against
# 0.74 / 0.69 %, mean offset under 0.1 % against 0.55 / 0.50 %. It moves points up to 3 % in x near the map edges;
# none of the 16 known ED landings (3, 4, 15 Nov) changes place. Old Oct fit: (902.9, 49.03, 861.1, 50.39).
CAL['ED'] = _game_tx(-43008, 43008, 21)
# Kings Canyon: the game transform only, no own-arrow pairs yet (the map's POI centres sit on the game minimap's
# buildings, so the framing matches). Its landings carry mapCalibrated = False until pairs confirm it.
# Olympus: left out (its minimap picture isn't identified, so the framing can't be checked); its game transform
# would be _game_tx(-52024, 48025, 22).
CAL['KC'] = _game_tx(-37541, 43886, 20)
CAL_UNCHECKED = {'KC'}

def minmax(s):
    """Per-pixel min and max over the colour channels, as int16. Same values as s.min(2) / s.max(2), but numpy's
    reduction over a 3-long last axis is slow (about 8x slower than combining the three channel planes)."""
    r, g, b = s[..., 0], s[..., 1], s[..., 2]
    return (np.minimum(np.minimum(r, g), b).astype(np.int16), np.maximum(np.maximum(r, g), b).astype(np.int16))

def white(s, lo=180):
    if LOOSE: lo = min(lo, 165)
    mn, mx = minmax(s)
    return (mn > lo) & ((mx - mn) < 50)

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
        m = white(a[449 + off:478 + off, :400])     # the clock's columns only (digits to x 375, label from x 60)
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
        cmn, cmx = minmax(c)
        icon = (cmn > 190) & ((cmx - cmn) < 40)
        ring_w = icon.sum()
        if orange.sum() < 30 and not (reddish.sum() >= 30 and ring_w >= 40) and not (LOOSE and ring_w >= 150): continue
        mn, mx = minmax(a[422 + off:460 + off, :400])     # the clock's columns only (digits to x 345)
        m = (mn > (165 if LOOSE else 190)) & ((mx - mn) < 40)
        d = [_glyph(m[:, x0:x1], PRE_T, 24, 34) for x0, x1 in (_runs(m, 195, 345, 4) if LOOSE else PSLOTS)]
        if None in d or d[2] > 5: return None
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

# POS line: exactly three numbers from the line's start. The decimal point is often read as '-' or '?', the gap before
# a minus as '-'. Anything else unreadable makes the line unread instead of wrong (Nov 2026, checked against the
# minimap position): a '?' at the start is a lost minus sign or first digit (?1607 for 11607), a '?' inside the
# digits split a number (15?63 for 15863.63; -7?77-31-27333 for -7377.31 27333). Wrong POS reads 1.9 % -> 0.4 %, reads kept 89 % -> 83 %.
_POSN = r'(-?\d{1,6})[.\-?](\d\d)'
POSRX = re.compile(r'^ *' + _POSN + r'[ -]' + _POSN + r'[ -]' + _POSN + r'(?!\d)')   # a gap cell between the numbers

def read_dbg(a):
    m = white(a[40:110, 0:300], 170)
    p = POSRX.match(_dbg_line(m, 22))
    pos = [float(p.group(2 * i + 1) + '.' + p.group(2 * i + 2)) for i in range(3)] if p else None
    vel = [float(x + '.' + y) for x, y in NUMRX.findall(_dbg_line(m, 50))]
    return pos, (vel[0] if vel else None)

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
    neighbours (+-5 samples, similar magnitude) agree on; not within 3000 units of 0, where the player can really be
    on either side (a landing at x 748 was turned to -748)."""
    P = [s for s in samples if s[4] is not None]
    for k in (4, 5):
        signs = [1 if s[k] >= 0 else -1 for s in P]
        for i in range(len(P)):
            a = abs(P[i][k]); vote = 0
            if a < 3000: continue        # near 0 the player really crosses between signs: the neighbours can't tell
            for j in range(max(0, i - 5), min(len(P), i + 6)):
                if j != i and abs(abs(P[j][k]) - a) < 3000: vote += signs[j]
            if vote and (1 if vote > 0 else -1) != signs[i]: P[i][k] = -P[i][k]
    return samples

def landing(samples, t0):
    """Own view only. On the dropship VEL reads 0.00; jumping makes it > 0; free fall > 600.
    exit = first moving sample of the run that leads into free fall; touchdown = height stops dropping.
    VEL counts from frames whose POS line was not read too (the POS reader refuses doubtful lines)."""
    P = [(s[0], s[4], s[5], s[6], s[7]) for s in samples if s[0] >= t0 and not s[3] and (s[4] is not None or s[7] is not None)]
    if sum(1 for p in P if p[1] is not None) < 4: return None
    fall = next((i for i in range(1, len(P)) if (P[i][4] or 0) > 600), None)
    if fall is None: return None
    ex = fall
    while ex > 0 and (P[ex - 1][4] is None or P[ex - 1][4] > 50): ex -= 1   # unreadable speed: keep walking back
    Z = [i for i in range(fall, len(P)) if P[i][1] is not None]            # touchdown: frames with a position
    land = None
    for a, b in zip(Z, Z[1:]):
        dz = (P[b][3] - P[a][3]) / max(P[b][0] - P[a][0], 0.2)
        if dz > -60 and (P[a][4] or 0) < 600: land = a; break
    if land is None: return None
    prev = max((i for i in range(land) if P[i][1] is not None), default=land)
    return {'exitWindow': [round(P[ex - 1][0], 2) if ex > 0 else None, round(P[ex][0], 2)], 'exitAt': round(P[ex][0], 2),
            'exitZ': P[ex][3], 'landWindow': [round(P[prev][0], 2), round(P[land][0], 2)], 'landedAt': round(P[land][0], 2),
            'x': P[land][1], 'y': P[land][2], 'z': P[land][3]}

# Scan on the team server (a keyframes-only copy of the recording, see clip_for): the 60 fps reads below need the
# frames between keyframes, so they come from short clips the player's app cuts (stream copy) and sends afterwards.
CLIPS = None   # folder of those clips (None = an ordinary scan of the whole recording)
NEEDS = []     # windows [a, b] a 60 fps read wanted while no clip covered it (sent back to the app)
_clip_list = None

def _start_time(f):
    o = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=start_time,duration', '-of', 'csv=p=0', f],
                                creationflags=0x08000000 if os.name == 'nt' else 0)
    return tuple(float(x) for x in o.decode().strip().split(',')[:2])

def _clips(path):
    """[(clip, start, end)] in the recording's own time (0 = its first frame), cut with -copyts from the recording."""
    global _clip_list
    if _clip_list is None:
        _clip_list = []
        try: base = _start_time(path)[0]          # the keyframes-only copy keeps the recording's times (-copyts)
        except Exception: base = 0.0
        for n in sorted(os.listdir(CLIPS)) if CLIPS and os.path.isdir(CLIPS) else []:
            if not n.lower().endswith(('.mp4', '.mkv', '.mov')): continue
            f = os.path.join(CLIPS, n)
            try:
                st, du = _start_time(f)
                _clip_list.append((f, st - base, st - base + du))
            except Exception:
                continue
    return _clip_list

def clip_for(path, a, b):
    """The clip (path, start in recording time) holding every frame from a to b, or None: the window goes to NEEDS.
    A clip is cut with -copyts, so its times are the recording's; '-ss x' on it seeks to start + x, and the decoder
    starts from the same keyframe as on the whole recording: the frames are the same."""
    for f, st, en in _clips(path):
        if st <= max(0, a) and en >= b: return f, st
    if [round(a, 3), round(b, 3)] not in NEEDS: NEEDS.append([round(a, 3), round(b, 3)])
    return None

def _corner(path, a, b, W=2560, H=1440, cw=420, ch=570):
    """Every frame (60 fps) between a and b, top-left corner only (debug overlay + ring clock live there)."""
    src, ss = path, max(0, a)
    if CLIPS is not None:
        c = clip_for(path, a, b)
        if c is None: return
        src, ss = c[0], max(0, a) - c[1]
    cmd = ['ffmpeg', '-v', 'error'] + DEC + ['-ss', f'{ss:.3f}', '-i', src, '-t', f'{b - a:.3f}',
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
    def _integral(a):
        return np.pad(a, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    @staticmethod
    def _box(a, h, w, c=None):
        """Sums of every h x w window of a; c: a's integral image (_integral), computed once for all icons."""
        if c is None: c = Weapons._integral(a)
        return c[h:, w:] - c[:-h, w:] - c[h:, :-w] + c[:-h, :-w]
    def match(self, rgb):
        """rgb: one feed line at full size (rows x cols x 3). -> None or {'weapon', 'score', 'x', 'y', 'w', 'h'}."""
        if not self.T: return None
        g = np.asarray(rgb, np.float32).min(2)[:self.FH, :self.FW]     # white icon stays bright, coloured names drop
        H, Wd = g.shape; c = np.zeros((self.FH, self.FW), np.float64); c[:H, :Wd] = g
        G = np.fft.rfft2(c); best = None; good = []
        I1 = self._integral(c[:H, :Wd]); I2 = self._integral(c[:H, :Wd] ** 2)     # once per line, not per icon
        for n, (th, tw), tn, F in self.T:
            if th > H or tw > Wd: continue
            num = np.fft.irfft2(G * F, s=(self.FH, self.FW))[:H - th + 1, :Wd - tw + 1]
            k = th * tw; S = self._box(None, th, tw, I1); S2 = self._box(None, th, tw, I2)
            sc = num / (np.sqrt(np.maximum(S2 - S * S / k, k * self.STD ** 2)) * tn)
            i = int(sc.argmax()); y, x = divmod(i, sc.shape[1])
            r = {'weapon': n, 'score': float(sc[y, x]), 'x': x, 'y': y, 'w': tw, 'h': th}
            if r['score'] >= self.MIN: good.append(r)
            if best is None or r['score'] > best['score']: best = r
        if not best or best['score'] < self.MIN: return None
        # A dual-wield icon is the single one drawn twice, so the single icon also fits a dual line (and can score
        # a hair higher). When a clearly bigger icon fits the same spot almost as well, it is the bigger one.
        for r in sorted(good, key=lambda r: -r['w'] * r['h']):
            if r['w'] * r['h'] >= 1.4 * best['w'] * best['h'] and r['score'] >= best['score'] - 0.06 \
               and r['x'] <= best['x'] + 2 and r['x'] + r['w'] >= best['x'] + best['w'] - 2: return r
        return best

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
        mn, mx = minmax(p)
        white = (mn > 222) & ((mx - mn) < 40)
        warm = (r > 210) & (g > 90) & (b < 110) & (r - b > 120)          # orange / yellow highlights
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
    LIT = (20, 80)                # frame coloured: pixels of its left edge window (of 140) within RGB distance 60 of the badge colour
    BADGE = 680                   # a badge (with its digit) colours at most 620 of its window's 768 pixels; more = a coloured background
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
        empty when this is not the legend select screen.
        The frame counts as coloured when enough pixels of its left edge have the badge's own colour (RGB distance):
        the window also holds the screen's background, which is often a saturated orange, so a median hue over the
        window drifted between the two colours and a frame read as coloured or not by a few degrees, which the CPU and
        GPU decoders did not agree on (a locked legend could be missed on every frame, or an orange player's grey frame
        read as coloured). Measured: coloured frame 33 to 60 of the 140 pixels, grey frame 0 to 2; 90 or more = the
        whole window is the background's colour. Same for the badge: an orange background fills the whole badge window
        at the wrong roster position (ranked vs other modes), a real badge with its digit at most 620 of 768 pixels."""
        for dx in cls.SHIFTS:
            out = []
            for k in range(3):
                y0 = int(round(cls.TOP + cls.STEP * k))
                bs = cls._badge(a, y0, dx)
                if not 60 <= len(bs) <= cls.BADGE: break
                L = a[y0 + 35:y0 + 42, 188 + dx:208 + dx].reshape(-1, 3)
                lit = cls.LIT[0] <= cls._near(L, bs) <= cls.LIT[1]          # the frame's edge is a stripe, not the whole window
                if not lit and (L.min(1) > 150).sum() < 6: break          # grey frame: its bright edge must be there
                out.append((k + 1, int(round(_hue(bs))), bool(lit)))
            if out: return out, dx
        return [], 0
    @staticmethod
    def _badge(a, y0, dx):
        """Coloured pixels of a slot's number badge (n x 3)."""
        B = a[y0 + 56:y0 + 80, 296 + dx:328 + dx].reshape(-1, 3)
        return B[(B.max(1) - B.min(1) > 100) & (B.max(1) > 140)]
    @staticmethod
    def _near(px, badge):
        """How many of the pixels px (n x 3) have the badge's colour (RGB distance < 60 to its median)."""
        return int((((px.astype(np.float32) - np.median(badge, 0).astype(np.float32)) ** 2).sum(1) < 60 ** 2).sum())
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
            # letters in the slot's own colour: own name 211 to 600 such pixels, white names 0 (the background behind
            # the names is often orange too, which is why this compares with the badge's colour, not a median hue)
            mine = self._near(nm, self._badge(a, y0, dx)) >= 100
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


# ---------- playlist emblem (top right: pubs, Wildcard) ----------
def _emblem_table(table):
    """pcs_banner.json 'emblems': {asset id: {'mode', 'rgb': 768 hex (16 x 16 x RGB, 4 bits), 'mask': 64 hex}}."""
    out = []
    for key, e in table.get('emblems', {}).items():
        rgb = np.array([int(c, 16) * 17 for c in e['rgb']], np.float32).reshape(16, 16, 3)
        mask = np.array([b == '1' for c in e['mask'] for b in format(int(c, 16), '04b')]).reshape(16, 16)
        out.append((key, e.get('mode'), rgb, mask))
    return out

class ModeEmblem:
    """The playlist's round emblem at the top right: in a game (x 2422..2539, y 8..123 at 1440p, under the minimised
    counters) and on the "waiting for players" screen (134 px lower). Pubs shows the red "unranked" emblem, Wildcard a
    teal one, ranked a rank badge instead (not an emblem: ranked is told by the RP panel, `ranked`). The emblem is
    squeezed to 16 x 16 colour cells and compared, on the cells the emblem fully covers, with the cells derived from
    the game's own emblem images (pcs_banner.json 'emblems', built by tools/mode_emblems.py; the images are not in the
    repo). Measured: the right emblem 9..14 apart (mean of |RGB| on 0..255), any other emblem, rank badge or scene 41
    or more. Small shifts are searched (the HUD sits a few px differently between decoders and screens)."""
    T = _emblem_table(BANNER_T)
    ANCHORS = ((8, 2422), (142, 2420))      # (y, x) of the emblem's top left: in game, waiting screen
    SIZE = (116, 118)                       # rows, columns
    ACCEPT, MARGIN = 24.0, 12.0
    SHIFTS = (-4, 0, 4)
    @classmethod
    def read(cls, a):
        """a: full 16:9 frame. -> (asset id, mode or None, distance) or None."""
        if not cls.T: return None
        h, w = cls.SIZE; best = None
        for y0, x0 in cls.ANCHORS:
            for dy in cls.SHIFTS:
                for dx in cls.SHIFTS:
                    y, x = y0 + dy, x0 + dx
                    if y < 0: continue
                    c = a[y:y + h, x:x + w]
                    if c.shape[:2] != (h, w): continue
                    # the emblem is a filled disc: its centre is never near-white or near-black in all channels
                    m = c[h // 2 - 8:h // 2 + 8, w // 2 - 8:w // 2 + 8]
                    if m.max() < 40: continue
                    s = np.asarray(Image.fromarray(np.ascontiguousarray(c)).resize((16, 16), Image.BOX)).astype(np.float32)
                    for key, mode, rgb, mask in cls.T:
                        d = float(np.abs(s - rgb)[mask].mean())
                        if best is None or d < best[2]: best = (key, mode, d, y, x)
                    if best and best[2] < cls.ACCEPT / 2: break     # a clear match: no need to look further
        if not best or best[2] > cls.ACCEPT: return None
        # the runner-up must be another emblem clearly further away (at the same place)
        y, x = best[3], best[4]
        s = np.asarray(Image.fromarray(np.ascontiguousarray(a[y:y + h, x:x + w])).resize((16, 16), Image.BOX)).astype(np.float32)
        other = min((float(np.abs(s - rgb)[mask].mean()) for key, mode, rgb, mask in cls.T if key != best[0]), default=999)
        if other - best[2] < cls.MARGIN: return None
        return best[0], best[1], round(best[2], 1)

def emblem_vote(reads, lo, hi, need=3, share=0.9):
    """Emblem reads [(t, asset id, mode, distance)] between lo and hi -> (asset id or None, mode or None, counts).
    One emblem must be read at least `need` times and make `share` of the reads, else nothing is told."""
    C = {}
    for t, key, mode, d in reads:
        if lo <= t < hi: C[key] = C.get(key, 0) + 1
    if not C: return None, None, C
    key = max(C, key=C.get); n = sum(C.values())
    if C[key] < need or C[key] < share * n: return None, None, C
    mode = next((m for k, m, _, _ in ModeEmblem.T if k == key), None)
    return key, mode, C


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

def _strip_table(table):
    """pcs_banner.json 'strip': {font: [[label, 216 hex digits (12 x 18 grey cells, 0..f), width in px], ...]}."""
    out = {}
    for f, L in table.get('strip', {}).items():
        out[f] = (np.array([r[0] for r in L]), np.array([[int(c, 16) / 15.0 for c in r[1]] for r in L], np.float32),
                  np.array([r[2] for r in L], np.float32))
    return out

_LIN = {}
def _lin(n, m):
    """n x m matrix that resamples m values to n (linear, pixel centres aligned): a cheap bilinear resize."""
    if (n, m) not in _LIN:
        R = np.zeros((n, m), np.float32); x = (np.arange(n) + 0.5) * m / n - 0.5
        x0 = np.clip(np.floor(x).astype(int), 0, m - 1); x1 = np.clip(x0 + 1, 0, m - 1); f = np.clip(x - x0, 0, 1)
        np.add.at(R, (np.arange(n), x0), 1 - f); np.add.at(R, (np.arange(n), x1), f)
        _LIN[(n, m)] = R
    return _LIN[(n, m)]

class OwnStats:
    """The recording player's own numbers in the strip under "SQUADS LEFT": kills (skull), assists (handshake),
    knocks (dark digits on a white plate, ranked) and damage (its own plate, right).

    Nothing is at a fixed place: the strip is built from the right (the damage plate's digits end at x = 2336, the
    plate grows to the left with the number), and the other plates come and go (kills + assists once one is not zero,
    the knocks plate in ranked, none of them in pubs where kills + assists use the small digits). So the strip is
    parsed from what is on screen: in rows 131..157, white shapes (brighter than the plate behind them, measured in
    the plate rows above and below the digits) and dark shapes on bright plates are cut into blobs, and each number is
    found next to its icon: damage right of the damage icon (37 x 24, digits ending at 2330..2342), assists right of
    the handshake (38 x 27), kills ending 16..34 px before the handshake and right of the skull, knocks right of the
    dark knocks icon (36 x 26). Three digit sizes: 'B' 22 px white (ranked kills / assists), 'S' 17 px white (damage,
    pubs kills / assists), 'K' 16 px dark (knocks).

    Each digit is cut at its own contrast (ink between the plate behind it and the brightest stroke, so thin strokes
    survive a bright scene behind the translucent plate), squeezed to 12 x 18 grey cells and compared with the shapes in
    pcs_banner.json ('strip': several per digit and size, averaged from our own recordings, labelled from frames read
    by eye). A digit counts only when the nearest shape is close AND clearly closer than any other digit's; one
    doubtful digit = no number. Shapes that are not digits (pieces of icons, plate edges) are in the table as 'X'."""
    Y0, Y1, X0, X1 = 128, 162, 1900, 2380
    REF = [1, 2, 3, 28, 29, 30]              # rows 129..131 and 156..158 (of the band): plate, no digits
    T = _strip_table(BANNER_T)               # font -> (labels, shapes n x 216, widths)
    ACCEPT, MARGIN = 0.16, 0.03             # mean grey difference to the nearest shape; lead over another digit
    @staticmethod
    def _medfilt(v, k=15, step=3):
        """Running median over k columns, taken every 'step' columns (the plate changes slowly)."""
        p = np.pad(v, k // 2, mode='edge')
        w = np.lib.stride_tricks.sliding_window_view(p, k)[::step]
        return np.repeat(np.partition(w, k // 2, axis=1)[:, k // 2], step)[:len(v)]
    @staticmethod
    def _runs(m, x0, y0):
        """Runs of columns with ink -> [(x0, x1, y0, y1)] (absolute, end excluded)."""
        col = m.any(0)
        d = np.diff(np.concatenate(([0], col.view(np.int8), [0])))
        st = np.flatnonzero(d == 1); en = np.flatnonzero(d == -1)
        if not len(st): return []
        top = np.where(col, m.argmax(0), 999); bot = np.where(col, m.shape[0] - m[::-1].argmax(0), 0)
        t = np.minimum.reduceat(top, st); b = np.maximum.reduceat(bot, st)
        return [(x0 + int(s), x0 + int(e), y0 + int(u), y0 + int(v)) for s, e, u, v in zip(st, en, t, b)]
    @classmethod
    def white(cls, a):
        """White shapes brighter than the plate behind them (measured per column in the plate rows without digits;
        not on a bright plate) -> blobs (x0, x1, y0, y1) sorted by x."""
        b = a[cls.Y0:cls.Y1, cls.X0:cls.X1]
        mn = np.minimum(np.minimum(b[..., 0], b[..., 1]), b[..., 2]).astype(np.int16)     # (much faster than .min(2))
        bg = cls._medfilt(np.median(mn[cls.REF], axis=0))
        return cls._split(((mn > bg + np.maximum(35, (255 - bg) * 0.5)) & (bg <= 185))[3:30], False)
    @classmethod
    def dark(cls, a):
        """Dark shapes on a bright plate (the knocks plate, white or tinted) -> blobs sorted by x."""
        b = a[cls.Y0:cls.Y1, cls.X0:cls.X1]
        mx = np.maximum(np.maximum(b[..., 0], b[..., 1]), b[..., 2]).astype(np.int16)
        hi = cls._medfilt(np.median(mx[cls.REF], axis=0))
        return cls._split(((mx < (hi + 30) / 2) & (hi > 190))[3:30], True)
    @classmethod
    def blobs(cls, a):
        return cls.white(a), cls.dark(a)
    @classmethod
    def _split(cls, m, dark):
        """Blobs of the mask (rows 131..157); two digits that touch (71, 11...) are cut where the ink is thinnest."""
        out = []
        for b in cls._runs(m, cls.X0, cls.Y0 + 3):
            w = b[1] - b[0]; h = b[3] - b[2]
            if (14 <= h <= 24) and (16 if h < 20 else 19) <= w <= 34 and cls.kind((b[0], b[0] + 8, b[2], b[3]), dark):
                ink = m[:, b[0] - cls.X0 + 4:b[1] - cls.X0 - 4].sum(0)
                x = b[0] + 4 + int(ink.argmin())
                for x0, x1 in ((b[0], x), (x + 1, b[1])):
                    ys = np.where(m[:, x0 - cls.X0:x1 - cls.X0].any(1))[0]
                    if len(ys): out.append((x0, x1, cls.Y0 + 3 + int(ys[0]), cls.Y0 + 4 + int(ys[-1])))
            else: out.append(b)
        return out
    @staticmethod
    def kind(b, dark=False):
        """'S' / 'B' / 'K' for a digit-sized blob, else None."""
        w = b[1] - b[0]; h = b[3] - b[2]
        if dark: return 'K' if 14 <= h <= 18 and 3 <= w <= 14 and 133 <= b[2] <= 138 else None
        if 15 <= h <= 19 and 3 <= w <= 15 and 133 <= b[2] <= 138: return 'S'
        if 20 <= h <= 24 and 3 <= w <= 18 and 130 <= b[2] <= 135: return 'B'
        return None
    @staticmethod
    def _icon(b, w0, w1, h0, h1, dark=False):
        return b is not None and OwnStats.kind(b, dark) is None and w0 <= b[1] - b[0] <= w1 and h0 <= b[3] - b[2] <= h1
    @classmethod
    def _chain(cls, L, j, step, font, dark=False, maxn=4):
        """Digit-sized blobs from L[j] on (step +1: to the right, -1: to the left), at most 6 px apart."""
        out = [L[j]]; k = j + step
        while 0 <= k < len(L) and cls.kind(L[k], dark) == font and len(out) <= maxn:
            gap = L[k][0] - out[-1][1] if step > 0 else out[-1][0] - L[k][1]
            if gap > 6: break
            out.append(L[k]); k += step
        return out if step > 0 else out[::-1]
    @classmethod
    def layout(cls, a):
        """-> {field: (font, [blob, ...])}: where each number of the strip is, from the icons on screen."""
        Wl = cls.white(a); out = {}
        di = [b for b in Wl if cls._icon(b, 34, 41, 22, 27) and 2235 <= b[0] <= 2280]
        if not di: return out                                         # no damage plate: no strip
        Dl = cls.dark(a)
        dic = di[-1]; j = Wl.index(dic)
        if j + 1 < len(Wl) and cls.kind(Wl[j + 1]) == 'S' and Wl[j + 1][0] - dic[1] <= 6:
            ch = cls._chain(Wl, j + 1, 1, 'S')
            if 2330 <= ch[-1][1] <= 2342: out['damage'] = ('S', ch)
        for j, b in enumerate(Wl):                                    # assists, then kills left of the handshake
            if b[1] > dic[0]: break
            if not (cls._icon(b, 35, 41, 24, 28) and j + 1 < len(Wl) and Wl[j + 1][0] - b[1] <= 6): continue
            font = cls.kind(Wl[j + 1])
            if font not in ('S', 'B'): continue
            ch = cls._chain(Wl, j + 1, 1, font, maxn=2)
            if ch[-1][1] < dic[0]: out['assists'] = (font, ch)
            ks = [m for m in range(j) if cls.kind(Wl[m]) == font and 16 <= b[0] - Wl[m][1] <= 34]
            if ks:                                                    # the skull (crowned from 4 kills): 4..14 px left
                kc = cls._chain(Wl, ks[-1], -1, font, maxn=2); m = Wl.index(kc[0]) - 1
                if len(kc) > 1 and kc[0][1] - kc[0][0] >= 8 and cls.digit(a, kc[0], font)[0] == 'X': kc = kc[1:]; skull = True
                else:                                                 # the skull may fall apart in pieces (its eyes)
                    q = m
                    while q > 0 and Wl[q][0] - Wl[q - 1][1] <= 12: q -= 1
                    skull = m >= 0 and 4 <= kc[0][0] - Wl[m][1] <= 14 and Wl[m][1] - Wl[q][0] >= 16 and max(Wl[x][3] - Wl[x][2] for x in range(q, m + 1)) >= 20
                if skull: out['kills'] = (font, kc)
        for j, b in enumerate(Dl):                                    # knocks: right of the dark knocks icon
            if b[1] > dic[0]: break
            if cls._icon(b, 33, 38, 24, 28, True) and j + 1 < len(Dl) and cls.kind(Dl[j + 1], True) == 'K' and Dl[j + 1][0] - b[1] <= 6:
                ch = cls._chain(Dl, j + 1, 1, 'K', True, maxn=2)
                if ch[-1][1] < dic[0]: out['knocks'] = ('K', ch)
        return out
    @staticmethod
    def patch(a, b, font):
        """The digit's ink from 0 (plate) to 1 (stroke), at its own contrast, as 12 x 18 grey cells; None if too faint."""
        x0, x1, y0, y1 = b
        box = a[y0 - 2:y1 + 2, max(0, x0 - 3):x1 + 3]
        v = (255 - np.maximum(np.maximum(box[..., 0], box[..., 1]), box[..., 2]) if font == 'K'
             else np.minimum(np.minimum(box[..., 0], box[..., 1]), box[..., 2])).astype(np.float32)
        s = v.ravel(); k1, k2 = int(0.25 * (s.size - 1)), int(0.97 * (s.size - 1))
        s = np.partition(s, (k1, k2)); lo, hi = float(s[k1]), float(s[k2])        # plate behind / the strokes
        if hi - lo < 40: return None
        g = np.clip((v[2:-2, x0 - max(0, x0 - 3):x0 - max(0, x0 - 3) + x1 - x0] - lo) / (hi - lo), 0, 1)
        return (_lin(18, g.shape[0]) @ g @ _lin(12, g.shape[1]).T).ravel()
    @classmethod
    def digit(cls, a, b, font):
        """-> (label, distance, lead): label a digit, 'X' (not a digit) or None (doubtful)."""
        if font not in cls.T: return None, 1.0, 0.0
        p = cls.patch(a, b, font)
        if p is None: return None, 1.0, 0.0
        labels, S, Wd = cls.T[font]
        d = np.abs(S - p).mean(1) + 0.5 * (np.abs(Wd - (b[1] - b[0])) > 3)
        i = int(d.argmin()); lab = labels[i]; di = float(d[i])
        rest = d[labels != lab]; other = float(rest.min()) if len(rest) else 1.0
        if di > cls.ACCEPT or other - di < cls.MARGIN: return None, di, other - di
        return str(lab), di, other - di
    @classmethod
    def number(cls, a, font, ch):
        """Digits of one number -> int or None. A not-digit shape may only follow the number (the edge of the next
        plate); every shape before must be a sure digit, all of the same height."""
        labs = [cls.digit(a, b, font)[0] for b in ch]
        while labs and labs[-1] == 'X': labs.pop(); ch = ch[:-1]
        if not labs or any(l is None or l == 'X' for l in labs) or len(labs) > 4: return None
        if max(b[2] for b in ch) - min(b[2] for b in ch) > 1 or max(b[3] for b in ch) - min(b[3] for b in ch) > 1: return None
        s = ''.join(labs)
        return None if len(s) > 1 and s[0] == '0' else int(s)
    @classmethod
    def read(cls, a):
        """a: full 16:9 frame. -> {} or {'kills', 'assists', 'knocks', 'damage'} with the numbers that could be read."""
        if not cls.T: return {}
        out = {}
        try: lay = cls.layout(a)
        except Exception: return out
        for k, (font, ch) in lay.items():
            try: v = cls.number(a, font, ch)
            except Exception: v = None
            if v is not None: out[k] = v
        return out

SINGLE_JUMP = 250      # damage: largest rise accepted from the read before a last value seen once (see stat_final)

def stat_final(reads, t0, t1, end=None, single_jump=None):
    """reads: [(t, value)] of a number that only grows during a game (damage, kills, assists, knocks).
    end: time of the last read of the strip in this game (any of its numbers): a number whose own last read is more
    than 10 s before it stopped being readable while the strip was still there, so its last value is not the total.
    single_jump: None = a last value seen only once is never the total; a number = it is, when consistent (below).
    -> (final value, time it was first read, time of the last read of any value, rests on a single read) or None.

    A value counts once it was read twice in a row (no other value read between). Normally the final value is the
    value of the LAST TWO reads of the game, which must be equal. The number often grows in the very last second of
    a game (damage in a final fight: 355 -> 375, 262 -> 297 -> 353, 106 -> 164 on the last frame with the strip),
    so with single_jump the last read alone is taken (coach's decision, 16 Nov 2026) when it is consistent:
    it is higher than EVERY earlier read of the game (a misread digit mostly gives a lower value, which this
    refuses) and at most single_jump above the read just before it (a misread higher digit gives a jump of hundreds;
    the largest real rise between two reads in our recordings was 178). Each read already passed the digit test.
    Always refused: values read twice that go down inside a game (a misread, or another player's strip), and a
    number whose last read is more than 10 s before the strip's (its value is only a floor)."""
    R = [(t, v) for t, v in reads if t0 <= t < t1]
    if len(R) < 2: return None
    if end is not None and R[-1][0] < end - 10: return None
    vals = [R[i][1] for i in range(1, len(R)) if R[i][1] == R[i - 1][1]]
    if any(b < a for a, b in zip(vals, vals[1:])): return None
    v = R[-1][1]
    if R[-2][1] == v:
        first = next(R[i - 1][0] for i in range(1, len(R)) if R[i][1] == v and R[i - 1][1] == v)
        return v, round(first, 2), round(R[-1][0], 2), False
    if single_jump is None or not vals: return None
    if v <= max(x for _, x in R[:-1]) or v - R[-2][1] > single_jump: return None
    return v, round(R[-1][0], 2), round(R[-1][0], 2), True

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
