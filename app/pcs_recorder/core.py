"""What the recorder does, independent of any window or tray icon (so it can be tested).

Every few seconds `tick()` looks at whether Apex is running:
- Apex starts      -> OBS records the Apex window.
- Apex closes      -> recording stops; the player is asked (with a countdown) to scan;
                      the scanner runs at low priority; results go to the hub.
- Apex starts again while a scan runs -> the scan is paused until Apex closes.
Nothing is uploaded while Apex is running, so the player's connection is never shared
with an upload mid-game. Old recordings are cleaned up after each session.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

from . import retention
from .settings import Settings

log = logging.getLogger("recorder")
APEX_EXES = {"r5apex_dx12.exe", "r5apex.exe"}


def apex_running() -> bool:
    import psutil
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower() in APEX_EXES:
                return True
        except psutil.Error:
            pass
    return False


class NeedsSignIn(ConnectionError):
    """Results couldn't be sent because the Google sign-in is missing or expired (message shows a Sign in button)."""
    action = "signin"


class Recorder:
    def __init__(self, s: Settings, obs, hub, ui, start_scan, is_apex_running=apex_running, clock=time.time):
        """start_scan(video_path, out_dir) -> subprocess.Popen running the scanner on that file."""
        self.s, self.obs, self.hub, self.ui = s, obs, hub, ui
        self.start_scan = start_scan
        self.is_apex_running, self.clock = is_apex_running, clock
        self.idx = retention.load_index()
        self.current: str | None = None          # index key of the recording in progress
        self.scan_proc: subprocess.Popen | None = None
        self.scan_key: str | None = None
        self.paused = False
        self.suspended: list = []                # processes this recorder paused (to resume exactly once)
        self.next_upload = 0.0
        self.queue: list[str] = []               # recordings waiting for the scanner (one scan at a time)
        self.status = "Waiting for Apex"
        self.lock = threading.RLock()
        self.youtube = None                      # youtube.Uploader, attached by the app
        self.night = None                        # night.Night (night uploads, turning the PC off)
        self.events: list[dict] = []             # marks and mutes in the recording in progress
        self.muted: dict[str, bool] = {}
        # after a crash or restart: a "recording" is just finished; unfinished scans start again
        for k in sorted(self.idx, key=lambda k: self.idx[k].started):
            e = self.idx[k]
            if e.status == "recording":
                e.status = "recorded"
            elif e.status in ("scanning", "waiting"):
                e.status = "waiting"
                self.queue.append(k)
        retention.save_index(self.idx)

    # ---------------------------------------------------------- main loop step
    def tick(self) -> None:
        with self.lock:
            apex = self.is_apex_running()
            if apex and self.current is None:
                self._start()
            elif not apex and self.current is not None:
                self._stop_and_offer_scan()
            self._manage_scan(apex)
            if self.scan_proc is None:
                self._next_in_queue(apex)
            if not apex and self.scan_proc is None:
                self._retry_uploads()
                if self.youtube:
                    try:
                        self.youtube.tick()
                    except Exception:
                        log.exception("youtube tick failed")
            if self.night:
                try:
                    self.night.tick(apex)
                except Exception:
                    log.exception("night tick failed")

    # ---------------------------------------------------------- recording
    def _start(self) -> None:
        try:
            self.obs.ensure_ready()
            self.obs.start()
        except Exception as e:
            self.status = f"Couldn't start recording: {e}"
            self.ui.notify("PCS Recorder", f"Recording didn't start: {e}")
            log.exception("start failed")
            return
        key = f"rec-{int(self.clock())}"
        n = 2
        while key in self.idx:                   # unique even if two sessions start within the same second
            key = f"rec-{int(self.clock())}-{n}"
            n += 1
        self.idx[key] = retention.Entry(path="", started=self.clock(), status="recording")
        self.current = key
        self.events, self.muted = [], {}
        retention.save_index(self.idx)
        self.status = "Recording Apex"
        self.hub.report(self._report(key))

    def _stop_and_offer_scan(self) -> None:
        key, self.current = self.current, None
        e = self.idx[key]
        try:
            path = self.obs.stop()
        except Exception as ex:
            path = None
            log.warning("stop failed: %s", ex)
        e.path = self._final_file(path) if path else ""
        if e.path:
            self._write_events(key)
        e.status = "recorded" if e.path else "failed"
        e.note = "" if e.path else "OBS didn't return a file"
        retention.save_index(self.idx)
        self.hub.report(self._report(key))
        self.status = "Waiting for Apex"
        if e.path:
            mins = (self.clock() - e.started) / 60
            size = retention.size_of(e) / 1e9
            scan = self.ui.ask_scan(mins, size, self.s.auto_scan, self.s.scan_delay_s)
            if scan:
                self._begin_scan(key)
            else:
                e.status = "skipped"
                retention.save_index(self.idx)
                self.hub.report(self._report(key))
        self.cleanup()

    def _final_file(self, path: str, wait_s: float = 90) -> str:
        """OBS records to .mkv and remuxes to .mp4 afterwards; use the .mp4 once it's complete."""
        mkv = Path(path)
        mp4 = mkv.with_suffix(".mp4")
        end = time.time() + wait_s
        last = -1
        while time.time() < end and mkv.suffix.lower() == ".mkv":
            if mp4.exists():
                size = mp4.stat().st_size
                if size == last and size > 0:
                    drop_original(mp4)
                    return str(mp4)
                last = size
            time.sleep(2)
        return str(mp4 if mp4.exists() else mkv)

    # ---------------------------------------------------------- hotkeys during a recording
    def hotkey(self, action: str) -> str | None:
        """'mark' or 'mute_game' / 'mute_discord' / 'mute_mic'. Returns what happened (for the beep), or None."""
        with self.lock:
            if self.current is None:
                return None
            e = self.idx[self.current]
            t = None
            try:
                t = self.obs.elapsed()
            except Exception:
                pass
            if t is None:
                t = self.clock() - e.started
            t = round(t, 2)
            if action == "mark":
                self.events.append({"t": t, "kind": "mark"})
                return "mark"
            if action.startswith("mute_"):
                track = action[5:]
                now = not self.muted.get(track, False)
                try:
                    if not self.obs.mute(track, now):
                        return None                      # that track isn't recorded
                except Exception as ex:
                    log.warning("mute failed: %s", ex)
                    return None
                self.muted[track] = now
                self.events.append({"t": t, "kind": "mute", "track": track, "muted": now})
                return "muted" if now else "unmuted"
            return None

    def _write_events(self, key: str) -> None:
        """Marks, mutes and the track layout, next to the recording (and sent with the scan results)."""
        import json
        e = self.idx[key]
        data = {"tracks": self.s.tracks(), "events": self.events, "player": self.s.player}
        try:
            Path(e.path).with_suffix(".events.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError as ex:
            log.warning("events not saved: %s", ex)

    # ---------------------------------------------------------- scanning
    def _begin_scan(self, key: str) -> None:
        e = self.idx[key]
        if self.scan_proc is not None:          # another session is being scanned: wait for it
            e.status = "waiting"
            if key not in self.queue:
                self.queue.append(key)
            retention.save_index(self.idx)
            return
        out = Path(e.path).with_suffix(".scan")
        out.mkdir(exist_ok=True)
        try:
            self.scan_proc = self.start_scan(e.path, str(out))
        except Exception as ex:
            e.status, e.note = "failed", f"scanner didn't start: {ex}"
            retention.save_index(self.idx)
            return
        self.scan_key = key
        e.status = "scanning"
        retention.save_index(self.idx)
        self.status = "Scanning last session"
        waiting = f" ({len(self.queue)} more after this one)" if self.queue else ""
        self.ui.notify("Scanning your session" + waiting,
                       "Usually a few minutes. You can keep using your PC; it pauses by itself if you start Apex.")

    def _manage_scan(self, apex: bool) -> None:
        if not self.scan_proc:
            return
        import psutil
        # The scanner splits a recording into parallel worker processes: pause/resume all of them.
        # On Windows pauses stack (paused twice = must be resumed twice), so each process is paused at
        # most once and exactly the processes that were paused get resumed. A worker that ends in the
        # meantime is simply skipped.
        if apex:
            # pause the scanner first (then it can't start new workers), then its workers; on later checks,
            # also catch a worker that was being started at that instant. Never the same process twice.
            try:
                root = psutil.Process(self.scan_proc.pid)
                done = {p.pid for p in self.suspended}

                def pause(p):
                    if p.pid in done:
                        return
                    try:
                        p.suspend()
                        self.suspended.append(p)
                        done.add(p.pid)
                    except psutil.Error:
                        pass                                         # ended in the meantime
                pause(root)                                          # frozen first: it can't start new workers,
                for p in root.children(recursive=True):              # so this list is complete
                    pause(p)
            except psutil.Error:
                pass
            self.paused = True
            self.status = "Scan paused while Apex runs"
        elif not apex and self.paused:
            self._resume_all()
            self.status = "Scanning last session"
        code = self.scan_proc.poll()
        if code is None:
            return
        self._resume_all()                      # nothing may stay paused once the scan is over
        key, self.scan_proc, self.scan_key = self.scan_key, None, None
        e = self.idx.get(key)
        if e:
            e.status = "scanned" if code == 0 else "failed"
            e.note = "" if code == 0 else f"scanner exit code {code}"
            retention.save_index(self.idx)
            if code == 0:
                self._add_extras(e)
                self.ui.notify("Scan finished", f"{summary(e)}. Sending the results…")
            else:
                self.ui.notify("Scan failed", f"The scanner stopped with an error (code {code}). The recording is kept; "
                               f"details in {Path(e.path).with_suffix('.scan') / 'scan.log'}", sticky=True)
        self.next_upload = 0
        self.status = "Waiting for Apex"
        self._next_in_queue(apex)

    def _add_extras(self, e) -> None:
        """What goes to the team with the scan results besides the scanner's files: the marks/mutes and,
        when the team agreed and the player allows it, the microphone track (for the comms review)."""
        import json
        import shutil
        out = Path(e.path).with_suffix(".scan")
        ev = Path(e.path).with_suffix(".events.json")
        try:
            if ev.exists():
                shutil.copy(ev, out / ev.name)
                tracks = json.loads(ev.read_text(encoding="utf-8")).get("tracks", [])
                if self.s.comms_consent and self.s.send_mic and "mic" in tracks:
                    extract_track(e.path, tracks.index("mic"), out / (Path(e.path).stem + ".mic.m4a"))
        except Exception as ex:
            log.warning("extras not added: %s", ex)

    def _resume_all(self) -> None:
        import psutil
        for p in self.suspended:
            try:
                p.resume()
            except psutil.Error:
                pass
        self.suspended = []
        self.paused = False

    def _next_in_queue(self, apex: bool) -> None:
        while self.queue and self.scan_proc is None and not apex:
            k = self.queue.pop(0)
            if k in self.idx:
                self._begin_scan(k)

    # ---------------------------------------------------------- upload
    def _retry_uploads(self) -> None:
        if self.clock() < self.next_upload:          # after a failure, try again once a minute
            return
        for key, e in list(self.idx.items()):
            if e.status != "scanned":
                continue
            out = Path(e.path).with_suffix(".scan")
            files = sorted(p for p in out.rglob("*") if p.is_file()) if out.exists() else []
            try:
                where = self.hub.upload(files)
            except Exception as ex:
                log.info("results not sent, will retry: %s", ex)
                self.next_upload = self.clock() + 60
                if not e.note.startswith("not sent"):    # tell the player once, not every minute
                    e.note = f"not sent yet: {ex}"
                    retention.save_index(self.idx)
                    self.ui.notify("Results not sent yet", f"{ex}. They are kept and sent automatically as soon as "
                                   "it works.", sticky=True, action=getattr(ex, "action", None))
                return
            e.status, e.note = "uploaded", ""
            retention.save_index(self.idx)
            self.hub.report(self._report(key))
            to = " and ".join(where) if where else "the team"
            self.ui.notify("Results sent", f"{summary(e)}: sent to {to}. Nothing else to do.")

    # ---------------------------------------------------------- housekeeping
    def cleanup(self) -> list[tuple[str, str]]:
        with self.lock:
            for e in self.idx.values():              # .mkv originals left by an earlier version or a locked file
                if e.path.lower().endswith(".mp4"):
                    drop_original(Path(e.path))
            protect = {k for k in (self.current, self.scan_key) if k}
            if self.youtube:
                protect |= self.youtube.protected()       # blocks still going to YouTube
            removed = retention.enforce(self.idx, self.s.keep_days, self.s.max_gb, protect)
            retention.save_index(self.idx)
        if removed:
            self.ui.notify("PCS Recorder", f"Removed {len(removed)} old recording(s) to stay within your limits.")
        return removed

    def usage(self) -> tuple[int, int]:
        return retention.folder_usage(self.idx), len(self.idx)

    def _report(self, key: str) -> dict:
        e = self.idx[key]
        return {"id": f"{self.s.player or 'player'}-{key}", "player": self.s.player, "file": Path(e.path).name if e.path else "",
                "started": e.started, "status": e.status, "note": e.note,
                "size": retention.size_of(e) if e.path else 0, "keep_days": self.s.keep_days, "max_gb": self.s.max_gb}


def drop_original(mp4: Path) -> bool:
    """OBS remuxes the .mkv it records into an .mp4 and keeps both. Once the .mp4 is complete
    (at least as large as the .mkv, give or take container overhead) the .mkv is just a duplicate."""
    mkv = mp4.with_suffix(".mkv")
    try:
        if mkv.exists() and mp4.exists() and mp4.stat().st_size >= 0.98 * mkv.stat().st_size:
            mkv.unlink()
            return True
    except OSError:
        pass                                          # still open somewhere: try again at the next clean-up
    return False


def extract_track(video: str, index: int, out: Path) -> Path:
    """Copy one audio track out of a recording (no re-encoding: a few seconds for an hour)."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(video), "-map", f"0:a:{index}", "-vn",
           "-c:a", "copy", str(out)]
    subprocess.run(cmd, check=True, timeout=600, capture_output=True, creationflags=_low_priority())
    return out


MAP_NAMES = {"WE": "World's Edge", "SP": "Storm Point", "ED": "E-District", "OL": "Olympus",
             "BM": "Broken Moon", "KC": "Kings Canyon"}


def summary(e) -> str:
    """'2 games: Olympus, World's Edge' from the scanner's result file, or a plain fallback."""
    import json
    try:
        f = Path(e.path).with_suffix(".scan") / (Path(e.path).stem + ".scan.json")
        games = json.loads(f.read_text(encoding="utf-8")).get("games", [])
        maps = [MAP_NAMES.get(g.get("map"), g.get("map") or "?") for g in games]
        if not games:
            return "No game found in this recording"
        return f"{len(games)} game{'s' if len(games) > 1 else ''}: {', '.join(maps)}"
    except Exception:
        return "Your session"


def _low_priority() -> int:
    BELOW_NORMAL, NO_WINDOW = 0x00004000, 0x08000000
    import os
    return BELOW_NORMAL | NO_WINDOW if os.name == "nt" else 0
