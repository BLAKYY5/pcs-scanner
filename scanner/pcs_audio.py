"""Sound cues in the game track, to back up what the scanner reads on screen.

Apex plays the same sound files every game, whatever the language, so a cue can be recognised by its
fingerprint. Found so far (from the team's own recordings, game track only):
  - ring starts moving: an alarm of steady tones near 374 Hz and 533 Hz (plus 1440 / 1973 Hz),
    pulsing three times about 1.75 s apart, starting with the ring.
More cues get added as they are learned from recordings (knock and kill confirm, ping, …).
"""
from __future__ import annotations

import subprocess

import numpy as np

SR = 8000
NPER, HOP = 2048, 512                       # 3.9 Hz bins, 64 ms frames

# a cue made of steady tones that all start together
TONE_CUES = {
    "ring_moving": {"freqs": (374.1, 532.9), "min_db": 9.0, "min_len": 0.4, "merge": 6.0},
}


def load(path: str, stream: int = 0, ffmpeg: str = "ffmpeg") -> np.ndarray:
    """The recording's game track (first audio track) as mono 8 kHz float32."""
    cmd = [ffmpeg, "-v", "error", "-i", path, "-map", f"0:a:{stream}", "-ac", "1", "-ar", str(SR),
           "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    return np.frombuffer(raw, "<i2").astype(np.float32) / 32768


def tone_strength(a: np.ndarray, freqs, chunk_s: int = 600):
    """Per frame and frequency: how far the tone stands above its ±30 Hz neighbourhood (dB)."""
    from scipy import signal
    times, rows = [], []
    n = chunk_s * SR
    for s in range(0, len(a), n):
        seg = a[s:s + n + NPER]
        if len(seg) < NPER:
            break
        f, t, Z = signal.stft(seg, SR, nperseg=NPER, noverlap=NPER - HOP, boundary=None, padded=False)
        keep = t < chunk_s if s + n < len(a) else np.ones(len(t), bool)
        P = 10 * np.log10(np.abs(Z[:, keep]) ** 2 + 1e-12)
        df = f[1]
        out = []
        for fr in freqs:
            i = int(round(fr / df))
            nb = [j for j in range(i - int(30 / df), i + int(30 / df) + 1) if abs(j - i) > int(6 / df) + 1]
            out.append(P[i - 1:i + 2].max(0) - np.median(P[nb], 0))
        rows.append(np.array(out))
        times.append(t[keep] + s / SR)
    if not rows:
        return np.zeros(0), np.zeros((len(freqs), 0))
    return np.concatenate(times), np.concatenate(rows, 1)


def tone_cue(a: np.ndarray, freqs, min_db=8.0, min_len=0.4, merge=6.0) -> list[dict]:
    """Moments where all the tones sound together: [{t, db}] (t = start, in seconds)."""
    T, R = tone_strength(a, freqs)
    if not len(T):
        return []
    both = R.min(0)
    k = max(1, int(0.5 * SR / HOP))
    sm = np.convolve(both, np.ones(k) / k, "same")
    on = sm > min_db
    d = np.diff(np.r_[0, on.astype(int), 0])
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    out = []
    for a0, b0 in zip(starts, ends):
        if T[min(b0, len(T) - 1)] - T[a0] < min_len:
            continue
        t, db = float(T[a0]), float(sm[a0:b0].max())
        if out and t - out[-1]["t"] < merge:          # the pulses of one alarm
            out[-1]["db"] = max(out[-1]["db"], round(db, 1))
            out[-1]["pulses"] += 1
            continue
        out.append({"t": round(t, 2), "db": round(db, 1), "pulses": 1})
    return out


def cues(a: np.ndarray) -> dict[str, list[dict]]:
    return {name: tone_cue(a, **c) for name, c in TONE_CUES.items()}


def scan(path: str, ffmpeg: str = "ffmpeg") -> dict:
    try:
        a = load(path, ffmpeg=ffmpeg)
    except (subprocess.CalledProcessError, OSError) as e:
        return {"error": str(e)[:200]}
    return {"seconds": round(len(a) / SR, 1), "cues": cues(a)}


def attach(games: list[dict], found: dict) -> None:
    """Match the sound cues with the rings read on screen: confirm them, sharpen their time, add missed ones."""
    moves = [c["t"] for c in found.get("cues", {}).get("ring_moving", [])]
    starts = sorted(g.get("tStart", 0) for g in games)
    for g in games:
        lo = g.get("tStart", 0)
        hi = next((s for s in starts if s > lo), 1e12)            # up to the next game
        used = set()
        for r in g.get("ring", []):
            if r.get("kind") != "wait" or r.get("endsAt") is None:
                continue
            near = [t for t in moves if abs(t - r["endsAt"]) <= 2.5]
            if near:
                t = min(near, key=lambda t: abs(t - r["endsAt"]))
                r["soundAt"] = t                                  # the alarm: when the ring really starts moving
                used.add(t)
        # heard during this game but not read on screen (map open, dead, spectating…)
        g["ringSound"] = [t for t in moves if lo <= t < hi and t not in used]
