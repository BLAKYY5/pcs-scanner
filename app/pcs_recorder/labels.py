"""What kind of Apex session a recording is: scrims, ranked, pubs or wildcard.

The player (or coach) can set it by hand in the app; otherwise it comes from the scan: the scrim drop screen
means scrims, the ranked badge means ranked. Pubs and wildcard can't be told apart by the scan yet, so those
are set by hand. The label decides what goes to YouTube and the word in the video title.
"""
from __future__ import annotations

import json
import os
from collections import Counter

from .settings import APP_DIR

FILE = APP_DIR / "labels.json"
KINDS = ("scrims", "ranked", "pubs", "wildcard")
TITLE = {"scrims": "Scrims", "ranked": "Ranked", "pubs": "Pubs", "wildcard": "Wildcard"}


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def load() -> dict[str, str]:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        return {k: v for k, v in d.items() if v in KINDS}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def manual(path: str) -> str | None:
    return load().get(_norm(path))


def set_label(path: str, kind: str | None) -> None:
    if kind and kind not in KINDS:
        raise ValueError(f"Unknown label: {kind}")
    d = load()
    if kind:
        d[_norm(path)] = kind
    else:
        d.pop(_norm(path), None)
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(d, indent=1), encoding="utf-8")


def auto(scan: dict) -> str | None:
    """From the scan: scrims when the scrim drop screen was read, ranked when the ranked badge was seen."""
    games = scan.get("games", []) if scan else []
    if any(g.get("mode") == "scrim" and g.get("drop") for g in games):
        return "scrims"
    if any(g.get("mode") == "ranked" for g in games):
        return "ranked"
    return None


def of(path: str, scan: dict) -> tuple[str | None, bool]:
    """(label, set by hand?)"""
    m = manual(path)
    return (m, True) if m else (auto(scan), False)


def of_block(paths: list[str], scans: list[dict]) -> tuple[str | None, bool]:
    """A block's label: what was set by hand on its recordings (most common), else what the scans say."""
    d = load()
    hand = [d[_norm(p)] for p in paths if _norm(p) in d]
    if hand:
        return Counter(hand).most_common(1)[0][0], True
    found = [auto(s) for s in scans]
    for k in ("scrims", "ranked"):
        if k in found:
            return k, False
    return None, False
