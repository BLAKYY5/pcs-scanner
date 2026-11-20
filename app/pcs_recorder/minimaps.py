"""The game's minimap pictures for the review player's Map panel, from the team hub (hub 0.5.2: collection `minimaps`).

- The pictures are EA's files: they are never in this repo or the install folder. The coach puts them on the hub once
  (the hub's `tools/upload_minimaps.py`); each player's app downloads one with the player's key the first time a game
  on that map is reviewed, and keeps it in the data folder (`minimaps/`). A new picture on the hub (new asset id) is
  downloaded again; the old copy is removed.
- The hub's list (map code -> asset id + world -> picture transform) is kept in `minimaps/index.json`, asked again at
  most every `INDEX_TTL` seconds (or `MISS_TTL` when the map wasn't in it), so the panel works offline once seen.
- No hub address or no key: nothing is downloaded and the panel says the minimap needs the team hub.

World -> picture (fractions 0..1 from the top left): fx = (x - pos_x) / scale / size, fy = (pos_y - y) / scale / size.
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

log = logging.getLogger("minimaps")
CACHE_DIR = APP_DIR / "minimaps"
INDEX_TTL = 6 * 3600
MISS_TTL = 600
MAX_BYTES = 20 << 20
CODE_RE = re.compile(r"[A-Z]{2,3}")
ASSET_RE = re.compile(r"[0-9a-f]{32}")
TX_KEYS = ("pos_x", "pos_y", "scale", "size")
NEEDS_HUB = "Minimap needs the team hub"
NO_MAP = "No minimap for this map yet"
_lock = threading.Lock()


def to_picture(tx: dict, x: float, y: float) -> tuple[float, float]:
    """World position -> fraction of the picture (0..1 from the top left)."""
    return (x - tx["pos_x"]) / tx["scale"] / tx["size"], (tx["pos_y"] - y) / tx["scale"] / tx["size"]


def _entry(d) -> dict | None:
    """A usable `minimaps` document's data, or None."""
    if not isinstance(d, dict) or not ASSET_RE.fullmatch(str(d.get("asset", ""))):
        return None
    try:
        tx = {k: float(d[k]) for k in TX_KEYS}
    except (KeyError, TypeError, ValueError):
        return None
    if tx["scale"] <= 0 or tx["size"] <= 0:
        return None
    return {"asset": d["asset"], "name": str(d.get("name") or ""), **tx}


def _read_index() -> dict:
    try:
        d = json.loads((CACHE_DIR / "index.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and isinstance(d.get("maps"), dict) else {}
    except (OSError, ValueError):
        return {}


def _write(f: Path, data: bytes) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, f)


def _fetch_index(url: str) -> dict:
    r = hub.request(url, "GET", "/api/db/minimaps", timeout=8)
    if r.status_code == 404:                              # a hub without the collection route: no pictures
        rows = []
    else:
        hub._check(r)
        rows = r.json()
    maps = {}
    for row in rows if isinstance(rows, list) else []:
        code = str((row or {}).get("id", "")).upper()
        e = _entry((row or {}).get("data"))
        if CODE_RE.fullmatch(code) and e:
            maps[code] = e
    idx = {"at": time.time(), "url": url, "maps": maps}
    _write(CACHE_DIR / "index.json", json.dumps(idx).encode())
    return idx


def _image_file(code: str, asset: str) -> Path:
    return CACHE_DIR / f"{code}-{asset}.img"


def _kind(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


def _download(url: str, code: str, asset: str) -> Path:
    r = hub.request(url, "GET", f"/_blob/{asset}", timeout=60)
    hub._check(r)
    data = r.content
    if not _kind(data) or len(data) > MAX_BYTES:          # the hub answers a placeholder drawing for a missing file
        raise hub.HubError("the team hub doesn't have this minimap's picture", "http", r.status_code)
    f = _image_file(code, asset)
    _write(f, data)
    for old in CACHE_DIR.glob(f"{code}-*.img"):           # an older picture of this map (made by us)
        if old != f:
            try:
                old.unlink()
            except OSError:
                pass
    log.info("minimap %s downloaded (%d KB)", code, len(data) // 1024)
    return f


def get(url: str, code: str) -> dict:
    """What the Map panel needs for a map: {"available": True, "code", "name", "asset", "pos_x", ...} with the picture
    in the data folder, or {"available": False, "reason": "hub" | "map", "text"}."""
    code = str(code or "").upper()
    if not CODE_RE.fullmatch(code):
        return {"available": False, "reason": "map", "text": NO_MAP}
    if not url or not hub.get_key():
        return {"available": False, "reason": "hub", "text": NEEDS_HUB}
    with _lock:
        idx = _read_index()
        age = time.time() - float(idx.get("at") or 0)
        known = idx.get("url") == url and code in idx.get("maps", {})
        if idx.get("url") != url or age > INDEX_TTL or (not known and age > MISS_TTL):
            try:
                idx = _fetch_index(url)
            except (hub.HubError, ValueError, OSError) as e:
                log.info("minimap list not read: %s", e)
                if idx.get("url") != url:
                    return {"available": False, "reason": "hub", "text": NEEDS_HUB, "error": str(e)}
        e = idx.get("maps", {}).get(code)
        if not e:
            return {"available": False, "reason": "map", "text": NO_MAP}
        f = _image_file(code, e["asset"])
        if not f.exists():
            try:
                _download(url, code, e["asset"])
            except (hub.HubError, OSError) as err:
                log.info("minimap %s not downloaded: %s", code, err)
                return {"available": False, "reason": "hub", "text": NEEDS_HUB, "error": str(err)}
        return {"available": True, "code": code, **e}


def picture(code: str) -> tuple[Path, str] | None:
    """The cached picture of a map (path, content type), for the page; None when there is none."""
    code = str(code or "").upper()
    if not CODE_RE.fullmatch(code):
        return None
    e = _read_index().get("maps", {}).get(code)
    f = _image_file(code, e["asset"]) if e else None
    if not f or not f.exists():
        return None
    with open(f, "rb") as fh:
        kind = _kind(fh.read(8))
    return (f, kind) if kind else None


SCAN_MAPS = ("WE", "SP", "ED", "BM", "KC", "OL")


def prefetch(url: str) -> int:
    """Every map picture the hub has, downloaded once (the scanner places the player on them: pcs_minimap, for
    players without the debug overlay). Returns how many pictures are here. Nothing without a hub and key."""
    if not url or not hub.get_key():
        return 0
    return sum(1 for code in SCAN_MAPS if get(url, code).get("available"))
