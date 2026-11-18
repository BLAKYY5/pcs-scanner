#!/usr/bin/env python3
"""Region cache: decode a recording ONCE and keep only the small screen regions the HUD readers look at, so a
reader can be re-run over a whole recording in seconds instead of re-decoding it (5 to 10 minutes) every try.

A developer tool for the coach's PC, not part of the app (not bundled in the exe).

  python tools/region_cache.py build "<recording.mp4>" [--cache DIR] [--start S --end E] [--verify] [--workers N]
  python tools/region_cache.py run   "<recording or cache dir>" [--readers squads,own,legends,banner] [--per minute|game]
  python tools/region_cache.py verify "<recording or cache dir>"       # cache vs the reads taken on the real frames

The cache folder (default: $PCS_REGION_CACHE, else --cache) gets one sub-folder per recording:
  index.json      recording, its size, the boxes (name -> y0, y1, x0, x1 at 2560 x 1440), codec, keyframe count
  m0035.npz       the keyframes of minute 35: 't' (seconds, float64) and per box '<box>' (the encoded images, one
                  after the other, uint8) + '<box>_off' (start of each image in it, n + 1 offsets)
  reads.json      (with --verify) what the readers gave on the REAL decoded frames, to prove the cache is faithful
Images are lossless WebP: decoded with Pillow they are bit-identical to the decoded frame, so every reader gives
exactly what it gives on the full frame. Readable with numpy + Pillow only.

The cache holds game footage: keep it OUT of the repo (it is private, like the recordings).
Boxes are for 16:9 recordings (4:3 stretched ones use other rows: not covered).
"""
import os, sys, io, json, time, argparse, collections
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scanner'))
import pcs_hud as HUD                     # noqa: E402

W, H = 2560, 1440
FORMAT = 1
# Boxes (y0, y1, x0, x1) at 2560 x 1440, from the row / column ranges the readers index (pcs_hud.py):
BOXES = {
    'dbg':      (40, 110, 0, 300),        # read_dbg: debug overlay a[40:110, 0:300]
    'left':     (138, 566, 0, 560),       # Legends: badges, frames, portraits (x 56..426 with the non-ranked shift),
                                          #   names (x to 560), rank badges (x 72..156), rows 150..491;
                                          # Banner: the plate a[150:190, 20:60] and the start of both words;
                                          # read_ring: clock rows 417..478 (+85 while spectating), x < 380
    'banner':   (138, 298, 560, 1100),    # Banner: the rest of the word rows (138..198 and 273..298, x to 1100)
    'counters': (70, 192, 1900, 2560),    # SquadsLeft a[77:99, 2040:2294], OwnStats a[128:162, 1900:2380]
                                          #   (the strip grows to the left), ranked() a[120:190, 2480:2545].
                                          #   Caches built before 16 Nov 2026 start at x = 1990: ranked kills there
                                          #   lose their skull icon, so OwnStats leaves them unread on those caches.
    'feed':     (260, 640, 1820, 2470),   # Feed: a[260:640, 1820:2470]
    'squad':    (860, 1400, 0, 600),      # pcs_respawn cards a[860:1330, 0:430]; pcs_bars own card and bars
                                          #   (rows 1290..1365, x 230..560) and teammate bars
    'heal':     (900, 1060, 1560, 2300),  # pcs_usage: the heal panel right of centre (icon, name, progress bar)
    'abil':     (1180, 1440, 600, 1440),  # pcs_usage: heal slot, tactical icon, ult icon + charge % (bottom centre)
}
CODEC = 'webp-lossless'

def _enc(arr):
    b = io.BytesIO(); Image.fromarray(np.ascontiguousarray(arr)).save(b, 'WEBP', lossless=True, quality=0, method=0)
    return b.getvalue()

def _dec(buf):
    return np.asarray(Image.open(io.BytesIO(buf)).convert('RGB'))

def cache_root(arg=None):
    root = arg or os.environ.get('PCS_REGION_CACHE')
    if not root: sys.exit('no cache folder: pass --cache DIR or set PCS_REGION_CACHE')
    return root

def cache_dir(rec_or_dir, root=None):
    """A recording path / name or a cache folder -> the cache folder of that recording."""
    if os.path.isfile(os.path.join(rec_or_dir, 'index.json')): return rec_or_dir
    return os.path.join(cache_root(root), os.path.splitext(os.path.basename(rec_or_dir))[0])


# ---------- the readers, by name: (boxes they need, function(frame) -> a JSON-able value or None) ----------
_LEG = []
def _legends(a):
    if not _LEG: _LEG.append(HUD.Legends())
    return _LEG[0].read(a)
def _ring(a):
    r = HUD.read_ring(a)
    return None if not r else {'v': r['v'], 'fine': r['fine'], 'spec': r['spec'], 'pre': bool(r.get('pre'))}
def _dbg(a):
    pos, vel = HUD.read_dbg(a)
    return None if pos is None and vel is None else {'pos': pos, 'vel': vel}
READERS = {
    'legends': (('left',), _legends),
    'banner':  (('left', 'banner'), HUD.Banner.read),
    'squads':  (('counters',), HUD.SquadsLeft.read),
    'own':     (('counters',), lambda a: HUD.OwnStats.read(a) or None),
    'ranked':  (('counters',), lambda a: HUD.ranked(a) or None),
    'ring':    (('left',), _ring),
    'dbg':     (('dbg',), _dbg),
    'ownbar':  (('squad',), lambda a: __import__('pcs_bars').read_own(a)),    # own card: shield, health, knocked
    'heal':    (('heal',), lambda a: __import__('pcs_usage').read_heal(a)),   # heal panel: item, bar colour, progress
    'abil':    (('abil',), lambda a: {k: v for k, v in __import__('pcs_usage').read_abilities(a).items() if 'Mask' not in k and v} or None),
}
VERIFY = ('legends', 'banner', 'squads', 'own', 'ranked', 'ring', 'dbg')   # stored by build --verify

def boxes_for(readers):
    out = []
    for r in readers:
        for b in READERS[r][0] if r in READERS else (('feed',) if r == 'feed' else BOXES):
            if b not in out: out.append(b)
    return out


# ---------- build ----------
def _write_minute(d, minute, rows, boxes):
    arrs = {'t': np.array([t for t, _ in rows], np.float64)}
    for name in boxes:
        bufs = [enc[name] for _, enc in rows]
        arrs[name] = np.frombuffer(b''.join(bufs), np.uint8)
        arrs[name + '_off'] = np.cumsum([0] + [len(b) for b in bufs]).astype(np.int64)
    tmp = os.path.join(d, f'm{minute:04d}.tmp.npz')
    np.savez(tmp, **arrs); os.replace(tmp, os.path.join(d, f'm{minute:04d}.npz'))

class Writer:
    """Adds keyframes (in time order) to a cache folder: one file per minute, written when the minute is complete."""
    def __init__(self, d, boxes=None):
        self.d = d; self.boxes = boxes or BOXES; self.rows = collections.OrderedDict(); self.n = 0; os.makedirs(d, exist_ok=True)
    def add(self, t, img):
        enc = {name: _enc(img[y0:y1, x0:x1]) for name, (y0, y1, x0, x1) in self.boxes.items()}
        m = int(t // 60); self.rows.setdefault(m, []).append((t, enc)); self.n += 1
        for old in [k for k in self.rows if k < m]: _write_minute(self.d, old, self.rows.pop(old), self.boxes)
    def close(self):
        for m, rr in self.rows.items(): _write_minute(self.d, m, rr, self.boxes)
        self.rows.clear()

def write_index(d, boxes=None, **info):
    size = sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d) if f.endswith('.npz'))
    idx = dict({'format': FORMAT, 'boxes': boxes or BOXES, 'codec': CODEC, 'bytes': size, 'built': time.strftime('%Y-%m-%d %H:%M:%S')}, **info)
    json.dump(idx, open(os.path.join(d, 'index.json'), 'w'), indent=1)
    return idx

def _build_part(args):
    """Worker: decode [start, end) with the scanner's own keyframes() and write its minute files."""
    path, d, start, end, dec, verify, boxes = args
    import pcs_scan as S
    S.DECODER = dec; HUD.DEC = dec[1]
    wr = Writer(d, boxes); reads = []
    for t, img in S.keyframes(path, start, end):
        wr.add(t, img)
        if verify:
            r = {'t': t}
            for name in VERIFY:
                try: r[name] = READERS[name][1](img)
                except Exception as e: r[name] = {'error': repr(e)}
            reads.append(r)
    wr.close()
    return start, wr.n, reads

def build(path, root=None, start=0.0, end=None, verify=False, workers=0, boxes=None):
    """boxes: names of the boxes to keep (default all: about 1.5 GB per hour; 'counters,dbg' alone about 0.2 GB)."""
    import multiprocessing as mp
    import pcs_scan as S
    boxes = {b: BOXES[b] for b in (boxes or BOXES)}
    d = cache_dir(path, root); os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):                 # a rebuild replaces the whole cache of this recording
        if f.endswith('.npz') or f in ('index.json', 'reads.json'): os.remove(os.path.join(d, f))
    total = S.duration(path); end = min(end or total + 5, total + 5); dec = S.pick_decoder(path)
    w, h = S.probe(path)
    if abs(w / max(1, h) - 16 / 9) > 0.01: print(f'  warning: {w}x{h} is not 16:9; the boxes are for 16:9 recordings', flush=True)
    span = end - start
    workers = workers or max(1, min(8, (os.cpu_count() or 2) // 2))
    if span < 600: workers = 1
    per = max(60, int(np.ceil(span / workers / 60)) * 60)          # whole minutes per worker: one writer per minute file
    m0 = int(start // 60) * 60
    edges = [start] + [m0 + k * per for k in range(1, workers + 1) if m0 + k * per < end] + [end]
    jobs = [(path, d, edges[k], edges[k + 1], dec, verify, boxes) for k in range(len(edges) - 1)]
    print(f'  caching {span / 60:.0f} min with {len(jobs)} parallel part(s), decoder {dec[0]} ...', flush=True)
    t0 = time.time(); out = []
    if len(jobs) == 1: out = [_build_part(jobs[0])]
    else:
        with mp.Pool(len(jobs)) as pool:
            for k, r in enumerate(pool.imap_unordered(_build_part, jobs), 1):
                out.append(r); print(f'  part {k}/{len(jobs)} done  [{time.time() - t0:.0f}s]', flush=True)
    out.sort(key=lambda r: r[0])
    n = sum(r[1] for r in out); wall = time.time() - t0
    idx = write_index(d, boxes, recording=os.path.basename(path), path=os.path.abspath(path), recSize=os.path.getsize(path), duration=total,
                      start=start, end=end, frames=n, decoder=dec[0], workers=len(jobs), buildSeconds=round(wall, 1))
    size = idx['bytes']
    if verify:
        reads = [r for o in out for r in o[2]]
        json.dump({'recording': idx['recording'], 'readers': list(VERIFY), 'reads': reads}, open(os.path.join(d, 'reads.json'), 'w'))
    print(f'  {n} keyframes in {wall:.0f} s; {size / 1e6:.0f} MB = {size / 1e6 / max(1e-9, span / 3600):.0f} MB per hour -> {d}', flush=True)
    return d


# ---------- load ----------
class Cache:
    """A built cache. frames() yields (t, frame) where frame is a 1440 x 2560 x 3 uint8 image with the cached boxes
    pasted at their place and black elsewhere, so the readers work on it unchanged.
    The same array is reused from one keyframe to the next (only the boxes are rewritten): copy it to keep it."""
    def __init__(self, d):
        self.dir = d; self.index = json.load(open(os.path.join(d, 'index.json')))
        self.boxes = {k: tuple(v) for k, v in self.index['boxes'].items()}
        self.minutes = sorted(int(f[1:5]) for f in os.listdir(d) if f.startswith('m') and f.endswith('.npz') and '.tmp' not in f)
    def crops(self, start=0.0, end=None, boxes=None):
        """-> (t, {box: crop array}) per keyframe in [start, end)."""
        missing = [b for b in boxes or () if b not in self.boxes]
        if missing: print(f'  this cache has no box {", ".join(missing)}: readers needing it read nothing', flush=True)
        boxes = [b for b in boxes or self.boxes if b in self.boxes]
        for m in self.minutes:
            if (m + 1) * 60 <= start or (end is not None and m * 60 >= end): continue
            z = np.load(os.path.join(self.dir, f'm{m:04d}.npz'))
            ts = z['t']; data = {b: (z[b], z[b + '_off']) for b in boxes}
            for i, t in enumerate(ts):
                if t < start - 0.01 or (end is not None and t >= end): continue
                yield float(t), {b: _dec(data[b][0][data[b][1][i]:data[b][1][i + 1]].tobytes()) for b in boxes}
    def frames(self, start=0.0, end=None, boxes=None):
        frame = np.zeros((H, W, 3), np.uint8)
        for t, cr in self.crops(start, end, boxes):
            for b, arr in cr.items():
                y0, y1, x0, x1 = self.boxes[b]; frame[y0:y1, x0:x1] = arr
            yield t, frame
    def times(self):
        return [float(t) for m in self.minutes for t in np.load(os.path.join(self.dir, f'm{m:04d}.npz'))['t']]

def paste(crops, boxes=BOXES):
    """{box: crop} -> a full 1440 x 2560 x 3 frame (black outside the boxes)."""
    frame = np.zeros((H, W, 3), np.uint8)
    for b, arr in crops.items():
        y0, y1, x0, x1 = boxes[b]; frame[y0:y1, x0:x1] = arr
    return frame


# ---------- run readers over a cache ----------
def run_readers(cache, readers, start=0.0, end=None):
    """-> {reader: [(t, value)]} with the non-empty reads."""
    out = {r: [] for r in readers}
    feed = HUD.Feed() if 'feed' in readers else None
    for t, a in cache.frames(start, end, boxes_for(readers)):
        for r in readers:
            if r == 'feed':                   # stateful: each new line once (as in the scan)
                k = len(feed.lines)
                try: feed.read(a, t)
                except Exception: pass
                out[r] += [(t, {x: l[x] for x in ('event', 'weaponName') if x in l}) for l in feed.lines[k:]]
                continue
            try: v = READERS[r][1](a)
            except Exception as e: v = {'error': repr(e)}
            if v not in (None, {}, False): out[r].append((t, v))
    return out

def _short(r, v):
    if r == 'legends': return '/'.join((s['legend'] or '?') + ('*' if s['self'] else '') for s in v['squad']) + (' ranked' if v.get('ranked') else '')
    if r == 'banner': return f"{v.get('mode') or '?'} {v.get('map') or '?'}"
    if r == 'own': return ' '.join(f'{k[0]}{v[k]}' for k in ('kills', 'assists', 'knocks', 'damage') if k in v)
    if r == 'ring': return f"{'pre ' if v['pre'] else ''}{v['v']}"
    if r == 'dbg': return 'pos' if v['pos'] else 'vel'
    if r == 'feed': return v.get('event', 'line')
    if r == 'heal': return f"{v['item'] or '?'} {v['p']:.1f}"
    if r == 'abil': return f"tac {v.get('tac') or '-'} ult {v.get('ult') or '-'}"
    if r == 'ownbar': return v['state'] if v['state'] != 'alive' else f"{v['tier'] or '-'} {v['shield']}/{v['health']}"
    return str(v)

def summary(reads, per='minute'):
    """Per minute (or per game, a game = from the start of one legend select screen to the next): for each reader
    the number of reads and the value read most often."""
    if per == 'game':
        sel = [{'t': t, 'kind': 'select'} for t, _ in reads.get('legends', [])]
        runs = []                             # [first, last] of each real select screen (8 s to 2 min, no hole)
        for e in HUD.select_runs(sel):
            if runs and e['t'] - runs[-1][1] <= 5: runs[-1][1] = e['t']
            else: runs.append([e['t'], e['t']])
        starts = [0.0] + [r[0] for r in runs]
        key = lambda t: max(i for i, s in enumerate(starts) if t >= s)
        label = lambda k: f'game {k} (from {starts[k]:.0f} s)' if k else 'before the first select'
    else:
        key = lambda t: int(t // 60); label = lambda k: f'{k:4d} min'
    groups = collections.defaultdict(dict)
    for r, L in reads.items():
        for t, v in L: groups[key(t)].setdefault(r, []).append(_short(r, v))
    lines = []
    for k in sorted(groups):
        parts = []
        for r in reads:
            vals = groups[k].get(r)
            if vals: v, c = collections.Counter(vals).most_common(1)[0]; parts.append(f'{r} {len(vals)}x [{v}]' + (f' {c}/{len(vals)}' if c < len(vals) else ''))
        lines.append(f'{label(k)}: ' + '; '.join(parts))
    return lines


# ---------- verify ----------
def _norm(v):
    """Reads are compared exactly (portrait scores included), as stored in JSON."""
    return json.loads(json.dumps(v))

def verify(cache):
    """Compare the reads stored at build time (real decoded frames) with the same readers on the cache."""
    ref = json.load(open(os.path.join(cache.dir, 'reads.json')))
    by_t = {round(r['t'], 3): r for r in ref['reads']}
    names = ref['readers']; same = collections.Counter(); diff = collections.Counter(); nonnull = collections.Counter(); ex = []
    n = 0
    for t, a in cache.frames(boxes=boxes_for(names)):
        r0 = by_t.get(round(t, 3))
        if r0 is None: continue
        n += 1
        for name in names:
            try: v = READERS[name][1](a)
            except Exception as e: v = {'error': repr(e)}
            x, y = _norm(r0[name]), _norm(v)
            if x not in (None, {}, False): nonnull[name] += 1
            if x == y: same[name] += 1
            else:
                diff[name] += 1
                if len(ex) < 20: ex.append((t, name, x, y))
    print(f'{n} keyframes compared (of {len(by_t)} read at build time)')
    print(f'{"reader":10s} {"same":>6s} {"differ":>6s} {"non-empty on real frames":>26s}')
    for name in names: print(f'{name:10s} {same[name]:6d} {diff[name]:6d} {nonnull[name]:26d}')
    for t, name, x, y in ex: print(f'  differs at {t:.2f} {name}: real {x} / cache {y}')
    return n, same, diff, nonnull


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', choices=('build', 'run', 'verify'))
    ap.add_argument('target', help='recording (build) or recording / cache folder (run, verify)')
    ap.add_argument('--cache', help='cache root folder (default: $PCS_REGION_CACHE)')
    ap.add_argument('--start', type=float, default=0.0); ap.add_argument('--end', type=float)
    ap.add_argument('--verify', action='store_true', help='build: also store the reads on the real frames')
    ap.add_argument('--workers', type=int, default=0)
    ap.add_argument('--boxes', help='build: only these boxes (' + ','.join(BOXES) + '); default all')
    ap.add_argument('--readers', default='squads,own,legends,banner', help='run: ' + ','.join(list(READERS) + ['feed']))
    ap.add_argument('--per', default='minute', choices=('minute', 'game'))
    a = ap.parse_args()
    if a.cmd == 'build':
        build(a.target, a.cache, a.start, a.end, a.verify, a.workers, a.boxes.split(',') if a.boxes else None); return
    c = Cache(cache_dir(a.target, a.cache))
    if a.cmd == 'verify': verify(c); return
    readers = [r.strip() for r in a.readers.split(',') if r.strip()]
    if a.per == 'game' and 'legends' not in readers: readers.append('legends')
    t0 = time.time(); reads = run_readers(c, readers, a.start, a.end); dt = time.time() - t0
    for line in summary(reads, a.per): print(line)
    print(f'{", ".join(readers)}: {sum(len(v) for v in reads.values())} reads in {dt:.1f} s')

if __name__ == '__main__':
    main()
