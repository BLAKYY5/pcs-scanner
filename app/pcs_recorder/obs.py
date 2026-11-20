"""A private copy of OBS that only the PCS Scanner auto-recorder uses.

It lives in %LOCALAPPDATA%\\PCS Scanner\\obs in "portable mode", with its own settings, so a
player's own OBS (scenes, stream setup) is never touched, and players without OBS get it
installed for them. The recorder drives it through OBS's built-in remote control
(obs-websocket, port 4466 by default, local only, password generated per PC).
"""
from __future__ import annotations

import io
import json
import os
import re
import logging
import subprocess
import time
import zipfile
from pathlib import Path

import secrets

from .settings import APP_DIR, Settings, save, valid_exe

log = logging.getLogger("recorder.obs")
OBS_ROOT = APP_DIR / "obs"
OBS_EXE = OBS_ROOT / "bin" / "64bit" / "obs64.exe"
CONFIG = OBS_ROOT / "config" / "obs-studio"
APEX_WINDOW = "Apex Legends:Respawn001:r5apex_dx12.exe"   # matched by executable (priority 2)
SCENE, SOURCE, AUDIO = "PCS Apex", "Apex", "Apex audio"
DISCORD_WINDOW = "Discord:Chrome_WidgetWin_1:Discord.exe"  # matched by executable (priority 2)
TRACK_INPUTS = {"game": "PCS Game", "discord": "PCS Discord", "mic": "PCS Mic"}   # one source per track
VOICE_EXTRA = "PCS Voice 2"     # second voice source on track 2 (TeamSpeak: classic and new client)
TRACK_NAMES = {"game": "Game", "discord": "Discord", "mic": "Microphone"}
FILENAME = "Apex Legends_%CCYY.%MM.%DD-%hh.%mm"           # same naming the scanner already uses

# Hardware encoders, best first, per GPU vendor. CPU (x264) is deliberately not used: it costs FPS.
ENCODERS = {
    "amd": ["av1_texture_amf", "h265_texture_amf", "h264_texture_amf"],
    "nvidia": ["obs_nvenc_av1_tex", "jim_av1_nvenc", "obs_nvenc_hevc_tex", "jim_hevc_nvenc", "obs_nvenc_h264_tex", "jim_nvenc"],
    "intel": ["obs_qsv11_av1", "obs_qsv11_hevc", "obs_qsv11_v2"],
}


# ------------------------------------------------------------------ install

def installed() -> bool:
    return OBS_EXE.exists()


def install(progress=print) -> None:
    """Download the latest official OBS release (Windows zip) from GitHub and unpack it."""
    import requests
    progress("Finding the latest OBS release…")
    rel = requests.get("https://api.github.com/repos/obsproject/obs-studio/releases/latest", timeout=30).json()
    assets = [a for a in rel.get("assets", []) if a["name"].lower().endswith(".zip")
              and "x64" in a["name"].lower() and "windows" in a["name"].lower() or a["name"].endswith("Full-x64.zip")]
    assets = [a for a in assets if "pdb" not in a["name"].lower() and "arm" not in a["name"].lower()]
    if not assets:
        raise RuntimeError("Couldn't find the OBS Windows zip in the latest release.")
    a = assets[0]
    progress(f"Downloading {a['name']} ({a['size'] // 1_000_000} MB)…")
    data = requests.get(a["browser_download_url"], timeout=600).content
    progress("Unpacking…")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(OBS_ROOT)
    if not OBS_EXE.exists():   # some zips have a top folder
        inner = next((p for p in OBS_ROOT.iterdir() if (p / "bin" / "64bit" / "obs64.exe").exists()), None)
        if inner:
            for item in inner.iterdir():
                item.rename(OBS_ROOT / item.name)
            inner.rmdir()
    (OBS_ROOT / "portable_mode.txt").write_text("PCS Recorder\n")
    progress(f"OBS {rel.get('tag_name', '')} installed.")


# ------------------------------------------------------------------ config written before OBS starts

def gpu_names() -> str:
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
                              capture_output=True, text=True, timeout=20, creationflags=0x08000000).stdout.lower()
    except Exception:
        return ""


# Integrated graphics (the chip inside the CPU): names without a model number. Its encoder is a last resort: on a PC
# with a graphics card, OBS draws on the card, and the chip's encoder then falls back to a slow copy ("Wrong adapter").
_INTEGRATED = re.compile(r"radeon\(tm\) graphics|radeon graphics|radeon\(tm\) \d{3}m|radeon \d{3}m|vega \d+ graphics|"
                         r"uhd graphics|hd graphics|iris|intel\(r\) graphics|^intel graphics|"
                         r"microsoft basic|basic display|meta virtual|parsec|virtual display|remote display")
INTEGRATED_VRAM_GB = 1.5        # OBS's log: dedicated VRAM under this = integrated (0.5 GB on a Ryzen 7000)


def vendor_of(name: str) -> str:
    n = (name or "").lower()
    for v in ("nvidia", "amd", "intel"):
        if v in n or (v == "amd" and "radeon" in n):
            return v
    return ""


def is_integrated(name: str, vram_gb: float | None = None) -> bool:
    n = (name or "").lower().strip()
    if _INTEGRATED.search(n):
        return True
    return vram_gb is not None and vram_gb < INTEGRATED_VRAM_GB


def gpu_vendor(names: str | None = None) -> str:
    """The main graphics card's vendor: a card before integrated graphics (an RTX 5070 next to a Ryzen's Radeon
    chip = nvidia, whatever order Windows lists them in)."""
    out = gpu_names() if names is None else names
    lines = [x for x in out.lower().splitlines() if x.strip()] or [out.lower()]
    for want_card in (True, False):
        for line in lines:
            v = vendor_of(line)
            if v and is_integrated(line) != want_card:
                return v
    return "unknown"


def has_av1_encoder(names: str) -> bool:
    """AV1 hardware encoding exists only on recent cards: NVIDIA RTX 40/50, AMD RX 7000/9000, Intel Arc.
    Trying AV1 on older cards makes OBS show "couldn't start recording" during the encoder test."""
    n = names.lower()
    return bool(re.search(r"rtx\s*(pro\s*)?[45]0\d\d|rtx\s*[2-9]000\s*ada|rtx pro \d+ blackwell", n) or
                re.search(r"rx\s*[79]\d{3}", n) or "arc" in n)


def vendors_present(names: str) -> list[str]:
    n = names.lower()
    return [v for v in ("nvidia", "amd", "intel") if v in n or (v == "amd" and "radeon" in n)]


# ------------------------------------------------------------------ what OBS's own log says

LOGS = CONFIG / "logs"


def newest_log() -> Path | None:
    try:
        logs = sorted(LOGS.glob("*.txt"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    return logs[-1] if logs else None


def log_size(path: Path | None = None) -> int:
    path = path or newest_log()
    try:
        return path.stat().st_size if path else 0
    except OSError:
        return 0


def read_log(path: Path | None = None, start: int = 0) -> str:
    """The text of OBS's newest log (or `path`), from byte `start`; "" if there is none."""
    path = path or newest_log()
    if path is None:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(start)
            return f.read(4_000_000).decode("utf-8", "replace")
    except OSError:
        return ""


_RECORD_TRY = re.compile(r"==== Recording Start|Unable to open file '|'advanced_video_recording'\] settings|"
                         r"Wrong adapter")


class FolderError(RuntimeError):
    """OBS couldn't create the recording file in the recordings folder (or the app couldn't write there)."""


def last_recording_log(max_logs: int = 8) -> str:
    """Our OBS's newest log in which it tried to record ("" if none of the last few did): the encoder's and the
    output's lines of the last recording try. A later start without a recording (Check again) doesn't hide them."""
    try:
        logs = sorted(LOGS.glob("*.txt"), key=lambda p: p.stat().st_mtime)[-max_logs:]
    except OSError:
        return ""
    for p in reversed(logs):
        text = read_log(p)
        if _RECORD_TRY.search(text):
            return text
    return ""


def log_adapters(text: str) -> dict | None:
    """OBS's start-up lines "Adapter N: <name>" / "Dedicated VRAM: <bytes>" and "Loading up D3D11 on adapter <name> (N)"
    -> {"adapters": [{"index", "name", "vram_gb", "vendor", "integrated"}], "render": the index OBS draws on, or None}.
    None when the log has no adapter list (OBS not started yet)."""
    adapters, cur = [], None
    for line in (text or "").splitlines():
        m = re.search(r"\bAdapter (\d+): (.+?)\s*$", line)
        if m:
            cur = {"index": int(m.group(1)), "name": m.group(2), "vram_gb": None}
            adapters.append(cur)
            continue
        m = re.search(r"Dedicated VRAM:\s*(\d+)", line)
        if m and cur is not None and cur["vram_gb"] is None:
            cur["vram_gb"] = round(int(m.group(1)) / 1024 ** 3, 2)
    if not adapters:
        return None
    render = None
    for m in re.finditer(r"Loading up D3D11 on adapter .*\((\d+)\)", text):
        render = int(m.group(1))                     # the last start in the text wins
    by = {}
    for a in adapters:                               # one list per OBS start: keep the last of each index
        a["vendor"] = vendor_of(a["name"])
        a["integrated"] = is_integrated(a["name"], a["vram_gb"])
        by[a["index"]] = a
    return {"adapters": [by[i] for i in sorted(by)], "render": render}


def obs_adapters() -> dict | None:
    """The graphics chips as our OBS saw them at its last start (its newest log), or None."""
    return log_adapters(read_log())


def render_vendor(adapters: dict | None) -> str:
    """The vendor of the chip OBS draws on ("" when unknown)."""
    for a in (adapters or {}).get("adapters") or []:
        if a["index"] == (adapters or {}).get("render"):
            return a.get("vendor") or ""
    return ""


# "Wrong adapter": AMD's texture encoder on a chip OBS doesn't draw on; it then falls back to a slow copy through the
# CPU ("fallback-amf-…"). NVIDIA / Intel say "falling back" in the same case. Such an encoder records, but it is never
# the right one when the card OBS draws on has its own.
_WRONG_CHIP = re.compile(r"wrong adapter|\[fallback-|falling back to (?:non-texture|software|ffmpeg)|"
                         r"fallback to (?:non-texture|software)", re.I)


def wrong_chip(text: str) -> str:
    """The first log line saying the encoder isn't running on the chip OBS draws on, else ""."""
    m = _WRONG_CHIP.search(text or "")
    if not m:
        return ""
    a, b = text.rfind("\n", 0, m.start()) + 1, text.find("\n", m.end())
    return text[a:b if b >= 0 else None].strip()[:200]


def unable_to_open(text: str) -> str:
    """OBS couldn't create the recording file: the folder it was given (as the log writes it), else "".
    Only a failure after the last successful recording start in the text counts."""
    last = None
    for m in re.finditer(r"Unable to open file '([^'\n]+)'|==== Recording Start", text or ""):
        last = m.group(1) if m.group(1) else None
    return str(Path(last).parent) if last else ""


def encoder_candidates(vendor: str, names: str = "", adapters: dict | None = None) -> list[str]:
    """Only the encoders of graphics chips this PC has: OBS shows a "missing encoder" box for any other one.
    Order: the chip OBS draws on (its log; else the main card), then other cards, integrated graphics last. (An RTX
    5070 next to a Ryzen 7800X3D's Radeon chip: NVENC first, AMD's encoder only when no NVENC works.)"""
    present = vendors_present(names) if names else []
    if vendor in ENCODERS and vendor not in present:
        present.insert(0, vendor)
    listed = (adapters or {}).get("adapters") or []
    for a in listed:
        if a.get("vendor") and a["vendor"] not in present:
            present.append(a["vendor"])
    if listed:
        cards = {a["vendor"] for a in listed if a.get("vendor") and not a["integrated"]}
        integrated = {a["vendor"] for a in listed if a.get("vendor") and a["integrated"]} - cards
    else:
        lines = [x for x in names.lower().splitlines() if x.strip()]
        cards = {vendor_of(x) for x in lines if not is_integrated(x)}
        integrated = {vendor_of(x) for x in lines if is_integrated(x)} - cards
    render = render_vendor(adapters)
    if not present:
        present = list(ENCODERS)                       # unknown card: try everything
    present = [v for v in present if v in ENCODERS]
    present.sort(key=lambda v: (v in integrated, bool(render) and v != render, v != vendor))
    # AV1 per vendor: an RTX 5070 has it, the Radeon chip next to it doesn't (OBS would fail with an error box)
    lines = [x for x in (names.lower().splitlines() + [a["name"].lower() for a in listed]) if x.strip()]
    av1 = {v for v in present if has_av1_encoder("\n".join(x for x in lines if vendor_of(x) == v))}
    return [e for v in present for e in ENCODERS[v] if "av1" not in e or v in av1]


HYBRID_MP4_SINCE = (30, 2)     # OBS 30.2 added "hybrid MP4": an mp4 that stays readable if OBS stops abruptly


def exe_version(path: Path = None) -> tuple[int, ...]:
    """OBS's version from obs64.exe's file information (before OBS runs), e.g. (33, 0, 1); () if unknown."""
    path = path or OBS_EXE
    try:
        import ctypes
        v = ctypes.windll.version
        size = v.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ()
        buf = ctypes.create_string_buffer(size)
        if not v.GetFileVersionInfoW(str(path), 0, size, buf):
            return ()
        p, n = ctypes.c_void_p(), ctypes.c_uint()
        if not v.VerQueryValueW(buf, "\\", ctypes.byref(p), ctypes.byref(n)) or n.value < 52:
            return ()
        ms, ls = ctypes.cast(p, ctypes.POINTER(ctypes.c_uint32))[2:4]        # VS_FIXEDFILEINFO dwFileVersionMS / LS
        return (ms >> 16, ms & 0xFFFF, ls >> 16)
    except Exception:
        return ()


def rec_format(version: tuple[int, ...] | None = None) -> str:
    """What OBS records into. Hybrid MP4 (OBS 30.2+): the file is an .mp4 from the start, so there is no copy to .mp4
    after the session (the remux read and wrote the whole recording again, minutes for a long session on a hard
    disk) and nothing to wait for before the scan. Older OBS: .mkv, remuxed to .mp4 as before."""
    version = exe_version() if version is None else version
    return "hybrid_mp4" if version and tuple(version[:2]) >= HYBRID_MP4_SINCE else "mkv"


def rec_tracks(s: Settings) -> int:
    """OBS's track bitmask: one track per recorded source (track 1 = first of s.tracks())."""
    return (1 << max(1, len(s.tracks()))) - 1


def encoder_settings(encoder: str, mbps: int) -> dict:
    """CBR (predictable file sizes for the folder limit), keyframe every second (the scanner seeks on keyframes),
    and a quality preset in each vendor's own words (a preset name another vendor uses can stop the encoder)."""
    kbps = mbps * 1000
    base = {"rate_control": "CBR", "bitrate": kbps, "keyint_sec": 1}
    if "amf" in encoder:
        return base | {"preset": "quality", "max_bitrate": kbps, "buffer_size": kbps, "bf": 0}
    if "nvenc" in encoder:
        return base | {"preset2": "p5", "tune": "hq", "multipass": "qres", "bf": 0}
    if "qsv" in encoder:
        return base | {"target_usage": "TU4", "bframes": 0}
    return base


def renew_password(s: Settings) -> bool:
    """Once per PC: a new random remote-control password. Up to 1.10.10 the OBS library wrote the password into
    app.log; the old one is useless once OBS runs with a new one. Only while our OBS is closed (so never during a
    recording), right before write_config() gives OBS the new one; the recorder connects with s.obs_password.
    -> True when it was renewed now."""
    if s.obs_password_new or is_running():
        return False
    s.obs_password = secrets.token_urlsafe(16)
    s.obs_password_new = True
    save(s)
    log.info("remote-control password renewed")
    return True


def write_config(s: Settings, encoder: str, width: int, height: int) -> None:
    ws = CONFIG / "plugin_config" / "obs-websocket"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "config.json").write_text(json.dumps({
        "alerts_enabled": False, "auth_required": True, "first_load": False,
        "server_enabled": True, "server_port": s.obs_port, "server_password": s.obs_password}, indent=2))
    prof = CONFIG / "basic" / "profiles" / "PCS"
    prof.mkdir(parents=True, exist_ok=True)
    folder = str(s.folder_path).replace("\\", "/")
    (prof / "basic.ini").write_text("\n".join([
        "[General]", "Name=PCS", "",
        "[Output]", "Mode=Advanced", f"FilenameFormatting={FILENAME}", "",
        "[AdvOut]", "RecType=Standard", f"RecFilePath={folder}", f"RecFormat2={rec_format()}",
        f"RecEncoder={encoder}", f"RecTracks={rec_tracks(s)}",
        *[f"Track{i}Name={TRACK_NAMES[t]}" for i, t in enumerate(s.tracks(), 1)],
        *[f"Track{i}Bitrate=160" for i in range(1, 4)], "",
        "[Video]", f"BaseCX={width}", f"BaseCY={height}", f"OutputCX={width}", f"OutputCY={height}",
        "FPSType=1", "FPSInt=60", "AutoRemux=true", ""]), encoding="utf-8")
    # constant bitrate (predictable file sizes for the folder limit), keyframe every second
    # (the scanner seeks on keyframes). CBR and these keys are understood by AMD, NVIDIA and Intel encoders.
    (prof / "recordEncoder.json").write_text(json.dumps(encoder_settings(encoder, s.mbps_for(encoder))))
    gl = CONFIG / "global.ini"
    gl.write_text("\n".join([
        "[General]", "FirstRun=true", "EnableAutoUpdates=false", "",
        "[Basic]", "Profile=PCS", "ProfileDir=PCS", "SceneCollection=PCS", "SceneCollectionFile=PCS", "",
        "[BasicWindow]", "SysTrayEnabled=true", "SysTrayWhenStarted=true", "SysTrayMinimizeToTray=true",
        "WarnBeforeStartingRecord=false", "WarnBeforeStoppingRecord=false", ""]), encoding="utf-8")


# ------------------------------------------------------------------ running OBS

MIC_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
PKEY_DESC = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"      # "Microphone", "Headset Microphone"…
PKEY_IFACE = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"     # "Blue Yeti", "Realtek(R) Audio"…


def mic_devices() -> list[dict]:
    """The PC's microphones (plugged in and enabled), as OBS names them: [{id, name}].
    Read from the registry, where Windows lists its audio devices (no extra library needed)."""
    try:
        import winreg
    except ImportError:
        return []
    out = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, MIC_KEY)
    except OSError:
        return []
    with root:
        i = 0
        while True:
            try:
                guid = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, guid) as k:
                    if winreg.QueryValueEx(k, "DeviceState")[0] != 1:      # 1 = active
                        continue
                with winreg.OpenKey(root, guid + r"\Properties") as k:
                    desc = _reg_str(winreg, k, PKEY_DESC)
                    iface = _reg_str(winreg, k, PKEY_IFACE)
            except OSError:
                continue
            name = f"{desc} ({iface})" if desc and iface else (desc or iface or guid)
            out.append({"id": "{0.0.1.00000000}." + guid.lower(), "name": name})
    return sorted(out, key=lambda d: d["name"].lower())


def _reg_str(winreg, key, name: str) -> str:
    try:
        v = winreg.QueryValueEx(key, name)[0]
        return v if isinstance(v, str) else ""
    except OSError:
        return ""


def is_running() -> bool:
    import psutil
    for p in psutil.process_iter(["exe"]):
        try:
            if p.info["exe"] and Path(p.info["exe"]) == OBS_EXE:
                return True
        except (psutil.Error, OSError):
            pass
    return False


def voice_window(exe: str) -> str:
    """OBS's "title:class:program" window text for an application audio source.
    Discord keeps its exact text (unchanged for existing installs). For other programs the title and class
    aren't known in advance: they are left empty, and with priority 2 ("match title, otherwise find window of
    same executable") OBS takes any window of that program. OBS writes ':' as '#3A' and '#' as '#22'."""
    if exe.lower() == "discord.exe":
        return DISCORD_WINDOW
    return "::" + exe.replace("#", "#22").replace(":", "#3A")


def voice_sources(s: Settings) -> list[tuple[str, dict]]:
    """Track 2's OBS sources: (input name, settings), one per voice program. The first keeps the name
    "PCS Discord" (saved OBS setups and the mute hotkey use it)."""
    names = [TRACK_INPUTS["discord"], VOICE_EXTRA]
    return [(names[i], {"window": voice_window(exe), "priority": 2}) for i, exe in enumerate(s.voice_exes()[:2])]


NOT_LISTED = {"textinputhost.exe", "msedgewebview2.exe", "applicationframehost.exe", "systemsettings.exe",
              "shellexperiencehost.exe", "searchhost.exe", "startmenuexperiencehost.exe", "python.exe", "pythonw.exe"}


def window_apps() -> list[dict]:
    """Programs running now with a visible, titled window: [{exe, title}], one per program, for picking a
    voice app. Only names are read (no paths are kept or used)."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes
    import psutil
    u = ctypes.windll.user32
    pids: dict[int, str] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if u.IsWindowVisible(hwnd) and not u.GetWindow(hwnd, 4):          # 4 = GW_OWNER: top-level windows only
            n = u.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, buf, n + 1)
                pid = wintypes.DWORD()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                pids.setdefault(pid.value, buf.value)
        return True
    u.EnumWindows(each, 0)
    out: dict[str, dict] = {}
    for pid, title in pids.items():
        try:
            exe = psutil.Process(pid).name()
        except (psutil.Error, OSError):
            continue
        if exe and exe.lower() not in out and exe.lower() not in NOT_LISTED and valid_exe(exe):
            out[exe.lower()] = {"exe": exe, "title": title[:80]}
    return sorted(out.values(), key=lambda d: d["exe"].lower())


def config_signature(s: Settings) -> str:
    """What needs an OBS restart when it changes (OBS reads encoder settings only at start-up).
    The voice app is added only when it isn't Discord, so existing installs keep the same signature."""
    sig = f"{s.encoder}|{s.mbps_for(s.encoder)}|{s.folder}|{','.join(s.tracks())}|{s.mic_device}"
    if s.voice_app != "discord":
        sig += "|voice=" + "+".join(s.voice_exes())
    return sig


def close() -> None:
    """Close the private OBS (only between games, never while it records)."""
    import psutil
    for p in psutil.process_iter(["exe"]):
        try:
            if p.info["exe"] and Path(p.info["exe"]) == OBS_EXE:
                p.terminate()
                p.wait(15)
        except (psutil.Error, OSError):
            pass


def launch() -> None:
    if is_running():
        return
    subprocess.Popen([str(OBS_EXE), "--portable", "--multi", "--minimize-to-tray", "--disable-shutdown-check",
                      "--disable-updater", "--profile", "PCS", "--collection", "PCS"],
                     cwd=OBS_EXE.parent, creationflags=0x08000000)


class OBS:
    """Thin wrapper around the remote control client."""

    def __init__(self, s: Settings):
        self.s = s
        self.c = None

    def connect(self, wait_s: float = 40) -> None:
        import obsws_python as obsws
        end = time.time() + wait_s
        last = None
        while time.time() < end:
            try:
                self.c = obsws.ReqClient(host="127.0.0.1", port=self.s.obs_port, password=self.s.obs_password, timeout=5)
                self.c.get_version()
                return
            except Exception as e:
                last = e
                time.sleep(1.5)
        raise RuntimeError(f"OBS didn't answer on port {self.s.obs_port}: {last}")

    def prepare(self, width: int, height: int, encoder: str | None = None) -> None:
        """Make sure the PCS scene, the Apex source and the output settings are in place."""
        c, s = self.c, self.s
        c.set_video_settings(60, 1, width, height, width, height)
        c.set_record_directory(str(s.folder_path))
        c.set_profile_parameter("Output", "Mode", "Advanced")
        c.set_profile_parameter("Output", "FilenameFormatting", FILENAME)
        c.set_profile_parameter("AdvOut", "RecFormat2", rec_format(self.version()))
        c.set_profile_parameter("Video", "AutoRemux", "true")
        if encoder:
            c.set_profile_parameter("AdvOut", "RecEncoder", encoder)
        if SCENE not in [x["sceneName"] for x in c.get_scene_list().scenes]:
            c.create_scene(SCENE)
        c.set_current_program_scene(SCENE)
        ours = (SOURCE, AUDIO, *TRACK_INPUTS.values(), VOICE_EXTRA)
        names = {i["inputName"] for i in c.get_input_list().inputs}
        for n in ours:
            if n in names:
                c.remove_input(n)
        # OBS removes sources a moment later, not at once: creating one with the same name before that fails
        # ("a source with that name already exists"), which stopped the whole encoder test on some PCs
        end = time.time() + 10
        while time.time() < end and {i["inputName"] for i in c.get_input_list().inputs} & set(ours):
            time.sleep(0.2)
        kind, settings = {
            "window": ("window_capture", {"window": APEX_WINDOW, "priority": 2, "method": 2, "cursor": False,
                                          "capture_audio": False}),
            "game": ("game_capture", {"capture_mode": "window", "window": APEX_WINDOW, "priority": 2,
                                      "capture_cursor": False, "capture_audio": False}),
            "display": ("monitor_capture", {"capture_cursor": False}),
        }[s.capture]
        c.create_input(SCENE, SOURCE, kind, settings, True)
        # sound: one source per track, each recorded on its own track only
        #   game    = Apex's sound only (application audio capture), never the rest of the PC
        #   discord = the voice app's sound only (Discord, TeamSpeak or the program picked; key kept from when
        #             it was always Discord);  mic = the microphone picked in the app (Windows default if none)
        proc = "wasapi_process_output_capture"
        sources = {
            "game": [(TRACK_INPUTS["game"], "wasapi_process_output_capture", {"window": APEX_WINDOW, "priority": 2})],
            "discord": [(n, proc, st) for n, st in voice_sources(s)],
            "mic": [(TRACK_INPUTS["mic"], "wasapi_input_capture", {"device_id": s.mic_device or "default"})],
        }
        tracks = s.tracks()
        self.inputs: dict[str, list[str]] = {}
        for i, t in enumerate(tracks, 1):
            for name, kind, st in sources[t]:
                c.create_input(SCENE, name, kind, st, True)
                c.set_input_audio_tracks(name, {str(n): n == i for n in range(1, 7)})
                c.set_input_mute(name, False)
                self.inputs.setdefault(t, []).append(name)
        # OBS's own default sources (whole-PC sound, default mic) are never used
        special = c.get_special_inputs()
        for attr in ("desktop1", "desktop2", "mic1", "mic2", "mic3", "mic4"):
            name = getattr(special, attr, None)
            if name:
                try:
                    c.set_input_mute(name, True)
                except Exception:
                    pass
        c.set_profile_parameter("AdvOut", "RecTracks", str(rec_tracks(s)))
        self.tracks = tracks

    def version(self) -> tuple[int, ...]:
        """The running OBS's version (e.g. (33, 0, 1)); () if it can't be read."""
        try:
            return tuple(int(x) for x in re.findall(r"\d+", self.c.get_version().obs_version)[:3])
        except Exception:
            return ()

    def mute(self, track: str, muted: bool) -> bool:
        if track not in getattr(self, "tracks", []):
            return False
        for name in getattr(self, "inputs", {}).get(track) or [TRACK_INPUTS[track]]:
            self.c.set_input_mute(name, muted)
        return True

    def elapsed(self) -> float | None:
        """Seconds into the current recording (exact, from OBS)."""
        try:
            st = self.c.get_record_status()
            return st.output_duration / 1000 if st.output_active else None
        except Exception:
            return None

    def start(self) -> None:
        if not self.c.get_record_status().output_active:
            self.c.start_record()

    def stop(self) -> str | None:
        st = self.c.get_record_status()
        if not st.output_active:
            return None
        return self.c.stop_record().output_path

    def recording(self) -> bool:
        return bool(self.c.get_record_status().output_active)


def pick_encoder(s: Settings, width: int, height: int, progress=print) -> str:
    """First-run self test: try encoders until one actually records (2-second test file, then deleted).
    OBS only reads the encoder when it starts, so it is restarted with each candidate's settings
    (changing the encoder while OBS runs has no effect: every test would use the first one).
    The order follows the chip OBS draws on, read from OBS's own log once it has started (Windows' list alone put a
    Ryzen's Radeon chip before an RTX 5070, 20 Nov 2026). An encoder whose test shows in OBS's log that it runs on
    another chip ("Wrong adapter", a fallback encoder) is skipped, and kept only as a last resort."""
    names = gpu_names()
    vendor = gpu_vendor(names)
    adapters = obs_adapters()
    tried, why = [], []
    second_best = ""                                     # recorded, but on the wrong chip
    while True:
        todo = [e for e in encoder_candidates(vendor, names, adapters) if e not in tried]
        if not todo:
            break
        enc = todo[0]
        tried.append(enc)
        step = "starting OBS"
        try:
            close()
            write_config(s, enc, width, height)
            launch()
            obs = OBS(s)
            step = "connecting to OBS"
            obs.connect()
            seen = obs_adapters()
            if seen and seen != adapters:                # OBS's own list (first start): maybe another order
                adapters = seen
                better = [e for e in encoder_candidates(vendor, names, adapters) if e not in tried or e == enc]
                if better and better[0] != enc:
                    tried.remove(enc)
                    log.info("OBS draws on %s: encoder order now %s", render_vendor(adapters) or "?", better)
                    continue
            step = "setting up the scene"
            obs.prepare(width, height, enc)
            step = "starting the test recording"
            at = newest_log()
            mark = log_size(at)
            obs.c.start_record()
            time.sleep(3)
            if obs.recording():
                path = obs.stop()
                time.sleep(2)
                for f in Path(path or "").parent.glob(Path(path or "x").stem + "*") if path else []:
                    f.unlink(missing_ok=True)
                bad = wrong_chip(read_log(at, mark)) if at is not None and at == newest_log() else ""
                if bad:
                    log.info("encoder %s runs on another chip than OBS draws on: %s", enc, bad)
                    why.append(f"{enc}: runs on the wrong graphics chip")
                    second_best = second_best or enc
                    continue
                progress(f"Recording with {enc} ({vendor} graphics card).")
                return enc
            log.info("encoder %s didn't start", enc)
            why.append(f"{enc}: OBS couldn't start recording")
        except Exception as e:
            log.info("encoder %s not usable (%s): %s", enc, step, e)
            why.append(f"{enc}: {step} failed ({e})")
    if second_best:                                      # better a slow encoder than no recording
        log.warning("only %s recorded, on another chip than OBS draws on (tried %s)", second_best, tried)
        close()
        write_config(s, second_best, width, height)
        launch()
        progress(f"Recording with {second_best} ({vendor} graphics card).")
        return second_best
    close()                                                # also closes any error box OBS left open
    log.warning("no encoder worked on %r (tried %s)", names, tried)
    raise RuntimeError(f"No hardware encoder worked on this PC ({names.strip() or 'unknown graphics card'}): "
                       + "; ".join(why) + ". Send your coach this message and the OBS logs (Settings → About → Recorder logs).")
