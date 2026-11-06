"""Night mode: YouTube uploads wait for the night while nobody uses the PC, and (separate switch) the PC
turns itself off once everything is sent.

- Uploads start only inside the night window (default 01:00-07:00) after `night_idle_min` minutes without
  mouse or keyboard input, and never while Apex runs.
- While a block waits for the night and while it uploads, Windows is kept from going to sleep (allowed
  again afterwards), so a PC left on in the evening is still on at night. A PC that is already asleep or
  turned off can't upload.
- Turning off: only after a night in which something was uploaded, when nothing is left to do (no recording,
  scan, result or YouTube upload waiting), the PC is still idle and Apex is closed. Windows shows its own
  1-minute warning, a corner message has "Cancel", and touching the mouse or keyboard cancels it too.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import logging
import os
import subprocess
import time

log = logging.getLogger("recorder.night")
SHUTDOWN_DELAY_S = 60


def idle_seconds() -> float:
    """Seconds since the last mouse or keyboard input on this PC (Windows), 0 elsewhere."""
    if os.name != "nt":
        return 0.0

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(li)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
        return 0.0
    return ((ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000


def keep_awake(on: bool) -> None:
    if os.name != "nt":
        return
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, creationflags=0x08000000 if os.name == "nt" else 0, timeout=30)


def in_window(now: dt.datetime, start: int, end: int) -> bool:
    h = now.hour
    return start <= h < end if start < end else (h >= start or h < end)     # e.g. 23 -> 7 crosses midnight


class Night:
    def __init__(self, s, rec, ui, idle=idle_seconds, clock=time.time, runner=run, awake=keep_awake):
        self.s, self.rec, self.ui = s, rec, ui
        self.idle, self.clock, self.run, self.awake = idle, clock, runner, awake
        self.worked_night: str | None = None      # which night something was uploaded
        self.cancelled_night: str | None = None   # the player cancelled the shutdown that night
        self.shutdown_at: float | None = None
        self.awake_on = False
        self.busy = False                         # a night upload is running
        self.waiting = False                      # a block waits for the night: the PC must not fall asleep
        self.next_check = 0.0

    def night_id(self) -> str:
        """Nights are named by the date they started (01:00 on the 7th belongs to the night of the 6th)."""
        now = dt.datetime.fromtimestamp(self.clock())
        return (now - dt.timedelta(hours=12)).strftime("%Y-%m-%d")

    def active(self) -> bool:
        return in_window(dt.datetime.fromtimestamp(self.clock()), self.s.night_from, self.s.night_to)

    def idle_enough(self) -> bool:
        return self.idle() >= self.s.night_idle_min * 60

    # ---------------------------------------------------------- asked by the YouTube uploader
    def upload_allowed(self) -> bool:
        if self.s.yt_when != "night":
            return True
        return self.active() and self.idle_enough()

    def uploading(self, on: bool) -> None:
        """The uploader tells us when a (night) upload runs: keep the PC awake meanwhile."""
        if on and self.s.yt_when == "night" and self.active():
            self.worked_night = self.night_id()
        self.busy = on
        self._set_awake()

    def _set_awake(self) -> None:
        on = self.busy or self.waiting
        if on != self.awake_on:
            self.awake(on)
            self.awake_on = on

    # ---------------------------------------------------------- shutdown
    def work_left(self) -> bool:
        r = self.rec
        if r.current is not None or r.scan_proc is not None or r.queue:
            return True
        if any(e.status in ("recording", "recorded", "scanning", "waiting") and e.path for e in r.idx.values()
               if not self._old(e)):
            return True
        if any(e.status == "scanned" and not e.note.startswith("not sent") for e in r.idx.values()):
            return True                                      # results about to be sent
        yt = r.youtube
        if yt and self.s.yt_enabled:
            if yt.thread and yt.thread.is_alive():
                return True
            if yt.ready_blocks():
                return True
        return False

    def _old(self, e) -> bool:
        return self.clock() - e.started > 2 * 86400            # stale entries never keep the PC on

    def tick(self, apex: bool) -> None:
        if self.clock() >= self.next_check:              # once a minute: is a block waiting for the night?
            self.next_check = self.clock() + 60
            yt = self.rec.youtube
            self.waiting = bool(self.s.yt_enabled and self.s.yt_when == "night" and yt and yt.ready_blocks())
            self._set_awake()
        if self.shutdown_at is not None:
            if apex or self.idle() < 30:                        # someone is back: don't turn off
                self.cancel("You're using the PC, so it stays on.")
            return
        if not (self.s.night_shutdown and self.active()):
            return
        night = self.night_id()
        if self.worked_night != night or self.cancelled_night == night:
            return
        if apex or not self.idle_enough() or self.work_left():
            return
        self.shutdown_at = self.clock() + SHUTDOWN_DELAY_S
        log.info("night uploads done: turning the PC off")
        try:
            self.run(["shutdown", "/s", "/t", str(SHUTDOWN_DELAY_S), "/c",
                      "PCS Scanner: night uploads are done, the PC turns off in 1 minute."])
        except Exception as e:
            log.warning("shutdown failed: %s", e)
            self.shutdown_at = None
            return
        self.ui.notify("Turning off in 1 minute", "Night uploads are done. Move the mouse or press Cancel to keep "
                       "the PC on.", sticky=True, action="cancelshutdown")

    def cancel(self, why: str = "Shutdown cancelled.") -> None:
        if self.shutdown_at is None:
            return
        try:
            self.run(["shutdown", "/a"])
        except Exception:
            pass
        self.shutdown_at = None
        self.cancelled_night = self.night_id()
        self.ui.notify("PC stays on", why)
