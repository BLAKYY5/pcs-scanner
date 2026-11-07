"""Keeps the auto-record folder within the player's limits.

Only files the recorder made itself (listed in recordings.json) are ever deleted,
so pointing the folder at an existing Videos folder can't wipe the player's own clips.

Rules, applied after every session and at start-up:
1. Never touch the recording in progress.
2. Delete recordings older than `keep_days`.
3. While the recorder's files take more than `max_gb`, delete the oldest:
   first those already uploaded (or skipped), then the rest.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .settings import APP_DIR

INDEX_FILE = APP_DIR / "recordings.json"
DONE = ("uploaded", "skipped", "no-scanner")


@dataclass
class Entry:
    path: str
    started: float
    status: str = "recording"       # recording | recorded | scanning | scanned | uploaded | skipped | failed | no-scanner
    note: str = ""
    nogame: bool = False            # the scan found no game at all (menus, lobby, firing range…)


def load_index() -> dict[str, Entry]:
    if not INDEX_FILE.exists():
        return {}
    try:
        raw = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        return {k: Entry(**v) for k, v in raw.items()}
    except (json.JSONDecodeError, TypeError, OSError):
        return {}


def save_index(idx: dict[str, Entry]) -> None:
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps({k: vars(v) for k, v in idx.items()}, indent=2), encoding="utf-8")


def files_of(entry: Entry) -> list[Path]:
    """The video plus anything the scanner wrote next to it (same name, other extension)."""
    p = Path(entry.path)
    if not p.parent.exists():
        return []
    return [f for f in p.parent.glob(p.stem + "*") if f.is_file()]


def size_of(entry: Entry) -> int:
    return sum(f.stat().st_size for f in files_of(entry))


def enforce(idx: dict[str, Entry], keep_days: int, max_gb: float, protect: set[str] | None = None,
            now: float | None = None, nogame_keep_h: float = 24) -> list[tuple[str, str]]:
    """Delete what the rules say. Returns [(file name, reason)] for what was removed."""
    now = now or time.time()
    protect = protect or set()
    removed: list[tuple[str, str]] = []

    def drop(key: str, reason: str) -> None:
        for f in files_of(idx[key]):
            try:
                f.unlink()
            except OSError:
                return              # file in use (opened in a player); try again next time
        removed.append((Path(idx[key].path).name, reason))
        del idx[key]

    # forget entries whose files are already gone
    for k in [k for k, e in idx.items() if k not in protect and not files_of(e) and e.status != "recording"]:
        del idx[k]

    for k in sorted(idx, key=lambda k: idx[k].started):
        e = idx[k]
        if k not in protect and e.status not in ("recording", "scanning", "waiting") and now - e.started > keep_days * 86400:
            drop(k, f"older than {keep_days} days")

    # recordings without any game: kept a short while (in case), then deleted
    for k in sorted(idx, key=lambda k: idx[k].started):
        e = idx[k]
        if (k not in protect and e.nogame and e.status not in ("recording", "scanning", "waiting")
                and now - end_time(e) >= nogame_keep_h * 3600):
            drop(k, "no game in it")

    cap = max_gb * 1024 ** 3
    total = sum(size_of(e) for e in idx.values())
    order = sorted((k for k in idx if k not in protect and idx[k].status not in ("recording", "scanning", "waiting")),
                   key=lambda k: (not idx[k].nogame, idx[k].status not in DONE, idx[k].started))   # no-game first
    for k in order:
        if total <= cap:
            break
        s = size_of(idx[k])
        drop(k, f"folder over {max_gb:g} GB")
        if k not in idx:
            total -= s
    return removed


def end_time(e: Entry) -> float:
    try:
        return Path(e.path).stat().st_mtime
    except OSError:
        return e.started


def folder_usage(idx: dict[str, Entry]) -> int:
    return sum(size_of(e) for e in idx.values())
