"""Hub backup copy (coach only, hub 0.5.4): a second copy of the team hub's nightly backup, kept on the coach's PC.

The hub writes `pcs-hub-<yyyymmdd>.zip` every night (its database, Loot Book images, uploaded scans, settings without
secrets). The coach's app downloads the newest one once a day into a folder on his PC (`GET /api/backup/latest`, his
key; players' keys are refused by the hub), so the team's data survives the hub PC.

- Shown and run only when the hub key's role is coach (`/api/me`, asked at most every ROLE_TTL, or the Test button).
- Folder: chosen in Settings. Default `E:\\PCS Hub backups` when this PC has an E: drive, else none (= off). Never a
  folder on the Windows drive (C:): the coach's is small, and a full C: breaks Windows.
- Once a day from DUE_HOUR (04:00; the hub's zip is made around midnight), or at the next app start after that: only
  while Apex is closed and nothing records or scans. HEAD first (date, size); that date already in the folder =
  nothing to do. Else downloaded to `<name>.partial` at background priority, checked (size, zip), renamed.
  Apex starting meanwhile stops the download (partial removed, tried again later; not a failure).
- Keeps the newest `keep` files named exactly `pcs-hub-<8 digits>.zip`; its own leftover `.partial` files go too.
  Nothing else in the folder is ever touched.
- No window, no pop-up. A failure is retried RETRY_S later the same day; after failures on FAIL_DAYS days in a row,
  one line in Activity: "Hub backup copy failed: <reason>" (once, until a copy works again).
- State (`hub-backup.json` in the data folder): folder, keep, the last result. Never the key.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from . import hub
from .settings import APP_DIR

log = logging.getLogger("hubbackup")
STATE_FILE = APP_DIR / "hub-backup.json"
NAME_RE = re.compile(r"pcs-hub-(\d{8})\.zip")
PARTIAL_RE = re.compile(r"pcs-hub-\d{8}\.zip\.partial")
DEFAULT_DIR = "E:\\PCS Hub backups"
DEFAULT_KEEP = 14
MAX_KEEP = 365
DUE_HOUR = 4
ROLE_TTL = 6 * 3600
RETRY_S = 3600
FAIL_DAYS = 3
CHUNK = 1 << 20
BUSY_EVERY_S = 3                      # during a download: look for Apex (and a recording or scan) this often
SPARE = 1 << 30                       # leave at least 1 GB free on the target drive
FAILED = "Hub backup copy failed: "


def system_drive() -> str:
    return (os.environ.get("SystemDrive") or "C:").rstrip("\\/").upper()


def drive_of(folder: str) -> str:
    return os.path.splitdrive(str(folder))[0].rstrip("\\/").upper()


def default_folder(exists=os.path.isdir) -> str:
    """E:\\PCS Hub backups when an E: drive exists (and E: isn't the Windows drive), else "" (off)."""
    if system_drive() != "E:" and exists("E:\\"):
        return DEFAULT_DIR
    return ""


def check_folder(folder: str) -> str:
    """A folder the copy may use: absolute, on a drive letter that isn't the Windows drive. "" = off.
    Raises ValueError with what to do."""
    f = str(folder or "").strip().strip('"')
    if not f:
        return ""
    d = drive_of(f)
    if not re.fullmatch(r"[A-Z]:", d) or not os.path.isabs(f):
        raise ValueError("Pick a full folder path, like E:\\PCS Hub backups.")
    if d == system_drive():
        raise ValueError(f"Pick a folder on another drive than {d} (the Windows drive).")
    return os.path.normpath(f)


def validate(folder=None, keep=None) -> tuple[str | None, int | None]:
    """(folder, keep) from Settings, checked; None = unchanged. Raises ValueError with what to do."""
    if folder is not None:
        folder = check_folder(folder)
    if keep is not None:
        try:
            keep = int(keep)
        except (TypeError, ValueError):
            raise ValueError("Keep: a number of copies, 1 to 365.") from None
        if not 1 <= keep <= MAX_KEEP:
            raise ValueError("Keep: a number of copies, 1 to 365.")
    return folder, keep


def own_zips(folder: Path) -> list[Path]:
    """The hub backup zips in a folder, oldest first: exactly pcs-hub-<8 digits>.zip, nothing else."""
    try:
        return sorted((p for p in folder.iterdir() if p.is_file() and NAME_RE.fullmatch(p.name)), key=lambda p: p.name)
    except OSError:
        return []


def prune(folder: Path, keep: int, current: str = "") -> list[str]:
    """Keep the newest `keep` hub zips; remove our own leftover .partial files. Nothing else is touched."""
    removed = []
    zips = own_zips(folder)
    for p in zips[:-keep] if keep > 0 else []:
        try:
            p.unlink()
            removed.append(p.name)
        except OSError as e:
            log.info("old hub backup not removed: %s (%s)", p.name, e)
    try:
        for p in folder.iterdir():
            if p.is_file() and PARTIAL_RE.fullmatch(p.name) and p.name != current + ".partial":
                try:
                    p.unlink()
                    removed.append(p.name)
                except OSError:
                    pass
    except OSError:
        pass
    return removed


def _background(on: bool) -> None:
    """Windows background mode for this thread: lowest CPU and disk priority (does nothing elsewhere)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetThreadPriority(k.GetCurrentThread(), 0x00010000 if on else 0x00020000)
    except Exception:
        pass


class Busy(Exception):
    """Apex started, or the PC got busy: stop quietly and try again later."""


class HubBackup:
    """`hub_url()` gives the hub address; `busy()` a reason not to run now (Apex, recording, scan) or None;
    `say(text)` writes a line in Activity."""

    def __init__(self, hub_url, busy, say, now=time.time, drive_exists=os.path.isdir):
        self.hub_url, self.busy, self.say, self.now = hub_url, busy, say, now
        self.drive_exists = drive_exists
        self.lock = threading.Lock()
        self.running = False
        self.next_try = 0.0
        self._role: tuple[str, str, float] | None = None       # (hub address + key fingerprint, role, when asked)
        self.st = self._load()

    # ---------------------------------------------------------------- state
    def _load(self) -> dict:
        try:
            d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".part")
            tmp.write_text(json.dumps(self.st, indent=2), encoding="utf-8")
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            log.warning("hub backup state not saved: %s", e)

    @property
    def folder(self) -> str:
        f = self.st.get("folder")
        if f is None:                                   # never chosen: the default (E:), else off
            return default_folder(self.drive_exists)
        return str(f)

    @property
    def keep(self) -> int:
        try:
            return max(1, min(MAX_KEEP, int(self.st.get("keep", DEFAULT_KEEP))))
        except (TypeError, ValueError):
            return DEFAULT_KEEP

    def set(self, folder=None, keep=None) -> None:
        """Settings → Hub backup copy. Raises ValueError (nothing saved) for a folder on the Windows drive."""
        folder, keep = validate(folder, keep)
        with self.lock:
            if folder is not None:
                self.st["folder"] = folder
            if keep is not None:
                self.st["keep"] = keep
            self._save()
        self.next_try = 0.0

    # ---------------------------------------------------------------- role
    def role(self, ask: bool = True) -> str:
        """The hub key's role ("coach" / "player"), "" when unknown (no hub, no key, hub not reachable yet)."""
        url, key = self.hub_url(), hub.get_key()
        if not url or not key:
            return ""
        who = url + "|" + hub._fp(key)
        last = hub.S.last
        if last.get("url") == url and last.get("role") in ("coach", "player") and not hub.public_state(url)["refused"]:
            if not self._role or self._role[0] != who or self._role[1] != last["role"]:
                self._role = (who, last["role"], self.now())          # the Test button just asked
        if self._role and self._role[0] == who and self.now() - self._role[2] < ROLE_TTL:
            return self._role[1]
        if not ask:
            return self._role[1] if self._role and self._role[0] == who else ""
        try:
            r = hub.request(url, "GET", "/api/me", timeout=8)
            hub._check(r)
            role = "coach" if r.json().get("role") == "coach" else "player"
        except hub.HubError as e:
            if e.kind == "refused":
                self._role = (who, "player", self.now())
                return "player"
            return self._role[1] if self._role and self._role[0] == who else ""
        except ValueError:
            return ""
        self._role = (who, role, self.now())
        return role

    def is_coach(self) -> bool:
        return self.role(ask=False) == "coach"

    # ---------------------------------------------------------------- what the page sees
    def public(self) -> dict | None:
        """Settings block, or None when the key isn't the coach's (the block is then not shown at all)."""
        if not self.is_coach():
            return None
        f = self.folder
        return {"folder": f, "keep": self.keep, "default": default_folder(self.drive_exists),
                "chosen": self.st.get("folder") is not None, "running": self.running,
                "last": self.st.get("last") or {}, "lastGood": self.st.get("last_good") or {},
                "failDays": len(self.st.get("fail_days") or [])}

    # ---------------------------------------------------------------- when
    def due(self) -> bool:
        if self.running or self.now() < self.next_try or not self.folder:
            return False
        t = datetime.fromtimestamp(self.now())
        return t.hour >= DUE_HOUR and self.st.get("done_day") != t.strftime("%Y-%m-%d")

    def tick(self) -> bool:
        """Called every minute by the app. Starts the daily copy in its own thread when due. -> started."""
        try:
            if self.busy() or not self.hub_url() or not hub.get_key():
                return False                                   # never during a game: not even a request
            if self.role() != "coach" or not self.due():
                return False
        except Exception:
            log.exception("hub backup check")
            return False
        return self.start("daily")

    def start(self, why: str = "by hand") -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
        threading.Thread(target=self._run, args=(why,), daemon=True, name="hub-backup").start()
        return True

    def copy_now(self) -> dict:
        """The "Copy now" button. Raises RuntimeError with the reason when it can't start."""
        if not self.is_coach() and self.role() != "coach":
            raise PermissionError("Only the coach's hub key can copy the hub backup.")
        why = self.busy()
        if why:
            raise RuntimeError(f"Close {why[:-len(' is running')]} first." if why.endswith(" is running")
                               and not why.startswith("a ") else f"Wait: {why}.")
        if not self.folder:
            raise RuntimeError("Pick a folder first.")
        if not self.start("by hand"):
            raise RuntimeError("A copy is already running.")
        return {"started": True}

    # ---------------------------------------------------------------- the copy
    def _run(self, why: str) -> None:
        _background(True)
        try:
            res = self.copy_once()
        except Busy as e:
            log.info("hub backup copy paused: %s", e)
            self.next_try = self.now() + 300
            res = None
        except Exception as e:
            log.exception("hub backup copy")
            res = {"ok": False, "error": f"{type(e).__name__}"}
        finally:
            _background(False)
            with self.lock:
                self.running = False
        if res is not None:
            self._record(res, why)

    def _check_busy(self) -> None:
        b = self.busy()
        if b:
            raise Busy(b)

    def copy_once(self) -> dict:
        """One copy: {"ok": True, "date", "bytes", "file", "skipped"?} or {"ok": False, "error", "wait"?}."""
        url, folder = self.hub_url(), self.folder
        if not folder:
            return {"ok": False, "error": "no folder"}
        self._check_busy()
        try:
            h = hub.request(url, "HEAD", "/api/backup/latest", timeout=15)
        except hub.HubError as e:
            return {"ok": False, "error": "key refused" if e.kind == "refused" else "hub not reachable"}
        if h.status_code == 409:
            return {"ok": False, "wait": True, "error": "the hub is writing tonight's backup"}
        if h.status_code == 403:
            self._role = None
            return {"ok": False, "error": "this key isn't the coach's"}
        if h.status_code == 404:
            return {"ok": False, "error": "no backup on the hub yet (hub 0.5.4 or newer needed)"}
        if h.status_code != 200:
            return {"ok": False, "error": f"hub error {h.status_code}"}
        date = str(h.headers.get("X-Backup-Date") or "")
        try:
            size = int(h.headers.get("X-Backup-Bytes") or h.headers.get("Content-Length") or -1)
        except ValueError:
            size = -1
        if not re.fullmatch(r"\d{8}", date) or size <= 0:
            return {"ok": False, "error": "the hub's answer has no backup date"}
        name = f"pcs-hub-{date}.zip"
        target = Path(folder)
        if not os.path.isdir(drive_of(folder) + "\\") and os.name == "nt":
            return {"ok": False, "error": f"drive {drive_of(folder)} not found"}
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"ok": False, "error": f"can't make the folder ({e.strerror or e})"}
        final = target / name
        if final.is_file() and final.stat().st_size == size:
            removed = prune(target, self.keep, name)
            return {"ok": True, "skipped": True, "date": date, "bytes": size, "file": name, "removed": removed}
        try:
            free = shutil.disk_usage(target).free
        except OSError:
            free = None
        if free is not None and free < size + SPARE:
            return {"ok": False, "error": f"not enough space on {drive_of(folder) or 'the drive'}"}
        partial = target / (name + ".partial")
        try:
            self._download(url, date, size, partial)
            os.replace(partial, final)
        except Busy:
            partial.unlink(missing_ok=True)
            raise
        except hub.HubError as e:
            partial.unlink(missing_ok=True)
            return {"ok": False, "error": "key refused" if e.kind == "refused" else "hub not reachable"}
        except _Bad as e:
            partial.unlink(missing_ok=True)
            return {"ok": False, "error": str(e)}
        except OSError as e:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
            return {"ok": False, "error": f"can't write to the folder ({e.strerror or type(e).__name__})"}
        removed = prune(target, self.keep, name)
        log.info("hub backup copied: %s, %d MB", name, size >> 20)
        return {"ok": True, "date": date, "bytes": size, "file": name, "removed": removed}

    def _download(self, url: str, date: str, size: int, partial: Path) -> None:
        import requests
        r = hub.request(url, "GET", f"/api/backup/latest?date={date}", timeout=(15, 120), stream=True)
        try:
            if r.status_code != 200:
                raise _Bad(f"hub error {r.status_code}")
            got, looked = 0, time.monotonic()
            with open(partial, "wb") as f:
                try:
                    for part in r.iter_content(CHUNK):
                        f.write(part)
                        got += len(part)
                        if time.monotonic() - looked >= BUSY_EVERY_S:
                            looked = time.monotonic()
                            self._check_busy()             # Apex started: stop, the rest waits for later
                except requests.RequestException:
                    raise _Bad("the download broke off") from None
        finally:
            r.close()
        if got != size:
            raise _Bad(f"the download is incomplete ({got >> 20} of {size >> 20} MB)")
        if not zipfile.is_zipfile(partial):
            raise _Bad("the downloaded file isn't a zip")

    def _record(self, res: dict, why: str) -> None:
        today = datetime.fromtimestamp(self.now()).strftime("%Y-%m-%d")
        at = datetime.fromtimestamp(self.now()).isoformat(timespec="seconds")
        with self.lock:
            if res.get("ok"):
                self.st["last"] = self.st["last_good"] = {
                    "at": at, "ok": True, "date": res["date"], "bytes": res["bytes"], "file": res["file"],
                    "result": "Already copied" if res.get("skipped") else "Copied"}
                self.st["done_day"] = today
                self.st["fail_days"] = []
                self.st.pop("told", None)
                self.next_try = 0.0
            else:
                self.st["last"] = {"at": at, "ok": False, "result": "Failed", "error": res["error"]}
                self.next_try = self.now() + (900 if res.get("wait") else RETRY_S)
                if not res.get("wait"):
                    days = [d for d in (self.st.get("fail_days") or []) if d != today] + [today]
                    self.st["fail_days"] = days[-10:]
            tell = (not res.get("ok") and not self.st.get("told")
                    and self._in_a_row(self.st.get("fail_days") or []) >= FAIL_DAYS)
            if tell:
                self.st["told"] = today
            self._save()
        if tell:                                                 # the only message; "Copy now" shows its result in Settings
            self.say(FAILED + res["error"])
        log.info("hub backup copy (%s): %s", why, self.st["last"])

    @staticmethod
    def _in_a_row(days: list[str]) -> int:
        """How many calendar days in a row, up to the last one, a copy failed."""
        ds = sorted({datetime.strptime(d, "%Y-%m-%d").date() for d in days})
        n = 0
        for i in range(len(ds) - 1, -1, -1):
            if i == len(ds) - 1 or (ds[i + 1] - ds[i]).days == 1:
                n += 1
            else:
                break
        return n


class _Bad(Exception):
    """A download that can't be used (not a network error)."""
