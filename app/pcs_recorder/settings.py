"""Auto-record settings, saved next to the app's other settings:
%LOCALAPPDATA%\\PCS Scanner\\recorder.json"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_DIR = Path(os.environ.get("PCS_DATA_DIR")
               or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PCS Scanner")
SETTINGS_FILE = APP_DIR / "recorder.json"
MIN_MAX_GB = 20          # below this a single scrim block may not fit
MAX_KEEP_DAYS = 90
MIN_MBPS, MAX_MBPS = 8, 60   # 8 = the floor that keeps HUD text readable (Twitch VODs are 8-10)


@dataclass
class Settings:
    enabled: bool = False                              # auto-record on/off (turned on from the app window)
    start_with_windows: bool = True                    # app starts in the tray when Windows starts
    player: str = ""                                   # same EEC name as in the app
    hub_url: str = ""                                  # team hub, e.g. http://pcs-hub:8420 (empty = Drive only)
    folder: str = str(Path.home() / "Videos" / "PCS Recordings")
    keep_days: int = 7                                 # delete recordings older than this
    max_gb: int = 100                                  # the recorder's files never take more than this
    bitrate_mbps: int = 12                             # constant bitrate; 12 Mbit/s ~ 5.4 GB per hour
    game_audio: bool = True                            # track 1: Apex's own sound only
    comms_consent: bool = False                        # the team agreed that voice comms are recorded
    discord_audio: bool = True                         # track 2: Discord only (needs comms_consent)
    mic_audio: bool = True                             # track 3: the player's microphone (needs comms_consent)
    send_mic: bool = True                              # mic track goes to the team with the scan results
    hk_mute_game: str = "ctrl+alt+1"                   # hotkeys while recording ("" = none)
    hk_mute_discord: str = "ctrl+alt+2"
    hk_mute_mic: str = "ctrl+alt+3"
    hk_mark: str = "ctrl+alt+m"                        # "mark this moment" for the review
    hk_sounds: bool = True                             # short beep when a hotkey is used
    auto_scan: bool = True                             # scan after Apex closes unless the player clicks Skip
    scan_delay_s: int = 60                             # countdown before the scan starts on its own
    capture: str = "window"                            # window | game | display
    obs_port: int = 4466
    obs_password: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    encoder: str = ""                                  # found by the first-run encoder test

    def clamp(self) -> "Settings":
        self.keep_days = max(1, min(MAX_KEEP_DAYS, int(self.keep_days)))
        self.max_gb = max(MIN_MAX_GB, int(self.max_gb))
        self.bitrate_mbps = max(MIN_MBPS, min(MAX_MBPS, int(self.bitrate_mbps)))
        self.scan_delay_s = max(5, min(600, int(self.scan_delay_s)))
        self.hub_url = (self.hub_url or "").strip().rstrip("/")
        if self.hub_url and "://" not in self.hub_url:
            self.hub_url = "http://" + self.hub_url
        for k in ("hk_mute_game", "hk_mute_discord", "hk_mute_mic", "hk_mark"):
            v = (getattr(self, k) or "").strip().lower().replace(" ", "")
            setattr(self, k, v if v == "" or parse_hotkey(v) else "")
        if self.capture not in ("window", "game", "display"):
            self.capture = "window"
        return self

    def tracks(self) -> list[str]:
        """Audio tracks recorded, in file order (track 1, 2, 3)."""
        t = ["game"] if self.game_audio else []
        if self.comms_consent:
            t += [n for n, on in (("discord", self.discord_audio), ("mic", self.mic_audio)) if on]
        return t

    @property
    def gb_per_hour(self) -> float:
        return (self.bitrate_mbps * 1000 + 160 * len(self.tracks())) * 3600 / 8 / 1e6   # video + audio tracks

    @property
    def folder_path(self) -> Path:
        p = Path(self.folder)
        p.mkdir(parents=True, exist_ok=True)
        return p


def load() -> Settings:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    s = Settings()
    if SETTINGS_FILE.exists():
        try:
            for k, v in json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).items():
                if hasattr(s, k):
                    setattr(s, k, v)
        except (json.JSONDecodeError, OSError):
            pass
    save(s)
    return s


def save(s: Settings) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(asdict(s.clamp()), indent=2), encoding="utf-8")


MODS = {"ctrl": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}


def parse_hotkey(text: str) -> tuple[int, int] | None:
    """'ctrl+alt+1' -> (modifier flags, virtual-key code) for Windows RegisterHotKey; None if invalid.
    At least one of ctrl/alt/win is required so a hotkey never steals a plain game key."""
    parts = [p for p in (text or "").lower().replace(" ", "").split("+") if p]
    if len(parts) < 2:
        return None
    mods, key = 0, parts[-1]
    for p in parts[:-1]:
        if p not in MODS:
            return None
        mods |= MODS[p]
    if not mods & (MODS["ctrl"] | MODS["alt"] | MODS["win"]):
        return None
    if len(key) == 1 and (key.isdigit() or "a" <= key <= "z"):
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key.startswith("num") and key[3:].isdigit() and len(key) == 4:
        vk = 0x60 + int(key[3:])
    else:
        return None
    return mods | 0x4000, vk                              # 0x4000 = MOD_NOREPEAT
