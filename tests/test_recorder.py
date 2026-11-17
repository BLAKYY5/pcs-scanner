"""Tests for the recorder's logic with a fake OBS, fake Apex process and fake scanner.
Run:  python tests/test_recorder.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["PCS_DATA_DIR"] = str(TMP / "appdata")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from pcs_recorder import retention, settings  # noqa: E402
from pcs_recorder.core import Recorder  # noqa: E402

GB = 1024 ** 3
UNIT = 1024 ** 2      # tests use MB-sized files (Windows has no sparse files) and scale the limit to match


def make_file(folder: Path, name: str, size: int, age_days: float = 0) -> Path:
    p = folder / name
    with open(p, "wb") as f:
        f.truncate(size)
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))
    return p


def test_retention_rules():
    folder = TMP / "ret"
    folder.mkdir()
    idx = {}
    for i, (age, status, size) in enumerate([(10, "uploaded", 1), (5, "uploaded", 30), (4, "recorded", 30),
                                              (2, "uploaded", 30), (1, "recorded", 30), (0, "recording", 30)]):
        p = make_file(folder, f"rec{i}.mp4", size * UNIT)
        idx[f"k{i}"] = retention.Entry(path=str(p), started=time.time() - age * 86400, status=status)
    other = make_file(folder, "my own clip.mp4", 50 * UNIT)        # not ours: must survive
    removed = retention.enforce(idx, keep_days=7, max_gb=100 * UNIT / GB, protect={"k5"})
    names = [n for n, _ in removed]
    assert names[0] == "rec0.mp4", removed                          # older than 7 days
    # 150 units left > 100: oldest *uploaded* goes before older unscanned ones
    assert "rec1.mp4" in names and "rec3.mp4" in names and "rec2.mp4" not in names, removed
    assert "k5" in idx and other.exists()
    assert retention.folder_usage(idx) <= 100 * UNIT


class FakeOBS:
    def __init__(self, folder):
        self.folder, self.rec, self.n = folder, False, 0

    def ensure_ready(self):
        pass

    def start(self):
        self.rec = True

    def stop(self):
        self.rec = False
        self.n += 1
        return str(make_file(self.folder, f"Apex Legends_{self.n}.mp4", 3 * UNIT))


class FakeHub:
    def __init__(self):
        self.reports, self.uploads, self.down = [], [], False

    def report(self, d):
        self.reports.append(d["status"])

    def upload(self, files):
        if self.down:
            from pcs_recorder.core import NeedsSignIn
            raise NeedsSignIn("Your Google sign-in has expired")
        self.uploads.append([f.name for f in files])
        return ["the team hub"]


class FakeUI:
    def __init__(self, answer=True):
        self.answer, self.asked, self.notes = answer, 0, []

    def ask_scan(self, *a):
        self.asked += 1
        return self.answer

    def notify(self, t, m, sticky=False, action=None):
        self.notes.append((t, m, sticky, action))


def fake_scan(video, out):
    """Stands in for PCS-Scanner.exe --scan: a process with a worker child, writes two result files."""
    import subprocess
    p = TMP / "fake_scanner.py"
    p.write_text("import sys,time,pathlib,subprocess\nout=pathlib.Path(sys.argv[2])\n"
                 "w=subprocess.Popen([sys.executable,'-c','import time;time.sleep(1.5)']);w.wait()\n"
                 "(out/'game1.feed.json').write_text('{}');(out/'session.scan.json').write_text('{}')\n")
    return subprocess.Popen([sys.executable, str(p), video, out])


def run_session(scan_answer=True, hub_down=False):
    retention.INDEX_FILE.unlink(missing_ok=True)      # each test starts with an empty recordings list
    s = settings.load()
    s.folder = str(TMP / f"vids-{time.time_ns()}")
    s.enabled = True
    apex = {"on": False}
    hub, ui = FakeHub(), FakeUI(scan_answer)
    hub.down = hub_down
    obs = FakeOBS(s.folder_path)
    r = Recorder(s, obs, hub, ui, fake_scan, is_apex_running=lambda: apex["on"])
    r._final_file = lambda p, wait_s=0: p
    r.tick()
    assert not obs.rec
    apex["on"] = True; r.tick()
    assert obs.rec and r.status == "Recording Apex"
    apex["on"] = False; r.tick()                      # game closed: stop + prompt + scan starts
    assert not obs.rec and ui.asked == 1
    return r, apex, hub, ui


def test_full_session_scan_and_upload():
    r, apex, hub, ui = run_session()
    assert r.scan_proc is not None
    time.sleep(0.8)                                   # let the scanner start its worker process
    apex["on"] = True; r.tick()                       # player relaunches: scan pauses, no upload
    assert "paused" in r.status
    import psutil
    root = psutil.Process(r.scan_proc.pid)
    tree = [root] + root.children(recursive=True)
    assert len(tree) >= 2 and all(p.status() == psutil.STATUS_STOPPED for p in tree), [p.status() for p in tree]
    time.sleep(2.5); r.tick()
    assert r.scan_proc is not None and hub.uploads == []   # still suspended, nothing sent mid-game
    apex["on"] = False                                # closes again: 2nd session recorded, its scan waits in line
    for _ in range(120):                              # up to 60 s: process start-up is slow on Windows
        r.tick(); time.sleep(0.5)
        if len(hub.uploads) == 2:
            break
    def core_files(u):
        return sorted(x for x in u if not x.endswith(".events.json"))
    assert len(hub.uploads) == 2 and all(core_files(u) == ["game1.feed.json", "scan.log", "session.scan.json"] or
                                         core_files(u) == ["game1.feed.json", "session.scan.json"] for u in hub.uploads), (
        hub.uploads, {k: vars(v) for k, v in r.idx.items()}, r.queue, r.scan_proc and r.scan_proc.poll(), r.paused,
        [p.pid for p in r.suspended], r.status)
    assert [e.status for e in r.idx.values()] == ["uploaded", "uploaded"], {k: vars(v) for k, v in r.idx.items()}
    titles = [n[0] for n in ui.notes]
    for t in ("Scanning your session", "Scan finished", "Results sent"):
        assert any(x.startswith(t) for x in titles), titles      # the player is told at every step
    assert hub.reports[:3] == ["recording", "recorded", "uploaded"] or "uploaded" in hub.reports


def test_hotkeys_marks_and_mutes():
    """Marks and mutes during a recording are saved with exact times; mutes reach OBS; nothing outside a recording."""
    import json
    retention.INDEX_FILE.unlink(missing_ok=True)
    s = settings.load()
    s.folder = str(TMP / f"vids-{time.time_ns()}")
    s.enabled, s.comms_consent = True, True
    assert s.tracks() == ["game", "discord", "mic"]
    apex = {"on": False}
    obs = FakeOBS(s.folder_path)
    calls = []
    obs.mute = lambda track, m: (calls.append((track, m)), track in s.tracks())[1]
    clock = {"t": 0.0}
    obs.elapsed = lambda: clock["t"]
    r = Recorder(s, obs, FakeHub(), FakeUI(False), fake_scan, is_apex_running=lambda: apex["on"])
    r._final_file = lambda p, wait_s=0: p
    assert r.hotkey("mark") is None                    # not recording: hotkeys do nothing
    apex["on"] = True; r.tick()
    clock["t"] = 61.5; assert r.hotkey("mark") == "mark"
    clock["t"] = 70.0; assert r.hotkey("mute_mic") == "muted"
    clock["t"] = 75.25; assert r.hotkey("mute_mic") == "unmuted"
    assert r.hotkey("mute_nothing") is None
    apex["on"] = False; r.tick()
    e = list(r.idx.values())[0]
    data = json.loads(Path(e.path).with_suffix(".events.json").read_text())
    assert data["tracks"] == ["game", "discord", "mic"]
    assert data["events"] == [{"t": 61.5, "kind": "mark"}, {"t": 70.0, "kind": "mute", "track": "mic", "muted": True},
                              {"t": 75.25, "kind": "mute", "track": "mic", "muted": False}], data
    assert calls[:2] == [("mic", True), ("mic", False)]
    apex["on"] = True; r.tick()                        # next recording starts clean
    assert r.events == [] and r.muted == {}
    # without the team's agreement only the game is recorded
    s.comms_consent = False
    assert s.tracks() == ["game"]
    assert settings.parse_hotkey("ctrl+alt+m") and settings.parse_hotkey("m") is None
    assert settings.parse_hotkey("shift+1") is None   # needs ctrl, alt or win
    assert settings.parse_hotkey("f5") and settings.parse_hotkey("shift+f8") and settings.parse_hotkey("num7")
    assert settings.parse_hotkey("pause") and settings.Settings(hk_mark="F5").clamp().hk_mark == "f5"
    assert settings.Settings(hk_mark="shift+x").clamp().hk_mark == ""


def test_youtube_blocks():
    """Recordings close together form one block -> one video with chapters; uploads wait for Apex to close."""
    import json
    import shutil
    import subprocess
    from pcs_recorder import youtube
    youtube.STATE_FILE.unlink(missing_ok=True)
    folder = TMP / f"yt-{time.time_ns()}"
    folder.mkdir()
    base = time.time() - 4 * 3600
    have_ffmpeg = shutil.which("ffmpeg") is not None
    idx = {}
    # block 1: two recordings (Apex restarted after 10 min), block 2: one recording 2 h later
    for key, start, length, mode in (("a", 0, 40, "scrim"), ("b", 30 * 60, 40, "scrim"), ("c", 2 * 3600, 40, "ranked")):
        v = folder / f"Apex Legends_{key}.mp4"
        if have_ffmpeg:
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=s=160x90:d={length}",
                            "-f", "lavfi", "-i", f"sine=440:d={length}", "-f", "lavfi", "-i", f"sine=880:d={length}",
                            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "mpeg4", "-g", "30", "-c:a", "aac",
                            "-shortest", str(v)], check=True)
        else:
            v.write_bytes(b"x")
        end = base + start + 10 * 60
        os.utime(v, (end, end))
        scan = v.with_suffix(".scan")
        scan.mkdir()
        drop = {"spots": [{"poi": "x", "kind": "enemy"}] * 20}
        (scan / (v.stem + ".scan.json")).write_text(json.dumps({"games": [{"map": "BM", "mode": mode, "tStart": 2.0, "drop": drop},
                                                                          {"map": "WE", "mode": mode, "tStart": 20.0, "drop": drop}]}))
        v.with_suffix(".events.json").write_text(json.dumps({"tracks": ["game", "mic"],
                                                             "events": [{"t": 5.0, "kind": "mark"}]}))
        idx[key] = retention.Entry(path=str(v), started=base + start, status="scanned")
    assert youtube.blocks(idx, 45) == [["a", "b"], ["c"]]
    assert youtube.blocks(idx, 10) == [["a"], ["b"], ["c"]]

    s = settings.Settings(player="Blakey", folder=str(folder), yt_enabled=True, yt_wait_eec=False)
    title, desc, n = youtube.describe(s, idx, ["a", "b"], [0.0, 40.0], links=["https://eternalesports.club/lobbies/4/e9483b"])
    assert "EEC lobby: https://eternalesports.club/lobbies/4/e9483b" in desc and "Sound: game only." in desc
    assert youtube.normalize_eec("eternalesports.club/lobbies/4/e9483b") == "https://eternalesports.club/lobbies/4/e9483b"
    assert youtube.normalize_eec("https://evil.example/eternalesports.club") is None
    g = {"drop": {"spots": [{"poi": "bm-stasis-array", "kind": "ours"}, {"poi": "bm-the-foundry", "kind": "enemy"}]}}
    assert youtube.our_drop(g) == "Stasis Array"
    assert n == 4 and title.startswith("Scrims ") and "Blakey POV" in title and "Broken Moon, World's Edge" in title, title
    assert "0:00 Game 1 · Broken Moon\n0:20 Game 2 · World's Edge\n0:42 Game 3 · Broken Moon\n1:00 Game 4" in desc, desc
    assert "Marked moments: 0:05, 0:45" in desc

    class Rec:
        current = None
    rec = Rec()
    rec.idx = idx
    ui = FakeUI()
    apex = {"on": True}
    sent = []

    class Req:
        def __init__(self, body, media):
            self.body, self.media, self.n = body, media, 0

        def next_chunk(self, num_retries=0):
            self.n += 1
            if self.n < 2:
                class P:
                    def progress(self):
                        return 0.5
                return P(), None
            sent.append((self.body, Path(self.media._filename).stat().st_size))
            return None, {"id": f"vid{len(sent)}"}

    class YT:
        def videos(self):
            return self

        def insert(self, part, body, media_body):
            return Req(body, media_body)
    service = {"yt": None}
    up = youtube.Uploader(s, rec, ui, lambda: service["yt"], lambda: apex["on"])
    up.tick()
    assert up.thread is None                                   # Apex running: nothing happens
    apex["on"] = False
    s.yt_since = base + 3 * 3600                               # turned on after these blocks: not uploaded
    assert up.ready_blocks() == []
    s.yt_since = 0
    up.tick()                                                  # not signed in: told once, with a button
    assert ui.notes[-1][0] == "YouTube upload waiting" and ui.notes[-1][3] == "ytsignin"
    up.tick()
    assert len([x for x in ui.notes if x[0] == "YouTube upload waiting"]) == 1
    assert up.state["c"]["status"] == "skipped"                # ranked only: skipped
    service["yt"] = YT()
    up.tick()
    assert up.protected() == {"a", "b"}                        # kept until uploaded
    up.thread.join(120)
    if not have_ffmpeg:
        print("  (no ffmpeg: video building not tested)")
        return
    assert up.state["a"]["status"] == "done" and up.state["a"]["url"] == "https://youtu.be/vid1", up.state
    body, size = sent[0]
    assert body["status"]["privacyStatus"] == "unlisted" and body["snippet"]["categoryId"] == "20"
    assert size > 0 and not list(folder.glob("youtube-*"))     # the joined file is deleted after the upload
    assert ui.notes[-1][0] == "Scrim block on YouTube"
    up.tick()
    assert len(sent) == 1                                      # never twice
    # the joined video: both recordings, one sound track (game only)
    out = folder / "check.mp4"
    youtube.build_video([idx["a"].path, idx["b"].path], [["game", "mic"]] * 2, "game", out)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration", "-of",
                            "json", str(out)], capture_output=True, text=True).stdout
    info = json.loads(probe)
    assert [x["codec_type"] for x in info["streams"]] == ["video", "audio"], info
    assert 79 < float(info["format"]["duration"]) < 81.5, info
    youtube.build_video([idx["a"].path], [["game", "mic"]], "mix", out)
    assert json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json",
                                      str(out)], capture_output=True, text=True).stdout)["streams"][1]["codec_type"] == "audio"
    # EEC link: the upload waits for it (asks once), then the link is in the description
    s.yt_wait_eec, s.yt_wait_eec_h = True, 12
    d = folder / "Apex Legends_d.mp4"
    shutil.copy(idx["a"].path, d)
    (d.with_suffix(".scan")).mkdir()
    shutil.copy(Path(idx["a"].path).with_suffix(".scan") / "Apex Legends_a.scan.json", d.with_suffix(".scan") / "Apex Legends_d.scan.json")
    shutil.copy(Path(idx["a"].path).with_suffix(".events.json"), d.with_suffix(".events.json"))
    os.utime(d, (time.time() - 3000, time.time() - 3000))
    idx["d"] = retention.Entry(path=str(d), started=time.time() - 3600, status="scanned")
    up.tick()
    assert up.thread is None or not up.thread.is_alive() or up.state.get("d", {}).get("status") != "waiting"
    asks = [x for x in ui.notes if x[0] == "EEC lobby link for YouTube"]
    assert len(asks) == 1 and asks[0][3] == "eeclink"
    up.tick()
    assert len([x for x in ui.notes if x[0] == "EEC lobby link for YouTube"]) == 1   # asked once
    try:
        up.set_links("d", ["not a link"])
    except Exception:
        pass
    assert up.set_links("d", ["https://eternalesports.club/lobbies/7/abc123"]) == ["https://eternalesports.club/lobbies/7/abc123"]
    up.tick(); up.thread.join(120)
    assert up.state["d"]["status"] == "done" and up.state["d"]["eec"], up.state["d"]
    assert "EEC lobby: https://eternalesports.club/lobbies/7/abc123" in sent[-1][0]["snippet"]["description"]
    assert "EEC" in sent[-1][0]["snippet"]["tags"] and "Blakey" in sent[-1][0]["snippet"]["tags"]
    # by hand: the ranked block can still be uploaded
    # an upload cut by quitting the app is judged again at the next start, not left "uploading" forever
    youtube.save_state({"zz": {"keys": ["zz"], "status": "uploading"}})
    assert youtube.Uploader(s, rec, ui, lambda: None, lambda: False).state["zz"]["status"] is None
    youtube.save_state(up.state)
    assert up.request("c") == ["c"] and up.state["c"]["status"] == "requested"
    up.tick(); up.thread.join(120)
    assert up.state["c"]["status"] == "done"


def test_scrim_needs_proof():
    """Older scans called every game without a ranked badge 'scrim': a whole ranked session was uploaded as a
    scrim block. A scrim now needs the scrim drop screen to have been read."""
    from pcs_recorder.youtube import is_scrim
    assert is_scrim({"mode": "scrim", "drop": {"spots": [1] * 20}})
    assert not is_scrim({"mode": "scrim", "drop": None})          # old scan, no drop screen: not a scrim
    assert not is_scrim({"mode": "ranked", "drop": {"spots": [1]}})


def test_eec_results():
    """EEC lobby export -> standing, points (ALGS), kills; attached to the block played then; in the description."""
    import json
    from pcs_recorder import eec, youtube
    youtube.STATE_FILE.unlink(missing_ok=True)
    t0 = time.time() - 5 * 3600

    def row(team, player, place, kills):
        return {"team_name": team, "player_name": player, "team_placement": place, "kills": kills}
    export = {"matches": [
        {"map_name": "mp_rr_divided_moon_mu1", "match_start": t0 + 300, "data": [
            row("PCS", "a", 2, 3), row("PCS", "b", 2, 1), row("Other", "c", 1, 2), row("Third", "d", 3, 0)]},
        {"map_name": "mp_rr_desertlands_hu", "match_start": t0 + 2400, "data": [
            row("PCS", "a", 1, 5), row("Other", "c", 3, 0), row("Third", "d", 2, 4)]}]}
    f = TMP / "lobbies_4_e9483b_scores.json"
    f.write_text(json.dumps(export))
    lobby = eec.read_file(str(f))
    assert lobby["url"] == "https://eternalesports.club/lobbies/4/e9483b" and lobby["games"][0]["map"] == "BM"
    r = eec.results(lobby, "pcs")
    # game 1: 2nd = 9 + 4 kills = 13; game 2: 1st = 12 + 5 = 17 -> 30 pts, 9 kills, 1st of 3
    assert (r["standing"], r["teams"], r["pts"], r["kills"], r["tied"]) == (1, 3, 30, 9, False), r
    assert [(g["place"], g["pts"]) for g in r["games"]] == [(2, 13), (1, 17)]
    assert eec.results(lobby, "Nobody") is None
    try:
        eec.from_export({"nope": 1})
        raise AssertionError("bad file accepted")
    except ValueError:
        pass
    assert [eec.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22)] == ["1st", "2nd", "3rd", "4th", "11th", "12th",
                                                                          "13th", "21st", "22nd"]
    # attach to the block recorded at that time
    folder = TMP / f"eec-{time.time_ns()}"
    folder.mkdir()
    v = folder / "Apex Legends_x.mp4"
    v.write_bytes(b"x")
    os.utime(v, (t0 + 3600, t0 + 3600))
    sc = v.with_suffix(".scan")
    sc.mkdir()
    (sc / "Apex Legends_x.scan.json").write_text(json.dumps({"games": [
        {"map": "BM", "mode": "scrim", "tStart": 290.0, "drop": {"spots": [{"poi": "bm-stasis-array", "kind": "ours"}]}},
        {"map": "WE", "mode": "scrim", "tStart": 2390.0}]}))

    class Rec:
        current = None
    rec = Rec()
    rec.idx = {"x": retention.Entry(path=str(v), started=t0, status="scanned")}
    s = settings.Settings(player="Blakey", yt_team="PCS")
    up = youtube.Uploader(s, rec, FakeUI(), lambda: None, lambda: False)
    got = up.add_results(lobby)
    assert got["block"] == ["x"] and up.results_of("x")["pts"] == 30
    assert up.links_of("x") == ["https://eternalesports.club/lobbies/4/e9483b"]      # the link comes with the file
    st = up.state["x"]
    title, desc, n = youtube.describe(s, rec.idx, ["x"], [0.0], links=st["eec"], results=st["eec_res"])
    assert "POV · 1st/3, 30 pts ·" in title, title
    assert "Result: 1st of 3 · 30 points · 9 kills · 2 games (ALGS scoring)" in desc, desc
    assert "4:50 Game 1 · Broken Moon · Stasis Array · 2nd, 13 pts" in desc and "39:50 Game 2 · World's Edge · 1st, 17 pts" in desc, desc
    assert "Games: G1 2nd (13 pts, 4 kills) · G2 1st (17 pts, 5 kills)" in desc
    # a lobby played at another time is refused
    late = json.loads(json.dumps(export))
    for g in late["matches"]:
        g["match_start"] += 3 * 86400
    try:
        up.add_results(eec.from_export(late))
        raise AssertionError("lobby from another day attached")
    except RuntimeError:
        pass


def test_night_uploads_and_shutdown():
    """Night mode: uploads only at night when idle; PC turned off only after a night upload with nothing left."""
    import datetime as dt
    from pcs_recorder.night import Night, in_window
    assert in_window(dt.datetime(2026, 11, 7, 2), 1, 7) and not in_window(dt.datetime(2026, 11, 7, 8), 1, 7)
    assert in_window(dt.datetime(2026, 11, 6, 23), 23, 7) and in_window(dt.datetime(2026, 11, 7, 3), 23, 7)
    s = settings.Settings(yt_enabled=True, yt_when="night", night_from=1, night_to=7, night_idle_min=20).clamp()

    class Rec:
        current, scan_proc, queue, idx, youtube = None, None, [], {}, None
    rec, ui = Rec(), FakeUI()
    clock = {"t": dt.datetime(2026, 11, 6, 23, 30).timestamp()}
    idle = {"s": 3600.0}
    cmds, awake = [], []
    n = Night(s, rec, ui, idle=lambda: idle["s"], clock=lambda: clock["t"], runner=cmds.append, awake=awake.append)
    assert not n.upload_allowed()                     # 23:30: not night yet
    clock["t"] = dt.datetime(2026, 11, 7, 2, 0).timestamp()
    idle["s"] = 5 * 60
    assert not n.upload_allowed()                     # someone used the PC 5 min ago
    idle["s"] = 30 * 60
    assert n.upload_allowed()
    s.night_shutdown = True
    n.tick(apex=False)
    assert cmds == []                                 # nothing uploaded tonight: the PC isn't turned off
    n.next_check = 1e12                               # (the once-a-minute check is tested below)
    n.uploading(True); assert awake == [True]         # kept awake during the upload
    rec.scan_proc = object()
    n.uploading(False); n.tick(apex=False)
    assert awake == [True, False] and cmds == []      # a scan still runs
    rec.scan_proc = None
    n.tick(apex=True)
    assert cmds == []                                 # Apex open
    n.tick(apex=False)
    assert cmds and cmds[0][:2] == ["shutdown", "/s"] and ui.notes[-1][3] == "cancelshutdown"
    idle["s"] = 2                                     # the player moves the mouse
    n.tick(apex=False)
    assert cmds[-1] == ["shutdown", "/a"] and n.shutdown_at is None
    idle["s"] = 3600
    n.tick(apex=False)
    assert len(cmds) == 2                             # not again that night
    s.yt_when = "after_block"
    assert n.upload_allowed()
    # a block waiting for the night keeps the PC awake in the evening, and lets it sleep once sent

    class YT:
        blocks = [["a"]]
        thread = None

        def ready_blocks(self):
            return self.blocks
    rec.youtube = YT()
    s.yt_when, s.night_shutdown = "night", False
    awake.clear(); n.awake_on = False; n.next_check = 0
    n.tick(apex=False)
    assert awake == [True]
    rec.youtube.blocks = []
    n.next_check = 0; n.tick(apex=False)
    assert awake == [True, False]


def test_encoder_candidates_skip_av1_on_older_cards():
    """AV1 is only tried on cards that can encode it, so OBS never shows "couldn't start recording" for it."""
    from pcs_recorder.obs import encoder_candidates, has_av1_encoder
    assert not has_av1_encoder("nvidia geforce rtx 3070") and has_av1_encoder("nvidia geforce rtx 4070 super")
    assert has_av1_encoder("amd radeon rx 9070 xt") and not has_av1_encoder("amd radeon rx 6700 xt")
    assert has_av1_encoder("intel(r) arc(tm) a770") and has_av1_encoder("nvidia geforce rtx 5080")
    c = encoder_candidates("nvidia", "nvidia geforce rtx 3060")
    assert c[0] == "obs_nvenc_hevc_tex" and not any("av1" in e for e in c)
    assert encoder_candidates("amd", "amd radeon rx 7800 xt")[0] == "av1_texture_amf"
    assert not any("qsv" in e or "amf" in e for e in c)                  # no other vendor's encoder: OBS would complain
    both = encoder_candidates("nvidia", "intel(r) uhd graphics 770\nnvidia geforce rtx 4060")
    assert both[0] == "obs_nvenc_av1_tex" and "obs_qsv11_v2" in both and not any("amf" in e for e in both)


def test_no_game_recordings():
    """A recording the scan is sure has no game (no game, no kill-feed line) is flagged, deleted after
    nogame_keep_h and first when space is needed; a recording with a single feed line is kept."""
    import json
    from pcs_recorder.core import no_game
    folder = TMP / f"nogame-{time.time_ns()}"
    folder.mkdir()
    now = time.time()

    def rec(name, games, feed, age_h, size=UNIT):
        v = folder / f"{name}.mp4"
        v.write_bytes(b"\0" * size)
        os.utime(v, (now - age_h * 3600, now - age_h * 3600))
        (v.with_suffix(".scan")).mkdir()
        (v.with_suffix(".scan") / f"{name}.scan.json").write_text(json.dumps({"games": games, "perf": {"feedLines": feed, "layoutVotes": {"16:9": 500}}}))
        e = retention.Entry(path=str(v), started=now - age_h * 3600 - 60, status="uploaded")
        e.nogame = no_game(e)
        return e
    idx = {"menus": rec("menus", [], 0, 30), "lobby": rec("lobby", [], 0, 2), "range": rec("range", [], 3, 30),
           "game": rec("game", [{"map": "WE"}], 40, 30)}
    assert [k for k, e in idx.items() if e.nogame] == ["menus", "lobby"]
    removed = retention.enforce(idx, keep_days=7, max_gb=100, nogame_keep_h=24, now=now)
    assert [r[0] for r in removed] == ["menus.mp4"] and set(idx) == {"lobby", "range", "game"}
    # space needed: the no-game recording goes first even though it's the newest
    removed = retention.enforce(idx, keep_days=7, max_gb=2.5 * UNIT / 1024 ** 3, nogame_keep_h=24, now=now)
    assert [r[0] for r in removed] == ["lobby.mp4"], removed
    # 0 h: right away
    idx["x"] = rec("x", [], 0, 0)
    assert [r[0] for r in retention.enforce(idx, 7, 100, nogame_keep_h=0, now=now + 1)] == ["x.mp4"]
    # a scan that can't be read is never treated as "no game"
    assert no_game(retention.Entry(path=str(folder / "missing.mp4"), started=now)) is False


def test_half_written_mp4_is_never_scanned():
    """OBS copies the .mkv into an .mp4 after the session; a half-written .mp4 that stops growing for a moment
    must not be taken (a 2-hour session was once scanned as 'no game' that way). The .mkv is used instead."""
    import json
    folder = TMP / f"final-{time.time_ns()}"
    folder.mkdir()
    mkv, mp4 = folder / "Apex Legends_x.mkv", folder / "Apex Legends_x.mp4"
    mkv.write_bytes(b"\0" * 10 * UNIT)
    mp4.write_bytes(b"\0" * 4 * UNIT)                      # stuck at 40 %
    s = settings.load()
    r = Recorder(s, FakeOBS(folder), FakeHub(), FakeUI(), fake_scan, is_apex_running=lambda: False)
    assert r._final_file(str(mkv), wait_s=0.5, poll=0.1) == str(mkv) and mkv.exists()
    mp4.write_bytes(b"\0" * 10 * UNIT)                     # complete now
    assert r._final_file(str(mkv), wait_s=2, poll=0.1) == str(mp4) and not mkv.exists()
    # a scan that never looked at a frame is a failed scan, not "no game"
    from pcs_recorder.core import no_game, read_nothing
    e = retention.Entry(path=str(mp4), started=time.time(), status="scanned")
    sc = mp4.with_suffix(".scan")
    sc.mkdir()
    (sc / "Apex Legends_x.scan.json").write_text(json.dumps({"games": [], "perf": {"feedLines": 0, "layoutVotes": {"4:3": 0, "16:9": 0}}}))
    assert read_nothing(e) and not no_game(e)
    (sc / "Apex Legends_x.scan.json").write_text(json.dumps({"games": [], "perf": {"feedLines": 0, "layoutVotes": {"16:9": 900}}}))
    assert not read_nothing(e) and no_game(e)
    # scan again: queued, flags cleared
    key = "rec-x"
    r.idx[key] = e
    e.nogame = True
    assert r.rescan(key) and e.status == "waiting" and not e.nogame and r.queue[-1] == key
    assert not r.rescan(key)                               # already queued


def test_encoder_test_restarts_obs_per_candidate():
    """OBS only reads the encoder at start-up: each candidate is tried in a freshly started OBS."""
    from pcs_recorder import obs as obsmod
    calls = []

    class Fake:
        def __init__(self, s):
            self.s = s

        def connect(self):
            calls.append("connect")

        def prepare(self, w, h, enc):
            self.enc = enc

        @property
        def c(self):
            me = self

            class C:
                def start_record(self):
                    calls.append(("start", current["enc"]))
            return C()

        def recording(self):
            return current["enc"] == "obs_nvenc_h264_tex"

        def stop(self):
            return None
    current = {}
    saved = (obsmod.OBS, obsmod.close, obsmod.write_config, obsmod.launch, obsmod.gpu_names, obsmod.time.sleep)
    obsmod.OBS = Fake
    obsmod.close = lambda: calls.append("close")
    obsmod.write_config = lambda s, enc, w, h: (calls.append(("config", enc)), current.update(enc=enc))
    obsmod.launch = lambda: calls.append("launch")
    obsmod.gpu_names = lambda: "nvidia geforce rtx 3060"
    obsmod.time.sleep = lambda x: None
    try:
        enc = obsmod.pick_encoder(settings.Settings(), 1920, 1080, progress=lambda m: None)
    finally:
        obsmod.OBS, obsmod.close, obsmod.write_config, obsmod.launch, obsmod.gpu_names, obsmod.time.sleep = saved
    assert enc == "obs_nvenc_h264_tex"
    configs = [c[1] for c in calls if isinstance(c, tuple) and c[0] == "config"]
    starts = [c[1] for c in calls if isinstance(c, tuple) and c[0] == "start"]
    assert configs == starts == ["obs_nvenc_hevc_tex", "jim_hevc_nvenc", "obs_nvenc_h264_tex"], calls
    assert calls.count("close") == 3 and calls.count("launch") == 3
    s = obsmod.encoder_settings("obs_nvenc_hevc_tex", 12)
    assert s["preset2"] == "p5" and "preset" not in s and s["bitrate"] == 12000 and s["keyint_sec"] == 1
    assert obsmod.encoder_settings("av1_texture_amf", 12)["preset"] == "quality"


def test_skip_and_offline_hub():
    r, apex, hub, ui = run_session(scan_answer=False)
    assert r.scan_proc is None and list(r.idx.values())[0].status == "skipped"
    r2, apex2, hub2, ui2 = run_session(hub_down=True)
    for _ in range(8):
        r2.tick(); time.sleep(0.5)
    assert list(r2.idx.values())[0].status == "scanned" and not hub2.uploads   # kept for retry
    fails = [n for n in ui2.notes if n[0] == "Results not sent yet"]
    assert len(fails) == 1 and fails[0][2] is True, ui2.notes                # said once, stays on screen
    assert fails[0][3] == "signin" and "expired" in fails[0][1]              # with a Sign in button
    hub2.down = False; r2.next_upload = 0; r2.tick()
    assert list(r2.idx.values())[0].status == "uploaded"


def test_pause_never_stacks():
    """Ticks while Apex runs must not pause the scanner again (on Windows pauses stack), and one that
    finished while paused must not be resumed or crash anything."""
    import subprocess, psutil
    retention.INDEX_FILE.unlink(missing_ok=True)
    s = settings.load(); s.folder = str(TMP / "stack"); s.enabled = True
    apex = {"on": False}
    hub, ui = FakeHub(), FakeUI(True)
    r = Recorder(s, FakeOBS(s.folder_path), hub, ui, fake_scan, is_apex_running=lambda: apex["on"])
    r._final_file = lambda p, wait_s=0: p
    apex["on"] = True; r.tick(); apex["on"] = False; r.tick()          # one session, scan starts
    time.sleep(0.8)
    calls = {"suspend": 0, "resume": 0}
    real_s, real_r = psutil.Process.suspend, psutil.Process.resume
    psutil.Process.suspend = lambda self: (calls.__setitem__("suspend", calls["suspend"] + 1), real_s(self))[1]
    psutil.Process.resume = lambda self: (calls.__setitem__("resume", calls["resume"] + 1), real_r(self))[1]
    try:
        apex["on"] = True
        for _ in range(5):
            r.tick()                                                     # five checks while Apex runs
        paused_once = calls["suspend"]
        assert paused_once >= 1 and len(r.suspended) == paused_once
        apex["on"] = False; r.tick()
        assert calls["resume"] == paused_once and not r.suspended and not r.paused, calls
    finally:
        psutil.Process.suspend, psutil.Process.resume = real_s, real_r
    for _ in range(60):
        r.tick(); time.sleep(0.5)
        if hub.uploads:
            break
    assert hub.uploads, "scan never finished after resume"


def test_mkv_duplicate_removed():
    from pcs_recorder.core import drop_original
    d = TMP / "remux"; d.mkdir()
    mkv = make_file(d, "Apex Legends_x.mkv", 10 * UNIT)
    part = make_file(d, "Apex Legends_x.mp4", 4 * UNIT)            # remux still running: keep the original
    assert not drop_original(part) and mkv.exists()
    make_file(d, "Apex Legends_x.mp4", 10 * UNIT + 1000)           # remux done: the .mkv is a duplicate
    assert drop_original(d / "Apex Legends_x.mp4") and not mkv.exists()


def test_settings_limits():
    s = settings.Settings(keep_days=0, max_gb=5, capture="weird").clamp()
    assert (s.keep_days, s.max_gb, s.capture) == (1, settings.MIN_MAX_GB, "window")


def test_quality_setting():
    import json
    from pcs_recorder import obs as obsmod
    assert settings.Settings(bitrate_mbps=4).clamp().bitrate_mbps == settings.MIN_MBPS == 8
    assert settings.Settings(bitrate_mbps=500).clamp().bitrate_mbps == settings.MAX_MBPS
    s = settings.Settings(folder=str(TMP / "q"))
    assert s.bitrate_mbps == 12 and 5 < s.gb_per_hour < 6
    obsmod.write_config(s, "av1_texture_amf", 2560, 1440)
    enc = json.loads((obsmod.CONFIG / "basic" / "profiles" / "PCS" / "recordEncoder.json").read_text())
    assert enc["rate_control"] == "CBR" and enc["bitrate"] == 12000 and enc["keyint_sec"] == 1, enc
    ini = (obsmod.CONFIG / "basic" / "profiles" / "PCS" / "basic.ini").read_text()
    assert "RecEncoder=av1_texture_amf" in ini and "FPSInt=60" in ini and "BaseCX=2560" in ini
    s.bitrate_mbps = 16
    assert obsmod.config_signature(s) != obsmod.config_signature(settings.Settings(folder=s.folder))


def test_message_window_placement():
    """Runs where a screen exists (the Windows build machine); skipped elsewhere."""
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception:
        print("  (no display: message window test skipped)")
        return
    root.withdraw()
    from pcs_recorder.runtime import UI
    ui = UI(root)
    ui.actions["signin"] = ("Sign in with Google", lambda: None)
    for sticky, action in ((False, None), (True, "signin")):
        ui._toast("Results not sent yet", "Your Google sign-in has expired. They are kept and sent later.", sticky, action)
        w = ui.toast_win
        root.update()
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        x, y, ww, wh = w.winfo_rootx(), w.winfo_rooty(), w.winfo_width(), w.winfo_height()
        assert ww >= 360 and wh >= 60, (ww, wh)                        # a real window, not an empty box
        assert x > sw / 2 and y > sh / 2, (x, y, sw, sh)                # bottom-right corner, not top-left
        assert x + ww <= sw and y + wh <= sh, (x, y, ww, wh, sw, sh)    # fully on screen
        def walk(x):
            for c in x.winfo_children():
                yield c
                yield from walk(c)
        texts = [c.cget("text") for c in walk(w) if isinstance(c, (tk.Label, tk.Button))]
        assert any("expired" in t for t in texts), texts
        assert (action is None) or any(t == "SIGN IN WITH GOOGLE" for t in texts), texts
    assert len([t for t in ui.toasts if t.winfo_exists()]) == 2          # stacked, not replaced
    a, b = [t for t in ui.toasts if t.winfo_exists()]
    root.update()
    assert a.winfo_rooty() + a.winfo_height() <= b.winfo_rooty(), "messages overlap"
    for i in range(4):
        ui._toast(f"Message {i}", "x", False)
    assert len([t for t in ui.toasts if t.winfo_exists()]) <= 3
    ui.TOAST_S = 0.3                                                   # disappears on its own
    ui._toast("Short", "bye", False)
    short = ui.toasts[-1]
    end = time.time() + 5
    while short.winfo_exists() and time.time() < end:
        root.update()
        time.sleep(0.02)
    assert not short.winfo_exists()
    root.destroy()


def test_labels_and_youtube_queue():
    """Session labels (scrims / ranked / pubs / wildcard) decide what goes to YouTube; the queue page can upload
    now, skip, put back and set a title and privacy per block."""
    import json
    from pcs_recorder import labels, youtube
    youtube.STATE_FILE.unlink(missing_ok=True)
    labels.FILE.unlink(missing_ok=True)
    folder = TMP / f"ytq-{time.time_ns()}"
    folder.mkdir()
    base = time.time() - 6 * 3600
    idx = {}
    drop = {"spots": [{"poi": "x", "kind": "enemy"}]}
    for key, start, games in (("p", 0, [{"map": "SP", "mode": "unknown", "tStart": 5}]),          # pubs: no proof
                              ("s", 2 * 3600, [{"map": "WE", "mode": "scrim", "tStart": 5, "drop": drop}]),
                              ("r", 4 * 3600, [{"map": "SP", "mode": "ranked", "tStart": 5}])):
        v = folder / f"Apex Legends_{key}.mp4"
        v.write_bytes(b"x")
        end = base + start + 600
        os.utime(v, (end, end))
        v.with_suffix(".scan").mkdir()
        (v.with_suffix(".scan") / (v.stem + ".scan.json")).write_text(json.dumps({"games": games}))
        idx[key] = retention.Entry(path=str(v), started=base + start, status="scanned")
    # automatic labels from the scan
    assert labels.of(idx["s"].path, youtube.scan_of(idx["s"])) == ("scrims", False)
    assert labels.of(idx["r"].path, youtube.scan_of(idx["r"])) == ("ranked", False)
    assert labels.of(idx["p"].path, youtube.scan_of(idx["p"])) == (None, False)
    labels.set_label(idx["p"].path, "pubs")
    assert labels.of(idx["p"].path, {}) == ("pubs", True)
    try:
        labels.set_label(idx["p"].path, "casual")
        assert False, "unknown label accepted"
    except ValueError:
        pass

    class Rec:
        current = None
    rec = Rec()
    rec.idx = idx
    s = settings.Settings(player="Xno", folder=str(folder), yt_enabled=True, yt_wait_eec=False)
    up = youtube.Uploader(s, rec, FakeUI(), lambda: None, lambda: False)
    assert up.ready_blocks() == [["s"]]
    assert up.state["p"]["why"] == "labelled pubs" and up.state["r"]["why"] == "no scrim game"
    q = {b["id"]: b for b in up.queue()}
    assert [b["id"] for b in up.queue()] == ["r", "s", "p"]                  # newest first
    assert q["p"]["status"] == "skipped" and q["p"]["label"] == "pubs" and q["p"]["label_by_hand"]
    assert q["s"]["status"] == "pending" and q["s"]["why"] == "ready" and q["s"]["maps"] == ["World's Edge"]
    # the ranked block was really scrims (no drop screen read): labelled by hand, it's judged again
    labels.set_label(idx["r"].path, "scrims")
    up.relabelled("r")
    assert up.state["r"]["status"] is None and ["r"] in up.ready_blocks()
    # a scrim labelled wildcard by hand isn't uploaded
    labels.set_label(idx["s"].path, "wildcard")
    up.relabelled("s")
    assert ["s"] not in up.ready_blocks() and up.state["s"]["why"] == "labelled wildcard"
    # queue actions
    up.act("p", "upload")
    assert up.state["p"]["status"] == "requested" and up.ready_blocks()[0] == ["p"]   # asked for by hand: first
    up.act("r", "skip")
    assert up.state["r"]["status"] == "skipped" and up.state["r"]["by_hand"]
    assert ["r"] not in up.ready_blocks()
    up.act("r", "edit", title="  Scrims night 2 · Xno POV ", privacy="private")
    assert up.state["r"]["custom_title"] == "Scrims night 2 · Xno POV" and up.state["r"]["privacy"] == "private"
    up.act("r", "unskip")
    assert up.state["r"]["status"] is None and up.state["r"]["custom_title"] == "Scrims night 2 · Xno POV"
    assert ["r"] in up.ready_blocks()
    try:
        up.act("r", "edit", privacy="everyone")
        assert False
    except RuntimeError:
        pass
    up.state["r"]["status"] = "done"
    for action in ("upload", "skip"):
        try:
            up.act("r", action)
            assert False, action
        except RuntimeError:
            pass
    # the title word follows the label
    title, desc, _ = youtube.describe(s, idx, ["p"], [0.0], kind="pubs")
    assert title.startswith("Pubs ") and "Pubs session" in desc, title
    title, _, _ = youtube.describe(s, idx, ["s"], [0.0])
    assert title.startswith("Scrims "), title
    assert "EEC" not in youtube.tags_for(s, desc, "pubs") and "pubs" in youtube.tags_for(s, desc, "pubs")


def test_microphone_choice():
    """The picked microphone goes to OBS's mic source, and changing it restarts OBS before the next recording."""
    from pcs_recorder import obs as obsmod
    s = settings.Settings()
    assert s.mic_device == "default"
    sig = obsmod.config_signature(s)
    s.mic_device = "{0.0.1.00000000}.{8d5c2f9a-1111-2222-3333-444455556666}"
    assert obsmod.config_signature(s) != sig
    devs = obsmod.mic_devices()
    assert isinstance(devs, list) and all({"id", "name"} <= set(d) for d in devs)
    if os.name == "nt":
        assert all(d["id"].startswith("{0.0.1.00000000}.{") for d in devs), devs
    # prepare() creates the mic source with that device
    created = {}

    class C:
        def __getattr__(self, name):
            def f(*a, **k):
                if name == "create_input":
                    created[a[1]] = a[3]
                if name == "get_scene_list":
                    return type("x", (), {"scenes": []})()
                if name == "get_input_list":
                    return type("x", (), {"inputs": []})()
                if name == "get_special_inputs":
                    return type("x", (), {})()
            return f
    s.comms_consent = True
    o = obsmod.OBS(s)
    o.c = C()
    o.prepare(1920, 1080)
    assert created["PCS Mic"]["device_id"] == s.mic_device, created


def test_voice_app_choice():
    """Track 2 records the voice app the player picked: Discord (default, unchanged), TeamSpeak (classic and new
    client both listened to), or another program given by its name only. Changing it restarts OBS."""
    import json
    from pcs_recorder import obs as obsmod
    v = settings.valid_exe
    assert v("Mumble.exe") == "Mumble.exe" and v(" TeamSpeak.exe ") == "TeamSpeak.exe" and v("ts3client_win64.exe")
    for bad in ("", "mumble", "C:\\x\\Mumble.exe", "..\\Mumble.exe", "a/b.exe", "x.exe & calc", "x.exe\"", "a:b.exe",
                "#x.exe", "r5apex_dx12.exe", "OBS64.EXE", "x" * 70 + ".exe", None, 5):
        assert v(bad) is None, bad
    # defaults: Discord, same OBS source text as before, same signature as before this setting existed
    s = settings.Settings(comms_consent=True)
    assert s.voice_app == "discord" and s.voice_exes() == ["Discord.exe"] and s.voice_name() == "Discord"
    assert obsmod.voice_sources(s) == [("PCS Discord", {"window": obsmod.DISCORD_WINDOW, "priority": 2})]
    sig = obsmod.config_signature(s)
    assert sig == f"{s.encoder}|{s.bitrate_mbps}|{s.folder}|game,discord,mic|{s.mic_device}"

    def prepared(s):
        created, tracks, mutes = {}, {}, []

        class C:
            def __getattr__(self, name):
                def f(*a, **k):
                    if name == "create_input":
                        created[a[1]] = (a[2], a[3])
                    if name == "set_input_audio_tracks":
                        tracks[a[0]] = [n for n, on in a[1].items() if on]
                    if name == "set_input_mute":
                        mutes.append(a)
                    if name == "get_scene_list":
                        return type("x", (), {"scenes": []})()
                    if name == "get_input_list":
                        return type("x", (), {"inputs": []})()
                    if name == "get_special_inputs":
                        return type("x", (), {})()
                return f
        o = obsmod.OBS(s)
        o.c = C()
        o.prepare(1920, 1080)
        return o, created, tracks, mutes
    o, created, tracks, _ = prepared(s)
    assert created["PCS Discord"] == ("wasapi_process_output_capture",
                                      {"window": "Discord:Chrome_WidgetWin_1:Discord.exe", "priority": 2})
    assert "PCS Voice 2" not in created and tracks["PCS Discord"] == ["2"] and tracks["PCS Mic"] == ["3"]
    # TeamSpeak: both clients on track 2, matched by program (title and class unknown: left empty)
    s.voice_app = "teamspeak"
    assert s.clamp().voice_name() == "TeamSpeak" and obsmod.config_signature(s) != sig
    o, created, tracks, mutes = prepared(s)
    assert created["PCS Discord"] == ("wasapi_process_output_capture", {"window": "::ts3client_win64.exe", "priority": 2})
    assert created["PCS Voice 2"] == ("wasapi_process_output_capture", {"window": "::TeamSpeak.exe", "priority": 2})
    assert tracks["PCS Discord"] == tracks["PCS Voice 2"] == ["2"] and tracks["PCS Mic"] == ["3"]
    mutes.clear()
    assert o.mute("discord", True) and mutes == [("PCS Discord", True), ("PCS Voice 2", True)]
    # another program
    s.voice_app, s.voice_exe = "other", "Mumble.exe"
    sig_ts = obsmod.config_signature(s)
    assert s.clamp().voice_name() == "Mumble" and s.voice_exes() == ["Mumble.exe"]
    o, created, tracks, _ = prepared(s)
    assert created["PCS Discord"] == ("wasapi_process_output_capture", {"window": "::Mumble.exe", "priority": 2})
    assert "PCS Voice 2" not in created
    s.voice_exe = "Vesktop.exe"
    assert obsmod.config_signature(s) not in (sig, sig_ts)
    # "other" without a valid program: no voice track (the mic moves up), never something else recorded
    s.voice_exe = "C:\\evil\\x.exe"
    s.clamp()
    assert s.voice_exe == "" and s.voice_exes() == [] and s.tracks() == ["game", "mic"]
    _, created, tracks, _ = prepared(s)
    assert "PCS Discord" not in created and tracks["PCS Mic"] == ["2"]
    # unknown app falls back to Discord; the voice track needs the team's consent, whatever the app
    assert settings.Settings(voice_app="skype").clamp().voice_app == "discord"
    assert settings.Settings(voice_app="teamspeak", comms_consent=False).tracks() == ["game"]
    # saved and loaded like the other settings
    settings.save(settings.Settings(voice_app="other", voice_exe="Mumble.exe"))
    saved = json.loads(settings.SETTINGS_FILE.read_text(encoding="utf-8"))
    assert saved["voice_app"] == "other" and saved["voice_exe"] == "Mumble.exe" and "discord_audio" in saved
    s2 = settings.load()
    assert (s2.voice_app, s2.voice_exe) == ("other", "Mumble.exe")
    # running programs with a window: names only, valid ones
    apps = obsmod.window_apps()
    assert isinstance(apps, list) and all(settings.valid_exe(a["exe"]) for a in apps), apps
    print("  programs with a window:", [a["exe"] for a in apps][:12])
    settings.save(settings.Settings())


def test_sound_ring_moving():
    """The ring alarm (steady 374 + 533 Hz tones, pulsing) is found in noisy game sound, and matched to the rings
    read on screen; other tones and plain noise don't trigger it."""
    import subprocess
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_audio as A
    sr = A.SR
    rng = np.random.default_rng(1)
    a = rng.normal(0, 0.05, sr * 120).astype(np.float32)            # 2 min of noise
    t = np.arange(int(sr * 1.2)) / sr
    pulse = 0.04 * (np.sin(2 * np.pi * 374.1 * t) + np.sin(2 * np.pi * 532.9 * t))
    for start in (30.0, 80.0):                                       # two alarms, three pulses each
        for k in range(3):
            i = int((start + 1.75 * k) * sr)
            a[i:i + len(pulse)] += pulse
    other = 0.05 * np.sin(2 * np.pi * 440 * np.arange(sr * 3) / sr)   # a different tone
    a[int(55 * sr):int(58 * sr)] += other
    found = A.cues(a)["ring_moving"]
    assert [round(c["t"]) for c in found] == [30, 80], found
    assert all(c["pulses"] == 3 for c in found), found
    games = [{"tStart": 0, "ring": [{"kind": "wait", "endsAt": 29.5}, {"kind": "closing", "endsAt": 50}]}]
    A.attach(games, {"cues": {"ring_moving": found}})
    assert abs(games[0]["ring"][0]["soundAt"] - 30) < 0.5 and "soundAt" not in games[0]["ring"][1]
    assert [round(x) for x in games[0]["ringSound"]] == [80]
    # read from a real file through ffmpeg
    wav = TMP / "alarm.wav"
    import wave
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())
    try:
        r = A.scan(str(wav))
    except FileNotFoundError:
        return                                                       # no ffmpeg here
    if "error" not in r:
        assert [round(c["t"]) for c in r["cues"]["ring_moving"]] == [30, 80], r


def test_scanner_no_function_defined_twice():
    """A second function with the same name silently replaces the first one (the scanner's files are long): a new
    helper once took the name of the pre-ring clock's helper and every scan failed. Caught here instead."""
    import ast
    import collections
    for f in sorted((Path(__file__).resolve().parent.parent / "scanner").glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        names = collections.Counter(n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)))
        twice = [k for k, v in names.items() if v > 1]
        assert not twice, f"{f.name}: defined twice: {twice}"


def test_feed_weapon_icon():
    """A kill line gets the name of its weapon icon (and whether the red arrow follows it); a line without an icon
    gets none; without the private icons file the feed is captured exactly as before."""
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_hud as HUD
    rng = np.random.default_rng(3)

    def shape(h, w):                                                 # a made-up icon: blocky white picture
        s = (rng.random((h // 2, w // 3)) > 0.45).repeat(2, 0).repeat(3, 1)[:h, :w]
        return np.pad((s * 255).astype(np.uint8), ((2, 2), (3, 3)))
    icons = {"alpha_gun": shape(20, 54), "beta_gun": shape(24, 36)}
    f = TMP / "weapons.npz"
    np.savez_compressed(f, **icons)

    def letters(img, x, y, n):                                       # stands in for a name: short white strokes
        for k in range(n):
            img[y + (0 if k % 2 else 9):y + (11 if k % 2 else 20), x + 7 * k:x + 7 * k + 3] = 255
        return x + 7 * n

    def frame(icon=None, arrow=False, y=300):
        img = np.zeros((1440, 2560, 3), np.uint8)
        x = letters(img, 2000, y, 14) + 18
        if icon is not None:
            t = icons[icon]; img[y - 2:y - 2 + t.shape[0], x:x + t.shape[1]] = t[..., None]; x += t.shape[1] + 12
        if arrow: img[y + 2:y + 18, x:x + 12] = (230, 40, 30); x += 24
        letters(img, 2470 - 7 * 16, y, 16)
        return img

    feed = HUD.Feed(HUD.Weapons(str(f)))
    feed.read(frame("alpha_gun", arrow=True, y=300), 10.0)
    feed.read(frame("beta_gun", y=360), 40.0)
    feed.read(frame(None, y=420), 70.0)
    assert len(feed.lines) == 3, feed.lines
    a, b, c = feed.lines
    assert a["weapon"] == "alpha_gun" and a["arrow"] is True and a["weaponScore"] >= 0.8, a
    assert a["event"] == "knock", a                                  # red arrow = knock
    assert b["weapon"] == "beta_gun" and b["arrow"] is False and b["weaponName"] == "Beta Gun", b
    assert b["event"] == "kill", b                                   # a weapon without the arrow = kill
    assert "weapon" not in c and set(c) == {"t", "y", "jpg"}, c
    assert a["marker"] == "arrow" and a["killer"] == {"side": "other"}, a

    # who is on each side: our squad's names are coloured, everyone else's are white; gold skull = headshot kill
    ORANGE, GREEN, WHITE = (255, 165, 40), (150, 215, 60), (255, 255, 255)

    def kill_line(killer, victim, marker=None, icon="alpha_gun", y=300, shift=0):
        img = np.zeros((1440, 2560, 3), np.uint8)

        def name(x, n, colour):
            for k in range(n):
                img[y + (0 if k % 2 else 9):y + (11 if k % 2 else 20), x + 7 * k:x + 7 * k + 3] = colour
            return x + 7 * n
        x = name(1960 + shift, 14, killer) + 18
        t = icons[icon]; img[y - 2:y - 2 + t.shape[0], x:x + t.shape[1]] = t[..., None]; x += t.shape[1] + 22
        if marker == "arrow": img[y + 1:y + 19, x:x + 12] = (230, 40, 30)
        if marker == "skull": img[y - 6:y + 28, x - 6:x + 24] = (240, 180, 60)
        name(x + (34 if marker else 4), 16 if marker else 19, victim)  # without a marker the name starts right after the icon
        img[y + 9:y + 20, 2440:2468:7] = 255                          # something at the right edge, as on the real HUD
        img[y + 9:y + 20, 2441:2469:7] = 255
        return img
    feed = HUD.Feed(HUD.Weapons(str(f)))
    feed.read(kill_line(ORANGE, WHITE, "arrow", y=300), 100.0)                 # we knock someone
    feed.read(kill_line(ORANGE, WHITE, None, y=380), 130.0)                    # then the kill
    feed.read(kill_line(WHITE, GREEN, "arrow", icon="beta_gun", y=420), 150.0) # one of us goes down
    feed.read(kill_line(GREEN, WHITE, "skull", icon="beta_gun", y=460), 190.0) # headshot kill by another of us
    feed.read(kill_line(WHITE, WHITE, None, y=500), 230.0)                     # nothing to do with us
    L = [{k: v for k, v in l.items() if k != "jpg"} for l in feed.lines if "event" in l]
    assert [l["event"] for l in L] == ["knock", "kill", "knock", "kill", "kill"], L
    assert [(l["killer"]["side"], l["victim"]["side"]) for l in L] == [("squad", "other")] * 2 + [("other", "squad"), ("squad", "other"), ("other", "other")], L
    assert L[0]["killer"]["color"] == "orange" and L[2]["victim"]["color"] == "green" and L[3]["killer"]["color"] == "green", L
    assert L[3].get("headshot") is True and L[3]["marker"] == "skull" and "headshot" not in L[1], L
    want = {"squadKnocks": 1, "squadKills": 2, "squadKnocked": 1, "squadKilled": 0, "headshotKills": 1}
    assert HUD.feed_stats(L, 0, 1000) == want, HUD.feed_stats(L, 0, 1000)
    again = sorted(L + [dict(L[0], t=104.0, y=340), dict(L[2], t=158.0)], key=lambda l: l["t"])   # the feed scrolls: the
    assert HUD.feed_stats(again, 0, 1000) == want                    # same line caught twice is counted once
    assert HUD.feed_stats(L, 0, 140)["squadKills"] == 1              # only the lines of that game's time window
    # with the legend select read, each slot's colour is the colour of that player's name in the feed
    squad = [{"slot": 1, "legend": "alpha", "self": True, "hue": L[0]["killer"]["hue"] + 4},
             {"slot": 2, "legend": "bravo", "self": False, "hue": L[2]["victim"]["hue"] - 3},
             {"slot": 3, "legend": "charlie", "self": False, "hue": 180}]
    pl = HUD.feed_stats(L, 0, 1000, squad)["players"]
    assert [(q["legend"], q["knocks"], q["kills"], q["knocked"], q["headshotKills"]) for q in pl] == \
        [("alpha", 1, 1, 0, 0), ("bravo", 0, 1, 1, 1), ("charlie", 0, 0, 0, 0)], pl
    assert "players" not in HUD.feed_stats(L, 0, 1000) and pl[0]["self"] is True
    old = {k: os.environ.pop(k, None) for k in ("PCS_WEAPON_ICONS", "PCS_DATA_DIR")}
    try:                                                             # the app's data folder is where the app puts the file
        d = TMP / "data-icons"; d.mkdir(exist_ok=True)
        os.environ["PCS_DATA_DIR"] = str(d)
        assert HUD.weapon_file() in (None, str(Path(HUD.HERE) / "pcs_weapons.npz"))
        np.savez_compressed(d / "pcs_weapons.npz", **icons)
        assert HUD.weapon_file() == str(d / "pcs_weapons.npz") and len(HUD.Weapons().T) == 2
    finally:
        for k, v in old.items():
            os.environ.pop(k, None)
            if v is not None: os.environ[k] = v
    assert HUD.WEAPON_NAMES["r97"] == "R-99"
    # a dual-wield icon is the single one drawn twice: the single icon fits it too, the bigger one must win
    one = icons["beta_gun"]; two = np.concatenate([one, one[:, 3:]], 1)
    f2 = TMP / "weapons-dual.npz"
    np.savez_compressed(f2, beta_gun=one, beta_gun_akimbo_active=two)
    W2 = HUD.Weapons(str(f2))
    line = np.zeros((40, 400, 3), np.uint8); line[6:6 + two.shape[0], 150:150 + two.shape[1]] = two[..., None]
    assert W2.match(line)["weapon"] == "beta_gun_akimbo_active", W2.match(line)
    line = np.zeros((40, 400, 3), np.uint8); line[6:6 + one.shape[0], 150:150 + one.shape[1]] = one[..., None]
    assert W2.match(line)["weapon"] == "beta_gun", W2.match(line)
    plain = HUD.Feed(HUD.Weapons(str(TMP / "missing.npz")))          # no icons file: same lines, no weapon
    plain.read(frame("alpha_gun", arrow=True), 10.0)
    assert len(plain.lines) == 1 and set(plain.lines[0]) == {"t", "y", "jpg"}


def test_legend_select_starts_a_game():
    """The legend select screen starts a game and gives each squad member's legend: the last read of a slot whose
    frame is coloured (picking or locked in) wins, the recording player is the slot whose name is not white, rank
    badges mean ranked. One odd frame, or a look-alike drawn at another height, starts nothing."""
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_hud as HUD
    import pcs_scan as SCAN
    rng = np.random.default_rng(11)
    names = ["alpha", "bravo", "charlie", "delta"]
    tiles = {n: np.clip(rng.integers(1, 255, (16, 25)).repeat(4, 0).repeat(4, 1), 1, 255).astype(np.uint8) for n in names}
    f = TMP / "legends.npz"
    np.savez_compressed(f, some_gun=np.full((20, 40), 200, np.uint8), **{"legend_" + n: t for n, t in tiles.items()})
    assert [t[0] for t in HUD.Weapons(str(f)).T] == ["some_gun"]              # portraits are not weapons
    L = HUD.Legends(str(f))
    assert sorted(t[0] for t in L.T) == names
    COL = [(240, 160, 40), (40, 180, 190), (140, 200, 50)]

    def screen(picks, locked=(True, True, True), own=1, ranked=True, dy=0, dx=0, bg=(60, 60, 60)):
        img = np.full((1440, 2560, 3), bg, np.uint8)
        for k, n in enumerate(picks):
            y0 = int(round(172 + 106.5 * k)) + dy
            img[y0 + 9:y0 + 73, 215 + dx:315 + dx] = tiles[n][..., None]                 # the portrait
            img[y0 + 60:y0 + 80, 300 + dx:328 + dx] = COL[k]                             # numbered badge, always coloured
            img[y0 + 64:y0 + 76, 312 + dx:316 + dx] = 255                                # its white digit
            img[y0 + 30:y0 + 46, 196 + dx:204 + dx] = COL[k] if locked[k] else (200, 200, 200)   # left edge of the frame
            for x in range(330 + dx, 520 + dx, 6): img[y0 + 10:y0 + 30, x:x + 3] = COL[k] if k == own else (255, 255, 255)
        if ranked: img[170:254:6, 72:156] = 230
        return img
    d = L.read(screen(["alpha", "bravo", "charlie"]).astype(np.int16))
    assert d and [s["legend"] for s in d["squad"]] == ["alpha", "bravo", "charlie"], d
    assert [s["self"] for s in d["squad"]] == [False, True, False] and d["ranked"] is True, d
    assert all(s["locked"] for s in d["squad"]) and d["squad"][0]["hue"] != d["squad"][1]["hue"], d
    assert L.read(screen(["alpha", "bravo", "charlie"], dy=40).astype(np.int16)) is None     # the lobby's party list
    d = L.read(screen(["delta", "alpha"], ranked=False, dx=-114, own=0).astype(np.int16))    # not ranked: no rank badges,
    assert d and [s["legend"] for s in d["squad"]] == ["delta", "alpha"] and d["ranked"] is False, d   # roster further left
    assert [s["self"] for s in d["squad"]] == [True, False], d
    assert L.read(np.full((1440, 2560, 3), 60, np.int16)) is None
    assert HUD.Legends(str(TMP / "missing.npz")).read(screen(["alpha", "bravo"]).astype(np.int16)) is None

    # a whole screen: slot 3 looks at "delta" first, locks "charlie", then the screen fades (slot read without lock)
    old = os.environ.get("PCS_WEAPON_ICONS"); os.environ["PCS_WEAPON_ICONS"] = str(f)
    try:
        frames = [(50.0, screen(["delta", "delta", "delta"]))]                # one odd frame long before: ignored
        frames += [(100.0 + k, screen(["alpha", "bravo", "delta"], locked=(True, k > 3, False), ranked=False)) for k in range(8)]
        frames += [(108.0 + k, screen(["alpha", "bravo", "charlie"], ranked=False)) for k in range(6)]
        frames += [(114.0, screen(["alpha", "bravo", "delta"], locked=(True, True, False), ranked=False))]
        events, samples, _ = SCAN.scan_frames(frames, quiet=True)
    finally:
        os.environ.pop("PCS_WEAPON_ICONS", None)
        if old is not None: os.environ["PCS_WEAPON_ICONS"] = old
    assert [e["kind"] for e in events] == ["select"] * 16, [e["kind"] for e in events]
    games = SCAN.assemble(events)
    assert len(games) == 1, games
    g = games[0]; sel = g["select"]
    assert g["tStart"] == 114.0 and g["map"] == "?" and g["drop"] is None, g
    assert (sel["tFirst"], sel["tLast"], sel["frames"]) == (100.0, 114.0, 15), sel
    assert [s["legend"] for s in sel["squad"]] == ["alpha", "bravo", "charlie"], sel
    assert [s["self"] for s in sel["squad"]] == [False, True, False] and sel["ranked"] is False, sel
    assert not any(k.startswith("_") for k in sel), sel
    SCAN.timeline(games, samples)
    assert g["mode"] == "unknown", g["mode"]

    # the select screen's background is often a saturated orange, right behind the frames and the names. The frame
    # and the own name are recognised by the badge's colour, not by a median hue over a window that is mostly
    # background (7 Nov, t 4206: slot 3 locked octane over orange read as "not coloured" on every frame with the GPU
    # decoder and on one frame with the CPU decoder, so the scan said pathfinder or octane depending on the decoder)
    ORANGE = (176, 98, 55)
    for bg_dx in (0, -114):                      # ranked, and other modes (roster further left, no rank badges)
        d = L.read(screen(["alpha", "bravo", "charlie"], bg=ORANGE, dx=bg_dx, ranked=bg_dx == 0))
        assert d and [s["legend"] for s in d["squad"]] == ["alpha", "bravo", "charlie"], (bg_dx, d)
        assert [s["locked"] for s in d["squad"]] == [True, True, True], (bg_dx, d)
        assert [s["self"] for s in d["squad"]] == [False, True, False], (bg_dx, d)
        d = L.read(screen(["alpha", "bravo", "charlie"], locked=(True, False, False), bg=ORANGE, dx=bg_dx, ranked=bg_dx == 0))
        assert d and [s["locked"] for s in d["squad"]] == [True, False, False], (bg_dx, d)    # orange player, grey frame
    def jitter(img, seed):                       # the other decoder: same picture, each pixel a few levels off
        r = np.random.default_rng(seed)
        return np.clip(img.astype(np.int16) + r.integers(-6, 7, img.shape), 0, 255).astype(np.uint8)
    seq = [(4181.0 + k, screen(["alpha", "bravo", "delta"], locked=(k > 6, k > 14, False), bg=ORANGE)) for k in range(18)]
    seq += [(4199.0 + k, screen(["alpha", "bravo", "charlie"], bg=ORANGE)) for k in range(8)]
    for seed in (None, 1, 2):
        evs = [{"t": t, "kind": "select", "data": L.read(img if seed is None else jitter(img, seed * 100 + int(t)))} for t, img in seq]
        assert all(e["data"] for e in evs), seed
        sel = None
        for e in HUD.select_runs(evs):
            sel = HUD.new_select(e["data"], e["t"]) if sel is None else HUD.merge_select(sel, e["data"], e["t"])
        assert [s["legend"] for s in sel["squad"]] == ["alpha", "bravo", "charlie"], (seed, sel)
        assert [s["self"] for s in sel["squad"]] == [False, True, False] and sel["frames"] == 26, (seed, sel)

    # the loading banner just before the legend select names the mode and the map in big white letters
    def word(img, y0, y1, seed, n):                                  # made-up block letters starting at x = 194
        r = np.random.default_rng(seed); h = y1 - y0
        for k in range(n):
            x = 194 + k * (h + 8)
            img[y0:y1, x:x + h] = 255
            for _ in range(2):
                yy, xx = int(r.integers(0, h - h // 3)), int(r.integers(h // 4, h - h // 3))
                img[y0 + yy:y0 + yy + h // 3, x + xx:x + h] = 60
    def banner(mode_seed, mode_n, map_seed, map_n):
        img = np.full((1440, 2560, 3), 60, np.uint8)
        word(img, 138, 198, mode_seed, mode_n); word(img, 273, 298, map_seed, map_n)
        return img
    b = HUD.Banner.read(banner(1, 6, 2, 9))
    assert b and b["mode"] is None and b["map"] is None and len(b["modeSig"][0]) == 150, b    # unknown words: their shapes
    assert HUD.Banner.read(np.full((1440, 2560, 3), 60, np.uint8)) is None
    assert HUD.Banner.read(screen(["alpha", "bravo", "charlie"])) is None
    keep = HUD.BANNER_T
    try:
        # the table's mode keys are the scanner's modes: the same word gives wildcard or pubs (pubs trios says TRIOS)
        for name in ("wildcard", "pubs"):
            HUD.BANNER_T = {"mode": {name: [b["modeSig"]]}, "map": {"SP": [b["mapSig"]]}}
            b2 = HUD.Banner.read(banner(1, 6, 2, 9)); other = HUD.Banner.read(banner(5, 8, 2, 9))
            assert (b2["mode"], b2["map"]) == (name, "SP") and (other["mode"], other["map"]) == (None, "SP"), (b2, other)
            frames = [(80.0 + k, banner(1, 6, 2, 9)) for k in range(6)]
            frames += [(100.0 + k, screen(["alpha", "bravo", "charlie"], ranked=False, dx=-114)) for k in range(12)]
            os.environ["PCS_WEAPON_ICONS"] = str(f)
            try: events, samples, _ = SCAN.scan_frames(frames, quiet=True)
            finally:
                os.environ.pop("PCS_WEAPON_ICONS", None)
                if old is not None: os.environ["PCS_WEAPON_ICONS"] = old
            assert [e["kind"] for e in events] == ["banner"] * 6 + ["select"] * 12, [e["kind"] for e in events]
            games = SCAN.assemble(events); SCAN.timeline(games, samples)
            g = games[0]
            assert len(games) == 1 and g["map"] == "SP" and g["mode"] == name and not g.get("mapGuessed"), g
            assert g["banner"]["mode"] == name and g["banner"]["frames"] == 6 and "modeSig" not in g["banner"], g["banner"]
            from pcs_recorder import labels
            assert labels.auto({"games": games}) == name
        # priority: scrims, ranked, wildcard, then pubs
        assert labels.auto({"games": [{"mode": "pubs"}, {"mode": "wildcard"}]}) == "wildcard"
        assert labels.auto({"games": [{"mode": "pubs"}, {"mode": "ranked"}]}) == "ranked"
        assert labels.auto({"games": [{"mode": "pubs"}, {"mode": "unknown"}]}) == "pubs"
    finally:
        HUD.BANNER_T = keep
    import json as _json
    real = _json.loads((Path(__file__).resolve().parent.parent / "scanner" / "pcs_banner.json").read_text())
    assert {"ranked", "wildcard", "pubs"} <= set(real["mode"]) and set(real["map"]) <= set(SCAN.MAPS), real.keys()
    pubs = real["mode"]["pubs"]
    assert pubs and all(len(s[0]) == 150 and int(s[0], 16) >= 0 for s in pubs), pubs
    cells = lambda a, b: bin(int(a, 16) ^ int(b, 16)).count("1")                # 150 hex = 600 cells
    for other_mode in ("ranked", "wildcard"):
        for s in pubs:
            for o in real["mode"][other_mode]:
                assert cells(s[0], o[0]) > 150, (other_mode, cells(s[0], o[0]))


def _own_put(HUD, img, font, text, x_end=None, x_start=None, gap=2):
    """Draws a number of the own-stats strip with the reader's own digit shapes ('S' small white, 'B' big white,
    'K' dark knocks digits), ending at x_end or starting at x_start. -> (first x, last x)."""
    import numpy as np
    from PIL import Image
    y0, h = {"S": (135, 17), "B": (133, 22), "K": (136, 16)}[font]
    labels, shapes, widths = HUD.OwnStats.T[font]
    gs = []
    for ch in text:
        i = list(labels).index(ch); g = shapes[i].reshape(18, 12)
        gs.append(np.asarray(Image.fromarray((g * 255).astype(np.uint8)).resize((int(round(widths[i])), h), Image.BILINEAR), np.float32) / 255)
    x = x_start if x_start is not None else x_end - sum(g.shape[1] for g in gs) - gap * (len(gs) - 1); x0 = x
    for g in gs:
        box = img[y0:y0 + h, x:x + g.shape[1]].astype(np.float32); ink = g[..., None]
        img[y0:y0 + h, x:x + g.shape[1]] = (box * (1 - ink) + (20 if font == "K" else 255) * ink).astype(np.uint8)
        x += g.shape[1] + gap
    return x0, x - gap


def test_squads_left_counter():
    """The number in front of "SQUADS LEFT" is read digit by digit; per game it only goes down, and a value
    counts once it was read twice, so one bad read never moves it."""
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_hud as HUD
    assert set("0123456789") <= {d for d, _ in HUD.SquadsLeft.T}

    def cells(digit):                                                # a table shape back to a 22 x 14 picture
        bits = next(t for d, t in HUD.SquadsLeft.T if d == digit)
        g = np.array([(bits >> (191 - i)) & 1 for i in range(192)], np.uint8).reshape(16, 12)
        from PIL import Image
        return np.asarray(Image.fromarray(g * 255).resize((14, 22), Image.NEAREST)) > 127

    def hud(number, words=True):
        img = np.full((1440, 2560, 3), 40, np.uint8)
        if words:
            for x in range(2140, 2286, 16): img[77:99, x:x + 4] = 255        # stands in for the words: right place, right density
            img[77:99, 2288:2292] = 255
        x = 2130
        for ch in reversed(str(number)):
            img[77:99, x - 14:x][cells(ch)] = 255; x -= 18
        return img
    for n in (20, 17, 13, 9, 4, 1, 58, 36):
        assert HUD.SquadsLeft.read(hud(n)) == n, (n, HUD.SquadsLeft.read(hud(n)))
    assert HUD.SquadsLeft.read(hud(14, words=False)) is None             # no "SQUADS LEFT" next to it: not the counter
    assert HUD.SquadsLeft.read(np.full((1440, 2560, 3), 250, np.uint8)) is None
    reads = [(10, 20), (11, 20), (30, 19), (31, 19), (40, 7), (50, 18), (52, 18), (60, 19), (70, 17), (300, 16), (310, 17), (311, 17),
             (500, 20), (501, 20)]
    assert HUD.squads_timeline(reads, 0, 400) == [[10, 20], [30, 19], [50, 18], [310, 17]], HUD.squads_timeline(reads, 0, 400)
    assert HUD.squads_timeline(reads, 400, 900) == [[500, 20]]

    # The player's own strip under it: kills (skull), assists (handshake), knocks (dark digits on a white plate,
    # ranked) and damage (its own plate, digits ending at x = 2336). Nothing is at a fixed place: each number is found
    # next to its icon. Synthetic strips: icons as plain blocks, digits drawn from the 'strip' shapes of the table.
    O = HUD.OwnStats
    assert set("0123456789") <= set(O.T["S"][0]) and set("0123456789") <= set(O.T["B"][0]), "digit shapes missing"
    put = lambda *a, **k: _own_put(HUD, *a, **k)

    def strip(kills=None, assists=None, knocks=None, damage=None, font="B", plate=40):
        img = np.full((1440, 2560, 3), plate, np.uint8)
        if damage is None: return img
        d0, _ = put(img, "S", str(damage), x_end=2336)
        img[132:156, d0 - 40:d0 - 3] = 255                                      # damage icon, 37 x 24
        left = d0 - 70
        if knocks is not None:                                                    # white plate, dark icon, dark digits
            img[126:162, left - 105:left] = 250
            img[131:157, left - 80:left - 44] = 20
            put(img, "K", str(knocks), x_start=left - 41)
            left -= 125
        if kills is not None:
            a0 = left - 20 - 15 * len(str(assists))                              # assists digits end 20 px before
            put(img, font, str(assists), x_start=a0)
            img[132:157, a0 - 41:a0 - 3] = 255                                   # handshake, 38 x 25
            k0, _ = put(img, font, str(kills), x_end=a0 - 41 - 24)              # kills end 24 px before the handshake
            img[132:157, k0 - 34:k0 - 9] = 255                                   # skull, 25 x 25, 9 px before
        return img

    assert O.read(strip()) == {}
    assert O.read(np.full((1440, 2560, 3), 250, np.uint8)) == {}
    assert O.read(strip(damage=54)) == {"damage": 54}                                         # damage only
    assert O.read(strip(knocks=2, damage=160)) == {"knocks": 2, "damage": 160}                # knocks plate alone
    ranked = O.read(strip(kills=3, assists=1, knocks=2, damage=716))                          # ranked: all four
    assert ranked == {"kills": 3, "assists": 1, "knocks": 2, "damage": 716}, ranked
    wide = O.read(strip(kills=12, assists=10, knocks=6, damage=2051))                         # the group shifts left
    assert wide == {"kills": 12, "assists": 10, "knocks": 6, "damage": 2051}, wide
    pubs = O.read(strip(kills=2, assists=0, damage=589, font="S"))                            # pubs: no knocks, small digits
    assert pubs == {"kills": 2, "assists": 0, "damage": 589}, pubs
    bright = O.read(strip(kills=4, assists=3, knocks=4, damage=1154, plate=150))              # bright scene behind the plates
    assert bright == {"kills": 4, "assists": 3, "knocks": 4, "damage": 1154}, bright
    img = strip(kills=3, assists=1, knocks=2, damage=716)
    img[135:152, 2310:2318] = 255                                                             # a damage digit smudged
    assert "damage" not in O.read(img) and O.read(img)["kills"] == 3                          # doubtful = no number
    img = strip(kills=3, assists=1, knocks=2, damage=716)
    sk = np.where((img[140, :2100] == 255).all(1))[0]
    img[132:157, sk[0]:sk[0] + 25] = 40                                                       # no skull: no kills
    assert "kills" not in O.read(img) and O.read(img)["assists"] == 1

    # the final value of a game: the value of the last two reads, which must agree; values read twice never go down
    R = [(t, 1) for t in range(10, 20)] + [(20, 7)] + [(t, 2) for t in range(30, 50)] + [(50, 9), (51, 9), (52, 9)]
    assert HUD.stat_final(R, 0, 100) == (9, 50, 52, False), HUD.stat_final(R, 0, 100)
    assert HUD.stat_final(R + [(53, 12)], 0, 100) is None                    # grew in the last second, read once: unknown
    assert HUD.stat_final(R + [(53, 5), (54, 5)], 0, 100) is None            # went down: something was misread
    assert HUD.stat_final(R[:-2], 0, 100) is None and HUD.stat_final(R, 60, 100) is None
    assert HUD.stat_final(R, 0, 100, end=53) == (9, 50, 52, False)
    assert HUD.stat_final(R, 0, 100, end=80) is None                         # stopped being read long before the strip did
    # damage (coach's decision): a last value read once counts when it is above every earlier read and at most
    # SINGLE_JUMP above the read before it; the result says it rests on a single read
    D = [(t, 300) for t in range(10, 20)] + [(20, 340), (21, 340), (22, 375)]
    J = HUD.SINGLE_JUMP
    assert HUD.stat_final(D, 0, 100, None, J) == (375, 22, 22, True), HUD.stat_final(D, 0, 100, None, J)
    assert HUD.stat_final(D, 0, 100) is None                                 # without the rule (kills, assists, knocks)
    assert HUD.stat_final(D[:-1] + [(22, 335)], 0, 100, None, J) is None     # lower than earlier reads: a misread
    assert HUD.stat_final(D[:-1] + [(22, 340 + J + 1)], 0, 100, None, J) is None   # implausible jump: a misread
    assert HUD.stat_final(D[:-3] + [(20, 900), (21, 340), (22, 375)], 0, 100, None, J) is None   # an earlier read was higher
    assert HUD.stat_final([(10, 50), (11, 80)], 0, 100, None, J) is None     # nothing confirmed before it
    assert HUD.stat_final(D, 0, 100, 40, J) is None                          # still refused: last read long before the strip's
    # in the scan: games[].own with kills / assists / knocks / damage (+ At, LastRead, damageSingleRead)
    import pcs_scan as SCAN
    own = [(t, {"kills": 1, "damage": 100 + t}) for t in range(10, 20)] + [(t, {"kills": 2, "assists": 1, "damage": 400}) for t in (40, 41, 42)]
    games = [{"tStart": 0.0, "drop": None, "map": "?", "select": {"tFirst": 0.0}}]
    SCAN.timeline(games, [], own=own)
    assert games[0]["own"] == {"kills": 2, "killsAt": 40, "killsLastRead": 42, "assists": 1, "assistsAt": 40, "assistsLastRead": 42,
                               "damage": 400, "damageAt": 40, "damageLastRead": 42, "damageSingleRead": False}, games[0]["own"]
    own += [(43, {"kills": 3, "assists": 1, "damage": 455})]                  # last second: damage and kills read once
    games = [{"tStart": 0.0, "drop": None, "map": "?", "select": {"tFirst": 0.0}}]
    SCAN.timeline(games, [], own=own)
    o = games[0]["own"]
    assert (o["damage"], o["damageAt"], o["damageSingleRead"]) == (455, 43, True) and "kills" not in o, o


def _tools():
    root = Path(__file__).resolve().parent.parent
    for p in (root / "scanner", root / "tools"):
        if str(p) not in sys.path: sys.path.insert(0, str(p))
    import region_cache, check_set
    return region_cache, check_set


def test_region_cache_round_trip():
    """Developer tool: the region cache keeps the readers' boxes bit for bit, so readers on the cache give exactly
    what they give on the full frame (synthetic frames: noise + a squads counter + an own-stats strip)."""
    import numpy as np
    RC, _ = _tools()
    import pcs_hud as HUD
    rng = np.random.default_rng(7)

    def cells(digit, h, w):
        bits = next(t for d, t in HUD.SquadsLeft.T if d == digit)
        g = np.array([(bits >> (191 - i)) & 1 for i in range(192)], np.uint8).reshape(16, 12)
        from PIL import Image
        return np.asarray(Image.fromarray(g * 255).resize((w, h), Image.NEAREST)) > 127

    def frame(n, kills):
        img = rng.integers(0, 120, (1440, 2560, 3), dtype=np.uint8)          # a noisy scene: no easy compression
        for x in range(2140, 2286, 16): img[77:99, x:x + 4] = 255
        img[77:99, 2288:2292] = 255
        x = 2130
        for ch in reversed(str(n)):
            img[77:99, x - 14:x][cells(ch, 22, 14)] = 255; x -= 18
        img[126:162, 2200:2380] = 40                                          # the damage plate
        d0, _ = _own_put(HUD, img, "S", str(kills * 100 + 7), x_end=2336)
        img[132:156, d0 - 40:d0 - 3] = 255                                   # its icon
        return img
    frames = [(60.0 * k + 0.5 * i, frame(20 - i, 1 + i % 5)) for k in (0, 2) for i in range(3)]   # two minutes, one gap
    with tempfile.TemporaryDirectory() as d:
        wr = RC.Writer(d)
        for t, img in frames: wr.add(t, img)
        wr.close(); RC.write_index(d, recording="synthetic.mp4", frames=len(frames))
        c = RC.Cache(d)
        assert c.minutes == [0, 2] and c.times() == [t for t, _ in frames]
        got = [(t, a.copy()) for t, a in c.frames()]
        assert [t for t, _ in got] == [t for t, _ in frames]
        for (t, a), (_, img) in zip(got, frames):
            for y0, y1, x0, x1 in RC.BOXES.values():
                assert np.array_equal(a[y0:y1, x0:x1], img[y0:y1, x0:x1]), t
            assert a[700:800, 1000:1100].max() == 0                         # outside the boxes: black
            for name in ("squads", "own", "ranked", "banner", "ring", "dbg"):
                assert RC.READERS[name][1](a) == RC.READERS[name][1](img), (t, name)
        assert [RC.READERS["squads"][1](a) for _, a in got] == [20, 19, 18, 20, 19, 18]
        assert RC.READERS["own"][1](got[1][1]) == {"damage": 207}
        # only the boxes a reader needs are decoded, and a time window only opens the minutes it covers
        assert [t for t, _ in c.crops(120, 121, ["counters"])] == [120.0, 120.5] and RC.boxes_for(["squads"]) == ["counters"]
        reads = RC.run_readers(c, ["squads"])
        assert [v for _, v in reads["squads"]] == [20, 19, 18, 20, 19, 18]
        assert RC.summary(reads)[0].startswith("   0 min: squads 3x")


def test_check_set_scoring():
    """Developer tool: the check set scorer counts truths, reads and correct reads per field, at the nearest
    keyframe and within 3 s."""
    _, CS = _tools()
    truths = [
        {"recording": "r.mp4", "t": 100, "squadsLeft": 8, "kills": 3, "damage": 1123},
        {"recording": "r.mp4", "t": 200, "squadsLeft": 14, "kills": 2},
        {"recording": "r.mp4", "t": 300, "legends": ["catalyst", "vantage", "bangalore"], "self": 3, "ranked": True},
        {"recording": "r.mp4", "t": 400, "mode": "ranked", "map": "SP"},
    ]
    sel = {"ranked": True, "squad": [{"slot": 1, "legend": "catalyst", "self": False}, {"slot": 2, "legend": "vantage", "self": False},
                                     {"slot": 3, "legend": "bangalore", "self": True}]}
    data = {   # keyframes: time -> reads
        99.0: {"squads": 8, "own": {"kills": 3}}, 100.0: {"squads": 8, "own": {"kills": 3}},
        199.0: {"squads": 14, "own": {"kills": 2}}, 200.0: {"squads": 41, "own": {}}, 202.0: {"squads": 14, "own": {}},
        300.0: {"legends": sel},
        401.0: {"banner": {"mode": "ranked", "map": None}}, 404.0: {"banner": {"mode": "ranked", "map": "SP"}},
    }

    def reads_at(rec, t0, t1, readers):
        return [(t, {r: v.get(r) for r in readers}) for t, v in sorted(data.items()) if t0 <= t <= t1]
    res = CS.score(truths, reads_at)
    row = lambda f: tuple(res[f][k] for k in ("n", "nearRead", "nearOk", "winRead", "winOk"))
    assert row("squadsLeft") == (2, 2, 1, 2, 2), row("squadsLeft")
    assert row("kills") == (2, 1, 1, 2, 2), row("kills")
    assert row("damage") == (1, 0, 0, 0, 0)
    assert row("legends") == row("self") == row("ranked") == (1, 1, 1, 1, 1)
    assert row("mode") == (1, 1, 1, 1, 1) and row("map") == (1, 0, 0, 0, 0), row("map")   # 404 is outside +-3 s
    assert [m["t"] for m in res["squadsLeft"]["misses"]] == [200] and res["squadsLeft"]["misses"][0]["nearest"] == 41
    assert CS.score(truths, reads_at, fields=["kills"]).keys() == {"kills"}
    assert "squadsLeft" in CS.table(res) and "miss damage" in CS.table(res)
    ex = Path(__file__).resolve().parent.parent / "tools" / "checkset_example.json"
    assert len(CS.load(str(ex))) >= 2


def test_hub_answer_shown_on_recording():
    """What the hub answered for a recording ("Sent to hub: 11 games") is kept on it (Recordings list) and told."""
    from pcs_recorder.runtime import Sent

    class HubWithAnswer(FakeHub):
        def upload(self, files):
            self.uploads.append([f.name for f in files])
            out = Sent(["the team hub"])
            out.hub_note = "Sent to hub: 11 games"
            return out
    retention.INDEX_FILE.unlink(missing_ok=True)
    s = settings.load()
    s.folder = str(TMP / f"vids-{time.time_ns()}")
    v = make_file(s.folder_path, "Apex Legends_hub.mp4", UNIT)
    out = v.with_suffix(".scan")
    out.mkdir()
    (out / (v.stem + ".scan.json")).write_text('{"games": [{"map": "SP"}]}')
    (out / (v.stem + ".feed.json")).write_text("{}")
    hub, ui = HubWithAnswer(), FakeUI()
    r = Recorder(s, FakeOBS(s.folder_path), hub, ui, fake_scan, is_apex_running=lambda: False)
    r.idx = {"k": retention.Entry(path=str(v), started=time.time(), status="scanned")}
    r._retry_uploads()
    e = r.idx["k"]
    assert e.status == "uploaded" and e.note == "Sent to hub: 11 games", vars(e)
    assert hub.uploads == [[v.stem + ".feed.json", v.stem + ".scan.json"]], hub.uploads   # one upload, both files
    assert any(t == "Results sent" and "11 games" in m for t, m, *_ in ui.notes), ui.notes
    r._retry_uploads()
    assert len(hub.uploads) == 1                                                       # never sent twice


class HubThenDrive(FakeHub):
    """A Sender stand-in: Drive takes the results, the hub fails with `fail` (a HubError kind) until it is None.
    `blocked` = what Sender.hub_blocked() would say (refused key, 429 wait)."""

    def __init__(self):
        super().__init__()
        self.fail, self.blocked, self.hub_calls = "network", "", []

    def upload(self, files):
        from pcs_recorder.runtime import Sent
        out = Sent(["Google Drive"])
        out.drive_ok, out.hub_ok, out.hub_why = True, False, "Hub not reachable"
        out.hub_note = "Not sent to hub: the team hub didn't answer"
        return out

    def hub_blocked(self):
        return self.blocked

    def upload_hub(self, files):
        from pcs_recorder import hub as hubmod
        self.hub_calls.append([f.name for f in files])
        if self.fail:
            raise hubmod.HubError("no", self.fail, {"refused": 401, "wait": 429, "http": 500}.get(self.fail, 0))
        return "Sent to hub: 2 games"


def _hub_recorder(n=1, status="scanned", age_days=0.0, note=""):
    retention.INDEX_FILE.unlink(missing_ok=True)
    retention.SENT_FILE.unlink(missing_ok=True)
    s = settings.load()
    s.folder, s.hub_url = str(TMP / f"vids-{time.time_ns()}"), "http://hub.test:8420"
    idx = {}
    for i in range(n):
        v = make_file(s.folder_path, f"Apex Legends_h{i}.mp4", UNIT)
        out = v.with_suffix(".scan")
        out.mkdir()
        (out / (v.stem + ".scan.json")).write_text('{"games": [{"map": "SP"}, {"map": "WE"}]}')
        idx[f"k{i}"] = retention.Entry(path=str(v), started=time.time() - age_days * 86400 - i, status=status, note=note)
    retention.save_index(idx)
    clock = {"t": time.time()}
    hub, ui = HubThenDrive(), FakeUI()
    r = Recorder(s, FakeOBS(s.folder_path), hub, ui, fake_scan, is_apex_running=lambda: False, clock=lambda: clock["t"])
    return r, hub, ui, clock


def test_hub_retried_when_only_drive_took_it():
    """Drive took the results, the hub was down: the recording shows "Not on hub yet", the hub is tried again on
    its own (backoff 1, 2, 4… min) until it takes them, then never again; nothing goes out while Apex runs."""
    r, hub, ui, clock = _hub_recorder()
    r._retry_uploads()
    assert r.idx["k0"].status == "uploaded" and r.hub_state("k0") == {"hub": "pending", "why": "Hub not reachable"}
    assert any(t == "Results sent" and "by itself" in m for t, m, *_ in ui.notes), ui.notes
    assert retention.load_sent(r.idx)["k0"]["drive"] == "sent"                         # kept on disk
    r._retry_hub()
    assert len(hub.hub_calls) == 1 and r.hub_state("k0")["hub"] == "pending"
    r._retry_hub(); r._retry_hub()
    assert len(hub.hub_calls) == 1                                                     # waits 1 min
    clock["t"] += 61; r._retry_hub()
    assert len(hub.hub_calls) == 2                                                     # then 2 min
    clock["t"] += 61; r._retry_hub()
    assert len(hub.hub_calls) == 2
    hub.fail = None                                                                    # hub back
    apex = {"on": True}
    r.is_apex_running = lambda: apex["on"]
    r._start = lambda: None                                                            # no recording in this test
    clock["t"] += 120; r.tick()
    assert len(hub.hub_calls) == 2                                                     # never while Apex runs
    apex["on"] = False; r.tick()
    assert len(hub.hub_calls) == 3 and r.hub_state("k0") == {"hub": "sent", "why": ""}
    assert r.idx["k0"].note == "Sent to hub: 2 games"
    assert any(t == "Sent to hub" and "2 games" in m for t, m, *_ in ui.notes), ui.notes
    clock["t"] += 3600; r.tick(); r._retry_hub()
    assert len(hub.hub_calls) == 3                                                     # a 200 is never re-sent
    # after a restart the state is read back: still on the hub, nothing re-sent
    r2 = Recorder(r.s, FakeOBS(r.s.folder_path), hub, FakeUI(), fake_scan, is_apex_running=lambda: False)
    assert r2.hub_state("k0")["hub"] == "sent"
    r2._retry_hub()
    assert len(hub.hub_calls) == 3


def test_hub_key_refused_then_fixed():
    """A refused key: nothing is tried while the key stays the same ("Key refused" shown); a new key = sent."""
    r, hub, ui, clock = _hub_recorder(n=2)
    r._retry_uploads()
    assert all(r.hub_state(k)["hub"] == "pending" for k in ("k0", "k1"))
    hub.fail = "refused"
    r._retry_hub()
    assert len(hub.hub_calls) == 1 and all(r.hub_state(k)["why"] == "Key refused" for k in ("k0", "k1"))
    hub.blocked = "Key refused"                                                        # what hub.py now knows
    for _ in range(5):
        clock["t"] += 3600; r._retry_hub()
    assert len(hub.hub_calls) == 1                                                     # stopped until the key changes
    hub.blocked, hub.fail = "", None                                                   # new key pasted
    r._retry_hub()
    assert len(hub.hub_calls) == 3 and all(r.hub_state(k)["hub"] == "sent" for k in ("k0", "k1"))
    assert [t for t, *_ in ui.notes].count("Sent to hub") == 1                         # one message for both
    # 429: waits, no backoff counted, goes out when the wait is over
    r3, hub3, _, clock3 = _hub_recorder()
    r3._retry_uploads()
    hub3.blocked = "Hub asks to wait"
    r3._retry_hub()
    assert not hub3.hub_calls and r3.hub_state("k0")["why"] == "Hub asks to wait"
    hub3.blocked, hub3.fail = "", None
    r3._retry_hub()
    assert r3.hub_state("k0")["hub"] == "sent"


def test_old_uploaded_recordings_not_flooded():
    """After the update: recordings "uploaded" by the old rule. Those the hub confirmed stay sent; only the last
    3 days' others are tried on the hub again; older ones are left alone."""
    r, hub, ui, clock = _hub_recorder(n=3, status="uploaded", age_days=10)
    assert all(r.hub_state(k)["hub"] == "" for k in r.idx), r.sent                # 10 days old: not re-sent
    hub.fail = None
    r._retry_hub()
    assert not hub.hub_calls
    r, hub, ui, clock = _hub_recorder(n=1, status="uploaded", age_days=1, note="Sent to hub: 4 games")
    assert r.hub_state("k0")["hub"] == "sent"                                          # the hub said so before
    r, hub, ui, clock = _hub_recorder(n=2, status="uploaded", age_days=1, note="Not sent to hub: key refused")
    assert all(r.hub_state(k)["hub"] == "pending" for k in r.idx)
    hub.fail = None
    r._retry_hub()
    assert len(hub.hub_calls) == 2 and all(r.hub_state(k)["hub"] == "sent" for k in r.idx)
    # done once: a later start never sorts them again (the file exists)
    r.idx["k0"].note = ""
    r2 = Recorder(r.s, FakeOBS(r.s.folder_path), hub, FakeUI(), fake_scan, is_apex_running=lambda: False)
    assert r2.hub_state("k0")["hub"] == "sent"
    # no hub set at update time: nothing pending
    retention.SENT_FILE.unlink()
    for e in r.idx.values():
        e.note = ""
    assert retention.load_sent(r.idx, "") == {}


def test_hub_and_drive_both_fine_or_no_hub():
    """Hub took it: "On hub", never retried. No hub set: no hub state at all."""
    from pcs_recorder.runtime import Sent
    r, hub, ui, clock = _hub_recorder()

    def both(files):
        out = Sent(["the team hub", "Google Drive"])
        out.hub_ok, out.drive_ok, out.hub_note = True, True, "Sent to hub: 2 games"
        return out
    hub.upload = both
    r._retry_uploads()
    assert r.sent["k0"] == {"hub": "sent", "why": "", "drive": "sent"}
    r._retry_hub()
    assert not hub.hub_calls
    r, hub, ui, clock = _hub_recorder()
    hub.upload = lambda files: Sent(["Google Drive"])                                  # no hub address
    r._retry_uploads()
    assert r.hub_state("k0")["hub"] == ""


def _silence_recorder(levels, tracks=("game", "discord", "mic"), consent=True, mutes=()):
    import numpy as np
    retention.INDEX_FILE.unlink(missing_ok=True)
    s = settings.load()
    s.folder = str(TMP / f"vids-{time.time_ns()}")
    s.comms_consent = consent
    s.game_audio, s.discord_audio, s.mic_audio = "game" in tracks, "discord" in tracks, "mic" in tracks
    s.voice_app, s.mic_device = "teamspeak", "default"
    clock = {"t": 1_000_000.0}
    ui = FakeUI()
    r = Recorder(s, FakeOBS(s.folder_path), FakeHub(), ui, fake_scan, is_apex_running=lambda: False,
                 clock=lambda: clock["t"])
    read = []

    def track_db(video, i):
        read.append(i)
        return np.array(levels[s.tracks()[i]], dtype=np.float32)
    r.track_db = track_db
    return r, ui, clock, read


def _record(r, clock, minutes, mutes=()):
    r.events = [{"t": 1, "kind": "mute", "track": t, "muted": True} for t in mutes]
    r.silence_thread = None
    r._start_silence_check(str(TMP / "x.mp4"), minutes * 60)
    if r.silence_thread:
        r.silence_thread.join(10)


def test_silent_voice_or_mic_track_warned_once():
    """A voice or mic track silent from start to end (loudest half second under -60 dBFS): told once, short and
    literal; not again for the same setting; again after the setting changed. Normal tracks, short recordings,
    tracks turned off, no consent, a track muted by hotkey: nothing."""
    (settings.APP_DIR / "silence.json").unlink(missing_ok=True)
    quiet, talk = [-120.0] * 1200, [-120.0] * 1100 + [-30.0] * 100
    noise = [-75.0] * 1200                                         # mic noise floor only: still "silent"
    r, ui, clock, read = _silence_recorder({"game": talk, "discord": quiet, "mic": talk})
    _record(r, clock, 60)
    assert [t for t, *_ in ui.notes] == ["Voice track was silent"], ui.notes
    assert "TeamSpeak" in ui.notes[0][1] and "Settings → Voice app" in ui.notes[0][1]
    assert read == [1, 2]                                          # tracks 2 and 3 only, never the game's
    _record(r, clock, 60)
    assert len(ui.notes) == 1                                      # same setting: not told again
    r.s.voice_app = "discord"                                      # setting changed, still silent: told again
    _record(r, clock, 60)
    assert len(ui.notes) == 2 and "Discord" in ui.notes[1][1]
    # mic silent
    r, ui, clock, read = _silence_recorder({"game": talk, "discord": talk, "mic": noise})
    _record(r, clock, 30)
    assert [t for t, *_ in ui.notes] == ["Mic track was silent"] and "Settings → Microphone" in ui.notes[0][1]
    # normal tracks: nothing; and sound again resets, so a later silence is told
    (settings.APP_DIR / "silence.json").unlink(missing_ok=True)
    r, ui, clock, read = _silence_recorder({"game": talk, "discord": talk, "mic": talk})
    _record(r, clock, 60)
    assert ui.notes == []
    # short recording, no consent, track off, muted by hotkey: not checked
    r, ui, clock, read = _silence_recorder({"game": talk, "discord": quiet, "mic": quiet})
    _record(r, clock, 3)
    assert ui.notes == [] and read == []
    r, ui, clock, read = _silence_recorder({"game": talk}, tracks=("game",), consent=False)
    _record(r, clock, 60)
    assert ui.notes == [] and read == []
    r, ui, clock, read = _silence_recorder({"game": talk, "mic": quiet}, tracks=("game", "mic"))
    _record(r, clock, 60, mutes=("mic",))
    assert ui.notes == [] and read == []
    r, ui, clock, read = _silence_recorder({"game": talk, "mic": quiet}, tracks=("game", "mic"))
    _record(r, clock, 60)
    assert [t for t, *_ in ui.notes] == ["Mic track was silent"] and read == [1]      # mic = 2nd track here


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
