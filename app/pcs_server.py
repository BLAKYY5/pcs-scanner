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
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pcs_core as core

WEB = Path(core.HERE) / "web"
VIDEO_EXT = (".mp4", ".mkv", ".mov")
REC_FIELDS = ("folder", "keep_days", "max_gb", "bitrate_mbps", "game_audio", "auto_scan", "scan_delay_s",
              "capture", "hub_url", "start_with_windows")


def vid(path: str) -> str:
    return hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode()).hexdigest()[:12]


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
        self.google = "checking"                           # ok | expired | none | checking
        self.update = None                                 # (tag, exe_url, page) when a new version exists
        self.scan_queue: list[str] = []
        self.scan_now: str | None = None
        self.scan_lock = threading.Lock()
        self.scan_started = 0.0
        self._worker_running = False
        self.pick_files = lambda: []                       # native "open files" dialog
        self.open_path = lambda p: os.startfile(p) if os.name == "nt" else None
        self.set_start_with_windows = lambda on: None
        self.quit = lambda: None

    # ------------------------------------------------------------- messages
    def say(self, text: str) -> None:
        self.messages.append({"t": time.time(), "text": str(text)})

    # ------------------------------------------------------------- Google
    def check_google(self) -> None:
        try:
            ok = core.drive_service(interactive=False) is not None
        except Exception:
            ok = False
        self.google = "ok" if ok else ("expired" if os.path.exists(core.TOKEN_F) else "none")

    def signin(self) -> None:
        def go():
            try:
                if os.path.exists(core.TOKEN_F) and core.drive_service(interactive=False) is None:
                    os.remove(core.TOKEN_F)
                core.drive_service(True)
                self.google = "ok"
                self.say("Signed in with Google.")
                if self.auto:
                    self.auto.rec.next_upload = 0
            except Exception as e:
                self.say(f"Google sign-in failed: {e}")
        threading.Thread(target=go, daemon=True).start()

    # ------------------------------------------------------------- library
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
            summary = None
            if scan:
                try:
                    d = json.loads(scan.read_text(encoding="utf-8"))
                    summary = {"games": [{"map": g.get("map"), "mode": g.get("mode")} for g in d.get("games", [])],
                               "recStart": d.get("recStart"), "feedLines": d.get("perf", {}).get("feedLines")}
                except Exception:
                    summary = None
            status = "scanned" if scan else "not scanned"
            if norm in rec_index:                          # recorder's own: recording, recorded, uploaded, skipped…
                status = rec_index[norm][1].status
                if status in ("recorded", "skipped", "failed") and scan:
                    status = "scanned"
            if self.scan_now == p:
                status = "scanning"
            elif p in self.scan_queue:
                status = "waiting"
            items[i] = {"id": i, "name": Path(p).name, "path": p, "size": st.st_size, "mtime": st.st_mtime,
                        "status": status, "scan": summary, "auto": norm in rec_index}
        return sorted(items.values(), key=lambda x: -x["mtime"])

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
                games.append({k: g.get(k) for k in ("map", "mode", "tStart", "tEnd", "ring", "landing", "rankedShare",
                                                     "clockStart", "harvesters")}
                             | {"drop": {k: (g.get("drop") or {}).get(k) for k in ("spots", "err")},
                                "track": [[s[0], s[4], s[5], s[6], s[7]] for s in g.get("samples", []) if s[4] is not None]})
            it["games"] = games
        if feed:
            f = json.loads(feed.read_text(encoding="utf-8"))
            it["feed"] = [{"t": l.get("t"), "jpg": l.get("jpg")} for l in f.get("lines", [])]
        return it

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

    def _busy_reason(self) -> str | None:
        """Manual scans wait while Apex runs or the recorder is busy, so a game never shares the PC with a scan."""
        a = self.auto
        if a and (a.rec.current is not None or a.rec.scan_proc is not None):
            return "the auto-recorder is busy"
        try:
            from pcs_recorder.core import apex_running
            if apex_running():
                return "Apex is running"
        except Exception:
            pass
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
                code = scan_process(p, core.OUT).wait()
                if code != 0:
                    self.say(f"Scan of {Path(p).name} failed (code {code}). Details: {Path(core.OUT) / 'scan.log'}")
                    continue
                scan, feed = scan_files(p)
                files = [x for x in (scan, feed) if x]
                try:
                    if self.auto:
                        where = self.auto.sender.upload(files)
                    else:
                        where = ["Google Drive"] if core.drive_upload_quiet([str(x) for x in files]) else []
                    self.say(f"Scan of {Path(p).name} done" + (f", sent to {' and '.join(where)}." if where else
                                                                 " (not sent: sign in with Google)."))
                except Exception as e:
                    self.say(f"Scan of {Path(p).name} done, but not sent: {e}")
        except Exception:
            self.say("Scan failed: " + traceback.format_exc().splitlines()[-1])
        finally:
            with self.scan_lock:
                self.scan_now = None
                self._worker_running = False

    # ------------------------------------------------------------- state for the Home page
    def state(self) -> dict:
        a = self.auto
        rec = None
        if a:
            used, n = a.rec.usage()
            s = a.s
            rec = {"enabled": s.enabled, "status": a.rec.status, "recording": a.rec.current is not None,
                   "scanning": a.rec.scan_proc is not None, "waiting": len(a.rec.queue),
                   "used": used, "count": n,
                   "settings": {k: getattr(s, k) for k in REC_FIELDS if hasattr(s, k)},
                   "gbPerHour": round(s.gb_per_hour, 2), "encoder": s.encoder}
        return {"version": core.BUILD.get("version"), "player": core.SETTINGS.get("player", ""),
                "role": core.SETTINGS.get("role", ""), "google": self.google,
                "update": {"tag": self.update[0], "page": self.update[2]} if self.update else None,
                "recorder": rec, "messages": list(self.messages)[-60:],
                "scans": {"now": Path(self.scan_now).name if self.scan_now else None, "queued": len(self.scan_queue),
                          "since": self.scan_started if self.scan_now else None},
                "results": core.OUT}

    def save_settings(self, body: dict) -> None:
        if "player" in body:
            core.SETTINGS["player"] = str(body["player"]).strip()
        if "role" in body:
            core.SETTINGS["role"] = str(body["role"])
        core.save_settings()
        a = self.auto
        if a:
            a.s.player = core.SETTINGS.get("player", "")
            before = (a.s.start_with_windows,)
            for k in REC_FIELDS:
                if k in body:
                    setattr(a.s, k, body[k])
            from pcs_recorder import settings as st
            st.save(a.s)
            if (a.s.start_with_windows,) != before:
                self.set_start_with_windows(a.s.start_with_windows)
            removed = a.rec.cleanup()
            if removed:
                self.say(f"{len(removed)} old recording(s) removed to stay within your limits.")

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
            def _json(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
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
                try:
                    return json.loads(self.rfile.read(n))
                except json.JSONDecodeError:
                    return {}

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

            # -- video with seeking
            def _video(self, item_id):
                it = b.find(item_id)
                if not it:
                    return self._json({"error": "not found"}, 404)
                p = it["path"]
                size = os.path.getsize(p)
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
                ctype = "video/mp4" if p.lower().endswith((".mp4", ".mov")) else "video/x-matroska"
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
                if not path.startswith(("/api/", "/video/")):
                    if not self._ok_origin():
                        return self._json({"error": "forbidden"}, 403)
                    return self._static(path)
                if not self._authed(q):
                    return self._json({"error": "forbidden"}, 403)
                try:
                    if path.startswith("/video/"):
                        return self._video(path.split("/")[2])
                    if path == "/api/state":
                        return self._json(b.state())
                    if path == "/api/library":
                        return self._json(b.library())
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
                body = self._body()
                try:
                    if path == "/api/settings":
                        b.save_settings(body)
                        return self._json(b.state())
                    if path == "/api/autorecord":
                        b.set_autorecord(bool(body.get("enabled")))
                        return self._json(b.state())
                    if path == "/api/signin":
                        b.signin()
                        return self._json({"ok": True})
                    if path == "/api/update":
                        if b.update and b.update[1] and core.install_update(b.update[1], b.say):
                            threading.Timer(1.0, b.quit).start()
                        return self._json({"ok": True})
                    if path == "/api/library/pick":
                        n = b.add_paths(b.pick_files() or [])
                        return self._json({"added": n})
                    if path == "/api/library/add":
                        return self._json({"added": b.add_paths(body.get("paths", []))})
                    if path == "/api/library/forget":
                        lib = core.SETTINGS.get("library", [])
                        it = b.find(body.get("id", ""))
                        if it and it["path"] in lib:
                            lib.remove(it["path"])
                            core.save_settings()
                        return self._json({"ok": True})
                    if path == "/api/scan":
                        return self._json({"queued": b.queue_scans(body.get("ids", []))})
                    if path == "/api/open":
                        what = body.get("what")
                        if what == "results":
                            b.open_path(core.OUT)
                        elif what == "recordings" and b.auto:
                            b.open_path(str(b.auto.s.folder_path))
                        elif what == "file":
                            it = b.find(body.get("id", ""))
                            if it:
                                b.open_path(it["path"])
                        return self._json({"ok": True})
                    if path == "/api/quit":
                        threading.Timer(0.3, b.quit).start()
                        return self._json({"ok": True})
                    return self._json({"error": "not found"}, 404)
                except Exception as e:
                    return self._json({"error": str(e)}, 500)

        return H
