"""Sound cues in the game track, to back up what the scanner reads on screen (and the screen backs up the sound).

Apex plays the same sound files every game, whatever the language, so a cue can be recognised by its
fingerprint. Found so far (from the team's own recordings, game track only):
  - ring starts moving: an alarm of steady tones near 374 Hz and 533 Hz (plus 1440 / 1973 Hz),
    pulsing three times about 1.75 s apart, starting with the ring.
Cross-checks with the screen (measured on three recordings, see `attach`):
  - screen confirms sound: where the ring clock says a countdown ended but no alarm passed the normal bar, a weaker
    alarm right after it is accepted (`soundWeak`); the bar is only lowered inside those few seconds.
  - screen rejects sound: an alarm heard while the ring clock shows the ring already closing, or a countdown still
    far from its end, is not a ring start (another event plays the same tones, e.g. "Loot Bins and Arsenals Reset").
  - sound rejects screen: a game's damage total read only once is refused when the game was quiet between the read
    before it and that read (damage needs hits; see `check_damage`).
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
# screen-guided alarm: within [countdown end - 0.5 s, + 3.5 s] a weaker alarm counts. Measured on 3 recordings
# (7, 15, 17 Nov 2026): random 4 s in-game windows reach 7 dB for 0.3 s in 20 of 5,355 cases (0.4 %); the 4 ring
# starts the normal bar missed reach 8.6 to 12.9 dB, each checked on screen ("RING CLOSING" banner).
GUIDED = {"min_db": 7.0, "min_len": 0.3, "before": 0.5, "after": 3.5}
REJECT_WAIT = 5.0        # an alarm more than this many seconds before a countdown read on screen ends is not a ring start


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


def tone_level(a: np.ndarray, freqs):
    """(times, level): the weakest of the tones, smoothed over 0.5 s, per 64 ms frame (dB above the neighbourhood)."""
    T, R = tone_strength(a, freqs)
    if not len(T):
        return T, np.zeros(0)
    k = max(1, int(0.5 * SR / HOP))
    return T, np.convolve(R.min(0), np.ones(k) / k, "same")


def runs(T, level, min_db, min_len, merge):
    """Moments where the level stays above min_db for min_len seconds: [{t, db, pulses}] (t = start)."""
    on = level > min_db
    d = np.diff(np.r_[0, on.astype(int), 0])
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    out = []
    for a0, b0 in zip(starts, ends):
        if T[min(b0, len(T) - 1)] - T[a0] < min_len:
            continue
        t, db = float(T[a0]), float(level[a0:b0].max())
        if out and t - out[-1]["t"] < merge:          # the pulses of one alarm
            out[-1]["db"] = max(out[-1]["db"], round(db, 1))
            out[-1]["pulses"] += 1
            continue
        out.append({"t": round(t, 2), "db": round(db, 1), "pulses": 1})
    return out


def tone_cue(a: np.ndarray, freqs, min_db=8.0, min_len=0.4, merge=6.0) -> list[dict]:
    """Moments where all the tones sound together: [{t, db, pulses}] (t = start, in seconds)."""
    T, lv = tone_level(a, freqs)
    return runs(T, lv, min_db, min_len, merge) if len(T) else []


def cues(a: np.ndarray) -> dict[str, list[dict]]:
    return {name: tone_cue(a, **c) for name, c in TONE_CUES.items()}


def scan(path: str, ffmpeg: str = "ffmpeg") -> dict:
    """-> {seconds, cues}. Also '_ring' (times, alarm level) for `attach`, which removes it (not JSON)."""
    try:
        a = load(path, ffmpeg=ffmpeg)
    except (subprocess.CalledProcessError, OSError) as e:
        return {"error": str(e)[:200]}
    return analyse(a)


def analyse(a: np.ndarray) -> dict:
    c = TONE_CUES["ring_moving"]
    T, lv = tone_level(a, c["freqs"])
    found = {"ring_moving": runs(T, lv, c["min_db"], c["min_len"], c["merge"]) if len(T) else []}
    return {"seconds": round(len(a) / SR, 1), "cues": found, "_ring": (T, lv), "_loud": loudness(a)}


LOUD_STEP = 0.05          # s per loudness value
DAMAGE_QUIET_DB = 12.0    # a damage rise read once needs game sound this far above the game's median just before it


def loudness(a: np.ndarray) -> np.ndarray:
    """Short-term loudness (dBFS), one value per LOUD_STEP seconds."""
    n = int(SR * LOUD_STEP)
    if len(a) < n:
        return np.zeros(0, np.float32)
    b = a[:len(a) // n * n].reshape(-1, n)
    return (20 * np.log10(np.sqrt((b * b).mean(1)) + 1e-6)).astype(np.float32)


def check_damage(games: list[dict], loud: np.ndarray) -> int:
    """Sound backs up a damage total that rests on a single read (`own.damageSingleRead`): damage needs hits, and
    hits are loud. On the 7 and 15 Nov recordings every one of 93 damage rises read on screen had game sound at least
    13.4 dB above the game's median within the second before the previous read and the new one, while 49 % of
    random 2 s windows are that loud. A single read with no such sound between the read before it and itself is
    refused (`damageRefused: "quiet"`, no damage total); otherwise `damageSoundDb` says how loud it was.
    -> number refused."""
    refused = 0
    for g in games:
        o = g.get("own") or {}
        if not o.get("damageSingleRead") or o.get("damageAt") is None or not len(loud):
            continue
        t0, t1 = g.get("tStart", 0), g.get("tEndGame") or o.get("damageLastRead") or o["damageAt"]
        game = loud[int(t0 / LOUD_STEP):int(t1 / LOUD_STEP) + 1]
        lo = (o.get("damagePrevReadAt") or o["damageAt"] - 1) - 1
        win = loud[max(0, int(lo / LOUD_STEP)):int(o["damageAt"] / LOUD_STEP) + 1]
        if not len(game) or not len(win):
            continue
        db = float(win.max() - np.median(game))
        if db >= DAMAGE_QUIET_DB:
            o["damageSoundDb"] = round(db, 1)
            continue
        for k in ("damage", "damageAt", "damageLastRead", "damageSingleRead"):
            o.pop(k, None)
        o["damageRefused"] = "quiet"
        o["damageSoundDb"] = round(db, 1)
        refused += 1
    return refused


def guided_alarm(T, level, t: float) -> dict | None:
    """A weaker ring alarm right after a countdown end read on screen at t: {t, db} or None."""
    g = GUIDED
    m = (T >= t - g["before"]) & (T <= t + g["after"])
    if not m.any():
        return None
    x, tt = level[m], T[m]
    need = max(1, int(np.ceil(g["min_len"] * SR / HOP)))
    run = 0
    for i, v in enumerate(x > g["min_db"]):
        run = run + 1 if v else 0
        if run >= need:
            j = i - run + 1
            return {"t": round(float(tt[j]), 2), "db": round(float(x[j:].max()), 1)}
    return None


def screen_says_not_ring(rings: list[dict], t: float) -> str | None:
    """Why the ring clock read on screen says an alarm at t is not a ring starting to move, or None."""
    for r in rings:
        if r.get("endsAt") is None or r.get("firstSeen") is None:
            continue
        if r.get("kind") == "closing" and r["firstSeen"] - 1 <= t <= r["endsAt"] - 3:
            return "closing"                      # the ring was already moving
        if r.get("kind") == "wait" and r["firstSeen"] <= t <= r["endsAt"] - REJECT_WAIT:
            return "countdown"                    # the countdown was still running
    return None


def attach(games: list[dict], found: dict) -> None:
    """Match the sound cues with the rings read on screen, both ways:
    - an alarm within 2.5 s of a countdown end confirms it and sharpens its time (`soundAt`);
    - a countdown end without such an alarm gets a weaker one found right after it (`soundAt` + `soundWeak`);
    - an alarm heard while the screen shows no ring starting (closing, or countdown still running) is not a ring
      start: kept apart in `ringSoundOther` with the reason, not in `ringSound`;
    - other alarms of the game (map open, dead, spectating, …) stay in `ringSound`."""
    level = found.pop("_ring", None)
    loud = found.pop("_loud", None)
    if loud is not None:
        found["damageRefused"] = check_damage(games, loud)
    moves = [c["t"] for c in found.get("cues", {}).get("ring_moving", [])]
    starts = sorted(g.get("tStart", 0) for g in games)
    weak = 0
    for g in games:
        lo = g.get("tStart", 0)
        hi = next((s for s in starts if s > lo), 1e12)            # up to the next game
        used = set()
        rings = g.get("ring", [])
        for r in rings:
            if r.get("kind") != "wait" or r.get("endsAt") is None:
                continue
            near = [t for t in moves if abs(t - r["endsAt"]) <= 2.5]
            if near:
                t = min(near, key=lambda t: abs(t - r["endsAt"]))
                r["soundAt"] = t                                  # the alarm: when the ring really starts moving
                used.add(t)
            elif level is not None and len(level[0]):
                w = guided_alarm(level[0], level[1], r["endsAt"])
                if w:
                    r["soundAt"] = w["t"]; r["soundWeak"] = True; r["soundDb"] = w["db"]; weak += 1
        other, keep = [], []
        for t in moves:
            if not (lo <= t < hi) or t in used:
                continue
            why = screen_says_not_ring(rings, t)
            (other.append({"t": t, "why": why}) if why else keep.append(t))
        g["ringSound"] = keep              # heard during this game but not read on screen (map open, dead, spectating…)
        if other:
            g["ringSoundOther"] = other
    if level is not None:
        found["ringWeak"] = weak
