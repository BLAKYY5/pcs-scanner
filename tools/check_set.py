#!/usr/bin/env python3
"""Labelled check set: truths about recordings at given times (checked by eye on screen), and a scorer that runs the
readers on the region cache (tools/region_cache.py) at those times, so a reader's accuracy is a number.

  python tools/check_set.py score <checkset.json> [--cache DIR] [--fields squadsLeft,kills] [--split dev|held]

File format (JSON):
  {"format": "pcs-checkset/1",
   "truths": [
     {"recording": "<file name of the recording>", "t": 3597, "squadsLeft": 8, "kills": 3, "assists": 0,
      "knocks": 3, "damage": 1123, "source": "given", "split": "dev", "note": "..."},
     {"recording": "...", "t": 2213, "legends": ["catalyst", "vantage", "bangalore"], "self": 3, "ranked": true},
     {"recording": "...", "t": 2190, "mode": "ranked", "map": "SP"}
   ]}
Optional 'split' per truth (e.g. "dev" for the truths a reader may be tuned on, "held" for truths kept back to test
it on unseen games; decide the split before tuning). Any subset of fields per truth. Fields and the reader that answers each:
  squadsLeft            SquadsLeft.read
  kills assists knocks  OwnStats.read
  damage                OwnStats.read (key 'damage')
  legends               Legends.read: the legend in each slot, in slot order (None = slot not named)
  self                  Legends.read: the slot (1..3) of the recording player
  ranked                Legends.read: rank badges on the select screen (true / false)
  mode, map             Banner.read: the loading banner's mode key (ranked, wildcard, pubs) and map code (SP, ED...)
  ringReveal            Feed.read on that frame: a "<name> revealed the next ring on map" line is in the feed (true / false)
Two measures per truth: the read at the nearest keyframe, and 'any read within +-3 s' (more lenient: correct when
one of the reads in the window gives the truth, read when any gives a value).
The real truth files reference private recordings: keep them out of this repo (tools/checkset_example.json is a
synthetic example).
"""
import os, sys, json, argparse, collections
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FIELDS = {   # field -> (reader in region_cache.READERS, function(read value) -> the field's value or None)
    'squadsLeft': ('squads', lambda v: v),
    'kills':      ('own', lambda v: v.get('kills')),
    'assists':    ('own', lambda v: v.get('assists')),
    'knocks':     ('own', lambda v: v.get('knocks')),
    'damage':     ('own', lambda v: v.get('damage')),
    'legends':    ('legends', lambda v: [s['legend'] for s in sorted(v['squad'], key=lambda s: s['slot'])]),
    'self':       ('legends', lambda v: next((s['slot'] for s in v['squad'] if s['self']), None)),
    'ranked':     ('legends', lambda v: bool(v.get('ranked'))),
    'mode':       ('banner', lambda v: v.get('mode')),
    'map':        ('banner', lambda v: v.get('map')),
    'ringReveal': ('ringreveal', lambda v: bool(v)),
}
WINDOW = 3.0

def load(path):
    d = json.load(open(path, encoding='utf-8'))
    if d.get('format') != 'pcs-checkset/1': raise ValueError(f'{path}: not a pcs-checkset/1 file')
    return d['truths']

def field_value(field, v):
    if v is None or v == {}: return None
    try: return FIELDS[field][1](v)
    except Exception: return None

def score(truths, reads_at, fields=None, window=WINDOW):
    """truths: list of dicts. reads_at(recording, t0, t1, readers) -> [(t, {reader: value})] for each keyframe in
    [t0, t1]. -> {field: {'n', 'nearRead', 'nearOk', 'winRead', 'winOk', 'misses': [...]}}."""
    res = collections.OrderedDict()
    for tr in truths:
        fs = [f for f in FIELDS if f in tr and (not fields or f in fields)]
        if not fs: continue
        readers = sorted({FIELDS[f][0] for f in fs})
        frames = reads_at(tr['recording'], tr['t'] - window - 0.5, tr['t'] + window + 0.5, readers)
        near = min(frames, key=lambda fr: abs(fr[0] - tr['t'])) if frames else None
        win = [fr for fr in frames if abs(fr[0] - tr['t']) <= window]
        for f in fs:
            r = res.setdefault(f, {'n': 0, 'nearRead': 0, 'nearOk': 0, 'winRead': 0, 'winOk': 0, 'misses': []})
            r['n'] += 1; want = tr[f]
            nv = field_value(f, near[1].get(FIELDS[f][0])) if near else None
            wv = [field_value(f, fr[1].get(FIELDS[f][0])) for fr in win]; wv = [x for x in wv if x is not None]
            r['nearRead'] += nv is not None; r['nearOk'] += nv == want
            r['winRead'] += bool(wv); r['winOk'] += want in wv
            if nv != want:
                r['misses'].append({'recording': tr['recording'], 't': tr['t'], 'truth': want, 'nearest': nv,
                                    'nearestT': round(near[0], 2) if near else None, 'window': sorted(set(map(json.dumps, wv)))})
    return res

def table(res):
    lines = [f'{"field":11s} {"truths":>6s} | {"nearest keyframe":^19s} | {"any read within 3 s":^19s}',
             f'{"":11s} {"":6s} | {"read":>8s} {"correct":>9s} | {"read":>8s} {"correct":>9s}']
    for f, r in res.items():
        lines.append(f'{f:11s} {r["n"]:6d} | {r["nearRead"]:8d} {r["nearOk"]:9d} | {r["winRead"]:8d} {r["winOk"]:9d}')
    for f, r in res.items():
        for m in r['misses']:
            lines.append(f'  miss {f} {m["recording"]} t={m["t"]}: truth {json.dumps(m["truth"])}, nearest '
                         f'({m["nearestT"]}) {json.dumps(m["nearest"])}, within 3 s {", ".join(m["window"]) or "nothing"}')
    return '\n'.join(lines)

def cache_reads(root=None):
    """reads_at() backed by the region cache: runs the readers on the cached keyframes."""
    import region_cache as RC
    caches = {}
    def reads_at(rec, t0, t1, readers):
        if rec not in caches:
            d = RC.cache_dir(rec, root)
            caches[rec] = RC.Cache(d) if os.path.exists(os.path.join(d, 'index.json')) else None
            if caches[rec] is None: print(f'  no cache for {rec} in {d}: its truths count as unread', flush=True)
        c = caches[rec]
        if c is None: return []
        out = []
        for t, a in c.frames(t0, t1, RC.boxes_for(readers)):
            vals = {}
            for r in readers:
                try: vals[r] = RC.READERS[r][1](a)
                except Exception: vals[r] = None
            out.append((t, vals))
        return out
    return reads_at

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', choices=('score',))
    ap.add_argument('checkset')
    ap.add_argument('--cache', help='region cache root (default: $PCS_REGION_CACHE)')
    ap.add_argument('--fields', help='only these fields, comma-separated')
    ap.add_argument('--split', help="only the truths with this 'split' value (e.g. dev, held)")
    a = ap.parse_args()
    fields = [f.strip() for f in a.fields.split(',')] if a.fields else None
    truths = [t for t in load(a.checkset) if not a.split or t.get('split') == a.split]
    print(table(score(truths, cache_reads(a.cache), fields)))

if __name__ == '__main__':
    main()
