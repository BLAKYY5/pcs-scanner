"""Teammates' paths on the Map panel (review player "Squad" switch) and the views' paths in Watch together.

Only what the team hub already gives every key (no hub change):
- `GET /api/db/timings`: one document per game and player (`gameKey` = same game across players, `uploadedBy`,
  `recording.file` = the recording's file name without extension, `recording.jumpAt` = the jump in that recording).
- `GET /api/scans` and `GET /api/scans/<id>/file`: the scan files players sent, with the debug overlay's positions
  (`games[].samples` rows: t, _, _, spectating, x, y, z, speed; the review page's `seen` rows are made from them).

A path is kept in game time (0 = the jump from the dropship), so it lines up with any player's recording of that game:
recording time = that recording's jump + game time.

Cache (`squad-cache/` in the data folder): the hub's game list (asked again after `LIST_TTL`) and, per scan file, only
the paths (a scan file never changes on the hub: kept until the cache folder is cleared). Offline, the copies show.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from . import hub
from .settings import APP_DIR

log = logging.getLogger("squadpaths")
CACHE_DIR = APP_DIR / "squad-cache"
LIST_TTL = 600
SAME_JUMP_S = 3.0          # our recording's game on the hub: jump within this many seconds
GAP_S = 6                  # no read for this long: the path has a gap
MAX_POINTS = 4000
NEEDS_HUB = "Squad paths need the team hub"
NOT_ON_HUB = "This game isn't on the hub yet"
_lock = threading.Lock()


def slug(name: str) -> str:
    """Same as the hub's: names compared in lower case, letters and digits only."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:24] or "unknown"


def _read(name: str) -> dict:
    try:
        d = json.loads((CACHE_DIR / name).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(name: str, d: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        f = CACHE_DIR / name
        tmp = f.with_name(f.name + ".part")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, f)
    except OSError as e:
        log.warning("squad cache not written: %s", e)


def _num(v):
    try:
        v = float(v)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _games(rows) -> list[dict]:
    """`timings` documents -> [{gameKey, player, file, jumpAt, map}] (only those with all of it)."""
    out = []
    for row in rows if isinstance(rows, list) else []:
        d = (row or {}).get("data") if isinstance(row, dict) else None
        if not isinstance(d, dict):
            continue
        rec = d.get("recording") if isinstance(d.get("recording"), dict) else {}
        jump = _num(rec.get("jumpAt"))
        if not d.get("gameKey") or not rec.get("file") or jump is None:
            continue
        out.append({"gameKey": str(d["gameKey"]), "player": str(d.get("uploadedBy") or ""), "file": str(rec["file"]),
                    "jumpAt": jump, "map": d.get("map")})
    return out


def _scans(rows) -> list[dict]:
    """`/api/scans` rows -> [{id, uploader, recording}] of scan files, newest first (as the hub lists them)."""
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or r.get("id") is None:
            continue
        name = str(r.get("name") or "")
        if r.get("kind", "scan") != "scan" and not name.lower().endswith(".scan.json"):
            continue
        rec = r.get("recording") or (name[:-len(".scan.json")] if name.lower().endswith(".scan.json") else "")
        if rec:
            out.append({"id": int(r["id"]), "uploader": str(r.get("uploader") or ""), "recording": str(rec)})
    return out


def _lists(url: str) -> tuple[dict, bool]:
    """The hub's games and scans (cached for LIST_TTL). Returns (lists, offline)."""
    c = _read("lists.json")
    if c.get("url") == url and time.time() - float(c.get("at") or 0) < LIST_TTL:
        return c, False
    try:
        g = hub.get_json(url, "/api/db/timings", timeout=10)
        s = hub.get_json(url, "/api/scans?limit=1000", timeout=10)
        c = {"at": time.time(), "url": url, "games": _games(g), "scans": _scans(s)}
        _write("lists.json", c)
        return c, False
    except (hub.HubError, ValueError, OSError) as e:
        log.info("squad lists not read: %s", e)
        if c.get("url") != url:
            raise
        return c, True


def paths_of_scan(scan: dict) -> list[dict]:
    """A scan file -> per game {jump, map, path: [[game time, x, y], ...] (None row = a gap), land: [x, y] | None}."""
    out = []
    for g in scan.get("games") or []:
        L = g.get("landing") or {}
        jump = _num(L.get("exitAtExact")) if L.get("exitAtExact") is not None else _num(L.get("exitAt"))
        if jump is None:
            continue
        end = _num(g.get("tEndGame"))
        pts, last = [], None
        for r in g.get("samples") or []:
            if not isinstance(r, list) or len(r) < 6:
                continue
            t, spec, x, y = _num(r[0]), r[3], _num(r[4]), _num(r[5])
            if t is None or spec or x is None or y is None or t < jump - 1 or (end is not None and t > end + 2):
                continue
            if last is not None and t - last > GAP_S:
                pts.append(None)
            pts.append([round(t - jump, 1), round(x), round(y)])
            last = t
        if len(pts) > MAX_POINTS:
            step = len(pts) / MAX_POINTS
            pts = [pts[int(i * step)] for i in range(MAX_POINTS)]
        land = [round(L["x"]), round(L["y"])] if _num(L.get("x")) is not None and _num(L.get("y")) is not None else None
        out.append({"jump": round(jump, 3), "map": g.get("map"), "path": pts, "land": land})
    return out


def _scan_paths(url: str, sid: int, offline: bool) -> list[dict] | None:
    name = f"scan-{int(sid)}.json"
    c = _read(name)
    if c.get("url") == url and isinstance(c.get("games"), list):
        return c["games"]
    if offline:
        return None
    try:
        r = hub.request(url, "GET", f"/api/scans/{int(sid)}/file", timeout=60)
        hub._check(r)
        games = paths_of_scan(r.json())
    except (hub.HubError, ValueError, OSError) as e:
        log.info("scan %s not read: %s", sid, e)
        return None
    _write(name, {"url": url, "games": games})
    return games


def _player_path(url: str, lists: dict, doc: dict, offline: bool) -> dict:
    who = slug(doc["player"])
    scan = next((s for s in lists.get("scans", []) if s["recording"] == doc["file"] and slug(s["uploader"]) == who), None)
    p = {"player": doc["player"] or "?", "recording": doc["file"], "path": [], "land": None}
    games = _scan_paths(url, scan["id"], offline) if scan else None
    if games:
        g = min(games, key=lambda g: abs(g["jump"] - doc["jumpAt"]))
        if abs(g["jump"] - doc["jumpAt"]) <= SAME_JUMP_S:
            p.update(path=g["path"], land=g["land"])
    return p


def get(url: str, *, game_key: str = "", recording: str = "", jump: float | None = None) -> dict:
    """Every player's path in one game, in game time.

    `game_key`: the whole game (Watch together). `recording` + `jump`: the game of our recording whose jump is at
    `jump` s (review player); our own recording is left out (the panel already draws it).
    {"available": True, "gameKey", "map", "players": [{player, recording, path, land}], "offline"} or
    {"available": False, "text"}."""
    if not url or not hub.get_key():
        return {"available": False, "text": NEEDS_HUB}
    with _lock:
        try:
            lists, offline = _lists(url)
        except (hub.HubError, ValueError, OSError):
            return {"available": False, "text": NEEDS_HUB}
        games = lists.get("games", [])
        if not game_key:
            if jump is None:
                return {"available": False, "text": NOT_ON_HUB}
            mine = [g for g in games if g["file"] == recording and abs(g["jumpAt"] - jump) <= SAME_JUMP_S]
            if not mine:
                return {"available": False, "text": NOT_ON_HUB, "offline": offline}
            game_key = mine[0]["gameKey"]
        docs = [g for g in games if g["gameKey"] == game_key and not (recording and g["file"] == recording)]
        players = [_player_path(url, lists, d, offline) for d in docs]
        mp = next((g["map"] for g in games if g["gameKey"] == game_key and g.get("map")), None)
        return {"available": True, "gameKey": game_key, "map": mp, "offline": offline,
                "players": sorted(players, key=lambda p: p["player"].lower())}
