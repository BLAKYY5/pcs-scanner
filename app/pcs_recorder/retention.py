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


SENT_FILE = APP_DIR / "sent.json"       # where each recording's results went, kept apart from recordings.json
OLD_RETRY_DAYS = 3                      # recordings sent before this file existed: hub retried only if this recent


def load_sent(idx: dict[str, Entry], hub_url: str = "", now: float | None = None) -> dict[str, dict]:
    """{recording key: {"hub": "sent" | "pending" | "", "why": reason, "drive": "sent" | "failed" | ""}}.

    Kept in its own file so that an older app version still reads recordings.json (it refuses unknown fields).
    The first time (no file yet), recordings already "uploaded" by the old rule (hub OR Drive) are sorted out from
    the note the old version left: "Sent to hub…" = on the hub; otherwise, only those recorded in the last
    OLD_RETRY_DAYS days go to the hub again (when a hub is set), so an update never sends a flood of old scans."""
    try:
        raw = json.loads(SENT_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {k: v for k, v in raw.items() if isinstance(v, dict)}
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError):
        pass
    now = now or time.time()
    out = {}
    for k, e in idx.items():
        if e.status != "uploaded":
            continue
        if e.note.startswith("Sent to hub"):
            out[k] = {"hub": "sent", "why": "", "drive": ""}
        elif hub_url and now - e.started <= OLD_RETRY_DAYS * 86400:
            out[k] = {"hub": "pending", "why": "", "drive": ""}
    save_sent(out, idx)
    return out


def save_sent(sent: dict[str, dict], idx: dict[str, Entry]) -> None:
    for k in [k for k in sent if k not in idx]:          # recording deleted: forget it
        del sent[k]
    SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SENT_FILE.write_text(json.dumps(sent, indent=1), encoding="utf-8")


def files_of(entry: Entry) -> list[Path]:
    """The video plus anything the scanner wrote next to it (same name, other extension).
    A recording without a file (OBS gave none: path "") has no files. Path("") is the working folder and its stem
    is "", so the glob below would match every file there: retention then deleted the whole folder the app was
    started from (the exe's folder) once that failed recording was older than the keep limit."""
    if not entry.path or not entry.path.strip():
        return []
    p = Path(entry.path)
    if not p.stem or not p.is_absolute() or not p.parent.exists():
        return []
    return [f for f in p.parent.glob(p.stem + "*") if f.is_file()]


def to_recycle_bin(path: str) -> None:
    """Windows Recycle Bin (can be restored); elsewhere a normal delete. Raises OSError when it didn't happen."""
    import os
    if os.name != "nt":
        os.remove(path)
        return
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p),
                    ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]
    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, os.path.abspath(path) + "\0", None,
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
    if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) != 0 or op.fAnyOperationsAborted:
        raise OSError("couldn't move it to the Recycle Bin")
    if os.path.exists(path):
        raise OSError("still there after the Recycle Bin")


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
    # (a recording that never had a file, path "", stays listed as failed until the keep limit, then goes)
    for k in [k for k, e in idx.items() if k not in protect and e.path and not files_of(e) and e.status != "recording"]:
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
