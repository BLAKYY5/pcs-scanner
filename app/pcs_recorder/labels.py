"""What kind of Apex session a recording is: scrims, ranked, pubs or wildcard.

The player (or coach) can set it by hand in the app (a label set by hand always wins); otherwise it comes from
the scan: the scrim drop screen means scrims, the ranked badge means ranked, the loading banner's mode word or the
playlist emblem at the top right of the HUD means wildcard or pubs (scanner: games[].modeSeen). A game whose
signs disagree, or that shows another playlist's emblem, stays unlabelled. The label decides what goes to YouTube
and the word in the video title.

EEC drops confirm scrims (coach, 18 Nov 2026): when a block's EEC results fit its games by map and time and the
EEC drops agree with where we landed (eec.check_block "match"), the block is scrims with source "eec", even when the
drop screen wasn't read. When they disagree ("mismatch", e.g. a tournament the same evening), the block is not that
scrim: its label comes from the scan alone and the EEC results are not used for it. `explain_block` says why.
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
    """From the scan: scrims when the scrim drop screen was read, ranked when the ranked badge was seen,
    wildcard or pubs when the loading banner before a game said so (in that order of priority)."""
    games = scan.get("games", []) if scan else []
    if any(g.get("mode") == "scrim" and g.get("drop") for g in games):
        return "scrims"
    if any(g.get("mode") == "ranked" for g in games):
        return "ranked"
    if any(g.get("mode") == "wildcard" for g in games):
        return "wildcard"
    if any(g.get("mode") == "pubs" for g in games):
        return "pubs"
    return None


def of(path: str, scan: dict) -> tuple[str | None, bool]:
    """(label, set by hand?)"""
    m = manual(path)
    return (m, True) if m else (auto(scan), False)


def of_block(paths: list[str], scans: list[dict], eec_check: dict | None = None) -> tuple[str | None, bool]:
    """A block's label: what was set by hand on its recordings (most common), else EEC's drops when they confirm
    the block (eec_check), else what the scans say."""
    r = explain_block(paths, scans, eec_check)
    return r["label"], r["source"] == "hand"


_MODE = {"scrims": "scrim", "ranked": "ranked", "wildcard": "wildcard", "pubs": "pubs"}
_SIGN = {"badge": "the ranked badge", "select": "the rank badges at legend select", "banner": "the loading banner",
         "emblem": "the playlist emblem", "drop": "the scrim drop screen"}


def _scan_why(kind: str | None, scans: list[dict] | None = None) -> str:
    if not kind:
        return "nothing in the scan tells"
    signs: list[str] = []
    for s in scans or []:
        for g in (s or {}).get("games", []):
            if g.get("mode") == _MODE[kind]:
                signs += [x for x in (g.get("modeSeen") or {}).get("from", []) if x in _SIGN and x not in signs]
    if signs:
        words = [_SIGN[x] for x in signs]
        said = ", ".join(words[:-1]) + " and " + words[-1] if len(words) > 1 else words[0]
        return said + " showed " + TITLE[kind]
    return {"scrims": "the scrim drop screen was read", "ranked": "the ranked badge was seen",
            "wildcard": "the loading banner said wildcard", "pubs": "the loading banner said trios (pubs)"}[kind]


def explain_block(paths: list[str], scans: list[dict], eec_check: dict | None = None) -> dict:
    """{"label", "source": "hand" | "eec" | "scan" | None, "why"} for a block of recordings."""
    d = load()
    hand = [d[_norm(p)] for p in paths if _norm(p) in d]
    if hand:
        return {"label": Counter(hand).most_common(1)[0][0], "source": "hand", "why": "set by hand"}
    verdict = (eec_check or {}).get("verdict")
    if verdict == "match":
        return {"label": "scrims", "source": "eec", "why": eec_check.get("why") or "EEC drops agree"}
    found = [auto(s) for s in scans]
    label = next((k for k in ("scrims", "ranked", "wildcard", "pubs") if k in found), None)
    why = _scan_why(label, scans)
    if verdict == "mismatch":
        why += f"; not the EEC lobby: {eec_check.get('why') or 'drops differ'}"
    elif verdict == "unknown" and eec_check.get("why") and eec_check.get("lobby"):
        why += f"; EEC lobby by time only: {eec_check['why']}"
    return {"label": label, "source": "scan" if label else None, "why": why}


def explain(path: str, scan: dict, eec_check: dict | None = None) -> dict:
    """One recording: {"label", "source", "why"} (eec_check: its block's, when there is one)."""
    m = manual(path)
    if m:
        return {"label": m, "source": "hand", "why": "set by hand"}
    if (eec_check or {}).get("verdict") == "match":
        return {"label": "scrims", "source": "eec", "why": eec_check.get("why") or "EEC drops agree"}
    label = auto(scan)
    why = _scan_why(label, [scan])
    if (eec_check or {}).get("verdict") == "mismatch":
        why += f"; not the EEC lobby: {eec_check.get('why') or 'drops differ'}"
    return {"label": label, "source": "scan" if label else None, "why": why}
