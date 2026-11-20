"""Crash reports to the team hub: private, no third-party service, nothing else sent.

When something breaks (an unhandled error, a recording that doesn't start or stop, OBS crashing, a failed scan),
the app sends a short report to the team hub, so the coach sees what went wrong on a player's PC without asking
for files. Only when:
- the player is on the team hub (hub address + key), and
- "Send crash reports to the team hub" is on in Settings (`crash_reports`, on by default).

What a report holds: the kind of failure, the error, the app and scanner versions, the Windows version, the
graphics card(s), other capture programs running (Medal, ShadowPlay…), the last 200 lines of app.log, and for
recordings / scans / OBS crashes the end of the OBS log, scan.log or OBS crash file. Every line is cleaned first
(`redact`): the hub key, the OBS password, Google tokens, any `password=` / `token=` value, e-mail addresses, the
Windows user name and PC name, the user folder in paths. The hub cleans it again on arrival.

Sending happens on a background thread and never raises. The same error is sent at most once every 30 minutes,
at most 10 reports per day; reports that couldn't be sent (hub not reachable) are kept in memory (up to 5) and
tried again later by `tick()`. A hub without `/api/crash` (older version) is simply skipped.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import sys
import threading
import time
import traceback
from pathlib import Path

from .settings import APP_DIR

log = logging.getLogger("crash")
KINDS = ("unhandled", "record_start", "record_stop", "obs_crash", "scan_failed", "other")
LOG_LINES = 200
ATTACH_LINES = 120
MAX_LINE = 400
REPEAT_S = 1800              # the same error: once per 30 min
PER_DAY = 10
PENDING_MAX = 5
STATE_FILE = APP_DIR / "crash_state.json"     # OBS crash files already looked at
# Other programs that capture the screen or the game: they can make OBS's recording fail to start.
CAPTURE_APPS = ("medal.exe", "medalencoder.exe", "overwolf.exe", "outplayed.exe", "nvidia share.exe",
                "nvidia overlay.exe", "streamlabs obs.exe", "streamlabs desktop.exe", "xsplit.core.exe",
                "action.exe", "bdcam.exe", "gamebar.exe", "gamebarftserver.exe", "insights capture.exe",
                "obs64.exe", "obs32.exe", "twitchstudio.exe", "radeonsoftware.exe")


# ------------------------------------------------------------------ cleaning

_PATTERNS = [
    (re.compile(r"pcs_[A-Za-z0-9_-]{20,}"), "pcs_<key>"),                                     # hub keys
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 <token>"),
    (re.compile(r"ya29\.[\w.-]+"), "<google-token>"),
    (re.compile(r"1//[\w-]{20,}"), "<google-token>"),
    (re.compile(r"GOCSPX-[\w-]+"), "<google-secret>"),
    (re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]+"), "<token>"),                             # JWT
    (re.compile(r"(?i)\b([\w-]*(?:password|passwd|pwd|secret|token|api_?key|hub_?key|auth)[\w-]*)"
                r"(\s*[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&)\"']+)"), r"\1\2***"),
    (re.compile(r"(?i)([?&#](?:key|token|access_token|refresh_token|code|sig|signature|password)=)[^&\s#\"']+"),
     r"\1***"),
    (re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63})+"), "<email>"),
    (re.compile(r"(?i)\b([a-z]:[\\/]+(?:users|documents and settings)[\\/]+)([^\\/:*?\"<>|\r\n]+)"), r"\1<user>"),
    (re.compile(r"(?i)\b(onedrive - )([^\\/\r\n]+)"), r"\1<name>"),
    (re.compile(r"(?i)(drive\.google\.com/\S*?/d/|[?&]id=)[\w-]{20,}"), r"\1<id>"),
]


def own_secrets(settings=None) -> list[tuple[str, str]]:
    """Values of this PC that must never leave it, with what replaces them: the hub key, the OBS password, the
    Windows user name, the PC's name."""
    out = []
    try:
        from . import hub
        k = hub.get_key()
        if k:
            out.append((k, "pcs_<key>"))
    except Exception:
        pass
    pw = getattr(settings, "obs_password", "") if settings is not None else ""
    if pw and len(pw) >= 4:
        out.append((pw, "***"))
    names = {os.environ.get("USERNAME", ""), Path.home().name}
    for n in names:
        if n and len(n) >= 3 and n.lower() not in ("user", "admin", "public", "default"):
            out.append((n, "<user>"))
    pc = os.environ.get("COMPUTERNAME", "")
    if pc and len(pc) >= 3:
        out.append((pc, "<pc>"))
    return sorted(out, key=lambda x: -len(x[0]))


def redact(text, secrets: list[tuple[str, str]] | None = None) -> str:
    """One line (or block) of text with keys, tokens, passwords, e-mail addresses and personal names taken out.
    `secrets`: exact values to replace as well (own_secrets())."""
    t = str(text if text is not None else "")
    for value, rep in secrets or ():
        if value:
            t = re.sub(re.escape(value), rep, t, flags=re.IGNORECASE)
    for rx, rep in _PATTERNS:
        t = rx.sub(rep, t)
    return t


def clean_lines(lines, secrets=None, keep: int = LOG_LINES) -> list[str]:
    return [redact(ln.rstrip("\r\n")[:MAX_LINE + 100], secrets)[:MAX_LINE] for ln in list(lines)[-keep:]]


def tail(path, n: int = LOG_LINES, max_bytes: int = 512 * 1024) -> list[str]:
    """The last `n` lines of a text file (reads at most the last `max_bytes`)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]                                    # first line is cut in the middle
    return lines[-n:]


# ------------------------------------------------------------------ what the PC is

_sysinfo: dict = {}


def windows_version() -> str:
    try:
        if os.name == "nt":
            v = sys.getwindowsversion()
            name = "Windows 11" if v.build >= 22000 else f"Windows {platform.release()}"
            return f"{name} {platform.win32_edition() or ''} {v.major}.{v.minor}.{v.build}".replace("  ", " ")
        return platform.platform()
    except Exception:
        return platform.platform()


def gpu() -> str:
    if "gpu" not in _sysinfo:
        try:
            from .obs import gpu_names
            raw = gpu_names()
        except Exception:
            raw = ""
        _sysinfo["gpu"] = "; ".join(x.strip() for x in raw.splitlines() if x.strip())[:200]
    return _sysinfo["gpu"]


def capture_apps() -> list[str]:
    """Other screen / game capture programs running now (names only)."""
    try:
        import psutil
        ours = str((APP_DIR / "obs").resolve()).lower()
        found = set()
        for p in psutil.process_iter(["name", "exe"]):
            n = (p.info.get("name") or "").lower()
            if n in CAPTURE_APPS and not (p.info.get("exe") or "").lower().startswith(ours):
                found.add(p.info["name"])
        return sorted(found)
    except Exception:
        return []


# ------------------------------------------------------------------ the reporter

class Reporter:
    def __init__(self):
        self.settings = None             # the recorder's Settings (hub_url, crash_reports, obs_password)
        self.version = "dev"
        self.scanner = ""
        self.log_file: str | None = None
        self.lock = threading.Lock()
        self.sent: dict[str, float] = {}         # error signature -> last time sent
        self.day: list[float] = []               # times of reports sent in the last 24 h
        self.pending: list[dict] = []            # built but not delivered (hub not reachable)
        self.next_retry = 0.0
        self.send = self._post                   # tests replace it
        self.sync = False                        # tests: send in the calling thread

    # -- set-up
    def attach(self, settings, version: str = "dev", scanner: str = "", log_file: str | None = None) -> None:
        self.settings, self.version, self.scanner = settings, str(version or "dev"), str(scanner or "")
        self.log_file = log_file

    def enabled(self) -> bool:
        s = self.settings
        if s is None or not getattr(s, "crash_reports", True) or not getattr(s, "hub_url", ""):
            return False
        try:
            from . import hub
            return bool(hub.get_key())
        except Exception:
            return False

    # -- building
    def build(self, kind: str, error: str, detail: str = "", attachments: list[tuple[str, list[str]]] = (),
              context: dict | None = None) -> dict:
        sec = own_secrets(self.settings)
        log_lines = tail(self.log_file) if self.log_file else []
        ctx = {"capture_apps": capture_apps()}
        s = self.settings
        if s is not None:
            ctx.update({"encoder": getattr(s, "encoder", ""), "capture": getattr(s, "capture", ""),
                        "bitrate_mbps": getattr(s, "bitrate_mbps", None), "tracks": ",".join(s.tracks())
                        if hasattr(s, "tracks") else ""})
        ctx.update(context or {})
        return {
            "kind": kind if kind in KINDS else "other",
            "error": redact(str(error)[:2000], sec) or kind,
            "detail": redact(str(detail or "")[-16000:], sec),
            "app": self.version, "scanner": self.scanner,
            "windows": windows_version(), "gpu": gpu(),
            "log": clean_lines(log_lines, sec),
            "attachments": [{"name": n, "lines": clean_lines(lines, sec, ATTACH_LINES)} for n, lines in attachments][:3],
            "context": {str(k)[:40]: (v if isinstance(v, (bool, int, float)) or v is None
                                      else redact(", ".join(v) if isinstance(v, (list, tuple)) else str(v), sec)[:300])
                        for k, v in ctx.items()},
        }

    @staticmethod
    def signature(kind: str, error: str) -> str:
        first = next((ln for ln in str(error).splitlines() if ln.strip()), "")
        norm = re.sub(r"\d+", "N", first.lower())[:200]
        return hashlib.sha256(f"{kind}|{norm}".encode()).hexdigest()[:12]

    # -- sending
    def report(self, kind: str, error, detail: str = "", attachments=(), context: dict | None = None,
               wait: bool = False) -> bool:
        """Send one report (in the background unless `wait`). False when off, throttled or failed. Never raises."""
        try:
            if not self.enabled():
                return False
            now = time.time()
            sig = self.signature(kind, str(error))
            with self.lock:
                self.day = [t for t in self.day if now - t < 86400]
                if now - self.sent.get(sig, 0) < REPEAT_S or len(self.day) >= PER_DAY:
                    return False
                self.sent[sig] = now
                self.day.append(now)
            rep = self.build(kind, str(error), detail, attachments, context)
        except Exception:
            log.debug("crash report not built", exc_info=True)
            return False
        if wait or self.sync:
            return self._deliver(rep)
        threading.Thread(target=self._deliver, args=(rep,), daemon=True, name="crash-report").start()
        return True

    def _deliver(self, rep: dict) -> bool:
        try:
            ok = self.send(rep)
        except Exception:
            ok = None
        if ok is None:                                       # not reachable: keep it for later
            with self.lock:
                self.pending = (self.pending + [rep])[-PENDING_MAX:]
            return False
        if ok:
            log.info("crash report sent to the team hub (%s)", rep.get("kind"))
        return bool(ok)

    def _post(self, rep: dict) -> bool | None:
        """True = stored, False = the hub said no (don't retry), None = not reachable (retry later)."""
        from . import hub
        s = self.settings
        try:
            r = hub.request(s.hub_url, "POST", "/api/crash", json=rep, timeout=15)
        except hub.HubError as e:
            return None if e.kind == "network" else False
        if r.status_code == 404:                             # a hub from before crash reports
            return False
        return r.status_code < 400

    def tick(self) -> None:
        """Try again the reports the hub didn't get, at most every 10 minutes."""
        now = time.time()
        if not self.pending or now < self.next_retry or not self.enabled():
            return
        self.next_retry = now + 600
        with self.lock:
            todo, self.pending = self.pending, []
        for rep in todo:
            self._deliver(rep)


R = Reporter()


def attach(settings, version: str = "dev", scanner: str = "", log_file: str | None = None) -> None:
    R.attach(settings, version, scanner, log_file)


def report(kind: str, error, detail: str = "", attachments=(), context: dict | None = None) -> bool:
    """Send a crash report if the player is on the hub and kept crash reports on. Never raises."""
    return R.report(kind, error, detail, attachments, context)


def report_exception(kind: str, e: BaseException, attachments=(), context: dict | None = None) -> bool:
    tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    return report(kind, f"{type(e).__name__}: {e}", tb, attachments, context)


def tick() -> None:
    try:
        R.tick()
        check_obs_crashes()
    except Exception:
        log.debug("crash tick failed", exc_info=True)


# ------------------------------------------------------------------ hooks

def install_hooks() -> None:
    """Unhandled errors in the main thread and in other threads are reported (and still logged as before)."""
    old_hook, old_thread_hook = sys.excepthook, threading.excepthook

    def hook(t, e, tb):
        try:
            if not issubclass(t, (KeyboardInterrupt, SystemExit)):
                report("unhandled", f"{t.__name__}: {e}", "".join(traceback.format_exception(t, e, tb)))
        finally:
            old_hook(t, e, tb)

    def thread_hook(args):
        try:
            if args.exc_type is not None and not issubclass(args.exc_type, SystemExit):
                name = getattr(args.thread, "name", "?")
                report("unhandled", f"{args.exc_type.__name__}: {args.exc_value}",
                       f"thread {name}\n" + "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                                                args.exc_traceback)))
        finally:
            old_thread_hook(args)

    sys.excepthook, threading.excepthook = hook, thread_hook


# ------------------------------------------------------------------ OBS: its log and its crash files

def obs_dir() -> Path:
    from .obs import CONFIG
    return CONFIG


def obs_log_tail(n: int = ATTACH_LINES) -> list[tuple[str, list[str]]]:
    """The end of the private OBS's newest log, as an attachment (empty if there is none)."""
    try:
        logs = sorted((obs_dir() / "logs").glob("*.txt"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return []
    return [("OBS log", tail(logs[-1], n))] if logs else []


def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def check_obs_crashes(now: float | None = None, first_days: int = 7) -> int:
    """OBS writes a crash file (config/obs-studio/crashes/Crash *.txt) when it crashes. Each new one is reported
    once: the first time this runs, those of the last `first_days` days (at most 3), then only newer ones.
    -> number reported. Looks at the folder at most once a minute."""
    now = time.time() if now is None else now
    if now - getattr(check_obs_crashes, "last", 0) < 60 or not R.enabled():
        return 0
    check_obs_crashes.last = now
    try:
        files = sorted((obs_dir() / "crashes").glob("Crash *.txt"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return 0
    st_ = _state()
    seen = float(st_.get("obs_crash_seen", now - first_days * 86400))
    new = [f for f in files if f.stat().st_mtime > seen][-3:]
    if not new and "obs_crash_seen" in st_:
        return 0
    n = 0
    for f in new:
        lines = tail(f, 400, 256 * 1024)
        head = [ln for ln in lines[:40] if ln.strip()]
        err = next((ln for ln in head if "unhandled exception" in ln.lower() or "crash" in ln.lower()), "OBS crashed")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime))
        if R.report("obs_crash", f"OBS crashed: {err.strip()[:200]}", f"crash file of {when}",
                    [("OBS crash file", lines[:ATTACH_LINES])], {"crash_file_time": when}):
            n += 1
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        st_["obs_crash_seen"] = max([seen] + [f.stat().st_mtime for f in files])
        STATE_FILE.write_text(json.dumps(st_), encoding="utf-8")
    except OSError:
        pass
    return n
