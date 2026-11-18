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

        b.coach = True                                     # importing by hand is the coach's
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


def test_recording_date_and_game_data_for_review():
    """'Recorded' is when the recording started (file name, else the scan's recStart), not the file's time; the
    review page gets each game's legend select, banner, mode, squads left and the player's own numbers."""
    import datetime
    folder = TMP / "review-vods"
    folder.mkdir(parents=True, exist_ok=True)
    named = folder / "Apex Legends_2026.11.07-21.42.mp4"           # OBS's name: the start time
    by_scan = folder / "scrim game 3.mp4"                            # renamed by hand, scan knows the start
    bare = folder / "clip.mp4"                                       # nothing tells: the file's time
    for v in (named, by_scan, bare):
        v.write_bytes(b"x" * 1000)
        os.utime(v, (1_900_000_000, 1_900_000_000))                  # written much later than recorded
    game = {"map": "SP", "mode": "ranked", "tStart": 541.0, "tEnd": 665.0, "tEndGame": 951.5, "drop": None,
            "ring": [{"kind": "wait", "round": 4, "firstSeen": 900.0, "endsAt": 990.0, "endsAfterGame": True}],
            "landing": None,
            "select": {"tFirst": 516.0, "tLast": 541.0, "ranked": True,
                       "squad": [{"slot": 1, "legend": "bangalore", "self": False, "hue": 77},
                                 {"slot": 2, "legend": "crypto", "self": True, "hue": 39}]},
            "banner": {"t": 461.0, "mode": "ranked", "map": "SP"},
            "squadsLeft": [[579.0, 20], [593.0, 19], [944.0, 5]],
            "own": {"kills": 2, "killsAt": 948.0, "assists": 1, "damage": 695, "damageSingleRead": False},
            "feedStats": {"squadKnocks": 1, "players": [{"slot": 2, "legend": "crypto", "self": True, "kills": 0}]},
            "samples": [[550.0, -1, None, False, 1.0, 2.0, 3.0, 0.0, False, False]]}
    old = {"map": "WE", "tStart": 10.0, "drop": {"spots": [], "err": 0}}              # a scan from before 1.10.8
    for v, games, rs in ((named, [game, old], "2026-11-07T21:42:00"), (by_scan, [old], "2026-11-03T22:17:00"), (bare, [old], None)):
        d = v.with_suffix(".scan")
        d.mkdir(exist_ok=True)
        (d / (v.stem + ".scan.json")).write_text(json.dumps({"games": games, "recStart": rs}))
    assert pcs_server.recorded_at(str(named)) == datetime.datetime(2026, 11, 7, 21, 42).timestamp()
    assert pcs_server.recorded_at("Apex Legends_2026.13.40-99.99.mp4") is None           # not a date: no crash
    b = pcs_server.Backend()
    before = list(core.SETTINGS.get("library", []))
    core.SETTINGS["library"] = [str(named), str(by_scan), str(bare)]
    srv = pcs_server.Server(b)
    srv.start()
    try:
        lib = {x["name"]: x for x in call(srv, "/api/library")[1]}
        assert lib[named.name]["recorded"] == datetime.datetime(2026, 11, 7, 21, 42).timestamp()
        assert lib[by_scan.name]["recorded"] == datetime.datetime(2026, 11, 3, 22, 17).timestamp()
        assert lib[bare.name]["recorded"] == lib[bare.name]["mtime"] == 1_900_000_000
        names = [x["name"] for x in call(srv, "/api/library")[1]]
        assert names == [bare.name, named.name, by_scan.name], names                   # newest recording first
        d = call(srv, f"/api/library/{lib[named.name]['id']}")[1]
        g, o = d["games"]
        assert g["select"]["squad"][1] == {"slot": 2, "legend": "crypto", "self": True, "hue": 39}
        assert g["banner"]["mode"] == "ranked" and g["mode"] == "ranked"
        assert g["squadsLeft"][-1] == [944.0, 5]
        assert g["tEndGame"] == 951.5 and g["ring"][0]["endsAfterGame"] is True      # game span; ring after the game
        js = (Path(pcs_server.WEB) / "app.js").read_text(encoding="utf-8")
        assert "g.tEndGame != null ? g.tEndGame" in js and "after your game ended" in js
        assert g["own"]["kills"] == 2 and "knocks" not in g["own"]                     # not read: the page shows "–"
        assert g["feedStats"]["players"][0]["legend"] == "crypto"
        assert o["select"] is None and o["own"] is None and o["squadsLeft"] is None   # old scans: fields just missing
        assert o["drop"]["spots"] == []
        # a missing file: the player can say so (404), not "format not supported"
        vid = lib[named.name]["id"]
        named.unlink()
        assert call(srv, f"/video/{vid}", raw=True)[0] == 404
    finally:
        srv.stop()
        core.SETTINGS["library"] = before


def test_review_page_texts():
    """The words the review player and Settings show (no browser in the tests: read from app.js)."""
    js = (Path(pcs_server.WEB) / "app.js").read_text(encoding="utf-8")
    assert "Your microphone is never recorded" not in js                       # Settings offers mic recording
    assert "Changes apply right away" not in js                                # most fields need Save changes
    assert "'Legend select'" in js and "'Game start'" in js and "Unsaved changes" in js
    assert "+ 480)" not in js and "r.gbPerHour + ' GB" not in js               # GB per hour: one formula (gbHour)
    assert "File missing" in js and "Video format not supported" in js


class _S:            # recorder settings stand-in
    enabled, start_with_windows, player, hub_url = True, True, "", ""
    keep_days, max_gb, bitrate_mbps, game_audio, auto_scan, scan_delay_s, capture = 7, 100, 12, True, True, 60, "window"
    encoder, gb_per_hour = "av1_texture_amf", 5.5
    comms_consent, discord_audio, mic_audio, send_mic, hk_sounds = False, True, True, True, True
    mic_device = "default"
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


def test_bitrate_setting_per_encoder():
    """Settings → Quality shows the encoder's bitrate (AV1 10, HEVC / H.264 12); saving the page unchanged keeps
    it automatic, another value is the player's own."""
    from pcs_recorder import settings as st
    a = _Auto(str(TMP / "recs"))
    a.s = st.Settings(folder=str(TMP / "recs"), encoder="av1_texture_amf").clamp()
    b = pcs_server.Backend(a)
    b.set_start_with_windows = lambda on: None
    st_save = st.save
    st.save = lambda s: s.clamp()                   # no recorder.json written by this test
    srv = pcs_server.Server(b)
    srv.start()
    try:
        code, state, _ = call(srv, "/api/state")
        r = state["recorder"]
        assert code == 200 and r["settings"]["bitrate_mbps"] == 10 and r["bitrateDefault"] == 10 and r["bitrateAuto"]
        code, state, _ = call(srv, "/api/settings", {"bitrate_mbps": 10, "keep_days": 5})
        assert code == 200 and a.s.bitrate_auto and state["recorder"]["settings"]["bitrate_mbps"] == 10
        code, state, _ = call(srv, "/api/settings", {"bitrate_mbps": 15})
        assert code == 200 and not a.s.bitrate_auto and state["recorder"]["bitrateAuto"] is False
        a.s.encoder = "obs_nvenc_hevc_tex"                  # e.g. "Test encoder again" on a new card
        a.s.clamp()
        code, state, _ = call(srv, "/api/state")
        assert state["recorder"]["settings"]["bitrate_mbps"] == 15 and state["recorder"]["bitrateDefault"] == 12
        call(srv, "/api/settings", {"bitrate_mbps": 12})    # back to the recommendation: automatic again
        assert a.s.bitrate_auto and a.s.bitrate_mbps == 12
    finally:
        srv.stop()
        st.save = st_save
    js = (Path(pcs_server.WEB) / "app.js").read_text(encoding="utf-8")
    assert "bitrateTip(r)" in js and "AV1 10, HEVC / H.264 12" in js and "12 is recommended." not in js


def test_voice_app_setting():
    """Settings → Voice app: saved through the API; "other" needs a program name, never a path."""
    from pcs_recorder import settings as st
    b = pcs_server.Backend(_Auto(str(TMP / "recs")))
    b.set_start_with_windows = lambda on: None
    st_save = st.save
    st.save = lambda s: None
    srv = pcs_server.Server(b)
    srv.start()
    try:
        code, state, _ = call(srv, "/api/settings", {"voice_app": "teamspeak", "voice_exe": ""})
        assert code == 200 and state["recorder"]["settings"]["voice_app"] == "teamspeak", state
        code, state, _ = call(srv, "/api/settings", {"voice_app": "other", "voice_exe": " Mumble.exe "})
        assert code == 200 and (b.auto.s.voice_app, b.auto.s.voice_exe) == ("other", "Mumble.exe")
        assert state["recorder"]["settings"]["voice_exe"] == "Mumble.exe"
        for bad in ({"voice_app": "other", "voice_exe": "C:\\Tools\\Mumble.exe"},
                    {"voice_app": "other", "voice_exe": "mumble"},
                    {"voice_app": "other", "voice_exe": "x.exe && del *"},
                    {"voice_app": "skype", "voice_exe": ""}):
            code, r, _ = call(srv, "/api/settings", dict(bad, keep_days=9))
            assert code != 200 and r.get("error"), (bad, code, r)
            assert b.auto.s.voice_exe == "Mumble.exe" and b.auto.s.keep_days != 9      # nothing saved
        code, _, _ = call(srv, "/api/settings", {"voice_app": "discord", "voice_exe": "x/y.exe"})
        assert code == 200 and (b.auto.s.voice_app, b.auto.s.voice_exe) == ("discord", "")
        code, v, _ = call(srv, "/api/voiceapps")
        assert code == 200 and v["current"] == "discord" and isinstance(v["running"], list)
        assert all(st.valid_exe(a["exe"]) and "\\" not in a["exe"] for a in v["running"]), v
    finally:
        srv.stop()
        st.save = st_save
    # a recording made with TeamSpeak names its track 2 after it in the review; older ones say Discord
    folder = TMP / "voice"
    folder.mkdir(exist_ok=True)
    rec = folder / "Apex Legends_ts.mp4"
    rec.write_bytes(b"x")
    rec.with_suffix(".events.json").write_text(json.dumps({"tracks": ["game", "discord", "mic"], "voice": "TeamSpeak"}))
    import subprocess
    real = subprocess.run
    subprocess.run = lambda *a, **k: type("R", (), {"stdout": "0\n1\n2\n"})()
    try:
        assert [t["label"] for t in pcs_server.audio_tracks(str(rec))] == ["Game", "TeamSpeak", "Microphone"]
        rec.with_suffix(".events.json").write_text(json.dumps({"tracks": ["game", "discord"]}))
        assert [t["label"] for t in pcs_server.audio_tracks(str(rec))] == ["Game", "Discord", "Audio 3"]
    finally:
        subprocess.run = real


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
        def wait(self, timeout=None):
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


def test_scan_by_hand_stops_when_apex_starts():
    """A scan asked for by hand that is running when Apex starts is stopped (not left running or paused in memory
    during the game) and goes on when Apex closes."""
    import subprocess
    from pcs_recorder import core as rcore
    b = pcs_server.Backend()
    apex = [False]
    stopped = []

    class P:
        def __init__(self):
            self.n = 0

        def wait(self, timeout=None):
            self.n += 1
            if self.n >= 2:
                apex[0] = True                       # Apex starts while it runs
            raise subprocess.TimeoutExpired("scan", timeout)
    real_running, real_stop = rcore.apex_running, rcore.stop_tree
    rcore.apex_running = lambda: apex[0]
    rcore.stop_tree = lambda p, **k: stopped.append(p)
    try:
        p = P()
        assert b._run_scan(p, every_s=0.01) is None and stopped == [p]
        apex[0] = False

        class Done:
            def wait(self, timeout=None):
                return 0
        assert b._run_scan(Done()) == 0 and stopped == [p]
    finally:
        rcore.apex_running, rcore.stop_tree = real_running, real_stop


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
        assert code == 503 and json.loads(body)["preparing"], (code, body)    # cut in the background, not now
        for _ in range(100):
            code, body, h = call(srv, f"/audio/{it['id']}/2", raw=True)
            if code != 503:
                break
            time.sleep(0.1)
        assert code == 200 and h["Content-Type"] == "audio/mp4" and len(body) > 1000
        assert v.with_name(v.stem + ".track3.m4a").exists()          # kept next to the recording, deleted with it
        assert v.with_name(v.stem + ".track1.m4a").exists()          # all tracks in one go
        assert not list(v.parent.glob("*.part.m4a"))
        code, body, h = call(srv, f"/audio/{it['id']}/1", headers={"Range": "bytes=0-99"}, raw=True)
        assert code == 206 and len(body) == 100
        assert call(srv, f"/audio/{it['id']}/7", raw=True)[0] == 404
    finally:
        srv.stop()


def test_delete_and_sound_levels():
    """Delete: app recordings for good (with their files), hand-added ones to the Recycle Bin; busy ones refused.
    Sound levels: one value per half second per track; loud game stretches and loud mic moments found."""
    import subprocess
    from pcs_recorder import retention
    folder = TMP / "del"
    folder.mkdir(exist_ok=True)
    own = folder / "Apex Legends_own.mp4"
    busy = folder / "Apex Legends_busy.mp4"
    mine = folder / "my vod.mp4"
    if shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=black:s=64x36:d=30",
                        "-f", "lavfi", "-i", "anoisesrc=d=30:a=0.01,volume='if(between(t,10,20),30,1)':eval=frame",
                        "-f", "lavfi", "-i", "anoisesrc=d=30:a=0.005,volume='if(between(t,5,6),80,1)':eval=frame",
                        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(own)],
                       check=True)
    else:
        own.write_bytes(b"x" * 1000)
    own.with_suffix(".events.json").write_text(json.dumps({"tracks": ["game", "mic"], "events": []}))
    (folder / "Apex Legends_own.track2.m4a").write_bytes(b"a")
    own.with_suffix(".scan").mkdir(exist_ok=True)
    busy.write_bytes(b"y" * 100)
    mine.write_bytes(b"z" * 100)
    auto = _Auto(str(folder))
    auto.rec.idx = {"k1": retention.Entry(path=str(own), started=time.time(), status="scanned"),
                    "k2": retention.Entry(path=str(busy), started=time.time(), status="scanning")}
    auto.rec.scan_key, auto.rec.lock = "k2", __import__("threading").RLock()
    b = pcs_server.Backend(auto)
    b.add_paths([str(mine)])
    lib = {x["name"]: x["id"] for x in b.library()}
    if shutil.which("ffmpeg"):
        lv = b.levels(lib[own.name])
        assert [t["label"] for t in lv["tracks"]] == ["Game", "Microphone"] and len(lv["tracks"][0]["level"]) >= 59
        assert lv["fights"] and 9 <= lv["fights"][0][0] <= 11 and 19 <= lv["fights"][0][1] <= 21.5, lv["fights"]
        assert lv["calls"] and 4.5 <= lv["calls"][0] <= 6, lv["calls"]
        assert own.with_name(own.stem + ".levels.json").exists()           # cached next to the recording
        assert b.levels(lib[own.name]) == lv
    real_save = retention.save_index
    retention.save_index = lambda idx: None
    try:
        r = b.delete([lib[own.name], lib[busy.name], lib[mine.name]])
    finally:
        retention.save_index = real_save
    assert sorted(r["deleted"]) == sorted([own.name, mine.name]) and r["skipped"] == [busy.name], r
    assert not own.exists() and not (folder / "Apex Legends_own.track2.m4a").exists() and not own.with_suffix(".scan").exists()
    assert not mine.exists() and busy.exists() and "k1" not in auto.rec.idx and "k2" in auto.rec.idx
    assert str(mine) not in core.SETTINGS.get("library", [])


def test_coach_role():
    """Coach only for the coach's Google account; importing recordings by hand is the coach's."""
    b = pcs_server.Backend()
    srv = pcs_server.Server(b)
    srv.start()
    real_build, real_drive = dict(core.BUILD), core.drive_service
    core.BUILD["coach"] = "coach@example.com"
    who = {"email": "player@example.com"}

    class Svc:
        def about(self):
            class A:
                def get(self, fields):
                    class X:
                        def execute(self):
                            return {"user": {"emailAddress": who["email"]}}
                    return X()
            return A()
    core.drive_service = lambda interactive=True: Svc()
    try:
        code, r, _ = call(srv, "/api/settings", {"role": "Coach", "player": "Xno"})
        assert code == 403 and "coach" in r["error"].lower()
        assert core.SETTINGS["player"] == "Xno" and core.SETTINGS.get("role") != "Coach"   # the rest was saved
        assert call(srv, "/api/library/add", {"paths": []})[0] == 403
        who["email"] = "Coach@Example.com"
        code, st, _ = call(srv, "/api/settings", {"role": "Coach"})
        assert code == 200 and st["coach"] is True and st["role"] == "Coach"
        assert call(srv, "/api/library/add", {"paths": []})[0] == 200
        code, st, _ = call(srv, "/api/settings", {"role": "IGL"})
        assert st["coach"] is False
    finally:
        core.BUILD.clear(); core.BUILD.update(real_build); core.drive_service = real_drive
        core.SETTINGS["role"] = ""
        srv.stop()


def test_team_link_weapon_icons():
    """The weapon icons come from the Google Drive link the player pastes in Settings: only Drive links are taken,
    only a real icons file is kept, and a new version of the file replaces the old one."""
    import io
    import numpy as np

    def icons(n):
        b = io.BytesIO()
        np.savez_compressed(b, **{f"gun{i}": np.full((20, 50), 10 * i, np.uint8) for i in range(n)})
        return b.getvalue()
    fid = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    assert core.drive_link_id(f"https://drive.google.com/file/d/{fid}/view?usp=sharing") == fid
    assert core.drive_link_id(f"https://drive.google.com/open?id={fid}") == fid
    assert core.drive_link_id(f"https://evil.example.com/file/d/{fid}/view") is None
    assert core.drive_link_id(f"http://drive.google.com/file/d/{fid}/view") is None
    assert core.icons_valid(icons(3)) and not core.icons_valid(b"<html>Sign in</html>") and not core.icons_valid(b"PK\x03\x04junk")
    big = io.BytesIO(); np.savez_compressed(big, gun=np.zeros((500, 900), np.uint8))
    assert not core.icons_valid(big.getvalue())                      # not an icon: far too large a picture

    served = {"data": icons(3), "urls": []}
    real_get = core.http_get
    core.http_get = lambda url, limit: (served["urls"].append(url), served["data"])[1]
    Path(core.ICONS_F).unlink(missing_ok=True)
    b = pcs_server.Backend()
    srv = pcs_server.Server(b)
    srv.start()
    try:
        assert call(srv, "/api/state")[1]["icons"] is None and b.sync_icons() == "none"
        assert call(srv, "/api/settings", {"team_link": "https://example.com/x"})[0] == 500
        assert core.SETTINGS.get("team_link", "") == ""
        code, st, _ = call(srv, "/api/settings", {"team_link": f"https://drive.google.com/file/d/{fid}/view"})
        assert code == 200 and st["teamLink"].endswith("/view")
        for _ in range(100):                                         # fetched in the background right after saving
            if Path(core.ICONS_F).exists(): break
            time.sleep(0.05)
        assert Path(core.ICONS_F).read_bytes() == served["data"] and fid in served["urls"][0]
        assert call(srv, "/api/state")[1]["icons"] is not None
        assert b.sync_icons() == "same"
        assert b.sync_icons(3600) == "skipped"                       # not asked again within the interval
        served["data"] = icons(5)                                    # the coach replaced the file (new season)
        n = len(b.messages)
        assert b.sync_icons() == "updated" and Path(core.ICONS_F).read_bytes() == served["data"]
        assert any("Weapon icons updated" in m["text"] for m in list(b.messages)[n:])
        served["data"] = b"<html>You need access</html>"             # link no longer public: keep what we have
        assert b.sync_icons() == "error" and core.icons_valid(Path(core.ICONS_F).read_bytes())
    finally:
        core.http_get = real_get
        core.SETTINGS["team_link"] = ""
        Path(core.ICONS_F).unlink(missing_ok=True)
        srv.stop()


def test_update_finisher():
    """The new exe waits for the old app to close, copies itself over it and starts it; if the copy keeps
    failing, the old version is started again (the player is never left without an app)."""
    new, cur = TMP / "PCS-Scanner.update.exe", TMP / "PCS-Scanner.exe"
    new.write_bytes(b"MZ new version")
    cur.write_bytes(b"MZ old version")
    started = []
    real_exe, real_spawn = sys.executable, core.spawn_detached
    sys.executable, core.spawn_detached = str(new), started.append
    try:
        core.finish_update(str(cur), "999999")             # that process doesn't exist: no waiting
    finally:
        sys.executable, core.spawn_detached = real_exe, real_spawn
    # started by an older version (no --show) or from the corner message: back in the tray, saying it updated
    assert cur.read_bytes() == b"MZ new version" and started == [[str(cur), "--background", "--updated"]]
    assert "copied, starting the new version (tray)" in Path(core.UPDATE_LOG).read_text()
    # the player pressed Update in the open window: the window comes back
    started.clear()
    sys.executable, core.spawn_detached = str(new), started.append
    try:
        core.finish_update(str(cur), "999999", show=True)
    finally:
        sys.executable, core.spawn_detached = real_exe, real_spawn
    assert started == [[str(cur)]]
    # copy failed: the old version is started as before, with its window (it doesn't know --updated)
    assert core.relaunch_args(str(cur), False, False) == [str(cur)]


def test_update_relaunch_arguments():
    """The window's Update button restarts with the window; the corner message's button (window not open) in the
    tray. The finisher is started with --show only in the first case."""
    b = pcs_server.Backend(_Auto(str(TMP / "recs-upd2")))
    b.update = ("v9.9.9", "https://x/PCS-Scanner.exe", "https://x", 123)
    got, spawned = [], []
    real_install, real_blocked = core.install_update, b.update_blocked
    core.install_update = lambda url, log, size=None, show=False, digest=None: (got.append(show), False)[1]
    b.update_blocked = lambda: None
    srv = pcs_server.Server(b)
    srv.start()
    try:
        assert call(srv, "/api/update", {})[0] == 200 and got == [True]
        assert b.install_update() is None and got == [True, False]
    finally:
        core.install_update = real_install
        srv.stop()
    # what install_update starts: the downloaded exe with --finish-update, and --show only when asked
    new = TMP / "dl" / "PCS-Scanner.exe"
    new.parent.mkdir(exist_ok=True)
    real = (sys.executable, core.spawn_detached, core.download, getattr(sys, "frozen", None), dict(core.BUILD))
    sys.executable, core.spawn_detached, sys.frozen = str(new), spawned.append, True
    core.BUILD["repo"] = "BLAKYY5/pcs-scanner"
    core.download = lambda url, p: (Path(p).write_bytes(b"MZ" + b"x" * 10), "ab" * 32)[1]
    url = "https://github.com/BLAKYY5/pcs-scanner/releases/download/v9.9.9/PCS-Scanner.exe"
    try:
        assert core.install_update(url, lambda m: None, 12, show=False)
        assert core.install_update(url, lambda m: None, 12, show=True, digest="sha256:" + "ab" * 32)
    finally:
        sys.executable, core.spawn_detached, core.download = real[:3]
        core.BUILD.clear()
        core.BUILD.update(real[4])
        if real[3] is None:
            del sys.frozen
        else:
            sys.frozen = real[3]
    staged = str(new.parent / "PCS-Scanner.update.exe")
    assert spawned[0] == [staged, "--finish-update", str(new), str(os.getpid())], spawned
    assert spawned[1] == [staged, "--finish-update", str(new), str(os.getpid()), "--show"], spawned
    # the app restarted by the updater never closes itself, even with auto-record and start-with-Windows off
    src = (Path(__file__).resolve().parent.parent / "app" / "pcs_app.py").read_text(encoding="utf-8")
    assert "if background and not updated and not" in src and "'--updated' in sys.argv" in src
    assert "show='--show' in sys.argv" in src


def test_update_download_checked():
    """The new exe comes only from this app's GitHub releases over HTTPS, and must match the SHA-256 GitHub lists for
    it: anything else is refused and never started (nothing left next to the app)."""
    folder = TMP / "dl-check"
    folder.mkdir(exist_ok=True)
    cur = folder / "PCS-Scanner.exe"
    staged = Path(core.staged_path(str(cur)))
    real = (sys.executable, core.spawn_detached, core.download, getattr(sys, "frozen", None), dict(core.BUILD))
    spawned, fetched = [], []
    sys.executable, core.spawn_detached, sys.frozen = str(cur), spawned.append, True
    core.BUILD["repo"] = "BLAKYY5/pcs-scanner"
    good = "https://github.com/BLAKYY5/pcs-scanner/releases/download/v9.9.9/PCS-Scanner.exe"
    body = b"MZ" + b"new version" * 10
    import hashlib
    sha = hashlib.sha256(body).hexdigest()

    def fake_download(url, p):
        fetched.append(url)
        Path(p).write_bytes(body)
        return hashlib.sha256(body).hexdigest()
    core.download = fake_download
    try:
        # addresses: only https, only github.com, only this repo's release files
        assert core.update_url_ok(good)
        for bad in ("http://github.com/BLAKYY5/pcs-scanner/releases/download/v9.9.9/PCS-Scanner.exe",
                    "https://github.com.evil.example/BLAKYY5/pcs-scanner/releases/download/v9.9.9/PCS-Scanner.exe",
                    "https://evil.example/BLAKYY5/pcs-scanner/releases/download/v9.9.9/PCS-Scanner.exe",
                    "https://github.com/someone/else/releases/download/v9.9.9/PCS-Scanner.exe",
                    "https://user:pw@github.com/BLAKYY5/pcs-scanner/releases/download/v9.9.9/PCS-Scanner.exe",
                    "", None):
            assert not core.update_url_ok(bad), bad
        # redirects of the download: GitHub's file hosts over https only
        assert core.update_url_ok("https://release-assets.githubusercontent.com/x?y=1", core.UPDATE_REDIRECT_HOSTS)
        assert not core.update_url_ok("http://release-assets.githubusercontent.com/x", core.UPDATE_REDIRECT_HOSTS)
        h = core._HttpsOnlyRedirect()
        try:
            h.redirect_request(urllib.request.Request(good), None, 302, "Found", {}, "http://evil.example/x.exe")
            raise AssertionError("redirect to http accepted")
        except urllib.error.URLError:
            pass
        # a foreign address is refused before anything is downloaded
        try:
            core.install_update("https://evil.example/PCS-Scanner.exe", lambda m: None, len(body), digest="sha256:" + sha)
            raise AssertionError("foreign address accepted")
        except RuntimeError:
            pass
        assert not fetched and not spawned
        # wrong SHA-256: refused, the download removed, nothing started
        try:
            core.install_update(good, lambda m: None, len(body), digest="sha256:" + "0" * 64)
            raise AssertionError("wrong digest accepted")
        except RuntimeError as e:
            assert "doesn't match" in str(e)
        assert not spawned and not staged.exists() and not Path(str(staged) + ".part").exists()
        assert "bad download: SHA-256" in Path(core.UPDATE_LOG).read_text()
        # right SHA-256: started
        assert core.install_update(good, lambda m: None, len(body), digest="SHA256:" + sha.upper())
        assert spawned and spawned[-1][0] == str(staged) and staged.read_bytes() == body
        assert "matches the release" in Path(core.UPDATE_LOG).read_text()
    finally:
        sys.executable, core.spawn_detached, core.download = real[:3]
        core.BUILD.clear()
        core.BUILD.update(real[4])
        if real[3] is None:
            del sys.frozen
        else:
            sys.frozen = real[3]
    # the update check passes GitHub's digest on, and ignores a release file that isn't on this repo's releases
    rel = {"tag_name": "v99.0.0", "html_url": "https://github.com/BLAKYY5/pcs-scanner/releases/tag/v99.0.0",
           "assets": [{"name": "PCS-Scanner.exe", "browser_download_url": good, "size": 5, "digest": "sha256:" + sha}]}
    real_open, real_build = core.urllib.request.urlopen, dict(core.BUILD)

    class _R:
        def __init__(self, d):
            self.d = json.dumps(d).encode()

        def read(self, *a):
            return self.d
    core.urllib.request.urlopen = lambda req, timeout=0: _R(rel)
    core.BUILD.update({"repo": "BLAKYY5/pcs-scanner", "version": "1.0.0"})
    try:
        assert core.check_update() == ("v99.0.0", good, rel["html_url"], 5, "sha256:" + sha)
        rel["assets"][0]["browser_download_url"] = "http://evil.example/PCS-Scanner.exe"
        assert core.check_update() is None
    finally:
        core.urllib.request.urlopen = real_open
        core.BUILD.clear()
        core.BUILD.update(real_build)


def test_sign_in_tokens_encrypted():
    """Google Drive and YouTube sign-ins are kept encrypted for this Windows user; a file in clear from an older
    version is encrypted the first time it is read, and an unreadable file means "sign in again"."""
    f = TMP / "appdata" / "test_token.json"
    info = {"token": "at-123", "refresh_token": "rt-secret-456", "client_id": "cid", "client_secret": "cs",
            "token_uri": "https://oauth2.googleapis.com/token", "scopes": core.SCOPES}
    core.write_token(str(f), json.dumps(info))
    assert core.read_token(str(f)) == info
    if os.name == "nt":
        assert b"rt-secret-456" not in f.read_bytes() and "dpapi" in json.loads(f.read_text())
    # an older version's file in clear: read, then encrypted in place
    f.write_text(json.dumps(info))
    assert core.read_token(str(f)) == info
    if os.name == "nt":
        assert b"rt-secret-456" not in f.read_bytes()
    assert core.read_token(str(f)) == info
    # google-auth reads it back
    creds = core._saved_creds(str(f), core.SCOPES)
    assert creds is not None and creds.refresh_token == "rt-secret-456"
    # unreadable (another user's encryption, damaged): no sign-in, no crash
    f.write_text(json.dumps({"dpapi": "AAAA"}))
    assert core.read_token(str(f)) is None and core._saved_creds(str(f), core.SCOPES) is None
    f.write_text("not json")
    assert core.read_token(str(f)) is None
    assert core.read_token(str(TMP / "appdata" / "missing_token.json")) is None
    # the sign-in code writes through write_token, never in clear
    src = (Path(__file__).resolve().parent.parent / "app" / "pcs_core.py").read_text(encoding="utf-8")
    assert "write_token(TOKEN_F, creds.to_json())" in src and "write_token(YT_TOKEN_F, creds.to_json())" in src
    assert "from_authorized_user_file" not in src and ".write(creds.to_json())" not in src


def test_request_body_limit():
    """The page sends small JSON only: a huge request body is refused before it is read."""
    b = pcs_server.Backend()
    srv = pcs_server.Server(b)
    srv.start()
    try:
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
        c.putrequest("POST", "/api/label")
        c.putheader("X-PCS-Token", srv.token)
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", str(pcs_server.MAX_BODY + 1))
        c.endheaders()
        r = c.getresponse()
        assert r.status == 413, r.status
        c.close()
    finally:
        srv.stop()


def test_show_guard_not_blocked_by_a_silent_client():
    """A program that connects to the single-instance port and sends nothing can't stop "show window" requests."""
    import socket
    import threading
    from pcs_recorder import runtime
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    real, runtime.GUARD_PORT = runtime.GUARD_PORT, port
    shown = threading.Event()
    try:
        s = runtime.hold_guard(shown.set)
        assert s is not None
        silent = socket.create_connection(("127.0.0.1", port), timeout=2)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as c:
                c.sendall(b"show")
            assert shown.wait(6), "show request blocked"
        finally:
            silent.close()
            s.close()
    finally:
        runtime.GUARD_PORT = real


def test_hub_notes_cleaned_for_the_page():
    """Times and colours from the hub go straight into the page's markup: whatever a hub sends, the page gets
    numbers and plain colours there (text is escaped by the page)."""
    from pcs_recorder import notes
    n = notes.clean_note({"id": "an-1", "t": '1"><img src=x onerror=alert(1)>', "dur": "7",
                          "drawings": [{"kind": "pen", "color": "red;background:url(x)", "pts": [[0.1, "0.2"], ["x", 1]]},
                                       {"kind": "line", "color": "#ffd60a", "pts": [[0, 0], [1, 1]]}, "junk"],
                          "text": "<b>kept as text</b>"})
    assert n["t"] == 0.0 and n["dur"] == 7.0 and n["text"] == "<b>kept as text</b>"
    assert [d["color"] for d in n["drawings"]] == ["#ffd60a", "#ffd60a"]
    assert n["drawings"][0]["pts"] == [[0.1, 0.2], [0.0, 1.0]]
    assert notes.num(float("nan")) == 0.0 and notes.num("12.5") == 12.5 and notes.num(None, 3.0) == 3.0


def test_no_password_in_app_log():
    """The OBS library logs its password when it connects: that never reaches app.log, and lines an older version
    wrote are cleaned at start (the file otherwise unchanged; a file held open elsewhere is left for next time)."""
    import logging
    secret = "Zq9-secretPW_4466"
    log_f = Path(core.LOG_F)
    import obsws_python as obsws
    try:
        obsws.ReqClient(host="127.0.0.1", port=1, password=secret, timeout=1)    # nothing listens: fails at once
    except Exception:
        pass
    logging.getLogger("obsws_python.baseclient.ObsClient").warning(
        "Connecting with parameters: host='127.0.0.1' port=4466 password='%s' subs=0 timeout=5", secret)
    logging.getLogger("recorder").info("ReqClient(host='x', port=1, password=%s, timeout=5)", secret)
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_f.read_text(encoding="utf-8", errors="replace")
    assert secret not in text and text.count("password=***") >= 2, text[-600:]
    # an old log
    old = TMP / "old-app.log"
    lines = ["2026-11-15 18:00:01 INFO recorder: Recording started\n",
             f"2026-11-15 18:00:02 INFO obsws_python.baseclient.ObsClient: Connecting with parameters: host='127.0.0.1' "
             f"port=4466 password='{secret}' subs=0 timeout=5\n",
             "2026-11-15 18:00:03 INFO recorder: café → fine\n"]
    old.write_text("".join(lines), encoding="utf-8")
    if os.name == "nt":                                    # held open by another program: left as it is
        with open(old, "a", encoding="utf-8"):
            assert core.scrub_log(str(old)) == 0
        assert secret in old.read_text(encoding="utf-8") and not Path(str(old) + ".scrub").exists()
    assert core.scrub_log(str(old)) == 1
    assert old.read_text(encoding="utf-8") == "".join(lines).replace(f"password='{secret}'", "password=***")
    assert core.scrub_log(str(old)) == 0                   # nothing left to clean
    assert core.scrub_log(str(TMP / "missing.log")) == 0


class Skipped(Exception):
    """A test that cannot run here: the runner reports it as skipped, not failed."""


def _app_already_running():
    """True when another PCS Scanner holds the single-instance mutex (e.g. the installed app on the coach's PC)."""
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenMutexW.restype = ctypes.c_void_p
    h = k32.OpenMutexW(0x00100000, False, "Local\\PCS-Scanner-app")      # SYNCHRONIZE
    if not h:
        return False
    k32.CloseHandle(ctypes.c_void_p(h))
    return True


def test_single_instance():
    """Only one PCS Scanner per Windows session (a second start just shows the first one's window).
    Skipped when a PCS Scanner already runs on this PC (it holds the mutex); CI has none, so it runs there."""
    import pcs_app
    if os.name != "nt":
        raise Skipped("not Windows")
    if _app_already_running():
        raise Skipped("another PCS Scanner is running on this PC (close it to run this test)")
    assert pcs_app.first_instance() is True
    first = pcs_app._MUTEX
    assert pcs_app.first_instance() is False             # a second app in the same session is refused
    pcs_app._MUTEX = first


def test_update_button_and_checks():
    """The Update button appears once a newer release is found; installing waits while Apex/recording/scan runs."""
    from pcs_recorder import core as rcore
    b = pcs_server.Backend(_Auto(str(TMP / "recs-upd")))
    calls = []
    real_check, real_running = core.check_update, rcore.apex_running
    core.check_update = lambda: (calls.append(1), ("v9.9.9", "https://x/PCS-Scanner.exe", "https://x", 123))[1]
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


def test_labels_queue_and_mics_api():
    """Recordings carry a session label that can be changed; the YouTube queue and the microphone list are served."""
    from pcs_recorder import labels, retention, settings as st, youtube
    labels.FILE.unlink(missing_ok=True)
    youtube.STATE_FILE.unlink(missing_ok=True)
    folder = TMP / "lab"
    folder.mkdir(exist_ok=True)
    v = folder / "Apex Legends_lab.mp4"
    v.write_bytes(b"x" * 100)
    os.utime(v, (time.time() - 7200, time.time() - 7200))
    v.with_suffix(".scan").mkdir(exist_ok=True)
    (v.with_suffix(".scan") / (v.stem + ".scan.json")).write_text(json.dumps({"games": [{"map": "SP", "mode": "ranked"}]}))
    auto = _Auto(str(folder))
    auto.rec.idx = {"k": retention.Entry(path=str(v), started=time.time() - 9000, status="scanned")}
    s = st.Settings(folder=str(folder), yt_enabled=True, yt_wait_eec=False)
    auto.rec.youtube = youtube.Uploader(s, auto.rec, None, lambda: None, lambda: False)
    b = pcs_server.Backend(auto)
    srv = pcs_server.Server(b)
    srv.start()
    try:
        _, lib, _ = call(srv, "/api/library")
        it = next(x for x in lib if x["name"] == v.name)
        assert it["label"] == "ranked" and it["label_by_hand"] is False
        code, r, _ = call(srv, "/api/label", {"id": it["id"], "label": "pubs"})
        assert code == 200 and r["label"] == "pubs"
        it = next(x for x in call(srv, "/api/library")[1] if x["name"] == v.name)
        assert it["label"] == "pubs" and it["label_by_hand"] is True
        assert call(srv, "/api/label", {"id": it["id"], "label": "casual"})[0] == 500
        code, q, _ = call(srv, "/api/youtube/queue")
        assert code == 200 and q["available"] and q["blocks"][0]["id"] == "k" and q["blocks"][0]["label"] == "pubs"
        code, r, _ = call(srv, "/api/youtube/block", {"id": "k", "action": "label", "title": "scrims"})
        assert code == 200 and labels.manual(str(v)) == "scrims"
        code, r, _ = call(srv, "/api/youtube/block", {"id": "k", "action": "upload"})
        assert code == 200 and r["status"] == "requested"
        assert call(srv, "/api/youtube/block", {"id": "nope", "action": "skip"})[0] == 500
        code, m, _ = call(srv, "/api/mics")
        assert code == 200 and m["current"] == "default" and m["devices"][0]["id"] == "default"
    finally:
        srv.stop()


# ------------------------------------------------------------------ team hub: key, Test, uploads, Team window
GOOD_KEY = "pcs_" + "A" * 43           # obviously fake keys
BAD_KEY = "pcs_" + "B" * 43


class FakeHub:
    """A hub on localhost. mode "new": keys needed (0.3), "old": no keys, no /api/ping or /api/me (0.2).
    `limit` = answer 429 to everything that needs a key. Every request is kept (method, path, Authorization)."""

    def __init__(self, mode="new"):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        self.mode, self.limit, self.seen, self.bodies = mode, False, [], []
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, headers=None):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                auth = self.headers.get("Authorization", "")
                hub.seen.append((self.command, self.path, auth))
                hub.bodies.append(body)
                p = self.path
                if hub.mode == "old":
                    if p == "/api/status":
                        return self._send(200, {"version": "0.2.0"})
                    if p == "/api/scans":
                        return self._send(200, {"scan_ids": [1]})
                    if p in ("/api/recorder/sessions",):
                        return self._send(200, {"ok": True})
                    if p == "/api/db/scrims":
                        return self._send(200, [])
                    return self._send(404, {"detail": "Not Found"})
                if p == "/api/ping":
                    return self._send(200, {"version": "0.3.0"})
                if hub.limit:
                    return self._send(429, {"detail": "Too many", "error": "too_many_attempts"}, {"Retry-After": "120"})
                if not auth:
                    return self._send(401, {"detail": "A key is needed.", "error": "no_key"})
                if auth != "Bearer " + GOOD_KEY:
                    return self._send(401, {"detail": "This key is not valid.", "error": "bad_key"})
                if p == "/api/me":
                    return self._send(200, {"name": "Tester", "role": "player", "via": "header",
                                            "can_edit_lootbook": True, "coach_only_collections": []})
                if p == "/api/scans":
                    return self._send(200, {"scan_ids": [7, 8], "uploader": "Tester",
                                            "imports": [{"scan_id": 7, "status": "imported", "note": "", "games": 11,
                                                         "new": [], "updated": []}]})
                if p == "/api/recorder/sessions":
                    return self._send(200, {"ok": True})
                if p == "/api/db/scrims":
                    return self._send(200, [])
                return self._send(404, {"detail": "Not Found"})

            do_GET = do_POST = _go

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _fresh_hub():
    from pcs_recorder import hub
    hub.set_key(None)
    hub.reset()
    return hub


def test_hub_key_link_parsing():
    from pcs_recorder import hub
    assert hub.parse_key(GOOD_KEY) == (GOOD_KEY, None)
    assert hub.parse_key("  " + GOOD_KEY + "\n") == (GOOD_KEY, None)
    assert hub.parse_key(f"http://100.64.1.2:8420/login#key={GOOD_KEY}") == (GOOD_KEY, "http://100.64.1.2:8420")
    assert hub.parse_key(f"https://pcs-hub.example.ts.net/login#key={GOOD_KEY}&x=1") == (
        GOOD_KEY, "https://pcs-hub.example.ts.net")
    assert hub.parse_key(f"http://pcs-hub:8420/login#key={GOOD_KEY.replace('_', '%5F')}")[0] == GOOD_KEY
    for bad in ("", "pcs_short", GOOD_KEY + "X", f"http://h/login?key={GOOD_KEY}",      # a key in the query: refused
                f"ftp://h/login#key={GOOD_KEY}", f"http://user@h/login#key={GOOD_KEY}", "hello"):
        assert hub.parse_key(bad) == (None, None), bad
    assert hub.mask(GOOD_KEY) == "pcs_…AAAA" and hub.mask(None) == ""
    assert hub.summary({"scan_ids": [1]}) == "Sent to hub"                                       # old hub
    assert hub.summary({"imports": [{"status": "imported", "games": 11}]}) == "Sent to hub: 11 games"
    assert hub.summary({"imports": [{"status": "imported", "games": 1}]}) == "Sent to hub: 1 game"
    assert hub.summary({"imports": [{"status": "failed", "note": "unknown field\nmore"}]}) == \
        "Sent to hub, not imported: unknown field"
    assert hub.summary({"imports": []}) == "Sent to hub"                                        # a feed alone


def test_hub_key_storage():
    """The key file is encrypted for this Windows user: the key's text is not in it; it reads back after a restart."""
    hub = _fresh_hub()
    hub.set_key(GOOD_KEY)
    raw = hub.KEY_FILE.read_bytes()
    assert GOOD_KEY.encode() not in raw and b"A" * 20 not in raw, raw[:80]
    hub.S.loaded = False                                        # as after a restart
    assert hub.get_key() == GOOD_KEY
    try:
        hub.set_key("pcs_nope")
        raise AssertionError("bad key saved")
    except ValueError as e:
        assert "pcs_nope" not in str(e)
    hub.set_key(None)
    assert not hub.KEY_FILE.exists() and hub.get_key() is None


def test_hub_settings_test_button_and_errors():
    """Settings → Team hub: link pasted (key + address), key masked everywhere, Test button against a hub that
    wants keys (good / wrong / no key / too many tries) and an old hub without keys."""
    from pcs_recorder import settings as st
    hub = _fresh_hub()
    fake = FakeHub("new")
    b = pcs_server.Backend(_Auto(str(TMP / "hubrecs")))
    st_save = st.save
    st.save = lambda s: None
    srv = pcs_server.Server(b)
    srv.start()
    try:
        code, state, _ = call(srv, "/api/state")
        assert state["hub"]["url"] == "" and state["hub"]["key"] == ""             # no hub: nothing (no Team tab)
        # a wrong paste: refused, nothing saved, the text pasted is not repeated
        code, r, _ = call(srv, "/api/settings", {"hub_key": "pcs_12345 not a key"})
        assert code == 500 and "12345" not in r["error"] and hub.get_key() is None
        # the coach's sign-in link: key and (empty) address both taken from it
        code, raw, _ = call(srv, "/api/settings", {"hub_url": "", "hub_key": f"{fake.url}/login#key={GOOD_KEY}"}, raw=True)
        assert code == 200 and GOOD_KEY.encode() not in raw
        state = json.loads(raw)
        assert b.auto.s.hub_url == fake.url and state["hub"] == {"url": fake.url, "key": "pcs_…AAAA", "refused": False,
                                                                 "wait": 0, "last": {}}
        assert hub.get_key() == GOOD_KEY
        # an empty key field = unchanged
        call(srv, "/api/settings", {"hub_key": "", "keep_days": 5})
        assert hub.get_key() == GOOD_KEY
        # Test: connected
        code, raw, _ = call(srv, "/api/hub/test", {}, raw=True)
        assert GOOD_KEY.encode() not in raw
        r = json.loads(raw)
        assert r["ok"] and r["text"] == "Connected as Tester (player)" and r["hub"]["last"]["text"] == r["text"], r
        assert all(a in ("", "Bearer " + GOOD_KEY) for _, _, a in fake.seen)
        assert all(GOOD_KEY not in p for _, p, _ in fake.seen)                          # never in an address
        assert ("GET", "/api/me", "Bearer " + GOOD_KEY) in fake.seen
        # the state page never carries the key
        assert GOOD_KEY.encode() not in call(srv, "/api/state", raw=True)[1]
        # a wrong key: "Key refused", and the hub is marked so
        call(srv, "/api/settings", {"hub_key": BAD_KEY})
        r = call(srv, "/api/hub/test", {})[1]
        assert not r["ok"] and r["text"] == "Key refused" and r["hub"]["refused"] is True, r
        # no key at all
        call(srv, "/api/settings", {"hub_key": None})
        r = call(srv, "/api/hub/test", {})[1]
        assert not r["ok"] and r["text"].startswith("Key needed"), r
        # too many wrong keys: wait
        call(srv, "/api/settings", {"hub_key": GOOD_KEY})
        fake.limit = True
        r = call(srv, "/api/hub/test", {})[1]
        assert not r["ok"] and "wait" in r["text"] and r["hub"]["wait"] > 60, r
        n = len(fake.seen)
        try:
            hub.report(fake.url, {"id": "x"})
            raise AssertionError("sent while waiting")
        except hub.HubError as e:
            assert e.kind == "wait" and len(fake.seen) == n                     # nothing sent before Retry-After
        fake.limit = False
        hub.set_key(GOOD_KEY)                                                   # a new key clears the wait
        # hub not reachable
        b.auto.s.hub_url = "http://127.0.0.1:9"
        assert call(srv, "/api/hub/test", {})[1]["text"] == "Hub not reachable"
        # an old hub (no keys): works with or without a key
        old = FakeHub("old")
        try:
            b.auto.s.hub_url = old.url
            for k in (GOOD_KEY, None):
                hub.set_key(k)
                r = call(srv, "/api/hub/test", {})[1]
                assert r["ok"] and r["text"] == "Hub is an older version: no key needed", r
        finally:
            old.stop()
    finally:
        srv.stop()
        fake.stop()
        st.save = st_save
        _fresh_hub()


def test_hub_join_from_setup():
    """Home → Get set up → Join the team hub: one paste of the coach's link saves the address (replacing an old one)
    and the key, and tests at once. Wrong pastes are refused without repeating them; the key is never answered back
    nor written to the log; an unreachable Tailscale hub says what to do."""
    from pcs_recorder import settings as st
    hub = _fresh_hub()
    fake = FakeHub("new")
    b = pcs_server.Backend(_Auto(str(TMP / "joinrecs")))
    b.auto.s.hub_url = "http://old-hub:8420"                       # an old address: the link's address wins
    st_save = st.save
    st.save = lambda s: None
    srv = pcs_server.Server(b)
    srv.start()
    try:
        code, r, _ = call(srv, "/api/hub/join", {"link": "pcs_12345 not a link"})
        assert code == 500 and "12345" not in r["error"] and hub.get_key() is None, r
        code, raw, _ = call(srv, "/api/hub/join", {"link": f"{fake.url}/login#key={GOOD_KEY}"}, raw=True)
        assert code == 200 and GOOD_KEY.encode() not in raw, raw
        r = json.loads(raw)
        assert r["ok"] and r["text"] == "Connected as Tester (player)" and "tailscale" not in r, r
        assert b.auto.s.hub_url == fake.url and hub.get_key() == GOOD_KEY
        assert r["hub"]["key"] == "pcs_…AAAA" and r["hub"]["last"]["name"] == "Tester"
        assert all(GOOD_KEY not in p for _, p, _ in fake.seen)                          # never in an address
        # a key alone works once an address is set
        code, r, _ = call(srv, "/api/hub/join", {"link": GOOD_KEY})
        assert code == 200 and r["ok"], r
        # a key alone with no address: asks for the whole link
        b.auto.s.hub_url = ""
        hub.set_key(None)
        code, r, _ = call(srv, "/api/hub/join", {"link": GOOD_KEY})
        assert code == 500 and "whole link" in r["error"] and GOOD_KEY not in r["error"] and hub.get_key() is None, r
        # a Tailscale address that doesn't answer: the answer says why (Tailscale missing / off / on)
        code, r, _ = call(srv, "/api/hub/join", {"link": f"http://100.127.255.254:9/login#key={GOOD_KEY}"})
        assert code == 200 and not r["ok"] and r["tailscale"] in ("missing", "off", "on"), r
        # a LAN address that doesn't answer: no Tailscale advice
        code, r, _ = call(srv, "/api/hub/join", {"link": f"http://127.0.0.1:9/login#key={GOOD_KEY}"})
        assert code == 200 and not r["ok"] and r["text"] == "Hub not reachable" and r["tailscale"] == "", r
        assert GOOD_KEY not in Path(core.LOG_F).read_text(encoding="utf-8", errors="replace")
    finally:
        srv.stop()
        fake.stop()
        st.save = st_save
        _fresh_hub()


def test_tailscale_state():
    """Tailscale advice is looked up on this PC only: installed? connected (a 100.64.0.0/10 address)?"""
    import psutil
    from types import SimpleNamespace as NS
    pf = TMP / "pf"
    old_env = {k: os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")}
    old_addrs = psutil.net_if_addrs
    try:
        for k in old_env:
            os.environ[k] = str(pf)
        assert pcs_server.tailscale_state("http://172.22.22.75:8420") == ""             # LAN: not Tailscale
        assert pcs_server.tailscale_state("http://pcs-hub:8420") == ""
        assert pcs_server.tailscale_state("http://100.74.165.47:8420") == "missing"
        assert pcs_server.tailscale_state("https://hub.tail1234.ts.net") == "missing"
        (pf / "Tailscale").mkdir(parents=True, exist_ok=True)
        (pf / "Tailscale" / "tailscale.exe").write_bytes(b"")
        psutil.net_if_addrs = lambda: {"Ethernet": [NS(address="192.168.1.20")], "lo": [NS(address="127.0.0.1")]}
        assert pcs_server.tailscale_state("http://100.74.165.47:8420") == "off"
        psutil.net_if_addrs = lambda: {"Ethernet": [NS(address="192.168.1.20")], "Tailscale": [NS(address="100.103.1.2")]}
        assert pcs_server.tailscale_state("http://100.74.165.47:8420") == "on"
    finally:
        psutil.net_if_addrs = old_addrs
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_setup_card_page():
    """Home's "Get set up" card: the four steps in order (auto-record second, so its setup runs while the player does
    the rest), the join call, and the consent line: comms are never switched on from the card."""
    js = (Path(pcs_server.WEB) / "app.js").read_text(encoding="utf-8")
    setup = js[js.index("function setupHTML"):js.index("function wireSetup")]
    order = [setup.index(t) for t in ("Your EEC name", "Record Apex by itself", "Join the team hub", "Google Drive")]
    assert order == sorted(order), order
    assert "/api/hub/join" in js and "comms_consent" not in setup and "not recorded until you turn them on" in setup
    b = pcs_server.Backend(_Auto(str(TMP / "setuprecs")))
    assert b.state()["recorder"]["settingUp"] is False
    b.auto._setting_up = True
    assert b.state()["recorder"]["settingUp"] is True


def test_hub_uploads_refused_key_and_old_hub():
    """Scan + feed go in one request with the key; 401 stops hub uploads until the key changes; an old hub
    keeps working; the answer is summed up for the player."""
    from pcs_recorder import runtime
    hub = _fresh_hub()
    fake = FakeHub("new")
    folder = TMP / "hubup"
    folder.mkdir(exist_ok=True)
    files = [folder / "rec.scan.json", folder / "rec.feed.json"]
    for f in files:
        f.write_text("{}")
    s = _S(str(folder))
    s.hub_url, s.player = fake.url, "Tester"
    drive = {"ok": False}
    sender = runtime.Sender(s, lambda paths: drive["ok"])
    try:
        hub.set_key(GOOD_KEY)
        where = sender.upload(files)
        assert where == ["the team hub"] and where.hub_note == "Sent to hub: 11 games", (where, where.hub_note)
        ups = [(m, p, a) for m, p, a in fake.seen if p == "/api/scans"]
        assert len(ups) == 1 and ups[0][2] == "Bearer " + GOOD_KEY                       # one request, with the key
        body = fake.bodies[fake.seen.index(ups[0])]
        assert b"rec.scan.json" in body and b"rec.feed.json" in body
        sender.report({"id": "Tester-rec-1", "status": "uploaded"})
        assert fake.seen[-1] == ("POST", "/api/recorder/sessions", "Bearer " + GOOD_KEY)
        from pcs_recorder import eec
        assert eec.from_hub(fake.url, "PCS", 0, 1) is None and fake.seen[-1][1] == "/api/db/scrims"
        # wrong key: refused once, then no more requests until the key changes
        hub.set_key(BAD_KEY)
        for _ in range(3):
            try:
                sender.upload(files)
                raise AssertionError("upload succeeded with a refused key")
            except ConnectionError as e:
                assert "refused your key" in str(e) and BAD_KEY not in str(e)
        assert len([1 for _, p, a in fake.seen if a == "Bearer " + BAD_KEY]) == 1          # tried once only
        # Drive still takes the results; the hub's refusal is noted
        drive["ok"] = True
        where = sender.upload(files)
        assert where == ["Google Drive"] and where.hub_note == "Not sent to hub: key refused"
        drive["ok"] = False
        hub.set_key(GOOD_KEY)                                                            # new key: sent again
        assert sender.upload(files) == ["the team hub"]
        # an old hub: same upload, no import answer
        old = FakeHub("old")
        try:
            s.hub_url = old.url
            for k in (GOOD_KEY, None):
                hub.set_key(k)
                where = sender.upload(files)
                assert where == ["the team hub"] and where.hub_note == "Sent to hub"
        finally:
            old.stop()
        # no hub set: nothing goes to a hub
        s.hub_url = ""
        n = len(fake.seen)
        drive["ok"] = True
        assert sender.upload(files) == ["Google Drive"] and len(fake.seen) == n
    finally:
        fake.stop()
        _fresh_hub()


def test_team_window_signed_in():
    """Team tab: the hub's pages open in a second app window, signed in through the hub's /login#key=… page
    (the key after '#', never sent to a server); an old hub opens the page itself."""
    hub = _fresh_hub()
    fake = FakeHub("new")
    auto = _Auto(str(TMP / "team"))
    auto.s.hub_url = fake.url
    b = pcs_server.Backend(auto)
    opened = []
    b.open_window = lambda url, title: opened.append(url) or True
    srv = pcs_server.Server(b)
    srv.start()
    try:
        hub.set_key(GOOD_KEY)
        code, raw, _ = call(srv, "/api/hub/open", {"path": "/lootbook/"}, raw=True)
        assert code == 200 and json.loads(raw) == {"window": True} and GOOD_KEY.encode() not in raw
        url = opened[-1]
        before, _, frag = url.partition("#")
        assert before == f"{fake.url}/login?next=/lootbook/" and frag == "key=" + GOOD_KEY, url
        call(srv, "/api/hub/open", {"path": "/#/scanner"})
        assert opened[-1].startswith(f"{fake.url}/login?next=/%23/scanner#key=")
        call(srv, "/api/hub/open", {"path": "//evil.example/"})
        assert opened[-1].startswith(f"{fake.url}/login?next=/#key=")                    # only the hub's own pages
        hub.set_key(None)
        call(srv, "/api/hub/open", {"path": "/lootbook/"})
        assert opened[-1] == f"{fake.url}/lootbook/"
        old = FakeHub("old")
        try:
            auto.s.hub_url = old.url
            hub.set_key(GOOD_KEY)
            call(srv, "/api/hub/open", {"path": "/lootbook/"})
            assert opened[-1] == f"{old.url}/lootbook/"                                  # old hub: no sign-in page
        finally:
            old.stop()
        auto.s.hub_url = ""
        assert call(srv, "/api/hub/open", {"path": "/"})[0] == 500
    finally:
        srv.stop()
        fake.stop()
        _fresh_hub()


PLAYER_KEY = "pcs_" + "C" * 43


class NotesHub:
    """A hub 0.4 with coach notes, on localhost: GOOD_KEY = the coach (Blakey), PLAYER_KEY = the player Moe, who
    recorded and sent the scan of `REC`. Same answers and rules as the real hub (AGENT.md), kept small."""
    REC = "Apex Legends_2026.11.07-21.42"

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlsplit
        import threading
        self.notes, self.seen, self.calls = {}, {}, []
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else {}
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                auth = self.headers.get("Authorization", "")
                hub.calls.append((self.command, u.path, q, body, auth))
                me = {"Bearer " + GOOD_KEY: ("Blakey", "coach"), "Bearer " + PLAYER_KEY: ("Moe", "player")}.get(auth)
                if not me:
                    return self._send(401, {"detail": "bad key", "error": "bad_key"})
                coach, p = me[1] == "coach", u.path
                if p == "/api/annotations" and self.command == "GET":
                    want = q.get("player") or ("Moe" if coach else me[0])
                    if not coach and want != me[0]:
                        return self._send(403, {"detail": "no", "error": "forbidden"})
                    known = q.get("recording") == hub.REC and want == "Moe"
                    notes = [dict(x, seen=me[0] in hub.seen.get(x["id"], []), **({"seenBy": {s: "t" for s in hub.seen.get(x["id"], [])}} if coach else {}))
                             for x in hub.notes.values() if known]
                    return self._send(200, {"recording": q.get("recording"), "player": want, "known": known,
                                            "players": ["Moe"] if coach and known else [],
                                            "me": {"name": me[0], "role": me[1]}, "notes": notes})
                if p.endswith("/seen"):
                    hub.seen.setdefault(p.split("/")[3], []).append(me[0])
                    return self._send(200, {"seen": True})
                if not coach:
                    return self._send(403, {"detail": "Your key can't do this. Coach only.", "error": "forbidden"})
                if p == "/api/annotations" and self.command == "POST":
                    if body.get("recording") != hub.REC or body.get("player") != "Moe":
                        return self._send(404, {"detail": "This recording isn't on the hub", "error": "unknown_recording"})
                    nid = f"an-{len(hub.notes) + 1}"
                    hub.notes[nid] = dict(body, id=nid, version=1, by="Blakey")
                    return self._send(200, hub.notes[nid])
                nid = p.split("/")[-1]
                if nid not in hub.notes:
                    return self._send(404, {"detail": "No such note.", "error": "not_found"})
                if self.command == "PUT":
                    if q.get("if_version") and int(q["if_version"]) != hub.notes[nid]["version"]:
                        return self._send(409, {"detail": "The note was changed meanwhile: reload it.", "error": "version_conflict"})
                    hub.notes[nid].update(body, version=hub.notes[nid]["version"] + 1)
                    return self._send(200, hub.notes[nid])
                if self.command == "DELETE":
                    del hub.notes[nid]
                    return self._send(200, {"deleted": True})
                return self._send(404, {"detail": "Not Found"})

            do_GET = do_POST = do_PUT = do_DELETE = _go

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_coach_notes():
    """Review player notes: the coach writes them through the app (hub key with the coach role), the player reads
    them; offline the last ones fetched still show; no hub / no key / a hub without notes: nothing."""
    hub = _fresh_hub()
    fake, old = NotesHub(), FakeHub()
    folder = TMP / "notes"
    folder.mkdir(exist_ok=True)
    rec = folder / (NotesHub.REC + ".mp4")
    rec.write_bytes(b"x" * 100)
    other = folder / "Someone else.mkv"
    other.write_bytes(b"y" * 100)
    auto = _Auto(str(TMP / "notesrecs"))
    auto.s.hub_url = fake.url
    b = pcs_server.Backend(auto)
    b.add_paths([str(rec), str(other)])
    ids = {x["name"]: x["id"] for x in b.library()}
    vid, vother = ids[rec.name], ids[other.name]
    srv = pcs_server.Server(b)
    srv.start()
    pen = {"kind": "pen", "color": "#ff3b30", "pts": [[0.1, 0.2], [0.3, 0.4]]}
    try:
        # no key: nothing, and nothing asked of the hub
        r = call(srv, f"/api/library/{vid}/notes")[1]
        assert r == {"available": False, "notes": []} and not fake.calls
        # the coach: sees whose recording it is, writes a note
        hub.set_key(GOOD_KEY)
        r = call(srv, f"/api/library/{vid}/notes")[1]
        assert r["available"] and not r["offline"] and r["me"]["role"] == "coach" and r["players"] == ["Moe"], r
        assert fake.calls[-1][2] == {"recording": NotesHub.REC} and fake.calls[-1][4] == "Bearer " + GOOD_KEY
        assert GOOD_KEY not in fake.calls[-1][1] and not any(GOOD_KEY in str(v) for v in fake.calls[-1][2].values())
        code, r, _ = call(srv, "/api/notes/save", {"id": vid, "player": "Moe", "note": {
            "t": 61.5, "dur": 5, "text": "Rotate earlier", "scope": "squad", "drawings": [pen], "junk": 1}})
        assert code == 200 and [n["text"] for n in r["notes"]] == ["Rotate earlier"], r
        sent = fake.calls[-2]
        assert sent[0] == "POST" and sent[3] == {"recording": NotesHub.REC, "player": "Moe", "t": 61.5, "dur": 5,
                                                 "text": "Rotate earlier", "scope": "squad", "drawings": [pen]}, sent
        n = r["notes"][0]
        r = call(srv, "/api/notes/save", {"id": vid, "player": "Moe", "note": {**n, "text": "Rotate now"}})[1]
        assert r["notes"][0]["text"] == "Rotate now" and r["notes"][0]["version"] == 2
        assert fake.calls[-2][0] == "PUT" and fake.calls[-2][2] == {"if_version": "1"}
        code, r, _ = call(srv, "/api/notes/save", {"id": vid, "player": "Moe", "note": {**n, "text": "stale"}})
        assert code == 500 and "409" in r["error"] and "reload" in r["error"], r                # someone changed it
        # a recording the hub doesn't know: said so, and saving is refused with the hub's words
        r = call(srv, f"/api/library/{vother}/notes")[1]
        assert r["available"] and r["known"] is False and fake.calls[-1][2] == {"recording": "Someone else"}
        code, r, _ = call(srv, "/api/notes/save", {"id": vother, "player": "Moe", "note": {"t": 1, "text": "x"}})
        assert code == 500 and "isn't on the hub" in r["error"]
        # the player: reads the note (with the drawing), can't write, marks it seen
        hub.set_key(PLAYER_KEY)
        r = call(srv, f"/api/library/{vid}/notes")[1]
        assert r["me"]["role"] == "player" and r["notes"][0]["drawings"] == [pen] and r["notes"][0]["seen"] is False
        code, r, _ = call(srv, "/api/notes/save", {"id": vid, "note": {"t": 1, "text": "mine"}})
        assert code == 500 and "403" in r["error"] and len(fake.notes) == 1
        assert call(srv, "/api/notes/seen", {"id": vid, "note_ids": [n["id"], "../../evil"]})[0] == 500   # bad id refused
        assert fake.seen == {n["id"]: ["Moe"]}
        # offline: the last answer for this recording, saved in the data folder
        fake.stop()
        r = call(srv, f"/api/library/{vid}/notes")[1]
        assert r["available"] and r["offline"] and r["notes"][0]["text"] == "Rotate now", r
        assert list((Path(os.environ["PCS_DATA_DIR"]) / "notes-cache").glob("*.json"))
        # a hub from before notes (0.3: no /api/annotations): nothing changes in the review
        auto.s.hub_url = old.url
        hub.set_key(GOOD_KEY)
        assert call(srv, f"/api/library/{vid}/notes")[1] == {"available": False, "notes": []}
        # no hub address: nothing
        auto.s.hub_url = ""
        assert call(srv, f"/api/library/{vid}/notes")[1]["available"] is False
        assert call(srv, "/api/notes/save", {"id": vid, "note": {}})[0] == 500
        assert call(srv, "/api/library/000000000000/notes")[0] == 500
    finally:
        srv.stop()
        old.stop()
        _fresh_hub()
        core.SETTINGS["library"] = [p for p in core.SETTINGS.get("library", []) if not p.startswith(str(folder))]


def test_coach_notes_delete():
    hub = _fresh_hub()
    fake = NotesHub()
    rec = TMP / "notes2" / (NotesHub.REC + ".mp4")
    rec.parent.mkdir(exist_ok=True)
    rec.write_bytes(b"x")
    auto = _Auto(str(TMP / "notesrecs2"))
    auto.s.hub_url = fake.url
    b = pcs_server.Backend(auto)
    b.add_paths([str(rec)])
    vid = next(x["id"] for x in b.library() if x["path"] == str(rec))
    try:
        hub.set_key(GOOD_KEY)
        r = b.note_change(vid, "save", {"player": "Moe", "note": {"t": 3, "text": "a"}})
        assert len(r["notes"]) == 1
        r = b.note_change(vid, "delete", {"player": "Moe", "note_id": r["notes"][0]["id"]})
        assert r["notes"] == [] and fake.calls[-2][0] == "DELETE"
    finally:
        fake.stop()
        _fresh_hub()
        core.SETTINGS["library"] = [p for p in core.SETTINGS.get("library", []) if p != str(rec)]


def test_hub_key_never_in_logs_or_data_files():
    """After everything above: the key's text is in no file of the data folder (app.log, recorder.json…)."""
    import logging
    for h in logging.getLogger().handlers:
        h.flush()
    hits = []
    for p in Path(os.environ["PCS_DATA_DIR"]).rglob("*"):
        if p.is_file():
            data = p.read_bytes()
            if GOOD_KEY.encode() in data or BAD_KEY.encode() in data:
                hits.append(p.name)
    assert not hits, hits


def test_hub_retry_with_real_hub_requests():
    """Recorder + Sender + hub.py against a hub on localhost: Drive takes the results while the hub is not
    reachable, then a refused key; the Recordings list says "Not on hub yet" and why; once the hub is reachable
    and the key is fixed, the recording goes to the hub by itself, once."""
    from pcs_recorder import retention, runtime, settings as st
    from pcs_recorder.core import Recorder
    hub = _fresh_hub()
    fake = FakeHub("new")
    folder = TMP / "hubretry"
    folder.mkdir(exist_ok=True)
    v = folder / "Apex Legends_retry.mp4"
    v.write_bytes(DATA)
    out = v.with_suffix(".scan")
    out.mkdir(exist_ok=True)
    (out / (v.stem + ".scan.json")).write_text('{"games": [{"map": "SP"}]}')
    (out / (v.stem + ".feed.json")).write_text("{}")
    retention.SENT_FILE.unlink(missing_ok=True)
    s = st.Settings(folder=str(folder), player="Tester", hub_url="http://127.0.0.1:9")      # nothing listens there
    notes = []

    class UI:
        def notify(self, t, m, sticky=False, action=None):
            notes.append((t, m))
    sender = runtime.Sender(s, lambda paths: True)                                      # Drive works
    rec = Recorder(s, None, sender, UI(), None, is_apex_running=lambda: False)
    rec.idx = {"k": retention.Entry(path=str(v), started=time.time(), status="scanned")}

    class Auto:
        pass
    auto = Auto()
    auto.s, auto.rec, auto.sender = s, rec, sender
    b = pcs_server.Backend(auto)
    srv = pcs_server.Server(b)
    srv.start()
    try:
        hub.set_key(BAD_KEY)
        rec._retry_uploads()
        assert rec.idx["k"].status == "uploaded" and rec.hub_state("k") == {"hub": "pending", "why": "Hub not reachable"}
        it = next(x for x in call(srv, "/api/library")[1] if x["name"] == v.name)
        assert it["hub"] == {"hub": "pending", "why": "Hub not reachable"}, it
        rec._retry_hub()                                                               # still down
        assert rec.hub_state("k")["why"] == "Hub not reachable" and rec.hub_next > time.time()
        s.hub_url = fake.url                                                           # hub reachable (new address)
        rec._retry_hub()
        assert rec.hub_state("k") == {"hub": "pending", "why": "Key refused"}
        n = len(fake.seen)
        for _ in range(3):
            rec._retry_hub()
        assert len(fake.seen) == n                                                     # nothing asked with that key
        hub.set_key(GOOD_KEY)                                                          # key fixed: goes out by itself
        rec._retry_hub()
        assert rec.hub_state("k") == {"hub": "sent", "why": ""}
        ups = [a for m, p, a in fake.seen if p == "/api/scans" and a == "Bearer " + GOOD_KEY]
        assert len(ups) == 1 and rec.idx["k"].note == "Sent to hub: 11 games"
        assert ("Sent to hub", "1 game: Storm Point: now on the team hub.") in notes, notes
        it = next(x for x in call(srv, "/api/library")[1] if x["name"] == v.name)
        assert it["hub"]["hub"] == "sent" and it["note"] == "Sent to hub: 11 games"
        rec._retry_hub(); rec._retry_uploads()
        assert len([1 for m, p, a in fake.seen if p == "/api/scans"]) == 2             # 1 refused + 1 taken: no more
    finally:
        srv.stop()
        fake.stop()
        _fresh_hub()


class SessionHub:
    """A hub 0.5 with Watch together, on localhost, kept small: GOOD_KEY = the coach (Blakey), PLAYER_KEY = Moe.
    HTTP as the real hub (AGENT.md) and a hand-made websocket (handshake + text frames) for the pushes."""

    def __init__(self, clock_ahead=5.0):
        import base64
        import hashlib
        import socket
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import urlsplit
        self.calls, self.ws_auth, self.socks, self.reports = [], [], [], []
        self.audio_gets, self.uploads, self.upload_code = [], [], 200
        self.ahead, self.state = clock_ahead, None
        hub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                multipart = "multipart/form-data" in self.headers.get("Content-Type", "")
                body = {"_multipart": raw} if multipart else (json.loads(raw) if raw else {})
                p = urlsplit(self.path).path
                auth = self.headers.get("Authorization", "")
                hub.calls.append((self.command, self.path, body, auth))
                me = {"Bearer " + GOOD_KEY: ("Blakey", "coach"), "Bearer " + PLAYER_KEY: ("Moe", "player")}.get(auth)
                if not me:
                    return self._send(401, {"detail": "bad key", "error": "bad_key"})
                if p.endswith("/ws") and self.headers.get("Upgrade", "").lower() == "websocket":
                    hub.ws_auth.append((self.path, auth))
                    acc = base64.b64encode(hashlib.sha1((self.headers["Sec-WebSocket-Key"] +
                                                         "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", acc)
                    self.end_headers()
                    self.wfile.flush()
                    sock = self.connection
                    hub.socks.append(sock)
                    hub.push({"type": "state", "state": hub.state, "me": {"name": me[0], "role": me[1]}}, [sock])
                    try:
                        while True:
                            m = hub.read_frame(sock)
                            if m is None:
                                break
                            m = json.loads(m)
                            if m["type"] == "ping":
                                hub.push({"type": "pong", "c": m["c"], "s": m["c"] + hub.ahead}, [sock])
                            elif m["type"] == "report":
                                hub.reports.append((me[0], m))
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                coach = me[1] == "coach"
                if p == "/api/me":
                    return self._send(200, {"name": me[0], "role": me[1]})
                if p == "/api/sessions" and self.command == "GET":
                    return self._send(200, {"sessions": [{"id": "s-1", "coach": "Blakey", "game": {"map": "SP"},
                                                          "members": ["Blakey"], "canJoin": True}] if hub.state else []})
                if p == "/api/videos":
                    return self._send(200, {"games": [{"gameKey": "g-1", "map": "SP", "players": ["Moe"], "videos": ["Moe"]}]})
                if p == "/api/sessions" and self.command == "POST":
                    if not coach:
                        return self._send(403, {"detail": "Your key can't do this. Coach only.", "error": "forbidden"})
                    hub.state = {"id": "s-1", "coach": "Blakey", "gameKey": body["gameKey"], "povs": [], "focus": "Moe",
                                 "playing": False, "t": 0, "at": 0, "speed": 1, "strokes": [], "members": {}, "rev": 0,
                                 "range": [-150, 600]}
                    return self._send(200, hub.state)
                if p == "/api/sessions/s-1" and self.command == "GET":
                    return self._send(200, hub.state)
                if p == "/api/sessions/s-1/join":
                    return self._send(200, hub.state)
                if p == "/api/sessions/s-1/leave":
                    return self._send(200, {"left": True})
                if p == "/api/sessions/s-1/flag":
                    return self._send(200, {"id": "f-1", "player": me[0], "t": body.get("t")})
                if p == "/api/review-extras":
                    return self._send(200, {"recording": "x", "player": me[0], "games": [
                        {"gameKey": "g-1", "jumpAt": 200.0, "bookmarks": [{"t": 75.0, "text": "Rotate here"}],
                         "talk": [{"id": "ta-0123456789ab", "track": "mic", "segments": [
                             {"a0": 0, "a1": 10, "g0": 70, "g1": 80, "speed": 1, "gameKey": "g-1"}]}]}]})
                if p.startswith("/api/session-audio/"):
                    data = b"TALK" * 1000
                    hub.audio_gets.append(p)
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/mp4")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if not coach:
                    return self._send(403, {"detail": "Your key can't do this. Coach only.", "error": "forbidden"})
                if p == "/api/sessions/s-1/control":
                    hub.state = dict(hub.state, playing=body["action"] == "play", t=body.get("t", 0), rev=hub.state["rev"] + 1)
                    hub.push({"type": "state", "state": hub.state})
                    return self._send(200, hub.state)
                if p == "/api/sessions/s-1/draw":
                    hub.state = dict(hub.state, strokes=hub.state["strokes"] + [body["stroke"]], rev=hub.state["rev"] + 1)
                    hub.push({"type": "stroke", "op": "put", "stroke": body["stroke"], "rev": hub.state["rev"]})
                    return self._send(200, {"rev": hub.state["rev"]})
                if p == "/api/sessions/s-1/end":
                    hub.push({"type": "ended", "reason": "ended"})
                    hub.state = None
                    return self._send(200, {"ended": True})
                if p == "/api/sessions/s-1/pointer":
                    hub.push({"type": "pointer", **body})
                    return self._send(200, {"ok": True})
                if p == "/api/sessions/s-1/bookmark":
                    return self._send(200, {"id": "b-1", "t": body.get("t"), "text": body.get("text")})
                if p == "/api/session-audio" and self.command == "POST":
                    hub.uploads.append(body["_multipart"])
                    return self._send(hub.upload_code, {"id": "ta-1"})
                return self._send(404, {"detail": "Not Found"})

            do_GET = do_POST = _go

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self._socket = socket
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @staticmethod
    def read_frame(sock):
        def exact(n):
            b = b""
            while len(b) < n:
                c = sock.recv(n - len(b))
                if not c:
                    raise OSError("closed")
                b += c
            return b
        b0, b1 = exact(2)
        n = b1 & 0x7F
        if n == 126:
            n = int.from_bytes(exact(2), "big")
        elif n == 127:
            n = int.from_bytes(exact(8), "big")
        mask = exact(4) if b1 & 0x80 else b"\0\0\0\0"
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(exact(n)))
        return None if b0 & 0x0F == 8 else data.decode()

    def push(self, msg, socks=None):
        data = json.dumps(msg).encode()
        n = len(data)
        head = bytes([0x81, n]) if n < 126 else bytes([0x81, 126]) + n.to_bytes(2, "big") if n < 65536 else \
            bytes([0x81, 127]) + n.to_bytes(8, "big")
        for s in list(socks or self.socks):
            try:
                s.sendall(head + data)
            except OSError:
                pass

    def stop(self):
        for s in self.socks:
            try:
                s.close()
            except OSError:
                pass
        self.httpd.shutdown()
        self.httpd.server_close()


def test_watch_together():
    """Watch together through the app: the window talks to the app only, the app talks to the hub with the key in
    the header (HTTP and websocket), keeps the session state from the pushes, estimates the hub's clock."""
    hub = _fresh_hub()
    fake = SessionHub(clock_ahead=5.0)
    auto = _Auto(str(TMP / "watchrecs"))
    auto.s.hub_url = fake.url
    b = pcs_server.Backend(auto)
    srv = pcs_server.Server(b)
    srv.start()

    def wait_for(cond, s=5):
        end = time.time() + s
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False
    try:
        assert call(srv, "/api/watch/list")[1] == {"available": False, "sessions": [], "games": [], "role": ""}
        assert call(srv, "/api/watch/list", token=False)[0] == 403
        assert not fake.calls                                               # no key: nothing asked of the hub
        hub.set_key(GOOD_KEY)
        r = call(srv, "/api/watch/list?fresh=1")[1]
        assert r["available"] and r["role"] == "coach" and r["games"][0]["gameKey"] == "g-1" and r["sessions"] == []
        code, snap, _ = call(srv, "/api/watch/start", {"gameKey": "g-1"})
        assert code == 200 and snap["sid"] == "s-1" and snap["state"]["gameKey"] == "g-1", snap
        assert fake.calls[-1][:3] == ("POST", "/api/sessions", {"gameKey": "g-1"})
        assert wait_for(lambda: b.watch.connected and len(b.watch.samples) >= 3)
        assert fake.ws_auth == [("/api/sessions/s-1/ws", "Bearer " + GOOD_KEY)]
        assert all(GOOD_KEY not in c[1] for c in fake.calls)                 # never in an address
        snap = call(srv, f"/api/watch/poll?seq={snap['seq']}&wait=2")[1]
        assert snap["connected"] and snap["me"] == {"name": "Blakey", "role": "coach"}
        assert abs(snap["offset"] - 5.0) < 0.2 and snap["rtt"] is not None   # the hub's clock, 5 s ahead here
        # the coach plays: the answer and the push both carry the new state
        code, snap, _ = call(srv, "/api/watch/control", {"action": "play", "t": 12.5, "junk": 1})
        assert code == 200 and snap["state"]["playing"] and snap["state"]["rev"] == 1
        assert fake.calls[-1][2] == {"action": "play", "t": 12.5}
        # a drawing: pushed as a stroke, applied to the state the window reads
        seq = snap["seq"]
        stroke = {"id": "k1", "kind": "arrow", "color": "#ffd60a", "pts": [[0.1, 0.1], [0.5, 0.5]]}
        assert call(srv, "/api/watch/draw", {"op": "put", "stroke": stroke})[0] == 200
        snap = call(srv, f"/api/watch/poll?seq={seq}&wait=3")[1]
        assert wait_for(lambda: b.watch.state["strokes"] == [stroke]) and b.watch.state["rev"] == 2
        # a push that skips a revision: the app asks for the whole state
        fake.state = dict(fake.state, rev=9, focus="Zed")
        fake.push({"type": "stroke", "op": "clear", "rev": 9})
        assert wait_for(lambda: b.watch.state["focus"] == "Zed" and b.watch.state["rev"] == 9)
        assert ("GET", "/api/sessions/s-1", {}, "Bearer " + GOOD_KEY) in fake.calls
        # presence and positions, and this app's own report over the websocket
        fake.push({"type": "members", "members": {"Moe": {"t": 3.0, "connected": True}}})
        assert wait_for(lambda: b.watch.state["members"].get("Moe", {}).get("t") == 3.0)
        assert call(srv, "/api/watch/report", {"t": 14.25, "buffering": True})[1] == {"ok": True}
        assert wait_for(lambda: fake.reports == [("Blakey", {"type": "report", "t": 14.25, "buffering": True, "browsing": False})])
        # the window's long poll returns at once on a change, else after the wait
        t0 = time.time()
        s2 = call(srv, f"/api/watch/poll?seq={b.watch.seq}&wait=0.5")[1]
        assert 0.4 < time.time() - t0 < 3 and s2["seq"] == b.watch.seq
        # the coach ends it: ended for the window, websocket closed
        snap = call(srv, "/api/watch/end", {})[1]
        assert snap["ended"] == "ended" and not snap["connected"]
        assert call(srv, "/api/watch/control", {"action": "play"})[0] == 500   # not in a session any more
        # a player: joins a running session, can't control it (the hub's words come back)
        call(srv, "/api/watch/start", {"gameKey": "g-2"})
        hub.set_key(PLAYER_KEY)
        r = call(srv, "/api/watch/list?fresh=1")[1]
        assert r["role"] == "player" and r["games"] == [] and r["sessions"][0]["id"] == "s-1"
        snap = call(srv, "/api/watch/join", {"id": "s-1"})[1]
        assert snap["sid"] == "s-1" and wait_for(lambda: b.watch.connected)
        code, r, _ = call(srv, "/api/watch/control", {"action": "pause"})
        assert code == 500 and "403" in r["error"] and "Coach only" in r["error"], r
        assert call(srv, "/api/watch/join", {"id": "../evil"})[0] == 500
        snap = call(srv, "/api/watch/leave", {})[1]
        assert snap["ended"] == "left" and fake.calls[-1][1] == "/api/sessions/s-1/leave"
    finally:
        srv.stop()
        fake.stop()
        b.watch.leave()
        _fresh_hub()


def test_watch_together_never_during_a_game():
    """While Apex runs a session can't be started or joined (nothing asked of the hub). When Apex starts during a
    session, this app leaves it (the hub session goes on for the others, it is not ended), the window hears it at
    once and the session page stops its players."""
    from pcs_recorder import core as rcore
    from pcs_recorder import watch as watchmod
    hub = _fresh_hub()
    fake = SessionHub()
    auto = _Auto(str(TMP / "watchrecs-apex"))
    auto.s.hub_url = fake.url
    b = pcs_server.Backend(auto)
    srv = pcs_server.Server(b)
    srv.start()
    apex, after = [True], []
    real_running, real_every = rcore.apex_running, watchmod.APEX_CHECK_S
    rcore.apex_running, watchmod.APEX_CHECK_S = (lambda: apex[0]), 0.2

    def wait_for(cond, s=5):
        end = time.time() + s
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False
    try:
        hub.set_key(GOOD_KEY)
        for path, body in (("/api/watch/start", {"gameKey": "g-1"}), ("/api/watch/join", {"id": "s-1"})):
            code, r, _ = call(srv, path, body)
            assert code == 500 and r["error"] == "Close Apex to watch together", r
        assert not [c for c in fake.calls if c[1].startswith("/api/sessions")], fake.calls
        apex[0] = False
        code, snap, _ = call(srv, "/api/watch/start", {"gameKey": "g-1"})
        assert code == 200 and snap["sid"] == "s-1"
        b.watch.on_apex_left = lambda: after.append("talk stopped")
        assert wait_for(lambda: b.watch.connected)
        time.sleep(0.5)
        assert b.watch.ended is None                       # no game: the session stays
        apex[0] = True                                     # the player starts Apex
        assert wait_for(lambda: b.watch.ended == "apex", 3)
        snap = call(srv, "/api/watch/poll?seq=-1&wait=0")[1]  # what the window's long poll hears
        assert snap["ended"] == "apex" and not snap["connected"], snap
        assert wait_for(lambda: fake.calls[-1][1] == "/api/sessions/s-1/leave")
        assert not any(c[1].endswith("/end") for c in fake.calls)              # left, not ended for the others
        assert after == ["talk stopped"]
    finally:
        rcore.apex_running, watchmod.APEX_CHECK_S = real_running, real_every
        srv.stop()
        fake.stop()
        b.watch.leave()
        _fresh_hub()
    js = (Path(pcs_server.WEB) / "session.js").read_text(encoding="utf-8")
    assert "apex: 'Apex started, so you left the session. The others keep watching.'" in js
    assert "stopPlayers();" in js


def test_watch_page_files():
    """The session page is its own files, loaded by the window's page; YouTube's player API is the only outside
    script, and app.js only routes to it."""
    web = Path(pcs_server.WEB)
    html = (web / "index.html").read_text(encoding="utf-8")
    assert '<script src="session.js"></script>' in html and 'href="session.css"' in html
    assert html.index("app.js") < html.index("session.js")
    js = (web / "session.js").read_text(encoding="utf-8")
    assert "https://www.youtube.com/iframe_api" in js
    assert [u for u in __import__("re").findall(r"https?://[^\s'\"`)]+", js) if "youtube.com" not in u] == []
    for text in ("No video for", "Start session", "Join", "End session", "Leave", "Sound on", "Paused by you",
                 "Back to coach", "Look at this", "Bookmark", "Record talk", "agreement to record comms", "1 frame",
                 "browsing", "FADE_MS = 300", "FRAME_S = 1 / 60"):
        assert text in js, text
    app = (web / "app.js").read_text(encoding="utf-8")
    assert "drawShapes(" in app and "reviewExtras(d, v)" in app
    # the review player's part: own file, after app.js, no outside address, talk read from the app only
    assert html.index("app.js") < html.index("review-extras.js")
    rx = (web / "review-extras.js").read_text(encoding="utf-8")
    assert __import__("re").findall(r"https?://", rx) == [] and "/talk/" in rx
    for text in ("Session bookmarks", "Review talk"):
        assert text in rx, text


def test_audio_track_never_blocks_the_review():
    """A track that isn't cut yet: the server answers at once ("preparing", 503), the cut runs in the background
    (the review page asks again), and a later request gets the file. Background cuts (after a recording) never run
    while Apex runs; a track the review waits for does. A stopped cut leaves no broken track file."""
    import subprocess
    import threading
    from pcs_recorder import tracks
    folder = TMP / "cutq"
    folder.mkdir(parents=True, exist_ok=True)
    v = folder / "Apex Legends_2026.11.17-15.27.mp4"
    v.write_bytes(b"v" * 1000)
    v.with_suffix(".events.json").write_text(json.dumps({"tracks": ["game", "discord", "mic"]}))
    apex, cuts, gate = [False], [], threading.Event()

    def slow_cut(video, outs):                        # stands in for ffmpeg on a 3-hour recording
        cuts.append([n for n, _ in outs])
        gate.wait(10)
        for _, out in outs:
            out.write_bytes(b"m4a-track-" + out.name.encode())
    c = tracks.Cutter(is_apex_running=lambda: apex[0], cut=slow_cut, count=lambda video: 3, wait_s=0.05)
    b = pcs_server.Backend()
    b.cutter = c
    b.add_paths([str(v)])
    srv = pcs_server.Server(b)
    srv.start()
    try:
        vid = next(x["id"] for x in b.library() if x["path"] == str(v))
        t0 = time.time()
        code, body, h = call(srv, f"/audio/{vid}/0", raw=True)
        assert code == 503 and json.loads(body)["preparing"] and h.get("Retry-After") == "3", (code, body)
        assert time.time() - t0 < 1.0
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/audio/{vid}/1", method="HEAD",
                                     headers={"X-PCS-Token": srv.token})
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("HEAD should say 503 while the track is cut")
        except urllib.error.HTTPError as e:
            assert e.code == 503 and e.read() == b""                 # HEAD: headers only
        gate.set()
        for _ in range(100):
            code, body, h = call(srv, f"/audio/{vid}/1", raw=True)
            if code != 503:
                break
            time.sleep(0.05)
        assert code == 200 and body == b"m4a-track-" + (v.stem + ".track2.m4a").encode(), (code, body)
        assert cuts == [[0, 1, 2]], cuts                               # one cut for all tracks, once
        assert call(srv, f"/audio/{vid}/5", raw=True)[0] == 404        # no such track: not "preparing" forever
    finally:
        srv.stop()
    # background cuts wait while Apex runs, then go; a cut the review waits for goes even during a game
    for f in folder.glob("*.m4a"):
        f.unlink()
    cuts.clear()
    apex[0] = True
    c.add(str(v))
    time.sleep(0.4)
    assert cuts == [] and c.busy()
    w = folder / "Apex Legends_2026.11.17-18.00.mp4"
    w.write_bytes(b"w" * 1000)
    w.with_suffix(".events.json").write_text("{}")
    assert c.state(str(w), 0)[0] == "preparing"
    for _ in range(100):
        if cuts and c.state(str(w), 0)[0] == "ready":
            break
        time.sleep(0.02)
    assert len(cuts) == 1 and c.state(str(w), 0)[0] == "ready", cuts
    time.sleep(0.2)
    assert len(cuts) == 1                                              # v still waits: Apex is running
    apex[0] = False
    for _ in range(100):
        if not c.busy():
            break
        time.sleep(0.05)
    assert len(cuts) == 2 and c.state(str(v), 2)[0] == "ready", cuts
    # a cut stopped halfway (ffmpeg fails, the app closes): no .m4a, no .part left behind
    if shutil.which("ffmpeg"):
        bad = folder / "Apex Legends_bad.mp4"
        bad.write_bytes(b"not a video")
        out = folder / "Apex Legends_bad.track1.m4a"
        try:
            tracks.cut_tracks(str(bad), [(0, out)])
            raise AssertionError("ffmpeg should fail on a broken file")
        except subprocess.CalledProcessError:
            pass
        assert not out.exists() and not tracks.part_of(out).exists()
    stale = folder / "Apex Legends_2026.11.17-15.27.track1.part.m4a"
    stale.write_bytes(b"half")
    c2 = tracks.Cutter(is_apex_running=lambda: False)
    c2.clean([folder])
    assert not stale.exists() and (folder / "Apex Legends_2026.11.17-15.27.track1.m4a").exists()


def test_review_kill_feed_our_squad():
    """Kill feed in the review: each line carries what the scan read (knock / kill, sides, weapon, headshot);
    repeats of a squad line are marked once; older scans say they have no sides. The page's words."""
    sig_a, sig_b = "f" * 36, "0" * 36
    lines = [
        {"t": 10.0, "jpg": "A", "event": "knock", "weapon": "r97", "weaponName": "R-99", "arrow": True,
         "killer": {"side": "squad", "hue": 40, "color": "orange"}, "victim": {"side": "other"},
         "killerSig": sig_a, "victimSig": sig_b},
        {"t": 11.0, "jpg": "B", "event": "knock", "weapon": "r97", "weaponName": "R-99",    # same line, captured again
         "killer": {"side": "squad", "hue": 41, "color": "orange"}, "victim": {"side": "other"},
         "killerSig": sig_a, "victimSig": sig_b},
        {"t": 20.0, "jpg": "C", "event": "kill", "weaponName": "Wingman", "headshot": True,
         "killer": {"side": "other"}, "victim": {"side": "squad", "hue": 179, "color": "teal"}},
        {"t": 30.0, "jpg": "D", "event": "kill", "killer": {"side": "other"}, "victim": {"side": "other"}},
        {"t": 40.0, "jpg": "E"},
    ]
    out, sides = pcs_server.feed_lines(lines)
    assert sides and len(out) == 5 and [x.get("dup", False) for x in out] == [False, True, False, False, False]
    assert out[0]["killer"] == {"side": "squad", "hue": 40, "color": "orange"} and "killerSig" not in out[0]
    assert out[2]["headshot"] is True and out[2]["victim"]["side"] == "squad"
    old, sides = pcs_server.feed_lines([{"t": 5.0, "jpg": "X"}])
    assert not sides and old == [{"t": 5.0, "jpg": "X"}]
    js = (Path(pcs_server.WEB) / "app.js").read_text(encoding="utf-8")
    for text in ("Only our squad", "Kill feed sides not read in this scan", "Preparing ${x.t.key === 'game' ? 'game sound'",
                 "'knocked'", "'got knocked'", "'died'", ">Theatre</button>", "pcs-theatre"):
        assert text in js, text


class MinimapHub:
    """A hub 0.5.2 with the `minimaps` collection: the list and the pictures need a key (any role). `docs` and `blobs`
    can be changed by the test; `blob_calls` counts picture downloads."""
    JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 8
    PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 50

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        self.docs = {"SP": {"name": "Storm Point", "asset": "a" * 32, "pos_x": -50606, "pos_y": 52139, "scale": 25, "size": 4096},
                     "BAD": {"asset": "../../x", "pos_x": 0, "pos_y": 0, "scale": 1, "size": 1}}
        self.blobs = {"a" * 32: self.JPEG}
        self.calls, self.blob_calls = [], 0
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, data, ctype="application/json"):
                data = data if isinstance(data, bytes) else json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                auth = self.headers.get("Authorization", "")
                hub.calls.append((self.path, auth))
                if auth != "Bearer " + GOOD_KEY:
                    return self._send(401, {"detail": "This key is not valid.", "error": "bad_key"})
                if self.path == "/api/db/minimaps":
                    return self._send(200, [{"id": k, "data": v, "version": 1} for k, v in hub.docs.items()])
                if self.path.startswith("/_blob/"):
                    hub.blob_calls += 1
                    b = hub.blobs.get(self.path[7:])
                    # the real hub answers a placeholder drawing (200) for a picture it doesn't have
                    return self._send(200, b, "image/jpeg") if b else self._send(200, b"<svg/>", "image/svg+xml")
                return self._send(404, {"detail": "Not Found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _age_minimap_list(data, seconds):
    idx = json.loads((data / "index.json").read_text())
    idx["at"] -= seconds
    (data / "index.json").write_text(json.dumps(idx))


def test_minimap_download_and_cache():
    """Map panel pictures: downloaded once from the hub with the key, kept in the data folder (never next to the
    app), served to the page with the page's token; offline the copy still shows; no key / no hub / no picture for
    the map: said so, nothing downloaded."""
    from pcs_recorder import minimaps
    hub = _fresh_hub()
    fake = MinimapHub()
    auto = _Auto(str(TMP / "mmrecs"))
    auto.s.hub_url = fake.url
    b = pcs_server.Backend(auto)
    srv = pcs_server.Server(b)
    srv.start()
    data = Path(os.environ["PCS_DATA_DIR"]) / "minimaps"
    try:
        # no key: needs the hub, nothing asked
        r = call(srv, "/api/minimap/SP")[1]
        assert r == {"available": False, "reason": "hub", "text": "Minimap needs the team hub"} and not fake.calls, r
        hub.set_key(GOOD_KEY)
        r = call(srv, "/api/minimap/SP")[1]
        assert r["available"] and r["asset"] == "a" * 32, r
        assert (r["pos_x"], r["pos_y"], r["scale"], r["size"]) == (-50606, 52139, 25, 4096), r
        assert [p for p, _ in fake.calls] == ["/api/db/minimaps", "/_blob/" + "a" * 32]
        assert all(a == "Bearer " + GOOD_KEY for _, a in fake.calls) and not any(GOOD_KEY in p for p, _ in fake.calls)
        files = sorted(f.name for f in data.iterdir())
        assert files == ["SP-" + "a" * 32 + ".img", "index.json"], files
        assert not list(Path(pcs_server.WEB).rglob("*.img")) and GOOD_KEY not in (data / "index.json").read_text()
        # the page gets the picture with its token only
        code, body, h = call(srv, "/minimap/SP", raw=True)
        assert code == 200 and body == MinimapHub.JPEG and h["Content-Type"] == "image/jpeg"
        assert call(srv, "/minimap/SP", token=False, raw=True)[0] == 403
        assert call(srv, "/minimap/WE", raw=True)[0] == 404 and call(srv, "/minimap/..%2F..%2Fx", raw=True)[0] == 404
        # asked again: no new download, no new list (kept for hours)
        call(srv, "/api/minimap/SP")
        assert fake.blob_calls == 1 and len(fake.calls) == 2
        # a map without a picture (Olympus), a game whose map wasn't read, a bad document: "no minimap yet"
        for code in ("OL", "?", "BAD", "x"):
            r = call(srv, "/api/minimap/" + urllib.request.quote(code))[1]
            assert r == {"available": False, "reason": "map", "text": "No minimap for this map yet"}, (code, r)
        # the coach uploads a new picture: the next list read downloads it, the old copy goes
        fake.docs["SP"] = dict(fake.docs["SP"], asset="b" * 32)
        fake.blobs["b" * 32] = MinimapHub.PNG
        _age_minimap_list(data, minimaps.INDEX_TTL + 1)
        r = call(srv, "/api/minimap/SP")[1]
        assert r["asset"] == "b" * 32 and sorted(f.name for f in data.glob("*.img")) == ["SP-" + "b" * 32 + ".img"]
        assert call(srv, "/minimap/SP", raw=True)[2]["Content-Type"] == "image/png"
        # a picture the hub lost (it answers a drawing instead): nothing written, the panel says it needs the hub
        fake.docs["WE"] = dict(fake.docs["SP"], asset="c" * 32)
        _age_minimap_list(data, minimaps.MISS_TTL + 1)
        r = call(srv, "/api/minimap/WE")[1]
        assert r["available"] is False and r["reason"] == "hub" and not list(data.glob("WE-*")), r
        # offline: the copy already here still shows
        fake.stop()
        _age_minimap_list(data, minimaps.INDEX_TTL + 1)
        r = call(srv, "/api/minimap/SP")[1]
        assert r["available"] and r["asset"] == "b" * 32
        # no hub address: needs the hub
        auto.s.hub_url = ""
        assert call(srv, "/api/minimap/SP")[1]["reason"] == "hub"
    finally:
        srv.stop()
        _fresh_hub()
        shutil.rmtree(data, ignore_errors=True)


def test_minimap_transform_and_page():
    """World -> picture with the game's transform (exact on its 4096 px minimap), and the panel's texts and hooks."""
    from pcs_recorder import minimaps
    tx = {"pos_x": -43008, "pos_y": 43008, "scale": 21, "size": 4096}        # E-District
    assert minimaps.to_picture(tx, 0, 0) == (0.5, 0.5)
    assert minimaps.to_picture(tx, -43008, 43008) == (0, 0)
    fx, fy = minimaps.to_picture(tx, -23252.34, -8508.78)                    # a landing in Neon Square (4 Nov scan)
    assert abs(fx * 4096 - 940.75) < 0.01 and abs(fy * 4096 - 2453.2) < 0.1
    we = {"pos_x": -45056, "pos_y": 45055, "scale": 22, "size": 4096}       # the hub's World's Edge formula
    assert minimaps.to_picture(we, 45056, -45057) == (1.0, 1.0)
    web = Path(pcs_server.WEB)
    js = (web / "minimap.js").read_text(encoding="utf-8")
    for text in ("Minimap needs the team hub", "No minimap for this map yet", "(x - info.pos_x) / info.scale / info.size",
                 "(info.pos_y - y) / info.scale / info.size", "/api/minimap/", "pcs-map", "ResizeObserver"):
        assert text in js, text
    assert "requestAnimationFrame" not in js and "setInterval" not in js          # drawn on changes only
    html = (web / "index.html").read_text(encoding="utf-8")
    assert '<script src="minimap.js"></script>' in html and 'href="minimap.css"' in html
    app = (web / "app.js").read_text(encoding="utf-8")
    assert "minimapSetup(d, v)" in app and "minimapToggle()" in app and "<kbd>M</kbd> map" in app
    # nothing of EA's in the app: no picture files next to the page
    assert not [f for f in web.rglob("*") if f.suffix.lower() in (".jpg", ".jpeg", ".img")]


def _edge():
    for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""), os.environ.get("LOCALAPPDATA", "")):
        p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if base and p.exists():
            return p
    return None


class _Page:
    """The app's page in a headless Edge, driven over the DevTools protocol (no extra library: websocket-client
    comes with the OBS library). Skipped where Edge isn't installed."""

    def __init__(self, url):
        import subprocess
        import websocket
        self.proc = None
        port = int(os.environ.get("PCS_TEST_CDP_PORT") or 0)   # a browser already open with remote debugging
        if not port:
            edge = _edge()
            if os.name != "nt" or not edge:
                raise Skipped("no Microsoft Edge on this PC")
            self.prof = Path(tempfile.mkdtemp(dir=TMP))
            self.proc = subprocess.Popen([str(edge), "--headless=new", "--remote-debugging-port=0", "--no-first-run",
                                          f"--user-data-dir={self.prof}", "--window-size=1320,860", "about:blank"])
            port_f, end = self.prof / "DevToolsActivePort", time.time() + 30
            while not (port_f.exists() and port_f.read_text().strip()) and time.time() < end:
                time.sleep(0.2)
            port = int(port_f.read_text().split()[0])
        end = time.time() + 20
        while True:
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5))
                page = next(t for t in tabs if t.get("type") == "page")
                break
            except Exception:
                if time.time() > end:
                    raise
                time.sleep(0.3)
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
        self.n = 0
        self.send("Page.navigate", {"url": url})
        end = time.time() + 20
        while self.js("typeof PCS_POLL") != "object":
            if time.time() > end:
                raise AssertionError("the page didn't load")
            time.sleep(0.2)
        # document.hidden is set by the test (headless Edge has no window to hide); the page reads it at each step
        self.js("let __h = false; Object.defineProperty(document, 'hidden', {configurable: true, get: () => __h});"
                "Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => __h ? 'hidden' : 'visible'});"
                "window.__setHidden = v => { __h = v; document.dispatchEvent(new Event('visibilitychange')); }; 1")

    def send(self, method, params=None):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.n:
                return m.get("result", {})

    def js(self, expr):
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        finally:
            if self.proc:
                self.proc.kill()
                self.proc.wait(10)


def test_window_polls_only_when_seen():
    """The page asks the app for its state every 2 s while it is seen, not at all while hidden (tray, minimized),
    every 10 s while Apex runs unless the player is using the window; shown again = asked at once.
    And no endless animation in the page's style (it made the window draw at the screen's refresh rate)."""
    css = (Path(pcs_server.WEB) / "app.css").read_text(encoding="utf-8")
    import re
    for rule in re.findall(r"[^{}]*\{[^{}]*animation:[^}]*infinite[^}]*\}", css):
        assert "spin" in rule, rule                        # only the scanning spinner (paused while Apex runs)
    assert "@keyframes pulse" not in css and "@keyframes glow" not in css
    assert ".quiet *{animation-play-state:paused" in css
    for extra in ("session.css",):                         # Watch together's style: no endless animation either
        assert not re.search(r"animation:[^;}]*infinite", (Path(pcs_server.WEB) / extra).read_text(encoding="utf-8"))
    wt = (Path(pcs_server.WEB) / "session.js").read_text(encoding="utf-8")
    assert "if (document.hidden) return;" in wt                                   # Join bar: not while hidden…
    assert "(S && S.apex) ||" in wt                                               # …nor asks the hub while Apex runs
    assert "page !== 'watch' || document.hidden) return;" in wt                   # session sync idles while hidden
    assert "wait=${document.hidden ? 25 : 15}" in wt         # hidden: a long poll on this PC only (hears an end at once)
    b = pcs_server.Backend()
    calls, apex = [], [False]
    real = b.state
    b.state = lambda: (calls.append(time.time()), {**real(), "apex": apex[0]})[1]
    srv = pcs_server.Server(b)
    srv.start()
    page = _Page(srv.url)
    try:
        def wait(cond, s):
            end = time.time() + s
            while time.time() < end and not cond():
                time.sleep(0.1)
            return cond()
        assert wait(lambda: len(calls) >= 3, 15), calls          # seen: every 2 s
        page.js("__setHidden(true)")
        time.sleep(0.5)
        n = len(calls)
        time.sleep(5)
        assert len(calls) == n, len(calls) - n                    # hidden: nothing at all
        assert page.js("PCS_POLL.active()") is False
        t = time.time()
        page.js("__setHidden(false)")
        assert wait(lambda: len(calls) > n, 2) and calls[n] - t < 1.5   # shown: asked at once
        apex[0] = True
        assert wait(lambda: page.js("PCS_POLL.delay()") == 10000, 5)
        assert page.js("document.body.classList.contains('quiet')") is True
        time.sleep(1)
        n = len(calls)
        time.sleep(6)
        assert len(calls) - n <= 1, len(calls) - n                 # Apex running: every 10 s
        n = len(calls)
        page.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'a'}))")
        assert wait(lambda: len(calls) > n, 2)               # the player uses the window: fresh at once…
        assert page.js("PCS_POLL.delay()") == 2000                # …and every 2 s while they do
    finally:
        page.close()
        srv.stop()


def run_all(tests):
    """Run every test, even after a failure, and list the failures at the end. -> exit code (1 if any failed)."""
    import traceback
    failed, skipped = [], []
    for name, fn in tests:
        try:
            fn()
        except Skipped as e:
            skipped.append(name)
            print(f"skip {name}: {e}", flush=True)
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            failed.append(name)
            print(f"FAIL {name}", flush=True)
            traceback.print_exc()
        else:
            print("ok", name, flush=True)
    print(f"\n{len(tests) - len(failed) - len(skipped)} passed, {len(failed)} failed, {len(skipped)} skipped", flush=True)
    for name in failed:
        print("  failed:", name, flush=True)
    return 1 if failed else 0


def test_watch_extras_through_the_app():
    """Pointer, "Look at this", bookmarks, a player browsing, and afterwards the review player's session bookmarks
    and review talk: all through the app, key only in the header."""
    hub = _fresh_hub()
    fake = SessionHub()
    folder = TMP / "extras"
    folder.mkdir(exist_ok=True)
    rec = folder / "Apex Legends_2026.11.07-21.42.mp4"
    rec.write_bytes(b"x" * 100)
    auto = _Auto(str(TMP / "extrasrecs"))
    auto.s.hub_url = fake.url
    b = pcs_server.Backend(auto)
    b.add_paths([str(rec)])
    vid = next(x["id"] for x in b.library() if x["path"] == str(rec))
    srv = pcs_server.Server(b)
    srv.start()

    def wait_for(cond, s=5):
        end = time.time() + s
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False
    try:
        assert call(srv, f"/api/library/{vid}/extras")[1] == {"available": False, "games": []}      # no key yet
        hub.set_key(GOOD_KEY)
        call(srv, "/api/watch/start", {"gameKey": "g-1"})
        assert wait_for(lambda: b.watch.connected)
        # the coach's pointer: sent, pushed back, in what the window reads (with when it came)
        assert call(srv, "/api/watch/pointer", {"x": 0.25, "y": 0.5, "junk": 1})[0] == 200
        assert fake.calls[-1][1:3] == ("/api/sessions/s-1/pointer", {"x": 0.25, "y": 0.5})
        assert wait_for(lambda: (b.watch.pointer_at or {}).get("x") == 0.25)
        snap = call(srv, "/api/watch/poll?seq=-1&wait=0")[1]
        assert snap["pointer"]["y"] == 0.5 and snap["pointer"]["at"] > 0
        call(srv, "/api/watch/pointer", {"hide": True})
        assert wait_for(lambda: (b.watch.pointer_at or {}).get("hide") is True)
        # a bookmark with its note and moment
        r = call(srv, "/api/watch/bookmark", {"text": "Rotate here", "t": 75.5})[1]
        assert r["id"] == "b-1" and fake.calls[-1][2] == {"text": "Rotate here", "t": 75.5}
        # a player: "Look at this" at their own moment, and browsing in their report
        hub.set_key(PLAYER_KEY)
        call(srv, "/api/watch/join", {"id": "s-1"})
        assert wait_for(lambda: b.watch.connected)
        r = call(srv, "/api/watch/flag", {"t": 101.25})[1]
        assert r["player"] == "Moe" and fake.calls[-1][2] == {"t": 101.25}
        call(srv, "/api/watch/report", {"t": 30, "buffering": False, "browsing": True})
        assert wait_for(lambda: any(m.get("browsing") for _, m in fake.reports))
        assert call(srv, "/api/watch/bookmark", {"text": "x"})[0] == 500               # the hub: coach only
        # afterwards, in the review player: bookmarks and the review talk of this recording's games
        r = call(srv, f"/api/library/{vid}/extras")[1]
        assert r["available"] and r["games"][0]["bookmarks"][0]["text"] == "Rotate here"
        assert ("GET", "/api/review-extras?recording=Apex+Legends_2026.11.07-21.42", {}, "Bearer " + PLAYER_KEY) in fake.calls
        code, data, h = call(srv, "/talk/ta-0123456789ab", raw=True, headers={"Range": "bytes=0-7"})
        assert code == 206 and data == b"TALKTALK" and h["Content-Type"] == "audio/mp4"
        assert call(srv, "/talk/ta-0123456789ab", raw=True)[1] == b"TALK" * 1000
        assert fake.audio_gets == ["/api/session-audio/ta-0123456789ab"]                    # downloaded once
        assert call(srv, "/talk/ta-0123456789ab", raw=True, token=False)[0] == 403
        assert call(srv, "/talk/..%2f..%2fhub_key.dat", raw=True)[0] == 500
        assert all(GOOD_KEY not in c[1] and PLAYER_KEY not in c[1] for c in fake.calls)
    finally:
        srv.stop()
        fake.stop()
        b.watch.leave()
        _fresh_hub()
        core.SETTINGS["library"] = [p for p in core.SETTINGS.get("library", []) if p != str(rec)]


def test_review_talk_lined_up_with_game_time():
    """Talk recorded in clock time, a session paused and seeked: segments say which audio goes with which game time."""
    from pcs_recorder.talk import segments
    tl = [{"w": 100.0, "playing": False, "t": 10.0, "at": 90.0, "speed": 1, "gameKey": "g"},
          {"w": 103.0, "playing": False, "t": 10.0, "at": 91.0, "speed": 1, "gameKey": "g"},     # same moment: merged
          {"w": 105.0, "playing": True, "t": 10.0, "at": 107.0, "speed": 1, "gameKey": "g"},     # hub clock = PC + 2
          {"w": 115.0, "playing": False, "t": 50.0, "at": 117.0, "speed": 1, "gameKey": "g"},    # seek, paused
          {"w": 118.0, "playing": True, "t": 50.0, "at": 120.0, "speed": 0.5, "gameKey": "g"},   # half speed
          {"w": 118.1, "playing": True, "t": 50.05, "at": 120.1, "speed": 0.5, "gameKey": "g"}]  # too short: dropped
    assert segments(tl, 100.0, 124.0, 2.0) == [
        {"a0": 0.0, "a1": 5.0, "g0": 10.0, "g1": 10.0, "speed": 1, "gameKey": "g"},
        {"a0": 5.0, "a1": 15.0, "g0": 10.0, "g1": 20.0, "speed": 1, "gameKey": "g"},
        {"a0": 15.0, "a1": 18.0, "g0": 50.0, "g1": 50.0, "speed": 1, "gameKey": "g"},
        {"a0": 18.1, "a1": 24.0, "g0": 50.05, "g1": 53.0, "speed": 0.5, "gameKey": "g"}]
    # the recording started after the session's last change: from its own start
    assert segments(tl[:1], 102.0, 104.0, 0) == [{"a0": 0.0, "a1": 2.0, "g0": 10.0, "g1": 10.0, "speed": 1, "gameKey": "g"}]


def test_review_talk_recording():
    """Review talk: only with comms consent, never while Apex runs or records; OBS records into its own folder and is
    set back for game recording; the two sound tracks are copied out, the temporary video deleted, then sent to the
    hub (coach only) and kept here until the hub took them."""
    import threading
    from pcs_recorder import core as reccore, settings as st, talk as talkmod
    hub = _fresh_hub()
    fake = SessionHub()
    folder = TMP / "talkrecs"
    s = st.Settings(folder=str(folder), comms_consent=False, discord_audio=True, mic_audio=True, hub_url=fake.url)

    class Status:
        def __init__(self, active):
            self.output_active = active

    class Client:
        def __init__(self):
            self.active, self.calls = False, []

        def get_record_status(self):
            return Status(self.active)

        def set_record_directory(self, d):
            self.calls.append(("dir", d))

        def set_profile_parameter(self, *a):
            self.calls.append(("param",) + a)

        def start_record(self):
            self.active = True
            self.calls.append(("start",))

        def stop_record(self):
            self.active = False
            f = talkmod.TALK_DIR / "PCS talk 2026.11.17-20.00.00.mkv"
            f.write_bytes(b"v" * 500)
            self.calls.append(("stop",))

            class R:
                output_path = str(f)
            return R()

    class Obs:
        def __init__(self):
            self.obs, self.ready, self.readied = type("I", (), {})(), True, 0
            self.obs.c = Client()

        def ensure_ready(self):
            self.readied += 1

    class Rec:
        current, lock = None, threading.RLock()
        apex = False

        def is_apex_running(self):
            return self.apex
    r = Rec()
    r.s, r.obs = s, Obs()
    clock = [1000.0]
    t = talkmod.Talk(r, clock=lambda: clock[0])
    copied = []

    def fake_extract(video, index, out):
        copied.append((Path(video).name, index, Path(out).name))
        Path(out).write_bytes(b"a" * (100 + index))
        return Path(out)
    real_extract = reccore.extract_track
    reccore.extract_track = fake_extract
    try:
        assert "agreement" in t.why_not()
        try:
            t.start("s-1", None)
            raise AssertionError("started without consent")
        except RuntimeError:
            pass
        s.comms_consent = True
        r.apex = True
        assert "Close Apex" in t.why_not() and not r.obs.obs.c.calls
        r.apex, r.current = False, "k"                                          # Apex being recorded
        assert "Close Apex" in t.why_not()
        r.current = None
        assert t.state()["tracks"] == ["voice", "mic"] and t.why_not() == ""
        st0 = {"id": "s-1", "playing": False, "t": 10.0, "at": 999.0, "speed": 1, "gameKey": "g-1"}
        assert t.start("s-1", st0)["active"] and r.obs.readied == 1
        c = r.obs.obs.c
        assert c.calls[:3] == [("dir", str(talkmod.TALK_DIR)), ("param", "Output", "FilenameFormatting", talkmod.TALK_FILENAME), ("start",)]
        clock[0] = 1004.0
        t.event(dict(st0, playing=True, at=1004.0))
        clock[0] = 1010.0
        made = t.stop(0.0)
        # OBS set back for game recording; tracks copied out (track order game, discord, mic); video deleted
        from pcs_recorder.obs import FILENAME
        assert c.calls[-2:] == [("dir", str(s.folder_path)), ("param", "Output", "FilenameFormatting", FILENAME)]
        assert copied == [("PCS talk 2026.11.17-20.00.00.mkv", 1, "s-1-voice.m4a"), ("PCS talk 2026.11.17-20.00.00.mkv", 2, "s-1-mic.m4a")]
        assert not list(talkmod.TALK_DIR.glob("*.mkv"))
        assert [m["track"] for m in made] == ["voice", "mic"] and made[0]["segments"] == [
            {"a0": 0.0, "a1": 4.0, "g0": 10.0, "g1": 10.0, "speed": 1.0, "gameKey": "g-1"},
            {"a0": 4.0, "a1": 10.0, "g0": 10.0, "g1": 16.0, "speed": 1.0, "gameKey": "g-1"}]
        assert talkmod.pending_count() == 2 and not t.active and t.stop() == []
        # to the hub: coach key in the header, multipart with the segments; files gone once taken
        assert talkmod.send_pending(fake.url) == 0                              # no key: nothing
        hub.set_key(GOOD_KEY)
        fake.upload_code = 500
        assert talkmod.send_pending(fake.url) == 0 and talkmod.pending_count() == 2
        fake.upload_code = 200
        assert talkmod.send_pending(fake.url) == 2 and talkmod.pending_count() == 0
        assert b'name="track"' in fake.uploads[-1] and b"s-1" in fake.uploads[-1] and b'"g1": 16.0' in fake.uploads[-1]
        assert fake.calls[-1][3] == "Bearer " + GOOD_KEY and not list(talkmod.TALK_DIR.glob("*.m4a"))
        # refused by the hub (e.g. a player's key): kept here, not sent again and again
        t.start("s-1", st0)
        clock[0] = 1020.0
        t.stop(0.0)
        fake.upload_code = 403
        n = len(fake.uploads)
        talkmod.send_pending(fake.url)
        talkmod.send_pending(fake.url)
        assert len(fake.uploads) == n + 2 and talkmod.pending_count() == 0 and len(talkmod.load_pending()) == 2
        # Apex starts while talk records: the recorder stops it first (keeps what was said)
        stops = []

        class FakeTalk:
            active = True

            def stop(self, offset=None, why=""):
                stops.append(why)
        rr = reccore.Recorder.__new__(reccore.Recorder)
        rr.lock, rr.talk, rr.current = threading.RLock(), FakeTalk(), None
        rr.is_apex_running = lambda: True
        started = []
        rr._start = lambda: started.append(stops[:])
        rr._manage_scan = rr._next_in_queue = lambda *a: None
        rr.scan_proc, rr.night = None, None
        rr.tick()
        assert started == [["Apex started"]]
    finally:
        reccore.extract_track = real_extract
        fake.stop()
        _fresh_hub()
        talkmod.save_pending([])


# ------------------------------------------------------------------ hub backup copy (coach's hub key only)
PLAYER_KEY = "pcs_" + "P" * 43


def _zip_bytes(tag: str, pad: int = 0) -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("data/hub.db", tag.encode() + os.urandom(pad))
        zf.writestr("backup-info.json", json.dumps({"hub_version": "0.5.4"}))
    return buf.getvalue()


class BackupHub:
    """A hub 0.5.4: /api/me (GOOD_KEY = coach, PLAYER_KEY = player) and the coach-only /api/backup/latest (GET, HEAD,
    ?date=). `zips` = {yyyymmdd: bytes}; `fail` = status to answer on the backup route; `lie` = bytes added to the
    size header. Every request is kept (method, path)."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        self.zips, self.fail, self.lie, self.calls = {}, 0, 0, []
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def _go(self):
                hub.calls.append((self.command, self.path))
                auth = self.headers.get("Authorization", "")
                role = {"Bearer " + GOOD_KEY: "coach", "Bearer " + PLAYER_KEY: "player"}.get(auth)
                if not role:
                    return self._json(401, {"error": "bad_key"})
                if self.path == "/api/me":
                    return self._json(200, {"name": "Blakey" if role == "coach" else "Moe", "role": role})
                if self.path.startswith("/api/backup/latest"):
                    if role != "coach":
                        return self._json(403, {"error": "coach_only"})
                    if hub.fail:
                        return self._json(hub.fail, {"error": "x"})
                    q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    days = sorted(hub.zips, reverse=True)
                    if q.get("date"):
                        days = [d for d in days if d == q["date"][0]]
                    if not days:
                        return self._json(404, {"error": "none"})
                    data = hub.zips[days[0]]
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("X-Backup-Date", days[0])
                    self.send_header("X-Backup-Bytes", str(len(data) + hub.lie))
                    self.end_headers()
                    if self.command == "GET":
                        self.wfile.write(data)
                    return
                return self._json(404, {"error": "not found"})

            do_GET = do_HEAD = do_POST = _go

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def backup_calls(self):
        return [c for c in self.calls if c[1].startswith("/api/backup")]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _backup_setup(key=GOOD_KEY):
    """App server + fake hub + a scratch target folder (standing in for E:). The test PC's own drives are never
    used: the Windows drive is pretended to be Q:, the E: drive check answers what the test says."""
    from pcs_recorder import hubbackup, settings as st
    hub = _fresh_hub()
    hub.set_key(key)
    hubbackup.STATE_FILE.unlink(missing_ok=True)
    if not hasattr(_backup_setup, "real_save"):
        _backup_setup.real_save = st.save
    st.save = lambda s: None                # the stand-in isn't a real Settings object (restored by _backup_done)
    fake = BackupHub()
    auto = _Auto(str(TMP / "bkrecs"))
    auto.s.hub_url = fake.url
    b = pcs_server.Backend(auto)
    hb = b.hubbackup
    hb.drive_exists = lambda p: False
    hb.busy = lambda: None
    clock = {"t": time.mktime((2026, 11, 18, 5, 0, 0, 0, 0, -1))}       # 05:00, after the hub's nightly zip
    hb.now = lambda: clock["t"]
    target = TMP / "E-drive" / "PCS Hub backups"
    shutil.rmtree(target.parent, ignore_errors=True)
    srv = pcs_server.Server(b)
    srv.start()
    return hubbackup, hub, fake, b, hb, srv, clock, target


def _backup_done(srv, fake):
    from pcs_recorder import settings as st
    st.save = _backup_setup.real_save
    srv.stop()
    fake.stop()
    _fresh_hub()


def _backup_wait(hb, s=10):
    end = time.time() + s
    while hb.running and time.time() < end:
        time.sleep(0.02)
    assert not hb.running


def test_hub_backup_folder_rules():
    """Default E:\\PCS Hub backups only when there is an E: drive (never C:); C: refused; bad paths refused."""
    from pcs_recorder import hubbackup
    real = hubbackup.system_drive
    try:
        hubbackup.system_drive = lambda: "C:"
        assert hubbackup.default_folder(lambda p: p == "E:\\") == "E:\\PCS Hub backups"
        assert hubbackup.default_folder(lambda p: False) == ""
        for bad in ("C:\\PCS Hub backups", "c:/x", "relative\\folder", "\\\\server\\share\\x", "E:"):
            try:
                hubbackup.check_folder(bad)
            except ValueError:
                pass
            else:
                raise AssertionError(bad)
        assert hubbackup.check_folder("  ") == "" and hubbackup.check_folder("E:\\PCS Hub backups") == "E:\\PCS Hub backups"
        hubbackup.system_drive = lambda: "E:"                  # Windows on E: -> no default there either
        assert hubbackup.default_folder(lambda p: True) == ""
        for k in (0, 366, "x"):
            try:
                hubbackup.validate(None, k)
            except ValueError:
                pass
            else:
                raise AssertionError(k)
    finally:
        hubbackup.system_drive = real


def test_hub_backup_download_skip_and_keep():
    """Coach: Settings block, folder + keep saved, the daily copy downloads the newest zip (.partial, then renamed),
    the same date again is skipped, only pcs-hub-<8 digits>.zip files beyond `keep` are removed, Copy now works."""
    hubbackup, hub, fake, b, hb, srv, clock, target = _backup_setup()
    real_sd = hubbackup.system_drive
    hubbackup.system_drive = lambda: "Q:"                      # the scratch folder may be on this PC's C:
    try:
        fake.zips = {"20261117": _zip_bytes("old"), "20261118": _zip_bytes("new", 3 << 20)}   # 3 MB: several chunks
        assert call(srv, "/api/state")[1]["hubBackup"] is None       # role not known yet: no block
        assert hb.role() == "coach" and fake.calls == [("GET", "/api/me")]
        st = call(srv, "/api/state")[1]["hubBackup"]
        assert st["folder"] == "" and st["keep"] == 14 and st["last"] == {}, st   # no E: here: off
        assert not hb.tick() and not fake.backup_calls()                         # off: nothing asked
        # settings: folder and keep, through the normal Save
        code, r, _ = call(srv, "/api/settings", {"hub_backup_dir": str(target), "hub_backup_keep": 3})
        assert code == 200 and r["hubBackup"]["folder"] == str(target) and r["hubBackup"]["keep"] == 3, r
        code, r, _ = call(srv, "/api/settings", {"hub_backup_dir": "Q:\\backups", "player": "changed?"})
        assert code == 500 and "another drive" in r["error"] and core.SETTINGS.get("player") != "changed?"
        assert hb.folder == str(target)
        # files that aren't the copy's own, and older copies
        target.mkdir(parents=True)
        others = ["holiday.jpg", "pcs-hub-notes.zip", "pcs-hub-2026111.zip", "PCS-HUB-20261101.zip.bak",
                  "pcs-hub-20261101.zip.txt", "my pcs-hub-20261101.zip"]
        for o in others:
            (target / o).write_text("mine")
        (target / "sub").mkdir()
        (target / "sub" / "pcs-hub-20261101.zip").write_text("mine")
        for d in ("20261110", "20261111", "20261112", "20261113"):
            (target / f"pcs-hub-{d}.zip").write_text("old copy")
        (target / "pcs-hub-20261112.zip.partial").write_text("left over")
        # 03:00: not yet
        clock["t"] -= 2 * 3600
        assert not hb.due() and not hb.tick()
        clock["t"] += 2 * 3600
        assert hb.tick()
        _backup_wait(hb)
        z = target / "pcs-hub-20261118.zip"
        assert z.read_bytes() == fake.zips["20261118"] and not list(target.glob("*.partial"))
        assert fake.backup_calls() == [("HEAD", "/api/backup/latest"), ("GET", "/api/backup/latest?date=20261118")]
        own = sorted(p.name for p in target.iterdir() if hubbackup.NAME_RE.fullmatch(p.name))
        assert own == ["pcs-hub-20261112.zip", "pcs-hub-20261113.zip", "pcs-hub-20261118.zip"], own
        for o in others:
            assert (target / o).read_text() == "mine"
        assert (target / "sub" / "pcs-hub-20261101.zip").exists()
        last = call(srv, "/api/state")[1]["hubBackup"]["last"]
        assert last["ok"] and last["result"] == "Copied" and last["date"] == "20261118" and last["bytes"] == z.stat().st_size
        assert not any("Hub backup" in m["text"] for m in b.messages)          # no message when it works
        # same day: not again; next day, same zip on the hub: HEAD only, skipped
        assert not hb.due() and not hb.tick()
        clock["t"] += 86400
        n = len(fake.backup_calls())
        assert hb.tick()
        _backup_wait(hb)
        assert fake.backup_calls()[n:] == [("HEAD", "/api/backup/latest")]
        assert call(srv, "/api/state")[1]["hubBackup"]["last"]["result"] == "Already copied"
        # Copy now (the button): a new zip on the hub
        fake.zips["20261119"] = _zip_bytes("newer")
        code, r, _ = call(srv, "/api/hubbackup/copy", {})
        assert code == 200 and r["started"], r
        _backup_wait(hb)
        assert (target / "pcs-hub-20261119.zip").read_bytes() == fake.zips["20261119"]
        assert not (target / "pcs-hub-20261112.zip").exists() and (target / "holiday.jpg").exists()
        # an incomplete download: nothing kept, the previous copies stay
        fake.zips["20261120"], fake.lie = _zip_bytes("bad"), 5
        res = hb.copy_once()
        assert not res["ok"] and "incomplete" in res["error"] and not (target / "pcs-hub-20261120.zip").exists()
        assert not list(target.glob("*.partial")) and (target / "pcs-hub-20261119.zip").exists()
        # the key is never in the state file
        assert GOOD_KEY not in hubbackup.STATE_FILE.read_text()
    finally:
        hubbackup.system_drive = real_sd
        _backup_done(srv, fake)


def test_hub_backup_never_during_apex():
    """Apex running (or a recording, a scan): no request at all, Copy now refused; Apex starting during a download
    stops it (partial removed, not counted as a failure) and it is tried again later."""
    hubbackup, hub, fake, b, hb, srv, clock, target = _backup_setup()
    real_sd, real_every = hubbackup.system_drive, hubbackup.BUSY_EVERY_S
    hubbackup.system_drive = lambda: "Q:"
    try:
        fake.zips = {"20261118": _zip_bytes("big", 4 << 20)}
        hb.role()
        hb.set(str(target), 14)
        fake.calls.clear()
        hb.busy = lambda: "Apex is running"
        assert not hb.tick() and fake.calls == []
        code, r, _ = call(srv, "/api/hubbackup/copy", {})
        assert code == 500 and r["error"] == "Close Apex first." and fake.calls == []
        # the real check: Apex, the recorder, a scan
        b2 = pcs_server.Backend(_Auto(str(TMP / "bkrecs2")))
        b2._apex_running = lambda: True
        assert b2._backup_busy() == "Apex is running"
        b2._apex_running = lambda: False
        assert b2._backup_busy() is None
        b2.auto.rec.current = object()
        assert b2._backup_busy() == "the auto-recorder is busy"
        b2.auto.rec.current, b2.scan_now = None, "x.mp4"
        assert b2._backup_busy() == "a scan is running"
        # Apex starts after the download began
        looks = []
        hb.busy = lambda: (looks.append(1), None if len(looks) <= 2 else "Apex is running")[1]
        hubbackup.BUSY_EVERY_S = 0
        assert hb.tick()
        _backup_wait(hb)
        assert ("GET", "/api/backup/latest?date=20261118") in fake.calls
        assert not list(target.glob("pcs-hub-*")), list(target.iterdir())
        assert hb.st.get("fail_days", []) == [] and hb.st.get("last") is None and hb.next_try > clock["t"]
        # later, Apex closed: done
        hb.busy = lambda: None
        clock["t"] = hb.next_try + 1
        assert hb.tick()
        _backup_wait(hb)
        assert (target / "pcs-hub-20261118.zip").read_bytes() == fake.zips["20261118"]
    finally:
        hubbackup.system_drive, hubbackup.BUSY_EVERY_S = real_sd, real_every
        _backup_done(srv, fake)


def test_hub_backup_not_for_players():
    """A player's key: no Settings block, settings ignored, Copy now refused, the backup route never asked."""
    hubbackup, hub, fake, b, hb, srv, clock, target = _backup_setup(PLAYER_KEY)
    try:
        fake.zips = {"20261118": _zip_bytes("x")}
        hb.drive_exists = lambda p: True                       # even with an E: drive
        assert not hb.tick()
        assert hb.role() == "player" and fake.backup_calls() == []
        assert call(srv, "/api/state")[1]["hubBackup"] is None
        code, r, _ = call(srv, "/api/settings", {"hub_backup_dir": str(target), "hub_backup_keep": 3})
        assert code == 200 and r["hubBackup"] is None and not hubbackup.STATE_FILE.exists()
        code, r, _ = call(srv, "/api/hubbackup/copy", {})
        assert code == 403 and "coach" in r["error"] and fake.backup_calls() == []
        assert not target.exists()
        # the page's code: the block only when the state has it
        js = (Path(pcs_server.WEB) / "app.js").read_text(encoding="utf-8")
        assert "S.hubBackup ? hubBackupHTML(S.hubBackup) : ''" in js and "Copy now" in js and "Hub backup copy" in js
        # no hub address: nothing
        b.auto.s.hub_url = ""
        assert call(srv, "/api/state")[1]["hubBackup"] is None
    finally:
        _backup_done(srv, fake)


def test_hub_backup_failure_message_rule():
    """One short line in Activity only when copies failed 3 days in a row; once; a working copy resets it."""
    hubbackup, hub, fake, b, hb, srv, clock, target = _backup_setup()
    real_sd = hubbackup.system_drive
    hubbackup.system_drive = lambda: "Q:"
    try:
        hb.role()
        hb.set(str(target), 14)
        fake.fail = 500
        said = lambda: [m["text"] for m in b.messages if "Hub backup" in m["text"]]
        for day in range(2):                                   # day 1 twice (retry), day 2
            hb._run("daily")
            hb._run("daily")
            assert said() == [] and hb.next_try > clock["t"]
            clock["t"] += 86400
        hb._run("daily")                                       # day 3
        assert said() == ["Hub backup copy failed: hub error 500"], said()
        assert call(srv, "/api/state")[1]["hubBackup"]["last"]["error"] == "hub error 500"
        clock["t"] += 86400
        hb._run("daily")                                       # day 4: told already
        assert len(said()) == 1
        # it works again: reset; then two failed days, a day off, one more: not 3 in a row
        fake.fail, fake.zips = 0, {"20261122": _zip_bytes("ok")}
        hb._run("daily")
        assert hb.st["fail_days"] == [] and "told" not in hb.st
        fake.fail = 500
        for gap in (1, 1, 2):
            clock["t"] += gap * 86400
            hb._run("daily")
        assert len(said()) == 1
        clock["t"] += 86400                                    # now 2 in a row... and a third
        hb._run("daily")
        hb._run("daily")
        assert len(said()) == 1
        clock["t"] += 86400
        hb._run("daily")
        assert len(said()) == 2 and said()[-1] == "Hub backup copy failed: hub error 500"
        # the hub writing tonight's zip (409): waits, not a failure day
        n = len(hb.st["fail_days"])
        fake.fail = 409
        clock["t"] += 86400
        hb._run("daily")
        assert len(hb.st["fail_days"]) == n and hb.next_try == clock["t"] + 900
        assert "Hub backup copy failed: " == hubbackup.FAILED
    finally:
        hubbackup.system_drive = real_sd
        _backup_done(srv, fake)


if __name__ == "__main__":
    try:
        code = run_all([(n, f) for n, f in list(globals().items()) if n.startswith("test_")])
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
