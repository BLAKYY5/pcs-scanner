"""Which game runs now: Apex (the only game recorded) or any other game (the app's heavy work waits).

The coach's request (19 Nov 2026): "detect whenever a game is played and pause any intensive or demanding
processes", e.g. Deadlock or The Finals. Apex keeps its own role: only Apex is recorded. While any game runs, the
heavy work (scans, track cutting, transcripts, clips, joins, the hub backup copy, team-track and map downloads,
result and YouTube uploads) waits exactly as it already did for Apex, and nothing new appears on the screen.

What counts as a game (`Detector.other`), in this order:
1. the player's own "not a game" list (Settings → Games → Remove) wins over everything below;
2. the player's own list of programs (Settings → Games → Add);
3. the built-in list `KNOWN` (exe names checked on the coach's PC where installed, see games-list.md);
4. Windows' own list of games it recognised on this PC (HKCU\\System\\GameConfigStore\\Children, value
   MatchedExeFullPath): read once, then again every `REGISTRY_S`; launchers, Java and the like are left out
   (`NOT_GAMES`);
5. off by default: a full-screen window in front that is not a browser, a video player, Explorer or this app.

How (anti-cheat safe): one Windows process snapshot (CreateToolhelp32Snapshot: program names only, no process is
ever opened, read or touched), at most once per `SNAPSHOT_S` for every caller together; the window in front only when
5 is on (its size and the name of its program, from the same snapshot). Never a game's memory or its process.
"""
from __future__ import annotations

import ctypes
import logging
import re
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("games")

APEX = "Apex"
APEX_EXES = {"r5apex_dx12.exe", "r5apex.exe"}
SNAPSHOT_S = 2.0                # one process snapshot serves every caller for this long
REGISTRY_S = 6 * 3600           # Windows' game list is read again this often (it changes when a game is installed)

# Built-in list: exe name (lower case) -> name shown (game names stay English). Checked on the coach's PC (Steam
# manifests + each game's folder, Windows' game list), 19 Nov 2026: deadlock.exe, Discovery*.exe, cs2.exe,
# VALORANT-Win64-Shipping.exe, FortniteClient-Win64-Shipping.exe, Overwatch.exe, Marvel-Win64-Shipping.exe,
# RainbowSix.exe. The others are the games' usual names, not installed there.
KNOWN = {
    "deadlock.exe": "Deadlock", "project8.exe": "Deadlock",
    "discovery.exe": "The Finals", "discovery-d.exe": "The Finals", "discovery-e.exe": "The Finals",
    "valorant-win64-shipping.exe": "Valorant",
    "cs2.exe": "Counter-Strike 2",
    "fortniteclient-win64-shipping.exe": "Fortnite",
    "overwatch.exe": "Overwatch",
    "marvel-win64-shipping.exe": "Marvel Rivals",
    "tslgame.exe": "PUBG",
    "cod.exe": "Call of Duty", "cod24-cod.exe": "Call of Duty", "blackops6.exe": "Call of Duty",
    "modernwarfare.exe": "Call of Duty",
    "rainbowsix.exe": "Rainbow Six Siege", "rainbowsix_be.exe": "Rainbow Six Siege",
    "rainbowsix_vulkan.exe": "Rainbow Six Siege",
    "league of legends.exe": "League of Legends",
    "dota2.exe": "Dota 2",
    "rocketleague.exe": "Rocket League",
    "deltaforceclient-win64-shipping.exe": "Delta Force",
    "pioneergame.exe": "Arc Raiders",
    "strinova-win64-shipping.exe": "Strinova",
    "sotgame.exe": "Sea of Thieves",
    "deadbydaylight-win64-shipping.exe": "Dead by Daylight",
}

# Programs Windows' game list may hold that are no game while they run (seen on the coach's PC: Java for a
# Minecraft client, game launchers, the League client) and programs that are never a game here.
NOT_GAMES = {
    "javaw.exe", "java.exe", "leagueclientux.exe", "leagueclient.exe", "riotclientservices.exe",
    "robloxstudiobeta.exe", "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "eadesktop.exe",
    "battle.net.exe", "upc.exe", "ubisoftconnect.exe", "explorer.exe", "obs64.exe", "pcs-scanner.exe",
    "python.exe", "pythonw.exe", "discord.exe", "crashreportclient.exe", "unitycrashhandler64.exe",
    "eaanticheat.gameservicelauncher.exe", "vconsole2.exe",
}
# The full-screen check (5) never counts these: browsers, video players, this app, Windows' own windows.
NOT_FULLSCREEN = NOT_GAMES | {
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "opera_gx.exe", "vivaldi.exe", "arc.exe",
    "vlc.exe", "mpc-hc64.exe", "mpc-hc.exe", "mpc-be64.exe", "mpv.exe", "potplayermini64.exe", "wmplayer.exe",
    "video.ui.exe", "microsoft.media.player.exe", "applicationframehost.exe", "spotify.exe", "netflix.exe",
    "msedgewebview2.exe", "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
    "lockapp.exe", "textinputhost.exe", "dwm.exe", "code.exe", "windowsterminal.exe",
}

_EXE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()+\-!]{0,79}\.exe", re.IGNORECASE)


def valid_exe(name) -> str | None:
    """A program name like "Deadlock.exe" (file name only), lower case; None otherwise. Apex is refused: it is
    always a game and the only one recorded."""
    n = Path(str(name or "").strip().replace("\\", "/")).name if "/" in str(name or "") or "\\" in str(name or "") \
        else str(name or "").strip()
    if not _EXE_RE.fullmatch(n) or n.lower() in APEX_EXES:
        return None
    return n.lower()


def exe_list(values, limit: int = 50) -> list[str]:
    """A saved list of program names, cleaned: valid names only, lower case, no repeats, at most `limit`."""
    out: list[str] = []
    for v in values if isinstance(values, (list, tuple)) else []:
        n = valid_exe(v)
        if n and n not in out:
            out.append(n)
    return out[:limit]


def pretty(exe: str, path: str = "") -> str:
    """The name shown for a program: the built-in name, else the program's name as Windows' list writes it (from
    `path`), without ".exe" and "-Win64-Shipping"."""
    e = exe.lower()
    if e in KNOWN:
        return KNOWN[e]
    if path:
        exe = path.replace("/", "\\").rsplit("\\", 1)[-1]
    stem = exe[:-4] if exe.lower().endswith(".exe") else exe
    return re.sub(r"[-_]win64[-_]shipping$", "", stem, flags=re.IGNORECASE) or stem


# ------------------------------------------------------------------ Windows reads (nothing else touches Windows)
_TOOLHELP = None


def _toolhelp():
    """kernel32's process snapshot functions, set up once."""
    global _TOOLHELP
    if _TOOLHELP is None:
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        _TOOLHELP = (k32, PROCESSENTRY32W)
    return _TOOLHELP


def process_names() -> dict[int, str]:
    """pid -> program name (lower case) of every running program: one snapshot of the process list. On Windows
    CreateToolhelp32Snapshot gives the names without opening any process; elsewhere psutil."""
    if sys.platform != "win32":
        import psutil
        out = {}
        for p in psutil.process_iter(["name"]):
            try:
                out[p.pid] = (p.info["name"] or "").lower()
            except psutil.Error:
                pass
        return out
    k32, PROCESSENTRY32W = _toolhelp()
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)               # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        raise OSError("process snapshot failed")
    out = {}
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[int(e.th32ProcessID)] = e.szExeFile.lower()
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def registry_games() -> dict[str, str]:
    """Windows' own list of games on this PC (Game Bar / Game Mode): exe name (lower case) -> full path."""
    if sys.platform != "win32":
        return {}
    import winreg
    out = {}
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"System\GameConfigStore\Children")
    except OSError:
        return {}
    with root:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, sub) as k:
                    path, _ = winreg.QueryValueEx(k, "MatchedExeFullPath")
            except OSError:
                continue
            if isinstance(path, str) and path.lower().endswith(".exe"):
                out[path.replace("/", "\\").rsplit("\\", 1)[-1].lower()] = path
    return out


def foreground_fullscreen() -> int | None:
    """The pid of the window in front when it covers its whole screen (a full-screen game, or a video), else None.
    Reads the window's size and owner only."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.GetForegroundWindow.restype = wintypes.HWND
    u32.GetShellWindow.restype = wintypes.HWND
    u32.MonitorFromWindow.restype = wintypes.HANDLE
    u32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
    u32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    u32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]
    u32.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.POINTER(MONITORINFO))
    hwnd = u32.GetForegroundWindow()
    if not hwnd or hwnd == u32.GetShellWindow():
        return None
    r = wintypes.RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    mon = u32.MonitorFromWindow(hwnd, 2)                    # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not mon or not u32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return None
    m = mi.rcMonitor
    if r.left > m.left or r.top > m.top or r.right < m.right or r.bottom < m.bottom:
        return None
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value) or None


# ------------------------------------------------------------------ the detector
class Detector:
    """Shared by every part of the app (`DETECTOR`): one process snapshot per `SNAPSHOT_S` for all of them."""

    def __init__(self, procs=process_names, registry=registry_games, foreground=foreground_fullscreen,
                 clock=time.monotonic):
        self.procs, self.registry, self.foreground, self.clock = procs, registry, foreground, clock
        self.pause = True              # Settings → Games → "Pause heavy work while any game runs"
        self.fullscreen = False        # also count a full-screen window in front (off: videos would count)
        self.extra: list[str] = []     # the player's own programs
        self.ignore: list[str] = []    # the player's "not a game" list
        self.lock = threading.Lock()
        self._snap: dict[int, str] = {}
        self._snap_at = float("-inf")
        self._reg: dict[str, str] = {}
        self._reg_at = float("-inf")
        self.last = ""                 # the other game seen at the last look ("" = none): read without a new look
        self.cost_ms = 0.0             # the last process snapshot's time (Settings shows nothing of it; logs, tests)

    def configure(self, s) -> None:
        """Take the player's settings (recorder Settings, or anything with the same fields)."""
        self.pause = bool(getattr(s, "game_pause", True))
        self.fullscreen = bool(getattr(s, "game_fullscreen", False))
        self.extra = exe_list(getattr(s, "game_exes", []))
        self.ignore = exe_list(getattr(s, "game_ignore", []))

    def names(self) -> dict[int, str]:
        """pid -> program name, from a snapshot at most `SNAPSHOT_S` old."""
        with self.lock:
            now = self.clock()
            if now - self._snap_at >= SNAPSHOT_S:
                t0 = time.perf_counter()
                try:
                    self._snap = self.procs()
                except Exception as e:
                    log.warning("process list not read: %s", e)
                    self._snap = {}
                self.cost_ms = (time.perf_counter() - t0) * 1000
                self._snap_at = now
            return self._snap

    def windows_games(self) -> dict[str, str]:
        """Windows' game list (exe -> path), read again every `REGISTRY_S`, launchers and the like left out."""
        with self.lock:
            now = self.clock()
            if now - self._reg_at >= REGISTRY_S:
                try:
                    reg = self.registry() or {}
                except Exception as e:
                    log.warning("Windows' game list not read: %s", e)
                    reg = {}
                self._reg = {k: v for k, v in reg.items()
                             if k not in NOT_GAMES and k not in APEX_EXES and "launcher" not in k}
                self._reg_at = now
            return self._reg

    def apex(self) -> bool:
        return any(n in APEX_EXES for n in self.names().values())

    def is_game(self, exe: str) -> bool:
        e = exe.lower()
        if e in self.ignore or e in APEX_EXES:
            return False
        return e in self.extra or e in KNOWN or e in self.windows_games()

    def other(self) -> str:
        """The name of a game other than Apex that runs now ("" = none, or the setting is off)."""
        if not self.pause:
            self.last = ""
            return ""
        names = self.names()
        found = ""
        for exe in sorted(set(names.values())):
            if self.is_game(exe):
                found = pretty(exe, self._reg.get(exe, ""))
                break
        if not found and self.fullscreen:
            try:
                pid = self.foreground()
            except Exception:
                pid = None
            exe = names.get(pid, "") if pid else ""
            if exe and exe not in NOT_FULLSCREEN and exe not in APEX_EXES and exe not in self.ignore:
                found = pretty(exe)
        self.last = found
        return found

    def holding(self) -> str:
        """What holds the heavy work now: "Apex", another game's name, or "" (nothing runs)."""
        return APEX if self.apex() else self.other()

    def public(self) -> dict:
        """Settings → Games: the switches, Windows' list on this PC, the built-in names, the player's lists, and
        which game runs now."""
        win = self.windows_games()
        running = set(self.names().values())
        rows = {}
        for exe in sorted(set(win) | set(self.extra) | {e for e in KNOWN if e in running}):
            rows[exe] = {"exe": exe, "name": pretty(exe, win.get(exe, "")), "running": exe in running,
                         "source": "yours" if exe in self.extra else ("built-in" if exe in KNOWN else "windows")}
        return {"pause": self.pause, "fullscreen": self.fullscreen, "now": self.last if self.pause else "",
                "games": sorted(rows.values(), key=lambda r: (r["source"] != "yours", r["name"].lower())),
                "ignored": [{"exe": e, "name": pretty(e, win.get(e, ""))} for e in self.ignore],
                "builtIn": sorted(set(KNOWN.values()))}


DETECTOR = Detector()


def configure(s) -> None:
    DETECTOR.configure(s)


def other_game() -> str:
    """A game other than Apex runs (and the player wants heavy work paused for it): its name, else ""."""
    return DETECTOR.other()


def apex_running() -> bool:
    return DETECTOR.apex()


def gaming_now() -> str:
    """The game seen at the last look, without a new one: for things that must cost nothing (corner messages)."""
    return DETECTOR.last
