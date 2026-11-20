"""Review talk: the coach's microphone and voice app recorded during a Watch together session, on the coach's PC
only, so players can hear the review again over their own recording later.

How it records, and why OBS
- The voice app (Discord, TeamSpeak…) has to be recorded on its own, without the rest of the PC's sound (the
  session's YouTube videos play on this PC). Only Windows' per-program audio capture does that. ffmpeg can't
  (it has no per-program capture: dshow gives microphones, not a program's sound), so the app's own private OBS
  is used: it already captures exactly the voice app (track "discord") and the mic (track "mic") on separate
  tracks, with the devices chosen in Settings.
- It never touches an Apex recording: talk recording refuses to start while Apex runs or is being recorded, and
  the recorder stops it (and keeps what was said) before it starts recording a game.
- OBS always writes a video too: the recording goes to its own folder (`talk/` in the data folder, never the
  recordings folder), the two sound tracks are copied out (no re-encoding) and the temporary video is deleted.
- Only with the team's consent to comms recording (`comms_consent`), and only the tracks switched on in Settings.

Lining the talk up with game time: a session is paused and seeked while people talk, so the clock time of the
recording is kept with a timeline of the session's state (playing, game time t at hub time at, speed, game);
`segments()` turns them into [{a0, a1, g0, g1, speed, gameKey}] (audio a0..a1 = game time g0..g1; g0 == g1 =
talk while paused at that moment). The hub keeps them with the audio (`POST /api/session-audio`).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from . import hub
from .settings import APP_DIR

log = logging.getLogger("talk")
TALK_DIR = APP_DIR / "talk"
PENDING = TALK_DIR / "pending.json"
TRACK_KEYS = {"discord": "voice", "mic": "mic"}       # recorder track -> hub track
MIN_SEG_S = 0.3
TALK_FILENAME = "PCS talk %CCYY.%MM.%DD-%hh.%mm.%ss"


def segments(timeline: list[dict], t0: float, t1: float, offset: float) -> list[dict]:
    """timeline: [{"w": this PC's clock, "playing", "t", "at" (hub clock), "speed", "gameKey"}] in order; the
    recording ran from t0 to t1 (this PC's clock); offset = hub clock - this PC's clock."""
    out: list[dict] = []
    for i, e in enumerate(timeline):
        w0 = max(e["w"], t0)
        w1 = min(timeline[i + 1]["w"] if i + 1 < len(timeline) else t1, t1)
        if w1 - w0 < MIN_SEG_S:
            continue
        if e["playing"]:
            g0 = e["t"] + (w0 + offset - e["at"]) * e["speed"]
            g1 = g0 + (w1 - w0) * e["speed"]
        else:
            g0 = g1 = e["t"]
        seg = {"a0": round(w0 - t0, 3), "a1": round(w1 - t0, 3), "g0": round(g0, 3), "g1": round(g1, 3),
               "speed": e["speed"], "gameKey": e["gameKey"]}
        last = out[-1] if out else None
        if last and not e["playing"] and last["g0"] == last["g1"] == seg["g0"] and last["gameKey"] == seg["gameKey"] \
                and abs(last["a1"] - seg["a0"]) < 0.01:
            last["a1"] = seg["a1"]                     # still paused at the same moment: one segment
        else:
            out.append(seg)
    return out


class Talk:
    """Records the review talk of the session that is open in the coach's app (one at a time)."""

    def __init__(self, rec, clock=time.time):
        self.rec, self.clock = rec, clock         # pcs_recorder.core.Recorder (its OBS adapter and settings)
        self.active = False
        self.sid: str | None = None
        self.t0 = 0.0
        self.timeline: list[dict] = []
        self.offset = 0.0
        self.status = ""
        self.lock = threading.Lock()

    # ------------------------------------------------------------ what can be recorded
    def tracks(self) -> list[str]:
        s = self.rec.s
        return [t for t in s.tracks() if t in TRACK_KEYS]

    def why_not(self) -> str:
        """Why talk can't be recorded now ("" = it can)."""
        s = self.rec.s
        if not s.comms_consent:
            return "Needs your team's agreement to record comms (Settings → Recording)"
        if not self.tracks():
            return "Turn on the mic or the voice app track (Settings → Recording)"
        if self.rec.current is not None or self.rec.is_apex_running():
            return "Close Apex first: talk is never recorded while Apex runs"
        return ""

    def state(self) -> dict:
        return {"active": self.active, "sid": self.sid, "why": "" if self.active else self.why_not(),
                "tracks": [TRACK_KEYS[t] for t in self.tracks()], "status": self.status,
                "since": self.t0 if self.active else None}

    # ------------------------------------------------------------ start / stop
    def start(self, sid: str, st: dict | None) -> dict:
        with self.lock, self.rec.lock:
            if self.active:
                return self.state()
            why = self.why_not()
            if why:
                raise RuntimeError(why)
            obs = self.rec.obs
            obs.ensure_ready()
            c = obs.obs.c
            if c.get_record_status().output_active:
                raise RuntimeError("OBS is recording something else")
            TALK_DIR.mkdir(parents=True, exist_ok=True)
            c.set_record_directory(str(TALK_DIR))
            c.set_profile_parameter("Output", "FilenameFormatting", TALK_FILENAME)
            c.start_record()
            self.t0, self.sid, self.active, self.timeline, self.status = self.clock(), sid, True, [], "Recording talk"
            if st:
                self.event(st)
        log.info("review talk started")
        return self.state()

    def event(self, st: dict) -> None:
        """The session's state changed (from watch.Watch): kept to line the talk up with game time."""
        if not self.active or not st:
            return
        e = {"w": self.clock(), "playing": bool(st.get("playing")), "t": float(st.get("t") or 0),
             "at": float(st.get("at") or 0), "speed": float(st.get("speed") or 1), "gameKey": st.get("gameKey")}
        last = self.timeline[-1] if self.timeline else None
        if last and all(last[k] == e[k] for k in ("playing", "t", "at", "speed", "gameKey")):
            return
        self.timeline.append(e)

    def stop(self, offset: float | None = None, why: str = "") -> list[dict]:
        """Stop, copy the tracks out, queue them for the hub. Returns the pending uploads made.
        offset: hub clock - this PC's clock (the session's estimate; the last one known by default)."""
        offset = self.offset if offset is None else offset
        with self.lock, self.rec.lock:
            if not self.active:
                return []
            t1 = self.clock()
            obs = self.rec.obs
            c = obs.obs.c
            try:
                path = c.stop_record().output_path
            finally:
                self._restore(c)
                self.active = False
            log.info("review talk stopped (%s)", why or "by the coach")
        made = []
        try:
            video = self._final(path)
            segs = segments(self.timeline, self.t0, t1, offset)
            order = self.rec.s.tracks()
            for t in self.tracks():
                out = TALK_DIR / f"{self.sid}-{TRACK_KEYS[t]}.m4a"
                from .core import extract_track
                extract_track(str(video), order.index(t), out)
                made.append({"session": self.sid, "track": TRACK_KEYS[t], "file": str(out), "segments": segs})
            for p in {Path(path), video, Path(path).with_suffix(".mp4")}:
                if p.parent == TALK_DIR:                 # only the temporary video this made
                    p.unlink(missing_ok=True)
        except Exception as e:
            log.exception("review talk not saved")
            self.status = f"Review talk not saved: {e}"
            return []
        add_pending(made)
        self.status = "Review talk saved: sending it to the hub" if made else ""
        return made

    def _restore(self, c) -> None:
        """The game recording's own folder and file names, as before."""
        from .obs import FILENAME
        try:
            c.set_record_directory(str(self.rec.s.folder_path))
            c.set_profile_parameter("Output", "FilenameFormatting", FILENAME)
        except Exception:
            log.exception("OBS settings not restored: they are set again when Apex starts")
            self.rec.obs.ready = False               # prepare() runs again before the next game recording

    @staticmethod
    def _final(path: str, wait_s: float = 20) -> Path:
        """The file OBS wrote, once it stopped growing (the mkv; its mp4 copy may come later)."""
        p = Path(path)
        end, last = time.time() + wait_s, -1
        while time.time() < end:
            try:
                n = p.stat().st_size
            except OSError:
                n = -1
            if n > 0 and n == last:
                return p
            last = n
            time.sleep(0.5)
        if p.exists():
            return p
        raise RuntimeError("OBS didn't write the talk recording")


# ------------------------------------------------------------ to the hub (kept until the hub took them)

def load_pending() -> list[dict]:
    try:
        d = json.loads(PENDING.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def save_pending(items: list[dict]) -> None:
    TALK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PENDING.with_suffix(".part")
    tmp.write_text(json.dumps(items), encoding="utf-8")
    os.replace(tmp, PENDING)


def add_pending(items: list[dict]) -> None:
    keep = [p for p in load_pending() if (p["session"], p["track"]) not in {(i["session"], i["track"]) for i in items}]
    save_pending(keep + items)


REFUSED_CODES = (400, 403, 404, 413)


def send_pending(url: str, stop=None) -> int:
    """Upload the review talk waiting here; a file the hub took is deleted. Returns how many were sent. Files over
    50 MB go in parts (hub 0.6; resumable, `stop()` pauses between parts); a hub before 0.6 gets one request."""
    if not url or not hub.get_key():
        return 0
    items, sent = load_pending(), 0
    left = []
    for it in items:
        f = Path(it["file"])
        if not f.exists():
            continue
        if it.get("refused"):
            left.append(it)
            continue
        try:
            done = None
            if f.stat().st_size > hub.PART_FILE:      # big: in parts (Cloudflare's 100 MB limit), resumable
                done = hub.send_in_parts(url, "session-audio", f, {"session": it["session"], "track": it["track"],
                                                                    "segments": it["segments"]}, stop=stop)
            if done is None:                         # small, or a hub before 0.6: one request
                with open(f, "rb") as fh:
                    r = hub.request(url, "POST", "/api/session-audio", timeout=600,
                                    data={"session": it["session"], "track": it["track"],
                                          "segments": json.dumps(it["segments"])},
                                    files={"file": (f.name, fh, "audio/mp4")})
                if r.status_code in REFUSED_CODES:   # the hub won't take it: kept on this PC, not asked again
                    log.warning("hub refused review talk %s: %s", f.name, r.status_code)
                    it["refused"] = r.status_code
                    left.append(it)
                    continue
                hub._check(r)
            else:
                hub._forget_done(url, "session-audio", [f])
        except hub.HubError as e:
            if e.kind == "http" and e.status in REFUSED_CODES and e.code and e.code != "bad_checksum":
                log.warning("hub refused review talk %s: %s %s", f.name, e.status, e.code)
                it["refused"] = e.status
            else:
                log.info("review talk not sent yet: %s", e)
            left.append(it)
            continue
        f.unlink(missing_ok=True)
        sent += 1
    save_pending(left)
    return sent


def pending_count() -> int:
    return len([p for p in load_pending() if not p.get("refused")])
