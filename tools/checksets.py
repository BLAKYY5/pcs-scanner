#!/usr/bin/env python3
"""Scorers for the event check sets: bans, fights, highlights, mode, own state, respawns, heals / abilities.

tools/check_set.py scores the frame-by-frame truths (squads left, own stats, legends, banner) on the region cache.
The truths here are events (a fight from t0 to t1, a knock, a heal, a respawn, the bans of a game), so they are scored
on what a full scan writes: `<recording>.scan.json` made by the scanner under test (scanner/pcs_scan.py <mp4> --out
DIR). Only the scan's results are read: the scorers check the `*.scan.json` contract, not a reader's insides.

  python tools/checksets.py recordings <check set folder>             recordings the sets need (one per line)
  python tools/checksets.py score <check set folder> --scans DIR [--split dev|held] [--json OUT] [--misses]

Files looked for in the check set folder (any missing one is skipped):
  checkset-bans.json        games [{recording, game, selectAt, bans: [legend, ...], split}], negatives ["<rec> (...)"]
  checkset-fights.json      fights [{rec, t0, t1, result, thirdParty, split}], notFights [{rec, t}],
                            ring [{rec, t0, t1, outside: true} | {rec, t: [..], outside: false}]
  checkset-highlights.json  {"<rec>": {split, highlights: [{t0, t1, kind}], borderline: [{t0, t1}]}}
  checkset-mode.json        truths {"<rec>.mp4": {split, modes: [mode | null per scanned game]}}
  checkset-own-state.json   knocks [{recording, t, end, outcome}], deathsNoKnock [{recording, t}], scope {rec: [t0, t1]},
                            bars [{recording, t, who: self | mate, slot, state, tier, shield, health}]
  checkset-respawn-ring.json respawns [{recording, t, tol, who: self | teammate}]
  checkset-usage.json       windows [{recording, from, to, what}], heals [{recording, t, item, done}],
                            abilities [{recording, t, kind}]
Each scorer gives counts (truths, found, missed, false, right...) per split; --misses lists every miss / false one.
The real check sets reference private recordings: they stay out of this repo (the tests use made-up ones).
"""
import os, sys, json, argparse, collections

PAD = 5.0            # a detected fight / highlight overlapping [t0 - PAD, t1 + PAD] finds the truth
KNOCK_TOL = 3.0      # own knock start
HEAL_TOL = 2.0       # heal / ability start (as pcs-handoff usage scoring)
BAR_TOL = 5          # shield points / health % (labels are read by eye, tol 5)
SPLITS = ('dev', 'held')


def rec_name(r):
    r = os.path.basename(str(r))
    return r[:-4] if r.lower().endswith('.mp4') else r


def load_json(path):
    with open(path, encoding='utf-8-sig') as f:
        return json.load(f)


class Scans:
    """<dir>/<recording>.scan.json, loaded once. games(rec) -> [] when the scan is missing (counted as unread)."""

    def __init__(self, folder):
        self.folder, self.cache, self.missing = folder, {}, set()

    def games(self, rec):
        rec = rec_name(rec)
        if rec not in self.cache:
            p = os.path.join(self.folder, rec + '.scan.json')
            if os.path.exists(p):
                self.cache[rec] = load_json(p).get('games') or []
            else:
                self.cache[rec] = None
                self.missing.add(rec)
        return self.cache[rec] or []

    def has(self, rec):
        self.games(rec)
        return self.cache.get(rec_name(rec)) is not None

    def events(self, rec, key):
        """Every game's list `key`, flattened, each item with its game index `_g`."""
        return [{**e, '_g': i} for i, g in enumerate(self.games(rec)) for e in (g.get(key) or [])]


def game_at(games, t, before=120.0):
    """The game running at t: the last one starting at most `before` s after t (tEnd is often its start; the next
    game's start ends it)."""
    best = None
    for g in games:
        if g.get('tStart', 0) - before <= t:
            best = g
    return best


def _counter():
    return collections.Counter()


class Result:
    """Counts per split ('all' too) + the list of misses."""

    def __init__(self):
        self.n = {s: _counter() for s in ('all',) + SPLITS}
        self.misses = []

    def add(self, split, key, k=1):
        self.n['all'][key] += k
        if split in self.n:
            self.n[split][key] += k

    def miss(self, split, what, **kw):
        self.misses.append({'split': split, 'what': what, **kw})

    def out(self, split=None):
        keep = (split,) if split else ('all',) + SPLITS
        return {s: dict(self.n[s]) for s in keep if self.n[s]}


def _overlap(a0, a1, b0, b1, pad=0.0):
    return a0 <= b1 + pad and a1 >= b0 - pad


def _pairs(truths, dets, tol, key=lambda x: x['t']):
    """Closest-first one-to-one matching within tol -> ({truth index: det index}, unmatched det indexes)."""
    cand = sorted((abs(key(d) - key(t)), i, j) for i, t in enumerate(truths) for j, d in enumerate(dets)
                  if abs(key(d) - key(t)) <= tol)
    mt, md = {}, set()
    for _, i, j in cand:
        if i not in mt and j not in md:
            mt[i] = j
            md.add(j)
    return mt, [j for j in range(len(dets)) if j not in md]


# ------------------------------------------------------------------------------------------------ bans

def score_bans(cs, scans, split=None):
    """Per labelled game: the scan's game holding `selectAt` (tStart - 60 .. tEnd); its bans (legend names) must be the
    truth's set. Legends are counted too: found / missed / false. Negative recordings: any ban read = false."""
    R = Result()
    for g in cs.get('games', []):
        sp = g.get('split')
        if split and sp != split:
            continue
        games = scans.games(g['recording'])
        at = g.get('selectAt')
        sg = next((x for x in games if at is not None and (x.get('select') or {}).get('tFirst', 1e12) - 5 <= at
                   <= (x.get('select') or {}).get('tLast', -1e12) + 5), None)
        if sg is None and at is not None:
            sg = next((x for x in games if x.get('tStart', 0) >= at - 30), None)
        if sg is None and 0 <= g.get('game', -1) < len(games):
            sg = games[g['game']]
        got = sorted({b.get('legend') for b in (sg or {}).get('bans') or [] if b.get('legend')})
        want = sorted(set(g['bans']))
        R.add(sp, 'games')
        R.add(sp, 'gamesRight', got == want)
        R.add(sp, 'gamesRead', bool(got))
        R.add(sp, 'legends', len(want))
        R.add(sp, 'legendsFound', len(set(got) & set(want)))
        R.add(sp, 'legendsFalse', len(set(got) - set(want)))
        if got != want:
            R.miss(sp, 'bans', recording=rec_name(g['recording']), t=at, truth=want, read=got)
    for neg in cs.get('negatives', []):
        rec = rec_name(str(neg).split(' (')[0])
        if split or not scans.has(rec):
            continue
        n = sum(len(x.get('bans') or []) for x in scans.games(rec))
        R.add('neg', 'negativeRecordings')
        R.add('neg', 'negativeFalse', n)
        if n:
            R.miss('neg', 'ban read where none', recording=rec, read=n)
    return R


# ------------------------------------------------------------------------------------------------ fights

def score_fights(cs, scans, split=None):
    """Truth fight found = a detected fight overlapping [t0 - PAD, t1 + PAD]; then result and third party compared
    on the best overlap. notFights: a detection within PAD = false. Ring stretches (outside: true) found when a ring
    damage segment overlaps; red scenery (outside: false) wrong when a segment covers one of its times."""
    R = Result()
    for f in cs.get('fights', []):
        sp = f.get('split')
        if split and sp != split:
            continue
        dets = [d for d in scans.events(f['rec'], 'fights') if _overlap(d['t0'], d['t1'], f['t0'], f['t1'], PAD)]
        R.add(sp, 'fights')
        if not dets:
            R.add(sp, 'missed')
            R.miss(sp, 'fight not found', recording=f['rec'], t0=f['t0'], t1=f['t1'], truth=f['result'])
            continue
        d = max(dets, key=lambda d: min(d['t1'], f['t1'] + PAD) - max(d['t0'], f['t0'] - PAD))
        R.add(sp, 'found')
        R.add(sp, 'resultRight', d.get('result') == f['result'])
        if d.get('result') != f['result']:
            R.miss(sp, 'fight result', recording=f['rec'], t0=f['t0'], t1=f['t1'], truth=f['result'],
                   read=d.get('result'), readT=[d['t0'], d['t1']])
        if f.get('thirdParty') in (True, False):
            R.add(sp, 'thirdPartyLabelled')
            R.add(sp, 'thirdPartyRight', bool(d.get('thirdParty')) == f['thirdParty'])
            if bool(d.get('thirdParty')) != f['thirdParty']:
                R.miss(sp, 'third party', recording=f['rec'], t0=f['t0'], truth=f['thirdParty'], read=d.get('thirdParty'))
    for f in cs.get('notFights', []):
        sp = f.get('split')
        if split and sp != split:
            continue
        hit = [d for d in scans.events(f['rec'], 'fights') if _overlap(d['t0'], d['t1'], f['t'], f['t'], PAD)]
        R.add(sp, 'notFights')
        R.add(sp, 'notFightsFalse', bool(hit))
        if hit:
            R.miss(sp, 'fight where none', recording=f['rec'], t=f['t'])
    for r in cs.get('ring', []):
        sp = r.get('split')
        if split and sp != split:
            continue
        segs = [s for g in scans.games(r['rec']) for s in ((g.get('ringDamage') or {}).get('segments') or [])]
        span = lambda s: (s.get('t0', s.get('t', 0)), s.get('t1', s.get('t', 0)))
        if r.get('outside'):
            hit = any(_overlap(*span(s), r['t0'], r['t1']) for s in segs)
            R.add(sp, 'ringStretches')
            R.add(sp, 'ringFound', hit)
            if not hit:
                R.miss(sp, 'ring stretch not found', recording=r['rec'], t0=r['t0'], t1=r['t1'])
        else:
            ts = r['t'] if isinstance(r.get('t'), list) else [r.get('t')]
            bad = [t for t in ts if t is not None and any(_overlap(*span(s), t, t) for s in segs)]
            R.add(sp, 'redScenery', len(ts))
            R.add(sp, 'redSceneryFalse', len(bad))
            if bad:
                R.miss(sp, 'ring damage on red scenery', recording=r['rec'], t=bad)
    return R


# ------------------------------------------------------------------------------------------------ highlights

def score_highlights(cs, scans, split=None):
    """Truth found = a detected highlight overlapping [t0 - PAD, t1 + PAD]; kind right when every part of the truth's
    kind ("multi+wipe") is in its tags. A detection near no truth = false, except near a borderline one (not counted)."""
    R = Result()
    for rec, tr in cs.items():
        if rec.startswith('_') or not isinstance(tr, dict):
            continue
        sp = tr.get('split')
        if split and sp != split:
            continue
        dets = scans.events(rec, 'highlights')
        for x in tr.get('highlights', []):
            hit = [h for h in dets if _overlap(h['t0'], h['t1'], x['t0'], x['t1'], PAD)]
            R.add(sp, 'highlights')
            R.add(sp, 'found', bool(hit))
            if hit:
                tags = set(hit[0].get('tags') or [hit[0].get('kind')])
                ok = all(k in tags for k in x['kind'].split('+'))
                R.add(sp, 'kindRight', ok)
                if not ok:
                    R.miss(sp, 'highlight kind', recording=rec, t0=x['t0'], truth=x['kind'], read=sorted(tags))
            else:
                R.miss(sp, 'highlight not found', recording=rec, t0=x['t0'], t1=x['t1'], truth=x['kind'])
        if not scans.has(rec):
            continue
        for h in dets:
            if any(_overlap(h['t0'], h['t1'], x['t0'], x['t1'], PAD) for x in tr.get('highlights', [])):
                continue
            if any(_overlap(h['t0'], h['t1'], x['t0'], x['t1'], 2) for x in tr.get('borderline', [])):
                R.add(sp, 'borderline')
                continue
            R.add(sp, 'false')
            R.miss(sp, 'highlight where none', recording=rec, t0=h['t0'], t1=h['t1'], read=h.get('kind'))
    return R


# ------------------------------------------------------------------------------------------------ mode

def score_mode(cs, scans, split=None):
    """Per labelled game (games[] order): the scan's `mode`. null truth = not a game: right when the scan has no
    game there or a game with no mode. A different number of games is listed as a miss."""
    R = Result()
    for rec, tr in (cs.get('truths') or {}).items():
        sp = tr.get('split')
        if split and sp != split:
            continue
        games = scans.games(rec)
        want = tr.get('modes') or []
        real = [g for g in games]
        if scans.has(rec) and len(real) != len(want):
            R.miss(sp, 'game count', recording=rec_name(rec), truth=len(want), read=len(real))
        for i, m in enumerate(want):
            got = real[i].get('mode') if i < len(real) else None
            if m is None:
                R.add(sp, 'notGames')
                R.add(sp, 'notGamesRight', got is None)
                if got is not None:
                    R.miss(sp, 'mode on a non-game', recording=rec_name(rec), game=i, read=got)
                continue
            R.add(sp, 'games')
            R.add(sp, 'read', got is not None)
            R.add(sp, 'right', got == m)
            if got != m:
                R.miss(sp, 'mode', recording=rec_name(rec), game=i, truth=m, read=got)
    return R


# ------------------------------------------------------------------------------------------------ own state

def _bar_at(rows, t):
    """shieldTimeline rows [t, tier, shield, health] / [t, 'knocked'...]: the last row at or before t."""
    best = None
    for r in rows or []:
        if r and r[0] <= t + 0.5:
            best = r
        elif r and r[0] > t + 0.5:
            break
    return best


def score_own_state(cs, scans, split=None):
    """Own knocks: matched to the scan's `ownKnocks` by start within KNOCK_TOL s (one to one); outcome compared.
    Inside `scope`, a detected knock with no truth is false (one near a death with no knock too).
    Bars: the scan's `shieldTimeline` at t (self, or mates[slot - 1]): state, tier, shield and health within BAR_TOL."""
    R = Result()
    scope = {rec_name(k): v for k, v in (cs.get('scope') or {}).items()}
    by_rec = collections.defaultdict(list)
    for k in cs.get('knocks', []):
        by_rec[rec_name(k['recording'])].append(k)
    for rec in sorted(set(by_rec) | set(scope)):
        lo, hi = scope.get(rec, (-1e12, 1e12))
        truths = [k for k in by_rec.get(rec, []) if not split or k.get('split') == split]
        dets = [d for d in scans.events(rec, 'ownKnocks') if lo <= d['t'] <= hi]
        mt, extra = _pairs(truths, dets, KNOCK_TOL)
        for i, k in enumerate(truths):
            sp = k.get('split')
            R.add(sp, 'knocks')
            if i in mt:
                d = dets[mt[i]]
                R.add(sp, 'found')
                R.add(sp, 'outcomeRight', d.get('outcome') == k.get('outcome'))
                if d.get('outcome') != k.get('outcome'):
                    R.miss(sp, 'knock outcome', recording=rec, t=k['t'], truth=k.get('outcome'), read=d.get('outcome'))
            else:
                R.miss(sp, 'knock not found', recording=rec, t=k['t'])
        rec_split = next((k.get('split') for k in by_rec.get(rec, [])), None)
        if split and rec_split != split:
            continue
        if not scans.has(rec):
            continue
        for j in extra:
            R.add(rec_split, 'false')
            R.miss(rec_split, 'knock where none', recording=rec, t=dets[j]['t'])
    for b in cs.get('bars', []):
        sp = b.get('split')
        if split and sp != split:
            continue
        rec = rec_name(b['recording'])
        tl = (game_at(scans.games(rec), b['t']) or {}).get('shieldTimeline')
        if b['who'] == 'self':
            rows = (tl or {}).get('self')
        else:
            mates = (tl or {}).get('mates') or []
            rows = mates[b['slot'] - 1] if 0 < b.get('slot', 0) <= len(mates) else None
        row = _bar_at(rows, b['t'])
        R.add(sp, 'bars')
        if row is None:
            R.miss(sp, 'bar not read', recording=rec, t=b['t'], who=b['who'])
            continue
        R.add(sp, 'barsRead')
        if b.get('state') == 'knocked':
            ok = len(row) > 1 and row[1] == 'knocked'
        else:
            ok = (len(row) >= 4 and row[1] == b.get('tier') and abs((row[2] or 0) - (b.get('shield') or 0)) <= BAR_TOL
                  and abs((row[3] or 0) - (b.get('health') or 0)) <= BAR_TOL)
        R.add(sp, 'barsRight', ok)
        if not ok:
            R.miss(sp, 'bar', recording=rec, t=b['t'], who=b['who'], slot=b.get('slot'),
                   truth=[b.get('state'), b.get('tier'), b.get('shield'), b.get('health')], read=row)
    return R


# ------------------------------------------------------------------------------------------------ respawns

def score_respawns(cs, scans, split=None):
    """A respawn truth is found by a scan respawn of the same who (self / teammate) within its tol (one to one).
    False ones are not counted: the labels cover the respawns seen on the sheets, not every second of a recording."""
    R = Result()
    by = collections.defaultdict(list)
    for x in cs.get('respawns', []):
        if not split or x.get('split') == split:
            by[(rec_name(x['recording']), x['who'])].append(x)
    for (rec, who), truths in sorted(by.items()):
        dets = [d for d in scans.events(rec, 'respawns') if d.get('who') == who]
        cand = sorted((abs(d['t'] - x['t']), i, j) for i, x in enumerate(truths) for j, d in enumerate(dets)
                      if abs(d['t'] - x['t']) <= x.get('tol', 6))
        mt, md = {}, set()
        for _, i, j in cand:
            if i not in mt and j not in md:
                mt[i] = j
                md.add(j)
        for i, x in enumerate(truths):
            sp = x.get('split')
            R.add(sp, 'respawns')
            R.add(sp, 'found', i in mt)
            R.add(sp, 'self' if who == 'self' else 'teammate')
            R.add(sp, 'selfFound' if who == 'self' else 'teammateFound', i in mt)
            if i not in mt:
                R.miss(sp, 'respawn not found', recording=rec, t=x['t'], who=who)
    return R


# ------------------------------------------------------------------------------------------------ usage

def score_usage(cs, scans, split=None):
    """Inside each labelled window: heals matched by start within HEAL_TOL s (one to one), item and done compared;
    abilities of the kinds the window labels ("heals+tactical+ult") the same way. A detection without a truth in a
    window is false (every heal / use there was labelled)."""
    R = Result()
    for w in cs.get('windows', []):
        sp = w.get('split')
        if split and sp != split:
            continue
        rec = rec_name(w['recording'])
        inside = lambda t: w['from'] <= t < w['to']
        what = w.get('what', 'heals')
        th = [h for h in cs.get('heals', []) if rec_name(h['recording']) == rec and inside(h['t'])]
        dh = [h for h in scans.events(rec, 'heals') if inside(h['t'])]
        mt, extra = _pairs(th, dh, HEAL_TOL)
        for i, h in enumerate(th):
            R.add(sp, 'heals')
            if i not in mt:
                R.miss(sp, 'heal not found', recording=rec, t=h['t'], truth=h.get('item'))
                continue
            d = dh[mt[i]]
            R.add(sp, 'healsFound')
            R.add(sp, 'itemRight', d.get('item') == h.get('item'))
            if d.get('item') != h.get('item'):
                R.miss(sp, 'heal item', recording=rec, t=h['t'], truth=h.get('item'), read=d.get('item'))
            if h.get('done') in (True, False):
                R.add(sp, 'doneLabelled')
                R.add(sp, 'doneRight', bool(d.get('done')) == h['done'])
        if scans.has(rec):
            for j in extra:
                R.add(sp, 'healsFalse')
                R.miss(sp, 'heal where none', recording=rec, t=dh[j]['t'], read=dh[j].get('item'))
        kinds = [k for k in ('tactical', 'ult') if k in what]
        for kind in kinds:
            ta = [a for a in cs.get('abilities', []) if rec_name(a['recording']) == rec and inside(a['t']) and a['kind'] == kind]
            da = [a for a in scans.events(rec, 'abilities') if inside(a['t']) and a.get('kind') == kind]
            mt, extra = _pairs(ta, da, HEAL_TOL)
            R.add(sp, kind + 's', len(ta))
            R.add(sp, kind + 'sFound', len(mt))
            for i, a in enumerate(ta):
                if i not in mt:
                    R.miss(sp, kind + ' not found', recording=rec, t=a['t'])
            if scans.has(rec):
                R.add(sp, kind + 'sFalse', len(extra))
                for j in extra:
                    R.miss(sp, kind + ' where none', recording=rec, t=da[j]['t'])
    return R


SETS = {   # file -> (name, scorer, recordings(check set) -> names)
    'checkset-bans.json': ('bans', score_bans, lambda c: [g['recording'] for g in c.get('games', [])]
                           + [str(n).split(' (')[0] for n in c.get('negatives', [])]),
    'checkset-fights.json': ('fights', score_fights, lambda c: [x['rec'] for k in ('fights', 'notFights', 'ring')
                                                                for x in c.get(k, [])]),
    'checkset-highlights.json': ('highlights', score_highlights, lambda c: [k for k, v in c.items()
                                                                            if not k.startswith('_') and isinstance(v, dict)]),
    'checkset-mode.json': ('mode', score_mode, lambda c: list((c.get('truths') or {}).keys())),
    'checkset-own-state.json': ('ownState', score_own_state, lambda c: [x['recording'] for k in ('knocks', 'bars')
                                                                        for x in c.get(k, [])] + list(c.get('scope') or {})),
    'checkset-respawn-ring.json': ('respawns', score_respawns, lambda c: [x['recording'] for x in c.get('respawns', [])]),
    'checkset-usage.json': ('usage', score_usage, lambda c: [x['recording'] for x in c.get('windows', [])]),
}


def recordings(folder):
    out = set()
    for f, (_, _, recs) in SETS.items():
        p = os.path.join(folder, f)
        if os.path.exists(p):
            out |= {rec_name(r) for r in recs(load_json(p))}
    return sorted(out)


def score_all(folder, scans_dir, split=None):
    """-> ({set name: {split: counts}}, {set name: misses}, missing scans)."""
    scans = Scans(scans_dir)
    res, misses = {}, {}
    for f, (name, fn, _) in SETS.items():
        p = os.path.join(folder, f)
        if not os.path.exists(p):
            continue
        r = fn(load_json(p), scans, split)
        res[name] = r.out(split)
        misses[name] = r.misses
    return res, misses, sorted(scans.missing)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', choices=('score', 'recordings'))
    ap.add_argument('folder', help='folder holding the checkset-*.json files')
    ap.add_argument('--scans', help='folder of <recording>.scan.json made by the scanner under test')
    ap.add_argument('--split', choices=SPLITS)
    ap.add_argument('--json', help='write the counts (and misses) to this file')
    ap.add_argument('--misses', action='store_true', help='print every miss / false detection')
    a = ap.parse_args(argv)
    if a.cmd == 'recordings':
        print('\n'.join(recordings(a.folder)))
        return 0
    if not a.scans:
        ap.error('score needs --scans')
    res, misses, missing = score_all(a.folder, a.scans, a.split)
    for name, per in res.items():
        for sp, c in per.items():
            print(f'{name:11s} {sp:4s} ' + ' '.join(f'{k} {v}' for k, v in c.items()))
        if a.misses:
            for m in misses[name]:
                print('   ', json.dumps(m, ensure_ascii=False))
    if missing:
        print('no scan for: ' + ', '.join(missing) + ' (their truths count as missed)')
    if a.json:
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump({'format': 'pcs-checksets-score/1', 'split': a.split, 'sets': res, 'misses': misses,
                       'missingScans': missing}, f, indent=1, ensure_ascii=False)
    return 0


if __name__ == '__main__':
    sys.exit(main())
