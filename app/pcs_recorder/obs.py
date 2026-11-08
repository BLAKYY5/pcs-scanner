"""A private copy of OBS that only the PCS Scanner auto-recorder uses.

It lives in %LOCALAPPDATA%\\PCS Scanner\\obs in "portable mode", with its own settings, so a
player's own OBS (scenes, stream setup) is never touched, and players without OBS get it
installed for them. The recorder drives it through OBS's built-in remote control
(obs-websocket, port 4466 by default, local only, password generated per PC).
"""
from __future__ import annotations

import io
import json
import re
import logging
import subprocess
import time
import zipfile
from pathlib import Path

from .settings import APP_DIR, Settings

log = logging.getLogger("recorder.obs")
OBS_ROOT = APP_DIR / "obs"
OBS_EXE = OBS_ROOT / "bin" / "64bit" / "obs64.exe"
CONFIG = OBS_ROOT / "config" / "obs-studio"
APEX_WINDOW = "Apex Legends:Respawn001:r5apex_dx12.exe"   # matched by executable (priority 2)
SCENE, SOURCE, AUDIO = "PCS Apex", "Apex", "Apex audio"
DISCORD_WINDOW = "Discord:Chrome_WidgetWin_1:Discord.exe"  # matched by executable (priority 2)
TRACK_INPUTS = {"game": "PCS Game", "discord": "PCS Discord", "mic": "PCS Mic"}   # one source per track
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


def gpu_vendor(names: str | None = None) -> str:
    out = gpu_names() if names is None else names
    for v in ("nvidia", "amd", "intel"):
        if v in out or (v == "amd" and "radeon" in out):
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


def encoder_candidates(vendor: str, names: str = "") -> list[str]:
    """Only the encoders of graphics chips this PC has: OBS shows a "missing encoder" box for any other one.
    (A PC with two, e.g. Intel + NVIDIA, gets both, the main card's first.)"""
    present = vendors_present(names) if names else []
    if vendor in ENCODERS and vendor not in present:
        present.insert(0, vendor)
    if not present:
        present = list(ENCODERS)                       # unknown card: try everything
    present.sort(key=lambda v: v != vendor)
    order = [e for v in present for e in ENCODERS[v]]
    if not has_av1_encoder(names):
        order = [e for e in order if "av1" not in e]
    return order


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
        "[AdvOut]", "RecType=Standard", f"RecFilePath={folder}", "RecFormat2=mkv",
        f"RecEncoder={encoder}", f"RecTracks={rec_tracks(s)}",
        *[f"Track{i}Name={TRACK_NAMES[t]}" for i, t in enumerate(s.tracks(), 1)],
        *[f"Track{i}Bitrate=160" for i in range(1, 4)], "",
        "[Video]", f"BaseCX={width}", f"BaseCY={height}", f"OutputCX={width}", f"OutputCY={height}",
        "FPSType=1", "FPSInt=60", "AutoRemux=true", ""]), encoding="utf-8")
    # constant bitrate (predictable file sizes for the folder limit), keyframe every second
    # (the scanner seeks on keyframes). CBR and these keys are understood by AMD, NVIDIA and Intel encoders.
    (prof / "recordEncoder.json").write_text(json.dumps(encoder_settings(encoder, s.bitrate_mbps)))
    gl = CONFIG / "global.ini"
    gl.write_text("\n".join([
        "[General]", "FirstRun=true", "EnableAutoUpdates=false", "",
        "[Basic]", "Profile=PCS", "ProfileDir=PCS", "SceneCollection=PCS", "SceneCollectionFile=PCS", "",
        "[BasicWindow]", "SysTrayEnabled=true", "SysTrayWhenStarted=true", "SysTrayMinimizeToTray=true",
        "WarnBeforeStartingRecord=false", "WarnBeforeStoppingRecord=false", ""]), encoding="utf-8")


# ------------------------------------------------------------------ running OBS

def is_running() -> bool:
    import psutil
    for p in psutil.process_iter(["exe"]):
        try:
            if p.info["exe"] and Path(p.info["exe"]) == OBS_EXE:
                return True
        except (psutil.Error, OSError):
            pass
    return False


def config_signature(s: Settings) -> str:
    """What needs an OBS restart when it changes (OBS reads encoder settings only at start-up)."""
    return f"{s.encoder}|{s.bitrate_mbps}|{s.folder}|{','.join(s.tracks())}"


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
        c.set_profile_parameter("AdvOut", "RecFormat2", "mkv")
        c.set_profile_parameter("Video", "AutoRemux", "true")
        if encoder:
            c.set_profile_parameter("AdvOut", "RecEncoder", encoder)
        if SCENE not in [x["sceneName"] for x in c.get_scene_list().scenes]:
            c.create_scene(SCENE)
        c.set_current_program_scene(SCENE)
        ours = (SOURCE, AUDIO, *TRACK_INPUTS.values())
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
        #   discord = Discord's sound only;  mic = the default microphone
        sources = {
            "game": ("wasapi_process_output_capture", {"window": APEX_WINDOW, "priority": 2}),
            "discord": ("wasapi_process_output_capture", {"window": DISCORD_WINDOW, "priority": 2}),
            "mic": ("wasapi_input_capture", {"device_id": "default"}),
        }
        tracks = s.tracks()
        for i, t in enumerate(tracks, 1):
            name = TRACK_INPUTS[t]
            c.create_input(SCENE, name, *sources[t], True)
            c.set_input_audio_tracks(name, {str(n): n == i for n in range(1, 7)})
            c.set_input_mute(name, False)
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

    def mute(self, track: str, muted: bool) -> bool:
        if track not in getattr(self, "tracks", []):
            return False
        self.c.set_input_mute(TRACK_INPUTS[track], muted)
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
    (changing the encoder while OBS runs has no effect: every test would use the first one)."""
    names = gpu_names()
    vendor = gpu_vendor(names)
    tried, why = [], []
    for enc in encoder_candidates(vendor, names):
        tried.append(enc)
        step = "starting OBS"
        try:
            close()
            write_config(s, enc, width, height)
            launch()
            obs = OBS(s)
            step = "connecting to OBS"
            obs.connect()
            step = "setting up the scene"
            obs.prepare(width, height, enc)
            step = "starting the test recording"
            obs.c.start_record()
            time.sleep(3)
            if obs.recording():
                path = obs.stop()
                time.sleep(2)
                for f in Path(path or "").parent.glob(Path(path or "x").stem + "*") if path else []:
                    f.unlink(missing_ok=True)
                progress(f"Recording with {enc} ({vendor} graphics card).")
                return enc
            log.info("encoder %s didn't start", enc)
            why.append(f"{enc}: OBS couldn't start recording")
        except Exception as e:
            log.info("encoder %s not usable (%s): %s", enc, step, e)
            why.append(f"{enc}: {step} failed ({e})")
    close()                                                # also closes any error box OBS left open
    log.warning("no encoder worked on %r (tried %s)", names, tried)
    raise RuntimeError(f"No hardware encoder worked on this PC ({names.strip() or 'unknown graphics card'}): "
                       + "; ".join(why) + ". Send Blakey this message and the OBS logs (Settings → About → Recorder logs).")
