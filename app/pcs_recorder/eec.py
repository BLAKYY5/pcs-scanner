"""Scrim results from EEC, for the YouTube description: standing out of the lobby, points, kills, drops, per game.

Two sources, both of data a person fetched from EEC by hand (EEC's terms forbid bots and scrapers, so this
app never reads the EEC site itself):
- the lobby's export file, from the lobby page's Download button (lobbies_<num>_<code>_scores.json);
- the team hub, when the coach already imported that export (collection "scrims").

Points use ALGS scoring, like the Loot Book: placement points plus one per kill.

Drops. The export's `draft` holds one POI pick per team and map; each player row carries `map_pick_id` (the same id
on every game the team played with that pick). A team's picks sorted by pick number belong to its ids sorted (as the
hub does it, hub/eec.py). Without ids: one pick per game when the counts say so, else one per map in play order. A
hub document has drops only when the hub imported it with the fixed drops (`draft` in the document): the old Loot
Book split put most November drops on the wrong game, so those are not used.

Coach's rule (18 Nov 2026): EEC is right about 95 % of the time. Where EEC's drop and where our scan says we landed
disagree, EEC wins for display and our value is kept next to it (never hidden). EEC drops also confirm that a block is
that EEC scrim (`check_block`): a scanned game is compared with the EEC game on the same map that started within
15 minutes of it; the block is that scrim when more compared games agree than differ, and is not that scrim when more
differ (e.g. a tournament the same evening) or when none of its games was played at the lobby's game times. Nothing
to compare (no drops, no landing) = the old behaviour (times only).
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

ALGS_PTS = [12, 9, 7, 5, 4, 3, 3, 2, 2, 2, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
EEC_MAP = {"desertlands": "WE", "district": "ED", "tropic": "SP", "olympus": "OL", "divided_moon": "BM",
           "canyonlands": "KC"}
FILE_RE = re.compile(r"lobbies_(\d+)_([a-z0-9]+)", re.I)


def place_pts(p: int) -> int:
    return ALGS_PTS[p - 1] if 1 <= p <= len(ALGS_PTS) else 0


def eec_map(name: str) -> str:
    for k, v in EEC_MAP.items():
        if k in (name or ""):
            return v
    return name or ""


def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _key(team) -> str:
    return str(team or "").strip().lower()


def poi_name(n) -> str:
    """'SKYHOOK WEST (2)' -> 'Skyhook West' (as the hub and the Loot Book show it)."""
    n = re.sub(r"\s*\(\d+\)\s*$", "", str(n or "")).strip().lower()
    return " ".join(w if w in ("a", "of") else w[:1].upper() + w[1:] for w in n.split())


def export_drops(matches: list[dict], draft: list) -> tuple[dict, str]:
    """{(game index, team key): drop name} and how they were found: "pick id", "order" or "" (none)."""
    ids: dict[str, dict[int, list[int]]] = {}       # team -> {map_pick_id: [game index]}
    maps: list[str] = []
    for gi, g in enumerate(matches):
        if (g.get("map_name") or "") not in maps:
            maps.append(g.get("map_name") or "")
        for r in g.get("data") or []:
            pid = _int(r.get("map_pick_id")) if isinstance(r, dict) else 0
            if pid:
                games = ids.setdefault(_key(r.get("team_name")), {}).setdefault(pid, [])
                if gi not in games:
                    games.append(gi)
    picks: dict[str, list[dict]] = {}
    for d in draft or []:
        if isinstance(d, dict) and d.get("team") and d.get("poi"):
            picks.setdefault(_key(d["team"]), []).append(d)
    played = {_key(r.get("team_name")) for g in matches for r in g.get("data") or [] if isinstance(r, dict)}
    out: dict[tuple[int, str], str] = {}
    how = set()
    for team, ps in picks.items():
        if team not in played:
            continue                                       # drafted, never in the games
        ps = sorted(ps, key=lambda d: (_int(d.get("pick_num")), _int(d.get("seed"))))
        have = sorted(ids.get(team, {}).items())
        if have:
            if len(have) != len(ps):
                continue                                   # a team that missed a map: not guessed here
            pairs = [(games, p) for (_pid, games), p in zip(have, ps)]
            how.add("pick id")
        elif len(ps) == len(matches):
            pairs = [([gi], p) for gi, p in enumerate(ps)]
            how.add("order")
        else:
            pairs = [([gi for gi, g in enumerate(matches) if (g.get("map_name") or "") == mn], p)
                     for mn, p in zip(maps, ps)]
            how.add("order")
        for games, p in pairs:
            for gi in games:
                out[(gi, team)] = poi_name(p.get("poi"))
    return out, ("pick id" if how == {"pick id"} else ("order" if how else ""))


def from_export(data: dict, filename: str = "") -> dict:
    """EEC Download file -> {"url", "drops" (how they were found, "" = none), "games": [{"start", "map",
    "rows": [[team, player, place, kills], ...], "drops": {team key: drop}}]}, games in play order."""
    if not isinstance(data, dict) or not isinstance(data.get("matches"), list) or not data["matches"]:
        raise ValueError("This isn't an EEC lobby export (lobbies_..._scores.json from the lobby's Download button)")
    m = FILE_RE.search(filename or "")
    url = f"https://eternalesports.club/lobbies/{m.group(1)}/{m.group(2)}" if m else ""
    matches = [g for g in data["matches"] if isinstance(g, dict)]
    if all(_int(g.get("match_start")) for g in matches):
        matches.sort(key=lambda g: _int(g.get("match_start")))
    drops, how = export_drops(matches, data.get("draft") if isinstance(data.get("draft"), list) else [])
    games = []
    for gi, g in enumerate(matches):
        rows = [[r.get("team_name", ""), r.get("player_name", ""), int(r.get("team_placement") or 0),
                 int(r.get("kills") or 0)] for r in g.get("data") or [] if isinstance(r, dict)]
        games.append({"start": float(g.get("match_start") or 0), "map": eec_map(g.get("map_name", "")), "rows": rows,
                      "drops": {t: d for (i, t), d in drops.items() if i == gi}})
    return {"url": url, "games": games, "drops": f"export, {how}" if how else ""}


def from_hub_doc(doc: dict) -> dict:
    """A hub / Loot Book "scrims" document (rows: [team, player, legend, place, kills, ..., drop (13)]).
    Drops only from documents the hub imported with the fixed drops (`draft` present)."""
    fixed = bool(doc.get("draft"))
    games = []
    for g in doc.get("games", []):
        rows = [r for r in g.get("rows", []) if isinstance(r, list) and len(r) > 4]
        drops: dict[str, str] = {}
        if fixed:
            for t in g.get("teams") or []:
                if isinstance(t, dict) and t.get("drop"):
                    drops[_key(t.get("team"))] = t["drop"]
            for r in rows:
                if len(r) > 13 and r[13]:
                    drops.setdefault(_key(r[0]), r[13])
        games.append({"start": float(g.get("start") or 0), "map": g.get("map", ""),
                      "rows": [[r[0], r[1], int(r[3] or 0), int(r[4] or 0)] for r in rows], "drops": drops})
    return {"url": doc.get("url", ""), "games": games, "drops": "hub" if fixed else ""}


def results(lobby: dict, team: str) -> dict | None:
    """Our team's result in the lobby, or None if the team isn't in it."""
    want = (team or "").strip().lower()
    totals: dict[str, dict] = {}
    ours = []
    for i, g in enumerate(lobby["games"], 1):
        teams: dict[str, dict] = {}
        for name, _player, place, kills in g["rows"]:
            t = teams.setdefault(name, {"place": place, "kills": 0})
            t["kills"] += kills
        for name, t in teams.items():
            pts = place_pts(t["place"]) + t["kills"]
            a = totals.setdefault(name, {"pts": 0, "kills": 0, "best": 99})
            a["pts"] += pts
            a["kills"] += t["kills"]
            a["best"] = min(a["best"], t["place"] or 99)
            if name.strip().lower() == want:
                ours.append({"game": i, "start": g["start"], "map": g["map"], "place": t["place"], "kills": t["kills"],
                             "pts": pts, "teams": len(teams), "drop": (g.get("drops") or {}).get(_key(name), "")})
    me = next((n for n in totals if n.strip().lower() == want), None)
    if not me:
        return None
    order = sorted(totals, key=lambda n: (-totals[n]["pts"], -totals[n]["kills"], totals[n]["best"]))
    # standing on points only (EEC's own tie-break isn't known): equal points share a place, marked "tied"
    mine = totals[me]["pts"]
    standing = 1 + sum(1 for t in totals.values() if t["pts"] > mine)
    tied = sum(1 for t in totals.values() if t["pts"] == mine) > 1
    return {"url": lobby.get("url", ""), "team": me, "standing": standing, "tied": tied, "teams": len(order),
            "pts": totals[me]["pts"], "kills": totals[me]["kills"], "games": ours,
            "leader": {"team": order[0], "pts": totals[order[0]]["pts"]}, "drops": lobby.get("drops", "")}


def read_file(path: str) -> dict:
    return from_export(json.loads(Path(path).read_text(encoding="utf-8")), Path(path).name)


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def match_game(res: dict, abs_time: float, index: int) -> dict | None:
    """The EEC game that matches a scanned game: by time (within 15 min), else by order."""
    games = res.get("games", [])
    timed = [g for g in games if g.get("start")]
    if timed and abs_time:
        best = min(timed, key=lambda g: abs(g["start"] - abs_time))
        if abs(best["start"] - abs_time) < 15 * 60:
            return best
    return games[index] if index < len(games) else None


def from_hub(hub_url: str, team: str, start: float, end: float) -> dict | None:
    """Ask the team hub for an imported lobby that was played during this block (with the player's key)."""
    found = hub_candidates(hub_url, team, start, end)
    return found[0] if found else None


def hub_candidates(hub_url: str, team: str, start: float, end: float) -> list[dict]:
    """Our results in every lobby on the hub played during this block (the caller checks the drops)."""
    from . import hub
    out = []
    for d in hub.get_json(hub_url, "/api/db/scrims"):
        doc = d.get("data", d)
        lobby = from_hub_doc(doc)
        starts = [g["start"] for g in lobby["games"] if g["start"]]
        if starts and start - 1800 <= min(starts) <= end + 600:
            res = results(lobby, team)
            if res:
                out.append(res)
    return out


# ---------------------------------------------------------------- EEC drop vs our scan

TIME_FIT_S = 15 * 60          # a scanned game and an EEC game are the same game: same map, started this close
_POIS: dict | None = None
_ALIASES: dict | None = None
_NUM = re.compile(r"\s*\(\d+\)\s*$")          # EEC's pick number: "MIRAGE A TROIS (6)"


def norm(s) -> str:
    """'Mirage à Trois' -> 'mirageatrois'."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def simple(s) -> str:
    """norm without a leading 'The' and a plural s: 'The Mill' and 'Mills' -> 'mill'."""
    n = norm(re.sub(r"^\s*the\s+", "", str(s or ""), flags=re.I))
    return n[:-1] if n.endswith("s") and len(n) > 4 else n


def _load() -> None:
    global _POIS, _ALIASES
    _POIS, _ALIASES = {}, {}
    here = Path(__file__).resolve()
    for f in (Path(getattr(sys, "_MEIPASS", "") or here.parent) / "pcs_maps.json",
              here.parents[2] / "scanner" / "pcs_maps.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            _POIS = {m: {p["id"]: p.get("name") or p["id"] for p in v.get("pois", [])} for m, v in d.items()}
            _ALIASES = {m: {p["id"]: [a for a in p.get("aliases") or [] if isinstance(a, str)] for p in v.get("pois", [])}
                        for m, v in d.items()}
            return
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue


def pois() -> dict:
    """{map: {poi id: name}} from the scanner's pcs_maps.json (next to the exe, or scanner/ from source). The names are
    the game's own (its minimap labels, Season 31) where the game names the place."""
    if _POIS is None:
        _load()
    return _POIS


def aliases() -> dict:
    """{map: {poi id: [other names]}}: our earlier names, the game's full zone name, the names EEC and ALGS use
    ('Skyhook West' for Trials, 'Canal' for Canal Plaza). Old scans keep POI ids, so a rename never splits history."""
    if _ALIASES is None:
        _load()
    return _ALIASES or {}


def resolve(mp: str | None, text: str | None) -> str | None:
    """The POI id a name stands for on this map, in any spelling we know: the id, our name, one of its aliases, EEC's
    'MIRAGE A TROIS (6)'. Exact (accents, case, signs ignored), then without 'The' / plural s; a name two POIs share
    resolves to none."""
    t = _NUM.sub("", str(text or "")).strip()
    names = pois().get(mp or "", {})
    if not t or not names:
        return None
    if t in names:
        return t
    al = aliases().get(mp or "", {})
    for f in (norm, simple):
        want = f(t)
        if not want:
            return None
        hits = {i for i, n in names.items() if f(n) == want}
        if not hits:
            hits = {i for i, a in al.items() if want in {f(x) for x in a}}
        if len(hits) == 1:
            return hits.pop()
        if hits:
            return None
    return None


CALLOUTS_TTL = 1800          # s between two looks at the team hub's POI callouts
CALLOUTS_RETRY = 600         # s after a failed look (no hub, older hub, network)
_CALLOUTS: dict = {"at": float("-inf"), "for": None, "map": {}}


def hub_callouts(hub_url: str, fetch=None, now=None, ask: bool = True) -> dict:
    """{poi id: callout} from the team hub's Loot Book POIs (`GET /api/db/pois`, the player's key): the team's own
    short name for a place, shown on hover after the game's name (coach's decision 76, 20 Nov 2026). Only a non-empty
    text `callout` counts; a hub without the field, out of reach or no hub = {} (nothing shown). Asked at most every
    CALLOUTS_TTL s; ask=False (a game runs) = the last answer, no request. The callouts stay in memory: never written
    to the app's files or the public repo."""
    import time
    now = time.time() if now is None else now
    c = _CALLOUTS
    if not hub_url:
        return {}
    if c["for"] == hub_url and (not ask or now - c["at"] < (CALLOUTS_TTL if c["map"] else CALLOUTS_RETRY)):
        return dict(c["map"])
    if not ask:
        return {}
    if fetch is None:
        from . import hub
        if not hub.get_key():
            return {}
        fetch = lambda url, path: hub.get_json(url, path, timeout=6)
    out = {}
    try:
        docs = fetch(hub_url, "/api/db/pois")
        for d in docs if isinstance(docs, list) else []:
            if not isinstance(d, dict):
                continue
            data = d["data"] if isinstance(d.get("data"), dict) else d
            pid = d.get("id") or data.get("id")
            co = data.get("callout")
            if isinstance(pid, str) and isinstance(co, str) and co.strip():
                out[pid] = co.strip()[:40]
    except Exception:
        out = {}
    c.update({"at": now, "for": hub_url, "map": out})
    return dict(out)


def app_poi_names(hub_url: str = "", fetch=None, ask: bool = True) -> dict:
    """{"names": {poi id: name}, "callouts": {poi id: callout}, "lang": "en"} for the app's page. POI names are game
    words: they stay as the game writes them in English in every app language (coach's rule, 19 Nov 2026: "keep any
    game related terms in English"), so the team hub's FR / KO names table is not asked here (the hub's Loot Book has
    its own switch). The main name is the game's (ALGS) name; the team's callout, when the hub has one, is only shown
    on hover as "Name (callout)" (decision 76)."""
    return {"names": {pid: name for ps in pois().values() for pid, name in ps.items()},
            "callouts": hub_callouts(hub_url, fetch, ask=ask), "lang": "en"}


def poi_label(mp: str | None, poi_id: str | None) -> str:
    """'we-mirage' -> its name in the map table ('Mirage à Trois'), else the id's words."""
    if not poi_id:
        return ""
    name = pois().get(mp or "", {}).get(poi_id)
    if name:
        return name
    words = poi_id.split("-")[1:] or poi_id.split("-")
    return " ".join(w.capitalize() for w in words)


def our_spot(g: dict) -> tuple[str | None, str]:
    """Where the scan says we went: (POI id, "landing" | "drop screen"), or (None, "")."""
    ld = g.get("landing") or {}
    if ld.get("poi"):
        return ld["poi"], "landing"
    spot = next((x for x in (g.get("drop") or {}).get("spots", []) if x.get("kind") == "ours" and x.get("poi")), None)
    return (spot["poi"], "drop screen") if spot else (None, "")


def compare_drop(mp: str | None, eec_drop: str, poi_id: str | None) -> tuple[str | None, str]:
    """('same' | 'near' | 'different' | None, why). None = can't be compared. The EEC name is looked up with our
    names and aliases first (`resolve`: EEC 'Canal' = our Canal Plaza, 'Skyhook West' = Trials); a name we don't know
    falls back to the old rule, 'near' = one name holds the other."""
    if not eec_drop:
        return None, "no EEC drop for this game"
    if not poi_id:
        return None, "the scan has no landing place"
    ours = poi_label(mp, poi_id)
    rid = resolve(mp, eec_drop)
    if rid:
        if rid == poi_id:
            return "same", f"EEC drop {eec_drop} = our landing"
        return "different", f"EEC drop {eec_drop}, our scan says {ours}"
    a = norm(_NUM.sub("", eec_drop))
    names = [norm(n) for n in pois().get(mp or "", {}).values()]
    if names and not any(a == n or a in n or n in a for n in names):
        return None, f"EEC drop {eec_drop} isn't in our map table"
    b = norm(ours)
    if a == b:
        return "same", f"EEC drop {eec_drop} = our landing"
    if a and b and (a in b or b in a):
        return "near", f"EEC drop {eec_drop} ~ our landing {ours}"
    return "different", f"EEC drop {eec_drop}, our scan says {ours}"


def check_block(res: dict | None, games: list[dict]) -> dict:
    """Our scanned games of a block against an EEC result (`results()`).
    games: [{"rec": recording key, "i": game index in the recording, "at": clock time the game started (the jump when
    seen), "map", "game": the scan's game}].
    Returns {"verdict": "match" | "mismatch" | "unknown", "why", "lobby", "games": [one per compared game: rec, i,
    eecGame, map, eecDrop, ourPoi, ourDrop, ourFrom, check, why]}. "unknown" = nothing to decide on: old behaviour."""
    out = {"verdict": "unknown", "why": "", "lobby": (res or {}).get("url", ""), "games": []}
    if not res:
        out["why"] = "no EEC results"
        return out
    eg = [g for g in res.get("games", []) if g.get("start")]
    timed = [g for g in games if g.get("at")]
    if not timed:
        out["why"] = "no scanned games"
        return out
    if not eg:
        out["why"] = "the EEC results have no game times"
        return out
    used: set = set()
    for g in sorted(timed, key=lambda x: x["at"]):
        cand = [e for e in eg if e["game"] not in used and abs(e["start"] - g["at"]) < TIME_FIT_S
                and (g.get("map") in (None, "", "?") or not e.get("map") or g["map"] == e["map"])]
        if not cand:
            continue
        e = min(cand, key=lambda e: abs(e["start"] - g["at"]))
        used.add(e["game"])
        poi, src = our_spot(g.get("game") or {})
        check, why = compare_drop(e.get("map"), e.get("drop", ""), poi)
        out["games"].append({"rec": g.get("rec"), "i": g.get("i"), "eecGame": e["game"], "map": e.get("map"),
                             "eecDrop": e.get("drop", ""), "ourPoi": poi, "ourDrop": poi_label(e.get("map"), poi),
                             "ourFrom": src, "check": check, "why": why})
    agree = sum(1 for x in out["games"] if x["check"] in ("same", "near"))
    differ = sum(1 for x in out["games"] if x["check"] == "different")
    if not out["games"]:
        out["verdict"], out["why"] = "mismatch", "no game of this block was played at the EEC lobby's game times"
    elif agree > differ:
        out["verdict"] = "match"
        out["why"] = f"EEC drops agree with where we landed in {agree} of {agree + differ} game(s)"
    elif differ > agree:
        out["verdict"] = "mismatch"
        out["why"] = f"EEC drops differ from where we landed in {differ} of {agree + differ} game(s)"
    elif not any(x["eecDrop"] for x in out["games"]):
        out["why"] = "the EEC results have no drops (game times fit)"
    else:
        out["why"] = (f"EEC drops agree in {agree} and differ in {differ} game(s) (game times fit)" if agree
                      else "no drop could be compared (game times fit)")
    return out
