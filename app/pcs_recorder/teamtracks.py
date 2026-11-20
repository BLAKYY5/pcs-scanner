"""Teammates' mic tracks, fetched by themselves (coach's decision, 18 Nov 2026; team hub `GET /api/team-tracks`).

A player who agreed in the setup step (or in Settings: `share_tracks`, with the team's comms consent and "Send my
mic track" on) shares their own mic track with their team, and the app downloads their teammates' mic tracks of
the same games, so the review player can play them next to the player's own sound.

- Which recordings: the app's own recordings whose results went to the hub (status "uploaded"), still on disk,
  made in the last MAX_AGE_DAYS. Each one is asked about again every RECHECK_S for RECHECK_DAYS (a teammate may send
  their results later), then left alone.
- Which tracks: those the hub lists for the recording (same team, same game, the teammate shares their track).
  A teammate who did not agree is skipped (the hub doesn't list them).
- When: low priority, one file at a time, only while Apex is closed and nothing records, scans or records review
  talk (`busy()`); Apex starting meanwhile stops the download (the partial file is removed, tried again later).
- Where: only the app's own folder, `team-tracks/<recording>/<player>-<scan id>.mic.m4a`. Written as `.part`, renamed
  when complete. A recording's folder is removed with the recording (retention, delete); nothing else is touched.
- The player's answer goes to the hub (`PUT /api/me/sharing`), so teammates' apps skip a player who said no and the
  hub refuses their mic track to teammates. Never answered = no (coach, 21 Nov 2026, like transcripts and clips): the
  hub is told no as well (an older hub let teammates download the track of a player who never answered).
- Clips are a separate yes (coach, 20 Nov 2026): sharing the mic with the team does not put the player's voice in a
  teammate's clip (Discord). "Allow my voice in teammates' clips" (`share_clips`, off by default) goes to the hub
  with the sharing answer (`share_clips`); the hub lists it per track (`share_clips` in `GET /api/team-tracks`), and a
  clip takes a teammate's mic only when that is true (missing = an older hub = left out).
- Transcripts withdrawn (decision 21, 20 Nov 2026): a player who switches comms transcripts from yes to no has the
  hub told (`PUT /api/me/transcripts {"transcripts": false}`, his own key), and the hub deletes his transcripts. Only
  when `GET /api/me` lists the feature "transcripts" (an older hub gets nothing; asked again every OLD_HUB_RETRY_S);
  kept as `transcripts_withdraw` in the settings and tried again at the next tick until the hub took it.
- State in `team-tracks.json` (no key, no personal data beyond teammates' names as the hub gives them).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import quote

from . import hub
from .settings import APP_DIR

log = logging.getLogger("teamtracks")
DIR_NAME = "team-tracks"
STATE_NAME = "team-tracks.json"
MAX_AGE_DAYS = 7
RECHECK_S = 3600
RECHECK_DAYS = 3
CHUNK = 1 << 18
BUSY_EVERY_S = 3
SPARE = 2 << 30                      # leave at least 2 GB free on the drive
TRACK_RE = re.compile(r"[A-Za-z0-9_-]{1,40}-\d{1,10}\.mic\.m4a")
OLD_HUB_RETRY_S = 6 * 3600           # a hub without the "transcripts" feature: asked again this much later


def base_dir() -> Path:
    return Path(APP_DIR) / DIR_NAME


def state_file() -> Path:
    return Path(APP_DIR) / STATE_NAME


def safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(text or ""))[:40] or "player"


def rec_dir_name(stem: str) -> str:
    """A folder name for a recording (its file name without extension, made safe)."""
    return re.sub(r"[^A-Za-z0-9 ._()-]", "_", stem).strip(" .")[:150] or "recording"


def sharing_answer(s) -> bool:
    """What this player tells the team: True = shares (and so gets theirs), False = said no or never answered (coach,
    21 Nov 2026: no answer counts as no, like transcripts and clips)."""
    return bool(s.comms_consent and s.send_mic and s.mic_audio and s.share_tracks)


def active(s) -> bool:
    return sharing_answer(s) is True


def clips_answer(s) -> bool:
    """The player's voice may go into teammates' clips: their own separate yes, and only while they share the track."""
    return active(s) and bool(getattr(s, "share_clips", False))


def _background(on: bool) -> None:
    from .hubbackup import _background as bg
    bg(on)


class Busy(Exception):
    """Apex started, or the PC got busy: stop quietly and try again later."""


class TeamTracks:
    """`settings()` gives the recorder settings; `recordings()` the app's own recordings as {key: Entry};
    `busy()` a reason not to run now, or None."""

    def __init__(self, settings, recordings, busy, now=time.time):
        self.settings, self.recordings, self.busy, self.now = settings, recordings, busy, now
        self.lock = threading.Lock()
        self.running = False
        self.next_try = 0.0
        self.synced: tuple | None = None                 # (hub address, key fingerprint, answer) the hub took
        self.withdraw_next = 0.0                         # older hub: when to look at its features again
        self.st = self._load()

    # ---------------------------------------------------------------- state
    def _load(self) -> dict:
        try:
            d = json.loads(state_file().read_text(encoding="utf-8"))
            return d if isinstance(d, dict) and isinstance(d.get("recs"), dict) else {"recs": {}}
        except (OSError, ValueError):
            return {"recs": {}}

    def _save(self) -> None:
        try:
            Path(APP_DIR).mkdir(parents=True, exist_ok=True)
            tmp = state_file().with_suffix(".part")
            tmp.write_text(json.dumps(self.st, indent=1), encoding="utf-8")
            os.replace(tmp, state_file())
        except OSError as e:
            log.warning("team tracks state not saved: %s", e)

    # ---------------------------------------------------------------- the player's answer to the hub
    def sync_sharing(self, force: bool = False) -> bool | None:
        """Tell the hub the player's answer once per hub, key and answer. Returns what the hub took, or None."""
        s = self.settings()
        ans = sharing_answer(s)
        key = hub.get_key()
        if ans is None or not s.hub_url or not key:
            return None
        clips = clips_answer(s)
        who = (s.hub_url, hub._fp(key), ans, clips)
        if self.synced == who and not force:
            return ans
        try:
            r = hub.request(s.hub_url, "PUT", "/api/me/sharing", json={"share_tracks": ans, "share_clips": clips},
                            timeout=8)
        except hub.HubError as e:
            log.info("sharing answer not sent: %s", e)
            return None
        if r.status_code == 404 or r.status_code == 405:
            log.info("the hub doesn't know mic track sharing yet (older hub)")
            self.synced = who                            # asked again after a restart or a change
            return None
        if r.status_code >= 400:
            log.info("sharing answer refused: %s", r.status_code)
            return None
        self.synced = who
        return ans

    def sync_transcripts(self) -> bool | None:
        """A transcripts "no" after a "yes" (`transcripts_withdraw`): tell the hub, which deletes the player's
        transcripts. True = the hub took it (the flag is cleared), None = not now (no hub / key, older hub, no answer:
        tried again later). Said yes again meanwhile = nothing to send."""
        from . import settings as st
        s = self.settings()
        if not getattr(s, "transcripts_withdraw", False):
            return None
        if s.transcript_consent:
            s.transcripts_withdraw = False
            st.save(s)
            return None
        if not s.hub_url or not hub.get_key() or self.now() < self.withdraw_next:
            return None
        try:
            r = hub.request(s.hub_url, "GET", "/api/me", timeout=8)
            if r.status_code >= 400:
                return None
            if "transcripts" not in ((r.json() or {}).get("features") or []):
                self.withdraw_next = self.now() + OLD_HUB_RETRY_S
                log.info("the hub doesn't take transcripts answers yet (older hub)")
                return None
            r = hub.request(s.hub_url, "PUT", "/api/me/transcripts", json={"transcripts": False}, timeout=8)
        except (hub.HubError, ValueError, AttributeError) as e:
            log.info("transcripts answer not sent: %s", e)
            return None
        if r.status_code >= 400:
            log.info("transcripts answer refused: %s", r.status_code)
            return None
        s.transcripts_withdraw = False
        st.save(s)
        log.info("the hub was told: no transcripts (it deletes them)")
        return True

    # ---------------------------------------------------------------- when
    def tick(self) -> bool:
        """Called every minute by the app. -> a download round started."""
        try:
            s = self.settings()
            if not s.hub_url or not hub.get_key() or self.busy():
                return False                             # never during a game: not even a request
            self.sync_sharing()
            self.sync_transcripts()
            self.cleanup()
            if not active(s) or self.running or self.now() < self.next_try or not self.todo():
                return False
        except Exception:
            log.exception("team tracks check")
            return False
        with self.lock:
            if self.running:
                return False
            self.running = True
        threading.Thread(target=self._run, daemon=True, name="team-tracks").start()
        return True

    def todo(self) -> list[tuple[str, str]]:
        """(key, recording path) to ask the hub about now, newest first."""
        now = self.now()
        out = []
        for k, e in (self.recordings() or {}).items():
            if e.status != "uploaded" or not e.path or not os.path.isfile(e.path):
                continue
            if now - float(e.started or 0) > MAX_AGE_DAYS * 86400:
                continue
            r = self.st["recs"].get(Path(e.path).stem) or {}
            if r.get("checked") and (now - r["checked"] < RECHECK_S or now - float(e.started or 0) > RECHECK_DAYS * 86400):
                continue
            out.append((float(e.started or 0), k, e.path))
        return [(k, p) for _, k, p in sorted(out, reverse=True)]

    def _run(self) -> None:
        _background(True)
        try:
            for _, path in self.todo():
                self._check_busy()
                self.fetch_recording(path)
        except Busy as e:
            log.info("team tracks paused: %s", e)
            self.next_try = self.now() + 300
        except hub.HubError as e:
            log.info("team tracks: %s", e)
            self.next_try = self.now() + 900
        except Exception:
            log.exception("team tracks")
            self.next_try = self.now() + 1800
        finally:
            _background(False)
            with self.lock:
                self.running = False

    def _check_busy(self) -> None:
        b = self.busy()
        if b:
            raise Busy(b)

    # ---------------------------------------------------------------- one recording
    def fetch_recording(self, path: str) -> dict:
        """Ask the hub for the teammates' tracks of one recording and download those not here yet."""
        s = self.settings()
        stem = Path(path).stem
        r = hub.request(s.hub_url, "GET", "/api/team-tracks?recording=" + quote(stem), timeout=15)
        if r.status_code == 404:                         # an older hub without the route
            self.next_try = self.now() + 6 * 3600
            return {}
        hub._check(r)
        ans = r.json()
        rec = self.st["recs"].setdefault(stem, {"tracks": {}})
        folder = base_dir() / rec_dir_name(stem)
        for g in ans.get("games") or []:
            for t in g.get("tracks") or []:
                sid = str(int(t["scan_id"]))
                name = f"{safe(t.get('player'))}-{sid}.mic.m4a"
                # one file per teammate recording; one part per game it covers (its own time offset)
                part = {"gameKey": g.get("gameKey"), "ownJumpAt": float(g["jumpAt"]), "jumpAt": float(t["jumpAt"]),
                        "start": g.get("start"), "end": g.get("end")}
                entry = rec["tracks"].get(sid) or {}
                parts = [p for p in entry.get("parts") or [] if p.get("gameKey") != part["gameKey"]] + [part]
                entry = {"player": str(t.get("player") or "")[:40], "file": name,
                         "parts": sorted(parts, key=lambda p: p["ownJumpAt"])}
                if not (folder / name).is_file():
                    self._check_busy()
                    self._download(s.hub_url, sid, int(t.get("size") or 0), folder, name)
                    log.info("teammate's mic track downloaded for %s (%s)", stem, entry["player"])
                rec["tracks"][sid] = entry
        rec["checked"] = self.now()
        self._save()
        return rec

    def _download(self, url: str, sid: str, size: int, folder: Path, name: str) -> None:
        import requests
        folder.mkdir(parents=True, exist_ok=True)
        try:
            free = shutil.disk_usage(folder).free
        except OSError:
            free = None
        if free is not None and free < size + SPARE:
            raise Busy("not enough free space")
        part = folder / (name + ".part")
        r = hub.request(url, "GET", f"/api/scans/{sid}/file", timeout=(15, 120), stream=True)
        try:
            hub._check(r)
            got, looked = 0, time.monotonic()
            with open(part, "wb") as f:
                try:
                    for chunk in r.iter_content(CHUNK):
                        f.write(chunk)
                        got += len(chunk)
                        if time.monotonic() - looked >= BUSY_EVERY_S:
                            looked = time.monotonic()
                            self._check_busy()
                except requests.RequestException:
                    raise hub.HubError("the download broke off", "network") from None
            if size and got != size:
                raise hub.HubError("the download is incomplete", "network")
            os.replace(part, folder / name)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        finally:
            r.close()

    # ---------------------------------------------------------------- what the review player gets
    def for_recording(self, path: str) -> list[dict]:
        """Downloaded teammates' tracks of a recording: {id, player, parts: [{start, end, off}]}. Inside a part
        (this recording's time start..end, end None = to its end) the teammate's time = this recording's time + off
        (off = their jump - own jump)."""
        stem = Path(path).stem
        rec = self.st["recs"].get(stem) or {}
        folder = base_dir() / rec_dir_name(stem)
        out = []
        for sid, t in sorted((rec.get("tracks") or {}).items(), key=lambda x: (x[1].get("player") or "", x[0])):
            if not (folder / t["file"]).is_file():
                continue
            out.append({"id": sid, "player": t.get("player") or "",
                        "parts": [{"start": p.get("start"), "end": p.get("end"), "off": round(p["jumpAt"] - p["ownJumpAt"], 3)}
                                  for p in t.get("parts") or []]})
        return out

    def file(self, path: str, sid: str) -> Path | None:
        stem = Path(path).stem
        t = ((self.st["recs"].get(stem) or {}).get("tracks") or {}).get(str(sid))
        if not t or not TRACK_RE.fullmatch(t["file"]):
            return None
        f = base_dir() / rec_dir_name(stem) / t["file"]
        return f if f.is_file() else None

    # ---------------------------------------------------------------- clean-up (only what this module made)
    def cleanup(self) -> list[str]:
        """Remove the teammates' tracks of recordings the app no longer has (retention, delete), and leftover .part
        files. Only files named like this module names them, in its own folder."""
        keep = {rec_dir_name(Path(e.path).stem) for e in (self.recordings() or {}).values() if e.path}
        removed = []
        root = base_dir()
        if not root.is_dir():
            keep_recs = {k: v for k, v in self.st["recs"].items() if rec_dir_name(k) in keep}
            if len(keep_recs) != len(self.st["recs"]):
                self.st["recs"] = keep_recs
                self._save()
            return removed
        for d in root.iterdir():
            if not d.is_dir():
                continue
            gone = d.name not in keep
            for f in d.iterdir():
                own = f.is_file() and (TRACK_RE.fullmatch(f.name) or (f.name.endswith(".part") and TRACK_RE.fullmatch(f.name[:-5])))
                if own and (gone or (f.name.endswith(".part") and not self.running)):
                    try:
                        f.unlink()
                        removed.append(f.name)
                    except OSError:
                        pass
            if gone:
                try:
                    d.rmdir()                            # only when empty: nothing else is ever removed
                except OSError:
                    pass
        left = {k: v for k, v in self.st["recs"].items() if rec_dir_name(k) in keep}
        if len(left) != len(self.st["recs"]):
            self.st["recs"] = left
            self._save()
        return removed

    def public(self) -> dict:
        s = self.settings()
        n = sum(1 for r in self.st["recs"].values() for _ in (r.get("tracks") or {}))
        return {"answer": sharing_answer(s), "running": self.running, "tracks": n}
