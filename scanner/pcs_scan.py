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

VERSION = '1.7.2'
HERE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))  # bundled data when frozen (PCS Scanner app)
MAPS = json.load(open(os.path.join(HERE, 'pcs_maps.json')))
W, H = 2560, 1440  # every frame is scaled to this
USE_HW = True

# ---------- image helpers ----------
def blobs(mask, amin, amax):
    """Connected blobs -> [(cx, cy, area)] with amin <= area <= amax (bincount based: fast with many specks)."""
    lab, n = ndimage.label(mask)
    if n == 0: return []
    ys, xs = np.nonzero(lab); l = lab[ys, xs]
    cnt = np.bincount(l, minlength=n + 1); sx = np.bincount(l, xs, n + 1); sy = np.bincount(l, ys, n + 1)
    k = np.nonzero((cnt >= amin) & (cnt <= amax))[0]
    return [(sx[i] / cnt[i], sy[i] / cnt[i], int(cnt[i])) for i in k]

def lime_mask(r, g, b):  # respawn beacons
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

# ---------- frame readers ----------
def read_dropscreen(a):
    """Scrim/custom pre-game screen: drop map panel on the right with red/green markers."""
    q = a[88:1088:4, 1520:2520:4]            # cheap gate on a 1/16 sample: dark panel with red markers
    if ((q[..., 0] > 140) & (q[..., 1] < 50) & (q[..., 2] < 50)).sum() < 25: return None   # no brightness test: Storm Point is bright
    lg = a[1125:1180, 1740:1800]; lr = a[1125:1180, 2020:2075]
    if ((lg[..., 1] > 150) & (lg[..., 0] < 120)).sum() < 40 or ((lr[..., 0] > 140) & (lr[..., 1] < 60)).sum() < 40: return None
    p = a[88:1088, 1520:2520]; r, g, b = p[..., 0], p[..., 1], p[..., 2]
    red = blobs(ndimage.binary_closing((r > 140) & (g < 50) & (b < 50), iterations=3), 60, 3000)
    if len(red) < 5: return None
    grn = blobs(ndimage.binary_closing((g > 150) & (r < 120) & (b < 120), iterations=3), 60, 3000)
    if len(red) + len(grn) < 8: return None
    lime = blobs(lime_mask(r, g, b), 5, 200)
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
        """Cheap gate (<1 ms): full map = dark key-hint bar along the bottom + white 'M' keycap bottom-left."""
        bar = a[1362:1430:2, ::4]; k = a[1375:1420, 80:120]
        return (bar.max(2) < 40).mean() > 0.8 and (k.min(2) > 200).mean() > 0.3
    def read(self, a):
        if not self.is_map(a): return None
        X0 = 560; c = a[0:1340, X0:1960]          # map area only (all image work on this crop)
        r, g, b = c[..., 0], c[..., 1], c[..., 2]
        sh = lambda L: [(x + X0, y, s_) for x, y, s_ in L]
        lime = sh(blobs(lime_mask(r, g, b), 5, 300))
        if not (10 <= len(lime) <= 80): return None
        P = np.array([[x, y] for x, y, _ in lime]); res = None
        if self.cache:
            mp, M0 = self.cache
            res = fit(P, np.array(MAPS[mp]['beacons']), M0)
            if not (res[0] < 0.12 and res[2] >= 0.6 * len(P) and sane(res[1])): res = None
            else: res = (mp,) + res
        if res is None:   # fast path: unzoomed full map always sits at the same place on screen
            M0 = np.array([[0.0739, 0], [0, 0.0739], [-44.64, 0.27]])
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
    games = []; g = None
    for ev in events:
        t, kind, d = ev['t'], ev['kind'], ev['data']
        if kind == 'drop':
            if g and g['drop'] and t - g['tEnd'] < 60 and g['map'] == d['map']:
                g['tEnd'] = t; g['drop']['tLast'] = t   # same drop screen, later keyframe: add markers missed before
                have = {x['poi'] for x in g['drop']['spots']}
                g['drop']['spots'] += [x for x in d['spots'] if x['poi'] not in have]
                g['drop']['frames'] = g['drop'].get('frames', 1) + 1
                continue
            g = new_game(t, d['map']); g['drop'] = dict(d, tFirst=t, tLast=t); games.append(g); continue
        # map frame
        if g is None or g['map'] != d['map'] or (t - g['tEnd'] > 25 * 60 and not g['drop']):
            g = new_game(t, d['map']); games.append(g)
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

def keyframes(path, start=0.0, end=None):
    """Keyframes (about 1 per second) between start and end, as (time, RGB image)."""
    name, dec = pick_decoder(path)
    w, h = probe(path)
    vf = 'showinfo' if (w, h) == (W, H) else f'scale={W}:{H},showinfo'
    cmd = ['ffmpeg', '-hide_banner', '-nostats', '-v', 'info'] + dec + ['-skip_frame', 'nokey', '-copyts'] + \
          (['-ss', f'{start:.2f}'] if start > 0 else []) + \
          ['-i', path, '-fps_mode', 'passthrough', '-vf', vf, '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10 ** 8)
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
            buf = pr.stdout.read(n)
            if len(buf) < n: break
            try: t = ts.get(timeout=10)
            except Exception: t = start + float(i)
            i += 1
            if t < start - 0.01: continue
            if end is not None and t >= end: break
            yield t, np.frombuffer(buf, np.uint8).reshape(H, W, 3)
    finally:
        if pr.poll() is None: pr.kill()
        pr.wait()

def duration(path):
    try:
        return float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path]).decode().strip())
    except Exception: return 0.0

def scan_frames(frames, snapdir=None, total=0, quiet=False, perf=None):
    mr = MapReader(); labels = HUD.Labels(); feed = HUD.Feed(); events = []; samples = []; t0 = time.time(); last = 0
    perf = perf if perf is not None else {}; tw = time.time()
    for t, img in frames:
        tp = time.time(); perf['decode'] = perf.get('decode', 0) + tp - tw
        a = img.astype(np.int16)
        d = read_dropscreen(a)
        if d: events.append({'t': round(t, 2), 'kind': 'drop', 'data': d})
        else:
            d = mr.read(a)
            if d: events.append({'t': round(t, 2), 'kind': 'map', 'data': d})
        if not d:   # normal HUD: ring clock + debug overlay + event feed
            rg = HUD.read_ring(a); pos, vel = HUD.read_dbg(a)
            if rg or pos: perf.setdefault('_rank', []).append((round(t, 2), HUD.ranked(a)))
            try: feed.read(img, t)
            except Exception: pass
            if rg or pos:
                samples.append([round(t, 2), labels.id(rg['lab']) if rg else -1, rg['v'] if rg else None, bool(rg and rg['spec'])]
                               + (pos if pos else [None, None, None]) + [vel, bool(rg and rg['fine']), bool(rg and rg.get('pre'))])
        if d and snapdir: Image.fromarray(img).resize((1280, 720)).save(os.path.join(snapdir, f'{int(t):05d}_{events[-1]["kind"]}.jpg'), quality=80)
        perf['frames'] = perf.get('frames', 0) + 1
        if not quiet and time.time() - last > 5:
            last = time.time(); pct = f' ({100 * t / total:.0f}%)' if total else ''
            print(f'  {int(t // 60):3d}:{int(t % 60):02d}{pct}  events: {len(events)}  HUD samples: {len(samples)}  [{time.time() - t0:.0f}s]', flush=True)
        tw = time.time(); perf['analysis'] = perf.get('analysis', 0) + tw - tp
    perf['feedLines'] = len(feed.lines); perf['_feed'] = feed.lines
    return events, samples, labels

def _segment(args):
    """Worker: scan one chunk of the recording (runs in its own process)."""
    path, start, end, snap, dec = args
    global DECODER; DECODER = dec; HUD.DEC = dec[1]
    perf = {}
    ev, sm, lb = scan_frames(keyframes(path, start, end), snap, quiet=True, perf=perf)
    return start, ev, sm, [(r, n) for r, n in zip(lb.reps, lb.n)], perf

def scan_video(path, snap=None, workers=0):
    """Split the recording into chunks and scan them in parallel; merge the results in time order."""
    import multiprocessing as mp
    total = duration(path); dec = pick_decoder(path)
    workers = workers or max(1, min(8, (os.cpu_count() or 2) // 2))
    if total < 300: workers = 1
    edges = [total * k / workers for k in range(workers + 1)]; edges[-1] = total + 5
    jobs = [(path, edges[k], edges[k + 1], snap, dec) for k in range(workers)]
    print(f'  scanning {total / 60:.0f} min of video in {workers} parallel chunk(s) ...', flush=True)
    t0 = time.time(); out = []
    if workers == 1: out = [_segment(jobs[0])]
    else:
        with mp.Pool(workers) as pool:
            for k, r in enumerate(pool.imap_unordered(_segment, jobs), 1):
                out.append(r); print(f'  chunk {k}/{workers} done  [{time.time() - t0:.0f}s]', flush=True)
    out.sort(key=lambda r: r[0])
    labels = HUD.Labels(); events = []; samples = []; perf = {}; feed = []; rank = []
    for start, ev, sm, reps, pf in out:
        feed += pf.pop('_feed', []); rank += pf.pop('_rank', [])
        remap = {}
        for i, (rep, n) in enumerate(reps):
            gid = labels.id(rep); labels.n[gid] += n - 1; remap[i] = gid
        for s_ in sm:
            if s_[1] >= 0: s_[1] = remap.get(s_[1], -1)
        events += ev; samples += sm
        for k, v in pf.items(): perf[k] = perf.get(k, 0) + v
    perf['wall'] = time.time() - t0; perf['workers'] = workers; perf['decoder'] = dec[0]; perf['_feed'] = sorted(feed, key=lambda l: l['t']); perf['_rank'] = sorted(rank)
    return events, samples, labels, perf

def split_restarts(games, samples):
    """A game whose drop screen was missed shows up as a ring-clock restart: split it off."""
    out = []
    starts = [g['tStart'] for g in games]
    for gi, g in enumerate(games):
        lo = g['tStart'] - (300 if g['drop'] is None else 0); hi = starts[gi + 1] if gi + 1 < len(starts) else 1e12
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

def timeline(games, samples, path=None, rank=None, mode=None):
    starts = [g['tStart'] for g in games]
    for gi, g in enumerate(games):
        lo = g['tStart'] - (300 if g['drop'] is None else 0); hi = starts[gi + 1] if gi + 1 < len(starts) else 1e12
        S = [s for s in samples if lo <= s[0] < hi]
        g['ring'] = HUD.number_rounds(HUD.ring_segments(S), g['map'])
        if path: HUD.refine_phases(path, g['ring'], S)
        t0 = g['drop']['tLast'] if g['drop'] else lo
        ld = HUD.landing(HUD.clean_pos(S), t0)
        if ld:
            if path: HUD.refine_landing(path, ld)
            mp = HUD.to_map(g['map'], ld['x'], ld['y'])
            if mp: ld['mapX'], ld['mapY'] = mp; ld['poi'] = poi_at(g['map'], *mp)[0]
        g['landing'] = ld
        g['hud'] = {'samples': len(S), 'self': sum(1 for s in S if s[4] is not None and not s[3])}
        R = [r for t_, r in (rank or []) if lo <= t_ < hi]
        g['rankedShare'] = round(sum(R) / len(R), 2) if R else None
        g['mode'] = mode or ('ranked' if R and sum(R) / len(R) >= 0.2 else 'scrim')
        g['samples'] = S
    return games

def main():
    ap = argparse.ArgumentParser(description='Scan Apex recordings for dropship spots and Evo harvesters.')
    ap.add_argument('files', nargs='*'); ap.add_argument('--image', action='append', default=[])
    ap.add_argument('--out', default=os.path.join(HERE, 'pcs_scan')); ap.add_argument('--snaps', action='store_true')
    ap.add_argument('--cpu', action='store_true', help='do not try hardware decoding')
    ap.add_argument('--workers', type=int, default=0, help='parallel chunks (default: half your CPU threads, max 8)')
    ap.add_argument('--mode', choices=['ranked', 'scrim'], help='force the game mode instead of detecting the ranked badge')
    A = ap.parse_args(); os.makedirs(A.out, exist_ok=True)
    global USE_HW; USE_HW = not A.cpu
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
            events, samples, labels, perf = scan_video(path, snap, A.workers)
        t_tl = time.time()
        games = assemble(events)
        if not games and samples: games = [new_game(samples[0][0], '?')]; games[0].update(harvesters=[])
        if kind == 'video': games = split_restarts(games, samples)
        timeline(games, samples, path if kind == 'video' else None, perf.pop('_rank', []), A.mode)
        perf['refine'] = time.time() - t_tl
        st = rec_start(path)
        for gm in games:
            if st:
                gm['clockStart'] = (datetime.datetime.fromisoformat(st) + datetime.timedelta(seconds=gm['tStart'])).isoformat(timespec='minutes')
        feed_lines = perf.pop('_feed', [])
        if feed_lines:
            json.dump({'scanner': VERSION, 'file': os.path.basename(path), 'recStart': st, 'lines': feed_lines},
                      open(os.path.join(A.out, name + '.feed.json'), 'w'))
            print(f'  kill/event feed: {len(feed_lines)} lines saved to {name}.feed.json', flush=True)
        res = {'scanner': VERSION, 'file': os.path.basename(path), 'recStart': st, 'scannedAt': datetime.datetime.now().isoformat(timespec='seconds'),
               'events': len(events), 'perf': {k: (round(v, 1) if isinstance(v, float) else v) for k, v in perf.items()},
               'ringLabels': labels.export(), 'games': games}
        op = os.path.join(A.out, name + '.scan.json')
        json.dump(res, open(op, 'w'), indent=1)
        print(f'  -> {len(games)} game(s): ' + '; '.join(f"{g['map']} drop={'yes' if g['drop'] else 'no'} harvesters={len(g['harvesters'])} ring phases={len(g.get('ring', []))} landing={'yes' if g.get('landing') else 'no'}" for g in games))
        print(f'  saved {op}')
        if perf.get('frames'):
            print(f"  time: {perf.get('wall', 0) / 60:.1f} min scanning ({perf['workers']} chunks, decoder {perf['decoder']}); "
                  f"per keyframe {1000 * perf['decode'] / perf['frames']:.0f} ms decoding + {1000 * perf['analysis'] / perf['frames']:.0f} ms analysis "
                  f"(summed over chunks); 60 fps refinement {perf['refine']:.0f} s", flush=True)

if __name__ == '__main__':
    main()
