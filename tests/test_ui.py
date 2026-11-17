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
    assert cur.read_bytes() == b"MZ new version" and started == [[str(cur)]]
    assert "copied, starting the new version" in Path(core.UPDATE_LOG).read_text()


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


if __name__ == "__main__":
    try:
        code = run_all([(n, f) for n, f in list(globals().items()) if n.startswith("test_")])
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
