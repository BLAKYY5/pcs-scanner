"""Tests for the app's local server (what the window talks to).
Run:  python tests/test_ui.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["PCS_DATA_DIR"] = str(TMP / "appdata")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

try:
    import tkinter  # noqa: F401
except ImportError:                      # Linux without Tk: the recorder's windows aren't used by these tests
    import types
    for m in ("tkinter", "tkinter.filedialog", "tkinter.ttk"):
        sys.modules[m] = types.ModuleType(m)
    sys.modules["tkinter"].filedialog = sys.modules["tkinter.filedialog"]
    sys.modules["tkinter"].ttk = sys.modules["tkinter.ttk"]
import pcs_core as core  # noqa: E402
import pcs_server  # noqa: E402

VIDEO = TMP / "vods" / "Apex Legends_2026.11.02-09.03.mp4"
DATA = bytes(range(256)) * 4096          # 1 MB with a recognisable pattern


def setup():
    VIDEO.parent.mkdir(parents=True, exist_ok=True)
    VIDEO.write_bytes(DATA)
    scan = VIDEO.with_suffix(".scan")
    scan.mkdir(exist_ok=True)
    game = {"map": "BM", "mode": "ranked", "tStart": 132.0, "tEnd": 135.0,
            "drop": {"spots": [{"poi": "bm-cultivation", "x": 40.8, "y": 76.7, "kind": "ours"}], "err": 0.017},
            "ring": [{"kind": "wait", "round": 1, "appearedAt": 196.0, "firstSeen": 197.0, "endsAt": 271.5, "lastSeen": 261.0}],
            "landing": {"exitAtExact": 180.2, "landedAtExact": 187.05},
            "samples": [[154.0, -1, None, False, 1.0, 2.0, 3.0, 0.0, False, False], [160.0, -1, None, False, None, None, None, 0, False, False]]}
    (scan / (VIDEO.stem + ".scan.json")).write_text(json.dumps({"games": [game], "recStart": "2026-11-02T09:03:00", "perf": {"feedLines": 1}}))
    (scan / (VIDEO.stem + ".feed.json")).write_text(json.dumps({"lines": [{"t": 123.0, "y": 260, "jpg": "AAAA"}]}))
    (TMP / "vods" / "notes.txt").write_text("not a video")


def call(srv, path, body=None, token=True, headers=None, raw=False):
    url = f"http://127.0.0.1:{srv.port}{path}"
    h = dict(headers or {})
    if token:
        h["X-PCS-Token"] = srv.token
    data = json.dumps(body).encode() if body is not None else None
    if data:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            out = r.read()
            return r.status, (out if raw else json.loads(out)), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, (e.read() if raw else json.loads(e.read() or b"{}")), dict(e.headers)


def test_server():
    setup()
    b = pcs_server.Backend()
    srv = pcs_server.Server(b)
    srv.start()
    try:
        # no token / wrong token: refused
        assert call(srv, "/api/state", token=False)[0] == 403
        assert call(srv, "/api/state", headers={"X-PCS-Token": "nope"}, token=False)[0] == 403
        # a page on another site can't reach it (Host check, DNS rebinding)
        assert call(srv, "/api/state", headers={"Host": "evil.example"})[0] == 403
        # the interface itself loads without a token, nothing outside the web folder does
        code, html, _ = call(srv, "/", token=False, raw=True)
        assert code == 200 and b"PCS Scanner" in html
        assert call(srv, "/../pcs_core.py", token=False, raw=True)[0] == 404
        assert call(srv, "/%2e%2e/pcs_core.py", token=False, raw=True)[0] == 404

        code, st, _ = call(srv, "/api/state")
        assert code == 200 and st["recorder"] is None and st["version"]

        # add a folder: only the video is added, once
        code, r, _ = call(srv, "/api/library/add", {"paths": [str(VIDEO.parent)]})
        assert r["added"] == 1, r
        assert call(srv, "/api/library/add", {"paths": [str(VIDEO)]})[1]["added"] == 0
        code, lib, _ = call(srv, "/api/library")
        assert len(lib) == 1 and lib[0]["status"] == "scanned" and lib[0]["scan"]["games"][0]["map"] == "BM", lib
        vid = lib[0]["id"]

        code, d, _ = call(srv, f"/api/library/{vid}")
        assert code == 200 and d["games"][0]["landing"]["landedAtExact"] == 187.05
        assert d["games"][0]["track"] == [[154.0, 1.0, 2.0, 3.0, 0.0]]          # rows without a position are dropped
        assert d["feed"] == [{"t": 123.0, "jpg": "AAAA"}]
        assert call(srv, "/api/library/000000000000")[0] == 404

        # video: whole file, a range, an open range, the last bytes, an impossible range
        code, body, h = call(srv, f"/video/{vid}", raw=True)
        assert code == 200 and body == DATA and h["Accept-Ranges"] == "bytes" and h["Content-Type"] == "video/mp4"
        code, body, h = call(srv, f"/video/{vid}", headers={"Range": "bytes=1000-1999"}, raw=True)
        assert code == 206 and body == DATA[1000:2000] and h["Content-Range"] == f"bytes 1000-1999/{len(DATA)}"
        code, body, h = call(srv, f"/video/{vid}", headers={"Range": f"bytes={len(DATA) - 10}-"}, raw=True)
        assert code == 206 and body == DATA[-10:]
        code, body, h = call(srv, f"/video/{vid}", headers={"Range": "bytes=-16"}, raw=True)
        assert code == 206 and body == DATA[-16:]
        code, body, h = call(srv, f"/video/{vid}", headers={"Range": f"bytes={len(DATA) + 5}-"}, raw=True)
        assert code == 416
        # the token can also be in the address (the <video> element can't send headers)
        code, body, _ = call(srv, f"/video/{vid}?t={srv.token}", token=False, headers={"Range": "bytes=0-3"}, raw=True)
        assert code == 206 and body == DATA[:4]
        # only listed recordings can be read
        assert call(srv, "/video/notalistedid", raw=True)[0] == 404

        # settings: name and role are saved
        code, st, _ = call(srv, "/api/settings", {"player": "  Blakey ", "role": "IGL"})
        assert st["player"] == "Blakey" and core.SETTINGS["role"] == "IGL"
        saved = json.loads(Path(core.SETTINGS_F).read_text())
        assert saved["player"] == "Blakey" and str(VIDEO) in saved["library"]

        # forget removes it from the list, the file stays
        call(srv, "/api/library/forget", {"id": vid})
        assert call(srv, "/api/library")[1] == [] and VIDEO.exists()
        assert b.messages is not None
    finally:
        srv.stop()


class _S:            # recorder settings stand-in
    enabled, start_with_windows, player, hub_url = True, True, "", ""
    keep_days, max_gb, bitrate_mbps, game_audio, auto_scan, scan_delay_s, capture = 7, 100, 12, True, True, 60, "window"
    encoder, gb_per_hour = "av1_texture_amf", 5.5
    comms_consent, discord_audio, mic_audio, send_mic, hk_sounds = False, True, True, True, True
    hk_mark, hk_mute_game, hk_mute_discord, hk_mute_mic = "ctrl+alt+m", "ctrl+alt+1", "ctrl+alt+2", "ctrl+alt+3"
    yt_enabled, yt_privacy, yt_audio, yt_scrims_only, yt_gap_min, yt_since = False, "unlisted", "game", True, 45, 0.0
    yt_when, night_from, night_to, night_idle_min, night_shutdown = "night", 1, 7, 20, False
    yt_team, yt_wait_eec, yt_wait_eec_h = "PCS", True, 12

    def __init__(self, folder):
        self.folder = folder


class _Rec:
    def __init__(self):
        self.idx, self.status, self.current, self.scan_proc, self.queue = {}, "Waiting for Apex", None, None, []
        self.cleaned = 0

    def usage(self):
        return 0, 0

    def cleanup(self):
        self.cleaned += 1
        return []


class _Auto:
    def __init__(self, folder):
        self.s, self.rec = _S(folder), _Rec()


def test_recorder_settings_and_start_with_windows():
    from pcs_recorder import settings as st
    b = pcs_server.Backend(_Auto(str(TMP / "recs")))
    calls = []
    b.set_start_with_windows = calls.append
    st_save = st.save
    st.save = lambda s: None                # the stand-in isn't a real Settings object
    try:
        srv = pcs_server.Server(b)
        srv.start()
        code, state, _ = call(srv, "/api/settings", {"start_with_windows": False, "keep_days": 3, "bogus": 1})
        assert code == 200 and calls == [False] and b.auto.s.keep_days == 3 and not hasattr(b.auto.s, "bogus")
        assert state["recorder"]["settings"]["start_with_windows"] is False
        call(srv, "/api/settings", {"keep_days": 4})                 # unchanged: no second shortcut update
        assert calls == [False] and b.auto.rec.cleaned == 2
        call(srv, "/api/settings", {"start_with_windows": True})
        assert calls == [False, True]
        srv.stop()
    finally:
        st.save = st_save
    assert st.Settings().start_with_windows is True                  # on by default


def test_scan_queue_waits_for_apex():
    """A scan asked for by hand never runs while Apex (or the recorder) is busy."""
    from pcs_recorder import core as rcore, runtime
    setup()
    b = pcs_server.Backend()
    b.add_paths([str(VIDEO)])
    vid = b.library()[0]["id"]
    apex = [True]
    started = []

    class P:
        def wait(self):
            return 0
    real_running, real_proc = rcore.apex_running, runtime.scan_process
    rcore.apex_running = lambda: apex[0]
    runtime.scan_process = lambda v, out: (started.append(v), P())[1]
    b.auto = None
    sent = []
    core_upload = core.drive_upload_quiet
    core.drive_upload_quiet = lambda paths: sent.append(paths) or True
    try:
        assert b.queue_scans([vid]) == 1
        assert b.queue_scans([vid]) == 0                     # already queued
        time.sleep(1.0)
        assert started == [] and b.library()[0]["status"] == "waiting"
        apex[0] = False
        for _ in range(40):
            if sent:
                break
            time.sleep(0.25)
        assert started == [str(VIDEO)] and sent, (started, sent)
        assert any("done, sent to Google Drive" in m["text"] for m in b.messages), list(b.messages)
    finally:
        rcore.apex_running, runtime.scan_process, core.drive_upload_quiet = real_running, real_proc, core_upload


def test_audio_tracks():
    """A 3-track recording: the review gets each track as its own file, named from the recorder's layout."""
    import subprocess
    if not shutil.which("ffmpeg"):
        print("  (no ffmpeg: audio track test skipped)")
        return
    v = TMP / "multi" / "Apex Legends_2026.11.06-21.00.mp4"
    v.parent.mkdir(parents=True, exist_ok=True)
    tones = []
    for f in (440, 660, 880):
        tones += ["-f", "lavfi", "-i", f"sine=frequency={f}:duration=3"]
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=black:s=160x90:d=3", *tones,
                    "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:a", "-c:v", "mpeg4", "-c:a", "aac",
                    "-shortest", str(v)], check=True)
    v.with_suffix(".events.json").write_text(json.dumps({"tracks": ["game", "discord", "mic"],
                                                         "events": [{"t": 1.5, "kind": "mark"}]}))
    b = pcs_server.Backend()
    b.add_paths([str(v)])
    srv = pcs_server.Server(b)
    srv.start()
    try:
        it = next(x for x in b.library() if x["path"] == str(v))
        d = call(srv, f"/api/library/{it['id']}")[1]
        assert [a["label"] for a in d["audio"]] == ["Game", "Discord", "Microphone"], d["audio"]
        assert d["marks"] == [{"t": 1.5, "kind": "mark"}]
        code, body, h = call(srv, f"/audio/{it['id']}/2", raw=True)
        assert code == 200 and h["Content-Type"] == "audio/mp4" and len(body) > 1000
        assert v.with_name(v.stem + ".track3.m4a").exists()          # kept next to the recording, deleted with it
        code, body, h = call(srv, f"/audio/{it['id']}/1", headers={"Range": "bytes=0-99"}, raw=True)
        assert code == 206 and len(body) == 100
        assert call(srv, f"/audio/{it['id']}/7", raw=True)[0] == 404
    finally:
        srv.stop()


def test_update_button_and_checks():
    """The Update button appears once a newer release is found; installing waits while Apex/recording/scan runs."""
    from pcs_recorder import core as rcore
    b = pcs_server.Backend(_Auto(str(TMP / "recs-upd")))
    calls = []
    real_check, real_running = core.check_update, rcore.apex_running
    core.check_update = lambda: (calls.append(1), ("v9.9.9", "https://x/PCS-Scanner.exe", "https://x"))[1]
    rcore.apex_running = lambda: False
    srv = pcs_server.Server(b)
    srv.start()
    try:
        assert call(srv, "/api/state")[1]["update"] is None
        b.check_update()
        assert call(srv, "/api/state")[1]["update"]["tag"] == "v9.9.9" and len(calls) == 1
        b.check_update(pcs_server.UPDATE_ON_OPEN_S)        # window opened right after: no second request
        assert len(calls) == 1
        assert sum("v9.9.9" in m["text"] for m in b.messages) == 1
        b.auto.rec.current = "rec-1"                       # recording: refused, nothing downloaded
        code, r, _ = call(srv, "/api/update", {})
        assert code == 409 and "recording" in r["error"].lower()
        b.auto.rec.current = None
        rcore.apex_running = lambda: True
        assert call(srv, "/api/update", {})[0] == 409
    finally:
        core.check_update, rcore.apex_running = real_check, real_running
        srv.stop()


if __name__ == "__main__":
    try:
        for name, fn in list(globals().items()):
            if name.startswith("test_"):
                fn(); print("ok", name)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
