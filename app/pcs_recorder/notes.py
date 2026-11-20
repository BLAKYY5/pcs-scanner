"""Coach notes and drawings on recordings, kept on the team hub (hub 0.4: /api/annotations).

- A recording is known to the hub by its file name without the extension and by the player who sent its scan.
  A player's key reads only that player's recordings; a coach key may read and write anyone's.
- The last answer for each recording is kept in the data folder (`notes-cache/`), so the notes still show
  when the hub can't be reached. No hub address, no key, or a hub without notes: nothing is shown.
- Replies (hub with `/api/annotations/<id>/replies`): each note has `replies` ("Got it" = kind `ack`, or a short
  text, by the player or the coach). They come with the notes, so they are cached for offline the same way.
  Sending one needs the hub (no queue): the page says so when it can't be reached.
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
FIELDS = ("t", "dur", "text", "scope", "drawings", "replayFrom", "replayTo")   # replay range: hub 0.6, game time


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
    d["notes"] = [clean_note(n) for n in d["notes"] if isinstance(n, dict)]
    return d


COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


def num(v, default: float = 0.0) -> float:
    """A number from the hub, or `default`: the page puts these straight into its markup (times, positions)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f and abs(f) < 1e7 else default


def clean_note(n: dict) -> dict:
    """The hub checks notes when they are written; the page still gets only numbers where it expects numbers and
    plain colours, whatever a hub sends."""
    n = dict(n)
    n["t"], n["dur"] = num(n.get("t")), num(n.get("dur"), 5.0)
    # the 2D replay range that goes with the note (hub 0.6; seconds after the jump): both numbers or neither
    a, b = num(n.get("replayFrom"), None), num(n.get("replayTo"), None)
    n.pop("replayFrom", None); n.pop("replayTo", None)
    if a is not None and b is not None and a < b:
        n["replayFrom"], n["replayTo"] = a, b
    dr = n.get("drawings") if isinstance(n.get("drawings"), list) else []
    if "replies" in n:                  # (a hub from before replies sends none: the page then offers no reply box)
        n["replies"] = [clean_reply(r) for r in (n["replies"] if isinstance(n["replies"], list) else [])
                        if isinstance(r, dict)][:MAX_REPLIES]
    n["drawings"] = [{**s, "color": s.get("color") if COLOR_RE.fullmatch(str(s.get("color") or "")) else "#ffd60a",
                      "pts": [[num(p[0]), num(p[1])] for p in (s.get("pts") or []) if isinstance(p, (list, tuple))
                              and len(p) == 2]}
                     for s in dr if isinstance(s, dict)]
    return n


MAX_REPLIES = 100
MAX_REPLY = 500


def clean_reply(r: dict) -> dict:
    """Only the fields the page shows, as plain strings (the page escapes them too)."""
    return {"id": str(r.get("id") or "")[:64], "by": str(r.get("by") or "")[:80],
            "role": "coach" if r.get("role") == "coach" else "player",
            "kind": "ack" if r.get("kind") == "ack" else "text", "text": str(r.get("text") or "")[:MAX_REPLY],
            "at": str(r.get("at") or "")[:40]}


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
        base = dict(cached) if isinstance(cached, dict) else {"notes": [], "known": False, "players": []}
        base["notes"] = [clean_note(n) for n in base.get("notes") or [] if isinstance(n, dict)]
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


def reply(url: str, note_id: str, ack: bool = False, text: str = "") -> dict:
    """"Got it" (`ack`) or a short text reply under a note. A hub without replies answers 404 / 405."""
    text = str(text or "").strip()
    if not ack and not text:
        raise ValueError("Write a reply first")
    if len(text) > MAX_REPLY:
        raise ValueError(f"A reply is at most {MAX_REPLY} characters")
    r = hub.request(url, "POST", f"/api/annotations/{_id(note_id)}/replies", timeout=10,
                    json={"ack": True} if ack else {"text": text})
    if r.status_code == 405:
        raise hub.HubError("the team hub doesn't take replies yet (needs a hub update)", "http", 405)
    hub._check(r)
    return r.json()


def delete_reply(url: str, note_id: str, reply_id: str) -> None:
    hub._check(hub.request(url, "DELETE", f"/api/annotations/{_id(note_id)}/replies/{_id(reply_id)}", timeout=10))
