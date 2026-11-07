"""What the recording sounds like, for the review: loudness of each audio track every half second, plus the
moments found from it (loud game sound = likely a fight; loud mic = a loud call; how much the mic talked).

These are first, simple rules on loudness only. Recognising specific sounds (knock, shield break, ring alarm)
needs reference clips of each sound and comes later.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import numpy as np

STEP = 0.5                 # seconds per value
RATE = 8000                # decoded sample rate (enough for loudness)
VERSION = 1


def _quiet() -> int:
    return 0x08000000 | 0x00004000 if os.name == "nt" else 0       # no window, below-normal priority


def track_db(video: str, index: int) -> np.ndarray:
    """Loudness (dBFS) of one audio track, one value per STEP seconds."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", video, "-map", f"0:a:{index}", "-vn", "-ac", "1",
           "-ar", str(RATE), "-f", "s16le", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_quiet())
    n = int(RATE * STEP)
    out, buf = [], b""
    while True:
        chunk = p.stdout.read(1 << 20)
        if not chunk:
            break
        buf += chunk
        usable = len(buf) // (2 * n) * (2 * n)
        if usable:
            a = np.frombuffer(buf[:usable], dtype=np.int16).astype(np.float32).reshape(-1, n) / 32768
            out.append(20 * np.log10(np.sqrt((a * a).mean(axis=1)) + 1e-6))
            buf = buf[usable:]
    p.wait()
    return np.concatenate(out) if out else np.zeros(0, np.float32)


def segments(mask: np.ndarray, gap: int, min_len: int) -> list[tuple[int, int]]:
    """Runs of True (index ranges), joining runs less than `gap` apart, keeping those at least `min_len` long."""
    runs, start, last = [], None, None
    for i in np.flatnonzero(mask):
        if start is None:
            start = last = i
        elif i - last <= gap:
            last = i
        else:
            runs.append((start, last))
            start = last = i
    if start is not None:
        runs.append((start, last))
    return [(a, b) for a, b in runs if b - a + 1 >= min_len]


def analyse(video: str, tracks: list[dict]) -> dict:
    out = {"version": VERSION, "step": STEP, "tracks": [], "fights": [], "calls": [], "talk": None}
    for t in tracks:
        db = track_db(video, t["n"])
        if not len(db):
            continue
        level = np.clip((db + 60) / 60 * 255, 0, 255).astype(int)          # -60..0 dBFS -> 0..255
        out["tracks"].append({"key": t["key"], "label": t["label"], "level": level.tolist()})
        if t["key"] in ("game", "track1") and not out["fights"]:
            # likely fights: game sound well above its usual level for a few seconds (gunfire is loud and dense)
            thr = max(float(np.median(db)) + 10, -32)
            out["fights"] = [[float(a * STEP), float((b + 1) * STEP)] for a, b in segments(db > thr, gap=6, min_len=8)]
        if t["key"] == "mic":
            speaking = db > max(np.percentile(db, 40) + 12, -48)
            out["talk"] = round(float(speaking.mean()), 3)
            thr = max(np.percentile(db, 98), -24)
            peaks, last = [], -1e9
            for i in np.flatnonzero(db > thr):
                if i * STEP - last >= 10:                  # one marker per loud moment
                    peaks.append(float(i * STEP))
                    last = i * STEP
            out["calls"] = peaks
    return out


def cached(video: str, tracks: list[dict], cache: Path) -> dict:
    """Analyse once, then reuse (the file is remade if the recording changed)."""
    try:
        if cache.exists() and cache.stat().st_mtime >= Path(video).stat().st_mtime:
            d = json.loads(cache.read_text(encoding="utf-8"))
            if d.get("version") == VERSION:
                return d
    except (OSError, json.JSONDecodeError):
        pass
    d = analyse(video, tracks)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(d), encoding="utf-8")
    return d
