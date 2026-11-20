"""Ring console scans in the review player: did the squad look at the map, and did the one who scanned say where the
ring goes? (coach's decisions 21 Nov 2026, question 2)

The scan gives each feed reveal line ("<name> revealed the next ring on map", `games[].ringIntel`): its time `t`, who
used the console (`by`: 'self' = the recording player, 'mate', or none when the line could not tell) and whether the
recording player opened the full map within 20 s (`mapCheck`, scanner pcs_ring.check_map_opens). This module adds the
callout: a ring / zone word said by the player who scanned, from 10 s before the line (the console takes a few
seconds to use) to 30 s after it, in the comms transcripts we may read:
- the player's own transcript (`<rec>.comms.json`, made only with their own yes: `transcript_consent`);
- teammates' lines from the team hub's replay of that game (`/api/replay/<gameKey>` -> `comms`): the hub gives a
  teammate's lines only when that teammate said yes to transcripts and shares their mic track (hub.comms.may_see).
"Not called out" is said only when the scanner's transcript exists for that game; otherwise the answer is unknown
(None) and nothing is marked. The feed line does not name the teammate (shape only), so for a teammate's scan every
teammate's transcript must be there to say no one called it.
"""
from __future__ import annotations

import re

BEFORE_S = 10.0
AFTER_S = 30.0
# ring / zone words, English and French (Whisper writes what it hears; French players say "la zone", "le ring",
# "le cercle")
RING_WORDS = re.compile(r"\b(rings?|zones?|circles?|end ?zones?|cercles?|anneaux?|endzone)\b", re.I)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:24] or "unknown"


def _num(v):
    try:
        v = float(v)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def jump_of(g: dict) -> float | None:
    """A game's jump from the dropship in recording seconds (the hub's game time 0), as squadpaths.paths_of_scan."""
    L = g.get("landing") or {}
    return _num(L.get("exitAtExact")) if L.get("exitAtExact") is not None else _num(L.get("exitAt"))


def _said(lines, t):
    """The first line said from BEFORE_S before t to AFTER_S after it with a ring / zone word, or None."""
    for x in sorted(lines, key=lambda x: x["start"]):
        if x["start"] <= t + AFTER_S and x.get("end", x["start"]) >= t - BEFORE_S and RING_WORDS.search(x["text"] or ""):
            return x
    return None


def check(game: dict, own: list | None, mates: list | None, t0: float, t1: float) -> list[dict]:
    """One game: [{t, by, mapCheck, mapOpenAt, called: True | False | None, at?, who?, text?}] per feed reveal line.
    own: the recording player's transcript lines [{start, end, text}] (recording seconds; None = no transcript of this
    recording). mates: teammates' lines [{start, end, text, player}] (recording seconds; None = hub not asked / no
    answer). t0..t1: the game's span (lines outside are not this game's)."""
    out = []
    own_in = None if own is None else [x for x in own if t0 <= x["start"] < t1]
    mate_in = None if mates is None else [x for x in mates if t0 <= x["start"] < t1]
    squad = len(((game.get("select") or {}).get("squad")) or []) or 3
    with_text = {slug(x.get("player") or "") for x in mate_in or []}
    for x in game.get("ringIntel") or []:
        t = _num(x.get("t"))
        if t is None:
            continue
        by = x.get("by")
        if x.get("spectatedYou"):       # "You" on the feed of the teammate the player was spectating
            by = "mate"
        e = {"t": t, "by": by, "mapCheck": x.get("mapCheck"), "mapOpenAt": x.get("mapOpenAt"), "called": None}
        if by == "self":
            src = [("self", own_in)]
        elif by == "mate":
            src = [("mate", mate_in)]
        else:
            src = [("self", own_in), ("mate", mate_in)]
        hit = None
        for who, L in src:
            s = _said(L or [], t)
            if s:
                hit = (who, s)
                break
        if hit:
            e.update(called=True, at=hit[1]["start"], who=hit[0], text=hit[1]["text"][:200])
            if hit[0] == "mate" and hit[1].get("player"):
                e["player"] = hit[1]["player"]
        else:
            have_self = own_in is not None
            have_mates = mate_in is not None and len(with_text) >= squad - 1
            if (by == "self" and have_self) or (by == "mate" and have_mates) or (by is None and have_self and have_mates):
                e["called"] = False
        out.append(e)
    return out


def check_recording(games: list, own: list | None, mates_by_game: dict, own_by_game: dict | None = None) -> list[dict]:
    """Every game of a scan: [{g: index, calls: [...]}] for games with feed reveal lines. mates_by_game: {index: lines
    in recording seconds, or None}. own_by_game: {index: the recording player's lines from the hub} when there is no
    transcript on this PC (the coach reviewing a player's recording)."""
    out = []
    for i, g in enumerate(games):
        if not g.get("ringIntel"):
            continue
        t0 = (_num(g.get("tStart")) or 0.0) - 60
        t1 = (_num(games[i + 1].get("tStart")) if i + 1 < len(games) else None) or float("inf")
        o = own if own is not None else (own_by_game or {}).get(i)
        out.append({"g": i, "calls": check(g, o, mates_by_game.get(i), t0, t1)})
    return out
