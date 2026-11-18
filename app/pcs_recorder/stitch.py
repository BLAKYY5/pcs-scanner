"""Short recordings and Apex crashes (coach's decision, 18 Nov 2026).

- A session shorter than `min_record_min` is not kept (no scan, no upload, no YouTube; its files go to the
  Recycle Bin), unless it belongs to a crash pair.
- Apex ended abnormally and was started again within RESTART_S: the next recording continues the first. Once that
  session ends the recordings are joined (ffmpeg concat, stream copy, below-normal priority, never while Apex runs)
  and the joined file is scanned. The originals go to the Recycle Bin only after the joined file was checked.
- When it can't be told for sure, every recording is kept as it is (never lose footage).

What "ended abnormally" means on a player's PC (read-only, nothing touches the game):
- Apex's own crash window `crashmsg.exe` (in the game's `bin` folder) is open when Apex closes;
- an Application event log entry about r5apex: Windows' "Application Error" (1000), "Windows Error Reporting"
  (1001), "Application Hang" (1002), or EA Javelin Anticheat's "The game r5apex_dx12.exe exited unexpectedly" (1000);
- or the last seconds of the recording still show the in-game HUD (ring clock or squads-left counter).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .settings import APP_DIR

log = logging.getLogger("recorder.stitch")
STATE_FILE = APP_DIR / "stitch.json"
RESTART_S = 600                 # Apex started again this soon after an abnormal end = the same session
CRASH_EXES = {"crashmsg.exe"}   # Apex's own crash window
JOIN_DIR = "PCS joining"        # joined file is written here (inside the recordings folder) until it is checked


def _quiet() -> int:
    return 0x08000000 | 0x00004000 if os.name == "nt" else 0          # no window, below-normal priority


# ---------------------------------------------------------------- state (kept apart from recordings.json)
def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    if not isinstance(st, dict):
        st = {}
    st.setdefault("holds", {})          # key -> {chain, until, kind, proof, mid, end, dur}
    st.setdefault("cont", {})           # recording in progress -> chain of recordings it continues
    st.setdefault("joins", [])          # chains waiting to be joined
    st.setdefault("keep", [])           # recordings never dropped as too short (part of a possible pair)
    return st


def save_state(st: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st, indent=1), encoding="utf-8")
    except OSError as ex:
        log.warning("stitch.json not saved: %s", ex)


# ---------------------------------------------------------------- crash signals (read-only)
def crash_window_open() -> bool:
    import psutil
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower() in CRASH_EXES:
                return True
        except psutil.Error:
            pass
    return False


def _utc(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(max(0.0, t)))


def parse_events(text: str) -> list[str]:
    """wevtutil's text output -> one line per event about Apex ("Source: description")."""
    out = []
    for block in re.split(r"(?m)^Event\[\d+\]", text or ""):
        if "r5apex" not in block.lower():
            continue
        src = re.search(r"(?m)^\s*Source:\s*(.+?)\s*$", block)
        desc = re.search(r"(?ms)^\s*Description:\s*\n?(.*)", block)
        first = next((ln.strip() for ln in (desc.group(1) if desc else "").splitlines() if ln.strip()), "")
        out.append(f"{src.group(1) if src else 'event log'}: {first}"[:200])
    return out


def apex_error_events(since: float, until: float) -> list[str]:
    """Application log entries about r5apex between two times (Windows only, read-only)."""
    if os.name != "nt":
        return []
    q = (f"*[System[(EventID=1000 or EventID=1001 or EventID=1002) and "
         f"TimeCreated[@SystemTime>='{_utc(since)}' and @SystemTime<='{_utc(until)}']]]")
    try:
        r = subprocess.run(["wevtutil", "qe", "Application", f"/q:{q}", "/f:text", "/c:50"], capture_output=True,
                           text=True, errors="replace", timeout=20, creationflags=_quiet())
    except (OSError, subprocess.SubprocessError) as ex:
        log.info("event log not read: %s", ex)
        return []
    return parse_events(r.stdout)


def crash_signal(since: float, until: float) -> str:
    """Why Apex looks like it crashed between these times, or "" (no sign)."""
    try:
        if crash_window_open():
            return "Apex crash window (crashmsg.exe)"
    except Exception as ex:
        log.info("process list not read: %s", ex)
    ev = apex_error_events(since, until)
    return ev[0] if ev else ""


# ---------------------------------------------------------------- the recording itself
def media_duration(path: str) -> float | None:
    """Seconds of video, or None when the file can't be read."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=60, creationflags=_quiet())
        d = float(r.stdout.strip())
        return d if d > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _size(path: str) -> tuple[int, int]:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=60,
                           creationflags=_quiet())
        w, h = r.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0, 0


def _hud():
    try:
        import pcs_hud
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scanner"))
        import pcs_hud
    return pcs_hud


def ended_in_game(path: str, dur: float | None, seconds: int = 12) -> bool | None:
    """Do the last seconds still show the in-game HUD (ring clock or squads left)? None = can't tell (unreadable,
    not 16:9). One frame a second, read like the scanner does, at below-normal priority."""
    if not dur or dur < 3:
        return None
    w, h = _size(path)
    if not w or not h or abs(w / h - 16 / 9) > 0.02:
        return None
    try:
        hud = _hud()
        import numpy as np
    except Exception as ex:
        log.info("HUD readers not available: %s", ex)
        return None
    W, H = 2560, 1440
    n = W * H * 3
    # keyframes only (OBS writes one a second): a dozen frames decoded, not 60 a second (a 1440p60 AV1 recording
    # took minutes of CPU when every frame was decoded)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-skip_frame", "nokey", "-ss", f"{max(0.0, dur - seconds):.3f}",
           "-i", str(path), "-fps_mode", "passthrough", "-vf", f"scale={W}:{H}", "-frames:v", str(seconds + 3),
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    frames = reads = 0
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_quiet())
    except OSError:
        return None
    try:
        while True:
            buf = p.stdout.read(n)
            if len(buf) < n:
                break
            frames += 1
            a = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
            try:
                if hud.read_ring(a) is not None or hud.SquadsLeft.read(a) is not None:
                    reads += 1
            except Exception:
                pass
    finally:
        try:
            p.stdout.close()
        except OSError:
            pass
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
    if not frames:
        return None
    return reads >= 2


# ---------------------------------------------------------------- joining
def join_paths(first: str) -> tuple[Path, Path]:
    """(temporary output, its folder) for a chain starting with this recording: in our own sub-folder, so no other
    recording's files (same name, other extension) ever match it."""
    p = Path(first)
    d = p.parent / JOIN_DIR
    return d / (p.stem + ".mp4"), d


def start_join(paths: list[str], out: Path) -> subprocess.Popen:
    """ffmpeg concat with stream copy (every track kept), at below-normal priority."""
    out.parent.mkdir(parents=True, exist_ok=True)
    lst = out.with_suffix(".txt")
    lst.write_text("".join("file '" + Path(p).as_posix().replace("'", "'\\''") + "'\n" for p in paths),
                   encoding="utf-8")
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
           "-map", "0", "-c", "copy", "-movflags", "+faststart", str(out)]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_quiet())


def check_joined(out: Path, expected: float) -> tuple[bool, str]:
    """The joined file is as long as its parts together (within 1 %, at least 3 s) and plays to its end."""
    d = media_duration(str(out))
    if d is None:
        return False, "the joined file can't be read"
    if abs(d - expected) > max(3.0, 0.01 * expected):
        return False, f"the joined file is {d:.0f} s long, the parts {expected:.0f} s"
    for at in (0.0, max(0.0, d - 3)):
        try:
            r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{at:.3f}", "-i", str(out), "-frames:v", "1",
                                "-f", "null", "-"], capture_output=True, timeout=120, creationflags=_quiet())
        except (OSError, subprocess.SubprocessError):
            return False, "the joined file can't be played"
        if r.returncode != 0:
            return False, "the joined file can't be played"
    return True, ""


def merged_events(paths: list[str], durations: list[float]) -> dict | None:
    """The parts' marks and mutes in one list, each part's times moved by the length of the parts before it."""
    data, t0 = None, 0.0
    for p, d in zip(paths, durations):
        try:
            ev = json.loads(Path(p).with_suffix(".events.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ev = None
        if isinstance(ev, dict):
            if data is None:
                data = dict(ev, events=[])
            for x in ev.get("events") or []:
                if isinstance(x, dict) and isinstance(x.get("t"), (int, float)):
                    data["events"].append(dict(x, t=round(x["t"] + t0, 2)))
        t0 += d
    if data is not None:
        data["joined"] = [{"file": Path(p).name, "offset": round(sum(durations[:i]), 3), "duration": round(d, 3)}
                          for i, (p, d) in enumerate(zip(paths, durations))]
    return data
