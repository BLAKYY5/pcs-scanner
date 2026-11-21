"""Fights (won / lost / even, third parties) and ring damage taken, per game, from what the scan already reads.

Ring damage (plan item 4). Outside the ring the game paints the screen edges red-orange (a thick haze all round the
view, "GET BACK IN THE RING!" in the middle) and takes health, never shield, every 1.5 s (deathfield script:
damage checked every 1.5 s, a per-stage share of max health). Two signals:
  - `read_storm` on every in-game keyframe: share of red-tinted pixels in four thin strips at the screen edges (left,
    right, top, bottom; the HUD sits further in). Outside the ring three or four strips are mostly red (0.5..1.0);
    in normal play they are 0..0.05. Low health alone tints them less (one or two strips).
  - ring ticks on the player's own card (`pcs_bars.read_own` reads): health drops in steps of one size while the
    shield stays the same (ring damage skips the shield). Ticks seen on our recordings: 3 per tick (rounds 1-2).
A stretch outside = a run of ring ticks (steady: 3+ of one size with the shield up, 4+ without; or 2+ inside red
edges), widened by the red-edge reads around it (two or more, own card alive: knocked / dying screens are red too).
The red edges alone never make a stretch: Storm Point's red grass and orange walls tint the strips as much. Its damage =
the ticks it lasted (seconds / 1.5) x the tick size measured in it (else in the same game and round); the health
drops actually read are kept too (`measured`), as heals during the stretch hide some of it.

Fights (plan items 3 and 12). Evidence of a fight, per game:
  - kill-feed lines with one side in our squad's colours (our knocks / kills, our players knocked / killed), counted
    once each as in `pcs_hud.feed_stats`, with the enemy name's shape (`killerSig` / `victimSig`: tells names apart
    without reading them);
  - the player's own knocks (`ownKnocks`) and shield / health lost outside the ring;
  - gunfire: game sound well above the game's usual level (`pcs_audio` loudness), only to see shooting going on after
    the opponents died. Measured on 7 Nov: 10 dB windows are 29 % of fight time and 12 % elsewhere, so loudness alone
    neither finds fights nor their ends (as the audio cross-check found); it does not join or stretch fights.
Evidence closer than FIGHT_GAP joins one fight. Result (`verdict`, coach's decision 32, 20 Nov 2026: "a fight is
neutral unless there is a knock on a side; a huge damage entry or taken also counts"): kills decide (the player's own
death in the fight counts as a kill on us); with as many kills each side, any knock lead; with as many knocks, the
player's damage dealt (`damageDealt`: their own damage number's rise during the fight) against damage taken
(`damageTaken`) when they differ by DAMAGE_LEAD or more; a squad wiped in it always lost; else even. Kill-feed names in a colour none of our squad has (a coloured
background behind a white name) are not ours. Third party: a new enemy name after the fight started (`new enemy`)
once more than a squad's worth of names showed, an enemy we had not met knocking or killing one of us late in the
fight, or shooting / hits still going on after every opponent we met died (not when an enemy line on one of us is
newer than our last kill: that enemy was still up).
Checked by eye on the coach's recordings (kill-feed line images + the own card), dev / held-out split decided first:
`pcs-handoff/checkset/checkset-fights.json`.
"""
from __future__ import annotations

import numpy as np

# ---------- ring damage ----------
STORM_STRIPS = {'L': (250, 1000, 0, 48), 'R': (300, 1150, 2512, 2560), 'T': (0, 24, 600, 1900), 'B': (1400, 1440, 700, 1850)}
STORM_ON = 0.2          # a strip counts as tinted when this share of it is red
STORM_MIN_STRIPS = 3    # ... and outside the ring needs this many tinted strips
STORM_MEAN = 0.35       # ... and this mean share over the four
TICK_S = 1.5            # the deathfield's damage step (script default)
TICK_MAX = 30           # a health drop larger than this per read is not one ring tick
TICK_SPREAD = 2         # ticks of one stretch differ by at most this much (health %)
TICK_GAP = 4.5          # s between ticks of one run
STORM_GAP = 5.0         # s between storm reads of one stretch
STORM_READS = 2         # storm reads a stretch needs
TICKS_ALONE = 4         # a run of ticks with no storm read must have this many (with the shield up: 3)


def read_storm(a):
    """The four edge strips' share of red-tinted pixels, (L, R, T, B) rounded to 0.01. a: a 2560 x 1440 RGB frame."""
    out = []
    for y0, y1, x0, x1 in STORM_STRIPS.values():
        p = a[y0:y1, x0:x1]
        r = p[..., 0].astype(np.int16); gb = np.maximum(p[..., 1], p[..., 2]).astype(np.int16)
        out.append(round(float(((r > 90) & (r - gb > 40)).mean()), 2))
    return tuple(out)


def storm_outside(s):
    """One storm read -> True when it looks like the player stands outside the ring."""
    return s is not None and sum(v >= STORM_ON for v in s) >= STORM_MIN_STRIPS and sum(s) / 4 >= STORM_MEAN


def _alive(o):
    return bool(o) and o.get('state') == 'alive' and o.get('health') is not None and o.get('shield') is not None


def ring_ticks(own):
    """own: [(t, read_own dict)] in time order -> [(t, drop, dt, shield)]: health drops from one read to the next while
    the shield stays the same, not undone by the read after (damage flashes)."""
    out = []
    for i in range(1, len(own)):
        (tp, p), (t, o) = own[i - 1], own[i]
        if not (_alive(p) and _alive(o)) or t - tp > 2.5: continue
        d = p['health'] - o['health']
        if not (1 <= d <= TICK_MAX) or o['shield'] != p['shield']: continue
        if i + 1 < len(own) and _alive(own[i + 1][1]) and own[i + 1][1]['health'] > o['health'] + 2 \
           and own[i + 1][1]['health'] >= p['health'] - 1: continue          # flash: back up at once
        out.append((t, d, t - tp, o['shield']))
    return out


def _tick_runs(ticks):
    runs = []
    for k in ticks:
        if runs and k[0] - runs[-1][-1][0] <= TICK_GAP: runs[-1].append(k)
        else: runs.append([k])
    return runs


def _steady(run):
    """A run of drops that look like ring ticks: one size (single-tick reads), and enough of them."""
    single = [d for t, d, dt, s in run if dt <= 1.2] or [d for t, d, dt, s in run]
    if max(single) - min(single) > TICK_SPREAD: return None
    need = 3 if all(s > 0 for t, d, dt, s in run) else TICKS_ALONE
    return float(np.median(single)) if len(run) >= need else None


def _round_at(g, t):
    """The ring round running at t: the last ring phase read that started before it (a round's closing phase is not
    always read; its wait is)."""
    best = None
    for r in g.get('ring') or []:
        a = r.get('firstSeen')
        if a is not None and a - 5 <= t and r.get('round') is not None and (best is None or a >= best.get('firstSeen')):
            best = r
    return (best.get('round'), best.get('kind')) if best else (None, None)


def ring_damage(g, own, storm):
    """own: [(t, read_own)] and storm: [(t, (L, R, T, B))] inside one game -> games[].ringDamage."""
    if not own and not storm: return None
    ticks = ring_ticks(own)
    alive = {round(t): True for t, o in own if _alive(o)}
    # the screen also goes red while knocked or dying: only reads with the player's own card up (alive) count
    out_reads = [t for t, s in storm if storm_outside(s) and alive.get(round(t))]
    # storm stretches: reads closer than STORM_GAP, STORM_READS or more
    stretches = []
    for t in out_reads:
        if stretches and t - stretches[-1][1] <= STORM_GAP: stretches[-1][1] = t; stretches[-1][2] += 1
        else: stretches.append([t, t, 1])
    storms = [(a, b) for a, b, n in stretches if n >= STORM_READS]
    # a stretch outside needs ring ticks: steady ones alone, or two of one size inside a storm stretch. The red
    # edges alone are not enough: Storm Point's red grass and orange walls tint them as much (7 Nov, by eye).
    spans = []
    for run in _tick_runs(ticks):
        a, b = run[0][0], run[-1][0]
        st = [x for x in storms if x[0] - 3 <= b and x[1] + 3 >= a]
        sizes_ = [d for t, d, dt, sh in run]
        if _steady(run) is not None or (st and len(run) >= 2 and max(sizes_) - min(sizes_) <= TICK_SPREAD):
            spans.append([a - TICK_S, b, 'ticks'])
            spans += [[x[0], x[1], 'storm'] for x in st]          # the red edges around it: still outside
    spans.sort()
    merged = []
    for a, b, how in spans:
        if merged and a - merged[-1][1] <= STORM_GAP:
            merged[-1][1] = max(merged[-1][1], b); merged[-1][2] |= {how}
        else: merged.append([a, b, {how}])
    sizes = []
    segs = []
    for a, b, how in merged:
        mine = [k for k in ticks if a - 0.5 <= k[0] <= b + 0.5]
        single = [d for t, d, dt, s in mine if dt <= 1.2]
        per = float(np.median(single)) if single else None
        rnd, kind = _round_at(g, a)
        if per is not None: sizes.append((rnd, per))
        segs.append({'t0': round(a, 2), 't1': round(b + 0.75, 2), 'seconds': round(b + 0.75 - a, 1),
                     'perTick': per, 'measured': int(sum(d for t, d, dt, s in mine)), 'round': rnd,
                     'how': '+'.join(sorted(how))})
    for s in segs:
        if s['perTick'] is None:          # no tick read in it: the same round's tick size, else the game's
            same = [p for r, p in sizes if r == s['round'] and r is not None] or [p for r, p in sizes]
            s['perTick'] = float(np.median(same)) if same else None
        n = max(1, int(s['seconds'] // TICK_S))
        s['ticks'] = n
        s['damage'] = max(s['measured'], int(round(n * s['perTick']))) if s['perTick'] else s['measured']
        if s['perTick'] is not None: s['perTick'] = round(s['perTick'], 1)
    return {'seconds': round(sum(s['seconds'] for s in segs), 1), 'damage': int(sum(s['damage'] for s in segs)),
            'measured': int(sum(s['measured'] for s in segs)), 'segments': segs}


# ---------- fights ----------
FIGHT_GAP = 25.0        # s between two pieces of evidence of one fight
HIT_MIN = 5             # shield + health lost in one read to count as a hit
POKE = 50               # a fight with no feed line and no knock needs this much damage taken
KNOCK_LEAD = 1          # with as many kills each side, knocks ahead needed to call it won / lost (decision 32: any)
DAMAGE_LEAD = 100       # with as many kills and knocks, damage dealt - damage taken this far apart decides (see verdict)
DEALT_AFTER = 3.0       # s after a fight's last evidence its damage may still show on the number (read once a second)
DEALT_MAX = 2000        # a rise larger than this in one fight is a misread, not damage
RING_KNOCK = 8.0        # s after a stretch outside the ring a knock is still the ring's
DEATH_AFTER = 25.0      # s after a fight's last evidence the player's death still belongs to it
LOUD_DB = 10.0          # gunfire: a 0.5 s window this far above the game's median loudness
LOUD_AFTER = 0.35       # share of loud windows 3..15 s after the last opponent died that means more shooting
NEW_ENEMY_AFTER = 20.0  # s after the first enemy line for a new name to count as arriving later (a squad comes in together)
SQUAD = 3
DUP_MOVED_BITS = 60   # name shapes (of 144 points) of one line captured again lower in the feed
HUE_MOVED = 35        # ... and its colour read off by this much at most (a red screen)
OPP_SAME_BITS = 60    # two enemy name shapes this close are one enemy (third party: more names than a squad)
OPP_MIN_ONES = 0      # a name shape with fewer points is not counted as an enemy name
ENDED_IN_FIGHT = 3.0  # s: the game ending this soon after a fight's last evidence = the squad went out in it
Y_SAME = 15           # px: one line read at one place (its band's top wobbles ~8 px; lines are ~43 px apart)
DEAD_AFTER = 2.0      # s after an own death (spectating starts) the feed's colours may be another squad's
END_AFTER = 5.0       # s after tEndGame
THIN_SIG = 45         # a name shape with fewer points than this was half hidden: it fits nothing


def _same_sig(a, b):
    if not a or not b: return False
    return bin(int(a, 16) ^ int(b, 16)).count('1') <= 26


def loud_windows(loud, step, t0, t1):
    """Gunfire mask per 0.5 s inside [t0, t1): [(t, bool)] from the 0.05 s loudness array."""
    if loud is None or not len(loud): return []
    k = max(1, int(round(0.5 / step)))
    i0 = max(0, int(t0 / step)); i1 = len(loud) if t1 == float("inf") else min(len(loud), int(t1 / step))
    seg = loud[i0:i1]; n = len(seg) // k
    if n < 10: return []
    w = seg[:n * k].reshape(n, k).max(1)
    thr = float(np.median(w)) + LOUD_DB
    return [(round((i0 + j * k) * step, 2), bool(v > thr)) for j, v in enumerate(w)]


def _hue_near(a, b, tol=20):
    ha, hb = (a or {}).get('hue'), (b or {}).get('hue')
    return ha is not None and hb is not None and min(abs(ha - hb), 360 - abs(ha - hb)) <= tol


def _ones(a):
    return bin(int(a, 16)).count('1') if a else 0


def _bits(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count('1') if a and b else None


def _dup(p, l, strict=False):
    """Two captures of one kill-feed line: same event and weapon, the same squad member (by colour) and the same
    enemy name. Our own name's shape is not compared: the feed appends a distance ('15M') to it now and then. New
    lines come in at the top and push older ones down, so a capture lower than a like line seconds before is that
    line again; the name's shape then only has to be roughly alike (it is read over a moving background)."""
    # one '+N RP' kill line (the same number): its weapon icon can be misread over a red screen (car / nemesis, 17 Nov
    # 3301 / 3303), so the weapon is not compared then
    same_rp = p.get('rp') is not None and p.get('rp') == l.get('rp')
    if l['t'] - p['t'] >= 25 or p['event'] != l['event'] or (p.get('weapon') != l.get('weapon') and not same_rp): return False
    ours = 'killer' if (l.get('killer') or {}).get('side') == 'squad' else 'victim'
    them = 'victim' if ours == 'killer' else 'killer'
    if (p.get(ours) or {}).get('side') != 'squad': return False
    a, b = p.get(them + 'Sig'), l.get(them + 'Sig')
    dy = (l.get('y') or 0) - (p.get('y') or 0)
    if not _hue_near(p.get(ours), l.get(ours)):
        # a Ranked '+N RP' kill line whose killer name was cut off at the feed's left edge (or read over scenery) has no
        # colour: it is ours all the same (only our kills give RP); two captures with the same number are compared by
        # the victim's name alone
        blind = same_rp and ((p.get(ours) or {}).get('hue') is None or (l.get(ours) or {}).get('hue') is None)
        # over a red screen (ring, storm) one name's colour is read up to ~25 degrees off (lime 67 / 46 / 65 on
        # 7 Nov 3585-3589): a line that moved down with the same enemy name is that line again
        moved = (_hue_near(p.get(ours), l.get(ours), HUE_MOVED) and l['t'] - p['t'] < 10 and dy > Y_SAME
                 and (_bits(a, b) or 999) <= DUP_MOVED_BITS)
        if not (blind or moved): return False
    if _same_sig(a, b) or (a is None and b is None): return True
    if strict: return False
    if them == 'killer' and (l['event'] == 'kill' or (l['t'] - p['t'] < 10 and dy >= -Y_SAME)):
        # a line on one of us: the enemy's name LEFT of the weapon is read over the bright scenery beyond the feed's
        # plate, so its shape is often noise (17 Nov 6526 / 6528 / 6532: one knock on us, three shapes 74-79 points
        # apart; 1264-1276: one knock and one kill on Xnoooo, 9 captures at one place). One of us knocked twice by
        # one weapon within 10 s, or killed twice within 25 s, does not happen: it is the same line.
        return True
    if l['t'] - p['t'] < 10 and abs(dy) <= Y_SAME:
        # the same place seconds later: the line did not move (a new line would have pushed it down); the victim's
        # name is on the plate but the plate lets the scenery through, so only roughly alike (or a thin shape: half
        # hidden while the line fades in or out)
        d = _bits(a, b)
        return d is None or d <= DUP_MOVED_BITS or min(_ones(a), _ones(b)) < THIN_SIG
    if l['t'] - p['t'] < 10 and dy > Y_SAME:
        # the line moved down (our kill: the victim's name on the plate, roughly alike over a moving background;
        # a name half hidden by what scrolls in gives a thin shape that fits nothing: the same line too)
        d = _bits(a, b)
        return d is None or d <= DUP_MOVED_BITS or min(_ones(a), _ones(b)) < THIN_SIG
    return False


def dead_spans(g):
    """[(from, to)] of one game where the feed is not our squad's: the player is dead and spectating (from just after
    each own death, `ownDeaths`, to his respawn) and after the game ended for him (`tEndGame`). Once the squad is out,
    the game spectates the killers and colours THEIR names (7 Nov 4299 / 4303 / 5954 / 6128 / 6427: their kills read
    as ours); before the right edge fix these lines were rarely captured in Ranked."""
    back = sorted(r['t'] for r in g.get('respawns') or [] if r.get('who') == 'self')
    out = []
    for d in g.get('ownDeaths') or []:
        out.append((d + DEAD_AFTER, next((r for r in back if r > d), float('inf'))))
    if g.get('tEndGame') is not None: out.append((g['tEndGame'] + END_AFTER, float('inf')))
    return out


def squad_lines(lines, t0, t1, squad=None, last=None, dead=None):
    """Kill-feed lines with one side in our squad inside [t0, t1), each counted once (the feed scrolls and fades).
    squad: [(slot, hue, self)] from the legend select screen: a 'squad' name in none of those colours is not ours
    (a coloured background behind a white name). last: a dict that gets, per line returned (by id), all its captures
    in time order (where the line was seen in the feed: lines never move up)."""
    seen, groups = [], []
    for l in lines:
        if not (t0 <= l['t'] < t1) or 'event' not in l: continue
        if dead and any(a <= l['t'] < b for a, b in dead): continue
        k = (l.get('killer') or {}).get('side'); v = (l.get('victim') or {}).get('side')
        if (k == 'squad') == (v == 'squad'): continue
        side = l.get('killer') if k == 'squad' else l.get('victim')
        # a Ranked '+N RP' kill line is ours even when the killer's name (cut off at the left) gave no colour
        rp_ours = k == 'squad' and l.get('rp') is not None and l.get('marker') == 'rank' and (side or {}).get('hue') is None
        if squad and not rp_ours and _who(side, squad) is None: continue
        # each line counted is followed through its captures. A capture is that line again when it fits the line's
        # LAST capture (same place or lower: lines never move up), or any capture by the same enemy name. Compared
        # with every capture instead, a new line at the top fit an old one's first capture there although the old
        # line had already moved down (17 Nov 2045 / 2048: two knocks by one player with one weapon, 3 s apart).
        grp = next((G for G in groups if _dup(G[-1], l) or any(_dup(p, l, strict=True) for p in G)), None)
        if grp is None: groups.append([l]); seen.append(l)
        else: grp.append(l)
    if last is not None:
        for G in groups: last[id(G[0])] = G
    return seen


def hits(own, ring_segs):
    """Shield + health lost per read outside ring stretches: [(t, amount)]."""
    out = []
    for i in range(1, len(own)):
        (tp, p), (t, o) = own[i - 1], own[i]
        if not (_alive(p) and _alive(o)) or t - tp > 2.5: continue
        d = (p['shield'] + p['health']) - (o['shield'] + o['health'])
        if d < HIT_MIN: continue
        if any(s['t0'] - 1 <= t <= s['t1'] + 1 for s in ring_segs) and o['shield'] == p['shield']: continue
        out.append((t, d))
    return out


def _squad_hue(g):
    return [(q['slot'], q['hue'], bool(q.get('self'))) for q in ((g.get('select') or {}).get('squad') or []) if q.get('hue') is not None]


def _who(side, squad):
    h = (side or {}).get('hue')
    if h is None or not squad: return None
    d = [min(abs(h - q[1]), 360 - abs(h - q[1])) for q in squad]
    k = int(np.argmin(d))
    return squad[k] if d[k] <= 20 else None


def damage_values(own_stats, t0, t1):
    """The player's damage number inside [t0, t1): [[first t, last t, value]] of values read twice in a row, or None
    when such values go down (a misread somewhere: no damage figures for the game). Before the number's first read, the
    strip without it counts as 0 (the plate only shows once there is damage)."""
    R = [(t, o.get('damage')) for t, o in own_stats or [] if t0 <= t < t1 and isinstance(o, dict) and o]
    first = next((i for i, (t, v) in enumerate(R) if v is not None), None)
    if first is None: return []
    R = [(t, 0) for t, v in R[:first]] + [(t, v) for t, v in R[first:] if v is not None]
    out = []
    for i in range(1, len(R)):
        if R[i][1] != R[i - 1][1]: continue
        if out and out[-1][2] == R[i][1]: out[-1][1] = R[i][0]
        else: out.append([R[i - 1][0], R[i][0], R[i][1]])
    if any(b[2] < a[2] for a, b in zip(out, out[1:])): return None
    return out


def damage_dealt(values, a, b):
    """The damage number's rise from just before a fight (the last value read by a) to its end (the last value read
    by b + DEALT_AFTER), or None when the number wasn't read on both sides."""
    if not values: return None
    before = [v for t0, t1, v in values if t0 <= a]
    after = [v for t0, t1, v in values if t0 <= b + DEALT_AFTER]
    if not before or not after or after[-1] < before[-1]: return None
    d = after[-1] - before[-1]
    return d if d <= DEALT_MAX else None


def fights(g, lines, own, loud, step, ring_segs, t_end, own_stats=None):
    """One game's fights: [{t0, t1, opponents, ours, theirs, result, thirdParty, ...}]. own_stats: the own stats
    strip reads [(t, {damage, ...})] (pcs_hud.OwnStats) for damage dealt, or None."""
    t0g = g['tStart']; squad = _squad_hue(g)
    L = squad_lines(lines, t0g, t_end, squad, dead=dead_spans(g))
    H = hits(own, ring_segs)
    ev = []                                           # (t, kind, line or amount)
    for l in L:
        ours = (l.get('killer') or {}).get('side') == 'squad'
        ev.append((l['t'], ('our' if ours else 'their') + l['event'].capitalize(), l))
    for k in g.get('ownKnocks') or []:
        if any(r['t0'] <= k['t'] <= r['t1'] + RING_KNOCK for r in ring_segs):
            continue                                  # knocked by the ring itself ('[The Ring]' in the feed)
        ev.append((k['t'], 'selfKnocked', k))
        if k.get('end') and k['end'] > k['t']: ev.append((k['end'], 'selfDown', k))   # still down: the fight goes on
    for t, d in H: ev.append((t, 'hit', d))
    ev.sort(key=lambda e: e[0])
    W = loud_windows(loud, step, t0g, t_end)
    wt = np.array([t for t, _ in W]); wv = np.array([v for _, v in W], bool)
    def loud_between(a, b):
        if not len(wt): return 0.0
        m = (wt >= a) & (wt < b)
        return float(wv[m].mean()) if m.any() else 0.0
    groups = []
    for e in ev:
        if groups:
            last = groups[-1][-1][0]
            if e[0] - last <= FIGHT_GAP:
                groups[-1].append(e); continue
        groups.append([e])
    deaths = list(g.get('ownDeaths') or []); back = [r['t'] for r in g.get('respawns') or [] if r.get('who') == 'self']
    sq = [v for _, v in g.get('squadsLeft') or []]
    champion = bool(sq) and min(sq) == 1
    dvals = damage_values(own_stats, t0g, t_end if t_end is not None else float('inf'))
    out = []
    for grp in groups:
        feed = [e for e in grp if e[1] not in ('hit', 'selfKnocked', 'selfDown')]
        dmg = sum(e[2] for e in grp if e[1] == 'hit')
        knocked = [e for e in grp if e[1] == 'selfKnocked']
        if not feed and not knocked and dmg < POKE: continue
        a, b = grp[0][0], grp[-1][0]
        ours = {'knocks': 0, 'kills': 0}; theirs = {'knocks': 0, 'kills': 0}
        opp = []                                       # [{'sig', 't'}] enemy names, in order of first sight
        late_new = None; self_in_feed = 0; self_killed = False
        first_enemy = None
        for t, kind, l in sorted(feed, key=lambda e: e[0]):
            ourside = kind.startswith('our')
            (ours if ourside else theirs)['knocks' if kind.endswith('Knock') else 'kills'] += 1
            sig = (l.get('victimSig') if ourside else l.get('killerSig'))
            if not ourside:
                q = _who(l.get('victim'), squad)
                if q and q[2]:
                    if kind.endswith('Knock'): self_in_feed += 1
                    else: self_killed = True
            if first_enemy is None: first_enemy = t
            if sig and _ones(sig) >= OPP_MIN_ONES and not any(_bits(sig, o['sig']) <= OPP_SAME_BITS for o in opp):
                opp.append({'sig': sig, 't': round(t, 2), 'by': 'us' if ourside else 'them'})
                if len(opp) > 1 and t - first_enemy >= NEW_ENEMY_AFTER and late_new is None:
                    late_new = (t, len(opp), ourside)
        theirs['knocks'] += max(0, len(knocked) - self_in_feed)        # own knocks the feed did not show
        # the player's own deaths in the fight (spectating after it, or a knock he never got up from), unless a kill
        # line on him already counted it
        died = [d for d in deaths if a - 2 <= d <= b + DEATH_AFTER] +             [k['end'] for t, kind, k in knocked if k.get('outcome') == 'died'
             and not any(abs(d - k['end']) <= DEATH_AFTER for d in deaths)]
        # the game ended for us while the fight was on (no HUD after it, not the last squad): the squad was eliminated
        # in it ('SQUAD ELIMINATED' straight away, no own death read: 7 Nov 7091, killed outright as the last alive)
        ended = t_end is not None and g.get('tEndGame') is not None and g['tEndGame'] <= b + ENDED_IN_FIGHT \
            and not champion and not any(r > b for r in back)
        if ended and not died: died = [g['tEndGame']]
        if died and not self_killed:
            theirs['kills'] += 1
        wiped = ended or (any(a - 2 <= d <= b + 25 for d in deaths) and not any(r > b for r in back)
                          and t_end is not None and (g.get('tEndGame') or t_end) <= b + 40 and not champion)
        dealt = damage_dealt(dvals, a, b)
        result = verdict(ours, theirs, wiped, dealt, int(dmg))
        third = None
        if late_new and (late_new[1] > SQUAD or not late_new[2]):
            third = {'t': late_new[0], 'why': 'new enemy'}
        if third is None and len(opp) > SQUAD:
            third = {'t': opp[SQUAD]['t'], 'why': 'new enemy'}
        if third is None and opp and ours['kills'] >= len(opp) and not _up_after_kills(feed):
            # every enemy we met is down for good: shooting or hits going on after that = someone else
            t_done = max(t for t, kind, l in feed if kind == 'ourKill')
            after_hits = sum(e[2] for e in grp if e[1] == 'hit' and e[0] > t_done + 2)
            if after_hits >= 25: third = {'t': round(t_done, 2), 'why': 'hit after opponents died'}
            elif loud_between(t_done + 3, t_done + 15) >= LOUD_AFTER: third = {'t': round(t_done, 2), 'why': 'gunfire after opponents died'}
        f = {'t0': round(a, 2), 't1': round(b, 2), 'opponents': len(opp), 'opponentNames': [o['sig'] for o in opp],
             'ours': ours, 'theirs': theirs, 'result': result, 'thirdParty': third is not None,
             'damageTaken': int(dmg), 'damageDealt': dealt, 'decidedBy': decided_by(ours, theirs, wiped, dealt, int(dmg)),
             'feedLines': [round(e[0], 2) for e in feed]}
        if third: f['thirdPartyAt'] = third['t']; f['thirdPartyWhy'] = third['why']
        if wiped: f['wiped'] = True
        out.append(f)
    return out


def _up_after_kills(feed):
    """An enemy line on one of us newer than our last kill line: that enemy was still up after it, so 'every opponent
    we met died' does not hold, whatever the name count says (enemy name shapes are read over moving scenery: two names
    can be closer than one name read twice). Lines come in at the top: in one capture, the line above is the newer one.
    7 Nov 6304: BeeAreOhDeehy knocks the player (y 288) just after our kill on Yani Neko (y 330); his name shape is 50
    points from Yani Neko's, so the fight counted one opponent, 'all dead', and his shooting read as a third party."""
    kills = [(t, l) for t, kind, l in feed if kind == 'ourKill']
    if not kills: return False
    t_done = max(t for t, _ in kills)
    y_done = min((l.get('y') or 0) for t, l in kills if t == t_done)
    return any(kind.startswith('their') and (t > t_done or (t == t_done and (l.get('y') or 0) < y_done))
               for t, kind, l in feed)


def decided_by(ours, theirs, wiped=False, dealt=None, taken=0):
    """What decides a fight's result: 'wiped', 'kills', 'knocks', 'damage', or '' (even)."""
    if wiped: return 'wiped'
    if ours['kills'] != theirs['kills']: return 'kills'
    if abs(ours['knocks'] - theirs['knocks']) >= KNOCK_LEAD: return 'knocks'
    if dealt is not None and abs(dealt - (taken or 0)) >= DAMAGE_LEAD: return 'damage'
    return ''


def verdict(ours, theirs, wiped=False, dealt=None, taken=0):
    """won / lost / even (coach's decision 32, 20 Nov 2026): kills decide (the player's own death counts as a kill on
    us); with as many kills each side, any knock lead (KNOCK_LEAD); with as many knocks, damage dealt against damage
    taken when they are DAMAGE_LEAD or more apart; a wiped squad always lost. None of that = even."""
    how = decided_by(ours, theirs, wiped, dealt, taken)
    if how == 'wiped': return 'lost'
    if how == 'kills': return 'won' if ours['kills'] > theirs['kills'] else 'lost'
    if how == 'knocks': return 'won' if ours['knocks'] > theirs['knocks'] else 'lost'
    if how == 'damage': return 'won' if dealt > (taken or 0) else 'lost'
    return 'even'


def attach(games, feed_lines, bar_reads, storm_reads, loud=None, loud_step=0.05, own_stats=None):
    """Adds games[].fights and games[].ringDamage. bar_reads: [(t, own, mates, spec)] (pcs_bars), storm_reads:
    [(t, (L, R, T, B))], loud: pcs_audio loudness (dBFS per loud_step s) or None, own_stats: the own stats strip
    reads [(t, {damage, ...})] for each fight's damage dealt, or None (damage then never decides)."""
    own_all = [(t, None if spec else o) for t, o, m, spec in bar_reads or []]
    for i, g in enumerate(games):
        t0 = g['tStart']; t1 = games[i + 1]['tStart'] if i + 1 < len(games) else float('inf')
        own = [(t, o) for t, o in own_all if t0 <= t < t1]
        storm = [(t, s) for t, s in storm_reads or [] if t0 <= t < t1]
        try: g['ringDamage'] = ring_damage(g, own, storm)
        except Exception as e: g['ringDamage'] = {'error': f'{type(e).__name__}: {e}'[:200]}
        segs = (g.get('ringDamage') or {}).get('segments') or []
        try:
            g['fights'] = fights(g, feed_lines or [], own, loud, loud_step, segs, t1, own_stats) \
                if any('event' in l for l in feed_lines or []) or own else []
        except Exception as e:
            g['fights'] = []; g['fightsError'] = f'{type(e).__name__}: {e}'[:200]
    return games
