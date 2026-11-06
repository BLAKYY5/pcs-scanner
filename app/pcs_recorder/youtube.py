"""Scrim blocks to the team YouTube channel: one video per player per block.

A block = the player's recordings that follow each other with less than `yt_gap_min` minutes between them
(usually one recording, several if Apex was restarted during the block). Once the block is over (no recording
for that long) and every recording in it is scanned, the recordings are joined into one video (no
re-encoding of the picture), with YouTube chapters for each game from the scan. Uploads only run while Apex
is closed and pause between chunks if it starts.

Sound on YouTube: the game only (default), or game + Discord + mic mixed when the team agreed to comms
recording. The other tracks never leave the PC.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import retention
from .settings import APP_DIR, Settings

log = logging.getLogger("recorder.youtube")
STATE_FILE = APP_DIR / "youtube.json"
MAP_NAMES = {"WE": "World's Edge", "SP": "Storm Point", "ED": "E-District", "OL": "Olympus",
             "BM": "Broken Moon", "KC": "Kings Canyon"}
DONE_STATES = ("scanned", "uploaded")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=1), encoding="utf-8")


def end_of(e: retention.Entry) -> float:
    try:
        return Path(e.path).stat().st_mtime
    except OSError:
        return e.started


def blocks(idx: dict[str, retention.Entry], gap_min: int) -> list[list[str]]:
    """Recording keys grouped into blocks, oldest first."""
    keys = sorted((k for k, e in idx.items() if e.path), key=lambda k: idx[k].started)
    out: list[list[str]] = []
    for k in keys:
        if out and idx[k].started - end_of(idx[out[-1][-1]]) < gap_min * 60:
            out[-1].append(k)
        else:
            out.append([k])
    return out


def scan_of(e: retention.Entry) -> dict:
    f = Path(e.path).with_suffix(".scan") / (Path(e.path).stem + ".scan.json")
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def events_of(e: retention.Entry) -> dict:
    try:
        return json.loads(Path(e.path).with_suffix(".events.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def duration(path: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=60, creationflags=_quiet())
    return float(r.stdout.strip() or 0)


def fmt(t: float) -> str:
    t = int(max(0, t))
    h, m, s = t // 3600, t // 60 % 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


EEC_RE = re.compile(r"^(?:https?://)?(?:www\.)?eternalesports\.club(/[\w\-./?=&%#]*)?$", re.I)


def normalize_eec(link: str) -> str | None:
    """An EEC page link (lobby, usually https://eternalesports.club/lobbies/<n>/<code>), or None."""
    link = (link or "").strip()
    m = EEC_RE.match(link)
    if not m:
        return None
    return "https://eternalesports.club" + (m.group(1) or "/")


def our_drop(g: dict) -> str | None:
    spot = next((x for x in (g.get("drop") or {}).get("spots", []) if x.get("kind") == "ours"), None)
    if not spot or not spot.get("poi"):
        return None
    words = spot["poi"].split("-")[1:] or spot["poi"].split("-")
    return " ".join(w.capitalize() for w in words)


def tags_for(s: Settings, desc: str) -> list[str]:
    tags = ["Apex Legends", "scrims", "EEC", s.yt_team or "PCS", s.player] + [m for m in MAP_NAMES.values() if m in desc]
    return [t for t in dict.fromkeys(tags) if t][:15]


def describe(s: Settings, idx, keys: list[str], offsets: list[float], links: list[str] | None = None,
             audio: str = "game") -> tuple[str, str, int]:
    """Title, description with chapters, and the number of games."""
    games, marks = [], []
    for k, off in zip(keys, offsets):
        for g in scan_of(idx[k]).get("games", []):
            if g.get("tStart") is not None:
                games.append((off + g["tStart"], g.get("map"), g.get("mode"), our_drop(g),
                              (g.get("landing") or {}).get("landedAtExact")))
        for m in events_of(idx[k]).get("events", []):
            if m.get("kind") == "mark":
                marks.append(off + m["t"])
    games.sort(key=lambda x: x[0])
    start = dt.datetime.fromtimestamp(idx[keys[0]].started)
    team = s.yt_team or "PCS"
    player = s.player or "Player"
    maps = ", ".join(dict.fromkeys(MAP_NAMES.get(g[1], g[1] or "?") for g in games)) or "no game found"
    scrim = any(g[2] == "scrim" for g in games)
    title = (f"{'Scrims' if scrim else 'Session'} {start:%d.%m.%Y %H:%M} · {team} · {player} POV · "
             f"{len(games)} game{'s' if len(games) != 1 else ''} ({maps})")[:100]

    def label(i, g):
        return f"Game {i} · {MAP_NAMES.get(g[1], g[1] or '?')}" + (f" · {g[3]}" if g[3] else "")
    # YouTube chapters: start at 0:00, ascending, at least 10 s apart, at least 3 of them
    chapters = [(0.0, "Lobby")]
    for i, g in enumerate(games, 1):
        if g[0] - chapters[-1][0] >= 10:
            chapters.append((g[0], label(i, g)))
        elif len(chapters) == 1:
            chapters[0] = (0.0, label(i, g))               # the recording starts right at the game
    lines = [f"{team} · {player} POV · {'EEC scrims' if scrim else 'Apex session'} · {start:%A %d %B %Y, %H:%M}"]
    if links:
        lines += [("EEC lobby: " if len(links) == 1 else "EEC lobbies: ") + "  ".join(links)]
    lines.append("")
    if len(chapters) >= 3:
        lines += [f"{fmt(t)} {name}" for t, name in chapters] + [""]
    elif games:
        lines += [f"{fmt(g[0])} {label(i, g)}" for i, g in enumerate(games, 1)] + [""]
    if marks:
        lines += ["Marked moments: " + ", ".join(fmt(t) for t in sorted(marks)), ""]
    lines.append("Sound: " + ("game + team comms" if audio == "mix" else "game only") + ".")
    lines.append("Recorded and uploaded by PCS Scanner.")
    return title, "\n".join(lines), len(games)


def build_video(paths: list[str], tracks: list[list[str]], audio: str, out: Path) -> Path:
    """Join the block's recordings into one file with a single sound track (game, or the mix).
    The picture is copied as is; only the sound is encoded (seconds per hour of video)."""
    tmp_dir = out.parent / (out.stem + ".parts")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    for i, (p, tr) in enumerate(zip(paths, tracks)):
        part = tmp_dir / f"part{i}.mp4"
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", p, "-map", "0:v:0"]
        n = max(1, len(tr))
        game = tr.index("game") if "game" in tr else 0
        if audio == "mix" and n > 1:
            cmd += ["-filter_complex", "".join(f"[0:a:{j}]" for j in range(n)) +
                    f"amix=inputs={n}:duration=longest:normalize=0[a]", "-map", "[a]"]
        else:
            cmd += ["-map", f"0:a:{game}?"]
        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(part)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=3600, creationflags=_quiet())
        parts.append(part)
    if len(parts) == 1:
        shutil.move(str(parts[0]), out)
    else:
        lst = tmp_dir / "list.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", "-movflags", "+faststart", str(out)], check=True, capture_output=True,
                       timeout=3600, creationflags=_quiet())
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return out


class Uploader:
    """Called from the recorder loop while Apex is closed; the work itself runs in its own thread."""

    def __init__(self, s: Settings, rec, ui, service, is_apex_running, clock=time.time):
        self.s, self.rec, self.ui = s, rec, ui
        self.service = service                    # () -> YouTube API client, or None when not signed in
        self.is_apex_running, self.clock = is_apex_running, clock
        self.state = load_state()
        self.thread: threading.Thread | None = None
        self.told_signin = False
        self.status = ""
        self.night = None                          # night.Night: when uploads may run, keeps the PC awake

    def _set(self, bid: str, **kw) -> dict:
        """Update a block's state, keeping what the player added (EEC links) and what was already asked."""
        keep = {k: v for k, v in self.state.get(bid, {}).items() if k in ("eec", "asked")}
        self.state[bid] = {**keep, **kw}
        return self.state[bid]

    # ---------------------------------------------------------- EEC lobby links
    def set_links(self, key: str, links: list[str]) -> list[str]:
        """The EEC lobby link(s) of the block this recording belongs to (shown in the video description)."""
        good = [normalize_eec(l) for l in links]
        good = [l for l in dict.fromkeys(good) if l]
        for keys in blocks(self.rec.idx, self.s.yt_gap_min):
            if key in keys:
                st = self.state.setdefault(keys[0], {"keys": keys})
                if st.get("status") in ("uploading", "done"):
                    raise RuntimeError("This block is already on YouTube; the link can't be added any more")
                st["eec"] = good
                save_state(self.state)
                return good
        raise RuntimeError("Recording not found")

    def links_of(self, key: str) -> list[str]:
        for keys in blocks(self.rec.idx, self.s.yt_gap_min):
            if key in keys:
                return self.state.get(keys[0], {}).get("eec", [])
        return []

    # blocks still waiting for YouTube: their recordings must not be cleaned up yet
    def protected(self) -> set[str]:
        return {k for b in self.state.values() if b.get("status") in ("waiting", "uploading", "waiting-retry", "requested")
                for k in b.get("keys", [])}

    def ready_blocks(self) -> list[list[str]]:
        idx = self.rec.idx
        out = []
        all_blocks = blocks(idx, self.s.yt_gap_min)
        for i, keys in enumerate(all_blocks):
            bid = keys[0]
            st = self.state.get(bid, {})
            requested = st.get("status") == "requested"
            if st.get("status") in ("done", "skipped", "failed", "waiting", "uploading") and st.get("keys") == keys:
                continue
            if st.get("next", 0) > self.clock():
                continue                                   # failed recently: try again later
            if not requested and idx[keys[0]].started < self.s.yt_since:
                continue                                   # recorded before YouTube upload was turned on
            if self.rec.current is not None and i == len(all_blocks) - 1:
                continue                                   # the block is still being played
            if self.clock() - end_of(idx[keys[-1]]) < self.s.yt_gap_min * 60:
                continue                                   # might still continue
            ok_states = DONE_STATES + (("skipped",) if requested else ())
            if any(idx[k].status not in ok_states for k in keys):
                if all(idx[k].status in ("skipped", "failed") for k in keys):
                    self._set(bid, keys=keys, status="skipped", why="not scanned")
                continue                                   # wait for the scans (they give the chapters)
            if self.s.yt_wait_eec and not st.get("eec") and not requested:
                waited = self.clock() - end_of(idx[keys[-1]])
                if waited < self.s.yt_wait_eec_h * 3600:
                    if not st.get("asked"):
                        self._set(bid, keys=keys, status=st.get("status"), asked=True)
                        save_state(self.state)
                        self.ui.notify("EEC lobby link for YouTube", "Paste the EEC lobby link of your last scrim block "
                                       "(Recordings → EEC link). It goes in the video description; the upload waits "
                                       f"for it up to {self.s.yt_wait_eec_h} h.", sticky=True, action="eeclink")
                    continue                               # waiting for the link (at most yt_wait_eec_h)
            if not requested and self.s.yt_scrims_only and not any(g.get("mode") == "scrim" for k in keys
                                                 for g in scan_of(idx[k]).get("games", [])):
                self._set(bid, keys=keys, status="skipped", why="no scrim game")
                save_state(self.state)
                continue
            out.append(keys)
        return out

    def tick(self) -> None:
        if not self.s.yt_enabled or (self.thread and self.thread.is_alive()) or self.is_apex_running():
            return
        if self.night and not self.night.upload_allowed():
            return
        ready = self.ready_blocks()
        if not ready:
            return
        if self.service() is None:
            if not self.told_signin:
                self.told_signin = True
                self.ui.notify("YouTube upload waiting", "Sign in to the team YouTube channel once (Settings → "
                               "YouTube) and your scrim blocks are uploaded by themselves.", sticky=True, action="ytsignin")
            return
        self.told_signin = False
        keys = ready[0]
        self._set(keys[0], keys=keys, status="waiting")
        save_state(self.state)
        self.thread = threading.Thread(target=self._upload_block, args=(keys,), daemon=True, name="youtube")
        self.thread.start()

    def _upload_block(self, keys: list[str]) -> None:
        bid = keys[0]
        st = self.state[bid]
        idx = self.rec.idx
        out = Path(self.s.folder_path) / f"youtube-{bid}.mp4"
        if self.night:
            self.night.uploading(True)
        try:
            paths = [idx[k].path for k in keys]
            tracks = [events_of(idx[k]).get("tracks") or ["game"] for k in keys]
            audio = self.s.yt_audio if self.s.comms_consent else "game"
            self.status = "Preparing the YouTube video"
            offsets, t = [], 0.0
            for p in paths:
                offsets.append(t)
                t += duration(p)
            title, desc, n_games = describe(self.s, idx, keys, offsets, links=st.get("eec", []), audio=audio)
            build_video(paths, tracks, audio, out)
            st.update(status="uploading", title=title)
            save_state(self.state)
            vid = self._send(out, title, desc)
            st.update(status="done", video=vid, url=f"https://youtu.be/{vid}", at=self.clock())
            save_state(self.state)
            self.ui.notify("Scrim block on YouTube", f"{title}\nhttps://youtu.be/{vid}")
        except Exception as e:
            log.exception("youtube upload failed")
            st.update(status="failed", error=str(e)[:300], tries=st.get("tries", 0) + 1)
            save_state(self.state)
            if st["tries"] >= 3:
                self.ui.notify("YouTube upload failed", f"{e}. The recordings are kept until their usual clean-up; "
                               "press Upload in the app to try again.", sticky=True)
            else:
                st.update(status="waiting-retry", next=self.clock() + 600 * st["tries"])
            save_state(self.state)
        finally:
            out.unlink(missing_ok=True)
            self.status = ""
            if self.night:
                self.night.uploading(False)

    def _send(self, path: Path, title: str, desc: str) -> str:
        from googleapiclient.http import MediaFileUpload
        yt = self.service()
        if yt is None:
            raise ConnectionError("not signed in to YouTube")
        body = {"snippet": {"title": title, "description": desc, "categoryId": "20",           # 20 = Gaming
                            "tags": tags_for(self.s, desc)},
                "status": {"privacyStatus": self.s.yt_privacy, "selfDeclaredMadeForKids": False}}
        media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=16 * 1024 * 1024, resumable=True)
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = None
        while resp is None:
            while self.is_apex_running():                     # never share the connection with a game
                self.status = "YouTube upload paused while Apex runs"
                time.sleep(10)
            status, resp = req.next_chunk(num_retries=5)
            if status:
                self.status = f"Uploading to YouTube: {int(status.progress() * 100)}%"
        return resp["id"]

    def request(self, key: str) -> list[str] | None:
        """Upload the block this recording belongs to (by hand: also older or non-scrim blocks)."""
        for keys in blocks(self.rec.idx, self.s.yt_gap_min):
            if key in keys:
                if self.state.get(keys[0], {}).get("status") in ("waiting", "uploading"):
                    return keys
                self._set(keys[0], keys=keys, status="requested")
                save_state(self.state)
                return keys
        return None

    def block_of(self, key: str) -> dict | None:
        for bid, b in self.state.items():
            if key in b.get("keys", []):
                return {"id": bid, **b}
        return None


def _quiet() -> int:
    import os
    return 0x08000000 | 0x00004000 if os.name == "nt" else 0          # no window, below-normal priority
