"""Local server behind the PCS Scanner window.

The window (Edge WebView2) shows the pages in app/web/ and talks to this server for everything:
state, settings, start with Windows, auto-record, the recordings library, scans, Google sign-in,
updates, and video playback (with seeking, via HTTP Range requests).

Only reachable from this PC (127.0.0.1) and only with the random token the app puts in the window's
address, so a web page open in a browser can't drive it.
"""
from __future__ import annotations

import collections
import hashlib
import json
import mimetypes
import os
import re
import secrets
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pcs_core as core
import pcs_i18n as i18n
from pcs_recorder import labels
from pcs_recorder.core import NoHub

WEB = Path(core.HERE) / "web"
VIDEO_EXT = (".mp4", ".mkv", ".mov")
UPDATE_EVERY_S = 6 * 3600           # update check in the background
UPDATE_ON_OPEN_S = 10 * 60          # and while the window is open / when it's opened, at most this often
REC_FIELDS = ("folder", "keep_days", "max_gb", "bitrate_mbps", "game_audio", "auto_scan", "scan_delay_s",
              "capture", "hub_url", "start_with_windows", "comms_consent", "discord_audio", "voice_app", "voice_exe",
              "crash_reports", "server_scan",
              "mic_audio", "mic_device", "send_mic", "share_tracks", "transcript_consent", "share_asked",
              "transcript_asked",
              "hk_mute_game", "hk_mute_discord", "hk_mute_mic", "hk_mark", "hk_sounds",
              "yt_enabled", "yt_privacy", "yt_audio", "yt_scrims_only", "yt_gap_min",
              "yt_when", "night_from", "night_to", "night_idle_min", "night_shutdown",
              "yt_team", "yt_wait_eec", "yt_wait_eec_h", "nogame_keep_h",
              "min_record_min", "hl_chain_s", "hl_multi_min", "hl_double_s",
              "game_pause", "game_fullscreen", "game_exes", "game_ignore")
TRACK_LABEL = {"game": "Game", "discord": "Discord", "mic": "Microphone"}
MAX_BODY = 4 << 20                  # largest request body the page may send (bytes)


def regroup_highlights(scan_games: list, games: list, s) -> None:
    """Highlights with the player's own rules (Settings -> Highlights): the scan's takedowns grouped again, so a changed
    rule shows at once in the review (pcs_highlights.find). Scans without takedowns keep what they have."""
    try:
        import pcs_highlights
        rule = pcs_highlights.parse_rules(s.highlight_rules()) if s is not None else pcs_highlights.rules()
    except Exception:
        return
    for g, out in zip(scan_games, games):
        if isinstance(g.get("takedowns"), list):
            try:
                out["highlights"] = pcs_highlights.find(g["takedowns"], g.get("fights"), g.get("lastAlive"), rule)
            except Exception:
                pass


def to_recycle_bin(path: str) -> None:
    """Windows Recycle Bin (can be restored); elsewhere a normal delete."""
    from pcs_recorder.retention import to_recycle_bin as recycle
    recycle(path)


def audio_tracks(video: str) -> list[dict]:
    """The recording's audio tracks: names from the recorder's .events.json, count from ffprobe."""
    import subprocess
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                            "-of", "csv=p=0", video], capture_output=True, text=True, timeout=20,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        n = len([x for x in r.stdout.split() if x.strip()])
    except Exception:
        n = 1
    names, label = [], dict(TRACK_LABEL)
    ev = Path(video).with_suffix(".events.json")
    if ev.exists():
        try:
            d = json.loads(ev.read_text(encoding="utf-8"))
            names = d.get("tracks", [])
            if isinstance(d.get("voice"), str) and d["voice"].strip():
                label["discord"] = d["voice"].strip()[:40]        # the voice app recorded: TeamSpeak, Mumble…
        except Exception:
            pass
    return [{"n": i, "key": names[i] if i < len(names) else f"track{i + 1}",
             "label": label.get(names[i], names[i]) if i < len(names) else f"Audio {i + 1}"} for i in range(n)]


def tailscale_state(url: str) -> str:
    """Why a hub reached through Tailscale (100.64.0.0/10 or *.ts.net) may not answer, looked up on this PC only
    (no request, nothing started): "missing" = Tailscale isn't installed, "off" = installed but this PC isn't
    connected (no Tailscale address), "on" = connected (the hub itself is off, or not shared with this player).
    "" = the hub isn't a Tailscale address, or it can't be told."""
    import ipaddress
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    ts_net = ipaddress.ip_network("100.64.0.0/10")
    try:
        on_ts = ipaddress.ip_address(host) in ts_net
    except ValueError:
        on_ts = host.endswith(".ts.net")
    if not on_ts:
        return ""
    roots = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
    if not any(r and os.path.exists(os.path.join(r, "Tailscale", "tailscale.exe")) for r in roots):
        return "missing"
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for ad in addrs:
                try:
                    if ipaddress.ip_address(ad.address.split("%")[0]) in ts_net:
                        return "on"
                except ValueError:
                    pass
    except Exception:
        return ""
    return "off"


def vid(path: str) -> str:
    return hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode()).hexdigest()[:12]


_REC_NAME = re.compile(r'(\d{4})[._-](\d{2})[._-](\d{2})[ _-](\d{2})[._-](\d{2})(?:[._-](\d{2}))?')


def recorded_at(path: str, scan: dict | None = None) -> float | None:
    """When the recording started (local time, epoch seconds): from the file name OBS gives it
    ("Apex Legends_2026.11.07-21.42", read like the scanner's rec_start), else the scan's recStart.
    None when neither tells: the caller falls back to the file's time."""
    import datetime
    cands = []
    m = _REC_NAME.search(Path(path).name)
    if m:
        y, mo, d, h, mi, s = m.groups()
        cands.append(f"{y}-{mo}-{d}T{h}:{mi}:{s or '00'}")
    if isinstance((scan or {}).get("recStart"), str):
        cands.append(scan["recStart"])
    for c in cands:
        try:
            return datetime.datetime.fromisoformat(c).timestamp()
        except (ValueError, OverflowError, OSError):
            continue
    return None


# what the review player shows of each game, beside the drop / rings / landing it always had
GAME_FIELDS = ("map", "mode", "tStart", "tEnd", "tEndGame", "ring", "landing", "rankedShare", "clockStart", "harvesters",
               "select", "banner", "squadsLeft", "own", "feedStats",
               "ringCircles", "respawns",   # scanner PR #52: respawns [{t, who: teammate | self, hue, deadSince, how}], shown like kills
               "ownDeaths",                 # the recording player's deaths [t]: dead time and lives in the game bar
               "ownKnocks",   # own knocks [{t, end, reads, outcome}], shown like respawns
               "carePackages", "strongholds",
               "fights", "ringDamage",   # pcs_fights: fights [{t0, t1, result, thirdParty...}] as bands, time outside the ring   # pcs_mapicons: [{t, n, mapX, mapY, ...}], [{t, mapX, mapY, state, ...}] on the map panel
               "heals", "abilities",     # pcs_usage: [{t, item, done}], [{t, kind: ult | tactical, legend}]: small marks in the review
               "ammoEvents", "ammoSummary",
               "highlights")             # pcs_highlights: key plays [{t0, t1, kind, count, score...}], best first   # pcs_ammo: low-ammo fights and out-of-heals marks, a summary in the game bar


# the game's minimap transform per map (world -> picture), as in scanner/pcs_minimap.MAPS and the hub's minimaps
MINIMAP_TX = {"WE": (-45056, 45055, 22), "SP": (-50606, 52139, 25), "ED": (-43008, 43008, 21),
              "BM": (-45000, 43500, 21), "KC": (-37541, 43886, 20), "OL": (-52024, 48025, 22)}


def seen_rows(g: dict) -> tuple[list, str | None]:
    """The review's position rows of a game: the debug overlay's (`samples`), or for a player without the overlay the
    scanner's HUD minimap reads (`minimapTrack`, map %) turned into world x, y. -> (rows, "pos" | "minimap" | None)."""
    rows = [[s[0], s[1], bool(s[3]), s[4], s[5], s[6], s[7]] for s in g.get("samples", [])]
    if any(r[3] is not None for r in rows):
        return rows, "pos"
    tx = MINIMAP_TX.get(g.get("map") or "")
    pts = (g.get("minimapTrack") or {}).get("pts") or []
    if not tx or not pts:
        return rows, None
    w = tx[2] * 4096
    return [[p[0], -1, False, round(tx[0] + p[1] / 100 * w), round(tx[1] - p[2] / 100 * w), None, None]
            for p in pts if isinstance(p, list) and len(p) >= 3], "minimap"


def _same_sig(a, b) -> bool:
    """Two name shapes from the scanner (pcs_hud._sig) close enough to be the same name."""
    if not a or not b:
        return a == b
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1") <= 26
    except ValueError:
        return False


def feed_lines(lines: list[dict]) -> tuple[list[dict], bool]:
    """Kill-feed lines for the review: picture and time, plus what the scanner read (1.10.x+): knock or kill, who is
    on each side (our squad or not, the squad colour), weapon, headshot. The feed scrolls and fades, so one line is
    often captured 2 or 3 times: repeats of a line involving our squad get "dup" (counted once, like the scan's
    feedStats). Second value: whether this scan read the sides at all (older scans didn't)."""
    out, seen, sides = [], [], False
    for l in lines:
        x = {"t": l.get("t"), "jpg": l.get("jpg")}
        for k in ("event", "weaponName", "headshot"):
            if l.get(k) is not None:
                x[k] = l[k]
        for k in ("killer", "victim"):
            if isinstance(l.get(k), dict):
                x[k] = {kk: l[k][kk] for kk in ("side", "hue", "color") if kk in l[k]}
                sides = True
        ks, vs = (x.get("killer") or {}).get("side"), (x.get("victim") or {}).get("side")
        if l.get("event") and (ks == "squad") != (vs == "squad") and isinstance(l.get("t"), (int, float)):
            if any(l["t"] - p["t"] < 25 and p.get("event") == l.get("event") and p.get("weapon") == l.get("weapon")
                   and _same_sig(p.get("killerSig"), l.get("killerSig"))
                   and _same_sig(p.get("victimSig"), l.get("victimSig")) for p in seen):
                x["dup"] = True
            else:
                seen.append(l)
        out.append(x)
    return out, sides


def scan_files(video: str) -> tuple[Path | None, Path | None]:
    """Where a recording's scan results are: next to it (auto-record) or in the app's results folder."""
    v = Path(video)
    for d in (v.with_suffix(".scan"), Path(core.OUT)):
        s, f = d / (v.stem + ".scan.json"), d / (v.stem + ".feed.json")
        if s.exists():
            return s, (f if f.exists() else None)
    return None, None


class Backend:
    """What the server needs from the app. The app replaces these with the real window/tray functions."""

    def __init__(self, auto=None):
        self.auto = auto                                   # pcs_recorder.runtime.AutoRecorder or None
        self.messages = collections.deque(maxlen=200)      # shown on the Home page
        self.youtube = "checking"                          # ok | expired | none | checking: the YouTube sign-in
        self.update = None                                 # (tag, exe_url, page) when a new version exists
        self._update_lock = threading.Lock()               # one update download at a time
        self.update_state = None                           # the window's "Updating" screen (see install_update)
        self.on_update_change = lambda: None               # the tray menu redraws its Update item
        self.scan_queue: list[str] = []
        self.scan_now: str | None = None
        self.scan_lock = threading.Lock()
        self.scan_started = 0.0
        self._levels_locks: dict[str, threading.Lock] = {}
        self._worker_running = False
        from pcs_recorder import tracks
        self.cutter = tracks.CUTTER                        # audio tracks for the review, cut in the background
        self.cutter.clean()                                # unfinished cuts of hand-added files (audio cache)
        self.pick_files = lambda kind="video": []          # native "open files" dialog
        self.open_path = lambda p: os.startfile(p) if os.name == "nt" else None
        self.open_window = lambda url, title: False        # a second app window (pywebview); False = none
        self.set_start_with_windows = lambda on: None
        from pcs_recorder import clips
        self.reveal_file = clips.show_in_folder           # a clip in Explorer, selected
        self.copy_file = lambda f: clips.copy_files_to_clipboard([f])   # a clip on the clipboard, as Explorer copies
        self.quit = lambda: None

    # ------------------------------------------------------------- messages
    def say(self, text: str) -> None:
        self.messages.append({"t": time.time(), "text": str(text)})

    def check_update(self, min_age_s: float = 0) -> None:
        """Ask GitHub for a newer release (60 requests/hour allowed without sign-in: far below what this uses)."""
        if time.time() - getattr(self, "update_checked", 0) < min_age_s:
            return
        self.update_checked = time.time()
        u = core.check_update()
        if u and (not self.update or self.update[0] != u[0]):
            self.update = u
            self.say(f'Version {u[0]} is available: press "Update" at the top right.')
            self._update_changed()

    def sync_icons(self, min_age_s: float = 0, tell: bool = False) -> str:
        """Fetch the weapon icons: from the team hub when it has them (pcs_recorder.weaponicons; "hub:<status>"),
        else from the team link (Settings), as before. Says something only when they changed, or when the player
        just pasted the link (tell)."""
        if time.time() - getattr(self, "icons_checked", 0) < min_age_s:
            return "skipped"
        self.icons_checked = time.time()
        from pcs_recorder import weaponicons
        url = self.auto.s.hub_url if self.auto else ""
        hs, htext = weaponicons.sync(url, core.ICONS_F, core.icons_valid)
        if htext:
            self.say(htext)
        if hs in ("updated", "same") or (hs in ("error", "bad") and weaponicons.from_hub(url, core.ICONS_F)):
            return "hub:" + hs                              # the hub's copy stays; the team link isn't needed
        status, text = core.fetch_icons()
        if text and (tell or status == "updated"):
            self.say(text)
        return status

    # The update, step by step, for the window's full-window "Updating PCS Scanner" screen (it locks the whole UI):
    # update_state = None, or {"step": "download" | "check" | "restart" | "failed", "tag", "done", "total" (bytes),
    # "error"}. It lives here, not in the page, so the window opened from the tray or refreshed shows the same screen.
    UPDATE_FAILED_DOWNLOAD = "The download stopped. Check the internet and try again."

    def _update_changed(self) -> None:
        try:
            self.on_update_change()
        except Exception:
            pass

    def _set_update_step(self, step: str, done: int = 0, total: int = 0, error: str = "") -> None:
        st = dict(self.update_state or {})
        new = st.get("step") != step
        if step == "failed":                               # the step it stopped at stays marked on the screen
            st["at"] = st.get("step") if st.get("step") in ("download", "check", "restart") else "download"
        st.update(step=step, done=int(done or 0), total=int(total or 0), error=error)
        st.setdefault("tag", self.update[0] if self.update else "")
        self.update_state = st
        if new:
            self._update_changed()

    def updating(self) -> bool:
        """An update is running (downloading, checking or restarting): the window and the tray menu are locked."""
        return bool(self.update_state and self.update_state.get("step") != "failed")

    def _begin_update(self) -> str | None:
        """Checks and the lock, before anything is downloaded. -> why not, or None (the lock is then held)."""
        why = self.update_blocked()
        if why:
            return why
        # One download at a time: a second press (window button, corner message, tray) while the first download runs
        # used to start a second download into the same .part file; Windows then refused it ("file in use") and the
        # update never finished (coach's PC, 18 Nov 2026). Kept after a good download: the app is closing.
        if not self._update_lock.acquire(blocking=False):
            return "The update is already downloading. PCS Scanner restarts by itself when it's done."
        if not (self.update and self.update[1]):
            self._update_lock.release()
            return "No new version to install."
        self.update_state = {"step": "download", "tag": self.update[0], "done": 0,
                             "total": int((self.update[3] if len(self.update) > 3 else 0) or 0), "error": ""}
        self._update_changed()
        return None

    def _run_update(self, show: bool) -> str | None:
        """Download, check, start the finisher (lock held by _begin_update). -> why it failed, or None."""
        done = False
        try:
            u = self.update
            if core.install_update(u[1], self.say, u[3] if len(u) > 3 else None, show=show,
                                   digest=u[4] if len(u) > 4 else None, step=self._set_update_step):
                done = True
                st = self.update_state or {}
                self._set_update_step("restart", st.get("done", 0), st.get("total", 0))
                threading.Timer(3.0, self.quit).start()    # time to read "Restarting" before the window closes
            else:                                          # run from source: nothing to install
                self.update_state = None
                self._update_changed()
            return None
        except RuntimeError as e:                          # address refused, bad or incomplete download
            why = str(e)
        except Exception as e:                             # no internet, GitHub unreachable, disk full...
            core.ulog(f"download failed: {e!r}")
            why = self.UPDATE_FAILED_DOWNLOAD
        finally:
            if not done:
                self._update_lock.release()
        self._set_update_step("failed", error=why)
        return why

    def install_update(self, show: bool = False) -> str | None:
        """Download and restart into the new version; returns why not, if it can't now (or why it failed). show:
        pressed in the open window, so the new version opens its window too (otherwise it starts in the tray)."""
        why = self._begin_update()
        return why or self._run_update(show)

    def start_update(self, show: bool = False) -> str | None:
        """The window's button: refused at once (Apex, recording, already updating...), else the download runs in the
        background and the window follows update_state."""
        why = self._begin_update()
        if why:
            return why
        threading.Thread(target=self._run_update, args=(show,), daemon=True, name="update").start()
        return None

    def update_back(self) -> bool:
        """The failed screen's Back button: the window is usable again. Never while an update runs."""
        if self.update_state and self.update_state.get("step") == "failed":
            self.update_state = None
            self._update_changed()
            return True
        return False

    def update_blocked(self) -> str | None:
        """Never restart in the middle of a game, a recording, a scan or an upload."""
        a = self.auto
        try:
            from pcs_recorder.core import apex_running
            if apex_running():
                return "Apex is running. Update after your games."
        except Exception:
            pass
        if a and a.rec.current is not None:
            return "A recording is running. Update after your games."
        if (a and a.rec.scan_proc is not None) or self.scan_now:
            return "A scan is running. Update when it's done (a few minutes)."
        up = getattr(a.rec, "youtube", None) if a else None
        if up and up.thread and up.thread.is_alive():
            return "A YouTube upload is running. Update when it's done."
        return None

    def check_youtube(self) -> None:
        try:
            ok = core.youtube_service(interactive=False) is not None
        except Exception:
            ok = False
        self.youtube = "ok" if ok else ("expired" if os.path.exists(core.YT_TOKEN_F) else "none")

    def yt_signin(self) -> None:
        def go():
            try:
                if os.path.exists(core.YT_TOKEN_F) and core.youtube_service(interactive=False) is None:
                    os.remove(core.YT_TOKEN_F)
                core.youtube_service(True)
                self.youtube = "ok"
                self.say("Signed in to YouTube. Scrim blocks will be uploaded to the channel you picked.")
            except Exception as e:
                self.say(f"YouTube sign-in failed: {e}")
        threading.Thread(target=go, daemon=True).start()

    def _rec_key(self, item_id: str) -> str:
        a = self.auto
        if not a or not getattr(a.rec, "youtube", None):
            raise RuntimeError("YouTube upload isn't available in this build")
        it = self.find(item_id)
        key = next((k for k, e in a.rec.idx.items() if it and e.path and
                    os.path.normcase(os.path.abspath(e.path)) == os.path.normcase(os.path.abspath(it["path"]))), None)
        if not key:
            raise RuntimeError("Only recordings made by auto-record belong to a scrim block")
        return key

    def set_eec(self, item_id: str, links: list[str]) -> list[str]:
        key = self._rec_key(item_id)
        from pcs_recorder.youtube import normalize_eec
        bad = [l for l in links if l.strip() and not normalize_eec(l)]
        if bad:
            raise RuntimeError("That isn't an EEC link (it should start with https://eternalesports.club/)")
        good = self.auto.rec.youtube.set_links(key, links)
        self.say("EEC lobby link saved for this block." if good else "EEC lobby link removed.")
        return good

    def add_eec_files(self, paths: list[str]) -> list[str]:
        """EEC lobby export files (Download button on the lobby page) -> results for the matching block."""
        a = self.auto
        if not a or not getattr(a.rec, "youtube", None):
            raise RuntimeError("YouTube upload isn't available in this build")
        from pcs_recorder import eec
        done = []
        for p in paths:
            lobby = eec.read_file(p)
            r = a.rec.youtube.add_results(lobby)
            done.append(f"{eec.ordinal(r['standing'])} of {r['teams']}, {r['pts']} points, {r['kills']} kills")
            self.say(f"EEC results added ({Path(p).name}): {done[-1]}. They go in the YouTube description.")
        return done

    def yt_upload(self, item_id: str) -> int:
        a = self.auto
        if not a or not a.rec.youtube:
            raise RuntimeError("YouTube upload isn't available in this build")
        it = self.find(item_id)
        key = next((k for k, e in a.rec.idx.items() if it and e.path and
                    os.path.normcase(os.path.abspath(e.path)) == os.path.normcase(os.path.abspath(it["path"]))), None)
        if not key:
            raise RuntimeError("Only recordings made by auto-record can be uploaded as a block")
        keys = a.rec.youtube.request(key) or []
        self.say(f"Block of {len(keys)} recording(s) queued for YouTube. It uploads once Apex is closed.")
        return len(keys)

    def yt_queue(self) -> dict:
        a = self.auto
        up = getattr(a.rec, "youtube", None) if a else None
        if not up:
            return {"available": False, "blocks": []}
        return {"available": True, "enabled": bool(a.s.yt_enabled), "signed_in": self.youtube == "ok",
                "privacy": a.s.yt_privacy, "now": up.status, "blocks": up.queue()}

    def yt_block(self, bid: str, action: str, title: str | None = None, privacy: str | None = None) -> dict:
        a = self.auto
        if not a or not getattr(a.rec, "youtube", None):
            raise RuntimeError("YouTube upload isn't available in this build")
        up = a.rec.youtube
        if action == "label":                          # the label of every recording in the block
            kind = title or None
            for k in up._block(bid):
                labels.set_label(a.rec.idx[k].path, kind)
            up.relabelled(bid)
            self.say(f"Block labelled {labels.TITLE[kind]}." if kind else "Block label back to automatic.")
            return {"id": bid, "label": kind}
        r = up.act(bid, action, title, privacy)
        self.say({"upload": "Block queued for YouTube: it uploads as soon as Apex is closed.",
                  "skip": "Block skipped: it won't go to YouTube.",
                  "unskip": "Block back in the queue (the usual rules apply).",
                  "edit": "Saved: used when this block uploads."}.get(action, "Done."))
        return {k: v for k, v in r.items() if k in ("id", "status", "custom_title", "privacy")}

    def set_label(self, item_id: str, label: str | None) -> str | None:
        it = self.find(item_id)
        if not it:
            raise RuntimeError("Recording not found")
        labels.set_label(it["path"], label)
        a = self.auto
        if a and getattr(a.rec, "youtube", None):
            key = next((k for k, e in a.rec.idx.items() if e.path and os.path.normcase(os.path.abspath(e.path)) ==
                        os.path.normcase(os.path.abspath(it["path"]))), None)
            if key:
                a.rec.youtube.relabelled(key)
        return label

    def mics(self) -> dict:
        from pcs_recorder import obs
        cur = getattr(self.auto.s, "mic_device", "default") if self.auto else "default"
        devs = obs.mic_devices()
        if cur and cur != "default" and cur not in {d["id"] for d in devs}:
            devs.append({"id": cur, "name": "Saved microphone (not plugged in)"})
        return {"current": cur or "default", "devices": [{"id": "default", "name": "Windows default microphone"}] + devs}

    def voice_apps(self) -> dict:
        """Programs running now with a window, to pick the voice app recorded on track 2."""
        from pcs_recorder import obs
        s = self.auto.s if self.auto else None
        try:
            running = obs.window_apps()
        except Exception:
            running = []
        return {"current": getattr(s, "voice_app", "discord"), "exe": getattr(s, "voice_exe", ""), "running": running}

    def library(self) -> list[dict]:
        items: dict[str, dict] = {}
        rec_index = {}
        if self.auto:
            for key, e in self.auto.rec.idx.items():
                if e.path:
                    rec_index[os.path.normcase(os.path.abspath(e.path))] = (key, e)
        paths = [e.path for _, e in rec_index.values()] + list(core.SETTINGS.get("library", []))
        if self.auto:
            folder = Path(self.auto.s.folder)
            if folder.exists():
                paths += [str(p) for p in folder.glob("*") if p.suffix.lower() in (".mp4", ".mov")]
        for p in paths:
            if not p or not os.path.exists(p):
                continue
            norm = os.path.normcase(os.path.abspath(p))
            if Path(p).suffix.lower() == ".mkv" and os.path.exists(str(Path(p).with_suffix(".mp4"))):
                continue                                   # the .mp4 copy is listed instead
            i = vid(p)
            if i in items:
                continue
            st = os.stat(p)
            scan, feed = scan_files(p)
            summary, full = None, {}
            if scan:
                try:
                    d = full = json.loads(scan.read_text(encoding="utf-8"))
                    summary = {"games": [{"map": g.get("map"), "mode": g.get("mode")} for g in d.get("games", [])],
                               "recStart": d.get("recStart"), "feedLines": d.get("perf", {}).get("feedLines")}
                except Exception:
                    summary = None
            status = "scanned" if scan else "not scanned"
            if norm in rec_index:                          # recorder's own: recording, recorded, uploaded, skipped…
                status = rec_index[norm][1].status
                if status in ("recorded", "skipped") and scan:
                    status = "scanned"
            if self.scan_now == p:
                status = "scanning"
            elif p in self.scan_queue:
                status = "waiting"
            yt = None
            chk = None
            if norm in rec_index and self.auto and getattr(self.auto.rec, "youtube", None):
                up = self.auto.rec.youtube
                b = up.block_of(rec_index[norm][0])
                yt = {k: b.get(k) for k in ("status", "url", "title", "why", "error")} if b else {}
                yt["eec"] = up.links_of(rec_index[norm][0])
                r = up.results_of(rec_index[norm][0])
                if r:
                    yt["res"] = {k: r.get(k) for k in ("standing", "teams", "pts", "kills")}
                try:
                    chk = up.check_of_key(rec_index[norm][0])
                except Exception:
                    chk = None
                if chk:
                    yt["eecCheck"] = {"verdict": chk["verdict"], "why": chk["why"]}
            lab = labels.explain(p, full, chk)
            label, by_hand = lab["label"], lab["source"] == "hand"
            rec = recorded_at(p, full)
            items[i] = {"id": i, "name": Path(p).name, "path": p, "size": st.st_size, "mtime": st.st_mtime,
                        # "Recorded" = when the recording started, not when the file was last written
                        "recorded": rec if rec is not None else st.st_mtime,
                        "label": label, "label_by_hand": by_hand, "label_source": lab["source"], "label_why": lab["why"],
                        # EEC drop per game where EEC's results were compared with this recording's games
                        "eecGames": [{k: x.get(k) for k in ("i", "eecGame", "eecDrop", "ourDrop", "ourFrom", "check", "why")}
                                     for x in (chk or {}).get("games", []) if norm in rec_index and x.get("rec") == rec_index[norm][0]]
                                    if chk and chk["verdict"] != "mismatch" else [],
                        "status": status, "scan": summary, "auto": norm in rec_index, "yt": yt,
                        "note": rec_index[norm][1].note if norm in rec_index else "",   # e.g. "Sent to hub: 11 games"
                        # on the team hub? {"hub": "sent" | "pending" | "", "why": "Hub not reachable" | "Key refused"…}
                        "hub": (self.auto.rec.hub_state(rec_index[norm][0])
                                if norm in rec_index and hasattr(self.auto.rec, "hub_state") else None),
                        "nogame": bool(norm in rec_index and getattr(rec_index[norm][1], "nogame", False))}
        return sorted(items.values(), key=lambda x: -x["recorded"])

    def find(self, item_id: str) -> dict | None:
        return next((x for x in self.library() if x["id"] == item_id), None)

    def add_paths(self, paths: list[str]) -> int:
        lib = core.SETTINGS.setdefault("library", [])
        n = 0
        for p in paths:
            p = os.path.normpath(p)
            if os.path.isdir(p):
                n += self.add_paths([os.path.join(p, f) for f in sorted(os.listdir(p)) if f.lower().endswith(VIDEO_EXT)])
            elif p.lower().endswith(VIDEO_EXT) and os.path.exists(p) and p not in lib:
                lib.append(p)
                n += 1
        core.save_settings()
        return n

    def detail(self, item_id: str) -> dict | None:
        it = self.find(item_id)
        if not it:
            return None
        scan, feed = scan_files(it["path"])
        it = dict(it)
        if scan:
            d = json.loads(scan.read_text(encoding="utf-8"))
            games = []
            for g in d.get("games", []):
                seen, seen_from = seen_rows(g)
                games.append({k: g.get(k) for k in GAME_FIELDS}
                             | {"drop": {k: (g.get("drop") or {}).get(k) for k in ("spots", "err")},
                                "track": [[s[0], s[4], s[5], s[6], s[7]] for s in g.get("samples", []) if s[4] is not None],
                                # what the scanner read each second: [t, ring label, spectating, x, y, z, speed]; without
                                # the debug overlay the HUD minimap's reads every 3 s (seenFrom "minimap")
                                "seen": seen, "seenFrom": seen_from})
            regroup_highlights(d.get("games", []), games, self.auto.s if self.auto else None)
            it["games"] = games
        if feed:
            f = json.loads(feed.read_text(encoding="utf-8"))
            it["feed"], it["feedSides"] = feed_lines(f.get("lines", []))
        ev = Path(it["path"]).with_suffix(".events.json")
        if ev.exists():
            try:
                it["marks"] = json.loads(ev.read_text(encoding="utf-8")).get("events", [])
            except Exception:
                pass
        it["audio"] = audio_tracks(it["path"])
        try:                                               # teammates' mic tracks the app downloaded (shared by them)
            it["teamAudio"] = self.teamtracks.for_recording(it["path"]) if self.auto else []
        except Exception:
            it["teamAudio"] = []
        try:                                               # the player's own comms transcript (comms.py), when made
            from pcs_recorder import comms
            it["comms"] = comms.transcript_of(it["path"])
        except Exception:
            it["comms"] = None
        return it

    # ------------------------------------------------------------- sound analysis for the review
    def levels(self, item_id: str) -> dict | None:
        it = self.find(item_id)
        if not it:
            return None
        import pcs_levels
        v = Path(it["path"])
        own = v.with_suffix(".events.json").exists()
        cache = (v.with_name(v.stem + ".levels.json") if own else Path(core.DATA) / "levels-cache" / f"{it['id']}.json")
        lock = self._levels_locks.setdefault(item_id, threading.Lock())
        with lock:                                         # one analysis per recording at a time
            return pcs_levels.cached(str(v), audio_tracks(str(v)), cache)

    # ------------------------------------------------------------- deleting
    def delete(self, ids: list[str]) -> dict:
        """App recordings: deleted for good with everything made from them (frees the space).
        Files added by hand: sent to the Recycle Bin (they're yours; the scan results stay)."""
        a = self.auto
        idx = a.rec.idx if a else {}
        busy_keys = set()
        if a:
            busy_keys = {k for k in (a.rec.current, a.rec.scan_key) if k} | set(a.rec.queue)
            up = getattr(a.rec, "youtube", None)
            if up:
                busy_keys |= {k for b in up.state.values() if b.get("status") in ("waiting", "uploading")
                              for k in b.get("keys", [])}
        deleted, skipped, freed = [], [], 0
        for item_id in ids:
            it = self.find(item_id)
            if not it:
                continue
            p = os.path.normcase(os.path.abspath(it["path"]))
            key = next((k for k, e in idx.items() if e.path and os.path.normcase(os.path.abspath(e.path)) == p), None)
            if it["path"] == self.scan_now or it["path"] in self.scan_queue or (key and key in busy_keys):
                skipped.append(it["name"])
                continue
            try:
                if key:
                    from pcs_recorder import retention
                    e = idx[key]
                    for f in retention.files_of(e):
                        freed += f.stat().st_size
                        f.unlink()
                    sc = Path(e.path).with_suffix(".scan")
                    if sc.is_dir():
                        import shutil
                        shutil.rmtree(sc, ignore_errors=True)
                    with a.rec.lock:
                        del idx[key]
                        retention.save_index(idx)
                else:
                    freed += it["size"]
                    to_recycle_bin(it["path"])
                    lib = core.SETTINGS.get("library", [])
                    if it["path"] in lib:
                        lib.remove(it["path"])
                        core.save_settings()
                deleted.append(it["name"])
            except Exception as ex:
                skipped.append(f"{it['name']} ({ex})")
        if deleted:
            self.say(f"Deleted {len(deleted)} recording(s), {freed / 1e9:.1f} GB freed.")
        return {"deleted": deleted, "skipped": skipped, "freed": freed}

    # ------------------------------------------------------------- scanning by hand
    def queue_scans(self, ids: list[str]) -> int:
        items = [self.find(i) for i in ids]
        added = 0
        with self.scan_lock:
            for it in items:
                if it and it["path"] not in self.scan_queue and it["path"] != self.scan_now:
                    self.scan_queue.append(it["path"])
                    added += 1
        if added:
            threading.Thread(target=self._scan_worker, daemon=True).start()
        return added

    def rescan(self, ids: list[str]) -> int:
        """Scan again: the app's recordings through the recorder (results next to the recording), others here."""
        n, manual = 0, []
        for item_id in ids:
            it = self.find(item_id)
            if not it:
                continue
            key = None
            if self.auto:
                p = os.path.normcase(os.path.abspath(it["path"]))
                key = next((k for k, e in self.auto.rec.idx.items()
                            if e.path and os.path.normcase(os.path.abspath(e.path)) == p), None)
            if key:
                n += bool(self.auto.rec.rescan(key))
            else:
                for f in (Path(core.OUT) / (Path(it["path"]).stem + ".scan.json"),
                          Path(core.OUT) / (Path(it["path"]).stem + ".feed.json")):
                    f.unlink(missing_ok=True)
                manual.append(item_id)
        if manual:
            n += self.queue_scans(manual)
        if n:
            self.say(f"{n} recording(s) will be scanned again.")
        return n

    def _busy_reason(self) -> str | None:
        """Manual scans wait while a game runs (Apex or any other) or the recorder is busy, so a game never shares
        the PC with a scan."""
        a = self.auto
        if a and (a.rec.current is not None or a.rec.scan_proc is not None):
            return "the auto-recorder is busy"
        try:
            g = self._game_running()
            if g:
                return f"{g} is running"
        except Exception:
            pass
        return None

    def _run_scan(self, proc, every_s: float = 3) -> int | None:
        """Wait for a scan; None when a game (Apex or any other) started meanwhile: the scan is then stopped (not
        kept paused in memory during the game) and the scanner goes on from its finished chunks next time."""
        from pcs_recorder.core import stop_tree
        while True:
            try:
                return proc.wait(timeout=every_s)
            except subprocess.TimeoutExpired:
                pass
            try:
                game = self._game_running()
            except Exception:
                game = ""
            if game:
                self._scan_held_by = game
                stop_tree(proc)
                return None

    def _scan_worker(self) -> None:
        with self.scan_lock:
            if self._worker_running:
                return
            self._worker_running = True
        try:
            from pcs_recorder.runtime import scan_process
            told = False
            while True:
                with self.scan_lock:
                    if not self.scan_queue:
                        break
                why = self._busy_reason()
                if why:
                    if not told:
                        self.say(f"Scans wait until {why.replace('is running', 'closes').replace('is busy', 'is done')}.")
                        told = True
                    time.sleep(5)
                    continue
                with self.scan_lock:
                    p = self.scan_queue.pop(0)
                    self.scan_now, self.scan_started = p, time.time()
                self.say(f"Scanning {Path(p).name}…")
                code = self._run_scan(scan_process(p, core.OUT))
                if code is None:                         # a game started: stopped, goes on from there when it closes
                    with self.scan_lock:
                        self.scan_queue.insert(0, p)
                        self.scan_now = None
                    g = getattr(self, "_scan_held_by", "") or "Apex"
                    if g == "Apex":
                        self.say(f"Scan of {Path(p).name} paused while Apex runs: it goes on when Apex closes.")
                    else:
                        self.say(f"Scan of {Path(p).name} paused while a game runs ({g}): it goes on when it closes.")
                    told = True
                    continue
                if code != 0:
                    self.say(f"Scan of {Path(p).name} failed (code {code}). Details: {Path(core.OUT) / 'scan.log'}")
                    continue
                scan, feed = scan_files(p)
                files = [x for x in (scan, feed) if x]
                try:
                    if not self.auto:                    # no recorder in this process: nothing sends results
                        raise ConnectionError("the recorder isn't running")
                    where = self.auto.sender.upload(files)
                    note = getattr(where, "hub_note", "")
                    self.say(f"Scan of {Path(p).name} done, sent to {' and '.join(where) or 'the team hub'}."
                             + (f" {note}." if note else ""))
                except NoHub:
                    self.say(f"Scan of {Path(p).name} done. Not sent: no team hub yet. Join your team's hub "
                             "(Home → Get set up) to send results.")
                except Exception as e:
                    self.say(f"Scan of {Path(p).name} done, but not sent: {e}")
        except Exception:
            self.say("Scan failed: " + traceback.format_exc().splitlines()[-1])
        finally:
            with self.scan_lock:
                self.scan_now = None
                self._worker_running = False

    # ------------------------------------------------------------- state for the Home page
    def apex_now(self) -> bool:
        """Apex running: what the recorder saw at its last look (every 3 s), else a look at most every 10 s."""
        a = self.auto
        if a and a.s.enabled and getattr(a, "thread", None):
            return bool(getattr(a.rec, "apex", False))
        if time.time() - getattr(self, "_apex_at", 0) > 10:
            self._apex_at = time.time()
            try:
                from pcs_recorder.core import apex_running
                self._apex = apex_running()
            except Exception:
                self._apex = False
        return bool(getattr(self, "_apex", False))

    def game_now(self) -> str:
        """A game other than Apex that holds the heavy work: what the recorder saw at its last look, else a look
        (one shared process snapshot, games.DETECTOR) at most every 10 s."""
        a = self.auto
        if a and a.s.enabled and getattr(a, "thread", None):
            return getattr(a.rec, "game", "") or ""
        if time.time() - getattr(self, "_game_at", 0) > 10:
            self._game_at = time.time()
            try:
                from pcs_recorder import games
                if a:
                    games.configure(a.s)
                self._game = games.other_game()
            except Exception:
                self._game = ""
        return getattr(self, "_game", "") or ""

    def games_info(self) -> dict:
        """Settings → Games: the switches, the games found on this PC, the player's own lists."""
        from pcs_recorder import games
        if self.auto:
            games.configure(self.auto.s)
        return games.DETECTOR.public()

    def state(self) -> dict:
        a = self.auto
        rec = None
        if a:
            from pcs_recorder.settings import default_mbps
            used, n = a.rec.usage()
            s = a.s
            rec = {"enabled": s.enabled, "status": a.rec.status, "recording": a.rec.current is not None,
                   "scanning": a.rec.scan_proc is not None, "waiting": len(a.rec.queue),
                   "used": used, "count": n,
                   "settings": {k: getattr(s, k) for k in REC_FIELDS if hasattr(s, k)},
                   "gbPerHour": round(s.gb_per_hour, 2), "encoder": s.encoder,
                   "bitrateDefault": default_mbps(s.encoder), "bitrateAuto": bool(getattr(s, "bitrate_auto", True)),
                   "settingUp": bool(getattr(a, "_setting_up", False)),   # first-time setup (OBS + encoder test)
                   "check": self.rec_check(),            # the recording check (Home, setup)
                   # the audio tracks' share of gb_per_hour, so the Quality slider shows the same figure as Home
                   "audioKbps": max(0, round(s.gb_per_hour * 8e6 / 3600 - s.bitrate_mbps * 1000))}
        up = getattr(a.rec, "youtube", None) if a else None
        return {"version": core.BUILD.get("version"), "player": core.SETTINGS.get("player", ""), "language": i18n.language(),
                "apex": self.apex_now(),                   # the window polls less often while the game runs
                "game": self.game_now(),                   # another game holds the heavy work: its name, else ""
                "youtube": {"signin": self.youtube, "status": up.status if up else "", "available": up is not None},
                "role": core.SETTINGS.get("role", ""), "coach": getattr(self, "coach", False),
                "teamLink": core.SETTINGS.get("team_link", ""),
                "icons": os.path.getmtime(core.ICONS_F) if os.path.exists(core.ICONS_F) else None,
                "iconsFromHub": self.icons_from_hub(),     # then the team link is optional
                "update": {"tag": self.update[0], "page": self.update[2]} if self.update else None,
                "updateState": dict(self.update_state) if self.update_state else None,   # the "Updating" screen
                "recorder": rec, "messages": list(self.messages)[-60:],
                "hub": self.hub_state(),
                "hubBackup": self.hubbackup.public() if a and a.s.hub_url else None,   # coach's hub key only
                "teamTracks": self.teamtracks.public() if a else None,
                "transcripts": self.comms.public() if a else None,
                "scans": {"now": Path(self.scan_now).name if self.scan_now else None, "queued": len(self.scan_queue),
                          "since": self.scan_started if self.scan_now else None},
                "results": core.OUT}

    # ------------------------------------------------------------- recording check
    def rec_check(self) -> dict | None:
        c = getattr(self.auto, "check", None) if self.auto else None
        return c.public() if c is not None else None

    def rec_check_again(self) -> dict:
        """Home → Check again: the full check in the background (refused while the encoder test runs)."""
        a = self.auto
        c = getattr(a, "check", None) if a else None
        if c is None:
            raise RuntimeError("Auto-record isn't available in this build")
        if getattr(a, "_setting_up", False):
            raise RuntimeError("Wait for the recorder set-up to finish.")
        c.start(manual=True)                              # may start OBS; closed again if it wasn't running
        return c.public()

    # ------------------------------------------------------------- team hub
    def hub_state(self) -> dict:
        """Hub address, key masked (never the key itself), refused / wait, last Test result."""
        from pcs_recorder import hub
        a = self.auto
        return hub.public_state(a.s.hub_url) if a else {"url": "", "key": "", "refused": False, "wait": 0, "last": {}}

    def hub_test(self) -> dict:
        from pcs_recorder import hub
        a = self.auto
        if not a or not a.s.hub_url:
            return {"ok": False, "text": "No hub address"}
        res = hub.test(a.s.hub_url)
        if res.get("https"):                               # http:// of a public hub that only answers on https://
            from pcs_recorder import settings as st
            a.s.hub_url = res["https"]
            hub.reset()
            st.save(a.s)
            res = hub.test(a.s.hub_url)
        if res.get("ok"):
            a.rec.next_upload = 0                          # results waiting for the hub: try now
        return res

    def icons_from_hub(self) -> bool:
        from pcs_recorder import weaponicons
        return weaponicons.from_hub(self.auto.s.hub_url if self.auto else "", core.ICONS_F)

    def _icons_soon(self) -> None:
        """The hub (or its key) changed: look for its weapon icons now, in the background."""
        self._icons_thread = threading.Thread(target=self.sync_icons, args=(0, False), daemon=True)
        self._icons_thread.start()

    def _use_invite(self, url: str, code: str) -> None:
        """Redeem a single-use invite and keep the key it gives and the link's address (replacing an old address:
        the key works only on that hub). Raises hub.HubError with the player's text; never the code."""
        from pcs_recorder import hub, settings as st
        a = self.auto
        key = hub.redeem(url, code)
        hub.set_key(key)                                     # saved at once: the hub gives it only this once
        if a.s.hub_url != url:
            a.s.hub_url = url
            hub.reset()
        st.save(a.s)
        a.rec.next_upload = 0

    def hub_join(self, link: str) -> dict:
        """First-run setup: the sign-in link from the coach (hub address + key in one paste), or a single-use invite
        link (`/join#code=`, redeemed for a key), is saved and tested at once. The link's address replaces the one
        set (the player pasted the coach's link on purpose). The link is never logged nor repeated in an answer or
        an error."""
        from pcs_recorder import hub
        a = self.auto
        if not a:
            raise RuntimeError("Auto-record isn't available in this build")
        code, inv_url = hub.parse_invite(link)
        if code:
            try:
                self._use_invite(inv_url, code)
            except hub.HubError as e:
                res = {"ok": False, "text": str(e)}
                if e.kind == "network":
                    res["tailscale"] = tailscale_state(inv_url)
                return res
            res = self.hub_test()
            if res.get("ok"):
                self._icons_soon()
            else:
                res["tailscale"] = tailscale_state(a.s.hub_url)
            return res
        key, url = hub.parse_key(link)
        if not key:
            raise ValueError("That isn't the link from your coach. Copy the whole link and paste it again.")
        if not url and not a.s.hub_url:
            raise ValueError("Paste the whole link from your coach: it has the hub address in it.")
        self._save_settings({"hub_url": url or a.s.hub_url, "hub_key": link})
        res = self.hub_test()
        if res.get("ok"):
            self._icons_soon()
        else:
            res["tailscale"] = tailscale_state(a.s.hub_url)
        return res

    def open_hub(self, path: str = "/") -> dict:
        """The hub's own pages, in a second app window signed in with the key (see pcs_recorder.hub.page_url).
        Without the app window (no WebView2), the default browser shows the page, without the key."""
        from pcs_recorder import hub
        a = self.auto
        url = a.s.hub_url if a else ""
        if not url:
            raise RuntimeError("No team hub address set (Settings → Team hub)")
        path = path if isinstance(path, str) and path.startswith("/") and not path.startswith("//") else "/"
        if self.open_window(hub.page_url(url, path), "PCS Team hub"):
            return {"window": True}
        import webbrowser
        webbrowser.open(url + path)
        return {"window": False}

    # ------------------------------------------------------------- coach notes on recordings (team hub)
    def _notes_target(self, item_id: str) -> tuple[str, dict]:
        it = self.find(item_id)
        if not it:
            raise RuntimeError("Recording not found")
        return (self.auto.s.hub_url if self.auto else ""), it

    def notes(self, item_id: str, player: str | None = None) -> dict:
        """The coach's notes for a recording (see pcs_recorder.notes): from the hub, or saved here when offline."""
        from pcs_recorder import notes
        url, it = self._notes_target(item_id)
        return notes.fetch(url, it["path"], item_id, player)

    def note_change(self, item_id: str, action: str, body: dict) -> dict:
        """Coach: save / delete a note; anyone: mark notes as seen, "Got it" / reply under a note (`reply`), remove
        an own reply (`unreply`). Returns the recording's notes afterwards."""
        from pcs_recorder import notes
        url, it = self._notes_target(item_id)
        if not url:
            raise RuntimeError("No team hub address set (Settings → Team hub)")
        player = str(body.get("player") or "").strip() or None
        if action == "save":
            notes.save(url, it["path"], player or "", body.get("note") or {})
        elif action == "delete":
            notes.delete(url, body.get("note_id"))
        elif action == "reply":
            notes.reply(url, body.get("note_id"), ack=bool(body.get("ack")), text=str(body.get("text") or ""))
        elif action == "unreply":
            notes.delete_reply(url, body.get("note_id"), body.get("reply_id"))
        elif action == "seen":
            for nid in (body.get("note_ids") or [])[:50]:
                notes.seen(url, nid)
            return {"ok": True}
        else:
            raise ValueError("unknown action")
        return self.notes(item_id, player)

    # ------------------------------------------------------------- minimap pictures (team hub)
    def minimap(self, code: str) -> dict:
        """The Map panel's picture and transform for a map (pcs_recorder.minimaps): downloaded once with the key."""
        from pcs_recorder import minimaps
        return minimaps.get(self.auto.s.hub_url if self.auto else "", code)

    def poi_names(self) -> dict:
        """POI names for the page: the game's names (pcs_maps.json), in the player's language when the team hub has
        the names table (pcs_recorder.eec.app_poi_names)."""
        from pcs_recorder import eec
        return eec.app_poi_names(self.auto.s.hub_url if self.auto else "")

    def squad_paths(self, q: dict) -> dict:
        """Players' paths in one game from the hub (pcs_recorder.squadpaths): `gameKey`, or our recording `id` + `jump`."""
        from pcs_recorder import squadpaths
        url, one = (self.auto.s.hub_url if self.auto else ""), (lambda k: (q.get(k) or [""])[0])
        if one("gameKey"):
            return squadpaths.get(url, game_key=one("gameKey")[:60])
        it = self.find(one("id"))
        if not it:
            raise RuntimeError("Recording not found")
        try:
            jump = float(one("jump"))
        except ValueError:
            jump = None
        return squadpaths.get(url, recording=Path(it["path"]).stem, jump=jump)

    # ------------------------------------------------------------- hub backup copy (coach's hub key only)
    @property
    def hubbackup(self):
        """pcs_recorder.hubbackup.HubBackup: the hub's nightly zip, copied once a day to a folder on this PC."""
        if getattr(self, "_hubbackup", None) is None:
            from pcs_recorder import hubbackup
            self._hubbackup = hubbackup.HubBackup(lambda: self.auto.s.hub_url if self.auto else "",
                                                  self._backup_busy, self.say)
        return self._hubbackup

    def _backup_busy(self) -> str | None:
        """Never while a game runs (Apex or any other), a recording or a scan is going on, or review talk records.
        Also holds the teammates' tracks and the comms transcripts."""
        a = self.auto
        if a and (a.rec.current is not None or a.rec.scan_proc is not None):
            return "the auto-recorder is busy"
        if self.scan_now:
            return "a scan is running"
        t = self.talk
        if t is not None and getattr(t, "active", False):
            return "review talk is recording"
        try:
            g = self._game_running()
            if g:
                return f"{g} is running"
        except Exception:
            pass
        return None

    def hub_backup_tick(self) -> None:
        if self.auto and self.auto.s.hub_url:
            self.hubbackup.tick()

    # ------------------------------------------------------------- watch together (team hub sessions)
    @property
    def watch(self):
        """pcs_recorder.watch.Watch: shared review sessions through the hub (the key stays in this process)."""
        if getattr(self, "_watch", None) is None:
            from pcs_recorder import watch
            self._watch = watch.Watch(lambda: self.auto.s.hub_url if self.auto else "")
            self._watch.on_state = self._talk_event
            self._watch.is_apex_running = self._apex_running
            self._watch.on_apex_left = lambda: self.talk_stop("Apex started")
        return self._watch

    @staticmethod
    def _apex_running() -> bool:
        from pcs_recorder.core import apex_running
        return apex_running()

    def _game_running(self) -> str:
        """Apex ("Apex") or another game's name while the player wants heavy work paused for any game; "" = none.
        Apex through `_apex_running` (tests replace it)."""
        if self._apex_running():
            return "Apex"
        from pcs_recorder import games
        if self.auto:
            games.configure(self.auto.s)
        return games.other_game()

    @property
    def talk(self):
        """pcs_recorder.talk.Talk of the auto-recorder (review talk, coach's app), or None."""
        return getattr(getattr(self.auto, "rec", None), "talk", None) if self.auto else None

    def _talk_event(self, st: dict, offset: float) -> None:
        t = self.talk
        if t is not None and t.active and st.get("id") == t.sid:
            t.offset = offset
            t.event(st)

    def talk_state(self) -> dict:
        from pcs_recorder import talk as talkmod
        t = self.talk
        if t is None:
            return {"available": False, "active": False, "why": "Review talk needs auto-record (Settings → Recording)",
                    "pending": 0}
        return {"available": True, **t.state(), "pending": talkmod.pending_count()}

    def talk_stop(self, why: str = "") -> None:
        """Stop the review talk (if any) and send it to the hub in the background."""
        from pcs_recorder import talk as talkmod
        t = self.talk
        if t is None or not t.active:
            return
        t.stop(self.watch.offset, why)
        url = self.auto.s.hub_url if self.auto else ""
        threading.Thread(target=talkmod.send_pending, args=(url,), daemon=True, name="talk-upload").start()

    def talk_retry(self, every_s: float = 300) -> None:
        """Review talk the hub didn't take yet (hub down, app closed): tried again, at most every 5 min."""
        from pcs_recorder import talk as talkmod
        if self.talk is None or self.talk.active or time.time() - getattr(self, "_talk_tried", 0) < every_s:
            return
        self._talk_tried = time.time()
        if talkmod.pending_count():
            threading.Thread(target=talkmod.send_pending, args=(self.auto.s.hub_url,), daemon=True).start()

    def review_extras(self, item_id: str, player: str | None = None) -> dict:
        it = self.find(item_id)
        if not it:
            raise RuntimeError("Recording not found")
        return self.watch.extras(it["path"], player)

    def watch_post(self, action: str, body: dict) -> dict:
        w = self.watch
        if action == "talk":
            if body.get("on"):
                if not w.sid or w.ended or (w.me or {}).get("role") != "coach":
                    raise RuntimeError("Only the coach records review talk, in a running session")
                if self.talk is None:
                    raise RuntimeError("Review talk needs auto-record (Settings → Recording)")
                self.talk.start(w.sid, w.state)
            else:
                self.talk_stop("by the coach")
            return self.talk_state()
        if action == "pointer":
            return w.pointer(body)
        if action == "flag":
            return w.flag(body.get("t"))
        if action == "bookmark":
            return w.bookmark(body.get("text") or "", body.get("t"))
        if action in ("start", "join") and self._apex_running():
            raise RuntimeError("Close Apex to watch together")    # a review never shares the PC with a game
        if action == "start":
            return w.start(body.get("gameKey") or "", body.get("plan") or None)
        if action == "join":
            return w.join(body.get("id") or "")
        if action == "control":
            return w.control(body)
        if action == "draw":
            return w.draw(body)
        if action == "report":
            w.report(body.get("t"), bool(body.get("buffering")), bool(body.get("browsing")))
            return {"ok": True}
        if action == "leave":
            self.talk_stop("left the session")
            w.leave()
            return w.snapshot()
        if action == "end":
            self.talk_stop("session ended")
            w.end()
            return w.snapshot()
        raise ValueError("unknown action")

    # ------------------------------------------------------------- coach
    def check_coach(self) -> bool:
        """The Coach role is only for the coach's hub key: the team hub says which key is the coach's (`/api/me`),
        checked here, not just the menu choice. No address or account name is built into the app."""
        self.coach = False
        if core.SETTINGS.get("role") != "Coach" or not self.auto:
            return False
        try:
            self.coach = self.hubbackup.role(ask=True) == "coach"
        except Exception:
            self.coach = False
        return self.coach

    def require_coach(self) -> None:
        if not getattr(self, "coach", False):
            raise PermissionError("Only the coach can do this (Settings → Role: Coach, with the coach's hub key).")

    def save_settings(self, body: dict) -> None:
        body = dict(body)
        role = body.pop("role", None)
        self._save_settings(body)
        if role is not None:                                 # last: refusing Coach must not lose the rest
            self._save_settings({"role": role})

    @staticmethod
    def _check_voice(body: dict) -> None:
        """The voice app: Discord, TeamSpeak, or another program given by its name only (e.g. Mumble.exe)."""
        from pcs_recorder import settings as st
        if "voice_app" not in body and "voice_exe" not in body:
            return
        app = body.get("voice_app", "discord")
        if app not in st.VOICE_APPS:
            raise ValueError("Unknown voice app.")
        exe = str(body.get("voice_exe") or "").strip()
        if app == "other" and not st.valid_exe(exe):
            raise ValueError("Pick the voice app's program, or type its name (like Mumble.exe).")
        if "voice_exe" in body:
            body["voice_exe"] = st.valid_exe(exe) or ""      # Discord / TeamSpeak: a leftover typed name is dropped

    def _save_settings(self, body: dict) -> None:
        self._check_voice(body)                              # before anything is saved
        # hub key: a key or the coach's sign-in link; "" = unchanged, None = remove. Checked before anything is
        # saved, and never echoed back (errors don't repeat what was pasted).
        from pcs_recorder import hub
        new_key, link_url, remove_key = None, None, False
        if "hub_key" in body:
            raw = body.pop("hub_key")
            code, inv_url = hub.parse_invite(raw) if raw is not None else (None, None)
            if code:                                         # a single-use invite: redeemed now, key and address kept
                if not self.auto:
                    raise RuntimeError("Auto-record isn't available in this build")
                try:
                    self._use_invite(inv_url, code)
                except hub.HubError as e:
                    raise ValueError(str(e)) from None
                body.pop("hub_url", None)                    # the invite's address wins over the field
                self._icons_soon()
            elif raw is None:
                remove_key = True
            elif str(raw).strip():
                new_key, link_url = hub.parse_key(raw)
                if not new_key:
                    raise ValueError("That isn't a hub key. Paste the key or the sign-in link from your coach.")
        # hub backup copy (coach's hub key only): checked before anything is saved
        bk = {k: body.pop(k) for k in ("hub_backup_dir", "hub_backup_keep") if k in body}
        if bk and not (self.auto and self.hubbackup.is_coach()):
            bk = {}                                          # not the coach's key: the block isn't shown, ignored
        if bk:
            from pcs_recorder import hubbackup
            bk = hubbackup.validate(bk.get("hub_backup_dir"), bk.get("hub_backup_keep"))
        if "player" in body:
            core.SETTINGS["player"] = str(body["player"]).strip()
        if "language" in body:                              # the app's language: the page reloads in it
            code = str(body.pop("language"))
            if not any(l["code"] == code for l in i18n.languages()):
                raise ValueError("Unknown language")
            core.SETTINGS["language"] = code
            i18n.set_language(code)
            icon = getattr(getattr(self.auto, "ui", None), "icon", None)
            if icon:
                try:
                    icon.update_menu()                       # the tray menu's words
                except Exception:
                    pass
        body.pop("coach_email", None)       # field removed (coach role = hub key); an old page may still send it
        if "team_link" in body:
            link = str(body["team_link"]).strip()
            if link and not core.drive_link_id(link):
                raise ValueError("The team link must be a Google Drive link. Paste the link your coach sent.")
            if link != core.SETTINGS.get("team_link", ""):
                core.SETTINGS["team_link"] = link
                core.save_settings()
                if link:                                     # fetch now, in the background: the result shows in Activity
                    threading.Thread(target=self.sync_icons, args=(0, True), daemon=True).start()
        if "role" in body:
            before = core.SETTINGS.get("role", "")
            core.SETTINGS["role"] = str(body["role"])
            if body["role"] == "Coach" and not self.check_coach():
                core.SETTINGS["role"] = before
                core.save_settings()
                raise PermissionError("Coach is reserved for the coach's hub key. Add it first "
                                      "(Settings → Team hub), then choose Coach again.")
            if body["role"] != "Coach":
                self.coach = False
        core.save_settings()
        a = self.auto
        if a:
            a.s.player = core.SETTINGS.get("player", "")
            before = (a.s.start_with_windows,)
            yt_before = getattr(a.s, "yt_enabled", False)
            hub_before = a.s.hub_url
            share_before = (a.s.share_tracks, a.s.transcript_consent)
            for k in REC_FIELDS:
                if k in body:
                    if k == "bitrate_mbps" and hasattr(a.s, "set_bitrate"):
                        a.s.set_bitrate(body[k])           # the encoder's default unless the player picks another
                    else:
                        setattr(a.s, k, body[k])
            if "share_tracks" in body and a.s.share_tracks != share_before[0]:
                a.s.share_asked = True                       # answered in Settings: the setup step stops asking
            if "transcript_consent" in body and a.s.transcript_consent != share_before[1]:
                a.s.transcript_asked = True
            if link_url and not str(a.s.hub_url or "").strip():
                a.s.hub_url = link_url                       # the sign-in link gives the address too
            from pcs_recorder import settings as st
            st.save(a.s)
            from pcs_recorder import games
            games.configure(a.s)                             # Settings → Games: at once, not at the next tick
            if new_key or remove_key:
                hub.set_key(new_key)
                a.rec.next_upload = 0                        # results waiting for the hub: try with the new key
            if a.s.hub_url != hub_before:
                hub.reset()
                a.rec.next_upload = 0
            if a.s.hub_url and (new_key or a.s.hub_url != hub_before):
                self._icons_soon()                           # the hub may have the weapon icons
            if getattr(a.s, "yt_enabled", False) and not yt_before:
                a.s.yt_since = time.time()                 # only blocks recorded from now on
                st.save(a.s)
            if (a.s.start_with_windows,) != before:
                self.set_start_with_windows(a.s.start_with_windows)
            if bk:
                self.hubbackup.set(*bk)
            self._sync_sharing()
            removed = a.rec.cleanup()
            if removed:
                self.say(f"{len(removed)} old recording(s) removed to stay within your limits.")

    def setup_choice(self, choice: str, on: bool) -> None:
        """Home → Get set up → Your choices. Each one is applied at once and can be changed later in Settings.
        share: yes = the team agreed that comms are recorded, the mic is recorded and sent ("Send my mic track" on),
        shared with the team, and the teammates' tracks come by themselves; no = nothing changes but the answer.
        transcripts: yes = the own mic track is turned into text after sessions (pcs_recorder.comms; also needs the
        comms consent and the mic on). autorecord: on / off."""
        a = self.auto
        if not a:
            raise RuntimeError("Auto-record isn't available in this build")
        from pcs_recorder import settings as st
        if choice == "autorecord":
            return self.set_autorecord(on)
        if choice == "share":
            a.s.share_asked, a.s.share_tracks = True, on
            if on:
                a.s.comms_consent = a.s.mic_audio = a.s.send_mic = True
        elif choice == "transcripts":
            a.s.transcript_asked, a.s.transcript_consent = True, on
        else:
            raise ValueError("unknown choice")
        st.save(a.s)
        self._sync_sharing()

    def _sync_sharing(self) -> None:
        """The player's mic sharing answer to the hub, in the background (never during a game: the next tick)."""
        if not (self.auto and self.auto.s.hub_url) or self._backup_busy():
            return
        threading.Thread(target=self.teamtracks.sync_sharing, daemon=True, name="share-sync").start()

    @property
    def teamtracks(self):
        """pcs_recorder.teamtracks.TeamTracks: teammates' mic tracks, fetched when the player agreed."""
        if getattr(self, "_teamtracks", None) is None:
            from pcs_recorder import teamtracks
            self._teamtracks = teamtracks.TeamTracks(lambda: self.auto.s, lambda: self.auto.rec.idx if self.auto else {},
                                                     self._backup_busy)
        return self._teamtracks

    def team_tracks_tick(self) -> None:
        if self.auto and self.auto.s.hub_url:
            self.teamtracks.tick()

    # ------------------------------------------------------------- clips for Discord (review player)
    @property
    def clipper(self):
        """pcs_recorder.clips.Clipper: one clip at a time, never while Apex runs or a game records."""
        if getattr(self, "_clipper", None) is None:
            from pcs_recorder import clips
            self._clipper = clips.Clipper(self._clip_busy)
        return self._clipper

    def _clip_busy(self) -> str | None:
        a = self.auto
        if a and a.rec.current is not None:
            return "a game is being recorded"
        try:
            g = self._game_running()
            if g:
                return f"{g} is running. Close {g} to make a clip"
        except Exception:
            pass
        return None

    def _clip_team(self, path: str, ids: list[str]) -> tuple[list, list[str]]:
        """The teammates' mic tracks asked for that may go into a clip: downloaded, and the hub says right now that
        the teammate still shares it. ([(file, parts, player)], [what was left out and why])"""
        ids = [str(i) for i in ids or [] if re.fullmatch(r"\d{1,10}", str(i))]
        if not ids:
            return [], []
        from pcs_recorder import teamtracks, hub
        from urllib.parse import quote
        have = {t["id"]: t for t in (self.teamtracks.for_recording(path) if self.auto else [])}
        names = [have[i]["player"] + "'s mic" if i in have else "a teammate's mic" for i in ids]
        if not self.auto or not teamtracks.active(self.auto.s):
            return [], [f"{n}: your own mic sharing is off" for n in names]
        try:
            r = hub.request(self.auto.s.hub_url, "GET", "/api/team-tracks?recording=" + quote(Path(path).stem), timeout=8)
            hub._check(r)
            still = {str(t.get("scan_id")) for g in r.json().get("games") or [] for t in g.get("tracks") or []}
        except Exception as e:
            import logging
            logging.getLogger("clips").info("teammates' tracks left out, hub not asked: %s", e)
            return [], [f"{n}: the team hub could not confirm they still share it" for n in names]
        out, left = [], []
        for i, n in zip(ids, names):
            f = self.teamtracks.file(path, i) if i in have else None
            if f and i in still:
                out.append((str(f), have[i]["parts"], have[i]["player"]))
            else:
                left.append(f"{n}: not shared any more" if f else f"{n}: not downloaded")
        return out, left

    def make_clip(self, body: dict) -> dict:
        """Review player → Make clip. body: id, start, end, size (discord | full), tracks [own track numbers],
        team [teammate track ids], notes (bool), player (whose notes: the coach looking at a player's recording)."""
        from pcs_recorder import clips
        it = self.find(str(body.get("id") or ""))
        if not it:
            raise RuntimeError("Recording not found")
        why = self._clip_busy()
        if why:
            raise RuntimeError(why + ".")
        try:
            start, end = float(body.get("start")), float(body.get("end"))
        except (TypeError, ValueError):
            raise RuntimeError("Set a start and an end first.") from None
        size = body.get("size") if body.get("size") in ("discord", "full") else "discord"
        src = clips.probe(it["path"])
        if src["dur"]:
            end = min(end, src["dur"])
        if not (0 <= start and end - start >= clips.MIN_S):
            raise RuntimeError("The clip must be at least 1 second long.")
        if size == "full" and end - start > clips.FULL_MAX_S:
            raise RuntimeError(f"A clip can be at most {clips.FULL_MAX_S // 60} minutes long.")
        tracks = {t["n"]: t for t in audio_tracks(it["path"])}
        own = []
        for n in body.get("tracks") or []:
            t = tracks.get(n) if isinstance(n, int) else None
            if not t:
                continue
            if t["key"] in clips.OWN_TRACKS_LEFT_OUT:          # teammates' voices: only through their own mic tracks
                raise RuntimeError("The voice app track can't go into a clip. Add your teammates' own mic tracks instead.")
            own.append(n)
        team, left = self._clip_team(it["path"], body.get("team") or [])
        n_audio = len(own) + len(team)
        if size == "discord" and clips.discord_video_kbps(end - start, n_audio) < clips.MIN_VIDEO_KBPS:
            raise RuntimeError(f"Too long for 10 MB. Make it {clips.discord_max_s(n_audio)} s or shorter, or pick Full quality.")
        notes = []
        if body.get("notes"):
            try:
                notes = self.notes(it["id"], body.get("player") or None).get("notes") or []
            except Exception:
                notes = []
        return self.clipper.start({"video": it["path"], "start": start, "end": end, "size": size, "src": src,
                                   "own": own, "team": team, "notes": notes, "leftOut": left,
                                   "obs_encoder": self.auto.s.encoder if self.auto else ""})

    def clip_file_action(self, action: str, file: str) -> dict:
        """Show in folder / Copy: only on a clip this app made."""
        f = self.clipper.own_clip(file)
        if not f:
            raise RuntimeError("Clip not found.")
        (self.reveal_file if action == "show" else self.copy_file)(f)
        return {"ok": True}

    @property
    def comms(self):
        """pcs_recorder.comms.Comms: the player's own mic track as text after a session, when they said yes."""
        if getattr(self, "_comms", None) is None:
            from pcs_recorder import comms, hub
            a = self.auto
            send = (lambda files: a.rec.hub.upload_hub(files)) if a else None
            self._comms = comms.Comms(lambda: self.auto.s, lambda: self.auto.rec.idx if self.auto else {},
                                      self._backup_busy, send, version=str(core.BUILD.get("version") or ""),
                                      hub_ok=lambda: hub.has_feature(self.auto.s.hub_url, "comms"))
        return self._comms

    def comms_tick(self) -> None:
        if self.auto:
            self.comms.cleanup()
            self.comms.tick()

    def minimaps_tick(self) -> int:
        """The hub's map pictures in the data folder, so scans can read the player's position from the HUD minimap
        (pcs_minimap). Not while a game runs; a picture already here is not downloaded again."""
        if not (self.auto and self.auto.s.hub_url):
            return 0
        if self._game_running():
            return 0
        from pcs_recorder import minimaps
        return minimaps.prefetch(self.auto.s.hub_url)

    def set_autorecord(self, on: bool) -> None:
        a = self.auto
        if not a:
            raise RuntimeError("Auto-record isn't available in this build")
        if on and not a.s.enabled:
            a.s.enabled = True
            from pcs_recorder import settings as st
            st.save(a.s)
            a.enable()                                      # private OBS + encoder test on first use
        elif not on and a.s.enabled:
            a.s.enabled = False
            from pcs_recorder import settings as st
            st.save(a.s)
            self.say("Auto-record is off.")


class Server:
    def __init__(self, backend: Backend, port: int = 0):
        self.b = backend
        self.token = secrets.token_urlsafe(18)
        handler = self._handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def start(self) -> None:
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()

    def _handler(self):
        srv = self
        b = self.b

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            # -- helpers
            def _json(self, obj, code=200, headers=None):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":                  # a body after a HEAD answer breaks the next request
                    self.wfile.write(data)

            def _ok_origin(self) -> bool:
                host = self.headers.get("Host", "")
                return host in (f"127.0.0.1:{srv.port}", f"localhost:{srv.port}")

            def _authed(self, q) -> bool:
                tok = self.headers.get("X-PCS-Token") or (q.get("t") or [""])[0]
                return self._ok_origin() and secrets.compare_digest(tok, srv.token)

            def _body(self) -> dict:
                n = int(self.headers.get("Content-Length") or 0)
                if not n:
                    return {}
                if n > MAX_BODY:                            # the page only sends small JSON (a drawing at most)
                    raise ValueError("request too large")
                try:
                    d = json.loads(self.rfile.read(n))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return {}
                return d if isinstance(d, dict) else {}

            # -- static pages
            def _static(self, path):
                rel = path.lstrip("/") or "index.html"
                f = (WEB / rel).resolve()
                if WEB.resolve() not in f.parents or not f.is_file():
                    return self._json({"error": "not found"}, 404)
                data = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)

            def _file(self, f, ctype):
                data = Path(f).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            # -- video with seeking
            def _audio(self, item_id, n):
                it = b.find(item_id)
                if not it:
                    return self._json({"error": "not found"}, 404)
                try:
                    st, p = b.cutter.state(it["path"], int(n))
                except Exception as e:
                    return self._json({"error": f"audio track not available: {e}"}, 404)
                if st == "preparing":                       # being cut in the background: the page asks again
                    return self._json({"error": "preparing", "preparing": True}, 503, {"Retry-After": "3"})
                if st != "ready":
                    return self._json({"error": f"audio track not available: {p}"}, 404)
                return self._video(item_id, str(p))

            def _video(self, item_id, p=None):
                if p is None:                               # a recording; else a file the caller already checked
                    it = b.find(item_id)
                    if not it:
                        return self._json({"error": "not found"}, 404)
                    p = it["path"]
                try:
                    size = os.path.getsize(p)
                except FileNotFoundError:
                    return self._json({"error": "file missing"}, 404)
                except OSError:                             # the review player says "can't be read"
                    return self._json({"error": "file can't be read"}, 500)
                start, end = 0, size - 1
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    a, _, z = rng[6:].split(",")[0].partition("-")
                    if a:
                        start = int(a)
                        end = int(z) if z else size - 1
                    else:                                   # "bytes=-N": the last N bytes
                        start = max(0, size - int(z))
                    end = min(end, size - 1)
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    self.send_response(200)
                ctype = ("audio/mp4" if p.lower().endswith(".m4a") else
                         "video/mp4" if p.lower().endswith((".mp4", ".mov")) else "video/x-matroska")
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                if self.command == "HEAD":
                    return
                with open(p, "rb") as fh:
                    fh.seek(start)
                    left = end - start + 1
                    try:
                        while left > 0:
                            chunk = fh.read(min(1 << 20, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass                                # the player seeked elsewhere

            # -- routing
            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                path = unquote(u.path)
                if not path.startswith(("/api/", "/video/", "/audio/", "/talk/", "/minimap/", "/teamtrack/")):
                    if not self._ok_origin():
                        return self._json({"error": "forbidden"}, 403)
                    return self._static(path)
                if not self._authed(q):
                    return self._json({"error": "forbidden"}, 403)
                try:
                    if path.startswith("/minimap/"):            # the map picture kept in the data folder
                        from pcs_recorder import minimaps
                        pic = minimaps.picture(path.split("/")[2])
                        return self._file(*pic) if pic else self._json({"error": "not found"}, 404)
                    if path.startswith("/api/minimap/"):
                        return self._json(b.minimap(path.split("/")[3]))
                    if path == "/api/pois":                      # POI names (game names; hub language if any)
                        return self._json(b.poi_names())
                    if path == "/api/squadpaths":                # teammates' paths on the map (minimap.js)
                        return self._json(b.squad_paths(q))
                    if path.startswith("/talk/"):               # review talk from the hub (downloaded once)
                        return self._video(None, str(b.watch.audio(path.split("/")[2])))
                    if path.startswith("/audio/"):
                        parts = path.split("/")
                        return self._audio(parts[2], parts[3])
                    if path.startswith("/teamtrack/"):          # a teammate's mic track, downloaded from the hub
                        parts = path.split("/")
                        it = b.find(parts[2]) if len(parts) > 3 else None
                        f = b.teamtracks.file(it["path"], parts[3]) if it else None
                        return self._video(None, str(f)) if f else self._json({"error": "not found"}, 404)
                    if path.startswith("/video/"):
                        return self._video(path.split("/")[2])
                    if path == "/api/state":
                        return self._json(b.state())
                    if path == "/api/i18n":                     # the page's string tables (i18n.js, before it draws)
                        return self._json(i18n.tables())
                    if path == "/api/library":
                        return self._json(b.library())
                    if path == "/api/youtube/queue":
                        return self._json(b.yt_queue())
                    if path == "/api/mics":
                        return self._json(b.mics())
                    if path == "/api/voiceapps":
                        return self._json(b.voice_apps())
                    if path == "/api/watch/list":
                        b.talk_retry()
                        return self._json(b.watch.listing(fresh=bool((q.get("fresh") or [""])[0])))
                    if path == "/api/watch/poll":
                        snap = b.watch.poll(int((q.get("seq") or ["-1"])[0]), float((q.get("wait") or ["15"])[0]))
                        if snap.get("ended"):
                            b.talk_stop("session ended")        # ended by the hub (idle, replaced): talk too
                        return self._json(snap)
                    if path == "/api/clips/state":
                        return self._json(b.clipper.state())
                    if path == "/api/games":                     # Settings → Games
                        return self._json(b.games_info())
                    if path == "/api/watch/talk":
                        return self._json(b.talk_state())
                    if path.startswith("/api/library/") and path.endswith("/extras"):
                        return self._json(b.review_extras(path.split("/")[3], (q.get("player") or [""])[0] or None))
                    if path.startswith("/api/library/") and path.endswith("/notes"):
                        return self._json(b.notes(path.split("/")[3], (q.get("player") or [""])[0] or None))
                    if path.startswith("/api/library/") and path.endswith("/levels"):
                        d = b.levels(path.split("/")[3])
                        return self._json(d) if d else self._json({"error": "not found"}, 404)
                    if path.startswith("/api/library/"):
                        d = b.detail(path.split("/")[3])
                        return self._json(d) if d else self._json({"error": "not found"}, 404)
                    return self._json({"error": "not found"}, 404)
                except Exception as e:
                    return self._json({"error": str(e)}, 500)

            def do_POST(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                path = unquote(u.path)
                if not self._authed(q):
                    return self._json({"error": "forbidden"}, 403)
                try:
                    body = self._body()
                except ValueError as e:
                    self.close_connection = True                # the unread body stays on the connection
                    return self._json({"error": str(e)}, 413)
                try:
                    if path == "/api/settings":
                        b.save_settings(body)
                        return self._json(b.state())
                    if path == "/api/hub/test":
                        return self._json(b.hub_test() | {"hub": b.hub_state()})
                    if path == "/api/hub/join":
                        return self._json(b.hub_join(str(body.get("link") or "")) | {"hub": b.hub_state()})
                    if path == "/api/hub/open":
                        return self._json(b.open_hub(body.get("path") or "/"))
                    if path == "/api/hubbackup/copy":
                        if not b.auto or not b.auto.s.hub_url:
                            raise RuntimeError("No team hub address set (Settings → Team hub)")
                        return self._json(b.hubbackup.copy_now() | {"hubBackup": b.hubbackup.public()})
                    if path == "/api/autorecord":
                        b.set_autorecord(bool(body.get("enabled")))
                        return self._json(b.state())
                    if path == "/api/youtube/signin":
                        b.yt_signin()
                        return self._json({"ok": True})
                    if path == "/api/youtube/eec":
                        return self._json({"eec": b.set_eec(body.get("id", ""), body.get("links", []))})
                    if path == "/api/youtube/results":
                        paths = body.get("paths") or b.pick_files("eec") or []
                        return self._json({"added": b.add_eec_files(paths)})
                    if path == "/api/youtube/upload":
                        return self._json({"queued": b.yt_upload(body.get("id", ""))})
                    if path == "/api/youtube/block":
                        return self._json(b.yt_block(body.get("id", ""), body.get("action", ""),
                                                     body.get("title"), body.get("privacy")))
                    if path in ("/api/notes/save", "/api/notes/delete", "/api/notes/seen", "/api/notes/reply",
                                "/api/notes/unreply"):
                        return self._json(b.note_change(str(body.get("id") or ""), path.rsplit("/", 1)[1], body))
                    if path.startswith("/api/watch/"):
                        return self._json(b.watch_post(path.rsplit("/", 1)[1], body))
                    if path == "/api/clips/make":
                        try:
                            return self._json(b.make_clip(body))
                        except RuntimeError as e:
                            return self._json({"error": str(e)}, 400)
                    if path == "/api/clips/stop":
                        b.clipper.stop()
                        return self._json({"ok": True})
                    if path in ("/api/clips/show", "/api/clips/copy"):
                        try:
                            return self._json(b.clip_file_action(path.rsplit("/", 1)[1], str(body.get("file") or "")))
                        except RuntimeError as e:
                            return self._json({"error": str(e)}, 404)
                        except OSError as e:
                            return self._json({"error": str(e)}, 500)
                    if path == "/api/label":
                        return self._json({"label": b.set_label(body.get("id", ""), body.get("label") or None)})
                    if path == "/api/update":
                        why = b.start_update(show=True)            # the window's button: the window comes back
                        return self._json({"error": why}, 409) if why else self._json({"ok": True, "updateState": b.update_state})
                    if path == "/api/update/back":
                        return self._json({"ok": b.update_back()})
                    if path == "/api/update/check":
                        threading.Thread(target=b.check_update, args=(UPDATE_ON_OPEN_S,), daemon=True).start()
                        return self._json({"ok": True})
                    if path == "/api/library/pick":
                        b.require_coach()
                        n = b.add_paths(b.pick_files("video") or [])
                        return self._json({"added": n})
                    if path == "/api/library/add":
                        b.require_coach()
                        return self._json({"added": b.add_paths(body.get("paths", []))})
                    if path == "/api/library/delete":
                        return self._json(b.delete(body.get("ids", [])))
                    if path == "/api/library/forget":
                        lib = core.SETTINGS.get("library", [])
                        it = b.find(body.get("id", ""))
                        if it and it["path"] in lib:
                            lib.remove(it["path"])
                            core.save_settings()
                        return self._json({"ok": True})
                    if path == "/api/rescan":
                        return self._json({"queued": b.rescan(body.get("ids", []))})
                    if path == "/api/scan":
                        return self._json({"queued": b.queue_scans(body.get("ids", []))})
                    if path == "/api/open":
                        what = body.get("what")
                        if what == "results":
                            b.open_path(core.OUT)
                        elif what == "recordings" and b.auto:
                            b.open_path(str(b.auto.s.folder_path))
                        elif what == "obslogs":
                            from pcs_recorder import obs as obsmod
                            logs = obsmod.CONFIG / "logs"
                            b.open_path(str(logs if logs.exists() else obsmod.OBS_ROOT))
                        elif what == "file":
                            it = b.find(body.get("id", ""))
                            if it:
                                b.open_path(it["path"])
                        return self._json({"ok": True})
                    if path == "/api/encoder/retest":
                        a = b.auto
                        if not a:
                            return self._json({"error": "Auto-record isn't available in this build"}, 400)
                        if a.rec.current is not None or b.update_blocked():
                            return self._json({"error": "Close Apex (and wait for the scan) first."}, 409)
                        from pcs_recorder import obs as obsmod
                        from pcs_recorder import settings as st

                        def retest():
                            try:
                                obsmod.close()
                            except Exception:
                                pass
                            a.s.encoder = ""
                            a.s.enabled = True
                            st.save(a.s)
                            a.enable()
                        threading.Thread(target=retest, daemon=True).start()
                        b.say("Testing the graphics card's encoders again (a few seconds)…")
                        return self._json({"ok": True})
                    if path == "/api/reccheck":                # Home → Recording check → Check again
                        return self._json({"check": b.rec_check_again()})
                    if path == "/api/setup/choice":            # first-run setup: one choice, applied at once
                        b.setup_choice(str(body.get("choice") or ""), bool(body.get("on")))
                        return self._json(b.state())
                    if path == "/api/quit":
                        threading.Timer(0.3, b.quit).start()
                        return self._json({"ok": True})
                    return self._json({"error": "not found"}, 404)
                except PermissionError as e:
                    return self._json({"error": str(e)}, 403)
                except Exception as e:
                    return self._json({"error": str(e)}, 500)

        return H
