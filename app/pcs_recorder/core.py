"""What the recorder does, independent of any window or tray icon (so it can be tested).

Every few seconds `tick()` looks at whether Apex is running:
- Apex starts      -> OBS records the Apex window.
- Apex closes      -> recording stops; the player is asked (with a countdown) to scan;
                      the scanner runs at low priority; results go to the hub.
- Apex starts again while a scan runs -> the scan is stopped (its memory freed) and goes on from where it was
                      when Apex closes (finished chunks are kept on disk by the scanner).
Nothing is uploaded while Apex is running, so the player's connection is never shared
with an upload mid-game. Old recordings are cleaned up after each session.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

from . import crash, retention
from .settings import Settings

log = logging.getLogger("recorder")
RECORDING_CHECK_S = 30          # after a recording starts, OBS must say it records (else a crash report)
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


def stop_tree(proc, wait_s: float = 10) -> None:
    """Stop a scan and every process it started (worker processes, their ffmpeg). Everything is paused first, so
    nothing can start a new worker while the rest is ended. In the exe, the top process only unpacks the app to a
    temporary folder and waits: it is let go once the others are gone, so it removes that folder itself, as at
    the end of a normal scan. Run from source, the top process is the scanner itself and is ended too."""
    import sys
    import psutil
    try:
        root = psutil.Process(proc.pid)
    except psutil.Error:
        return
    try:
        root.suspend()                                   # the top first, then its children: the list is complete
    except psutil.Error:
        pass
    try:
        kids = root.children(recursive=True)
    except psutil.Error:
        kids = []
    for p in kids:
        try:
            p.suspend()
        except psutil.Error:
            pass
    for p in kids:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(kids, timeout=wait_s)
    onefile = getattr(sys, "frozen", False) and Path(getattr(sys, "_MEIPASS", "")).name.startswith("_MEI")
    if onefile:
        try:
            root.resume()                               # the unpacker: sees its app end, cleans up, exits
            root.wait(timeout=wait_s)
        except psutil.TimeoutExpired:
            pass
        except psutil.Error:
            pass
    try:
        if root.is_running():
            root.kill()
            root.wait(timeout=wait_s)
    except psutil.Error:
        pass
    try:
        proc.wait(timeout=wait_s)                        # reaps it for subprocess too
    except Exception:
        pass


def result_files(out: Path) -> list[Path]:
    """The scan's files to send: everything in the results folder except an unfinished scan's kept chunks."""
    if not out.exists():
        return []
    return sorted(p for p in out.rglob("*") if p.is_file() and not any(q.suffix == ".partial" for q in p.relative_to(out).parents))


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
        self.paused = False                      # a scan was stopped because Apex runs (goes on when it closes)
        self.resumed: set[str] = set()           # recordings whose scan was stopped for Apex (no new message)
        self.next_upload = 0.0
        self.queue: list[str] = []               # recordings waiting for the scanner (one scan at a time)
        self.status = "Waiting for Apex"
        self.apex = False                        # Apex running at the last tick()
        self.lock = threading.RLock()
        self.youtube = None                      # youtube.Uploader, attached by the app
        self.talk = None                         # talk.Talk (review talk of a Watch together session), by the app
        self.night = None                        # night.Night (night uploads, turning the PC off)
        self.events: list[dict] = []             # marks and mutes in the recording in progress
        self.muted: dict[str, bool] = {}
        self.sent = retention.load_sent(self.idx, s.hub_url, clock())   # hub / Drive, per recording
        self.hub_next, self.hub_fails, self.hub_fail_url = 0.0, 0, ""   # hub retries: backoff after network errors
        self.track_db = None                     # loudness of one audio track (tests); default pcs_levels.track_db
        self.silence_thread: threading.Thread | None = None
        self.cutter = None                       # audio tracks for the review (tests); default tracks.CUTTER
        # after a crash or restart: a "recording" is just finished; unfinished scans start again
        for k in sorted(self.idx, key=lambda k: self.idx[k].started):
            e = self.idx[k]
            if e.status == "recording":
                e.status = "recorded"
            elif e.status in ("scanning", "waiting"):
                e.status = "waiting"
                self.queue.append(k)
            if e.nogame and not no_game(e):          # flagged by an older, less careful version: keep it
                e.nogame = False
        retention.save_index(self.idx)

    # ---------------------------------------------------------- main loop step
    def tick(self) -> None:
        with self.lock:
            apex = self.is_apex_running()
            self.apex = apex                     # read by the window (it polls less often during games)
            if apex and self.talk is not None and self.talk.active:
                try:                                 # the game recording comes first: keep what was said, stop
                    self.talk.stop(why="Apex started")
                except Exception:
                    log.exception("review talk stop failed")
            if apex and self.current is None:
                self._start()
            elif not apex and self.current is not None:
                self._stop_and_offer_scan()
            elif apex and self.current is not None:
                self._check_recording()
            self._manage_scan(apex)
            if self.scan_proc is None:
                self._next_in_queue(apex)
            if not apex and self.scan_proc is None:
                self._retry_uploads()
                self._retry_hub()
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
            crash.report_exception("record_start", e, crash.obs_log_tail())
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
        self.rec_check_at = self.clock() + RECORDING_CHECK_S
        self.hub.report(self._report(key))

    def _check_recording(self) -> None:
        """Once, a little after the start: is OBS really recording? (A recording can "start" and never run: OBS
        log ending before the recording starts, another capture program holding the GPU encoder.) -> crash report."""
        at = getattr(self, "rec_check_at", 0)
        if not at or self.clock() < at:
            return
        self.rec_check_at = 0
        elapsed = getattr(self.obs, "elapsed", None)
        try:
            running = elapsed() if elapsed else True
        except Exception:
            running = None
        if running is None:
            log.warning("OBS isn't recording %d s after the start", RECORDING_CHECK_S)
            crash.report("record_start", f"OBS isn't recording {RECORDING_CHECK_S} s after the start",
                         "", crash.obs_log_tail())

    def _stop_and_offer_scan(self) -> None:
        key, self.current = self.current, None
        e = self.idx[key]
        stop_error = None
        try:
            path = self.obs.stop()
        except Exception as ex:
            path, stop_error = None, ex
            log.warning("stop failed: %s", ex)
        e.path = self._final_file(path) if path else ""
        if not e.path:                           # a session with no file: tell the coach why (crash report)
            mins = round((self.clock() - e.started) / 60, 1)
            if stop_error is not None:
                crash.report_exception("record_stop", stop_error, crash.obs_log_tail(), {"minutes": mins})
            else:
                crash.report("record_stop", "No recording file when Apex closed" + ("" if path else
                             ": OBS wasn't recording"), f"OBS gave {Path(path).name if path else 'no file'}",
                             crash.obs_log_tail(), {"minutes": mins})
        if e.path:
            self._write_events(key)
            self._start_silence_check(e.path, self.clock() - e.started)
            self._queue_tracks(e.path)
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

    def _final_file(self, path: str, wait_s: float | None = None, poll: float = 3) -> str:
        """OBS records to .mkv and copies it into an .mp4 afterwards (2 min for a 2-hour session on a hard disk).
        Use the .mp4 only once it's complete: about as large as the .mkv and no longer growing. Until then,
        and if that takes too long, the .mkv is the complete recording (a half-written .mp4 can't be read:
        the scan of a 2-hour session once found nothing because of that)."""
        mkv = Path(path)
        if mkv.suffix.lower() != ".mkv":
            return str(mkv)
        mp4 = mkv.with_suffix(".mp4")
        try:
            gb = mkv.stat().st_size / 1e9
        except OSError:
            gb = 0
        end = time.time() + (wait_s if wait_s is not None else 120 + 60 * gb)     # ~1 min per GB on a slow disk
        last = -1
        while time.time() < end:
            if mp4.exists():
                size = mp4.stat().st_size
                complete = mkv.exists() and size >= 0.98 * mkv.stat().st_size
                if complete and size == last:
                    drop_original(mp4)
                    return str(mp4)
                last = size
            time.sleep(poll)
        return str(mkv if mkv.exists() else mp4)

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
        if "discord" in data["tracks"]:
            data["voice"] = self.s.voice_name()          # the voice app on that track: Discord, TeamSpeak…
        try:
            Path(e.path).with_suffix(".events.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError as ex:
            log.warning("events not saved: %s", ex)

    # ---------------------------------------------------------- scanning
    def _begin_scan(self, key: str, queued: bool = False) -> None:
        e = self.idx[key]
        # another session is being scanned, or others wait before this one (a scan stopped while Apex ran): wait
        if self.scan_proc is not None or (not queued and any(k != key for k in self.queue)):
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
            crash.report_exception("scan_failed", ex)
            return
        self.scan_key = key
        e.status = "scanning"
        retention.save_index(self.idx)
        self.status = "Scanning last session"
        if key in self.resumed:                 # goes on after Apex closed: the player was told when it started
            self.resumed.discard(key)
            return
        waiting = f" ({len(self.queue)} more after this one)" if self.queue else ""
        self.ui.notify("Scanning your session" + waiting,
                       "Usually a few minutes. You can keep using your PC; it pauses by itself if you start Apex.")

    def _manage_scan(self, apex: bool) -> None:
        # Apex starts while a scan runs: the scan is stopped, not paused. A paused scan kept its memory (up to ~5 GB
        # with 8 workers) for the whole game, and Windows pushed it to disk while Apex wanted the RAM. The scanner
        # keeps each finished chunk next to its results, so when Apex closes the scan starts again and goes on
        # from the chunks still to do (same results as one uninterrupted scan).
        if apex and self.scan_proc is not None and self.scan_proc.poll() is None:
            key = self.scan_key
            stop_tree(self.scan_proc)
            log.info("scan stopped while Apex runs; it goes on when Apex closes")
            self.scan_proc, self.scan_key = None, None
            if key in self.idx:
                self.idx[key].status = "waiting"
                if key in self.queue:
                    self.queue.remove(key)
                self.queue.insert(0, key)               # first in line when Apex closes
                self.resumed.add(key)
                retention.save_index(self.idx)
            self.paused = True
        if self.paused:
            if apex:
                self.status = "Scan paused while Apex runs"
                return
            self.paused = False
            self.status = "Scanning last session"
            self._next_in_queue(apex)
        if not self.scan_proc:
            return
        code = self.scan_proc.poll()
        if code is None:
            return
        key, self.scan_proc, self.scan_key = self.scan_key, None, None
        e = self.idx.get(key)
        if e:
            nothing = code == 0 and read_nothing(e)      # "finished" without seeing a single frame
            if nothing:
                code = 3
            e.status = "scanned" if code == 0 else "failed"
            e.note = "" if code == 0 else ("the scan couldn't read the video" if nothing else f"scanner exit code {code}")
            retention.save_index(self.idx)
            if code != 0:
                scanlog = Path(e.path).with_suffix(".scan") / "scan.log" if e.path else None
                crash.report("scan_failed", f"Scan failed: {e.note}", f"recording {Path(e.path).name if e.path else '?'}",
                             [("scan.log", crash.tail(scanlog, 150))] if scanlog else [],
                             {"exit_code": code, "read_nothing": nothing})
            if code == 0:
                self._add_extras(e)
                e.nogame = no_game(e)
                retention.save_index(self.idx)
                if e.nogame:
                    when = ("now" if self.s.nogame_keep_h == 0 else f"in {self.s.nogame_keep_h} h")
                    self.ui.notify("No game in this recording", "Only menus or lobby were recorded: it's deleted "
                                   f"{when} (and first, if space is needed).")
                    if self.s.nogame_keep_h == 0:
                        self.cleanup()
                else:
                    self.ui.notify("Scan finished", f"{summary(e)}. Sending the results…")
            elif nothing:
                self.ui.notify("Scan failed", "The scan couldn't read this recording. It's kept: press Scan again "
                               "on it in Recordings.", sticky=True)
            else:
                self.ui.notify("Scan failed", f"The scanner stopped with an error (code {code}). The recording is kept; "
                               f"details in {Path(e.path).with_suffix('.scan') / 'scan.log'}", sticky=True)
        self.next_upload = 0
        self.status = "Waiting for Apex"
        self._next_in_queue(apex)

    def rescan(self, key: str) -> bool:
        """Scan one of the app's recordings again (after a failed or empty scan). Runs like the automatic scan."""
        with self.lock:
            e = self.idx.get(key)
            if not e or not e.path or key in (self.current, self.scan_key) or key in self.queue:
                return False
            mkv, mp4 = Path(e.path).with_suffix(".mkv"), Path(e.path).with_suffix(".mp4")
            if mkv.exists() and mp4.exists():
                if mp4.stat().st_size >= 0.98 * mkv.stat().st_size:
                    drop_original(mp4)
                    e.path = str(mp4)
                else:
                    e.path = str(mkv)                    # the .mp4 is incomplete: the .mkv is the whole recording
            e.status, e.note, e.nogame = "waiting", "", False
            self.queue.append(key)
            retention.save_index(self.idx)
            return True

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

    def _next_in_queue(self, apex: bool) -> None:
        while self.queue and self.scan_proc is None and not apex:
            k = self.queue.pop(0)
            if k in self.idx:
                self._begin_scan(k, queued=True)

    # ---------------------------------------------------------- upload
    def _retry_uploads(self) -> None:
        if self.clock() < self.next_upload:          # after a failure, try again once a minute
            return
        for key, e in list(self.idx.items()):
            if e.status != "scanned":
                continue
            out = Path(e.path).with_suffix(".scan")
            files = result_files(out)
            try:
                where = self.hub.upload(files)
            except Exception as ex:
                log.info("results not sent, will retry: %s", ex)
                self.next_upload = self.clock() + 60
                import re
                note = f"not sent yet: {ex}"
                # tell the player once per reason, not every minute (a new reason, e.g. "key refused", is told)
                if re.sub(r"\d+", "", e.note) != re.sub(r"\d+", "", note):
                    e.note = note
                    retention.save_index(self.idx)
                    self.ui.notify("Results not sent yet", f"{ex}. They are kept and sent automatically as soon "
                                   "as it works.", sticky=True, action=getattr(ex, "action", None))
                return
            hub_note = getattr(where, "hub_note", "")          # what the hub answered: "Sent to hub: 11 games"
            e.status, e.note = "uploaded", hub_note
            retention.save_index(self.idx)
            hub_ok, drive_ok = getattr(where, "hub_ok", None), getattr(where, "drive_ok", None)
            self.sent[key] = {"hub": {True: "sent", False: "pending"}.get(hub_ok, ""),
                              "why": getattr(where, "hub_why", "") if hub_ok is False else "",
                              "drive": {True: "sent", False: "failed"}.get(drive_ok, "")}
            self._save_sent()
            self.hub.report(self._report(key))
            to = " and ".join(where) if where else "the team"
            later = " It goes to the hub by itself as soon as it works." if hub_ok is False else " Nothing else to do."
            self.ui.notify("Results sent", f"{summary(e)}: sent to {to}." + (f" {hub_note}." if hub_note else "")
                           + later)

    def _save_sent(self) -> None:
        try:
            retention.save_sent(self.sent, self.idx)
        except OSError as ex:
            log.warning("sent.json not saved: %s", ex)

    def hub_state(self, key: str) -> dict:
        """{"hub": "sent" | "pending" | "", "why": ...} for the Recordings list."""
        r = self.sent.get(key) or {}
        return {"hub": r.get("hub", ""), "why": r.get("why", "")}

    # hub tries after a network error: 1, 2, 4, 8 min, then every 15 min (a hub address or key change: at once)
    HUB_BACKOFF = (60, 120, 240, 480, 900)

    def _retry_hub(self) -> None:
        """Recordings whose results reached Drive but not the hub: sent to the hub on their own schedule until it
        takes them. Never while Apex runs (tick), never twice once the hub said 200. A refused key waits for a new
        key or address; 429 waits for Retry-After (both known by pcs_recorder.hub without asking the hub)."""
        upload_hub = getattr(self.hub, "upload_hub", None)
        pending = [k for k, r in self.sent.items() if r.get("hub") == "pending" and k in self.idx
                   and self.idx[k].status == "uploaded" and self.idx[k].path]
        if not pending or upload_hub is None:
            return
        blocked = self.hub.hub_blocked() if hasattr(self.hub, "hub_blocked") else ""
        if blocked:
            self._hub_why(pending, blocked)
            return
        if self.s.hub_url != self.hub_fail_url:                  # another hub address: try it now
            self.hub_next, self.hub_fails = 0.0, 0
        if self.clock() < self.hub_next:
            return
        sent = []
        for key in sorted(pending, key=lambda k: self.idx[k].started):
            e = self.idx[key]
            out = Path(e.path).with_suffix(".scan")
            files = result_files(out)
            if not files:                                        # results deleted by hand: nothing to send
                self.sent[key]["hub"], self.sent[key]["why"] = "", ""
                self._save_sent()
                continue
            try:
                note = upload_hub(files)
            except Exception as ex:
                from .hub import reason
                why = reason(ex)
                log.info("hub retry failed (%s): %s", why, ex)
                if getattr(ex, "kind", "network") in ("network", "http"):
                    self.hub_fails += 1
                    self.hub_next = self.clock() + self.HUB_BACKOFF[min(self.hub_fails, len(self.HUB_BACKOFF)) - 1]
                    self.hub_fail_url = self.s.hub_url
                self._hub_why([k for k in pending if self.sent[k].get("hub") == "pending"], why)
                break
            self.hub_fails, self.hub_next = 0, 0.0
            self.sent[key].update(hub="sent", why="")
            e.note = note or "Sent to hub"
            retention.save_index(self.idx)
            self._save_sent()
            sent.append(key)
            self.hub.report(self._report(key))
        if sent:
            what = summary(self.idx[sent[0]]) if len(sent) == 1 else f"{len(sent)} recordings"
            self.ui.notify("Sent to hub", f"{what}: now on the team hub.")

    def _hub_why(self, keys: list[str], why: str) -> None:
        changed = False
        for k in keys:
            if k in self.sent and self.sent[k].get("why") != why:
                self.sent[k]["why"] = why
                changed = True
        if changed:
            self._save_sent()

    # ---------------------------------------------------------- silent voice / mic track
    SILENT_DB = -60.0          # loudest half second below this = nothing recorded (level 0 in the review player)
    SILENT_MIN_S = 300         # shorter recordings: not checked (a quick restart says nothing)

    def _start_silence_check(self, path: str, seconds: float) -> None:
        """After a recording: was the voice app's or the mic's track silent from start to end? Runs beside the
        scan prompt (reads the audio track only, never the video)."""
        tracks = self.s.tracks()
        check = [t for t in ("discord", "mic") if t in tracks
                 and not any(ev.get("kind") == "mute" and ev.get("track") == t for ev in self.events)]
        if not check or seconds < self.SILENT_MIN_S or not self.s.comms_consent:
            return
        sigs = {"discord": f"{self.s.voice_app}|{self.s.voice_exe}", "mic": self.s.mic_device or "default"}
        names = {"discord": self.s.voice_name()}
        self.silence_thread = threading.Thread(
            target=self._check_silence, args=(path, tracks, check, sigs, names), daemon=True, name="silence")
        self.silence_thread.start()

    def _check_silence(self, path: str, tracks: list[str], check: list[str], sigs: dict, names: dict) -> None:
        import json
        from .settings import APP_DIR
        f = APP_DIR / "silence.json"           # {track: settings it was found silent with}: told once per setting
        try:
            told = json.loads(f.read_text(encoding="utf-8"))
            told = told if isinstance(told, dict) else {}
        except (OSError, ValueError):
            told = {}
        track_db = self.track_db
        if track_db is None:
            from pcs_levels import track_db
        before = dict(told)
        for t in check:
            try:
                db = track_db(path, tracks.index(t))
            except Exception as ex:
                log.info("silence check skipped (%s): %s", t, ex)
                continue
            if not len(db):
                continue
            silent = float(max(db)) < self.SILENT_DB
            log.info("track %s: loudest %.0f dBFS", t, float(max(db)))
            if not silent:
                told.pop(t, None)                # sound again: a later silence is told again
            elif told.get(t) != sigs[t]:
                told[t] = sigs[t]
                if t == "discord":
                    self.ui.notify("Voice track was silent", f"Nothing was recorded from {names['discord']}. "
                                   "Check Settings → Voice app.")
                else:
                    self.ui.notify("Mic track was silent", "Nothing was recorded from your microphone. "
                                   "Check Settings → Microphone.")
        if told != before:
            try:
                f.write_text(json.dumps(told), encoding="utf-8")
            except OSError:
                pass

    # ---------------------------------------------------------- audio tracks for the review player
    TRACKS_RESUME_S = 24 * 3600    # at start: recordings this recent whose tracks aren't cut yet are queued again

    def _cutter(self):
        if self.cutter is None:
            from .tracks import CUTTER
            self.cutter = CUTTER
        return self.cutter

    def _queue_tracks(self, path: str) -> None:
        """The review plays game, voice and mic as separate files: cut them now, in the background (low priority,
        never while Apex runs), so they are ready when the review opens. Only with 2 tracks or more."""
        if len(self.s.tracks()) < 2:
            return
        try:
            self._cutter().add(path)
        except Exception:
            log.exception("track cut not queued")

    def resume_tracks(self) -> None:
        """At start: remove what a cut stopped halfway left behind, then queue recent recordings without tracks."""
        from .tracks import track_path, fresh
        c = self._cutter()
        with self.lock:
            entries = [e for e in self.idx.values() if e.path and e.status not in ("recording", "failed")]
        try:
            c.clean({str(Path(e.path).parent) for e in entries} | {self.s.folder})
        except Exception:
            log.exception("track clean-up failed")
        if len(self.s.tracks()) < 2:
            return
        for e in sorted(entries, key=lambda e: -e.started):
            if self.clock() - e.started < self.TRACKS_RESUME_S and Path(e.path).exists() \
                    and not fresh(track_path(e.path, 0), e.path):
                c.add(e.path)

    # ---------------------------------------------------------- housekeeping
    def cleanup(self) -> list[tuple[str, str]]:
        with self.lock:
            for e in self.idx.values():              # .mkv originals left by an earlier version or a locked file
                if e.path.lower().endswith(".mp4"):
                    drop_original(Path(e.path))
            protect = {k for k in (self.current, self.scan_key) if k}
            if self.youtube:
                protect |= self.youtube.protected()       # blocks still going to YouTube
            removed = retention.enforce(self.idx, self.s.keep_days, self.s.max_gb, protect,
                                        nogame_keep_h=self.s.nogame_keep_h)
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


def _scan_json(e) -> dict | None:
    import json
    try:
        f = Path(e.path).with_suffix(".scan") / (Path(e.path).stem + ".scan.json")
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def read_nothing(e) -> bool:
    """The scanner wrote a result but never looked at a frame (unreadable or half-written file)."""
    d = _scan_json(e)
    if d is None:
        return False
    votes = sum(((d.get("perf") or {}).get("layoutVotes") or {}).values())
    return not d.get("games") and votes == 0


def no_game(e) -> bool:
    """True only when the scan is sure nothing was played: it read the video (frames were looked at), found
    no game (no drop map, no ring, no HUD) and not a single kill-feed line. Anything doubtful counts as a game,
    so a real game is never deleted early."""
    d = _scan_json(e)
    if d is None or read_nothing(e):
        return False
    return not d.get("games") and int((d.get("perf") or {}).get("feedLines") or 0) == 0


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
