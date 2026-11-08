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

from . import eec, labels, retention
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


def is_scrim(g: dict) -> bool:
    """A scrim game for sure: the scrim drop screen was read and the scan said scrim. Older scans called every
    game without a ranked badge 'scrim' (a whole ranked session once went to YouTube that way)."""
    return g.get("mode") == "scrim" and bool(g.get("drop"))


def block_label(idx, keys: list[str]) -> tuple[str | None, bool]:
    """(scrims / ranked / pubs / wildcard / None, set by hand?) for a block of recordings."""
    return labels.of_block([idx[k].path for k in keys], [scan_of(idx[k]) for k in keys])


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


def pl(n: int, word: str) -> str:
    """'1 kill', '3 kills', '1 pt', '5 pts'."""
    plural = {"pt": "pts", "point": "points", "kill": "kills", "game": "games"}[word]
    return f"{n} {word if n == 1 else plural}"


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


def tags_for(s: Settings, desc: str, kind: str | None = "scrims") -> list[str]:
    tags = ["Apex Legends", kind or "", "EEC" if kind == "scrims" else "", s.yt_team or "PCS", s.player] + [m for m in MAP_NAMES.values() if m in desc]
    return [t for t in dict.fromkeys(tags) if t][:15]


def describe(s: Settings, idx, keys: list[str], offsets: list[float], links: list[str] | None = None,
             audio: str = "game", results: dict | None = None, kind: str | None = None) -> tuple[str, str, int]:
    """Title, description with chapters, and the number of games."""
    games, marks = [], []
    for k, off in zip(keys, offsets):
        for g in scan_of(idx[k]).get("games", []):
            if g.get("tStart") is not None:
                games.append((off + g["tStart"], g.get("map"), "scrim" if is_scrim(g) else ("ranked" if g.get("mode") == "ranked" else "unknown"), our_drop(g),
                              idx[k].started + g["tStart"]))
        for m in events_of(idx[k]).get("events", []):
            if m.get("kind") == "mark":
                marks.append(off + m["t"])
    games.sort(key=lambda x: x[0])
    start = dt.datetime.fromtimestamp(idx[keys[0]].started)
    team = s.yt_team or "PCS"
    player = s.player or "Player"
    maps = ", ".join(dict.fromkeys(MAP_NAMES.get(g[1], g[1] or "?") for g in games)) or "no game found"
    scrim = any(g[2] == "scrim" for g in games)
    kind = kind or ("scrims" if scrim else None)            # the label set in the app wins over the scan
    scrim = kind == "scrims"
    word = labels.TITLE.get(kind, "Session")
    standing = f" · {eec.ordinal(results['standing'])}/{results['teams']}, {pl(results['pts'], 'pt')}" if results else ""
    title = (f"{word} {start:%d.%m.%Y %H:%M} · {team} · {player} POV{standing} · "
             f"{len(games)} game{'s' if len(games) != 1 else ''} ({maps})")[:100]

    per_game = {}
    if results:
        for i, g in enumerate(games):
            r = eec.match_game(results, g[4], i)
            if r:
                per_game[i + 1] = r

    def label(i, g):
        r = per_game.get(i)
        return (f"Game {i} · {MAP_NAMES.get(g[1], g[1] or '?')}" + (f" · {g[3]}" if g[3] else "") +
                (f" · {eec.ordinal(r['place'])}, {pl(r['pts'], 'pt')}" if r else ""))
    # YouTube chapters: start at 0:00, ascending, at least 10 s apart, at least 3 of them
    chapters = [(0.0, "Lobby")]
    for i, g in enumerate(games, 1):
        if g[0] - chapters[-1][0] >= 10:
            chapters.append((g[0], label(i, g)))
        elif len(chapters) == 1:
            chapters[0] = (0.0, label(i, g))               # the recording starts right at the game
    lines = [f"{team} · {player} POV · {'EEC scrims' if scrim else (word + ' session' if kind else 'Apex session')} · {start:%A %d %B %Y, %H:%M}"]
    if results:
        n = len(results.get("games", []))
        lines.append(f"Result: {eec.ordinal(results['standing'])}{' (tied)' if results.get('tied') else ''} of "
                     f"{results['teams']} · {pl(results['pts'], 'point')} · "
                     f"{pl(results['kills'], 'kill')} · {pl(n, 'game')} (ALGS scoring)")
        if results.get("standing", 1) > 1 and results.get("leader"):
            lines.append(f"Lobby winner: {results['leader']['team']}, {pl(results['leader']['pts'], 'point')}")
    if links:
        lines += [("EEC lobby: " if len(links) == 1 else "EEC lobbies: ") + "  ".join(links)]
    lines.append("")
    if len(chapters) >= 3:
        lines += [f"{fmt(t)} {name}" for t, name in chapters] + [""]
    elif games:
        lines += [f"{fmt(g[0])} {label(i, g)}" for i, g in enumerate(games, 1)] + [""]
    if results and results.get("games"):
        lines += ["Games: " + " · ".join(f"G{r['game']} {eec.ordinal(r['place'])} ({pl(r['pts'], 'pt')}, {pl(r['kills'], 'kill')})"
                                         for r in results["games"]), ""]
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
        # an upload cut by quitting the app: judge that block again (it isn't on YouTube; YouTube drops
        # unfinished uploads), instead of leaving it "uploading" forever
        for st in self.state.values():
            if st.get("status") in ("waiting", "uploading"):
                st["status"] = "requested" if st.get("requested") else None
        self.thread: threading.Thread | None = None
        self.told_signin = False
        self.status = ""
        self.night = None                          # night.Night: when uploads may run, keeps the PC awake

    def _set(self, bid: str, **kw) -> dict:
        """Update a block's state, keeping what the player added (EEC links) and what was already asked."""
        keep = {k: v for k, v in self.state.get(bid, {}).items() if k in ("eec", "eec_res", "asked", "hub_tried", "custom_title", "privacy")}
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

    def add_results(self, lobby: dict) -> dict:
        """EEC lobby results (export file or hub): attached to the block played at that time."""
        res = eec.results(lobby, self.s.yt_team)
        if not res:
            raise RuntimeError(f"Team \"{self.s.yt_team}\" isn't in this lobby (check the team name in Settings → YouTube)")
        starts = [g["start"] for g in lobby["games"] if g.get("start")]
        idx = self.rec.idx
        for keys in blocks(idx, self.s.yt_gap_min):
            b0, b1 = idx[keys[0]].started, end_of(idx[keys[-1]])
            if not starts or b0 - 1800 <= min(starts) <= b1 + 600:
                st = self.state.setdefault(keys[0], {"keys": keys})
                if st.get("status") in ("uploading", "done"):
                    raise RuntimeError("That block is already on YouTube")
                st["eec_res"] = res
                if res.get("url") and res["url"] not in st.get("eec", []):
                    st["eec"] = st.get("eec", []) + [res["url"]]
                save_state(self.state)
                return {"block": keys, **res}
        raise RuntimeError("None of your recordings was made while this lobby was played")

    def results_of(self, key: str) -> dict | None:
        for keys in blocks(self.rec.idx, self.s.yt_gap_min):
            if key in keys:
                return self.state.get(keys[0], {}).get("eec_res")
        return None

    def _try_hub(self, keys: list[str]) -> None:
        """The coach may already have imported the lobby on the team hub: take the results from there."""
        st = self.state.get(keys[0], {})
        if not self.s.hub_url or st.get("eec_res") or self.clock() - st.get("hub_tried", 0) < 600:
            return
        self.state.setdefault(keys[0], {"keys": keys})["hub_tried"] = self.clock()
        try:
            res = eec.from_hub(self.s.hub_url, self.s.yt_team, self.rec.idx[keys[0]].started,
                               end_of(self.rec.idx[keys[-1]]))
        except Exception as e:
            log.info("hub results not available: %s", e)
            return
        if res:
            st = self.state[keys[0]]
            st["eec_res"] = res
            if res.get("url") and res["url"] not in st.get("eec", []):
                st["eec"] = st.get("eec", []) + [res["url"]]
        save_state(self.state)

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
            kind, by_hand = block_label(idx, keys)
            if not requested and self.s.yt_scrims_only and by_hand and kind != "scrims":
                self._set(bid, keys=keys, status="skipped", why=f"labelled {kind}")
                save_state(self.state)
                continue                                   # labelled by hand: no need to ask for EEC results
            self._try_hub(keys)
            st = self.state.get(bid, {})
            if self.s.yt_wait_eec and not (st.get("eec") or st.get("eec_res")) and not requested:
                waited = self.clock() - end_of(idx[keys[-1]])
                if waited < self.s.yt_wait_eec_h * 3600:
                    if not st.get("asked"):
                        self._set(bid, keys=keys, status=st.get("status"), asked=True)
                        save_state(self.state)
                        self.ui.notify("EEC lobby link for YouTube", "Add your last scrim block's EEC results: drop the "
                                       "lobby's export file (Download button on the EEC lobby page) on the app, or "
                                       "paste the lobby link (Recordings → EEC). The upload waits for it up to "
                                       f"{self.s.yt_wait_eec_h} h.", sticky=True, action="eeclink")
                    continue                               # waiting for the link (at most yt_wait_eec_h)
            if not requested and self.s.yt_scrims_only and kind != "scrims" and not st.get("eec_res"):
                self._set(bid, keys=keys, status="skipped", why="no scrim game")
                save_state(self.state)
                continue
            out.append(keys)
        # blocks asked for by hand go first
        out.sort(key=lambda ks: self.state.get(ks[0], {}).get("status") != "requested")
        return out

    def tick(self) -> None:
        if not self.s.yt_enabled or (self.thread and self.thread.is_alive()) or self.is_apex_running():
            return
        ready = self.ready_blocks()
        if not ready:
            return
        if self.night and not self.night.upload_allowed():
            # "Upload now" in the app doesn't wait for the night
            ready = [ks for ks in ready if self.state.get(ks[0], {}).get("status") == "requested"]
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
            kind, _ = block_label(idx, keys)
            title, desc, n_games = describe(self.s, idx, keys, offsets, links=st.get("eec", []), audio=audio,
                                            results=st.get("eec_res"), kind=kind)
            title = (st.get("custom_title") or title)[:100]
            build_video(paths, tracks, audio, out)
            st.update(status="uploading", title=title)
            save_state(self.state)
            vid = self._send(out, title, desc, privacy=st.get("privacy"), kind=kind)
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

    def _send(self, path: Path, title: str, desc: str, privacy: str | None = None, kind: str | None = "scrims") -> str:
        from googleapiclient.http import MediaFileUpload
        yt = self.service()
        if yt is None:
            raise ConnectionError("not signed in to YouTube")
        body = {"snippet": {"title": title, "description": desc, "categoryId": "20",           # 20 = Gaming
                            "tags": tags_for(self.s, desc, kind)},
                "status": {"privacyStatus": privacy or self.s.yt_privacy, "selfDeclaredMadeForKids": False}}
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

    # ---------------------------------------------------------- the queue in the app
    def _block(self, bid: str) -> list[str]:
        for keys in blocks(self.rec.idx, self.s.yt_gap_min):
            if keys[0] == bid:
                return keys
        raise RuntimeError("This block isn't in your recordings any more")

    def _pending_why(self, i: int, n: int, keys: list[str], st: dict) -> str:
        """Why a block that hasn't been decided yet isn't uploading (no side effects, unlike ready_blocks)."""
        idx = self.rec.idx
        if not self.s.yt_enabled:
            return "YouTube upload is off (Settings → YouTube)"
        if st.get("status") != "requested" and idx[keys[0]].started < self.s.yt_since:
            return "recorded before YouTube upload was turned on"
        if self.rec.current is not None and i == n - 1:
            return "the block is still being played"
        left = self.s.yt_gap_min * 60 - (self.clock() - end_of(idx[keys[-1]]))
        if left > 0:
            return f"waiting {int(left // 60) + 1} min to be sure the block is over"
        if any(idx[k].status not in DONE_STATES + ("skipped",) for k in keys):
            return "waiting for the scans"
        if st.get("status") != "requested" and self.s.yt_scrims_only:
            kind, by_hand = block_label(idx, keys)
            if by_hand and kind != "scrims":
                return f"won't upload: labelled {labels.TITLE[kind]} (Scrims only is on)"
        if st.get("status") == "requested":
            if self.is_apex_running():
                return "uploads as soon as Apex is closed"
            return "uploads in a moment"
        if self.s.yt_wait_eec and not (st.get("eec") or st.get("eec_res")):
            return f"waiting for the EEC results (up to {self.s.yt_wait_eec_h} h)"
        if self.night and not self.night.upload_allowed():
            return f"waiting for the night ({self.s.night_from}:00 to {self.s.night_to}:00, PC idle)"
        if self.is_apex_running():
            return "waiting for Apex to close"
        return "ready"

    def queue(self, limit: int = 40) -> list[dict]:
        """Every block, newest first, with what happens to it: on YouTube, uploading, waiting (and why), skipped."""
        idx = self.rec.idx
        all_blocks = blocks(idx, self.s.yt_gap_min)
        out = []
        for i, keys in enumerate(all_blocks):
            bid = keys[0]
            st = self.state.get(bid, {})
            status = st.get("status")
            if status in ("skipped", "failed") and st.get("keys") != keys:
                status = None                              # the block grew since: judged again
            games = [g for k in keys for g in scan_of(idx[k]).get("games", [])]
            kind, by_hand = block_label(idx, keys)
            item = {"id": bid, "keys": keys, "start": idx[keys[0]].started, "end": end_of(idx[keys[-1]]),
                    "recordings": len(keys), "games": len(games),
                    "maps": list(dict.fromkeys(MAP_NAMES.get(g.get("map"), g.get("map") or "?") for g in games)),
                    "label": kind, "label_by_hand": by_hand, "status": status or "pending",
                    "url": st.get("url"), "title": st.get("title"), "custom_title": st.get("custom_title", ""),
                    "privacy": st.get("privacy", ""), "eec": bool(st.get("eec") or st.get("eec_res")),
                    "res": {k: st["eec_res"].get(k) for k in ("standing", "teams", "pts")} if st.get("eec_res") else None,
                    "why": "", "progress": ""}
            if item["status"] in ("waiting", "uploading"):
                item["progress"] = self.status
            elif item["status"] == "skipped":
                item["why"] = "skipped by you" if st.get("by_hand") else f"skipped: {st.get('why') or '?'}"
            elif item["status"] in ("failed", "waiting-retry"):
                item["why"] = f"failed: {st.get('error') or '?'}" + (" (trying again soon)" if item["status"] == "waiting-retry" else "")
            elif item["status"] in ("pending", "requested"):
                item["why"] = self._pending_why(i, len(all_blocks), keys, st)
            out.append(item)
        return out[::-1][:limit]

    def act(self, bid: str, action: str, title: str | None = None, privacy: str | None = None) -> dict:
        """Upload now / skip / put back / edit title and privacy, from the queue page."""
        keys = self._block(bid)
        st = self.state.get(bid, {})
        if st.get("status") in ("waiting", "uploading"):
            raise RuntimeError("This block is uploading right now")
        if action == "upload":
            if st.get("status") == "done":
                raise RuntimeError("This block is already on YouTube")
            self._set(bid, keys=keys, status="requested")
        elif action == "skip":
            if st.get("status") == "done":
                raise RuntimeError("This block is already on YouTube (remove it in YouTube Studio)")
            self._set(bid, keys=keys, status="skipped", why="by you", by_hand=True)
        elif action == "unskip":                       # back to the automatic rules
            self._set(bid, keys=keys, status=None)
        elif action == "edit":
            if st.get("status") == "done":
                raise RuntimeError("This block is already on YouTube: edit it in YouTube Studio")
            if privacy and privacy not in ("private", "unlisted", "public"):
                raise RuntimeError("Privacy must be private, unlisted or public")
            cur = self.state.setdefault(bid, {"keys": keys})
            cur["custom_title"] = (title or "").strip()[:100]
            cur["privacy"] = privacy or ""
        else:
            raise RuntimeError(f"Unknown action: {action}")
        save_state(self.state)
        return {"id": bid, **self.state.get(bid, {})}

    def relabelled(self, key: str) -> None:
        """A recording's label changed: its block is judged again if it had been skipped automatically."""
        for keys in blocks(self.rec.idx, self.s.yt_gap_min):
            if key in keys:
                st = self.state.get(keys[0], {})
                if st.get("status") == "skipped" and not st.get("by_hand"):
                    self._set(keys[0], keys=keys, status=None)
                    save_state(self.state)
                return


def _quiet() -> int:
    import os
    return 0x08000000 | 0x00004000 if os.name == "nt" else 0          # no window, below-normal priority
