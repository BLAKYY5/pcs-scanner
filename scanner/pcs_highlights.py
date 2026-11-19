"""Highlights ("key plays" for the team's media person): moments worth a clip, found from what the scan already reads.

The coach asked (19 Nov 2026) for spectacular plays to be found even when nobody pressed the mark key: a player taking
down several enemies at once or in quick succession, a squad wipe, a clutch. Agreed rules (tunable in Settings):
  - chain: a run of the recording player's own knocks or kills, each within CHAIN_S (8 s) of the one before;
    MULTI_MIN (3) or more enemies taken down = "multi" (Multi-kill);
  - quick double: 2 enemies within DOUBLE_S (5 s) = "double", the lower tier;
  - squad wipe: the player's own kills on a whole squad's worth of enemies (3) in one chain = "wipe";
  - clutch: the player takes CLUTCH_MIN (2) enemies down while last alive in our squad, in a fight that ends won = "clutch".
Score: 10 per enemy taken down, up to 2 per second each gap is shorter than CHAIN_S, +15 for a wipe, +20 for a clutch.
The list is sorted best first.

What a takedown rests on (never a single read): each knock or kill counted needs two sources that agree.
  - "feed": a kill-feed line (weapon + arrow = knock, no arrow = kill) whose killer is in our squad's colour, and that
    colour is the player's own slot on the legend select screen (`select.squad[].self`);
  - "squad": such a line when the player's own colour is not known (no legend select read): ours, but maybe a
    teammate's: it counts only with the counter;
  - "counter": the player's own kills number on the stats strip (`pcs_hud.OwnStats`) going up, the new value read
    twice in a row (a single misread never moves it). Only kills: in Ranked the strip's third number rose when
    teammates knocked enemies the player had hit (18 Nov, by eye), so it is not the player's knocks; pubs and
    Wildcard show no third number;
  - "damage": the player's damage number went up in the 15 s before;
  - "fight": inside one of the game's fights (`pcs_fights`), or within a few seconds of its evidence.
A takedown counts with two of them (feed + fight alone included, as agreed; squad only with the counter). A kill line
the kills number was read around without moving is dropped (a teammate's line in a colour like ours). The same line
captured again (same event and weapon within 10 s) counts once. A kill of an enemy the player knocked earlier in the
same chain (within 5 s, or the most alike name shape) is the same enemy: it keeps the chain going but is not counted
twice. The feed misses some own lines (a line with rank badges or a red plate); then the kills number alone, with
damage or a fight, places the kill.
Checked by eye (19 Nov 2026, split before tuning; truths in the private handoff folder): see the PR.
Last alive (clutch): own card alive and no teammate card alive (`pcs_bars` reads: a knocked or dead teammate's card has
no white health bar) on 2 reads in a row, in a game where teammates' cards were read alive before.

Per game: `takedowns` = [{t, kind: knock | kill, sources, enemy?, same?}] (the confirmed ones; `same` = a kill of an
enemy already counted), `lastAlive` = [[t0, t1]], and `highlights` = [{t0, t1, kind, count, gaps, score, sources,
knocks, kills, tags}] (times in recording seconds). `find` regroups takedowns with other rules (the review player does
this with the player's own Settings). No numpy: the app imports this module too.
"""
from __future__ import annotations

CHAIN_S = 8.0           # s between two takedowns of one chain
MULTI_MIN = 3           # enemies in a chain for a multi-kill
DOUBLE_S = 5.0          # s between two takedowns for a quick double
SQUAD = 3               # enemies in a squad (trios) for a wipe
LINE_BEFORE = 3.0       # a kill line may be captured this long before the last read of the old kills value ...
LINE_AFTER = 10.0       # ... or this long after the first read of the new one (the plate is not read while it flashes)
COUNTER_NEAR = 6.0      # the kills number read this close before and after a kill line, unchanged = not the player's
DUP_S = 10.0            # two lines of ours, same event and weapon, this close, no kills step for the second = one line
SPREAD_MAX = 10.0       # s: a kills rise read within this long is placed inside the gap (see takedowns)
SAME_S = 5.0            # a knock and a kill of ours this close = one enemy (a knock that kills at once)
FIGHT_BEFORE, FIGHT_AFTER = 5.0, 12.0   # a takedown this close before / after a fight's evidence is in it
DAMAGE_BEFORE = 15.0    # the damage number rising in the s before a counter rise backs it up
RISE_MAX = 4            # a counter rise larger than this between two reads is not trusted (misread)
RISE_GAP = 20.0         # s between the last read of the old value and the first of the new one, at most
LAST_ALIVE_READS = 2    # reads in a row with no teammate card alive
CLUTCH_MIN = 2          # enemies the player takes down while last alive (one is a 1 v 1, not a clutch)
CLUTCH_BEFORE = 20.0    # the clip of a clutch starts at most this long before its first takedown
W_ENEMY, W_FAST, W_WIPE, W_CLUTCH = 10, 2, 15, 20


def rules(chain_s=None, multi_min=None, double_s=None) -> dict:
    """The rules in use, from Settings (None = the default); out-of-range values are clamped."""
    c = CHAIN_S if chain_s is None else max(2.0, min(30.0, float(chain_s)))
    m = MULTI_MIN if multi_min is None else max(2, min(6, int(multi_min)))
    d = DOUBLE_S if double_s is None else max(1.0, min(c, float(double_s)))
    return {'chainS': c, 'multiMin': m, 'doubleS': d}


def parse_rules(s: str | None) -> dict:
    """'8,3,5' (chain s, enemies for a multi-kill, double s) -> rules(); empty or bad -> the defaults."""
    try:
        a = [x.strip() for x in (s or '').split(',')]
        return rules(*(float(x) if x else None for x in (a + [''] * 3)[:3]))
    except (TypeError, ValueError):
        return rules()


# ---------- sources ----------
def _hue_d(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def _squad(g):
    return [(q['slot'], q['hue'], bool(q.get('self'))) for q in ((g.get('select') or {}).get('squad') or [])
            if q.get('hue') is not None]


def _who(side, squad):
    h = (side or {}).get('hue')
    if h is None or not squad: return None
    best = min(squad, key=lambda q: _hue_d(h, q[1]))
    return best if _hue_d(h, best[1]) <= 20 else None


def _bits(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count('1') if a and b else 72


def own_lines(lines, t0, t1, squad, last=None, dead=None):
    """Kill-feed knocks / kills by our squad inside [t0, t1), each line once (pcs_fights.squad_lines rules) ->
    [(t, kind, line, who)]: who = 'self' (the player's own colour), 'mate' or None (colour not known).
    last: a dict that gets each line's last capture by id (pcs_fights.squad_lines)."""
    import pcs_fights            # the same once-per-line rule as the fights (needs numpy: scanner side only)
    out = []
    for l in pcs_fights.squad_lines(lines, t0, t1, squad or None, last, dead):
        if (l.get('killer') or {}).get('side') != 'squad': continue
        q = _who(l.get('killer'), squad)
        who = None if not squad else ('self' if q and q[2] else 'mate')
        out.append((l['t'], l['event'], l, who))
    return out


def _above(l, prev_last):
    """l was first seen above where an earlier line of ours had already moved to: a new line (lines never move up;
    17 Nov 2045 / 2048: two knocks by the player with one weapon, the first already pushed down when the second came)."""
    import pcs_fights
    return bool(prev_last) and l['t'] > prev_last['t'] and (l.get('y') or 0) < (prev_last.get('y') or 0) - pcs_fights.Y_SAME


def counter_reads(reads, key, t0, t1):
    """[(t, value)] of one strip number inside [t0, t1). Before the number's first read, reads of the strip without it
    count as 0: the plate only shows once the number is above 0 (7 Nov, by eye), so a game's first knock or kill would
    otherwise never be seen as a rise."""
    R = [(t, o.get(key)) for t, o in reads if t0 <= t < t1 and isinstance(o, dict) and o]
    first = next((i for i, (t, v) in enumerate(R) if v is not None), None)
    if first is None: return []
    return [(t, 0) for t, v in R[:first]] + [(t, v) for t, v in R[first:] if v is not None]


def counter_rises(reads, key, t0, t1):
    """Own stats reads [(t, {key: n})] -> [(t_first_new, t_last_old, n_up)]: a value counts once read twice in a row,
    and only rises between two such values count (a value that later goes down = misread: the game's counter is
    refused). t_first_new = the first read of the new value."""
    R = counter_reads(reads, key, t0, t1)
    stable = []                                  # [(value, first t, last t)]
    for i in range(1, len(R)):
        if R[i][1] != R[i - 1][1]: continue
        v = R[i][1]
        if stable and stable[-1][0] == v: stable[-1][2] = R[i][0]
        else: stable.append([v, R[i - 1][0], R[i][0]])
    if any(b[0] < a[0] for a, b in zip(stable, stable[1:])): return []
    out = []
    for a, b in zip(stable, stable[1:]):
        up = b[0] - a[0]
        if 0 < up <= RISE_MAX and b[1] - a[2] <= RISE_GAP:
            # the first read of the new value (it may be the single read before the pair)
            first = next((t for t, v in R if a[2] < t <= b[1] and v == b[0]), b[1])
            out.append((first, a[2], up))
    return out


def counter_still(reads, key, t, t0, t1):
    """True when the strip number was read within COUNTER_NEAR s before and after t with the same value: the counter
    says the player did not knock / kill at t (a line in our colour then was a teammate's, or misread)."""
    R = counter_reads(reads, key, t0, t1)
    before = [v for tt, v in R if t - COUNTER_NEAR <= tt <= t - 1]
    after = [v for tt, v in R if t + 1 <= tt <= t + COUNTER_NEAR]
    return bool(before) and bool(after) and before[-1] == after[-1] == after[0]


def last_alive(bar_reads, t0, t1):
    """pcs_bars reads [(t, own, mates, spec)] -> [[t0, t1]] where the player's own card was alive and no teammate card
    was (2 reads in a row), once teammates' cards had been read alive in this game."""
    R = [(t, o, m) for t, o, m, spec in bar_reads or [] if t0 <= t < t1 and not spec]
    out, run, seen_mates = [], [], False
    for t, o, m in R:
        alive = bool(o) and o.get('state') == 'alive'
        if m: seen_mates = True
        if alive and not m and seen_mates: run.append(t)
        elif alive or (o and o.get('state') in ('knocked', 'spectating')) or m:
            if len(run) >= LAST_ALIVE_READS: out.append([round(run[0], 2), round(run[-1], 2)])
            run = []
    if len(run) >= LAST_ALIVE_READS: out.append([round(run[0], 2), round(run[-1], 2)])
    return out


def _in_fight(t, fights):
    """Inside one of the game's fights (pcs_fights), or just after its last evidence: the counter is often read a few
    seconds after the feed line or hit that ended the fight's evidence."""
    return any(f['t0'] - FIGHT_BEFORE <= t <= f['t1'] + FIGHT_AFTER for f in fights or [])


def _damage_rose(dmg, t):
    """The player's damage number went up from DAMAGE_BEFORE s before t to just after it: they were shooting."""
    before = [v for tt, v in dmg if t - DAMAGE_BEFORE <= tt <= t - DAMAGE_BEFORE / 3]
    after = [v for tt, v in dmg if t - 2 <= tt <= t + 3]
    return bool(before) and bool(after) and max(after) > min(before)


def takedowns(g, lines, own_reads, t0, t1):
    """The player's confirmed knocks and kills in one game: [{t, kind, sources, enemy?}] in time order."""
    squad = _squad(g)
    last = {}
    import pcs_fights
    feed = [x for x in own_lines(lines, t0, t1, squad, last, pcs_fights.dead_spans(g)) if x[3] != 'mate']
    fights = g.get('fights') or []
    # only the kills number: the strip's third number is not the player's own knocks in Ranked (it rose when teammates
    # knocked enemies the player had hit, 18 Nov by eye), and pubs / Wildcard have no third number at all
    rises = [[t, lo, n] for t, lo, n in counter_rises(own_reads, 'kills', t0, t1)]
    dmg = counter_reads(own_reads, 'damage', t0, t1)
    out = []
    # each kill line takes one step of a kills rise near it (a kill line read as a knock stays a knock: the knock and
    # the kills step close to it are then one enemy, see _mark_same)
    hits = {}
    for i, (t, kind, l, who) in enumerate(feed):
        if kind != 'kill': continue
        r = next((r for r in rises if r[2] > 0 and r[1] - LINE_BEFORE <= t <= r[0] + LINE_AFTER), None)
        if r is not None: r[2] -= 1; hits[i] = r[0]
    kept = []
    for i, (t, kind, l, who) in enumerate(feed):
        src = {'feed' if who == 'self' else 'squad'}
        at = t
        if i not in hits and any(t - pt <= DUP_S and pk == kind and pl.get('weapon') == l.get('weapon')
                                 and not _above(l, last.get(id(pl))) for pt, pk, pl in kept):
            continue                        # the same line captured again (its name shape read over a moving background)
        kept.append((t, kind, l))
        if i in hits:
            src.add('counter'); at = min(t, hits[i])
        elif kind == 'kill' and counter_still(own_reads, 'kills', t, t0, t1):
            continue                        # the kills number was read around it and did not move: not the player's
        if who == 'self':
            if _in_fight(t, fights): src.add('fight')
            if _damage_rose(dmg, t): src.add('damage')
        e = {'t': round(at, 2), 'kind': kind, 'sources': sorted(src)}
        if l.get('victimSig'): e['enemy'] = l['victimSig']
        out.append(e)
    for t, lo, n in rises:                  # kills no line explained: one takedown per step, with a fight or the damage
        src = ['counter'] + (['damage'] if _damage_rose(dmg, t) else []) + (['fight'] if _in_fight(t, fights) else [])
        for k in range(n):                  # number going up around it. The plate flashes red for ~2 s after a change
            # and is not read then, so the kill came between the last read of the old value and the first of the new
            # one: steps spread over that gap when it is short, else at the first new read
            at = lo + (t - lo) * (k + 1) / (n + 1) if t - lo <= SPREAD_MAX else t
            out.append({'t': round(at, 2), 'kind': 'kill', 'sources': sorted(src)})
    ok = lambda s: len(s) >= 2 and s != ['fight', 'squad']
    return sorted((e for e in out if ok(e['sources'])), key=lambda e: (e['t'], e['kind'] == 'kill'))


# ---------- grouping ----------
def _mark_same(chain):
    """Kills of an enemy already counted in this chain: a knock and a kill of ours within SAME_S of each other (a knock
    that kills at once, e.g. the last of a squad, moves both numbers), else a kill after a knock of ours in the chain
    not yet matched (the most alike name shape first). A kill with no knock of ours left before it is a new enemy
    (finishing someone a teammate knocked). Sets e['same'] = True on them."""
    for e in chain: e.pop('same', None)
    knocks = [e for e in chain if e['kind'] == 'knock']
    used = set()
    for e in chain:
        if e['kind'] != 'kill': continue
        free = [k for k in knocks if id(k) not in used]
        pick = [k for k in free if abs(k['t'] - e['t']) <= SAME_S]
        if not pick:            # an earlier knock of ours in the chain, the most alike name first (name shapes are read
            pick = sorted((k for k in free if k['t'] < e['t']),        # over a moving background: never trusted alone)
                          key=lambda k: _bits(k.get('enemy'), e.get('enemy')))
        if pick:
            used.add(id(pick[0])); e['same'] = True


def find(tds, fights=None, alive=None, rule=None) -> list[dict]:
    """Takedowns of one game (+ its fights and last-alive spans) -> highlights, best first."""
    r = rule or rules()
    tds = sorted((dict(e) for e in tds or [] if isinstance(e, dict) and isinstance(e.get('t'), (int, float))),
                 key=lambda e: e['t'])
    chains = []
    for e in tds:
        if chains and e['t'] - chains[-1][-1]['t'] <= r['chainS']: chains[-1].append(e)
        else: chains.append([e])
    out = []
    for ch in chains:
        _mark_same(ch)
        new = [e for e in ch if not e.get('same')]               # one per enemy
        count = len(new)
        gaps = [round(b['t'] - a['t'], 1) for a, b in zip(ch, ch[1:])]          # between all takedowns of the chain
        egaps = [b['t'] - a['t'] for a, b in zip(new, new[1:])]                # between enemies
        tags = []
        if count >= r['multiMin']: tags.append('multi')
        elif count >= 2 and any(x <= r['doubleS'] for x in egaps): tags.append('double')
        if sum(1 for e in ch if e['kind'] == 'kill') >= SQUAD: tags.append('wipe')     # a squad's worth of own kills
        t0, t1 = ch[0]['t'], ch[-1]['t']
        clutch = None
        for a, b in alive or []:
            mine = [e for e in new if a - 1 <= e['t'] <= b + 3]
            won = any(f.get('result') == 'won' and f['t0'] - 2 <= mine[0]['t'] <= f['t1'] + 2 for f in fights or []) if mine else False
            if len(mine) >= CLUTCH_MIN and won: clutch = (a, b); break
        if clutch: tags.append('clutch')
        if not tags: continue
        kind = next(k for k in ('wipe', 'clutch', 'multi', 'double') if k in tags)
        if kind == 'clutch': t0 = max(min(t0, clutch[0]), t0 - CLUTCH_BEFORE)
        src = sorted({s for e in ch for s in e['sources']} | ({'cards'} if clutch else set()))
        score = W_ENEMY * count + sum(W_FAST * max(0.0, r['chainS'] - x) for x in egaps) + \
            (W_WIPE if 'wipe' in tags else 0) + (W_CLUTCH if clutch else 0)
        out.append({'t0': round(t0, 2), 't1': round(t1, 2), 'kind': kind, 'count': count, 'gaps': gaps,
                    'score': int(round(score)), 'sources': src,
                    'knocks': sum(1 for e in new if e['kind'] == 'knock'), 'kills': sum(1 for e in ch if e['kind'] == 'kill'),
                    'tags': tags})
    out.sort(key=lambda h: (-h['score'], h['t0']))
    return out


def attach(games, feed_lines, own_reads, bar_reads, rule=None):
    """Adds games[].takedowns, games[].lastAlive and games[].highlights (after pcs_fights.attach: uses fights)."""
    for i, g in enumerate(games):
        t0 = g['tStart']; t1 = games[i + 1]['tStart'] if i + 1 < len(games) else float('inf')
        try:
            g['takedowns'] = takedowns(g, feed_lines or [], own_reads or [], t0, t1)
            g['lastAlive'] = last_alive(bar_reads, t0, t1)
            g['highlights'] = find(g['takedowns'], g.get('fights'), g['lastAlive'], rule)
        except Exception as e:
            g['highlights'] = []; g['highlightsError'] = f'{type(e).__name__}: {e}'[:200]
    return games
