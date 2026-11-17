"""Coach notes and drawings on recordings, kept on the team hub (hub 0.4: /api/annotations).

- A recording is known to the hub by its file name without the extension and by the player who sent its scan.
  A player's key reads only that player's recordings; a coach key may read and write anyone's.
- The last answer for each recording is kept in the data folder (`notes-cache/`), so the notes still show
  when the hub can't be reached. No hub address, no key, or a hub without notes: nothing is shown.
- Requests go through `hub.request` (key in the Authorization header, never in the address or a file).
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from . import hub
from .settings import APP_DIR

log = logging.getLogger("notes")
CACHE_DIR = APP_DIR / "notes-cache"
FIELDS = ("t", "dur", "text", "scope", "drawings")


def recording_name(path: str) -> str:
    return Path(path).stem


def _cache_file(item_id: str) -> Path:
    return CACHE_DIR / (re.sub(r"[^A-Za-z0-9_-]", "_", item_id)[:40] + ".json")


def _load(item_id: str) -> dict:
    try:
        d = json.loads(_cache_file(item_id).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(item_id: str, player: str | None, answer: dict) -> None:
    """One file per recording: the last answer per player asked for ("" = the hub's choice)."""
    try:
        d = _load(item_id)
        d[player or ""] = answer
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        f = _cache_file(item_id)
        tmp = f.with_suffix(".part")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, f)
    except OSError as e:
        log.warning("notes cache not written: %s", e)


def _answer(r) -> dict:
    hub._check(r)
    d = r.json()
    if not isinstance(d, dict) or not isinstance(d.get("notes"), list):
        raise hub.HubError("the team hub sent something unexpected", "http", r.status_code)
    return d


def fetch(url: str, path: str, item_id: str, player: str | None = None) -> dict:
    """Notes for this recording: from the hub, or the last ones saved here (`offline`)."""
    if not url or not hub.get_key():
        return {"available": False, "notes": []}
    try:
        r = hub.request(url, "GET", "/api/annotations", timeout=8,
                        params={"recording": recording_name(path), **({"player": player} if player else {})})
        if r.status_code == 404:                                 # a hub from before notes (0.3 or older)
            return {"available": False, "notes": []}
        d = _answer(r)
    except (hub.HubError, ValueError) as e:
        cached = _load(item_id).get(player or "")
        base = cached if isinstance(cached, dict) else {"notes": [], "known": False, "players": []}
        return {**base, "available": bool(cached) or getattr(e, "kind", "") != "refused",
                "offline": True, "error": str(e)}
    _save(item_id, player, d)
    return {**d, "available": True, "offline": False}


def save(url: str, path: str, player: str, note: dict) -> dict:
    """Coach: a new note (no `id`) or a change to one (`id`, `version`). Returns the hub's note."""
    body = {k: note[k] for k in FIELDS if k in note}
    if note.get("id"):
        r = hub.request(url, "PUT", f"/api/annotations/{_id(note['id'])}", json=body, timeout=10,
                        params={"if_version": int(note["version"])} if note.get("version") else None)
    else:
        r = hub.request(url, "POST", "/api/annotations", timeout=10,
                        json={"recording": recording_name(path), "player": player, **body})
    hub._check(r)
    return r.json()


def delete(url: str, note_id: str) -> None:
    hub._check(hub.request(url, "DELETE", f"/api/annotations/{_id(note_id)}", timeout=10))


def seen(url: str, note_id: str) -> None:
    hub._check(hub.request(url, "POST", f"/api/annotations/{_id(note_id)}/seen", timeout=5))


def _id(note_id) -> str:
    s = str(note_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", s):
        raise ValueError("bad note id")
    return s
