"""The scanner's weapon icons file (`pcs_weapons.npz`) from the team hub, like the minimaps.

- The file holds EA's icons: never in this repo. The coach uploads it to the hub once (the hub's
  `tools/upload_weapon_icons.py`); each app reads `GET /api/db/weaponicons/current` with the player's key (404 = none
  uploaded yet) and downloads `GET /_blob/<asset>` only when the asset id changed since the last download.
- The file is checked with the app's own icons check before it replaces the copy in the data folder; a bad file is
  refused and its asset id remembered, so it isn't downloaded again until the coach uploads another one.
- With icons on the hub, the Google Drive "team link" isn't needed; without (no hub, no key, none uploaded, hub
  unreachable) the team link still works as before.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

from . import hub
from .settings import APP_DIR

log = logging.getLogger("weaponicons")
STATE_F = APP_DIR / "weaponicons.json"
ASSET_RE = re.compile(r"[0-9a-f]{32}")
MAX_BYTES = 2 * 1024 * 1024
UPDATED = "Weapon icons updated from the team hub: new scans name the weapons in the kill feed."
BAD = "The team hub's weapon icons file isn't usable. Ask your coach to upload it again."
_lock = threading.Lock()


def state() -> dict:
    """{"url", "asset", "ok", "good"} of the last file seen on the hub ("good" = the asset id of the copy in the data
    folder that came from this hub, kept when a later file is refused); {} when none."""
    try:
        d = json.loads(STATE_F.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(d: dict) -> None:
    STATE_F.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_F.with_name(STATE_F.name + ".part")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.replace(tmp, STATE_F)


def from_hub(url: str, target: str | os.PathLike) -> bool:
    """The icons in `target` came from this hub (so the team link isn't needed)."""
    s = state()
    return bool(url and s.get("url") == url and s.get("good") and Path(target).exists())


def sync(url: str, target: str | os.PathLike, valid) -> tuple[str, str]:
    """Fetch the icons from the hub when they changed. -> (status, text for the player);
    status: none (no hub / no key / none on the hub) / same / updated / bad (file refused) / error (not reachable)."""
    if not url or not hub.get_key():
        return "none", ""
    target = Path(target)
    with _lock:
        try:
            r = hub.request(url, "GET", "/api/db/weaponicons/current", timeout=8)
            if r.status_code == 404:                      # none uploaded yet (or a hub before weapon icons)
                _save_state({"url": url, "asset": None, "ok": False, "good": None})
                return "none", ""
            hub._check(r)
            doc = r.json()
            asset = str(((doc or {}).get("data") or {}).get("asset") or "") if isinstance(doc, dict) else ""
        except (hub.HubError, ValueError, OSError) as e:
            log.info("weapon icons: list not read (%s)", getattr(e, "kind", e.__class__.__name__))
            return "error", ""
        if not ASSET_RE.fullmatch(asset):
            log.info("weapon icons: the hub's document has no usable asset id")
            return "error", ""
        s = state()
        if s.get("url") == url and s.get("asset") == asset:
            if not s.get("ok"):
                return "bad", ""                          # refused before: wait for a new upload
            if target.exists():
                return "same", ""
        try:
            b = hub.request(url, "GET", f"/_blob/{asset}", timeout=60)
            hub._check(b)
            data = b.content
        except (hub.HubError, OSError) as e:
            log.info("weapon icons: not downloaded (%s)", getattr(e, "kind", e.__class__.__name__))
            return "error", ""
        if len(data) > MAX_BYTES or not valid(data):
            log.info("weapon icons: the hub's file was refused (%d bytes)", len(data))
            _save_state({"url": url, "asset": asset, "ok": False, "good": s.get("good") if s.get("url") == url else None})
            return "bad", BAD
        try:
            same = target.exists() and target.read_bytes() == data
            if not same:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".part")
                tmp.write_bytes(data)
                os.replace(tmp, target)
        except OSError:
            log.exception("weapon icons: saving")
            return "error", ""
        _save_state({"url": url, "asset": asset, "ok": True, "good": asset})
        log.info("weapon icons downloaded from the hub (%d KB)", len(data) // 1024)
        return ("same", "") if same else ("updated", UPDATED)
