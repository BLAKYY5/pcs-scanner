#!/usr/bin/env python3
"""Score the sound / screen cross-checks of scanner/pcs_audio.py on one recording, from its scan (no video decode):

  python tools/audio_cross.py "<recording>" "<recording>.scan.json"

Reads the recording's game track (track 1, Apex only: never the voice or mic tracks), re-runs the sound cues and
`attach` on the scan's games, and prints per game and in total:
  - ring countdown ends read on screen while the player was alive, and how many have their alarm (normal bar, or the
    weaker screen-guided one), versus what the scan itself said (its own `soundAt`),
  - alarms heard that the screen explains as not a ring start (`ringSoundOther`) and those left unexplained,
  - damage totals that rest on a single read, kept or refused by the sound check.
'Alive' = up to the game's `tEndGame`, or the last read of the player's own numbers for scans older than 1.10.9.
"""
import argparse, copy, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scanner'))
import pcs_audio as A

def alive_end(g):
    if g.get('tEndGame') is not None: return g['tEndGame']
    o = g.get('own') or {}
    return max([v for k, v in o.items() if k.endswith('LastRead')] or [g.get('tStart', 0)])

def rings_alive(g):
    end = alive_end(g)
    return [r for r in g.get('ring', []) if r.get('kind') == 'wait' and r.get('endsAt') is not None and r['endsAt'] <= end]

def score(games_before, games_after):
    tot = {'rings': 0, 'heardBefore': 0, 'heardAfter': 0, 'weak': 0, 'unexplainedBefore': 0, 'unexplainedAfter': 0,
           'other': 0, 'singleDamage': 0, 'damageRefused': 0}
    rows = []
    for gb, ga in zip(games_before, games_after):
        rb, ra = rings_alive(gb), rings_alive(ga)
        hb = sum(r.get('soundAt') is not None for r in rb); ha = sum(r.get('soundAt') is not None for r in ra)
        weak = [r['endsAt'] for r in ra if r.get('soundWeak')]
        other = ga.get('ringSoundOther', [])
        ob = (gb.get('own') or {}); oa = (ga.get('own') or {})
        row = {'tStart': ga.get('tStart'), 'rings': len(ra), 'heardBefore': hb, 'heardAfter': ha, 'weak': weak,
               'unexplainedBefore': gb.get('ringSound', []), 'unexplainedAfter': ga.get('ringSound', []), 'other': other}
        if ob.get('damageSingleRead'):
            row['singleDamage'] = {'value': ob.get('damage'), 'at': ob.get('damageAt'), 'refused': oa.get('damageRefused'),
                                   'soundDb': oa.get('damageSoundDb')}
        rows.append(row)
        tot['rings'] += len(ra); tot['heardBefore'] += hb; tot['heardAfter'] += ha; tot['weak'] += len(weak)
        tot['unexplainedBefore'] += len(row['unexplainedBefore']); tot['unexplainedAfter'] += len(row['unexplainedAfter'])
        tot['other'] += len(other); tot['singleDamage'] += 'singleDamage' in row
        tot['damageRefused'] += bool(oa.get('damageRefused'))
    return rows, tot

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('recording'); ap.add_argument('scan')
    a = ap.parse_args()
    d = json.load(open(a.scan, encoding='utf-8'))
    before = d['games']; after = copy.deepcopy(before)
    for g in after:                       # what attach sets is computed again
        for r in g.get('ring', []):
            for k in ('soundAt', 'soundWeak', 'soundDb'): r.pop(k, None)
        for k in ('ringSound', 'ringSoundOther'): g.pop(k, None)
        o = g.get('own') or {}
        o.pop('damageSoundDb', None)
    found = A.analyse(A.load(a.recording))
    A.attach(after, found)
    rows, tot = score(before, after)
    for r in rows:
        print(json.dumps(r))
    print(json.dumps(tot))

if __name__ == '__main__':
    main()
