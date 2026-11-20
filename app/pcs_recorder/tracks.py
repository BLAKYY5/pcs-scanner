"""A recording's audio tracks as separate files, for the review player (game, voice app and mic side by side).

Cutting a track copies it out of the recording (no re-encoding), but it still reads the whole file: minutes for a
3-hour session. So it never happens inside a request of the review page:
- right after a recording ends, the recorder queues it here: cut at low priority, one recording at a time, all its
  tracks in one read of the file, never while Apex runs;
- the review page asking for a track that isn't there yet queues that recording first (and is told "preparing"
  until it is done). That one also runs while Apex is open: someone is waiting for it.
Each file is written as .part.m4a and renamed once complete, so a cut stopped halfway (app closed, PC off) never
leaves a broken track; stale .part files are removed at start.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("tracks")
RETRY_FAILED_S = 300          # a cut that failed is tried again when asked after this long
WAIT_S = 5                    # background cuts: look again this often while Apex runs


def _quiet() -> int:
    return 0x08000000 | 0x00004000 if os.name == "nt" else 0       # no window, below-normal priority


def cache_dir() -> Path:
    from .settings import APP_DIR
    return Path(APP_DIR) / "audio-cache"


def track_path(video: str, n: int) -> Path:
    """Where track n (0-based) of a recording goes: next to the app's own recordings (deleted with them), in the
    app's cache for files added by hand."""
    v = Path(video)
    if v.with_suffix(".events.json").exists():
        return v.with_name(f"{v.stem}.track{n + 1}.m4a")
    import hashlib
    vid = hashlib.sha1(os.path.normcase(os.path.abspath(video)).encode()).hexdigest()[:12]
    return cache_dir() / f"{vid}.track{n + 1}.m4a"


def part_of(out: Path) -> Path:
    return out.with_name(out.stem + ".part.m4a")


def fresh(out: Path, video: str) -> bool:
    try:
        return out.exists() and out.stat().st_mtime >= Path(video).stat().st_mtime
    except OSError:
        return False


def audio_count(video: str) -> int:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                            "-of", "csv=p=0", video], capture_output=True, text=True, timeout=20,
                           creationflags=_quiet())
        return len([x for x in r.stdout.split() if x.strip()])
    except Exception:
        return 0


def cut_tracks(video: str, outs: list[tuple[int, Path]]) -> None:
    """All the given tracks in one read of the recording; each to its .part file, renamed when all are done."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(video)]
    for n, out in outs:
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd += ["-map", f"0:a:{n}", "-vn", "-c:a", "copy", str(part_of(out))]
    try:
        subprocess.run(cmd, check=True, timeout=3 * 3600, capture_output=True, creationflags=_quiet())
        for _, out in outs:
            os.replace(part_of(out), out)
    finally:
        for _, out in outs:
            part_of(out).unlink(missing_ok=True)


def clean_parts(folders) -> int:
    """Remove .part files left by a cut that was stopped (the app was closed mid-cut)."""
    n = 0
    for d in {Path(f) for f in folders if f}:
        try:
            for p in d.glob("*.track*.part.m4a"):
                try:
                    p.unlink()
                    n += 1
                except OSError:
                    pass
        except OSError:
            pass
    return n


class Cutter:
    def __init__(self, is_apex_running=None, cut=cut_tracks, count=audio_count, wait_s: float = WAIT_S):
        self.is_apex_running = is_apex_running
        self.cut, self.count, self.wait_s = cut, count, wait_s
        self.cv = threading.Condition()
        self.jobs: list[list] = []                # [key, asked by the review page, video]
        self.now: str | None = None               # recording being cut
        self.counts: dict[str, int] = {}          # audio tracks per recording, once known
        self.failed: dict[str, tuple[float, str]] = {}
        self.thread: threading.Thread | None = None

    def _apex(self) -> bool:
        f = self.is_apex_running
        if f is None:
            from .core import game_running as f         # Apex or any other game (games.py)
        try:
            return bool(f())
        except Exception:
            return False

    def clean(self, folders=()) -> None:
        """At start, before any cut: stale .part files in these folders and in the cache."""
        with self.cv:
            if self.now or self.jobs:                       # a cut is writing its .part files now
                return
            n = clean_parts(list(folders) + [cache_dir()])
        if n:
            log.info("removed %d unfinished track file(s)", n)

    # ------------------------------------------------------------ asking
    def add(self, video: str, asked: bool = False) -> None:
        """Queue a recording (all its tracks). asked = the review page waits for it: goes first, even during a game."""
        key = os.path.normcase(os.path.abspath(video))
        with self.cv:
            if self.now == key:
                return
            job = next((j for j in self.jobs if j[0] == key), None)
            if job:
                if asked and not job[1]:
                    job[1] = True
                    self.jobs.remove(job)
                    self.jobs.insert(0, job)
            elif asked:
                self.jobs.insert(0, [key, True, video])
            else:
                self.jobs.append([key, False, video])
            if not (self.thread and self.thread.is_alive()):
                self.thread = threading.Thread(target=self._run, daemon=True, name="tracks")
                self.thread.start()
            self.cv.notify_all()

    def state(self, video: str, n: int) -> tuple[str, Path | str | None]:
        """("ready", path) | ("preparing", None) | ("missing", why). Never waits for a cut: queues one when needed."""
        out = track_path(video, n)
        if fresh(out, video):
            return "ready", out
        key = os.path.normcase(os.path.abspath(video))
        with self.cv:
            count = self.counts.get(key)
            failed = self.failed.get(key)
            busy = self.now == key or any(j[0] == key for j in self.jobs)
        if count is not None and (n >= count or count < 2):
            return "missing", "this recording has no such audio track"
        if failed and not busy and time.time() - failed[0] < RETRY_FAILED_S:
            return "missing", failed[1]
        if not Path(video).exists():
            return "missing", "file missing"
        self.add(video, asked=True)
        return "preparing", None

    def busy(self) -> bool:
        with self.cv:
            return bool(self.now or self.jobs)

    # ------------------------------------------------------------ the worker
    def _next(self):
        with self.cv:
            while True:
                if not self.jobs:
                    return None
                job = next((j for j in self.jobs if j[1]), None)
                if job is None and not self._apex():
                    job = self.jobs[0]
                if job:
                    self.jobs.remove(job)
                    self.now = job[0]
                    return job
                self.cv.wait(self.wait_s)          # only background cuts left and Apex is running

    def _run(self) -> None:
        while True:
            job = self._next()
            if job is None:
                return
            key, video = job[0], job[2]
            try:
                self._cut(key, video)
                with self.cv:
                    self.failed.pop(key, None)
            except Exception as ex:
                why = "couldn't cut the audio tracks"
                if isinstance(ex, subprocess.CalledProcessError):
                    why += ": " + ((ex.stderr or b"").decode("utf-8", "replace").strip().splitlines() or ["ffmpeg failed"])[-1][:200]
                log.warning("track cut failed for %s: %s", video, ex)
                with self.cv:
                    self.failed[key] = (time.time(), why)
            finally:
                with self.cv:
                    self.now = None
                    self.cv.notify_all()

    def _cut(self, key: str, video: str) -> None:
        if not Path(video).exists():
            raise FileNotFoundError(video)
        count = self.count(video)
        with self.cv:
            self.counts[key] = count
        if count < 2:                                   # one track: the video plays it itself
            return
        todo = [(n, track_path(video, n)) for n in range(count)]
        todo = [(n, out) for n, out in todo if not fresh(out, video)]
        if todo:
            t0 = time.time()
            self.cut(video, todo)
            log.info("cut %d audio track(s) of %s in %.0f s", len(todo), Path(video).name, time.time() - t0)


CUTTER = Cutter()
