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
    game_audio: bool = True                            # Apex sound in the recording (never the mic)
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
        if self.capture not in ("window", "game", "display"):
            self.capture = "window"
        return self

    @property
    def gb_per_hour(self) -> float:
        return self.bitrate_mbps * 3600 / 8 / 1000 * 1.02      # video + game sound, roughly

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
