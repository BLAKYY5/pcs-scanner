"""Scrim results from EEC, for the YouTube description: standing out of the lobby, points, kills, per game.

Two sources, both of data a person fetched from EEC by hand (EEC's terms forbid bots and scrapers, so this
app never reads the EEC site itself):
- the lobby's export file, from the lobby page's Download button (lobbies_<num>_<code>_scores.json);
- the team hub, when the coach already imported that export into the Loot Book (collection "scrims").

Points use ALGS scoring, like the Loot Book: placement points plus one per kill.
"""
from __future__ import annotations

import json
import re
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


def from_export(data: dict, filename: str = "") -> dict:
    """EEC Download file -> {"url", "games": [{"start", "map", "rows": [[team, player, place, kills], ...]}]}."""
    if not isinstance(data, dict) or not isinstance(data.get("matches"), list) or not data["matches"]:
        raise ValueError("This isn't an EEC lobby export (lobbies_..._scores.json from the lobby's Download button)")
    m = FILE_RE.search(filename or "")
    url = f"https://eternalesports.club/lobbies/{m.group(1)}/{m.group(2)}" if m else ""
    games = []
    for g in data["matches"]:
        rows = [[r.get("team_name", ""), r.get("player_name", ""), int(r.get("team_placement") or 0),
                 int(r.get("kills") or 0)] for r in g.get("data") or []]
        games.append({"start": float(g.get("match_start") or 0), "map": eec_map(g.get("map_name", "")), "rows": rows})
    return {"url": url, "games": games}


def from_hub_doc(doc: dict) -> dict:
    """A Loot Book "scrims" document (rows: [team, player, legend, place, kills, ...])."""
    games = [{"start": float(g.get("start") or 0), "map": g.get("map", ""),
              "rows": [[r[0], r[1], int(r[3] or 0), int(r[4] or 0)] for r in g.get("rows", [])]}
             for g in doc.get("games", [])]
    return {"url": doc.get("url", ""), "games": games}


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
                             "pts": pts, "teams": len(teams)})
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
            "leader": {"team": order[0], "pts": totals[order[0]]["pts"]}}


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
    """Ask the team hub for an imported lobby that was played during this block."""
    import requests
    r = requests.get(f"{hub_url}/api/db/scrims", timeout=10)
    r.raise_for_status()
    for d in r.json():
        doc = d.get("data", d)
        lobby = from_hub_doc(doc)
        starts = [g["start"] for g in lobby["games"] if g["start"]]
        if starts and start - 1800 <= min(starts) <= end + 600:
            res = results(lobby, team)
            if res:
                return res
    return None
