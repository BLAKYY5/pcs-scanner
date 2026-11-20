"""Comms transcripts: the player's own mic track turned into text after a session (idea 1 of 19 Nov 2026).

Only for a player who said yes to transcripts (setup step "Your choices" or Settings: `transcript_consent`), with the
team's comms consent and the mic recorded. Only the player's own mic track (track 3: one voice, theirs); never the
voice-app track (every teammate's voice mixed) nor the game sound, and nobody else's track.

- When: after the recording was scanned, one recording at a time, below-normal priority, only while Apex is closed and
  nothing records, scans or records review talk (`busy()`, as the scan and the teammates' tracks). Apex starting stops
  the transcriber at once (`core.stop_tree`); finished 10-minute parts are kept (`.comms.partial.json`) and the next
  run goes on from there. Recordings of the last MAX_AGE_DAYS only; a recording that fails MAX_TRIES times is left.
- What: `<recording>.comms.json` in the recording's results folder (`<recording>.scan/`), format in
  `pcs_transcribe.py`: segments with start / end in recording seconds (the same clock as scan.json), text, speaker =
  the player's name. Never sent to Google Drive (`core.result_files` leaves it out); sent to the team hub on its own
  (POST /api/scans, like a scan file) once written, only while the player still says yes.
- How: `pcs_transcribe.py` with faster-whisper (Whisper large-v3-turbo, CPU, int8) runs in its own process. It is
  not in PCS-Scanner.exe (~200 MB more that every start and every scan would unpack): it is the optional
  "Transcripts" component (`COMPONENT`, a zip built by build.yml), downloaded once on first use after the yes, checked
  by SHA-256. The model (1.6 GB) is downloaded the same way from a pinned Hugging Face commit (`MODEL_FILES`, each
  file's SHA-256), into the app's data folder. A developer can point `PCS_TRANSCRIBE_PY` at a Python with
  faster-whisper (runs `pcs_transcribe.py` from source) and `PCS_WHISPER_MODEL` at a model folder.
- On the team server (coach's decision 20 Nov 2026, Whisper on the server's GPU): when the hub's /api/me lists
  "comms-server" and the player's mic track goes to the hub (comms consent, mic on, "Send my mic track"), nothing runs
  on this PC (`where() == "server"`): the app tells the hub the player's yes (`PUT /api/me/transcripts`, once per
  hub and key; the hub transcribes only players whose yes it holds), and downloads the transcripts the hub made of
  its own recordings into the results folder (`<rec>.comms.json`, for the review player; state "server"). Said no:
  the copies downloaded from the hub are deleted here (the hub deletes its own when told).

State in `transcripts.json` (no key, no text): per recording status, tries, note, sent.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

from .settings import APP_DIR

log = logging.getLogger("comms")

STATE_NAME = "transcripts.json"
DIR_NAME = "transcripts"
SUFFIX = ".comms.json"
PARTIAL_SUFFIX = ".comms.partial.json"
MAX_AGE_DAYS = 3
MAX_TRIES = 3
FETCH_EVERY_S = 600                   # on the team server: look for its transcripts every 10 min
FETCH_DAYS = 7                        # ... of recordings from the last 7 days
BUSY_EVERY_S = 3
SPARE = 2 << 30                       # leave at least 2 GB free on the drive

# Whisper large-v3-turbo in CTranslate2 format (MIT licence), pinned to one commit; every file checked by SHA-256.
MODEL_NAME = "large-v3-turbo"
MODEL_COMMIT = "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
MODEL_URL = "https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo/resolve/" + MODEL_COMMIT + "/"
MODEL_FILES = (
    ("config.json", 2263, "b0253ea6c0d3bea6b1e19e91a02acfd3b53f4467362efcb5a3e6b16c9b3a9b7e"),
    ("preprocessor_config.json", 340, "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711"),
    ("tokenizer.json", 2710337, "297b13372ac43916285644fb9687add3cc62ee2a1adb60da3dc25cc94c1871fd"),
    ("vocabulary.json", 1068114, "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1"),
    ("model.bin", 1617884929, "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da"),
)
MODEL_HOSTS = ("huggingface.co",)     # where the pinned address points; its downloads redirect to HF's file hosts (https)

# The "Transcripts" component (pcs-transcribe.exe and its libraries, a zip made by build.yml), published as release
# "transcripts-1" of this repo: a prerelease, never "latest" (the updater reads the latest release only), from the
# green main build of 1.11.0 (run 44559028735). A new zip = a new release tag (never v...: that tag would build an app
# release) and a new pin here; the old one stays published for the app versions that pin it.
COMPONENT: dict | None = {
    "url": "https://github.com/BLAKYY5/pcs-scanner/releases/download/transcripts-1/PCS-Transcripts.zip",
    "sha256": "dcb201bbbfa52ba4a4b2d66490b2c7b09677ab4b671b5e2d7d2ffb86c581a3ec",
    "size": 62863532,
}
COMPONENT_EXE = "pcs-transcribe.exe"


class Busy(Exception):
    """Apex started, or the PC got busy: stop quietly and go on later."""


class Unavailable(Exception):
    """The transcriber can't run on this PC yet (component not published, no space, download refused)."""


def base_dir() -> Path:
    return Path(APP_DIR) / DIR_NAME


def model_dir() -> Path:
    env = os.environ.get("PCS_WHISPER_MODEL")
    return Path(env) if env else base_dir() / f"model-{MODEL_NAME}-{MODEL_COMMIT[:8]}"


def runtime_dir() -> Path:
    return base_dir() / "runtime"


def state_file() -> Path:
    return Path(APP_DIR) / STATE_NAME


def eligible(s) -> bool:
    """The player said yes to transcripts, the team's comms consent is on and the mic is recorded."""
    return bool(s.transcript_consent and s.comms_consent and s.mic_audio)


def comms_path(rec_path: str | Path) -> Path:
    p = Path(rec_path)
    return p.with_suffix(".scan") / (p.stem + SUFFIX)


def is_comms_file(name: str) -> bool:
    n = name.lower()
    return n.endswith(SUFFIX) or n.endswith(PARTIAL_SUFFIX) or n.endswith(SUFFIX + ".tmp") or n.endswith(PARTIAL_SUFFIX + ".tmp")


def mic_stream(rec_path: str | Path) -> int | None:
    """The mic's audio stream index in the recording (from its events.json track list), or None: no mic track."""
    ev = Path(rec_path).with_suffix(".events.json")
    try:
        tracks = json.loads(ev.read_text(encoding="utf-8")).get("tracks") or []
    except (OSError, ValueError, AttributeError):
        return None
    return tracks.index("mic") if "mic" in tracks else None


def prompt_for(rec_path: str | Path) -> str:
    """The worker's word list plus the place names of the maps played in this recording (from its scan)."""
    try:
        import pcs_transcribe                            # (no heavy imports: argparse, json, re)
        base = pcs_transcribe.PROMPT
    except ImportError:
        base = ""
    p = Path(rec_path)
    try:
        scan = json.loads((p.with_suffix(".scan") / (p.stem + ".scan.json")).read_text(encoding="utf-8"))
        maps = {g.get("map") for g in scan.get("games") or [] if isinstance(g, dict)}
    except (OSError, ValueError):
        maps = set()
    names = []
    for m in sorted(x for x in maps if isinstance(x, str)):
        names += poi_names(m)
    if names:
        base = (base + " " + ", ".join(dict.fromkeys(names))[:400] + ".").strip()
    return base


def poi_names(code: str) -> list[str]:
    """POI names of one map from the scanner's pcs_maps.json (our own table)."""
    for d in (Path(getattr(sys, "_MEIPASS", "")), Path(__file__).resolve().parents[2] / "scanner"):
        f = d / "pcs_maps.json"
        if f.is_file():
            try:
                m = json.loads(f.read_text(encoding="utf-8")).get(code) or {}
                return [p.get("name") for p in m.get("pois") or [] if isinstance(p, dict) and p.get("name")][:40]
            except (OSError, ValueError, AttributeError):
                return []
    return []


# ---------------------------------------------------------------- downloads (model, component)

def _https_ok(url: str, hosts: tuple | None) -> bool:
    import urllib.parse
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.username or u.password:
        return False
    return hosts is None or host in hosts


def fetch(url: str, dest: Path, sha256: str, size: int, busy=None, hosts: tuple | None = MODEL_HOSTS,
          progress=None) -> None:
    """Download over HTTPS (redirects: https only) to dest; written as .part, kept only when size and SHA-256 match.
    `busy()` is asked every few seconds: a reason stops the download (Busy; the part is removed). `progress(n)` gets
    the bytes of each chunk written (the Settings line shows how far the first-time download is)."""
    import requests
    if not _https_ok(url, hosts):
        raise Unavailable("not an allowed download address")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        free = shutil.disk_usage(dest.parent).free
    except OSError:
        free = None
    if free is not None and free < size + SPARE:
        raise Unavailable(f"not enough free space on {dest.anchor or dest.parent} ({size / 1e9:.1f} GB needed + 2 GB spare)")
    part = dest.with_name(dest.name + ".part")
    h, got, looked = hashlib.sha256(), 0, time.monotonic()
    try:
        with requests.Session() as sess:
            sess.max_redirects = 5
            r = sess.get(url, stream=True, timeout=(15, 120), headers={"User-Agent": "PCS-Scanner"})
            if any(not x.url.startswith("https://") for x in r.history) or not r.url.startswith("https://"):
                raise Unavailable("the download was redirected to a non-https address")
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if progress:
                        progress(len(chunk))
                    if busy and time.monotonic() - looked >= BUSY_EVERY_S:
                        looked = time.monotonic()
                        b = busy()
                        if b:
                            raise Busy(b)
        if got != size or h.hexdigest() != sha256:
            raise Unavailable(f"{dest.name}: the download doesn't match the pinned file (size or SHA-256)")
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def model_ready(d: Path | None = None) -> bool:
    d = d or model_dir()
    if os.environ.get("PCS_WHISPER_MODEL"):
        return (d / "model.bin").is_file()
    return all((d / n).is_file() and (d / n).stat().st_size == size for n, size, _ in MODEL_FILES)


def missing_bytes(component: dict | None = None) -> int:
    """What the first transcript still has to download: the component zip (when not on this PC) + the model files
    not there yet. 0 = nothing (or a developer's own model / Python)."""
    comp = component if component is not None else COMPONENT
    n = 0 if runtime_cmd() or not comp else int(comp.get("size") or 0)
    if not os.environ.get("PCS_WHISPER_MODEL"):
        d = model_dir()
        n += sum(size for name, size, _ in MODEL_FILES if not ((d / name).is_file() and (d / name).stat().st_size == size))
    return n


def get_model(busy=None, progress=None) -> Path:
    d = model_dir()
    if model_ready(d):
        return d
    for name, size, sha in MODEL_FILES:
        f = d / name
        if f.is_file() and f.stat().st_size == size:
            continue
        log.info("downloading the transcript model: %s (%d MB)", name, size >> 20)
        fetch(MODEL_URL + name, f, sha, size, busy, progress=progress)
    return d


def runtime_cmd() -> list[str] | None:
    """How to start the transcriber, or None when it isn't on this PC."""
    py = os.environ.get("PCS_TRANSCRIBE_PY")
    if py:
        script = Path(__file__).resolve().parents[1] / "pcs_transcribe.py"
        if Path(py).is_file() and script.is_file():
            return [py, "-I", str(script)]
    exe = runtime_dir() / "pcs-transcribe" / COMPONENT_EXE
    return [str(exe)] if exe.is_file() else None


def safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Every member inside dest (no absolute paths, no ..)."""
    root = dest.resolve()
    for m in zf.infolist():
        target = (dest / m.filename).resolve()
        if root != target and root not in target.parents:
            raise Unavailable("the component zip holds a path outside its folder")
    zf.extractall(dest)


def get_runtime(busy=None, component: dict | None = None, progress=None) -> list[str]:
    cmd = runtime_cmd()
    if cmd:
        return cmd
    comp = component if component is not None else COMPONENT
    if not comp:
        raise Unavailable("the Transcripts download isn't published yet")
    zpath = base_dir() / "PCS-Transcripts.zip"
    fetch(comp["url"], zpath, comp["sha256"], int(comp["size"]), busy, hosts=("github.com",), progress=progress)
    tmp = runtime_dir().with_name("runtime.new")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        with zipfile.ZipFile(zpath) as zf:
            safe_extract(zf, tmp)
        if not (tmp / "pcs-transcribe" / COMPONENT_EXE).is_file():
            raise Unavailable("the component zip has no pcs-transcribe.exe")
        shutil.rmtree(runtime_dir(), ignore_errors=True)
        os.replace(tmp, runtime_dir())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        zpath.unlink(missing_ok=True)
    return runtime_cmd() or []


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


# ---------------------------------------------------------------- the job

HUB_TOO_OLD = "Not sent: your team hub needs an update before it can take transcripts"


class Comms:
    """`settings()` gives the recorder settings; `recordings()` the app's own recordings as {key: Entry}; `busy()` a
    reason not to run now, or None; `send(files)` sends files to the team hub (None: no hub)."""

    def __init__(self, settings, recordings, busy, send=None, now=time.time, version="", hub_ok=None,
                 server_ok=None, hub_call=None):
        self.settings, self.recordings, self.busy, self.send, self.now = settings, recordings, busy, send, now
        # server_ok(): the hub makes transcripts itself (/api/me lists "comms-server"); hub_call(method, path, **kw)
        # -> requests.Response with the player's key (pcs_recorder.hub.request). None = never on the server.
        self.server_ok, self.hub_call = server_ok, hub_call
        self.told: tuple | None = None                   # (hub address, key fingerprint) that took the player's yes
        self.next_fetch = 0.0
        # hub_ok(): the team hub keeps transcripts with their rules (its /api/me lists "comms"). An older hub would
        # store the file as a plain scan file any teammate can download, so nothing is sent to it (None = no check).
        self.hub_ok = hub_ok
        self.version = version
        self.lock = threading.Lock()
        self.running = False
        self.proc: subprocess.Popen | None = None
        self.current: str | None = None
        self.progress: tuple[float, float] | None = None
        self.download: list[int] | None = None           # [bytes done, bytes total] of the first-time download
        self.next_try = 0.0
        self.note = ""                                   # last reason it couldn't run (shown in Settings)
        self.st = self._load()

    # ------------------------------------------------------------ state
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
            log.warning("transcripts state not saved: %s", e)

    def where(self) -> str:
        """Where transcripts are made: "server" when the team hub makes them (it lists "comms-server" and the player's
        mic track goes to it), else "local" (this PC)."""
        s = self.settings()
        if self.server_ok is None or self.hub_call is None or not s.hub_url or not (s.send_mic and s.comms_consent):
            return "local"
        try:
            return "server" if self.server_ok() else "local"
        except Exception:
            return "local"

    # ------------------------------------------------------------ when
    def tick(self) -> bool:
        """Called every minute by the app. -> a round started."""
        try:
            s = self.settings()
            if not eligible(s):
                self.stop("the player said no")
                self.drop_server_copies()
                return False
            if self.busy() or self.running or self.now() < self.next_try:
                return False
            if self.where() == "server":
                if self.now() < self.next_fetch:
                    return False
                self.next_fetch = self.now() + FETCH_EVERY_S
                with self.lock:
                    if self.running:
                        return False
                    self.running = True
                threading.Thread(target=self._run_server, daemon=True, name="transcripts-server").start()
                return True
            if not self.todo() and not self.unsent():
                return False
        except Exception:
            log.exception("transcripts check")
            return False
        with self.lock:
            if self.running:
                return False
            self.running = True
        threading.Thread(target=self._run, daemon=True, name="transcripts").start()
        return True

    def todo(self) -> list[tuple[str, str]]:
        """(key, recording path) still to transcribe, oldest first."""
        now, out = self.now(), []
        for k, e in (self.recordings() or {}).items():
            if e.status not in ("scanned", "uploaded") or not e.path or not os.path.isfile(e.path):
                continue
            if getattr(e, "nogame", False) or now - float(e.started or 0) > MAX_AGE_DAYS * 86400:
                continue
            stem = Path(e.path).stem
            r = self.st["recs"].get(stem) or {}
            if comms_path(e.path).is_file() or r.get("tries", 0) >= MAX_TRIES or r.get("status") == "no_mic":
                continue
            out.append((float(e.started or 0), k, e.path))
        return [(k, p) for _, k, p in sorted(out)]

    def unsent(self) -> list[Path]:
        """Transcripts written but not on the hub yet (the hub was down, or set up later)."""
        if self.send is None or not self.settings().hub_url:
            return []
        out = []
        for e in (self.recordings() or {}).values():
            if not e.path:
                continue
            f = comms_path(e.path)
            r = self.st["recs"].get(Path(e.path).stem) or {}
            if f.is_file() and not r.get("sent"):
                out.append(f)
        return out

    def _run(self) -> None:
        from .hubbackup import _background
        _background(True)
        try:
            self.note = ""
            for f in self.unsent():
                self._send(f)
            for _, path in self.todo():
                self._check_busy()
                self.transcribe(path)
        except Busy as e:
            log.info("transcripts paused: %s", e)
            self.next_try = self.now() + 120
        except Unavailable as e:
            log.info("transcripts can't run: %s", e)
            self.note = str(e)
            self.next_try = self.now() + 6 * 3600
        except Exception as e:
            log.exception("transcripts")
            self.note = f"failed: {e}"
            self.next_try = self.now() + 1800
        finally:
            _background(False)
            with self.lock:
                self.running, self.current, self.progress, self.download = False, None, None, None

    def _check_busy(self) -> None:
        b = self.busy()
        if b:
            raise Busy(b)
        if not eligible(self.settings()):
            raise Busy("the player said no")

    # ------------------------------------------------------------ one recording
    def transcribe(self, path: str) -> Path | None:
        s = self.settings()
        stem = Path(path).stem
        rec = self.st["recs"].setdefault(stem, {"tries": 0})
        stream = mic_stream(path)
        if stream is None:
            rec.update(status="no_mic")
            self._save()
            return None
        need = missing_bytes()
        self.download = [0, need] if need else None

        def got(n: int) -> None:
            if self.download:
                self.download[0] = min(self.download[0] + n, self.download[1])
        try:
            cmd = get_runtime(self.busy, progress=got)
            model = get_model(self.busy, progress=got)
        finally:
            self.download = None
        self._check_busy()
        out = comms_path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        args = cmd + [str(path), "--stream", str(stream), "--ffmpeg", _ffmpeg(), "--model", str(model),
                      "--out", str(out), "--speaker", s.player or "", "--prompt", prompt_for(path),
                      "--version", self.version]
        rec["tries"] = rec.get("tries", 0) + 1
        self._save()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        log.info("transcribing the mic track of %s", stem)
        self.current = stem
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, creationflags=flags)
        self.proc = p
        lines: list[str] = []
        reader = threading.Thread(target=self._read, args=(p, lines), daemon=True)
        reader.start()
        try:
            while p.poll() is None:
                time.sleep(BUSY_EVERY_S)
                b = self.busy() or (None if eligible(self.settings()) else "the player said no")
                if b:
                    from .core import stop_tree
                    stop_tree(p)
                    rec["tries"] -= 1                    # a stop is not a failure: goes on from the kept parts
                    self._save()
                    raise Busy(b)
        finally:
            self.proc = None
        reader.join(5)
        if p.returncode != 0 or not out.is_file():
            err = next((json.loads(x).get("error") for x in reversed(lines) if x.startswith("{") and '"error"' in x), "")
            rec.update(status="failed", note=(err or f"exit code {p.returncode}")[:200])
            self._save()
            if p.returncode == 3:
                raise Unavailable("the transcriber is missing its libraries")
            log.warning("transcript of %s failed: %s", stem, rec["note"])
            return None
        try:
            d = json.loads(out.read_text(encoding="utf-8"))
            rec.update(status="done", note="", segments=len(d.get("segments") or []), audio=d.get("audioSeconds"),
                       seconds=d.get("seconds"), language=d.get("language"), at=self.now())
        except (OSError, ValueError):
            rec.update(status="done", at=self.now())
        self._save()
        log.info("transcript written for %s", stem)
        self._send(out)
        return out

    def _read(self, p, lines: list[str]) -> None:
        for line in p.stdout:
            line = line.strip()
            lines.append(line)
            try:
                d = json.loads(line)
                if "done" in d:
                    self.progress = (float(d["done"]), float(d["total"]))
            except (ValueError, TypeError, KeyError):
                pass

    def _send(self, f: Path) -> bool:
        """The transcript to the team hub (never Drive), while the player still says yes."""
        if self.send is None or not self.settings().hub_url or not eligible(self.settings()):
            return False
        stem = f.name[:-len(SUFFIX)]
        rec = self.st["recs"].setdefault(stem, {"tries": 0})
        if self.hub_ok is not None and not self.hub_ok():
            rec["sendNote"] = HUB_TOO_OLD
            self._save()
            return False
        try:
            self.send([f])
        except Exception as e:
            log.info("transcript not sent to the hub yet: %s", e)
            rec["sendNote"] = str(e)[:200]
            self._save()
            return False
        rec.update(sent=self.now(), sendNote="")
        self._save()
        return True

    def stop(self, why: str = "") -> None:
        """Stop a running transcriber (the player said no, or the app closes). The part being done is lost."""
        p = self.proc
        if p is not None and p.poll() is None:
            from .core import stop_tree
            log.info("transcriber stopped: %s", why)
            stop_tree(p)

    # ------------------------------------------------------------ on the team server
    def _run_server(self) -> None:
        """Tell the hub the yes, then fetch the transcripts it made of this PC's recordings."""
        try:
            self.note = ""
            if self.tell_yes():
                self.fetch_from_hub()
        except Exception as e:
            log.info("transcripts from the team server: %s", e)
        finally:
            with self.lock:
                self.running = False

    def tell_yes(self) -> bool:
        """PUT /api/me/transcripts {"transcripts": true} once per hub address and key. True = the hub holds the yes."""
        from . import hub
        s = self.settings()
        key = hub.get_key()
        if not key or not eligible(s):
            return False
        who = (s.hub_url, hub._fp(key))
        if self.told == who:
            return True
        r = self.hub_call("PUT", "/api/me/transcripts", json={"transcripts": True}, timeout=8)
        if r.status_code >= 400:
            log.info("transcripts answer refused by the hub: %s", r.status_code)
            return False
        self.told = who
        return True

    def fetch_from_hub(self) -> int:
        """The hub's transcripts of this PC's recent recordings that have none here yet -> `<rec>.comms.json`.
        Returns how many were written."""
        want = {}
        now = self.now()
        for e in (self.recordings() or {}).values():
            if e.path and e.status == "uploaded" and now - float(e.started or 0) <= FETCH_DAYS * 86400 \
                    and not comms_path(e.path).is_file():
                want[Path(e.path).stem] = e.path
        if not want:
            return 0
        me = self.hub_call("GET", "/api/me", timeout=8).json().get("name") or ""
        rows = self.hub_call("GET", "/api/scans", params={"limit": 300}, timeout=15).json()
        n = 0
        for r in rows if isinstance(rows, list) else []:
            stem = r.get("recording") or ""
            if r.get("kind") != "comms" or stem not in want or (r.get("uploader") or "").lower() != me.lower():
                continue
            if not eligible(self.settings()):
                break
            resp = self.hub_call("GET", f"/api/scans/{int(r['id'])}/file", timeout=30)
            if resp.status_code != 200:
                continue
            try:
                d = json.loads(resp.content.decode("utf-8"))
                if d.get("kind") != "comms":
                    continue
            except (ValueError, UnicodeDecodeError, AttributeError):
                continue
            out = comms_path(want.pop(stem))
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(out.name + ".tmp")
            tmp.write_bytes(resp.content)
            os.replace(tmp, out)
            self.st["recs"][stem] = {"status": "server", "sent": self.now(), "tries": 0}
            n += 1
        if n:
            self._save()
            log.info("%d transcript(s) made on the team server downloaded", n)
        return n

    def drop_server_copies(self) -> int:
        """Said no: the transcripts downloaded from the team server are deleted here (only those)."""
        n = 0
        for e in (self.recordings() or {}).values():
            if not e.path:
                continue
            stem = Path(e.path).stem
            if (self.st["recs"].get(stem) or {}).get("status") != "server":
                continue
            try:
                comms_path(e.path).unlink(missing_ok=True)
                n += 1
            except OSError:
                continue
            self.st["recs"].pop(stem, None)
        if n:
            self._save()
            log.info("%d transcript(s) from the team server deleted (the player said no)", n)
        self.told = None
        return n

    def cleanup(self) -> None:
        """Forget recordings the app no longer has (their transcript went with the results folder)."""
        keep = {Path(e.path).stem for e in (self.recordings() or {}).values() if e.path}
        left = {k: v for k, v in self.st["recs"].items() if k in keep}
        if len(left) != len(self.st["recs"]):
            self.st["recs"] = left
            self._save()

    def public(self) -> dict:
        s = self.settings()
        done = sum(1 for r in self.st["recs"].values() if r.get("status") == "done")
        prog = None
        if self.progress and self.progress[1]:
            prog = round(100 * self.progress[0] / self.progress[1])
        return {"on": eligible(s), "where": self.where() if eligible(s) else "local", "running": self.running, "current": self.current, "percent": prog, "done": done,
                "note": self.note, "model": model_ready(), "runtime": runtime_cmd() is not None,
                "available": runtime_cmd() is not None or bool(COMPONENT),
                "download": ({"done": self.download[0], "total": self.download[1]} if self.download else None)}


def transcript_of(rec_path: str | Path) -> dict | None:
    """The recording's transcript for the review player: {language, speaker, segments}, or None."""
    f = comms_path(rec_path)
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    segs = [{"start": s["start"], "end": s["end"], "text": s["text"], "speaker": s.get("speaker") or d.get("speaker")}
            for s in d.get("segments") or [] if isinstance(s, dict) and isinstance(s.get("text"), str)
            and isinstance(s.get("start"), (int, float))]
    return {"language": d.get("language"), "speaker": d.get("speaker"), "model": d.get("model"), "segments": segs}

