#!/usr/bin/env python3
"""PCS recording scanner: dropship spots, Evo harvesters, landing and ring timings from Apex recordings.

Usage:
  python pcs_scan.py "D:/Videos/Apex Legends_2026.10.02-22.04.mp4" [more files...]
  python pcs_scan.py --image screenshot.png        (one still frame)
Options:
  --out DIR     where the results go (default: a "pcs_scan" folder next to this script)
  --snaps       also save a JPEG of every drop screen and map frame used

Needs: Python 3.9+, ffmpeg + ffprobe on PATH, and:  pip install numpy pillow scipy
Only keyframes are decoded (about 1 per second), so a 2-hour 1440p block takes a few minutes.
Output: <out>/<recording name>.scan.json  (small file: send it to Claude for import)
"""
import sys, os, re, json, time, subprocess, argparse, datetime
import numpy as np
from PIL import Image
from scipy import ndimage
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcs_hud as HUD
import pcs_ring, pcs_respawn, pcs_bars, pcs_mapicons, pcs_fights, pcs_usage, pcs_minimap, pcs_ranked

VERSION = '1.10.14'
HERE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))  # bundled data when frozen (PCS Scanner app)

def build_stamp(here=HERE):
    """Which build made a scan, for results files ('scannerBuild'; 'scanner' stays VERSION for the hub and old readers):
    a release build = VERSION; a test build from the workflow (build.json version 'dev-N') = VERSION + '+dev.N', so scans
    made between releases can be told apart; run from source (no build.json) = VERSION + '+source'."""
    try: b = json.load(open(os.path.join(here, 'build.json'), encoding='utf-8-sig'))
    except Exception: return VERSION + '+source'
    v = str(b.get('version') or '')
    if v.startswith('dev-'): return f"{VERSION}+dev.{v[4:]}"
    return VERSION if v == VERSION else f'{VERSION}+{v or "unknown"}'
MAPS = json.load(open(os.path.join(HERE, 'pcs_maps.json')))
W, H = 2560, 1440  # every frame is scaled to this
USE_HW = True
ICONS_EVERY = 2.0  # s between two care package / stronghold reads of the full map (about 0.1 s a read)
EMBLEM_EVERY = 3.0  # s between two reads of the playlist emblem (top right; about 3 ms a read)
RING_EVERY = 3.0   # s between two ring-circle reads of the full map (one read takes about 2 s; a 10 s map open gives 4)
LAYOUT = None   # None = detect per recording; False = 16:9; True = 4:3 stretched to 16:9 (forced with --layout)

# ---------- 4:3 stretched recordings ----------
# Apex in 4:3 stretched to 16:9 keeps every HUD element at its 16:9 left/right position but squashes it to 75 % height.
# Top elements (ring clock, kill feed, ranked badge) stay anchored to the top; the drop-screen map panel keeps its
# bottom edge; the full map is centred. These row maps rebuild the 16:9 layout so the normal readers work unchanged.
_Y = np.arange(H)
ROWS_TOP = np.clip(np.round(_Y * 0.75), 0, H - 1).astype(int)
ROWS_MAP = np.clip(np.round(204 + (_Y[:1340] - 40) * 0.75), 0, H - 1).astype(int)        # map 204..1149 -> 40..1300
ROWS_DROP = np.clip(np.round(337 + (_Y[88:1088] - 88) * 0.75), 0, H - 1).astype(int)     # panel 337..1087 -> 88..1088
ST_MAP_M0 = np.array([[100 / 1264, 0], [0, 100 / 1264], [-648 * 100 / 1264, -40 * 100 / 1264]])

# Each view fills only the boxes its readers look at (left black elsewhere; a new zero frame costs nothing until
# written): the whole remapped frame used to be built 3 to 4 times per keyframe. Boxes, by reader:
#   top:  ring clock rows 417..563 (spectating +85) x 0..400, debug overlay 40..110 x 0..300, ranked badge
#         120..190 x 2480..2545, kill feed 260..640 x 1820..2470  -> rows 0..640, whole width
#   map:  full map 0..1340 x 560..1960 (remapped), key-hint bar 1362..1430 (not remapped)
#   drop: map panel 88..1088 x 1520..2520 (remapped), legend 1125..1180 x 1740..2075 (moved right)
TOP_ROWS = 640
def view_top(img):
    b = np.zeros_like(img); b[:TOP_ROWS] = img[ROWS_TOP[:TOP_ROWS]]; return b
def view_map(img):
    b = np.zeros_like(img); b[:1340, 560:1960] = img[ROWS_MAP, 560:1960]; b[1340:] = img[1340:]; return b
def view_drop(img):
    b = np.zeros_like(img); b[88:1088, 1520:2520] = img[ROWS_DROP, 1520:2520]
    b[1125:1180, 1740:2075] = img[1140:1195, 1983:2318]    # legend ('Your / Enemy Drop Point') moves right
    return b

# ---------- image helpers ----------
def blobs(mask, amin, amax):
    """Connected blobs -> [(cx, cy, area)] with amin <= area <= amax (bincount based: fast with many specks)."""
    lab, n = ndimage.label(mask)
    if n == 0: return []
    ys, xs = np.nonzero(lab); l = lab[ys, xs]
    cnt = np.bincount(l, minlength=n + 1); sx = np.bincount(l, xs, n + 1); sy = np.bincount(l, ys, n + 1)
    k = np.nonzero((cnt >= amin) & (cnt <= amax))[0]
    return [(sx[i] / cnt[i], sy[i] / cnt[i], int(cnt[i])) for i in k]

def lime_mask(r, g, b, loose=False):  # respawn beacons
    if loose:   # stretched recordings come out paler: lime = clearly greener than red, far greener than blue
        return (g > 165) & (g - b > 55) & (r > 100) & (g - r >= 15)
    return (r > 150) & (r < 215) & (g > 205) & (b > 60) & (b < 130)

def chev_mask(r, g, b):  # Evo harvesters (yellow)
    return (r > 190) & (g > 190) & (b < 80) & (np.abs(r - g) < 35)

def fit(P, B, M, iters=25):
    for _ in range(iters):
        X = np.c_[P, np.ones(len(P))] @ M
        dd = np.linalg.norm(X[:, None] - B[None], axis=2); j = dd.argmin(1); dm = dd.min(1)
        k = dm < np.percentile(dm, 75) + 0.3
        if k.sum() < 3: break
        M = np.linalg.lstsq(np.c_[P[k], np.ones(k.sum())], B[j[k]], rcond=None)[0]
    X = np.c_[P, np.ones(len(P))] @ M
    dm = np.linalg.norm(X[:, None] - B[None], axis=2).min(1)
    return float(np.median(dm)), M, int((dm < 0.7).sum())

def sane(M, s=None):
    sx, sy = M[0, 0], M[1, 1]
    ok = sx > 0 and abs(sx - sy) < 0.03 * sx and abs(M[0, 1]) < 0.02 * sx and abs(M[1, 0]) < 0.02 * sx
    return ok and (s is None or abs(sx - s) < 0.1 * s)

def search(P, B, scales, grid=10, step=2.0):
    best = None
    for s0 in scales:
        c = B.mean(0) - s0 * P.mean(0)
        for dx in np.arange(-grid, grid + 0.1, step):
            for dy in np.arange(-grid, grid + 0.1, step):
                M = np.array([[s0, 0], [0, s0], [c[0] + dx, c[1] + dy]], float)
                res = fit(P, B, M)
                if not sane(res[1]): continue
                if best is None or res[0] < best[0]: best = res
                if best and best[0] < 0.08 and best[2] >= 0.6 * len(P): return best
    return best or (99.0, np.zeros((3, 2)), 0)

def pip(x, y, poly):
    ins = False; n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]; x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1: ins = not ins
    return ins

def poi_at(mp, x, y):
    P = MAPS[mp]['pois']
    for p in P:
        if p.get('poly') and pip(x, y, p['poly']): return p['id'], True
    p = min(P, key=lambda q: (q['x'] - x) ** 2 + (q['y'] - y) ** 2)
    return p['id'], False

POI_NEAR = 12.0   # map %: on a map without POI outlines, a landing farther than this from every POI centre gets no name

def landing_poi(mp, x, y):
    """The POI named for a landing. Maps with POI outlines: as poi_at. Maps with centres only (BM, KC): the nearest
    centre only when it is within POI_NEAR, else None (the nearest far-away centre would be a wrong name)."""
    pid, inside = poi_at(mp, x, y)
    P = MAPS[mp]['pois']
    if inside or any(p.get('poly') for p in P): return pid
    p = next(q for q in P if q['id'] == pid)
    return pid if (p['x'] - x) ** 2 + (p['y'] - y) ** 2 <= POI_NEAR ** 2 else None

# ---------- frame readers ----------
def read_dropscreen(a, loose=False):
    """Scrim/custom pre-game screen: drop map panel on the right with red/green markers.
    a: the frame (uint8 or int16); the gates only compare values, the panel is made int16 here for its arithmetic."""
    q = a[88:1088:4, 1520:2520:4]            # cheap gate on a 1/16 sample: dark panel with red markers
    if ((q[..., 0] > 140) & (q[..., 1] < 50) & (q[..., 2] < 50)).sum() < 25: return None   # no brightness test: Storm Point is bright
    lg = a[1125:1180, 1740:1800]; lr = a[1125:1180, 2020:2075]
    if ((lg[..., 1] > 150) & (lg[..., 0] < 120)).sum() < 40 or ((lr[..., 0] > 140) & (lr[..., 1] < 60)).sum() < 40: return None
    p = a[88:1088, 1520:2520].astype(np.int16); r, g, b = p[..., 0], p[..., 1], p[..., 2]
    red = blobs(ndimage.binary_closing((r > 140) & (g < 50) & (b < 50), iterations=3), 60, 3000)
    if len(red) < 5: return None
    grn = blobs(ndimage.binary_closing((g > 150) & (r < 120) & (b < 120), iterations=3), 60, 3000)
    if len(red) + len(grn) < 8: return None
    lime = blobs(lime_mask(r, g, b, loose), 4 if loose else 5, 200)
    if len(lime) < 12: return None
    P = np.array([[x, y] for x, y, _ in lime]); best = None
    for mp, d in MAPS.items():
        B = np.array(d['beacons'])
        res = search(P, B, [0.1], grid=4, step=2)
        if best is None or res[0] < best[1][0]: best = (mp, res)
    mp, (err, M, inl) = best
    if err > 0.15 or inl < 0.7 * len(P) or not sane(M, 0.1): return None
    spots = []
    for kind, L in (('enemy', red), ('ours', grn)):
        merged = []  # one marker can split into 2 blobs (skull cut-out): merge within 25 px
        for x, y, a in sorted(L, key=lambda v: -v[2]):
            for m in merged:
                if abs(m[0] - x) < 25 and abs(m[1] - y) < 25:
                    m[0] = (m[0] * m[2] + x * a) / (m[2] + a); m[1] = (m[1] * m[2] + y * a) / (m[2] + a); m[2] += a; break
            else: merged.append([x, y, a])
        for x, y, _ in merged:
            X, Y = (np.array([x, y, 1]) @ M)
            pid, inside = poi_at(mp, X, Y)
            spots.append({'poi': pid, 'inside': inside, 'x': round(float(X), 2), 'y': round(float(Y), 2), 'kind': kind})
    return {'map': mp, 'err': round(err, 3), 'spots': spots}

class MapReader:
    """Full map (M key) during a game: register on respawn beacons, read harvesters."""
    SCALES = [100 / 1353] + list(np.geomspace(100 / 1500, 100 / 4500, 7))
    def __init__(self): self.cache = None; self.cool = 0
    @staticmethod
    def is_map(a):
        """Cheap gate (<1 ms): full map = dark key-hint bar along the bottom + white 'M' keycap bottom-left
        (or, on controller, a 'Close Map' button glyph and text in the same corner)."""
        bar = a[1362:1430:2, ::4]
        if (HUD.minmax(bar)[1] < 40).mean() <= 0.8: return False
        if (HUD.minmax(a[1375:1420, 80:120])[0] > 200).mean() > 0.3: return True
        wf = (HUD.minmax(a[1375:1420, 40:320])[0] > 170).mean()
        return 0.04 < wf < 0.5
    def read(self, a, stretched=False):
        """a: the frame (uint8 or int16); the map area is made int16 here for its arithmetic."""
        if not self.is_map(a): return None
        X0 = 560; c = a[0:1340, X0:1960].astype(np.int16)          # map area only (all image work on this crop)
        r, g, b = c[..., 0], c[..., 1], c[..., 2]
        sh = lambda L: [(x + X0, y, s_) for x, y, s_ in L]
        lime = sh(blobs(lime_mask(r, g, b, stretched), 4 if stretched else 5, 300))
        if not (10 <= len(lime) <= 80): return None
        P = np.array([[x, y] for x, y, _ in lime]); res = None
        if self.cache:
            mp, M0 = self.cache
            res = fit(P, np.array(MAPS[mp]['beacons']), M0)
            if not (res[0] < 0.12 and res[2] >= 0.6 * len(P) and sane(res[1])): res = None
            else: res = (mp,) + res
        if res is None:   # fast path: unzoomed full map always sits at the same place on screen
            M0 = ST_MAP_M0 if stretched else np.array([[0.0739, 0], [0, 0.0739], [-44.64, 0.27]])
            for mp, d in MAPS.items():
                rr = fit(P, np.array(d['beacons']), M0)
                if rr[0] < 0.12 and rr[2] >= 0.6 * len(P) and sane(rr[1]): res = (mp,) + rr; break
        if res is None: return None   # zoomed / half-open map: skipped (the unzoomed map gives the same data)
        mp, err, M, inl = res; self.cache = (mp, M)
        ch = sh(blobs(ndimage.binary_dilation(chev_mask(r, g, b), iterations=2), 45, 150))
        hv = [[round(float(v), 2) for v in (np.array([x, y, 1]) @ M)] for x, y, _ in ch]
        hv = [h for h in hv if 0 <= h[0] <= 100 and 0 <= h[1] <= 100]
        me = sorted(sh(blobs((r < 140) & (g > 190) & (b > 190), 40, 400)), key=lambda v: -v[2])[:1]   # cyan = own arrow (calibration of world -> map)
        # drop markers shown on the full map while in the dropship (ranked: only nearby squads, scrims: all)
        dm = []
        for kind, mk in (('enemy', (r > 140) & (g < 50) & (b < 50)), ('ours', (g > 115) & (r < 110) & (b < 80) & (g - r > 60))):
            for x, y, a_ in sh(blobs(ndimage.binary_closing(mk, iterations=3), 300, 4000)):
                X, Y = (np.array([x, y, 1]) @ M)
                if 0 <= X <= 100 and 0 <= Y <= 100: dm.append({'kind': kind, 'x': round(float(X), 2), 'y': round(float(Y), 2), 'poi': poi_at(mp, X, Y)[0]})
        me = [[round(float(v), 2) for v in (np.array([x, y, 1]) @ M)] + [a_] for x, y, a_ in me]
        return {'map': mp, 'err': round(err, 3), 'scale': round(float(M[0, 0]), 4), 'harv': hv, 'me': me, 'dropMarks': dm}

# ---------- games ----------
def new_game(t, mp):
    return {'tStart': t, 'tEnd': t, 'map': mp, 'drop': None, 'harvRaw': [], 'mapFrames': 0, 'calib': []}

def assemble(events):
    games = []; g = None; events = HUD.select_runs(events); banner = []; rdrop = []
    def ranked_drop(g, t):
        """Ranked's drop-point screen seen just before the game (kept as data: it never starts a game or proves a scrim)."""
        R = [x for x in rdrop if 0 <= t - x[0] < 180]; rdrop.clear()
        if not R: return
        best = max(R, key=lambda x: len(x[1]['spots']))[1]
        g['rankedDrop'] = dict(best, tFirst=R[0][0], tLast=R[-1][0], frames=len(R))
    def titled(g, t):
        """Give the game the mode and map words of the loading banner seen just before its legend select
        (the word read most often; a word the scanner doesn't know yet is kept as its shape)."""
        B = [b for b in banner if 0 <= t - b[0] < 180]; banner.clear()
        if not B: return
        out = {'t': B[0][0], 'frames': len(B)}
        for key in ('mode', 'map'):
            names = [b[1][key] for b in B if b[1].get(key)]
            out[key] = max(set(names), key=names.count) if names else None
            if not names: out[key + 'Sig'] = B[len(B) // 2][1][key + 'Sig']
        g['banner'] = out
        if out['map'] in MAPS: g['map'] = out['map']
    for ev in events:
        t, kind, d = ev['t'], ev['kind'], ev['data']
        if kind == 'banner': banner.append((t, d)); continue
        if kind == 'rankedDrop': rdrop.append((t, d)); continue
        if kind == 'select':    # legend select screen: a new game starts here
            if g and g.get('select') and t - g['select']['tLast'] <= 5: HUD.merge_select(g['select'], d, t); g['tEnd'] = max(g['tEnd'], t); continue
            if g and g['drop'] and g['mapFrames'] == 0 and not g.get('select') and t - g['drop']['tLast'] < 180:
                g['select'] = HUD.new_select(d, t); g['tEnd'] = t; continue      # same game: its drop screen came first
            g = new_game(t, None); g['select'] = HUD.new_select(d, t); titled(g, t); ranked_drop(g, t); games.append(g); continue
        if kind == 'drop':
            if g and g.get('select') and not g['drop'] and g['mapFrames'] == 0 and t - g['select']['tLast'] < 180:
                g['map'] = d['map']; g['tStart'] = g['tEnd'] = t; g['drop'] = dict(d, tFirst=t, tLast=t); continue   # same game: legend select came first
            if g and g['drop'] and t - g['tEnd'] < 60 and g['map'] == d['map']:
                g['tEnd'] = t; g['drop']['tLast'] = t   # same drop screen, later keyframe: add markers missed before
                have = {x['poi'] for x in g['drop']['spots']}
                g['drop']['spots'] += [x for x in d['spots'] if x['poi'] not in have]
                g['drop']['frames'] = g['drop'].get('frames', 1) + 1
                continue
            g = new_game(t, d['map']); g['drop'] = dict(d, tFirst=t, tLast=t); games.append(g); continue
        # map frame
        if g is not None and g['map'] is None: g['map'] = d['map']      # game opened by its legend select: first map seen
        elif g is not None and g.get('banner') and g['mapFrames'] == 0 and g['map'] != d['map']: g['map'] = d['map']   # the full map outranks the banner word
        if g is None or g['map'] != d['map'] or (t - g['tEnd'] > 25 * 60 and not g['drop']):
            g = new_game(t, d['map']); ranked_drop(g, t); games.append(g)
        elif d['harv'] and len(g['harvRaw']) >= 5 and not any(e['kind'] == 'drop' for e in events):  # ranked only
            known = np.array([h[:2] for h in g['harvRaw']])
            near = [np.min(np.linalg.norm(known - np.array(h), axis=1)) < 1.0 for h in d['harv']]
            if len(d['harv']) >= 4 and sum(near) < 0.3 * len(near):
                g = new_game(t, d['map']); games.append(g)
        g['tEnd'] = t; g['mapFrames'] += 1
        g['harvRaw'] += [[h[0], h[1], t] for h in d['harv']]
        g.setdefault('calib', []).append({'t': t, 'me': d.get('me', [])})
        if d.get('dropMarks') and len(d['dropMarks']) > len(g.get('dropMap') or []): g['dropMap'] = d['dropMarks']
    out = []
    for g in games:
        if g.get('select') and not g['drop']: g['tStart'] = g['select']['tLast']     # the game starts when the screen closes
        if g['map'] is None: g['map'] = '?'
        cl = []
        for x, y, t in g['harvRaw']:
            for c in cl:
                if abs(c['x'] - x) < 0.8 and abs(c['y'] - y) < 0.8:
                    c['n'] += 1; c['sx'] += x; c['sy'] += y; break
            else: cl.append({'x': x, 'y': y, 'sx': x, 'sy': y, 'n': 1, 't': t})
        hv = [{'x': round(c['sx'] / c['n'], 2), 'y': round(c['sy'] / c['n'], 2), 'frames': c['n'], 'firstSeen': c['t'],
               'poi': poi_at(g['map'], c['sx'] / c['n'], c['sy'] / c['n'])[0]} for c in cl]
        # reliable = seen in >= 25 % of map opens (min 2) and not a ghost within 2 % of a stronger detection
        top = max([h['frames'] for h in hv] or [0]); keep = []
        for h in sorted(hv, key=lambda h: -h['frames']):
            ok = h['frames'] >= max(2, 0.25 * top) and all(abs(h['x'] - k['x']) > 2 or abs(h['y'] - k['y']) > 2 for k in keep)
            h['reliable'] = ok
            if ok: keep.append(h)
        hv.sort(key=lambda h: (h['y'], h['x']))
        out.append({'map': g['map'], 'tStart': g['tStart'], 'tEnd': g['tEnd'], 'mapFrames': g['mapFrames'],
                    'drop': g['drop'], 'dropMap': g.get('dropMap'), 'harvesters': hv, 'calib': g['calib']})
        if g.get('select'): out[-1]['select'] = {k: v for k, v in g['select'].items() if not k.startswith('_')}
        if g.get('banner'): out[-1]['banner'] = g['banner']
        if g.get('rankedDrop'): out[-1]['rankedDrop'] = g['rankedDrop']
    # a game opened by its legend select where nobody opened the full map: same map as the nearest game of the
    # session (less than 45 min away), marked as a guess
    known = [g for g in out if g['map'] != '?']
    for g in out:
        if g['map'] == '?' and known:
            n = min(known, key=lambda k: abs(k['tStart'] - g['tStart']))
            if abs(n['tStart'] - g['tStart']) < 45 * 60: g['map'] = n['map']; g['mapGuessed'] = True
    return out

# ---------- input ----------
def rec_start(path):
    n = os.path.basename(path)
    m = re.search(r'(\d{4})[._-](\d{2})[._-](\d{2})[ _-](\d{2})[._-](\d{2})(?:[._-](\d{2}))?', n)
    if not m: return None
    y, mo, d, h, mi, s = m.groups()
    return f'{y}-{mo}-{d}T{h}:{mi}:{s or "00"}'

def probe(path):
    try:
        o = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height',
                                     '-of', 'csv=p=0', path]).decode().strip().split(',')
        return int(o[0]), int(o[1])
    except Exception: return 0, 0

DECODER = None   # ffmpeg input options chosen by pick_decoder()
NATIVE = {'av1': 'av1', 'hevc': 'hevc', 'h264': 'h264'}

def _codec(path):
    try:
        return subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=codec_name',
                                        '-of', 'csv=p=0', path]).decode().strip()
    except Exception: return ''

def _bench(path, opts, start, secs=30):
    """Decode `secs` of video keyframes-only to RGB; returns (keyframes, seconds) or None if it fails."""
    cmd = ['ffmpeg', '-hide_banner', '-nostats', '-v', 'error'] + opts + ['-skip_frame', 'nokey', '-ss', f'{start:.1f}', '-t', str(secs),
           '-i', path, '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    try:
        t = time.time(); r = subprocess.run(cmd, capture_output=True, timeout=90); dt = time.time() - t
        w, h = probe(path); n = len(r.stdout) // max(1, w * h * 3)
        if r.returncode != 0 or n == 0: return None
        return n, dt
    except Exception: return None

def pick_decoder(path):
    """Pick the fastest decoder for this video format: CPU, DirectX 11 (AMD / NVIDIA / Intel) or CUDA (NVIDIA).
    For AV1, '-hwaccel' alone is ignored (ffmpeg picks the CPU decoder libdav1d), so the GPU needs '-c:v av1'.
    The choice is remembered in decoder.json next to this script, so the tests run only once per video format."""
    global DECODER
    if DECODER is not None: return DECODER
    codec = _codec(path); w, h = probe(path); key = f'{codec}-{w}x{h}-{"gpu" if USE_HW else "cpu"}'
    cache_f = os.path.join(os.environ.get('PCS_DATA_DIR', HERE), 'decoder.json')
    try: cache = json.load(open(cache_f))
    except Exception: cache = {}
    if key in cache:
        name, opts = cache[key]; print(f'  decoder: {name} (remembered for {codec} {w}x{h}; delete decoder.json to re-test)', flush=True)
        DECODER = (name, opts); HUD.DEC = opts; return DECODER
    start = max(0.0, min(600.0, duration(path) / 3))
    nat = NATIVE.get(codec, '')
    cands = [('CPU', [])]
    if USE_HW: cands += [('GPU d3d11va', ['-hwaccel', 'd3d11va'] + (['-c:v', nat] if nat else [])),
                         ('GPU cuda', ['-hwaccel', 'cuda'] + (['-c:v', nat] if nat else []))]
    res = []
    for name, o in cands:
        b = _bench(path, o, start, secs=20)
        if b: res.append((b[1] / b[0], name, o, b[0])); print(f'  decoder test {name:12s}: {1000 * b[1] / b[0]:.0f} ms per keyframe', flush=True)
        else: print(f'  decoder test {name:12s}: not available', flush=True)
    cpu = next((r for r in res if r[1] == 'CPU'), None)
    ok = [r for r in res if cpu is None or r[3] <= cpu[3] * 1.2]   # must really skip to keyframes like the CPU path
    best = min(ok or res, key=lambda r: r[0]) if (ok or res) else (0, 'CPU', [], 0)
    print(f'  -> using {best[1]}', flush=True)
    DECODER = (best[1], best[2]); HUD.DEC = best[2]
    try: cache[key] = [best[1], best[2]]; json.dump(cache, open(cache_f, 'w'), indent=1)
    except Exception: pass
    return DECODER

# How the decoded frames get from ffmpeg to the scanner. Each 1440p keyframe is 11 MB. Until 1.10.11 the pipe was read
# through a 100 MB Python buffer (bufsize=10**8, then .read(n)): 134 ms and 0.18 CPU-s per keyframe, 3/4 of it kernel
# time. Read straight into one buffer per frame (no Python buffer in between): 25 to 30 ms and 0.08 CPU-s, the same
# bytes. Measured 18 Nov 2026, also against a socket on 127.0.0.1 (29 ms, ~12 % more CPU than this pipe).
def _read_full(f, buf):
    """Fill buf from the unbuffered stream f. False if the stream ended first."""
    mv = memoryview(buf); got = 0; n = len(buf)
    while got < n:
        r = f.readinto(mv[got:])
        if not r: return False
        got += r
    return True

def keyframes(path, start=0.0, end=None):
    """Keyframes (about 1 per second) between start and end, as (time, RGB image). The images are read-only."""
    name, dec = pick_decoder(path)
    w, h = probe(path)
    vf = 'showinfo' if (w, h) == (W, H) else f'scale={W}:{H},showinfo'
    cmd = ['ffmpeg', '-hide_banner', '-nostats', '-v', 'info'] + dec + ['-skip_frame', 'nokey', '-copyts'] + \
          (['-ss', f'{start:.2f}'] if start > 0 else []) + \
          ['-i', path, '-fps_mode', 'passthrough', '-vf', vf, '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    pr = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                          creationflags=0x08000000 if os.name == 'nt' else 0)       # no console window
    import threading, queue
    ts = queue.Queue()
    def rd():
        for line in pr.stderr:
            m = re.search(rb'pts_time:([0-9.]+)', line)
            if m: ts.put(float(m.group(1)))
    threading.Thread(target=rd, daemon=True).start()
    n = W * H * 3; i = 0
    try:
        while True:
            buf = bytearray(n)                          # a new buffer per frame: a reader may keep the previous one
            if not _read_full(pr.stdout, buf): break
            try: t = ts.get(timeout=10)
            except Exception: t = start + float(i)
            i += 1
            if t < start - 0.01: continue
            if end is not None and t >= end: break
            img = np.frombuffer(buf, np.uint8).reshape(H, W, 3); img.flags.writeable = False    # read-only, as before
            yield t, img
    finally:
        if pr.poll() is None: pr.kill()
        pr.wait()

def duration(path):
    try:
        return float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path]).decode().strip())
    except Exception: return 0.0

def scan_frames(frames, snapdir=None, total=0, quiet=False, perf=None):
    mr = MapReader(); labels = HUD.Labels(); feed = HUD.Feed(); rpfeed = pcs_ranked.Tracker(); events = []; samples = []; t0 = time.time(); last = 0
    legends = HUD.Legends(); ring_at = -99.0; icons_at = -99.0; mm_at = -99.0; emb_at = -99.0
    perf = perf if perf is not None else {}; tw = time.time()
    mode = LAYOUT; votes = {False: 0, True: 0}     # layout: 16:9 (False) or 4:3 stretched (True), voted from successful reads
    for t, img in frames:
        tp = time.time(); perf['decode'] = perf.get('decode', 0) + tp - tw
        cands = [mode] if mode is not None else ([True, False] if votes[True] > votes[False] else [False, True])
        d = None; hud = None; top = []; bn = None
        def view_top_once():    # 4:3 stretched: the remapped top, built once per keyframe
            if not top: top.append(view_top(img))
            return top[0]
        if False in cands:     # legend select screen (16:9 only so far): start of a game, the squad's legends
            try: d = legends.read(img)
            except Exception: d = None
            if d: events.append({'t': round(t, 2), 'kind': 'select', 'data': d})
            else:               # loading banner before it: the mode and the map in words
                try: bn = HUD.Banner.read(img)
                except Exception: bn = None
                if bn: events.append({'t': round(t, 2), 'kind': 'banner', 'data': bn}); d = bn
        # The readers take the uint8 frame and make int16 only the small areas they compute on: converting the whole
        # 1440p frame cost about 6 ms each time, up to three times per keyframe (results are identical).
        # A banner read does not end the keyframe: the scrim drop screen shows the map's name in the same place at the
        # top left (custom lobbies, e.g. "WORLD'S EDGE (SEASON 17)"), read as a banner with unknown words. Skipping the
        # drop reader there lost every scrim's drop screen, so no scrim mode (1.10.8 to 1.10.14; found on Oct 2026 scrims).
        if bn: d = None
        for st in ([] if d else cands):
            HUD.LOOSE = st
            d = read_dropscreen(view_drop(img) if st else img, st)
            if d and bn and bn.get('mode') == 'ranked':     # Ranked's drop-point screen ("RANKED - DROPZONE", 17 Nov
                d['ranked'] = True                          # 2026 update): same layout as the scrim one, kept as data only
                events.append({'t': round(t, 2), 'kind': 'rankedDrop', 'data': d}); break
            if d: events.append({'t': round(t, 2), 'kind': 'drop', 'data': d}); votes[st] += 1; break
            d = mr.read(view_map(img) if st else img, st)
            if d:
                events.append({'t': round(t, 2), 'kind': 'map', 'data': d}); votes[st] += 1
                if t - ring_at >= RING_EVERY:     # ring circles on the full map (about 2 s a read)
                    ring_at = t
                    R = perf.get('_rings') or [(None, None, None)]
                    hint = R[-1][2] if R[-1][1] == d['map'] and t - R[-1][0] < 90 else None    # same ring a moment ago
                    try: rr = pcs_ring.read(view_map(img) if st else img, mr.cache[1], hint)
                    except Exception: rr = None
                    if rr and (rr['cur'] or rr['next']): perf.setdefault('_rings', []).append((round(t, 2), d['map'], rr))
                if t - icons_at >= ICONS_EVERY:    # care packages and strongholds (pcs_mapicons)
                    icons_at = t
                    try: ic = pcs_mapicons.read(view_map(img) if st else img, mr.cache[1], pcs_mapicons.SCALE_43 if st else 1.0)
                    except Exception: ic = None
                    if ic and (ic['care'] or ic['strongholds']): perf.setdefault('_icons', []).append((round(t, 2), d['map'], ic))
                break
        if bn and not d: d = bn     # a loading banner only: as before (no HUD reads on it)
        if not d:   # normal HUD: ring clock + debug overlay + event feed
            for st in cands:
                HUD.LOOSE = st
                a = view_top_once() if st else img
                rg = HUD.read_ring(a); pos, vel = HUD.read_dbg(a)
                if rg or pos: hud = (st, a, rg, pos, vel); votes[st] += 1; break
            st = hud[0] if hud else (mode if mode is not None else votes[True] > votes[False])
            if hud:
                _, a, rg, pos, vel = hud
                perf.setdefault('_rank', []).append((round(t, 2), HUD.ranked(a)))
                samples.append([round(t, 2), labels.id(rg['lab']) if rg else -1, rg['v'] if rg else None, bool(rg and rg['spec'])]
                               + (pos if pos else [None, None, None]) + [vel, bool(rg and rg['fine']), bool(rg and rg.get('pre'))])
            try: feed.read(view_top_once() if st else img, t)
            except Exception: pass
            if not st:          # ranked: green '+N RP' feed lines and the victim's rank badge (16:9 only)
                try: rpfeed.read(img, t)
                except Exception: pass
            try: perf.setdefault('_storm', []).append((round(t, 2), pcs_fights.read_storm(img)))   # ring damage: red screen edges
            except Exception: pass
            if not st:          # "N SQUADS LEFT" (16:9 only so far)
                try: rp = pcs_ranked.panel_rp(img)        # ranked: the game's RP so far, under the rank emblem
                except Exception: rp = None
                if rp is not None and not (hud and hud[2] and hud[2]['spec']): perf.setdefault('_rp', []).append((round(t, 2), rp))
                try: sq = HUD.SquadsLeft.read(img)
                except Exception: sq = None
                if sq: perf.setdefault('_squads', []).append((round(t, 2), sq))
                try: own = HUD.OwnStats.read(img)
                except Exception: own = {}
                if own and not (hud and hud[2] and hud[2]['spec']):       # while spectating the strip is someone else's
                    perf.setdefault('_own', []).append((round(t, 2), own))
                if t - mm_at >= pcs_minimap.EVERY and not (hud and hud[2] and hud[2]['spec']):   # HUD minimap: position
                    try: mm = pcs_minimap.grab(img)
                    except Exception: mm = None
                    if mm is not None: mm_at = t; perf.setdefault('_minimap', []).append((round(t, 2), mm))
        if not (mode if mode is not None else votes[True] > votes[False]):      # squad cards (16:9 only so far)
            if (not d or bn) and t - emb_at >= EMBLEM_EVERY:     # playlist emblem, top right (pubs, Wildcard)
                emb_at = t
                try: em = HUD.ModeEmblem.read(img)
                except Exception: em = None
                if em: perf.setdefault('_emblem', []).append((round(t, 2),) + em)
            try: cards = pcs_respawn.read_cards(img)
            except Exception: cards = []
            if cards: perf.setdefault('_cards', []).append((round(t, 2), cards, bool(hud[2]['spec']) if hud and hud[2] else None))
            try: own_bar = pcs_bars.read_own(img); mate_bars = pcs_bars.read_mates(img, cards)    # shields, health, knocks
            except Exception: own_bar, mate_bars = None, []
            if own_bar or mate_bars:
                perf.setdefault('_bars', []).append((round(t, 2), own_bar, mate_bars, bool(hud[2]['spec']) if hud and hud[2] else None))
            if not d:                                                       # own HUD only (no map, drop or select)
                try: us = pcs_usage.read(img, t, perf)                   # heal panel, ult and tactical icons
                except Exception: us = None
                if us: perf.setdefault('_usage', []).append(us)
        if mode is None and max(votes.values()) >= 3 and min(votes.values()) == 0:
            mode = votes[True] > votes[False]
            if not quiet: print(f'  layout: {"4:3 stretched" if mode else "16:9"}', flush=True)
        if d and snapdir: Image.fromarray(img).resize((1280, 720)).save(os.path.join(snapdir, f'{int(t):05d}_{events[-1]["kind"]}.jpg'), quality=80)
        perf['frames'] = perf.get('frames', 0) + 1
        if not quiet and time.time() - last > 5:
            last = time.time(); pct = f' ({100 * t / total:.0f}%)' if total else ''
            print(f'  {int(t // 60):3d}:{int(t % 60):02d}{pct}  events: {len(events)}  HUD samples: {len(samples)}  [{time.time() - t0:.0f}s]', flush=True)
        tw = time.time(); perf['analysis'] = perf.get('analysis', 0) + tw - tp
    perf['feedLines'] = len(feed.lines); perf['_feed'] = feed.lines; perf['_rpSeen'] = rpfeed.seen; perf['_rpLines'] = rpfeed.finish()
    perf['_votes43'] = votes[True]; perf['_votes169'] = votes[False]
    return events, samples, labels

def _segment(args):
    """Worker: scan one chunk of the recording (runs in its own process)."""
    path, start, end, snap, dec, layout = args
    global DECODER, LAYOUT; DECODER = dec; HUD.DEC = dec[1]; LAYOUT = layout
    perf = {}
    ev, sm, lb = scan_frames(keyframes(path, start, end), snap, quiet=True, perf=perf)
    return start, ev, sm, [(r, n) for r, n in zip(lb.reps, lb.n)], perf

# Processes scanning at the same time. 54-min recording, 18 Nov 2026 (peak memory of the whole scan / wall time):
# 8 (old, buffered pipe) 4.5 GB / 151 s; 2: 0.85 GB / 134 s; 3: 1.2 GB / 106 s; 4: 1.6 GB / 99 s (~360 MB each).
WORKERS = 2

def default_chunks(total):
    """How many pieces a recording is cut into. Results depend on where the cuts are (each piece starts its readers
    afresh), so this is the same split as before 1.10.12, whatever the number of worker processes."""
    return 1 if total < 300 else max(1, min(8, (os.cpu_count() or 2) // 2))

class Resume:
    """Finished chunks of an unfinished scan, kept on disk so a scan stopped while Apex runs (the app stops it instead
    of keeping it paused in memory) goes on later from the chunks still to do. A chunk is used again only for the same
    file (size, time), the same cuts, decoder, layout and scanner build: then its results are what a rescan would give."""
    def __init__(self, folder, path, edges, dec, snap):
        self.folder = folder
        try: stt = os.stat(path); f = [os.path.basename(path), stt.st_size, stt.st_mtime_ns]
        except OSError: f = [os.path.basename(path)]
        self.key = json.dumps({'file': f, 'edges': [round(e, 3) for e in edges], 'decoder': list(dec[1]), 'layout': LAYOUT,
                               'snaps': bool(snap), 'build': build_stamp()}, sort_keys=True)
    def _f(self, k): return os.path.join(self.folder, f'chunk{k:02d}.pkl')
    def load(self):
        """-> {chunk number: result} of the chunks finished earlier (nothing when anything differs)."""
        import pickle
        try:
            with open(os.path.join(self.folder, 'key.json'), encoding='utf-8') as fh:
                if fh.read() != self.key: return {}
        except OSError: return {}
        done = {}
        for name in sorted(os.listdir(self.folder)):
            m = re.fullmatch(r'chunk(\d+)\.pkl', name)
            if not m: continue
            try:
                with open(os.path.join(self.folder, name), 'rb') as fh: done[int(m.group(1))] = pickle.load(fh)
            except Exception: pass                      # half-written or damaged: that chunk is scanned again
        return done
    def save(self, k, res):
        import pickle
        try:
            os.makedirs(self.folder, exist_ok=True)
            kf = os.path.join(self.folder, 'key.json')
            if not os.path.exists(kf):
                with open(kf + '.tmp', 'w', encoding='utf-8') as fh: fh.write(self.key)
                os.replace(kf + '.tmp', kf)
            with open(self._f(k) + '.tmp', 'wb') as fh: pickle.dump(res, fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(self._f(k) + '.tmp', self._f(k))          # all or nothing: a stop mid-write leaves no chunk
        except OSError: pass
    def reset(self):
        """A different key on disk (other file, cuts or build): its chunks are not ours to use."""
        try:
            with open(os.path.join(self.folder, 'key.json'), encoding='utf-8') as fh: same = fh.read() == self.key
        except OSError: same = True
        if not same: self.clear()
    def clear(self):
        import shutil
        shutil.rmtree(self.folder, ignore_errors=True)

def _segment_k(args):
    return args[0], _segment(args[1])

def scan_video(path, snap=None, workers=0, chunks=0, resume=None, total=None):
    """Split the recording into chunks and scan them in parallel; merge the results in time order.
    workers: processes at the same time (default WORKERS); chunks: pieces (default: as before, see default_chunks);
    resume: a folder for finished chunks (Resume), so a stopped scan goes on from there; total: the recording's length
    when `path` is a copy whose own length differs by a few ms (the team server's keyframes-only copy): the chunks are
    then cut where they are on the player's PC."""
    import multiprocessing as mp
    total = total or duration(path); dec = pick_decoder(path)
    chunks = chunks or default_chunks(total)
    workers = max(1, min(workers or WORKERS, chunks))
    edges = [total * k / chunks for k in range(chunks + 1)]; edges[-1] = total + 5
    jobs = [(path, edges[k], edges[k + 1], snap, dec, LAYOUT) for k in range(chunks)]
    rs = Resume(resume, path, edges, dec, snap) if resume else None
    done = {}
    if rs:
        rs.reset(); done = rs.load()
        if done: print(f'  going on with an earlier scan: {len(done)} of {chunks} chunk(s) already done', flush=True)
    todo = [(k, jobs[k]) for k in range(chunks) if k not in done]
    print(f'  scanning {total / 60:.0f} min of video in {chunks} chunk(s), {workers} at a time ...', flush=True)
    t0 = time.time(); out = list(done.values())
    def got(k, r):
        out.append(r)
        if rs: rs.save(k, r)
        print(f'  chunk {len(out)}/{chunks} done  [{time.time() - t0:.0f}s]', flush=True)
    if workers == 1 or len(todo) <= 1:
        for k, j in todo: got(k, _segment(j))
    elif todo:
        with mp.Pool(min(workers, len(todo))) as pool:
            for k, r in pool.imap_unordered(_segment_k, todo): got(k, r)
    out.sort(key=lambda r: r[0])
    labels = HUD.Labels(); events = []; samples = []; perf = {}; feed = []; rpc = []; rpl = []; rps = []; rank = []; squads = []; own = []
    rings = []; cards = []; icons = []; bars = []; storm = []; usage = []; minimap = []; emblem = []
    for start, ev, sm, reps, pf in out:
        feed += pf.pop('_feed', []); rpc += pf.pop('_rp', []); rpl += pf.pop('_rpLines', []); rps += pf.pop('_rpSeen', []); rank += pf.pop('_rank', []); squads += pf.pop('_squads', []); own += pf.pop('_own', [])
        rings += pf.pop('_rings', []); cards += pf.pop('_cards', []); icons += pf.pop('_icons', []); bars += pf.pop('_bars', []); storm += pf.pop('_storm', [])
        usage += pf.pop('_usage', []); minimap += pf.pop('_minimap', []); emblem += pf.pop('_emblem', []); pf.pop('_usageMaskAt', None)
        remap = {}
        for i, (rep, n) in enumerate(reps):
            gid = labels.id(rep); labels.n[gid] += n - 1; remap[i] = gid
        for s_ in sm:
            if s_[1] >= 0: s_[1] = remap.get(s_[1], -1)
        events += ev; samples += sm
        for k, v in pf.items(): perf[k] = perf.get(k, 0) + v
    v43, v169 = perf.pop('_votes43', 0), perf.pop('_votes169', 0)
    HUD.STRETCH = LAYOUT if LAYOUT is not None else v43 > v169; HUD.LOOSE = HUD.STRETCH
    perf['layout'] = '4:3 stretched' if HUD.STRETCH else '16:9'; perf['layoutVotes'] = {'4:3': v43, '16:9': v169}
    print(f'  layout: {perf["layout"]} ({v43} / {v169} reads)', flush=True)
    perf['wall'] = time.time() - t0; perf['workers'] = workers; perf['chunks'] = chunks; perf['chunksResumed'] = len(done); perf['decoder'] = dec[0]; perf['_feed'] = sorted(feed, key=lambda l: l['t']); perf['_rp'] = sorted(rpc); perf['_rpLines'] = sorted(rpl, key=lambda l: l['t']); perf['_rpSeen'] = sorted(rps); perf['_rank'] = sorted(rank); perf['_squads'] = sorted(squads); perf['_own'] = sorted(own, key=lambda r: r[0])
    perf['_rings'] = sorted(rings, key=lambda r: r[0]); perf['_cards'] = sorted(cards, key=lambda r: r[0]); perf['_icons'] = sorted(icons, key=lambda r: r[0])
    perf['_bars'] = sorted(bars, key=lambda r: r[0])
    perf['_storm'] = sorted(storm, key=lambda r: r[0])
    perf['_usage'] = sorted(usage, key=lambda r: r[0])
    perf['_minimap'] = sorted(minimap, key=lambda r: r[0])
    perf['_emblem'] = sorted(emblem, key=lambda r: r[0])
    return events, samples, labels, perf

def split_restarts(games, samples):
    """A game whose drop screen was missed shows up as a ring-clock restart: split it off."""
    out = []
    starts = [g['tStart'] for g in games]
    for gi, g in enumerate(games):
        lo = g['tStart'] - (300 if g['drop'] is None and not g.get('select') else 0); hi = starts[gi + 1] if gi + 1 < len(starts) else 1e12
        S = HUD.clean_pos([s for s in samples if lo <= s[0] < hi])
        cuts = HUD.restart_cuts(S, HUD.ring_segments(S), g['map'])
        b = [g['tStart']] + cuts + [hi]
        for k in range(len(b) - 1):
            part = dict(g) if k == 0 else {'map': g['map'], 'drop': None, 'dropMap': None, 'mapFrames': 0, 'calib': [], 'restart': True}
            part['tStart'] = b[k]; part['tEnd'] = min(g['tEnd'], b[k + 1]) if k == 0 else b[k + 1]
            part['harvesters'] = [h for h in g['harvesters'] if b[k] <= h['firstSeen'] < b[k + 1]]
            if k > 0 and not [s for s in samples if b[k] <= s[0] < b[k + 1] and s[2] is not None]: continue
            out.append(part)
    return out

EMBLEM_BEFORE = 150     # s: the "waiting for players" screen (emblem 134 px lower) comes up to this long before a game's start

def mode_seen(g, R, emblem, starts, gi):
    """What the screen tells about a game's mode, and the mode it gives ('ranked', 'scrim', 'wildcard', 'pubs' or None).
    Signals: the ranked RP panel (share of HUD reads, R), the scrim drop screen, rank badges on the legend select
    screen, the loading banner's mode word, the playlist emblem at the top right (pubs, Wildcard, or another
    playlist's). A scrim needs the drop screen, as before. When two signals name different modes the game stays
    unlabelled (mode None, conflict true): better no label than a wrong one."""
    lo = g['tStart'] - EMBLEM_BEFORE
    hi = starts[gi + 1] - EMBLEM_BEFORE if gi + 1 < len(starts) else float('inf')
    ekey, emode, counts = HUD.emblem_vote(emblem or [], lo, hi)
    out = {'mode': None, 'from': [], 'conflict': False, 'emblem': ekey, 'emblemReads': counts}
    badge = bool(R) and sum(R) / len(R) >= 0.2
    sel = bool((g.get('select') or {}).get('ranked'))
    word = (g.get('banner') or {}).get('mode')
    word = word if word in ('ranked', 'wildcard', 'pubs') else None
    other = 'other' if ekey and not emode else None      # an emblem of a playlist we can't name: not pubs, not Wildcard
    seen = {'badge': 'ranked' if badge else None, 'drop': 'scrim' if g.get('drop') else None,
            'select': 'ranked' if sel else None, 'banner': word, 'emblem': emode or other}
    named = {k: v for k, v in seen.items() if v}
    if 'ranked' in named.values() and named.get('emblem') == 'other':
        named.pop('emblem')         # rank badges are round emblems too: an unnamed emblem never outvotes ranked signs
    if seen['drop'] and 'ranked' in named.values():
        # Since the 17 Nov 2026 update Ranked shows a drop-point screen ("RANKED - DROPZONE") laid out like the scrim
        # drop screen: any ranked sign beats it. The drop read stays as data, marked ranked, never scrim proof.
        g['drop']['ranked'] = True; seen['drop'] = None; named.pop('drop')
    if badge:                       # the RP panel: ranked, as before, unless the banner or emblem names another mode
        if any(v in ('wildcard', 'pubs') for v in named.values()):
            out['conflict'] = True; out['from'] = sorted(named); return out
        out['mode'] = 'ranked'; out['from'] = sorted(k for k, v in named.items() if v == 'ranked'); return out
    if seen['drop']:                # scrim proof: the drop screen and no ranked sign (Wildcard / pubs signs: as before)
        out['mode'] = 'scrim'; out['from'] = ['drop']; return out
    names = set(named.values())
    if len(names) > 1:
        out['conflict'] = True; out['from'] = sorted(named); return out
    if names and 'other' not in names:      # another playlist's emblem: no label
        out['mode'] = names.pop()
    out['from'] = sorted(named)
    return out

def timeline(games, samples, path=None, rank=None, mode=None, squads=None, own=None, emblem=None):
    starts = [g['tStart'] for g in games]
    for gi, g in enumerate(games):
        lo = g['tStart'] - (300 if g['drop'] is None and not g.get('select') else 0); hi = starts[gi + 1] if gi + 1 < len(starts) else 1e12
        S = [s for s in samples if lo <= s[0] < hi]
        g['ring'] = HUD.number_rounds(HUD.ring_segments(S), g['map'])
        if path: HUD.refine_phases(path, g['ring'], S)
        t0 = g['drop']['tLast'] if g['drop'] else lo
        ld = HUD.landing(HUD.clean_pos(S), t0)
        if ld:
            if path: HUD.refine_landing(path, ld)
            mp = HUD.to_map(g['map'], ld['x'], ld['y'])
            if mp:
                ld['mapX'], ld['mapY'] = mp; ld['poi'] = landing_poi(g['map'], *mp)
                ld['mapCalibrated'] = g['map'] not in HUD.CAL_UNCHECKED
        g['landing'] = ld
        g['hud'] = {'samples': len(S), 'self': sum(1 for s in S if s[4] is not None and not s[3])}
        R = [r for t_, r in (rank or []) if lo <= t_ < hi]
        g['rankedShare'] = round(sum(R) / len(R), 2) if R else None
        # scrim only with evidence: the pre-game drop screen was read and no ranked badge. Without the drop
        # screen (e.g. a ranked session where the badge wasn't read) the mode is 'unknown', never 'scrim'.
        g['modeSeen'] = seen = mode_seen(g, R, emblem, starts, gi)
        g['mode'] = mode or seen['mode'] or 'unknown'
        g['samples'] = S
        if squads:
            g['squadsLeft'] = HUD.squads_timeline(squads, g['tStart'], hi)
        if own:                 # the recording player's own numbers: the value of the game's last two reads (stat_final)
            st = {}
            end = max((t_ for t_, _ in own if g['tStart'] <= t_ < hi), default=None)
            for k in ('kills', 'assists', 'knocks', 'damage'):
                f = HUD.stat_final([(t_, o[k]) for t_, o in own if k in o], g['tStart'], hi, end,
                                   HUD.SINGLE_JUMP if k == 'damage' else None)   # damage: a consistent last read counts
                if f:
                    st[k] = f[0]; st[k + 'At'] = f[1]; st[k + 'LastRead'] = f[2]
                    if k == 'damage':
                        st['damageSingleRead'] = f[3]                       # true: the total rests on one read
                        if f[3]:    # the read before it: the rise happened in between (the sound check looks there)
                            prev = [t_ for t_, o in own if 'damage' in o and g['tStart'] <= t_ < f[1]]
                            if prev: st['damagePrevReadAt'] = round(prev[-1], 2)
            if st: g['own'] = st
    # A game seen without its drop screen used to start where the HUD was first read (often minutes after the
    # jump). It starts at the jump from the dropship when that was seen (a minute earlier: the ship ride).
    prev = None
    for g in games:
        ld = g.get('landing') or {}
        jump = ld.get('exitAtExact') or ld.get('landedAtExact')
        if g['drop'] is None and jump is not None and jump < g['tStart']:
            g['tStart'] = max(jump - 60, (prev + 60) if prev is not None else 0)
        prev = g['tStart']
    game_ends(games, samples, squads, own)
    return games

def game_ends(games, samples, squads=None, own=None):
    """games[].tEndGame: when the game is over for this recording = the last in-game HUD read (ring clock, squads
    counter, own stats strip, full map) from the game's start to the next game's start. Not the debug overlay alone:
    it also shows in the firing range and between games (3 Nov: 2.5 hours of it after a game).
    tEnd keeps its old meaning (the last drop screen / legend select / full map frame, often = tStart).
    A game's ring phases stay in its window: a phase first seen after the next game started is dropped, and a phase
    whose countdown ends after the game ended for this recording (the player died before) gets endsAfterGame."""
    for gi, g in enumerate(games):
        t0 = g['tStart']; hi = games[gi + 1]['tStart'] if gi + 1 < len(games) else float('inf')
        inside = lambda t_: t0 <= t_ < hi
        reads = [s[0] for s in samples if s[2] is not None and inside(s[0])] +[t_ for t_, _ in (squads or []) if inside(t_)] + \
                [t_ for t_, _ in (own or []) if inside(t_)] + ([g['tEnd']] if g.get('tEnd') is not None and inside(g['tEnd']) else [])
        end = max(reads, default=t0)
        g['tEndGame'] = round(float(end), 2)
        if g.get('ring') is not None:
            g['ring'] = [r for r in g['ring'] if r['firstSeen'] < hi]
            for r in g['ring']:
                if r.get('endsAt') is not None and r['endsAt'] > end: r['endsAfterGame'] = True
    return games

def main():
    ap = argparse.ArgumentParser(description='Scan Apex recordings for dropship spots and Evo harvesters.')
    ap.add_argument('files', nargs='*'); ap.add_argument('--image', action='append', default=[])
    ap.add_argument('--out', default=os.path.join(HERE, 'pcs_scan')); ap.add_argument('--snaps', action='store_true')
    ap.add_argument('--cpu', action='store_true', help='do not try hardware decoding')
    ap.add_argument('--workers', type=int, default=0, help=f'processes scanning at the same time (default {WORKERS})')
    ap.add_argument('--chunks', type=int, default=0, help='pieces the recording is cut into (default: half your CPU threads, max 8; results depend on it)')
    ap.add_argument('--fresh', action='store_true', help='ignore chunks kept from an earlier, unfinished scan of the same file')
    ap.add_argument('--mode', choices=['ranked', 'scrim'], help='force the game mode instead of detecting the ranked badge')
    ap.add_argument('--layout', choices=['auto', '16:9', '4:3'], default='auto', help='screen layout of the recordings (default: detect; 4:3 = 4:3 stretched to 16:9)')
    # scan on the team server: the file is a keyframes-only copy of the recording (pcs_recorder.serverscan)
    ap.add_argument('--duration', type=float, default=0, help=argparse.SUPPRESS)      # the recording's own length
    ap.add_argument('--clips', default='', help=argparse.SUPPRESS)    # folder of 60 fps clips; missing ones -> .needs.json
    ap.add_argument('--keep-chunks', action='store_true', help=argparse.SUPPRESS)    # keep .partial for a second pass
    A = ap.parse_args(); os.makedirs(A.out, exist_ok=True)
    global USE_HW, LAYOUT; USE_HW = not A.cpu; LAYOUT = {'auto': None, '16:9': False, '4:3': True}[A.layout]
    if A.clips: HUD.CLIPS = A.clips
    jobs = [(f, 'video') for f in A.files] + [(f, 'image') for f in A.image]
    if not jobs: ap.print_help(); return
    for path, kind in jobs:
        name = os.path.splitext(os.path.basename(path))[0]
        print(f'Scanning {name} ...', flush=True)
        snap = None
        if A.snaps: snap = os.path.join(A.out, name + '_snaps'); os.makedirs(snap, exist_ok=True)
        if kind == 'image':
            img = np.array(Image.open(path).convert('RGB').resize((W, H)))
            perf = {}; events, samples, labels = scan_frames([(0.0, img)], snap, perf=perf)
        else:
            resume = os.path.join(A.out, name + '.partial')       # finished chunks, until the scan is complete
            if A.fresh: Resume(resume, path, [], ('', []), snap).clear()
            events, samples, labels, perf = scan_video(path, snap, A.workers, A.chunks, resume, A.duration or None)
        t_tl = time.time()
        games = assemble(events)
        if not games and samples: games = [new_game(samples[0][0], '?')]; games[0].update(harvesters=[])
        if kind == 'video': games = split_restarts(games, samples)
        timeline(games, samples, path if kind == 'video' else None, perf.pop('_rank', []), A.mode, perf.pop('_squads', []), perf.pop('_own', []),
                 perf.pop('_emblem', []))
        try: pcs_ring.attach(games, perf.pop('_rings', []))               # ring circles per stage, ring ends, speed
        except Exception as e: perf['ringError'] = f'{type(e).__name__}: {e}'[:200]
        try: pcs_mapicons.attach(games, perf.pop('_icons', []))         # care packages (after the ring ends), strongholds
        except Exception as e: perf['mapIconsError'] = f'{type(e).__name__}: {e}'[:200]
        try: pcs_respawn.attach(games, perf.pop('_cards', []), samples)    # respawns, the player's own deaths
        except Exception as e: perf['respawnError'] = f'{type(e).__name__}: {e}'[:200]
        bar_reads = perf.pop('_bars', [])
        try: pcs_bars.attach(games, bar_reads, samples)     # own knocks, shield / health timelines
        except Exception as e: perf['barsError'] = f'{type(e).__name__}: {e}'[:200]
        t_mm = time.time(); mm = perf.pop('_minimap', [])
        try: pcs_minimap.attach(games, mm)                                 # position from the HUD minimap (no debug overlay needed)
        except Exception as e: perf['minimapError'] = f'{type(e).__name__}: {e}'[:200]
        perf['minimap'] = time.time() - t_mm; perf['minimapReads'] = len(mm)
        perf.pop('_usageMaskAt', None)
        try: pcs_usage.attach(games, pcs_usage.unpack(perf.pop('_usage', [])), bar_reads)    # heals, ult / tactical uses
        except Exception as e: perf['usageError'] = f'{type(e).__name__}: {e}'[:200]
        perf['refine'] = time.time() - t_tl
        audio = None; loud = None
        if kind == 'video':     # sound cues from the game track back up what was read on screen
            t_au = time.time()
            try:
                import pcs_audio
                audio = pcs_audio.scan(path)
                loud = audio.get('_loud')
                pcs_audio.attach(games, audio)
            except Exception as e:      # never lose a scan to the sound check
                audio = {'error': f'{type(e).__name__}: {e}'[:200]}
            perf['audio'] = time.time() - t_au
            if audio.get('cues'):
                other = sum(len(g.get('ringSoundOther', [])) for g in games)
                print(f"  sound: ring moving heard {len(audio['cues'].get('ring_moving', []))} time(s)"
                      f", {audio.get('ringWeak', 0)} more found where the screen saw a countdown end"
                      f", {other} not a ring start by the screen", flush=True)
        st = rec_start(path)
        for gm in games:
            if st:
                gm['clockStart'] = (datetime.datetime.fromisoformat(st) + datetime.timedelta(seconds=gm['tStart'])).isoformat(timespec='minutes')
        feed_lines = perf.pop('_feed', [])
        if any('event' in l for l in feed_lines):   # our squad's knocks / kills / downs per game, from the kill lines
            for i, gm in enumerate(games):          # a game's lines: from 60 s before it starts to 60 s before the next
                t1 = games[i + 1]['tStart'] - 60 if i + 1 < len(games) else float('inf')
                gm['feedStats'] = HUD.feed_stats(feed_lines, gm['tStart'] - 60, t1, (gm.get('select') or {}).get('squad'))
        rp_reads = perf.pop('_rp', []); rp_lines = perf.pop('_rpLines', []); rp_seen = perf.pop('_rpSeen', [])
        for i, gm in enumerate(games):              # ranked: RP counter and the feed's green RP lines per game
            t1 = games[i + 1]['tStart'] - 60 if i + 1 < len(games) else float('inf')
            try:
                g_rp = pcs_ranked.game_rp(rp_reads, gm['tStart'] - 60, t1, rp_seen)
                if g_rp:            # green RP lines after the counter's last value: RP it never showed (spectating)
                    later = [l for l in rp_lines if g_rp['lastAt'] + 5 < l['t'] < t1]
                    if later: g_rp['laterLines'] = len(later)
                    gm['rp'] = g_rp
                f_rp = pcs_ranked.game_feed(rp_lines, gm['tStart'] - 60, t1)
                if f_rp: gm['rpFeed'] = f_rp
            except Exception as e: perf['rankedError'] = f'{type(e).__name__}: {e}'[:200]
        try: pcs_fights.attach(games, feed_lines, bar_reads, perf.pop('_storm', []), loud)    # fights, ring damage taken
        except Exception as e: perf['fightsError'] = f'{type(e).__name__}: {e}'[:200]
        if feed_lines or rp_lines:
            json.dump({'scanner': VERSION, 'scannerBuild': build_stamp(), 'file':os.path.basename(path), 'recStart': st, 'lines': feed_lines,
                       **({'rpLines': rp_lines} if rp_lines else {})},
                      open(os.path.join(A.out, name + '.feed.json'), 'w'))
            print(f'  kill/event feed: {len(feed_lines)} lines saved to {name}.feed.json', flush=True)
        res = {'scanner': VERSION, 'scannerBuild': build_stamp(), 'file':os.path.basename(path), 'recStart': st, 'scannedAt': datetime.datetime.now().isoformat(timespec='seconds'),
               'events': len(events), 'perf': {k: (round(v, 1) if isinstance(v, float) else v) for k, v in perf.items()},
               'ringLabels': labels.export(), 'games': games}
        if audio is not None: res['audio'] = audio
        op = os.path.join(A.out, name + '.scan.json')
        json.dump(res, open(op, 'w'), indent=1)
        if kind == 'video' and not A.keep_chunks: Resume(resume, path, [], ('', []), snap).clear()     # complete: the kept chunks go
        if HUD.CLIPS is not None:       # team server: the 60 fps windows no clip covered (the app cuts and sends them)
            json.dump({'windows': sorted(HUD.NEEDS)}, open(os.path.join(A.out, name + '.needs.json'), 'w'))
        print(f'  -> {len(games)} game(s): ' + '; '.join(f"{g['map']} drop={'yes' if g['drop'] else 'no'} harvesters={len(g['harvesters'])} ring phases={len(g.get('ring', []))} landing={'yes' if g.get('landing') else 'no'}" for g in games))
        print(f'  saved {op}')
        if perf.get('frames'):
            print(f"  time: {perf.get('wall', 0) / 60:.1f} min scanning ({perf['chunks']} chunks, {perf['workers']} at a time, decoder {perf['decoder']}); "
                  f"per keyframe {1000 * perf['decode'] / perf['frames']:.0f} ms decoding + {1000 * perf['analysis'] / perf['frames']:.0f} ms analysis "
                  f"(summed over chunks); 60 fps refinement {perf['refine']:.0f} s", flush=True)

if __name__ == '__main__':
    main()
