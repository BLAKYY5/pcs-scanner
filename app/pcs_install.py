"""PCS Scanner as an installed folder instead of a single exe that unpacks itself at every start.

The release file is still one `PCS-Scanner.exe` (so a player can download it, and versions 1.10.12+ update to it the
way they always did), but it is now a small *installer*: it carries the app as a folder (PyInstaller "onedir") and a
tiny *launcher*. Three programs, one module (standard library only, so the launcher stays small):

- installer (`pcs_setup.py`, the release file): unpacks once, copies the app folder to
  `%LOCALAPPDATA%\\PCS Scanner\\app\\<version>\\`, records it in `app\\current.json`, starts it and exits. With
  `--finish-update CUR PID` (what every updater since 1.10.2 runs) it first waits for the old app to close.
- app (`app\\<version>\\PCS-Scanner.exe`): starts in about a second (nothing to unpack), scans without unpacking.
  It knows the player's own file (shortcut, Start-up link, the place they downloaded it to) from `PCS_LAUNCHER`, and
  once the installer has exited it puts the launcher there (`adopt_launcher`), so the next start is fast too.
- launcher (`pcs_launcher.py`, ~10 MB): what the player's shortcut runs from then on: starts the installed app with
  the same arguments.

Updates: the app downloads the release file next to the launcher and runs it with `--finish-update <launcher> <pid>`:
the same command an old version gives, so one path covers both.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

APP_EXE = "PCS-Scanner.exe"
LAUNCHER_ENV = "PCS_LAUNCHER"            # the player's own file (launcher or installer), set for the app
KEEP_OLD = 1                             # older version folders kept (one, to go back by hand if needed)


def app_root() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "PCS Scanner", "app")


def _current_file(root: str | None = None) -> str:
    return os.path.join(root or app_root(), "current.json")


def current(root: str | None = None) -> dict | None:
    """{version, dir, exe} of the installed app, or None when there is none (or its files are gone)."""
    try:
        with open(_current_file(root), encoding="utf-8") as f:
            cur = json.load(f)
        exe = os.path.join(cur["dir"], APP_EXE)
        if os.path.isfile(exe) and os.path.isfile(os.path.join(cur["dir"], ".complete")):
            return cur | {"exe": exe}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def folder_name(version: str) -> str:
    return "".join(c if c.isalnum() or c in ".-_" else "_" for c in str(version)) or "unknown"


def install(payload: str, version: str, root: str | None = None) -> dict:
    """Copy the app folder `payload` to <root>/<version>/ (unless that version is already there, complete) and make
    it the current one. A half-copied folder (crash, full disk) never becomes current: it is copied under a
    temporary name, marked complete, then renamed. -> current()."""
    root = root or app_root()
    os.makedirs(root, exist_ok=True)
    dest = os.path.join(root, folder_name(version))
    if not os.path.isfile(os.path.join(dest, ".complete")):
        tmp = dest + ".partial"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(payload, tmp)
        with open(os.path.join(tmp, ".complete"), "w", encoding="utf-8") as f:
            f.write(str(version))
        if os.path.exists(dest):                              # an incomplete copy from before
            shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
    tmp = _current_file(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": str(version), "dir": dest, "at": int(time.time())}, f)
    os.replace(tmp, _current_file(root))
    return current(root)


def remove_old(root: str | None = None, keep: int = KEEP_OLD, in_use=None) -> list[str]:
    """Version folders other than the current one and the `keep` newest others; never one still in use (a running
    copy keeps its python DLL loaded: Windows refuses to delete it, so that DLL goes first, as clean_old_unpacked
    does). Only folders this installer made (they hold `.complete` or end in `.partial`). -> folders removed."""
    root = root or app_root()
    cur = current(root)
    try:
        names = os.listdir(root)
    except OSError:
        return []
    mine = []
    for n in names:
        p = os.path.join(root, n)
        if not os.path.isdir(p) or (cur and os.path.normcase(p) == os.path.normcase(cur["dir"])):
            continue
        if n.endswith(".partial") or os.path.isfile(os.path.join(p, ".complete")):
            mine.append(p)
    mine.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    done = []
    for p in [p for p in mine if p.endswith(".partial")] + [p for p in mine if not p.endswith(".partial")][keep:]:
        internal = os.path.join(p, "_internal")
        try:
            for f in os.listdir(internal) if os.path.isdir(internal) else []:
                if f.lower().startswith("python3") and f.lower().endswith(".dll"):
                    os.remove(os.path.join(internal, f))       # refused while that version runs: folder kept
        except OSError:
            continue
        shutil.rmtree(p, ignore_errors=True)
        if not os.path.exists(p):
            done.append(p)
    return done


def run_app(args: list[str], launcher: str, root: str | None = None, wait: bool = False) -> int | None:
    """Start the installed app with these arguments; it learns the player's own file from PCS_LAUNCHER.
    -> its exit code when wait, else None. Raises FileNotFoundError when no app is installed."""
    cur = current(root)
    if not cur:
        raise FileNotFoundError("PCS Scanner isn't installed in " + (root or app_root()))
    env = dict(os.environ)
    env[LAUNCHER_ENV] = os.path.abspath(launcher)
    flags = 0x00000008 | 0x00000200 if not wait else 0     # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen([cur["exe"], *args], env=env, cwd=cur["dir"], close_fds=True, creationflags=flags)
    return p.wait() if wait else None


def launcher_path() -> str | None:
    """In the installed app: the player's own file (launcher, or the installer they ran), if any."""
    p = os.environ.get(LAUNCHER_ENV, "")
    return p if p.lower().endswith(".exe") and os.path.isabs(p) else None


def is_installed_app() -> bool:
    """True in the installed app (onedir), False in a onefile exe or from source."""
    return bool(getattr(sys, "frozen", False)) and not os.path.basename(getattr(sys, "_MEIPASS", "")).startswith("_MEI")


LAUNCHER_MAX = 40 * 2 ** 20               # a file bigger than this at the player's place is the installer, not the launcher


def adopt_launcher(target: str, launcher_src: str, wait_s: float = 120, sleep=time.sleep) -> bool:
    """Put the small launcher at the player's own file `target` (the big installer they ran, or the old onefile exe
    an update replaces), so the next start doesn't unpack anything. The file may still be in use for a moment
    (the installer exiting): retried for wait_s. A file that is already the launcher is left alone. -> replaced."""
    try:
        if not os.path.isfile(target) or not os.path.isfile(launcher_src):
            return False
        if os.path.getsize(target) <= LAUNCHER_MAX and _same(target, launcher_src):
            return False
    except OSError:
        return False
    tmp = target + ".new"
    end = time.time() + wait_s
    while True:
        try:
            shutil.copy2(launcher_src, tmp)
            os.replace(tmp, target)
            return True
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            if time.time() >= end:
                return False
            sleep(1)


def _same(a: str, b: str) -> bool:
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(1 << 20), fb.read(1 << 20)
            if x != y:
                return False
            if not x:
                return True


def message(text: str, title: str = "PCS Scanner") -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        print(text)
