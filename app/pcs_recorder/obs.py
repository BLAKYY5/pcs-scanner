"""A private copy of OBS that only the PCS Scanner auto-recorder uses.

It lives in %LOCALAPPDATA%\\PCS Scanner\\obs in "portable mode", with its own settings, so a
player's own OBS (scenes, stream setup) is never touched, and players without OBS get it
installed for them. The recorder drives it through OBS's built-in remote control
(obs-websocket, port 4466 by default, local only, password generated per PC).
"""
from __future__ import annotations

import io
import json
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

def gpu_vendor() -> str:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-CimInstance Win32_VideoController).Name"],
                             capture_output=True, text=True, timeout=20, creationflags=0x08000000).stdout.lower()
    except Exception:
        return "unknown"
    for v in ("nvidia", "amd", "intel"):
        if v in out or (v == "amd" and "radeon" in out):
            return v
    return "unknown"


def encoder_candidates(vendor: str) -> list[str]:
    return ENCODERS.get(vendor, []) + [e for v, l in ENCODERS.items() if v != vendor for e in l]


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
        f"RecEncoder={encoder}", "RecTracks=1", "",
        "[Video]", f"BaseCX={width}", f"BaseCY={height}", f"OutputCX={width}", f"OutputCY={height}",
        "FPSType=1", "FPSInt=60", "AutoRemux=true", ""]), encoding="utf-8")
    # constant bitrate (predictable file sizes for the folder limit), keyframe every second
    # (the scanner seeks on keyframes). CBR and these keys are understood by AMD, NVIDIA and Intel encoders.
    kbps = s.bitrate_mbps * 1000
    (prof / "recordEncoder.json").write_text(json.dumps({
        "rate_control": "CBR", "bitrate": kbps, "max_bitrate": kbps, "buffer_size": kbps,
        "keyint_sec": 1, "preset": "quality", "bf": 0}))
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
    return f"{s.encoder}|{s.bitrate_mbps}|{s.folder}"


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
        names = {i["inputName"] for i in c.get_input_list().inputs}
        for n in (SOURCE, AUDIO):
            if n in names:
                c.remove_input(n)
        kind, settings = {
            "window": ("window_capture", {"window": APEX_WINDOW, "priority": 2, "method": 2, "cursor": False,
                                          "capture_audio": s.game_audio}),
            "game": ("game_capture", {"capture_mode": "window", "window": APEX_WINDOW, "priority": 2,
                                      "capture_cursor": False, "capture_audio": s.game_audio}),
            "display": ("monitor_capture", {"capture_cursor": False}),
        }[s.capture]
        c.create_input(SCENE, SOURCE, kind, settings, True)
        if s.capture == "display" and s.game_audio:
            c.create_input(SCENE, AUDIO, "wasapi_process_output_capture", {"window": APEX_WINDOW, "priority": 2}, True)
        # never record the microphone or the rest of the PC's sound
        special = c.get_special_inputs()
        for attr in ("desktop1", "desktop2", "mic1", "mic2", "mic3", "mic4"):
            name = getattr(special, attr, None)
            if name:
                try:
                    c.set_input_mute(name, True)
                except Exception:
                    pass

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
    """First-run self test: try encoders until one actually records (2-second test file, then deleted)."""
    vendor = gpu_vendor()
    obs = OBS(s)
    obs.connect()
    for enc in encoder_candidates(vendor):
        try:
            obs.prepare(width, height, enc)
            obs.c.start_record()
            time.sleep(2.5)
            if obs.recording():
                path = obs.stop()
                time.sleep(2)
                for f in Path(path or "").parent.glob(Path(path or "x").stem + "*") if path else []:
                    f.unlink(missing_ok=True)
                progress(f"Recording with {enc} ({vendor} GPU).")
                return enc
        except Exception as e:
            log.info("encoder %s not usable: %s", enc, e)
    raise RuntimeError("No hardware encoder worked. Update the graphics driver and try again.")
