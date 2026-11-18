"""Position over time from the HUD minimap (top left), for players without the debug overlay.

The HUD minimap is north-up (it never turns with the view: the player's arrow turns) and always shows the same world
scale: about `U` world units per screen pixel at 1440p on every map, from the dropship to the end. Its centre is the
player. So a position is the place on the game's own minimap picture where the HUD crop fits best: a translation
search only (no rotation, no zoom).

Scan side (`grab`, every `EVERY` s of normal HUD, 16:9): the crop is checked (the minimap's white corner brackets at
their normal place: while spectating the HUD moves 85 px down and the minimap is someone else's; menus, map open,
death screens have no brackets) and kept small: grey, averaged over 4 x 4 pixels (70 x 79 bytes) + which of those
cells are free of icons. Matching runs once the scan knows each game's map (`attach`), on the game's own minimap
picture (EA's file: never in the repo; found at run time like the weapon icons, see `pictures`).

Matching (`Ref.match`): both sides as local contrast (high-pass, unit local spread: the ring's red tint and the
see-through rings change colours, not edges), icons masked, normalised correlation by FFT. With the last good
position known the search covers only where the player could have moved; else the whole map. A read counts when its
score is high enough AND a read within `CONFIRM_S` s agrees with it (never trust a single read).

Output per game: `minimapTrack` = {"v": 1, "every": s, "pts": [[t, mapX, mapY, q], ...]} with map % (the picture's
frame = the app's map frame, as `landing.mapX`) and q = match score x 100. Gaps (indoors, menus, spectating) are
simply missing points.
"""
import os, sys, json
import numpy as np

HERE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
EVERY = 3.0                 # seconds between minimap reads
LEVELS = (78.0, 62.5, 56.5, 45.0)   # minimap zoom levels seen: world units per HUD pixel at 1440p (same on every map)
CY, CX = 226.0, 226.5       # the minimap's centre (the player) on a 2560 x 1440 frame
R0, R1, C0, C1 = 104, 384, 70, 386      # the part matched: rows from 104 leave out the debug overlay's lines
D = 4                       # kept at 1/4 size: 70 x 79 cells
SIG = 4.0                   # local contrast window (cells)
ACCEPT = 0.33               # match score needed
CONFIRM_S = 12.0            # a read needs the read before or after it, this close in time, to agree with it
SPEED = 1500.0              # fastest horizontal move (world units per s; free fall incl.) for the search and checks
NEAR = 0.006                # agreement slack (fraction of the map) on top of SPEED x time
PAD = 48                    # cells of plain around the picture (the HUD minimap near a map edge)
OUTSIDE = 35.0              # grey of the HUD minimap beyond the map's picture
GLOBAL_EVERY = 9.0          # whole-map searches at most this often while lost

# map code -> (internal name, pos_x, pos_y, scale): the game's overview transform for its 4096 px minimap
# (world -> picture fraction: (x - pos_x) / scale / 4096, (pos_y - y) / scale / 4096). Same values as the hub's
# tools/upload_minimaps.py.
MAPS = {
    'WE': ('mp_rr_desertlands_mu5', -45056, 45055, 22),
    'SP': ('mp_rr_tropic_island_mu2_landscape', -50606, 52139, 25),
    'ED': ('mp_rr_district_mu1', -43008, 43008, 21),
    'BM': ('mp_rr_divided_moon_mu1', -45000, 43500, 21),
    'KC': ('mp_rr_canyonlands_hu_avt', -37541, 43886, 20),
    'OL': ('mp_rr_olympus_mu3', -52024, 48025, 22),
}


# ---------- scan side: one crop every EVERY s ----------
def present(img):
    """The minimap at its normal place: white corner brackets above and below it (rows 62..67 and 385..390, x 336..386),
    and not inside an all-white area (sky, menu)."""
    def white(r0, r1):
        s = img[r0:r1, 336:386].astype(np.int16)
        mn = np.minimum(np.minimum(s[..., 0], s[..., 1]), s[..., 2]); mx = np.maximum(np.maximum(s[..., 0], s[..., 1]), s[..., 2])
        return (mn > 200) & (mx - mn < 40)
    return white(62, 67).any(0).mean() >= 0.9 and white(385, 390).any(0).mean() >= 0.9 and white(70, 82).mean() < 0.6


def grab(img):
    """-> (grey cells uint8 70 x 79, packed mask of icon-free cells) or None when no minimap is shown."""
    if img is None or img.shape[0] != 1440 or img.shape[1] != 2560 or not present(img):
        return None
    s = img[R0:R1, C0:C1]
    r, g, b = s[..., 0].astype(np.uint16), s[..., 1].astype(np.uint16), s[..., 2].astype(np.uint16)
    grey = (r * 77 + g * 150 + b * 29) >> 8
    mn = np.minimum(np.minimum(r, g), b); mx = np.maximum(np.maximum(r, g), b)
    icon = ((mx - mn) > 90) | (mn > 200)            # icons, arrows, pings, ring lines: saturated or very bright
    h, w = grey.shape[0] // D, grey.shape[1] // D
    gc = grey[:h * D, :w * D].reshape(h, D, w, D).mean((1, 3))
    ic = icon[:h * D, :w * D].reshape(h, D, w, D).mean((1, 3))
    keep = ic <= 0.06
    return np.round(gc).astype(np.uint8), np.packbits(keep)


def unpack(read):
    gc, mb = read
    m = np.unpackbits(mb)[:gc.size].reshape(gc.shape).astype(bool)
    return gc.astype(np.float32), m


# ---------- matching ----------
def _prep(g):
    from scipy import ndimage
    hp = g - ndimage.gaussian_filter(g, SIG)
    return hp / (np.sqrt(ndimage.gaussian_filter(hp * hp, SIG)) + 2.0)


def _ncc(img, tpl, m):
    """Masked normalised correlation of tpl (mask m) at every position of img where it fits (Padfield)."""
    from scipy import fft as sfft
    H, W = img.shape; h, w = tpl.shape
    sh = (sfft.next_fast_len(H + h), sfft.next_fast_len(W + w))
    t = tpl * m; nm = max(float(m.sum()), 1.0)
    Fi = sfft.rfft2(img, sh); Fi2 = sfft.rfft2(img * img, sh)
    Fm = sfft.rfft2(m[::-1, ::-1], sh); Ft = sfft.rfft2(t[::-1, ::-1], sh)
    s1 = sfft.irfft2(Fi * Fm, sh); s2 = sfft.irfft2(Fi2 * Fm, sh); st = sfft.irfft2(Fi * Ft, sh)
    tm = t.sum() / nm
    var_t = float(((t - tm * m) ** 2).sum())
    c = (st - s1 * tm) / np.sqrt(np.maximum(s2 - s1 * s1 / nm, 1e-3) * max(var_t, 1e-6))
    return c[h - 1:H, w - 1:W]


class Ref:
    """A map's minimap picture prepared for matching at each zoom level (about 0.1 s once per map)."""
    def __init__(self, code, picture, levels=None):
        from PIL import Image
        _, self.pos_x, self.pos_y, self.scale = MAPS[code]
        self.code = code
        im = picture.convert('L') if hasattr(picture, 'convert') else Image.fromarray(np.asarray(picture)).convert('L')
        self.S = im.size[0]
        self.levels = {}
        for u in (levels or LEVELS):
            k = u / self.scale * self.S / 4096       # picture px per HUD px
            n = int(round(self.S / (D * k)))
            g = np.full((n + 2 * PAD, n + 2 * PAD), OUTSIDE, np.float32)    # the HUD shows a dark plain beyond the map
            g[PAD:PAD + n, PAD:PAD + n] = np.asarray(im.resize((n, n), Image.BOX), np.float32)
            self.levels[u] = (self.S / n, _prep(g))  # picture px per cell, cells with PAD cells around

    def frac(self, x, y):
        return (x - self.pos_x) / self.scale / 4096, (self.pos_y - y) / self.scale / 4096

    def match(self, read, u, prior=None, radius=None):
        """At zoom u -> (fx, fy, score) or None. prior = (fx, fy) picture fractions, radius = fraction of the map."""
        g, m = read if isinstance(read, Prepared) else Prepared(read)
        if g is None:
            return None
        cs, img = self.levels[u]; n = img.shape[0]; h, w = g.shape
        cy0, cx0 = (CY - R0 + 0.5) / D - 0.5 - PAD, (CX - C0 + 0.5) / D - 0.5 - PAD   # template corner -> centre (picture cells)
        y0, x0, y1, x1 = 0, 0, n, n
        if prior is not None:
            f = self.S / cs; r = radius * f
            py, px = prior[1] * f - 0.5, prior[0] * f - 0.5      # picture cells
            y0 = int(max(0, py - cy0 - r)); x0 = int(max(0, px - cx0 - r))
            y1 = int(min(n, np.ceil(py - cy0 + r) + h)); x1 = int(min(n, np.ceil(px - cx0 + r) + w))
            if y1 - y0 < h + 2 or x1 - x0 < w + 2:
                return None
        c = _ncc(img[y0:y1, x0:x1], g, m)
        yy, xx = divmod(int(np.argmax(c)), c.shape[1]); best = float(c[yy, xx])
        def para(a, b, d):
            q = a - 2 * b + d
            return 0.0 if q >= 0 else max(-0.5, min(0.5, 0.5 * (a - d) / q))
        dy = para(c[yy - 1, xx], best, c[yy + 1, xx]) if 0 < yy < c.shape[0] - 1 else 0.0
        dx = para(c[yy, xx - 1], best, c[yy, xx + 1]) if 0 < xx < c.shape[1] - 1 else 0.0
        f = cs / self.S
        return (x0 + xx + dx + cx0 + 0.5) * f, (y0 + yy + dy + cy0 + 0.5) * f, best


class Prepared(tuple):
    """A read as (local-contrast cells, mask) once, for several matches; (None, None) when mostly icons."""
    def __new__(cls, read):
        g, m = unpack(read)
        if m.mean() < 0.4:
            return tuple.__new__(cls, (None, None))
        return tuple.__new__(cls, (_prep(g), m.astype(np.float32)))


def track(reads, ref):
    """reads = [(t, read)] of one game in time order -> [[t, fx, fy, score, zoom]] confirmed positions (fractions).
    The zoom is kept from read to read; the other levels are tried when the kept one fits nowhere near."""
    unit = 1.0 / (ref.scale * 4096)                  # world unit -> fraction of the map
    found, last, last_global, zoom = [], None, -1e9, None
    for t, rd in reads:
        p = Prepared(rd)
        if p[0] is None:
            continue
        res = None
        if last is not None and t - last[0] <= 30:
            rad = min(0.25, 0.01 + SPEED * unit * (t - last[0]))
            for u in [zoom] + [u for u in ref.levels if u != zoom]:
                r = ref.match(p, u, last[1:3], rad)
                if r and r[2] >= ACCEPT:
                    res = r + (u,); break
        if res is None and t - last_global >= GLOBAL_EVERY:
            last_global = t
            best = None
            for u in ([zoom] if zoom else []) + [u for u in ref.levels if u != zoom]:
                r = ref.match(p, u)
                if r and (best is None or r[2] > best[2]):
                    best = r + (u,)
            if best and best[2] >= ACCEPT:
                res = best
        if res:
            found.append([t, res[0], res[1], res[2], res[3]])
            last = (t, res[0], res[1]); zoom = res[3]
    # never trust a single read: keep a read when the read just before or just after it (within CONFIRM_S) agrees
    out = []
    for i, (t, fx, fy, q, u) in enumerate(found):
        for j in (i - 1, i + 1):
            if 0 <= j < len(found) and abs(found[j][0] - t) <= CONFIRM_S and                     np.hypot(found[j][1] - fx, found[j][2] - fy) <= NEAR + SPEED * unit * abs(found[j][0] - t):
                out.append([t, fx, fy, q, u]); break
    return out


# ---------- the pictures (EA's files: never in the repo) ----------
def pictures():
    """map code -> picture path. PCS_MINIMAPS = a folder with the RSX exports (`<internal>.jpg` / `.png` or a folder
    `<internal>` with the 4096 px png); else the app's data folder `minimaps/` (downloaded from the team hub by
    pcs_recorder.minimaps: index.json + <code>-<asset>.img)."""
    out = {}
    root = os.environ.get('PCS_MINIMAPS')
    if root and os.path.isdir(root):
        for code, (internal, *_r) in MAPS.items():
            jpg, pngs = None, []
            for d, _ds, fs in os.walk(root):
                for f in fs:
                    p = os.path.join(d, f)
                    if f.lower() in (internal + '.jpg', internal + '.png'):
                        jpg = jpg or p
                    elif f.lower().endswith('.png') and internal in os.path.normpath(d).split(os.sep):
                        pngs.append(p)
            p = jpg or (max(pngs, key=os.path.getsize) if pngs else None)
            if p: out[code] = p
    data = os.environ.get('PCS_DATA_DIR')
    if data:
        try:
            idx = json.load(open(os.path.join(data, 'minimaps', 'index.json'), encoding='utf-8'))
            for code, e in (idx.get('maps') or {}).items():
                p = os.path.join(data, 'minimaps', f"{code}-{e.get('asset')}.img")
                if code in MAPS and code not in out and os.path.exists(p) and _same_tx(code, e):
                    out[code] = p
        except (OSError, ValueError, AttributeError):
            pass
    return out


def _same_tx(code, e):
    """The hub's transform for the picture is the one this module assumes (a new season's map would differ)."""
    try:
        _, px, py, sc = MAPS[code]
        return abs(float(e['pos_x']) - px) < 1 and abs(float(e['pos_y']) - py) < 1 and abs(float(e['scale']) - sc) < 1e-3
    except (KeyError, TypeError, ValueError):
        return False


_REFS = {}


def load_ref(code):
    """The prepared picture of a map (kept for the scan), or None when there is no picture."""
    cache = _REFS
    if code in cache: return cache[code]
    ref = None
    p = pictures().get(code)
    if p:
        try:
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = None
            with Image.open(p) as im:
                if im.size[0] > 2048:
                    im = im.convert('L').resize((2048, 2048), Image.BOX)
                ref = Ref(code, im)
        except Exception:
            ref = None
    cache[code] = ref
    return ref


def attach(games, reads):
    """reads = [(t, read)] of the whole recording. Adds games[].minimapTrack for games on a map with a picture."""
    reads = sorted(reads, key=lambda r: r[0])
    for gi, g in enumerate(games):
        if g.get('map') not in MAPS:
            continue
        lo = g['tStart']; hi = games[gi + 1]['tStart'] if gi + 1 < len(games) else float('inf')
        end = g.get('tEndGame')
        if end is not None: hi = min(hi, end + 1)
        mine = [r for r in reads if lo <= r[0] < hi]
        if not mine:
            continue
        ref = load_ref(g['map'])
        if ref is None:
            g['minimapTrack'] = {'v': 1, 'every': EVERY, 'pts': [], 'reads': len(mine), 'noPicture': True}
            continue
        pts = track(mine, ref)
        g['minimapTrack'] = {'v': 1, 'every': EVERY, 'reads': len(mine),
                             'pts': [[round(float(t), 1), round(100 * float(fx), 2), round(100 * float(fy), 2), int(round(100 * q))] for t, fx, fy, q, _u in pts]}
