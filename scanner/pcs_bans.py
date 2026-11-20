"""Legend bans (Ranked Diamond and above): which legends were banned before each game.

What the game shows (checked on the 17 and 18 Nov 2026 Ranked Diamond recordings; the game's own scripts at rest,
`sh_legend_ban_voting` / `sh_character_select`, say the feature is switched on per playlist):
- Before legend select, a BAN PHASE screen (red background, a red bar at the bottom: "Requests increase the chances
  that a Legend will be banned."): each player requests a legend, the game shows each requested legend with its
  chance, rolls, and a box titled BANNED! shows the banned legends for a few seconds (2 in Season 31 Ranked).
- Then legend select: in the grid of all legends the banned ones stay greyed, framed in orange-red with a round "no"
  sign under them, for the whole pick phase (about 25 s).
Both draw a banned portrait the same way: an orange-red frame (about R 232, G 79, B 60) with the "no" sign under the
middle of it. While the game rolls, the candidates look alike but carry a red chance tag ("42.9%") right above the
frame: those are refused. The portrait inside a frame is named with the legend tiles of the private icons file
(`legend_<name>`, as `pcs_hud.Legends`; no game image is in this repo), scaled from the frame's size to the tiles'
size: the grid draws the tiles at about 0.52 of their source size, the BANNED! box at 0.66, the tiles are at 0.443.

Per game (`attach`): `bans` = [{legend, how, reads, hows}] (how: 'ban-screen' when the BANNED! box was read, else
'banned-portrait' = the legend select grid; reads = keyframes the legend was read on; hows = both sources' counts) and
`bansSeen` (True: banned portraits were on screen). `bans` is None when the screen could not be read (no legend select
screen seen, a 4:3 stretched recording): "not read", unlike [] = "no bans shown" (below Diamond, pubs, scrims).
A legend counts when read on 2 keyframes or more. A frame whose portrait matches no legend clearly is kept with
legend None (an unknown or new legend, or no icons file), grouped by its place on screen.

Scrims (EEC custom lobbies) ban by another rule (the most played legend of each game, kept for the rest of the block)
and the lobby's export names those bans: the hub takes them from EEC. Whether a custom lobby draws its bans like this
is not seen yet (no scrim recording with a legend select on this PC): the reader would report them if it does.

Checked (by eye on the BANNED! box; truths `checkset-bans.json` in the private handoff folder): 25 Ranked Diamond games on 17 / 18 Nov, 50 of 50
bans named right, no third legend, no unnamed read (tuned on the first game only). No ban read on every keyframe of 3
recordings without bans shown. 16:9 only. Cost: a few microseconds per keyframe outside the ban and select screens
(the bottom bar test), about 40 ms per select keyframe and 120 ms per BANNED! keyframe.
"""
import numpy as np

try:
    from pcs_hud import weapon_file
except Exception:                      # pragma: no cover
    def weapon_file(): return None

TILE = 0.443          # scale of the legend tiles in the icons file (the roster draws them at that size)
SRC_W = 195.0         # a frame's width / the tiles' draw scale (grid ~102 px at 0.52, BANNED! box ~128 px at 0.66)
BANDS = ((660, 900, 118, 140, 'ban-screen'), (880, 1300, 92, 112, 'banned-portrait'))   # rows, frame width min / max
MIN, GAP = 0.55, 0.06  # accept a name: score, and lead over the next legend
PH, PW = 40, 34        # the face patch compared, in tile pixels
MIN_READS = 2          # keyframes a legend must be read on
BEFORE = 60            # s before the legend select screen: the ban phase (the BANNED! box shows ~10 s before it)


def orange(a):
    """Pixels of the ban colour (orange-red frame, sign and bars)."""
    R = a[..., 0].astype(np.int16); G = a[..., 1].astype(np.int16); B = a[..., 2].astype(np.int16)
    return (R >= 190) & (G >= 40) & (G <= 120) & (B >= 20) & (B <= 105) & (R - G >= 100)


def ban_phase(a):
    """Cheap test (microseconds): the ban phase's red bar at the bottom of the screen (rows ~1340..1418)."""
    if a.shape[0] != 1440 or a.shape[1] != 2560: return False
    return float(orange(a[1346:1414:16, 8:2552:24]).mean()) >= 0.6


def no_sign(a, xm, y0, w):
    """The round "no" sign under the middle of a banned portrait's frame: a dark disc in an orange-red ring, its centre
    1.09 (BANNED! box) to 1.15 (grid) frame widths below the frame's top edge."""
    r = int(w * 0.12); R = int(w * 0.2)
    for yc in range(y0 + int(w * 1.05), y0 + int(w * 1.2), 3):
        core = a[yc - r:yc + r, xm - r:xm + r]; ring = a[yc - R:yc + R, xm - R:xm + R]
        if core.size == 0 or ring.size == 0: continue
        if (core.max(2) < 70).mean() >= 0.25 and orange(ring).mean() >= 0.2: return True
    return False


def frames(a):
    """-> [(how, x0, y0, x1)]: orange-red banned-portrait frames on a 2560 x 1440 frame (top edge at row y0, x0..x1)."""
    out = []
    if a.shape[0] != 1440 or a.shape[1] != 2560: return out
    for y0, y1, wmin, wmax, how in BANDS:
        M = orange(a[y0:y1]); seen = []
        for y in np.flatnonzero(M.sum(1) >= wmin):
            if y >= M.shape[0] - 10: break
            d = np.diff(np.concatenate(([0], M[y].view(np.int8), [0])))
            for s, e in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):
                w = int(e - s)
                if not wmin <= w <= wmax: continue
                if any(abs(s - sx) < 8 and 0 <= y - sy < 12 for sx, sy in seen): continue     # same frame, next row
                if y >= 4 and M[y - 4:y - 1, s:e].mean() > 0.5: continue          # inside a block (a chance tag), not an edge
                h = int(w * 0.8)
                if y + h > M.shape[0]: continue
                L = M[y:y + h, s:s + 4].any(1).mean(); R = M[y:y + h, e - 4:e].any(1).mean()   # side edges
                inner = M[y + 8:y + h, s + 8:e - 8].mean()                                      # not a filled block
                tag = M[y - 30:y - 6, s:e].mean() if y >= 30 else 0.0       # a chance tag above: a candidate, not a ban
                if max(L, R) >= 0.8 and min(L, R) >= 0.4 and inner < 0.3 and tag < 0.4 \
                        and no_sign(a, int(s + e) // 2, int(y + y0), w):
                    seen.append((s, y)); out.append((how, int(s), int(y + y0), int(e)))
    return out


class Bans:
    """Names banned portraits. The tiles come from the private icons file (keys 'legend_<name>', see pcs_hud.Legends);
    without it the frames are still found, unnamed."""
    def __init__(self, path=None):
        self.T = []
        try:
            z = np.load(path or weapon_file())
            for n in z.files:
                if not n.startswith('legend_') or n == 'legend_dummie': continue
                t = z[n].astype(np.float64)
                if (t > 0).sum() < 200 or t.shape[0] < PH or t.shape[1] < PW: continue
                I = lambda x: np.pad(x, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
                box = lambda J: J[PH:, PW:] - J[:-PH, PW:] - J[PH:, :-PW] + J[:-PH, :-PW]
                S, S2, C = box(I(t)), box(I(t * t)), box(I((t > 0).astype(np.float64))) / (PH * PW)
                den = np.sqrt(np.maximum(S2 - S * S / (PH * PW), 1e-6))
                self.T.append((n[7:], t.shape, np.fft.rfft2(t), den, C >= 0.97))
        except Exception:
            self.T = []

    def name(self, a, x0, y0, x1):
        """-> (legend or None, score) for the portrait inside the frame whose top edge is (x0, y0)..(x1, y0).
        The tiles show the middle of the source picture (from 0.30 of its width), the grid a window from about 0.2:
        the face patch is taken from the right part of the frame, at three places, best score kept per legend."""
        if not self.T: return None, 0.0
        w = x1 - x0; k = TILE / (w / SRC_W)
        g = a[y0 + 6:y0 + int(w * 0.95), x0 + 6:x1 - 6].astype(np.float64)
        if g.size == 0: return None, 0.0
        g = 0.299 * g[..., 0] + 0.587 * g[..., 1] + 0.114 * g[..., 2]
        h2, w2 = int(g.shape[0] * k), int(g.shape[1] * k)
        g = g[(np.arange(h2) / k).astype(int)][:, (np.arange(w2) / k).astype(int)]
        best = {}
        for cx in (int(w2 * 0.3), int(w2 * 0.4), int(w2 * 0.5)):
            p = g[12:12 + PH, cx:cx + PW]
            if p.shape != (PH, PW): continue
            p = p - p.mean(); pn = np.sqrt((p * p).sum()) + 1e-6
            for n, shp, F, den, ok in self.T:
                num = np.fft.irfft2(F * np.conj(np.fft.rfft2(p, s=shp)), s=shp)[:shp[0] - PH + 1, :shp[1] - PW + 1]
                best[n] = max(best.get(n, -1.0), float(np.where(ok, num / (den * pn), -1.0).max()))
        res = sorted(((v, n) for n, v in best.items()), reverse=True)
        if not res: return None, 0.0
        ok = res[0][0] >= MIN and (len(res) < 2 or res[0][0] - res[1][0] >= GAP)
        return (res[0][1] if ok else None), round(res[0][0], 3)

    def read(self, a):
        """a: full 16:9 frame (1440 x 2560 x 3). -> [(how, legend or None, score, x, y)] banned portraits on screen."""
        out = []
        for how, x0, y0, x1 in frames(a):
            n, sc = self.name(a, x0, y0, x1)
            out.append((how, n, sc, x0, y0))
        return out

    def read_if(self, a, select):
        """The scan's per-keyframe call: reads only on the legend select screen (`select`: pcs_hud.Legends saw it) or
        the ban phase (its bottom bar), else nothing (a few microseconds)."""
        if not (select or ban_phase(a)): return None
        return self.read(a) or None


def attach(games, reads, stretched=False):
    """reads: [(t, [(how, legend, score, x, y)])] from Bans.read_if over the whole recording. Sets games[]['bans'] and
    ['bansSeen'] (see the module text). A game's window: from BEFORE s before its legend select screen (else 150 s
    before its start) to its start, never before the previous game's start."""
    starts = [g['tStart'] for g in games]
    for i, g in enumerate(games):
        sel = g.get('select') or {}
        g['bans'] = None; g['bansSeen'] = False
        if stretched: continue                       # 4:3 stretched: not read (16:9 only)
        lo = (sel.get('tFirst', g['tStart']) - BEFORE) if sel else g['tStart'] - 150
        lo = max(lo, starts[i - 1] + 1 if i else -1e12)
        hi = max(g['tStart'], sel.get('tLast', g['tStart'])) + 2
        legends = {}                                 # a named legend, or an unnamed frame by its place on screen
        for t, rr in reads:
            if not lo <= t <= hi: continue
            for how, n, sc, x, y, *_ in (tuple(r) + (0,) if len(r) == 4 else tuple(r) for r in rr):
                key = n if n else ('?', how, int(x) // 24, int(y) // 40)
                e = legends.setdefault(key, {'legend': n, 'reads': 0, 'hows': {}})
                e['reads'] += 1; e['hows'][how] = e['hows'].get(how, 0) + 1
        legends = {k: e for k, e in legends.items() if e['reads'] >= MIN_READS}
        if not sel and not legends:
            continue                                 # no select screen and nothing seen: stays "not read"
        out = []
        for e in legends.values():
            e['how'] = 'ban-screen' if 'ban-screen' in e['hows'] else 'banned-portrait'
            out.append(e)
        named = [e for e in out if e['legend']]
        if named:                                   # an unnamed group next to named ones is one of them, seen off-place
            out = named + [e for e in out if not e['legend'] and e['reads'] >= 3 * MIN_READS]
        g['bans'] = sorted(out, key=lambda e: (-e['reads'], e['legend'] or ''))
        g['bansSeen'] = bool(out)
