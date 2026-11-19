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
            raise ConnectionError("the team hub didn't answer")
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
    import psutil
    root = psutil.Process(r.scan_proc.pid)
    tree = [root] + root.children(recursive=True)
    assert len(tree) >= 2, tree
    first = r.scan_key
    starts = []
    r.start_scan = lambda v, out: (starts.append(v), fake_scan(v, out))[1]
    apex["on"] = True; r.tick()                      # player relaunches: scan stops (memory freed), no upload
    assert "paused" in r.status
    assert r.scan_proc is None and r.queue == [first] and r.idx[first].status == "waiting", (r.queue, r.scan_proc)
    assert not any(p.is_running() and p.status() != psutil.STATUS_ZOMBIE for p in tree), tree   # nothing left
    time.sleep(2.5); r.tick()
    assert r.scan_proc is None and hub.uploads == [] and "paused" in r.status   # nothing runs or is sent mid-game
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
        r.status)
    assert [e.status for e in r.idx.values()] == ["uploaded", "uploaded"], {k: vars(v) for k, v in r.idx.items()}
    titles = [n[0] for n in ui.notes]
    for t in ("Scanning your session", "Scan finished", "Results sent"):
        assert any(x.startswith(t) for x in titles), titles      # the player is told at every step
    # the stopped scan went on first (no second "Scanning" message for it), then the second session's
    assert sum(x.startswith("Scanning your session") for x in titles) == 2, titles
    assert starts[0] == r.idx[first].path and len(starts) == 2, (starts, r.idx[first].path)
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


def _eec_rec(folder, name, started, end, games):
    """A recording with a scan, for the EEC drop tests."""
    import json
    v = folder / f"Apex Legends_{name}.mp4"
    v.write_bytes(b"x")
    os.utime(v, (end, end))
    v.with_suffix(".scan").mkdir()
    (v.with_suffix(".scan") / (v.stem + ".scan.json")).write_text(json.dumps({"games": games}))
    return retention.Entry(path=str(v), started=started, status="scanned")


def _eec_export(t0, pcs_pois, other_pois=("THERMAL STATION (8)", "CITY HALL (1)")):
    """A November-style export: one pick per team and map (WE then ED, two games each), map_pick_id on every row."""
    maps = ["mp_rr_desertlands_hu", "mp_rr_desertlands_hu", "mp_rr_district", "mp_rr_district"]
    matches = []
    for i, mn in enumerate(maps):
        def row(team, player, place, kills, pid):
            return {"team_name": team, "player_name": player, "team_placement": place, "kills": kills,
                    "map_pick_id": str(pid)}
        m = 0 if i < 2 else 1
        matches.append({"map_name": mn, "match_start": str(int(t0 + 1500 + i * 1800)), "data": [
            row("PCS", "a", 2, 3, 500 + m), row("PCS", "b", 2, 1, 500 + m), row("Other", "c", 1, 2, 600 + m)]})
    draft = [{"team": "PCS", "poi": pcs_pois[0], "pick_num": 4, "seed": 2},
             {"team": "PCS", "poi": pcs_pois[1], "pick_num": 25, "seed": 2},
             {"team": "Other", "poi": other_pois[0], "pick_num": 1, "seed": 1},
             {"team": "Other", "poi": other_pois[1], "pick_num": 22, "seed": 1}]
    return {"matches": matches[::-1], "draft": draft}       # file order isn't play order


def _scrim_game(t, mp, poi, jump=60.0):
    return {"map": mp, "mode": "scrim", "tStart": t, "drop": {"spots": [{"poi": poi, "kind": "ours"}]},
            "landing": {"poi": poi, "exitAtExact": t + jump}}


def test_eec_drops_confirm_scrims():
    """EEC drops (draft joined by map_pick_id): EEC wins for display, ours kept when it differs; a block whose games
    fit the lobby by map, time and drop is labelled scrims from EEC, with the reason kept."""
    from pcs_recorder import eec, labels, youtube
    youtube.STATE_FILE.unlink(missing_ok=True)
    labels.FILE.unlink(missing_ok=True)
    t0 = time.time() - 8 * 3600
    lobby = eec.from_export(_eec_export(t0, ("STAGING (3)", "CITY HALL (1)")), "lobbies_4_abc123_scores.json")
    assert [g["map"] for g in lobby["games"]] == ["WE", "WE", "ED", "ED"]          # sorted by start
    assert lobby["drops"] == "export, pick id"
    assert [g["drops"]["pcs"] for g in lobby["games"]] == ["Staging", "Staging", "City Hall", "City Hall"]
    assert lobby["games"][0]["drops"]["other"] == "Thermal Station"
    res = eec.results(lobby, "PCS")
    assert [g["drop"] for g in res["games"]] == ["Staging", "Staging", "City Hall", "City Hall"]
    assert eec.compare_drop("WE", "Staging", "we-staging")[0] == "same"
    assert eec.compare_drop("ED", "Canal", "ed-canal-plaza")[0] == "same"           # an alias in our map table
    assert eec.compare_drop("WE", "Staging", "we-thermal")[0] == "different"
    assert eec.compare_drop("WE", "Nowhere Plaza", "we-staging")[0] is None         # not in our map table: no verdict
    assert eec.compare_drop("WE", "", "we-staging")[0] is None

    folder = TMP / f"eecdrops-{time.time_ns()}"
    folder.mkdir()
    # 4 games 30 min apart; the third one landed somewhere else than EEC says (EEC wins, ours kept)
    # game 2 has no drop screen read (mode unknown): EEC still counts it
    games = [_scrim_game(1440, "WE", "we-staging"), _scrim_game(3240, "WE", "we-staging"),
             _scrim_game(5040, "ED", "ed-old-town"), _scrim_game(6840, "ED", "ed-city-hall")]
    games[1].update(mode="unknown", drop=None)
    idx = {"s": _eec_rec(folder, "s", t0, t0 + 8000, games)}

    class Rec:
        current = None
    rec = Rec()
    rec.idx = idx
    s = settings.Settings(player="Xno", yt_team="PCS", folder=str(folder), yt_enabled=True, yt_wait_eec=False)
    up = youtube.Uploader(s, rec, FakeUI(), lambda: None, lambda: False)
    got = up.add_results(lobby)
    assert got["block"] == ["s"] and got["check"]["verdict"] == "match", got["check"]
    chk = got["check"]
    assert [(x["eecGame"], x["check"]) for x in chk["games"]] == [(1, "same"), (2, "same"), (3, "different"), (4, "same")]
    assert chk["why"] == "EEC drops agree with where we landed in 3 of 4 game(s)"
    g3 = chk["games"][2]
    assert (g3["eecDrop"], g3["ourDrop"], g3["ourFrom"]) == ("City Hall", "Old Town", "landing")
    # label: scrims from EEC (even a block with no drop screen read would be)
    lab = labels.explain_block([idx["s"].path], [youtube.scan_of(idx["s"])], chk)
    assert lab == {"label": "scrims", "source": "eec", "why": chk["why"]}, lab
    q = up.queue()[0]
    assert q["label"] == "scrims" and q["label_source"] == "eec" and q["eec_check"]["verdict"] == "match"
    # description: EEC's drop, ours in brackets where it differs
    title, desc, n = youtube.describe(s, idx, ["s"], [0.0], results=res, kind="scrims", check=chk)
    assert "Game 1 · World's Edge · Staging · 2nd" in desc, desc
    assert "Game 3 · E-District · City Hall (scan: Old Town) · 2nd" in desc, desc
    # a block with no drop screen at all: the scan says nothing, EEC says scrims
    bare = [dict(g, mode="unknown", drop=None) for g in games]
    idx2 = {"b": _eec_rec(folder, "b", t0, t0 + 8000, bare)}
    chk2 = youtube.check_of(idx2, ["b"], res)
    assert chk2["verdict"] == "match"
    assert labels.of_block([idx2["b"].path], [youtube.scan_of(idx2["b"])]) == (None, False)
    assert labels.of_block([idx2["b"].path], [youtube.scan_of(idx2["b"])], chk2) == ("scrims", False)
    # a label set by hand still wins
    labels.set_label(idx2["b"].path, "ranked")
    assert labels.explain_block([idx2["b"].path], [youtube.scan_of(idx2["b"])], chk2)["source"] == "hand"
    labels.FILE.unlink(missing_ok=True)


def test_poi_names_and_aliases():
    """POI names: a name, an old name, an alias or EEC's 'NAME (6)' all resolve to the same POI id (scans keep ids,
    so a rename never splits history); a name two POIs share resolves to none; the EEC check compares ids. Fake map."""
    from pcs_recorder import eec
    keep = eec._POIS, eec._ALIASES
    try:
        eec._POIS = {"ZZ": {"zz-north": "Fake North", "zz-south": "The Fake Mill", "zz-east": "Glass Yard",
                            "zz-west": "Glass Yards East"}}
        eec._ALIASES = {"ZZ": {"zz-north": ["Old Fake North", "Fake Top"], "zz-south": [], "zz-east": ["Shared"],
                               "zz-west": ["Shared"]}}
        assert eec.resolve("ZZ", "zz-north") == "zz-north"
        assert eec.resolve("ZZ", "FAKE NORTH (6)") == "zz-north"
        assert eec.resolve("ZZ", "Old Fake North") == "zz-north"
        assert eec.resolve("ZZ", "fake top") == "zz-north"
        assert eec.resolve("ZZ", "Fake Mills") == "zz-south"                       # no 'The', plural s
        assert eec.resolve("ZZ", "Shared") is None                                  # two POIs: none
        assert eec.resolve("ZZ", "Somewhere") is None
        assert eec.resolve("QQ", "Fake North") is None                              # other map
        assert eec.compare_drop("ZZ", "OLD FAKE NORTH (3)", "zz-north")[0] == "same"
        assert eec.compare_drop("ZZ", "Fake Top", "zz-south")[0] == "different"
        assert eec.compare_drop("ZZ", "Glass", "zz-east")[0] == "near"              # unknown name: old rule
        assert eec.compare_drop("ZZ", "Somewhere", "zz-east")[0] is None
        # POI names are game words: English in every app language; the hub is never asked for FR / KO names
        calls = []
        d = eec.app_poi_names("http://hub.test:8420", lambda url, path: calls.append(path) or {"lang": "fr"})
        assert d["lang"] == "en" and d["names"]["zz-north"] == "Fake North" and d["names"]["zz-south"] == "The Fake Mill"
        assert calls == [] and not hasattr(eec, "shown_names")
    finally:
        eec._POIS, eec._ALIASES = keep


def test_maps_game_names():
    """pcs_maps.json: every POI has a name, aliases are a list of other names (never the name itself), no two POIs of
    a map share a name or alias, and the EEC names seen in our lobbies resolve to the right POI."""
    import json as _json
    from pcs_recorder import eec
    maps = _json.loads((Path(__file__).resolve().parent.parent / "scanner" / "pcs_maps.json").read_text(encoding="utf-8"))
    for mp, d in maps.items():
        seen = {}
        for p in d["pois"]:
            assert p["name"], p["id"]
            al = p.get("aliases", [])
            assert isinstance(al, list) and all(isinstance(a, str) and a for a in al), p["id"]
            assert p["name"] not in al, p["id"]
            for n in [p["name"], *al]:
                k = eec.norm(n)
                assert seen.get(k, p["id"]) == p["id"], f"{mp}: {n} on {seen.get(k)} and {p['id']}"
                seen[k] = p["id"]
    for mp, name, pid in [("WE", "SKYHOOK WEST (2)", "we-skyhook"), ("WE", "Skyhook East", "we-skyhook-east"),
                          ("WE", "MIRAGE A TROIS (6)", "we-mirage"), ("WE", "STACKS (14)", "we-stacks"),
                          ("ED", "Canal", "ed-canal-plaza"), ("ED", "Uptown", "ed-vibe-isle"),
                          ("SP", "Mills", "sp-the-mill"), ("SP", "Cascades", "sp-cascade-falls"),
                          ("SP", "Pylon", "sp-wattsons-pylon"), ("SP", "Zeus Station", "sp-zeus-station"),
                          ("KC", "Cage", "kc-cage")]:
        assert eec.resolve(mp, name) == pid, (mp, name)


def test_eec_drops_refuse_tournament():
    """A tournament the same evening, played while the lobby's games ran (times fit) but from other drops, is not
    that scrim: the lobby goes to the block whose drops agree; the tournament keeps its scan label and says why."""
    from pcs_recorder import eec, labels, youtube
    youtube.STATE_FILE.unlink(missing_ok=True)
    labels.FILE.unlink(missing_ok=True)
    t0 = time.time() - 10 * 3600
    lobby = eec.from_export(_eec_export(t0, ("STAGING (3)", "CITY HALL (1)")), "lobbies_4_abc123_scores.json")
    res = eec.results(lobby, "PCS")
    folder = TMP / f"eectour-{time.time_ns()}"
    folder.mkdir()
    # tournament block: 2 WE games, jumps 10 min before the EEC games 1 and 2, landed at Thermal Station
    tour = _eec_rec(folder, "t", t0, t0 + 1500, [_scrim_game(800, "WE", "we-thermal"), _scrim_game(1340, "WE", "we-thermal")])
    # the scrim block, 15 min later
    scrim = _eec_rec(folder, "s", t0 + 2400, t0 + 9000,
                     [_scrim_game(1440 - 2400 + 1800, "WE", "we-staging"), _scrim_game(5040 - 2400, "ED", "ed-city-hall")])
    idx = {"t": tour, "s": scrim}

    class Rec:
        current = None
    rec = Rec()
    rec.idx = idx
    s = settings.Settings(player="Xno", yt_team="PCS", folder=str(folder), yt_enabled=True, yt_wait_eec=False,
                          yt_gap_min=10)
    assert youtube.blocks(idx, 10) == [["t"], ["s"]]
    chk_t = youtube.check_of(idx, ["t"], res)
    assert chk_t["verdict"] == "mismatch" and "differ" in chk_t["why"], chk_t
    up = youtube.Uploader(s, rec, FakeUI(), lambda: None, lambda: False)
    got = up.add_results(lobby)
    assert got["block"] == ["s"] and got["check"]["verdict"] == "match", got     # the tournament block was passed over
    assert up.state["t"]["eec_rejected"][0]["url"] == res["url"]
    assert "eec_res" not in up.state["t"] and up.results_of("t") is None
    lab = labels.explain_block([tour.path], [youtube.scan_of(tour)], chk_t)
    assert lab["label"] == "scrims" and lab["source"] == "scan" and "not the EEC lobby" in lab["why"], lab
    # results attached to the tournament block by an older version: no longer used for it (title, scrims-only)
    up.state["t"]["eec_res"] = res
    assert up.usable(["t"]) == (None, chk_t) and up.results_of("t") is None
    q = {b["id"]: b for b in up.queue()}
    assert q["t"]["res"] is None and q["t"]["eec_check"]["verdict"] == "mismatch" and q["s"]["label_source"] == "eec"
    # only the tournament in the recordings: the lobby is refused, with the reason
    rec.idx = {"t": tour}
    up2 = youtube.Uploader(s, rec, FakeUI(), lambda: None, lambda: False)
    up2.state = {}
    try:
        up2.add_results(lobby)
        raise AssertionError("tournament got the scrim lobby")
    except RuntimeError as e:
        assert "doesn't match your games" in str(e), e
    # times that don't fit at all (a block in the lobby's window, games an hour off): not that lobby either
    off = _eec_rec(folder, "o", t0 - 1500, t0 + 1000, [_scrim_game(10, "WE", "we-staging")])
    assert youtube.check_of({"o": off}, ["o"], res)["verdict"] == "mismatch"


def test_eec_drops_missing_fallback():
    """No EEC drops (an export without a draft, an old Loot Book document) or nothing to compare: the old behaviour
    (lobby by time, label from the scan)."""
    from pcs_recorder import eec, labels, youtube
    youtube.STATE_FILE.unlink(missing_ok=True)
    labels.FILE.unlink(missing_ok=True)
    t0 = time.time() - 12 * 3600
    export = _eec_export(t0, ("STAGING (3)", "CITY HALL (1)"))
    export.pop("draft")
    lobby = eec.from_export(export, "lobbies_4_abc123_scores.json")
    assert lobby["drops"] == "" and all(not g["drops"] for g in lobby["games"])
    res = eec.results(lobby, "PCS")
    assert all(g["drop"] == "" for g in res["games"])
    folder = TMP / f"eecnone-{time.time_ns()}"
    folder.mkdir()
    # landed somewhere else than the (unknown) drop: no verdict without EEC drops
    games = [_scrim_game(1440, "WE", "we-thermal"), _scrim_game(5040, "ED", "ed-old-town")]
    idx = {"s": _eec_rec(folder, "s", t0, t0 + 8000, games)}
    chk = youtube.check_of(idx, ["s"], res)
    assert chk["verdict"] == "unknown" and chk["why"] == "the EEC results have no drops (game times fit)", chk
    assert labels.of_block([idx["s"].path], [youtube.scan_of(idx["s"])], chk) == ("scrims", False)
    lab = labels.explain_block([idx["s"].path], [youtube.scan_of(idx["s"])], chk)
    assert lab["source"] == "scan" and lab["why"].startswith("the scrim drop screen was read"), lab

    class Rec:
        current = None
    rec = Rec()
    rec.idx = idx
    s = settings.Settings(player="Xno", yt_team="PCS", folder=str(folder), yt_enabled=True, yt_wait_eec=False)
    up = youtube.Uploader(s, rec, FakeUI(), lambda: None, lambda: False)
    assert up.add_results(lobby)["block"] == ["s"] and up.results_of("s")["pts"] == res["pts"]
    title, desc, n = youtube.describe(s, idx, ["s"], [0.0], results=res, kind="scrims", check=chk)
    assert "Game 1 · World's Edge · Thermal · 2nd" in desc, desc            # our drop screen, as before
    # an old Loot Book document (no draft): its drops are not used
    doc = {"url": "u", "games": [{"start": t0 + 1500, "map": "WE",
                                  "rows": [["PCS", "a", "", 2, 3, 0, 0, 0, 0, 0, 0, 0, 0, "Thermal Station"]]}]}
    assert eec.from_hub_doc(doc)["games"][0]["drops"] == {}
    doc["draft"] = [{"team": "PCS"}]
    assert eec.from_hub_doc(doc)["games"][0]["drops"] == {"pcs": "Thermal Station"}
    # no EEC results at all: labels as before
    assert youtube.check_of(idx, ["s"], None) is None
    assert labels.explain_block([idx["s"].path], [youtube.scan_of(idx["s"])], None)["label"] == "scrims"
    assert labels.explain(idx["s"].path, youtube.scan_of(idx["s"]))["source"] == "scan"


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


def test_hybrid_mp4_recording():
    """OBS 30.2+ records straight into an .mp4 (hybrid MP4): no remux after the session, the file is used once OBS
    has finished writing it (size holding still). Older OBS keeps .mkv + remux. Both in OBS's config and when the
    running OBS is prepared."""
    import threading
    from pcs_recorder import obs as obsmod
    assert obsmod.rec_format((33, 0, 1)) == obsmod.rec_format((30, 2, 0)) == "hybrid_mp4"
    assert obsmod.rec_format((30, 1, 2)) == obsmod.rec_format((29, 1, 3)) == obsmod.rec_format(()) == "mkv"
    real = obsmod.exe_version
    try:
        for ver, fmt in (((33, 0, 1), "hybrid_mp4"), ((30, 0, 0), "mkv")):
            obsmod.exe_version = lambda path=None, v=ver: v
            obsmod.write_config(settings.Settings(folder=str(TMP / "hy")), "av1_texture_amf", 2560, 1440)
            ini = (obsmod.CONFIG / "basic" / "profiles" / "PCS" / "basic.ini").read_text()
            assert f"RecFormat2={fmt}\n" in ini and "AutoRemux=true" in ini, ini
    finally:
        obsmod.exe_version = real
    folder = TMP / f"hybrid-{time.time_ns()}"
    folder.mkdir()
    mp4 = folder / "Apex Legends_x.mp4"
    mp4.write_bytes(b"\0" * UNIT)
    r = Recorder(settings.load(), FakeOBS(folder), FakeHub(), FakeUI(), fake_scan, is_apex_running=lambda: False)

    def finish():                                           # OBS still writing the index for a moment
        time.sleep(0.1)
        with open(mp4, "ab") as f:
            f.write(b"\1" * UNIT)
    t = threading.Thread(target=finish)
    t.start()
    t0 = time.time()
    assert r._final_file(str(mp4), wait_s=5, poll=0.3) == str(mp4)
    t.join()
    assert mp4.stat().st_size == 2 * UNIT and time.time() - t0 < 2      # waited for the end, not for the limit
    assert not (folder / "Apex Legends_x.mkv").exists()


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
    assert fails[0][3] is None and "didn't answer" in fails[0][1]
    hub2.down = False; r2.next_upload = 0; r2.tick(); r2.wait_uploads()
    assert list(r2.idx.values())[0].status == "uploaded"


def test_pause_never_stacks():
    """Ticks while Apex runs stop the scan once (no second stop, no new scan started mid-game); when Apex closes
    the scan starts again once and finishes."""
    import psutil
    from pcs_recorder import core as rcore
    retention.INDEX_FILE.unlink(missing_ok=True)
    s = settings.load(); s.folder = str(TMP / "stack"); s.enabled = True
    apex = {"on": False}
    hub, ui = FakeHub(), FakeUI(True)
    starts = []
    r = Recorder(s, FakeOBS(s.folder_path), hub, ui, lambda v, o: (starts.append(v), fake_scan(v, o))[1],
                 is_apex_running=lambda: apex["on"])
    r._final_file = lambda p, wait_s=0: p
    apex["on"] = True; r.tick(); apex["on"] = False; r.tick()          # one session, scan starts
    time.sleep(0.8)
    stops = []
    real = rcore.stop_tree
    rcore.stop_tree = lambda p, **k: (stops.append(p.pid), real(p, **k))[1]
    try:
        apex["on"] = True
        for _ in range(5):
            r.tick()                                                     # five checks while Apex runs
        assert len(stops) == 1 and r.scan_proc is None and r.paused and len(starts) == 1, (stops, starts)
        assert not psutil.pid_exists(stops[0]) or psutil.Process(stops[0]).status() == psutil.STATUS_ZOMBIE
        apex["on"] = False; r.tick()
        assert not r.paused and r.scan_proc is not None and len(starts) == 2, starts
    finally:
        rcore.stop_tree = real
    for _ in range(60):
        r.tick(); time.sleep(0.5)
        if hub.uploads:
            break
    assert hub.uploads, "scan never finished after resume"


def test_upload_part_never_delays_a_recording():
    """A big file going to the hub in parts on a slow line: Apex starting starts the recording in the same tick
    (the upload runs in its own thread), the part in flight stops at once, and once Apex closes the upload goes on
    from what the hub already has, until the hub has the whole file."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit, parse_qs
    from pcs_recorder import hub as hubmod
    from pcs_recorder.runtime import Sender
    key = "pcs_" + "A" * 43
    hubmod.set_key(key)
    hubmod.reset()
    old = (hubmod.PART_FILE, hubmod.PART_MAX)
    hubmod.PART_FILE, hubmod.PART_MAX = 1024 * 1024, 16 * 1024 * 1024
    PART = 16 * 1024 * 1024
    st = {"data": b"", "size": 0, "reading": threading.Event(), "offsets": [], "cut": 0, "active": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            raw = __import__("json").dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _view(self):
            return {"id": "up-" + "0" * 32, "received": len(st["data"]), "size": st["size"],
                    "part_max": PART, "complete": len(st["data"]) == st["size"]}

        def _go(self):
            u = urlsplit(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            if self.command == "PUT":                 # a slow line: 128 KB every 20 ms; a part kept when whole
                off = int(parse_qs(u.query)["offset"][0])
                st["offsets"].append(off)
                if off != len(st["data"]):
                    self.rfile.read(n)
                    return self._send(409, dict(self._view(), error="offset_mismatch"))
                got, left = b"", n
                st["active"] += 1
                while left:
                    if len(st["data"]) >= PART and len(got) >= 1024 * 1024:
                        st["reading"].set()               # the second part is on its way
                    try:
                        chunk = self.rfile.read(min(131072, left))
                    except OSError:
                        chunk = b""
                    if not chunk:
                        st["cut"] += 1
                        st["active"] -= 1
                        return                        # the app stopped the part: nothing of it kept
                    got += chunk
                    left -= len(chunk)
                    time.sleep(0.02)
                st["data"] += got
                st["active"] -= 1
                try:
                    return self._send(200, self._view())
                except OSError:
                    return
            body = self.rfile.read(n) if n else b""
            if u.path == "/api/uploads" and self.command == "POST":
                st["size"] = __import__("json").loads(body)["size"]
                return self._send(200, self._view())
            if u.path.startswith("/api/uploads/") and u.path.endswith("/finish"):
                return self._send(200, {"scan_ids": [5], "imports": []})
            if u.path.startswith("/api/uploads/"):
                return self._send(200, self._view())
            if u.path == "/api/scans":
                return self._send(200, {"scan_ids": [4], "imports": [{"status": "imported", "games": 2}]})
            return self._send(200, {"ok": True})

        do_GET = do_POST = do_PUT = _go

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        retention.INDEX_FILE.unlink(missing_ok=True)
        s = settings.load()
        s.folder, s.enabled = str(TMP / f"vids-{time.time_ns()}"), True
        s.hub_url = f"http://127.0.0.1:{srv.server_address[1]}"
        v = make_file(s.folder_path, "Apex Legends_slow.mp4", UNIT)
        out = v.with_suffix(".scan")
        out.mkdir()
        (out / (v.stem + ".scan.json")).write_text('{"games": [{"map": "SP"}]}')
        (out / (v.stem + ".feed.json")).write_text("{}")
        mic = out / (v.stem + ".mic.m4a")
        mic.write_bytes(os.urandom(36 * 1024 * 1024))           # 16 + 16 + 4 MB parts, ~2.5 s per 16 MB here
        apex = {"on": False}
        obs, ui = FakeOBS(s.folder_path), FakeUI()
        sender = Sender(s, stop=lambda: apex["on"])
        r = Recorder(s, obs, sender, ui, fake_scan, is_apex_running=lambda: apex["on"])
        r.idx = {"k": retention.Entry(path=str(v), started=time.time() - 3600, status="scanned")}
        r.tick()                                                 # the upload starts in its own thread
        assert st["reading"].wait(10), "the part never started"
        apex["on"] = True
        t0 = time.monotonic()
        r.tick()                                                 # Apex starts while the part is on its way
        took = time.monotonic() - t0
        assert obs.rec and r.current is not None, "recording not started"
        assert took < 1.0, f"the tick waited {took:.1f} s for the upload"
        r.wait_uploads(1.0 - took)                               # the part in flight needs ~2 s more
        assert not r.upload_thread.is_alive(), "the part in flight did not stop"
        for _ in range(200):                                     # the hub reads what was already on the line
            if not st["active"]:
                break
            time.sleep(0.05)
        had = len(st["data"])
        assert 0 < had < mic.stat().st_size and r.idx["k"].status == "scanned", (had, st)
        apex["on"] = False
        r._final_file = lambda p, wait_s=0: p
        r.ui.answer = False                                      # no scan of the new recording in this test
        r.tick()                                                 # game over: recording stops
        r.next_upload = 0
        for _ in range(40):
            r.tick()
            r.wait_uploads(30)
            if r.idx["k"].status == "uploaded":
                break
        assert r.idx["k"].status == "uploaded", r.idx["k"].note
        assert st["data"] == mic.read_bytes(), "the hub's copy differs"
        assert st["offsets"].count(0) == 1, st["offsets"]                    # part 1 never sent again
    finally:
        hubmod.PART_FILE, hubmod.PART_MAX = old
        hubmod.set_key(None)
        hubmod.reset()
        srv.shutdown()
        srv.server_close()


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
    assert settings.Settings(bitrate_mbps=4, bitrate_auto=False).clamp().bitrate_mbps == settings.MIN_MBPS == 8
    assert settings.Settings(bitrate_mbps=500, bitrate_auto=False).clamp().bitrate_mbps == settings.MAX_MBPS
    s = settings.Settings(folder=str(TMP / "q"))
    assert s.bitrate_mbps == 12 and 5 < s.gb_per_hour < 6
    obsmod.write_config(s, "av1_texture_amf", 2560, 1440)
    enc = json.loads((obsmod.CONFIG / "basic" / "profiles" / "PCS" / "recordEncoder.json").read_text())
    assert enc["rate_control"] == "CBR" and enc["bitrate"] == 10000 and enc["keyint_sec"] == 1, enc   # AV1: 10
    ini = (obsmod.CONFIG / "basic" / "profiles" / "PCS" / "basic.ini").read_text()
    assert "RecEncoder=av1_texture_amf" in ini and "FPSInt=60" in ini and "BaseCX=2560" in ini
    s.set_bitrate(16)
    assert obsmod.config_signature(s) != obsmod.config_signature(settings.Settings(folder=s.folder))


def test_bitrate_per_encoder():
    """Coach's decision (18 Nov 2026): AV1 records at 10 Mbit/s, HEVC and H.264 at 12. Players who never changed
    the bitrate get it (a saved 12, the old default, follows the encoder); a value set by hand is kept."""
    import json
    from pcs_recorder import obs as obsmod
    for enc in ("av1_texture_amf", "obs_nvenc_av1_tex", "jim_av1_nvenc", "obs_qsv11_av1"):
        assert settings.default_mbps(enc) == 10, enc
    for enc in ("h265_texture_amf", "h264_texture_amf", "obs_nvenc_hevc_tex", "jim_hevc_nvenc", "obs_nvenc_h264_tex",
                "jim_nvenc", "obs_qsv11_hevc", "obs_qsv11_v2", ""):
        assert settings.default_mbps(enc) == 12, enc
    prof = obsmod.CONFIG / "basic" / "profiles" / "PCS" / "recordEncoder.json"
    # a new player: the encoder test tries each candidate at its own bitrate, the setting follows the one picked
    s = settings.Settings(folder=str(TMP / "br"))
    for enc, mbps in (("av1_texture_amf", 10), ("h265_texture_amf", 12), ("obs_nvenc_h264_tex", 12)):
        obsmod.write_config(s, enc, 1920, 1080)
        assert json.loads(prof.read_text())["bitrate"] == mbps * 1000, enc
        s.encoder = enc
        assert s.clamp().bitrate_mbps == mbps and s.bitrate_auto, enc
        assert obsmod.config_signature(s).startswith(f"{enc}|{mbps}|")
    s.encoder = "av1_texture_amf"
    assert s.clamp().bitrate_mbps == 10 and 4 < s.gb_per_hour < 5
    # Settings page: the shown value comes back with every save (unchanged = still automatic)
    s.set_bitrate(10)
    assert s.bitrate_auto and s.bitrate_mbps == 10
    s.set_bitrate(14)                                       # the player's own value: kept on any encoder
    assert not s.bitrate_auto and s.clamp().bitrate_mbps == 14
    s.encoder = "h264_texture_amf"
    assert s.clamp().bitrate_mbps == 14 and s.mbps_for("av1_texture_amf") == 14
    s.encoder = "av1_texture_amf"
    s.set_bitrate(12)                                       # 12 on AV1 is a choice too
    assert not s.bitrate_auto and s.clamp().bitrate_mbps == 12
    s.set_bitrate(10)                                       # back to the recommendation: follows the encoder again
    assert s.bitrate_auto
    # existing recorder.json files (saved before this setting): only the old default moves
    saved_file = settings.SETTINGS_FILE
    try:
        settings.SETTINGS_FILE = TMP / "br" / "recorder.json"
        for enc, old, want, auto in (("av1_texture_amf", 12, 10, True), ("obs_nvenc_hevc_tex", 12, 12, True),
                                     ("h264_texture_amf", 12, 12, True), ("av1_texture_amf", 16, 16, False),
                                     ("av1_texture_amf", 8, 8, False), ("h264_texture_amf", 10, 10, False)):
            settings.SETTINGS_FILE.write_text(json.dumps({"encoder": enc, "bitrate_mbps": old}), encoding="utf-8")
            s = settings.load()
            assert (s.bitrate_mbps, s.bitrate_auto) == (want, auto), (enc, old, s.bitrate_mbps, s.bitrate_auto)
            on_disk = json.loads(settings.SETTINGS_FILE.read_text(encoding="utf-8"))
            assert on_disk["bitrate_mbps"] == want and on_disk["bitrate_auto"] is auto
            s2 = settings.load()                            # loaded again: unchanged (the migration runs once)
            assert (s2.bitrate_mbps, s2.bitrate_auto) == (want, auto)
        # an AV1 player who moved to 12 by hand after this version keeps 12
        settings.SETTINGS_FILE.write_text(json.dumps({"encoder": "av1_texture_amf", "bitrate_mbps": 12,
                                                      "bitrate_auto": False}), encoding="utf-8")
        assert settings.load().bitrate_mbps == 12
    finally:
        settings.SETTINGS_FILE = saved_file


def test_obs_password_renewed_once_while_obs_closed():
    """Versions up to 1.10.10 had the remote-control password written in app.log: it is replaced once by a new
    random one, only while our OBS is closed, written to OBS's config before OBS starts, saved in recorder.json,
    and the recorder connects with it."""
    import json
    from pcs_recorder import obs as obsmod
    from pcs_recorder import runtime
    s = settings.Settings(folder=str(TMP / "pw"))
    s.obs_password, s.obs_password_new = "old-logged-one", False
    running, events = [True], []

    class FakeOBS:
        def __init__(self, st):
            self.s = st

        def connect(self, wait_s=40):
            written = json.loads((obsmod.CONFIG / "plugin_config" / "obs-websocket" / "config.json").read_text())
            events.append(("connect", self.s.obs_password, written["server_password"]))

        def prepare(self, w, h, enc=None):
            pass
    saved = (obsmod.OBS, obsmod.installed, obsmod.is_running, obsmod.launch, obsmod.close, runtime.screen_size)
    obsmod.OBS, obsmod.installed = FakeOBS, lambda: True
    obsmod.is_running = lambda: running[0]
    obsmod.launch = lambda: (events.append("launch"), running.__setitem__(0, True))
    obsmod.close = lambda: (events.append("close"), running.__setitem__(0, False))
    runtime.screen_size = lambda: (1920, 1080)
    try:
        # OBS already running (it may be recording): not now
        assert obsmod.renew_password(s) is False and s.obs_password == "old-logged-one"
        a = runtime.ObsAdapter(s)
        a.ensure_ready()                     # left running by an older start: restarted, renewed while closed
        assert events[:2] == ["close", "launch"], events
        new = s.obs_password
        assert new != "old-logged-one" and len(new) >= 20 and s.obs_password_new
        assert events[2] == ("connect", new, new)                 # OBS got it, the recorder uses it
        on_disk = json.loads(settings.SETTINGS_FILE.read_text(encoding="utf-8"))
        assert on_disk["obs_password"] == new and on_disk["obs_password_new"] is True
        # next OBS start: same password (once only)
        running[0], a.ready = False, False
        a.ensure_ready()
        assert s.obs_password == new and events[-1] == ("connect", new, new)
        assert settings.load().obs_password == new
    finally:
        obsmod.OBS, obsmod.installed, obsmod.is_running, obsmod.launch, obsmod.close, runtime.screen_size = saved


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


def test_idle_threads_sleep_longer():
    """Waiting in the tray all day: the hotkey thread looks for key presses 20 times a second only while hotkeys are
    registered (a recording), else once a second; the message pump runs 5 times a second only for a while after
    something was shown, else once a second."""
    from pcs_recorder.runtime import Hotkeys, UI
    assert Hotkeys.delay(True) == 0.05 and Hotkeys.delay(False) >= 1.0
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception:
        print("  (no display: message pump test skipped)")
        return
    root.withdraw()
    try:
        ui = UI(root)
        assert ui._pump_ms() == UI.PUMP_IDLE_MS                        # nothing happened yet
        done = []
        ui.on_main(lambda: done.append(1))
        end = time.time() + 3
        while not done and time.time() < end:
            root.update()
            time.sleep(0.02)
        assert done and ui._pump_ms() == UI.PUMP_BUSY_MS              # something shown: quick for a while
        ui.busy_until = time.time() - 1
        assert ui._pump_ms() == UI.PUMP_IDLE_MS
    finally:
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


def _mm_picture(size=2048, seed=3):
    """A made-up map picture (the game's are EA's files: never in the repo): smooth blobs and a few roads."""
    import numpy as np
    from scipy import ndimage
    rng = np.random.default_rng(seed)
    g = ndimage.gaussian_filter(rng.normal(0, 1, (size, size)), 9)
    g = (g - g.min()) / (g.max() - g.min()) * 170 + 40
    for _ in range(40):                                        # roads: thin bright lines
        if rng.random() < 0.5:
            g[rng.integers(0, size), :] = 230
        else:
            g[:, rng.integers(0, size)] = 230
    return np.repeat(g.astype(np.uint8)[..., None], 3, 2)


def _mm_frame(pic, M, code, x, y, zoom, icon=True, brackets=True):
    """A 1440p frame whose HUD minimap shows the picture around world (x, y) at a zoom, like the game draws it."""
    import numpy as np
    from scipy import ndimage
    _, px, py, sc = M.MAPS[code]
    S = pic.shape[0]; k = zoom / sc * S / 4096
    fx, fy = (x - px) / sc / 4096, (py - y) / sc / 4096
    rr, cc = np.mgrid[66:386, 66:388].astype(np.float32)
    ys = fy * S + (rr + 0.5 - (M.CY + 0.5)) * k - 0.5; xs = fx * S + (cc + 0.5 - (M.CX + 0.5)) * k - 0.5
    img = np.zeros((1440, 2560, 3), np.uint8); img[:] = (60, 70, 80)
    v = ndimage.map_coordinates(pic[..., 0].astype(np.float32), [ys, xs], order=1, cval=35)
    img[66:386, 66:388] = np.clip(v, 0, 255).astype(np.uint8)[..., None]
    if icon:                                                   # the player's arrow and a ping: coloured, masked
        img[216:236, 216:236] = (40, 220, 230); img[120:150, 300:330] = (230, 200, 30)
    if brackets:
        img[64:66, 300:392] = 255; img[387:389, 300:392] = 255
    return img


def test_minimap_track():
    """The HUD minimap gives the player's position on the map's picture (players without the debug overlay): every
    read at the right place within a fraction of a percent, the zoom found when it changes, frames without the
    minimap (no corner brackets: menus, map open, spectating) skipped, a lone read with nothing to confirm it dropped,
    and no picture = an empty track that says so."""
    import json
    import numpy as np
    from PIL import Image
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_minimap as M
    pic = _mm_picture()
    d = TMP / "minimaps-rsx"; d.mkdir(exist_ok=True)
    Image.fromarray(pic).save(d / "mp_rr_district_mu1.png")    # the 2048 px copy, as on the hub
    os.environ["PCS_MINIMAPS"] = str(d); M._REFS.clear()
    assert set(M.pictures()) == {"ED"}
    assert M.grab(_mm_frame(pic, M, "ED", 0, 0, 78.0, brackets=False)) is None
    path = [(100.0 + 3 * i, -8000 + 600 * i, 5000 - 250 * i, 78.0 if i < 8 else 62.5) for i in range(14)]
    reads = [(t, M.grab(_mm_frame(pic, M, "ED", x, y, z))) for t, x, y, z in path]
    assert all(r is not None for _, r in reads)
    reads += [(140.5, reads[0][1]), (170.0, reads[0][1])]      # a read far from where the player was 1.5 s before; a lone read
    games = [{"tStart": 90.0, "tEndGame": 200.0, "map": "ED"}, {"tStart": 300.0, "map": "SP"}]
    M.attach(games, reads + [(320.0, reads[0][1])])
    tr = games[0]["minimapTrack"]
    assert tr["v"] == 1 and tr["every"] == M.EVERY and tr["reads"] == 16, tr
    ref = M.load_ref("ED")
    pts = {p[0]: p for p in tr["pts"]}
    assert 140.5 not in pts and 170.0 not in pts, tr
    for t, x, y, z in path:
        fx, fy = ref.frac(x, y)
        p = pts[round(t, 1)]
        assert abs(p[1] - 100 * fx) < 0.15 and abs(p[2] - 100 * fy) < 0.15, (t, p, 100 * fx, 100 * fy)
        assert 0 < p[3] <= 100
    assert games[1]["minimapTrack"] == {"v": 1, "every": M.EVERY, "pts": [], "reads": 1, "noPicture": True}
    json.dumps(games)                                          # plain JSON for the scan file
    # the app's data folder: pictures downloaded from the hub, used only when the hub's transform is this one
    os.environ.pop("PCS_MINIMAPS"); M._REFS.clear()
    data = TMP / "mm-data"; (data / "minimaps").mkdir(parents=True, exist_ok=True)
    Image.fromarray(pic).save(data / "minimaps" / ("BM-" + "a" * 32 + ".img"), "JPEG")
    Image.fromarray(pic).save(data / "minimaps" / ("KC-" + "b" * 32 + ".img"), "JPEG")
    (data / "minimaps" / "index.json").write_text(json.dumps({"maps": {
        "BM": {"asset": "a" * 32, "pos_x": -45000, "pos_y": 43500, "scale": 21, "size": 4096},
        "KC": {"asset": "b" * 32, "pos_x": -1, "pos_y": 43886, "scale": 20, "size": 4096}}}))
    old = os.environ.get("PCS_DATA_DIR"); os.environ["PCS_DATA_DIR"] = str(data)
    try:
        assert set(M.pictures()) == {"BM"}
    finally:
        if old is None: os.environ.pop("PCS_DATA_DIR")
        else: os.environ["PCS_DATA_DIR"] = old


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


def test_sound_ring_cross_check():
    """Screen and sound check each other: a weak alarm right after a countdown read on screen confirms it (only
    there: the same weak alarm elsewhere is ignored), and an alarm while the screen shows the ring closing is not a
    ring start (kept apart with the reason)."""
    import json
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_audio as A
    # the alarm level per 64 ms frame (what tone_level gives): loud alarms at 30 and 120 s, weak ones (8 dB, under
    # the normal 9 dB bar) at 70 and 160 s, background 2 dB
    T = np.arange(0, 200, A.HOP / A.SR)
    level = np.full(len(T), 2.0)
    for start, db in ((30.0, 25.0), (70.0, 8.0), (120.0, 25.0), (160.0, 8.0)):
        for k in range(3):
            level[(T >= start + 1.75 * k) & (T < start + 1.75 * k + 1.0)] = db
    c = A.TONE_CUES["ring_moving"]
    found = {"seconds": 200.0, "cues": {"ring_moving": A.runs(T, level, c["min_db"], c["min_len"], c["merge"])},
             "_ring": (T, level)}
    assert [round(c["t"]) for c in found["cues"]["ring_moving"]] == [30, 120], found["cues"]
    games = [{"tStart": 0, "ring": [
        {"kind": "wait", "firstSeen": 10, "endsAt": 29.6},
        {"kind": "wait", "firstSeen": 50, "endsAt": 69.8},         # weak alarm at 70: confirmed by the screen
        {"kind": "closing", "firstSeen": 100, "endsAt": 140},      # alarm at 120 while closing: not a ring start
    ]}]
    A.attach(games, found)
    r = games[0]["ring"]
    assert abs(r[0]["soundAt"] - 30) < 0.5 and "soundWeak" not in r[0], r
    assert abs(r[1]["soundAt"] - 70) < 0.6 and r[1]["soundWeak"] is True and r[1]["soundDb"] > 7, r
    assert "soundAt" not in r[2], r
    assert games[0]["ringSound"] == [], games[0]
    assert [(round(x["t"]), x["why"]) for x in games[0]["ringSoundOther"]] == [(120, "closing")], games[0]
    assert found["ringWeak"] == 1 and "_ring" not in found
    json.dumps(found)                                         # what the scan saves stays plain JSON
    # a countdown still running far from its end also rejects an alarm; no countdown: the alarm is kept
    assert A.screen_says_not_ring([{"kind": "wait", "firstSeen": 0, "endsAt": 100}], 50) == "countdown"
    assert A.screen_says_not_ring([{"kind": "wait", "firstSeen": 0, "endsAt": 100}], 99) is None
    assert A.screen_says_not_ring([], 50) is None


def test_sound_damage_single_read():
    """A damage total that rests on one read is kept when the game was loud between the read before it and that
    read (hits), and refused when it was quiet; totals read twice are never touched."""
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_audio as A
    sr = A.SR
    rng = np.random.default_rng(3)
    a = rng.normal(0, 0.003, sr * 300).astype(np.float32)          # quiet game sound
    a[int(98.5 * sr):int(99.5 * sr)] += rng.normal(0, 0.2, sr).astype(np.float32)   # gunfire before the read at 100
    loud = A.loudness(a)
    assert len(loud) == 300 / A.LOUD_STEP

    def game(t, prev, single=True):
        return {"tStart": 0, "tEndGame": 290, "own": {"kills": 1, "damage": 400, "damageAt": t, "damageLastRead": t,
                                                      "damageSingleRead": single, "damagePrevReadAt": prev}}
    games = [game(100, 98), game(200, 198), game(200, 198, single=False), game(100, 99.8)]
    assert A.check_damage(games, loud) == 1
    kept, refused, twice, close = (g["own"] for g in games)
    assert kept["damage"] == 400 and kept["damageSoundDb"] > A.DAMAGE_QUIET_DB, kept
    assert "damage" not in refused and refused["damageRefused"] == "quiet" and refused["kills"] == 1, refused
    assert twice["damage"] == 400 and "damageSoundDb" not in twice, twice
    assert close["damage"] == 400, close                     # the window starts 1 s before the previous read
    # through attach (as the scan calls it)
    found = {"cues": {"ring_moving": []}, "_loud": loud}
    gs = [game(200, 198)]
    A.attach(gs, found)
    assert found["damageRefused"] == 1 and "_loud" not in found and "damage" not in gs[0]["own"]
    # the developer tool that scores these checks on a scan
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import audio_cross
    before = [{"tStart": 0, "tEndGame": 300, "ring": [{"kind": "wait", "endsAt": 100, "soundAt": 100.4},
                                                       {"kind": "wait", "endsAt": 200}, {"kind": "wait", "endsAt": 400}],
               "ringSound": [150.0], "own": {"damage": 400, "damageAt": 200, "damageSingleRead": True}}]
    after = [{"tStart": 0, "tEndGame": 300, "ring": [{"kind": "wait", "endsAt": 100, "soundAt": 100.4},
                                                      {"kind": "wait", "endsAt": 200, "soundAt": 200.5, "soundWeak": True},
                                                      {"kind": "wait", "endsAt": 400}],
              "ringSound": [], "ringSoundOther": [{"t": 150.0, "why": "closing"}], "own": {"damageRefused": "quiet"}}]
    rows, tot = audio_cross.score(before, after)
    assert tot == {"rings": 2, "heardBefore": 1, "heardAfter": 2, "weak": 1, "unexplainedBefore": 1, "unexplainedAfter": 0,
                   "other": 1, "singleDamage": 1, "damageRefused": 1}, tot


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


def test_maps_broken_moon_places():
    """Broken Moon's named places from the game's full map (Season 31) are in the map data, so landings in the
    middle of the map get a name: centres only, ids as on the hub, unique, inside the map (0..100 %).
    Storm Point's "Lift" carries the game's name "Mountain Lift" (id kept: the hub uses sp-lift)."""
    import json as _json
    maps = _json.loads((Path(__file__).resolve().parent.parent / "scanner" / "pcs_maps.json").read_text(encoding="utf-8"))
    bm = {p["id"]: p for p in maps["BM"]["pois"]}
    new = {"bm-quarantine-zone": "Quarantine Zone", "bm-hazmat-tunnel": "Hazmat Tunnel", "bm-cliff-side": "Cliff Side",
           "bm-underpass": "Underpass", "bm-space-port": "Space Port", "bm-experimental-labs": "Experimental Labs",
           "bm-solar-pods": "Solar Pods"}
    for i, name in new.items():
        assert i in bm and bm[i]["name"] == name, i
        assert 0 <= bm[i]["x"] <= 100 and 0 <= bm[i]["y"] <= 100, i
        assert "poly" not in bm[i], i
    for mp, d in maps.items():
        ids = [p["id"] for p in d["pois"]]
        assert len(ids) == len(set(ids)), f"{mp}: duplicate POI ids"
    assert len({p["id"] for d in maps.values() for p in d["pois"]}) == sum(len(d["pois"]) for d in maps.values())
    sp = {p["id"]: p for p in maps["SP"]["pois"]}
    assert sp["sp-lift"]["name"] == "Mountain Lift"
    assert not [p for p in maps["SP"]["pois"] if p["name"] == "Lift"]
    # the middle of Broken Moon now has a name nearby (it used to fall 17-25 % from any named place)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_scan
    assert pcs_scan.poi_at("BM", 46.48, 47.56)[0] == "bm-quarantine-zone"
    assert pcs_scan.poi_at("BM", 57.0, 59.0)[0] == "bm-hazmat-tunnel"


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


def test_scrim_drop_screen_read_under_a_banner():
    """The scrim drop screen shows the map's name at the top left (custom lobbies: "WORLD'S EDGE (SEASON 17)"), which
    the loading-banner reader reads as a banner with unknown words. The drop screen must still be read there: from
    1.10.8 to 1.10.14 a banner read ended the keyframe, every scrim lost its drop screen and its scrim mode."""
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_hud as HUD
    import pcs_scan as SCAN

    def word(img, y0, y1, seed, n):                                  # made-up block letters starting at x = 194
        r = np.random.default_rng(seed); h = y1 - y0
        for k in range(n):
            x = 194 + k * (h + 8)
            img[y0:y1, x:x + h] = 255
            for _ in range(2):
                yy, xx = int(r.integers(0, h - h // 3)), int(r.integers(h // 4, h - h // 3))
                img[y0 + yy:y0 + yy + h // 3, x + xx:x + h] = 60
    img = np.full((1440, 2560, 3), 60, np.uint8)
    word(img, 138, 198, 1, 6); word(img, 273, 298, 2, 9)
    assert HUD.Banner.read(img), "the test frame must read as a banner"
    banner_only = img.copy()
    img[200:1000, 1600:2400] = (90, 40, 40)                          # marks the frame as "drop screen" for the stub below
    spots = [{"poi": f"we-{k}", "inside": True, "x": 10.0 + k, "y": 20.0, "kind": "enemy"} for k in range(20)]
    real = SCAN.read_dropscreen
    SCAN.read_dropscreen = lambda a, loose=False: ({"map": "WE", "err": 0.02, "spots": spots}
                                                   if (a[600, 2000] == (90, 40, 40)).all() else None)
    try:
        frames = [(100.0 + k, img) for k in range(4)] + [(104.0 + k, banner_only) for k in range(3)]
        events, samples, _ = SCAN.scan_frames(frames, quiet=True)
    finally:
        SCAN.read_dropscreen = real
    kinds = [e["kind"] for e in events]
    assert kinds == ["banner", "drop"] * 4 + ["banner"] * 3, kinds              # a banner alone stays a banner
    games = SCAN.assemble(events); SCAN.timeline(games, samples)
    assert len(games) == 1 and games[0]["drop"] and games[0]["map"] == "WE", games
    assert games[0]["mode"] == "scrim" and games[0]["modeSeen"]["from"] == ["drop"], games[0].get("modeSeen")

    # Ranked's drop-point screen (17 Nov 2026 update) looks the same, titled "RANKED - DROPZONE": the banner reads
    # mode ranked on it. It is kept as data (rankedDrop) and never starts a game or proves a scrim.
    keep = HUD.BANNER_T
    HUD.BANNER_T = {"mode": {"ranked": [HUD.Banner.read(banner_only)["modeSig"]]}, "map": {}}
    SCAN.read_dropscreen = lambda a, loose=False: ({"map": "SP", "err": 0.02, "spots": spots}
                                                   if (a[600, 2000] == (90, 40, 40)).all() else None)
    try:
        events, samples, _ = SCAN.scan_frames([(600.0 + k, img) for k in range(3)], quiet=True)
    finally:
        SCAN.read_dropscreen = real; HUD.BANNER_T = keep
    assert [e["kind"] for e in events] == ["banner", "rankedDrop"] * 3, [e["kind"] for e in events]
    assert all(e["data"]["ranked"] for e in events if e["kind"] == "rankedDrop")
    sq = [{"slot": 1, "legend": "alpha", "self": True, "locked": True, "hue": 40}]
    events += [{"t": 638.0 + 2 * k, "kind": "select", "data": {"squad": sq, "ranked": True}} for k in range(14)]
    games = SCAN.assemble(events); SCAN.timeline(games, [])
    assert len(games) == 1 and games[0]["drop"] is None and games[0]["tStart"] == 664.0, games
    rd = games[0]["rankedDrop"]
    assert (rd["tFirst"], rd["tLast"], rd["frames"], len(rd["spots"]), rd["ranked"]) == (600.0, 602.0, 3, 20, True), rd
    assert games[0]["mode"] == "ranked" and games[0]["banner"]["mode"] == "ranked", games[0]["modeSeen"]

    # any ranked sign beats a drop screen read without the RANKED title; Wildcard / pubs signs don't (as before)
    def seen(**kw):
        g = {"tStart": 100.0, "drop": {"map": "WE", "spots": spots}, **kw}
        return g, SCAN.mode_seen(g, kw.pop("R", []), [], [100.0], 0)
    g, m = seen()
    assert m["mode"] == "scrim" and m["from"] == ["drop"] and "ranked" not in g["drop"], m
    for kw in ({"select": {"ranked": True}}, {"banner": {"mode": "ranked"}}):
        g, m = seen(**kw)
        assert m["mode"] == "ranked" and "drop" not in m["from"] and g["drop"]["ranked"] is True, (kw, m)
    g, m = seen(R=[1, 1, 1, 0])
    assert m["mode"] == "ranked" and g["drop"]["ranked"] is True, m
    g, m = seen(banner={"mode": "wildcard"})
    assert m["mode"] == "scrim" and "ranked" not in g["drop"], m


def test_mode_from_playlist_emblem():
    """The playlist emblem at the top right (pubs: red "unranked" emblem, Wildcard: teal) gives a game its mode when
    nothing else does (games[].modeSeen); signs that disagree, or another playlist's emblem, leave it unlabelled.
    Ranked (RP panel) and scrims (drop screen) keep their old rules; a label set by hand still wins."""
    import numpy as np
    from PIL import Image
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_hud as HUD
    import pcs_scan as SCAN
    from pcs_recorder import labels
    E = HUD.ModeEmblem
    modes = {m: k for k, m, _, _ in E.T if m}
    assert set(modes) == {"pubs", "wildcard"} and len(E.T) >= 5, [(k, m) for k, m, _, _ in E.T]
    rng = np.random.default_rng(3)

    def scene():
        img = np.full((1440, 2560, 3), 90, np.uint8)
        img[:300, 2160:] = rng.integers(60, 200, (300, 400, 3), dtype=np.uint8)
        return img

    def emblem(img, key, y, x):
        _, _, rgb, mask = next(e for e in E.T if e[0] == key)
        h, w = E.SIZE
        big = np.asarray(Image.fromarray(rgb.astype(np.uint8)).resize((w, h), Image.NEAREST))
        m = np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).resize((w, h), Image.NEAREST)) > 0
        box = img[y:y + h, x:x + w]; box[m] = big[m]
        return img
    for mode, key in modes.items():
        for (y, x) in ((8, 2422), (10, 2420), (142, 2420)):          # in game, a few px off, waiting screen
            r = E.read(emblem(scene(), key, y, x))
            assert r and r[0] == key and r[1] == mode, (mode, y, x, r)
    other = next(k for k, m, _, _ in E.T if not m)
    r = E.read(emblem(scene(), other, 8, 2422))
    assert r and r[0] == other and r[1] is None, r
    for _ in range(5):
        assert E.read(scene()) is None
    assert E.read(np.zeros((1440, 2560, 3), np.uint8)) is None

    # per game: a vote of the reads in the game's window (from 150 s before its start to 150 s before the next)
    P, W = modes["pubs"], modes["wildcard"]
    reads = [(t, P, "pubs", 10.0) for t in (520, 600, 700, 900)] + [(t, W, "wildcard", 12.0) for t in (1500, 1600, 1700)]
    assert HUD.emblem_vote(reads, 500, 1350)[:2] == (P, "pubs")
    assert HUD.emblem_vote(reads, 1350, 9e9)[:2] == (W, "wildcard")
    assert HUD.emblem_vote(reads[:2], 0, 9e9)[:2] == (None, None)                       # two reads: not enough
    assert HUD.emblem_vote(reads, 0, 9e9)[:2] == (None, None)                           # 4 against 3: no majority

    def game(t, **kw):
        g = SCAN.new_game(t, "SP"); g.update(kw); return g
    games = [game(650, select={"tFirst": 650, "tLast": 680, "ranked": False, "squad": []}),
             game(1550), game(3000, banner={"mode": "pubs", "map": "SP"}),
             game(4000, banner={"mode": "pubs", "map": "SP"}),
             game(5000), game(6000, drop={"spots": [], "tLast": 6000}),
             game(7000, select={"tFirst": 7000, "tLast": 7030, "ranked": True, "squad": []})]
    reads += [(t, W, "wildcard", 12.0) for t in (3900, 3950, 4100, 4200)]              # banner pubs vs emblem wildcard
    reads += [(t, other, None, 15.0) for t in (4900, 5000, 5100)]                      # another playlist
    reads += [(t, P, "pubs", 9.0) for t in (5900, 6000, 6100, 7000, 7100, 7200)]      # scrim drop / ranked select
    rank = [(t, True) for t in range(7000, 7100, 10)]
    SCAN.timeline(games, [], rank=rank, emblem=reads)
    got = [(g["mode"], g["modeSeen"]["mode"], g["modeSeen"]["conflict"]) for g in games]
    assert got == [("pubs", "pubs", False), ("wildcard", "wildcard", False), ("pubs", "pubs", False),
                   ("unknown", None, True), ("unknown", None, False), ("scrim", "scrim", False),
                   ("unknown", None, True)], got
    assert games[0]["modeSeen"]["from"] == ["emblem"] and games[0]["modeSeen"]["emblemReads"] == {P: 4}
    assert games[2]["modeSeen"]["from"] == ["banner"] and games[4]["modeSeen"]["emblem"] == other
    assert games[6]["modeSeen"]["from"] == ["badge", "emblem", "select"], games[6]["modeSeen"]
    # the RP panel alone keeps ranked; the banner's RANKED word now counts too
    g1, g2 = game(100), game(900, banner={"mode": "ranked", "map": "ED"})
    SCAN.timeline([g1, g2], [], rank=[(t, True) for t in range(100, 400, 10)])
    assert (g1["mode"], g2["mode"]) == ("ranked", "ranked") and g1["modeSeen"]["from"] == ["badge"], (g1["modeSeen"], g2["modeSeen"])
    g4 = game(4950, select={"tFirst": 4950, "tLast": 4980, "ranked": True, "squad": []})
    SCAN.timeline([g4], [], emblem=[r for r in reads if r[1] == other])                # rank badge read as another emblem
    assert g4["mode"] == "ranked" and not g4["modeSeen"]["conflict"], g4["modeSeen"]
    g3 = game(100)
    SCAN.timeline([g3], [], mode="scrim", emblem=reads)                                 # --mode still wins
    assert g3["mode"] == "scrim"

    # labels: pubs / wildcard reach the block label, with the sign that told it; by hand wins
    labels.FILE.unlink(missing_ok=True)
    folder = TMP / f"emb-{time.time_ns()}"
    folder.mkdir()
    pa, pb = str(folder / "a.mp4"), str(folder / "b.mp4")
    sa = {"games": [{"mode": "pubs", "modeSeen": {"mode": "pubs", "from": ["emblem"]}}]}
    sb = {"games": [{"mode": "unknown", "modeSeen": {"mode": None, "from": ["banner", "emblem"], "conflict": True}}]}
    r = labels.explain_block([pa, pb], [sa, sb])
    assert r["label"] == "pubs" and r["source"] == "scan" and "playlist emblem" in r["why"], r
    sw = {"games": [{"mode": "wildcard", "modeSeen": {"mode": "wildcard", "from": ["banner", "emblem"]}}]}
    assert labels.explain_block([pa, pb], [sa, sw])["label"] == "wildcard"
    assert labels.explain(pb, sb)["label"] is None
    labels.set_label(pa, "ranked")
    assert labels.explain_block([pa, pb], [sa, sw]) == {"label": "ranked", "source": "hand", "why": "set by hand"}
    labels.FILE.unlink(missing_ok=True)
    # the ranked banner since 17 Nov 2026 says "RANKED - DROPZONE" (one wide word): taught from 18 Nov
    dz = "e4aaee077777f57a4aa8a055553954acea8a055552954aeecca655552956eaecca657552976eeee8a057574934eeaa8a056574934aaaa8a055544934aaaaee075747f17b2aaee065747617"
    assert HUD.Banner.near(dz, 14.05, HUD.BANNER_T["mode"]) == "ranked"

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
    assert o["damagePrevReadAt"] == 42, o                   # where the sound check looks for the hits


def test_game_end_time():
    """games[].tEndGame = the last in-game HUD read (ring clock, squads counter, own strip, full map) before the next
    game starts; the debug overlay alone does not count (it also shows in the firing range). tEnd is unchanged.
    Ring phases first seen after the next game started are dropped; a countdown ending after the game ended for
    this recording is marked endsAfterGame."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_scan as SCAN

    def s(t, ring=None, pos=True):          # [t, label, ring value, spectating, x, y, z, vel, fine, pre]
        return [t, 0, ring, False] + ([1.0, 2.0, 3.0] if pos else [None, None, None]) + [0.0, False, False]
    samples = [s(100, 300), s(400, 50), s(460, None), s(900, None)]           # game 1: ring read last at 400, then overlay only
    samples += [s(1000, 200), s(1100, 10)]                                       # game 2
    games = [{"tStart": 90.0, "tEnd": 90.0, "ring": [{"firstSeen": 100.0, "endsAt": 400.0}, {"firstSeen": 400.0, "endsAt": 450.0},
                                                      {"firstSeen": 960.0, "endsAt": 1100.0}]},
             {"tStart": 950.0, "tEnd": 950.0, "ring": []}]
    SCAN.game_ends(games, samples, squads=[(420, 5), (990, 20)], own=[(430, {"damage": 5}), (1150, {"kills": 1})])
    g1, g2 = games
    assert g1["tEnd"] == 90.0 and g1["tEndGame"] == 430.0, g1               # own strip at 430; overlay at 460 / 900 not
    assert g2["tEndGame"] == 1150.0, g2                                       # last game: no cap
    assert [r["firstSeen"] for r in g1["ring"]] == [100.0, 400.0]             # the phase at 960 is the next game's
    assert "endsAfterGame" not in g1["ring"][0] and g1["ring"][1]["endsAfterGame"] is True
    lone = [{"tStart": 5.0, "tEnd": 5.0}]
    assert SCAN.game_ends(lone, [], None, None)[0]["tEndGame"] == 5.0         # nothing read: ends where it starts


def _ring_frame(cur, nxt, decoy=None, seed=3):
    """A synthetic full-map frame (2560 x 1440): blotchy map art in the map square, red tint outside the current ring
    with a dark line on it, a see-through white disc for the next ring, optionally a purple disc (another map
    feature). Circles in screen px (x, y, r). No game picture: numbers only."""
    import numpy as np
    from scipy import ndimage
    rng = np.random.default_rng(seed)
    a = np.full((1440, 2560, 3), 30, np.float32)
    h, w = 1340, 1353
    blot = ndimage.gaussian_filter(rng.random((h, w)), 20)
    blot = (blot - blot.min()) / (np.ptp(blot) + 1e-9)
    navy, tan = np.array([25, 45, 60], np.float32), np.array([190, 150, 85], np.float32)
    m = (blot > 0.55)[..., None]
    art = np.where(m, tan, navy) + rng.normal(0, 6, (h, w, 3))
    for k in range(40):                        # white labels / icons
        y, x = rng.integers(20, h - 30), rng.integers(20, w - 120)
        art[y:y + 14, x:x + 90:6] = 235
    a[0:h, 604:604 + w] = art
    Y, X = np.mgrid[0:1440, 0:2560]
    if nxt:
        d = np.hypot(X - nxt[0], Y - nxt[1]) < nxt[2]
        a[d] = a[d] * 0.85 + 255 * 0.15
    if decoy:
        d = np.hypot(X - decoy[0], Y - decoy[1]) < decoy[2]
        a[d] = a[d] * 0.7 + np.array([150, 60, 230]) * 0.3
    if cur:
        dist = np.hypot(X - cur[0], Y - cur[1])
        out = (dist > cur[2])[..., None] & (X < 604 + w)[..., None] & (X >= 604)[..., None] & (Y < h)[..., None]
        a = np.where(out, a * 0.55 + np.array([200, 70, 50], np.float32) * 0.45, a)
        line = (np.abs(dist - cur[2]) < 2.5) & (X >= 604) & (X < 604 + w) & (Y < h)
        a[line] *= 0.3
    return np.clip(a, 0, 255).astype(np.uint8)


def test_ring_circles_on_full_map():
    """The ring reader finds the current ring (red tint outside) and the next ring (white see-through disc) on the
    full map, in map % through the map transform, and refuses a coloured disc of another map feature."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import numpy as np
    import pcs_ring
    M = pcs_ring.M_DEFAULT
    pct = lambda x, y, r: (x * M[0, 0] + M[2, 0], y * M[1, 1] + M[2, 1], r * M[0, 0])
    cur, nxt = (1500, 740, 410), (1580, 900, 200)
    r = pcs_ring.read(_ring_frame(cur, nxt, decoy=(1000, 400, 130)))
    for key, want in (("cur", cur), ("next", nxt)):
        got, exp = r[key], pct(*want)
        assert got, (key, r)
        assert abs(got["cx"] - exp[0]) < 0.5 and abs(got["cy"] - exp[1]) < 0.5 and abs(got["r"] - exp[2]) < 0.5, (key, got, exp)
    # first ring stage: no current ring on the map yet, the next ring alone (bigger than half the map, partly off it)
    r = pcs_ring.read(_ring_frame(None, (1350, 650, 560), decoy=(800, 1100, 150), seed=5))
    exp = pct(1350, 650, 560)
    assert r["cur"] is None and r["next"], r
    assert abs(r["next"]["cx"] - exp[0]) < 0.5 and abs(r["next"]["r"] - exp[2]) < 0.5, (r, exp)
    # nothing drawn: nothing read (the purple disc is not a ring)
    r = pcs_ring.read(_ring_frame(None, None, decoy=(1300, 700, 200), seed=7))
    assert r == {"cur": None, "next": None}, r
    # a zoomed map is skipped
    assert pcs_ring.read(_ring_frame(cur, nxt), np.array([[0.05, 0], [0, 0.05], [-20, 0]])) is None
    # 4:3 stretched: the unzoomed map has the stretched layout's scale (scanner ST_MAP_M0); it is read, not skipped
    M43 = np.array([[100 / 1264, 0], [0, 100 / 1264], [-648 * 100 / 1264, -40 * 100 / 1264]])
    r = pcs_ring.read(_ring_frame(cur, nxt, decoy=(1000, 400, 130)), M43)
    assert r and r["cur"] and r["next"], r
    assert abs(r["cur"]["r"] - cur[2] * M43[0, 0]) < 0.5 and abs(r["next"]["r"] - nxt[2] * M43[0, 0]) < 0.5, r


def test_ring_ends_and_speed():
    """Per game: map-frame reads grouped by ring stage, the end circle of each ring (next ring shown during the round,
    or the current circle during the next round's wait), and the closing speed = radius drop / closing time."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_ring, pcs_hud
    ring = [{'round': 1, 'kind': 'wait', 'endsAt': 175.0, 'firstSeen': 100.0, 'firstV': 75},
            {'round': 1, 'kind': 'closing', 'endsAt': 440.0, 'firstSeen': 184.0, 'firstV': 256},
            {'round': 2, 'kind': 'wait', 'endsAt': 565.0, 'firstSeen': 445.0, 'firstV': 120},
            {'round': 2, 'kind': 'closing', 'endsAt': 665.0, 'firstSeen': 570.0, 'firstV': 95}]
    c = lambda cx, cy, r: {'cx': cx, 'cy': cy, 'r': r}
    reads = [(150.0, 'ED', {'cur': None, 'next': c(50.0, 50.0, 30.0)}),
             (151.0, 'ED', {'cur': None, 'next': c(50.1, 49.9, 30.1)}),
             (152.0, 'ED', {'cur': None, 'next': c(70.0, 20.0, 5.0)}),            # a misread: outvoted
             (500.0, 'ED', {'cur': c(50.05, 50.0, 30.05), 'next': c(55.0, 52.0, 15.0)}),
             (501.0, 'ED', {'cur': c(50.0, 50.05, 30.0), 'next': c(55.1, 52.0, 15.1)}),
             (600.0, 'ED', {'cur': c(52.0, 51.0, 22.0), 'next': c(55.0, 52.05, 15.0)}),
             (605.0, 'ED', {'cur': c(52.4, 51.2, 21.0), 'next': None}),          # closing: 0.2 % a second
             (610.0, 'ED', {'cur': c(52.8, 51.4, 20.0), 'next': None}),
             (2500.0, 'ED', {'cur': c(1, 1, 1), 'next': None})]                    # the next game's: left out
    games = [{'map': 'ED', 'tStart': 100.0, 'ring': ring}, {'map': 'ED', 'tStart': 2000.0, 'ring': []}]
    pcs_ring.attach(games, reads)
    g = games[0]
    st = [(e['stage'], e['phase']) for e in g['ringCircles']]
    assert st == [(1, 'wait'), (2, 'wait'), (2, 'closing')], st
    w1 = g['ringCircles'][0]
    assert 'r' not in w1 and 'mapR' not in w1 and w1['next']['r'] == 30.05 and abs(w1['conf'] - 2 / 3) < 0.01, w1
    w2 = g['ringCircles'][1]          # the review player's map reads {t, mapX, mapY, mapR}
    assert (w2['t'], w2['mapX'], w2['mapY'], w2['mapR'], w2['round']) == (500.0, w2['cx'], w2['cy'], w2['r'], 2), w2
    ends = {e['ring']: e for e in g['ringEnds']}
    assert ends[1]['from'] == ['current', 'next'] and ends[1]['agree'] and abs(ends[1]['r'] - 30.0) < 0.1, ends[1]
    assert abs(ends[2]['r'] - 15.0) < 0.1 and ends[2]['from'] == ['next'], ends[2]
    u = pcs_ring.units_per_pct('ED')
    assert u == pcs_hud.CAL['ED'][0] and ends[2]['rUnits'] == round(ends[2]['r'] * u), (u, ends[2])
    sp = {s['method']: s for s in g['ringSpeed']}
    assert abs(sp['closing']['unitsPerS'] - 0.2 * u) < 0.5 and sp['closing']['ring'] == 2, sp
    sp = [sp['ends']]
    assert len(sp) == 1 and sp[0]['ring'] == 2 and sp[0]['seconds'] == 100.0, sp
    assert abs(sp[0]['unitsPerS'] - (ends[1]['rUnits'] - ends[2]['rUnits']) / 100.0) < 0.2, sp
    assert [e['t'] for e in games[1]['ringCircles']] == [2500.0], games[1]


def _icons_frame(care=(), other=(), red=(), dark=(), seed=4):
    """A synthetic full-map frame with map icons (numbers only, no game picture): care packages (a light diamond outline
    around the helmet drawn from pcs_mapicons.CARE_SIGNS), other diamonds (another glyph), red octagons (strongholds that
    can be taken) and dark octagons with a light outline (taken). Centres in screen px."""
    import numpy as np
    import pcs_mapicons as I
    rng = np.random.default_rng(seed)
    a = np.clip(rng.normal(70, 12, (1440, 2560, 3)), 0, 255).astype(np.float32)
    a[0:1340, 604:1957] = np.clip(a[0:1340, 604:1957] * [1.6, 0.8, 0.7], 0, 255)          # red-tinted map outside the ring
    for k in range(30):                                                                   # white labels
        y, x = rng.integers(40, 1300), rng.integers(620, 1800)
        a[y:y + 14, x:x + 90:6] = 235
    Y, X = np.mgrid[-30:31, -30:31]
    D = np.abs(X) + np.abs(Y)
    O = np.maximum(np.maximum(np.abs(X), np.abs(Y)), (np.abs(X) + np.abs(Y)) / np.sqrt(2))
    S = np.array([c == '#' for c in I.CARE_SIGNS]).reshape(I.G, I.G)
    k = 0.62 * 22
    gi = np.clip(((X + k) / (2 * k) * I.G).astype(int), 0, I.G - 1)
    gj = np.clip(((Y + k) / (2 * k) * I.G).astype(int), 0, I.G - 1)
    inside_sq = (np.abs(X) < k) & (np.abs(Y) < k)

    def put(cx, cy, mask, col):
        a[cy - 30:cy + 31, cx - 30:cx + 31][mask] = col
    for cx, cy, glyph in [(x, y, S) for x, y in care] + [(x, y, ~S[:, ::-1].T) for x, y in other]:
        put(cx, cy, D < 26, (40, 40, 40))
        put(cx, cy, (D >= 22) & (D < 25.5), (230, 230, 230))
        put(cx, cy, inside_sq & (D < 17) & glyph[gj, gi], (235, 235, 235))
    door = np.array([c == '#' for c in I.DOOR_SIGNS]).reshape(I.G, I.G)       # True = the red fill
    kd = 12.4
    di = np.clip(((X + kd) / (2 * kd) * I.G).astype(int), 0, I.G - 1); dj = np.clip(((Y + kd) / (2 * kd) * I.G).astype(int), 0, I.G - 1)
    in_door = (np.abs(X) < kd) & (np.abs(Y) < kd)
    for cx, cy in red:
        put(cx, cy, O < 21, (20, 20, 20)); put(cx, cy, O < 18, (233, 50, 85)); put(cx, cy, in_door & ~door[dj, di], (20, 20, 20))
    for cx, cy in dark:
        put(cx, cy, O < 22, (25, 25, 25)); put(cx, cy, (O >= 18) & (O < 19.5), (225, 225, 225))
        put(cx, cy, in_door & ~door[dj, di], (190, 190, 190))
    return a.astype(np.uint8)


def test_map_icons_care_packages_and_strongholds():
    """Care packages (diamond with the helmet) and strongholds (red or dark octagon) are found on the full map, in map %
    through the map transform; another diamond icon (a survey beacon) and labels are not care packages."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_mapicons as I, pcs_ring
    M = pcs_ring.M_DEFAULT
    pct = lambda x, y: (x * M[0, 0] + M[2, 0], y * M[1, 1] + M[2, 1])
    care, other, red, dark = [(1000, 500), (1500, 900)], [(1200, 300), (800, 1000)], [(1300, 600)], [(900, 700)]
    r = I.read(_icons_frame(care, other, red, dark), M)
    got = sorted((round(c['px']), round(c['py'])) for c in r['care'])
    assert len(got) == 2 and all(abs(g[0] - w[0]) <= 2 and abs(g[1] - w[1]) <= 2 for g, w in zip(got, sorted(care))), r['care']
    c0 = min(r['care'], key=lambda c: c['px']); e = pct(1000, 500)
    assert abs(c0['x'] - e[0]) < 0.3 and abs(c0['y'] - e[1]) < 0.3 and c0['style'] == 'white' and c0['score'] >= I.CARE_MIN, c0
    st = sorted((s['state'], round(s['px']), round(s['py'])) for s in r['strongholds'])
    assert [s[0] for s in st] == ['available', 'unavailable'], st
    assert abs(st[0][1] - 1300) <= 2 and abs(st[0][2] - 600) <= 2 and abs(st[1][1] - 900) <= 3 and abs(st[1][2] - 700) <= 3, st
    # nothing there: nothing read
    assert I.read(_icons_frame(), M) == {'care': [], 'strongholds': []}


def test_map_icons_per_game():
    """Per game: reads of one place make one care package (seen count, first seen), numbered by the ring phases
    (1-2 after ring 1 starts closing, 3 in round 2, 4 in round 3), checked against the next ring's circle; reads before
    ring 1 starts closing are dropped; strongholds keep their last state; a taken one seen once only (never red) is not kept."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_mapicons as I
    ring = [{'round': 1, 'kind': 'wait', 'endsAt': 300.0}, {'round': 1, 'kind': 'closing', 'endsAt': 560.0},
            {'round': 2, 'kind': 'wait', 'endsAt': 690.0}, {'round': 2, 'kind': 'closing', 'endsAt': 790.0},
            {'round': 3, 'kind': 'wait', 'endsAt': 885.0}, {'round': 3, 'kind': 'closing', 'endsAt': 935.0}]
    ends = [{'ring': 2, 'cx': 50.0, 'cy': 50.0, 'r': 15.0}, {'ring': 3, 'cx': 52.0, 'cy': 48.0, 'r': 8.0}]
    cp = lambda x, y, st='white': {'care': [{'x': x, 'y': y, 'score': 0.8, 'style': st}], 'strongholds': []}
    sh = lambda x, y, st: {'care': [], 'strongholds': [{'x': x, 'y': y, 'state': st}]}
    reads = [(200.0, 'ED', cp(10.0, 10.0)),                            # before ring 1 starts closing: not a package
             (320.0, 'ED', {'care': [{'x': 45.0, 'y': 52.0, 'score': 0.8, 'style': 'white'},
                                     {'x': 70.0, 'y': 30.0, 'score': 0.8, 'style': 'white'}], 'strongholds': []}),
             (330.0, 'ED', cp(45.3, 51.8)), (600.0, 'ED', cp(45.1, 52.1, 'opened')),
             (650.0, 'ED', cp(53.0, 47.0)), (700.0, 'ED', cp(53.1, 47.2)),
             (400.0, 'ED', sh(30.0, 60.0, 'available')), (800.0, 'ED', sh(30.5, 60.2, 'unavailable')),
             (810.0, 'ED', sh(80.0, 80.0, 'unavailable')),             # taken, seen once, never red: dropped
             (820.0, 'ED', sh(10.0, 90.0, 'unavailable')), (830.0, 'ED', sh(10.1, 90.0, 'unavailable')),   # seen twice: kept
             (5000.0, 'ED', cp(20.0, 20.0))]                            # the next game's
    games = [{'map': 'ED', 'tStart': 100.0, 'ring': ring, 'ringEnds': ends}, {'map': 'ED', 'tStart': 4000.0, 'ring': []}]
    I.attach(games, reads)
    P = games[0]['carePackages']
    assert [(p['n'], p['seenCount'], p['t']) for p in P] == [(1, 3, 320.0), (2, 1, 320.0), (3, 2, 650.0)], P
    a, b, c = P
    assert a['openedAt'] == 600.0 and a['styles'] == ['white', 'opened'] and a['nSure'], a
    assert a['nextRing'] == 2 and a['inNextRing'] and abs(a['mapX'] - 45.1) < 0.01, a
    assert b['inNextRing'] is False and c['nextRing'] == 3 and c['inNextRing'] and c['nSure'], (b, c)
    assert a['inRings'] == {'2': True, '3': True} and b['inRings'] == {'2': False, '3': False}, (a, b)
    assert games[0]['carePackageRingCheck'] == {'checked': 3, 'inside': 2, 'checkedClosing': 1, 'insideClosing': 1}, games[0]['carePackageRingCheck']
    S = games[0]['strongholds']
    assert len(S) == 2 and S[0]['state'] == 'unavailable' and S[0]['availableLast'] == 400.0 and S[0]['seenCount'] == 2, S
    assert S[1]['mapX'] == 10.05 and S[1]['state'] == 'unavailable' and 'availableLast' not in S[1], S
    assert [p['t'] for p in games[1]['carePackages']] == [5000.0] and games[1]['carePackages'][0]['nSure'] is False


def _card_frame(cards, dy=0):
    """Bottom-left squad cards (synthetic, rows as measured at 1440p): per card a white name at x 184, then a white
    health bar (alive) or a lime status line (dead). cards: list of (name row, 'alive' | 'dead', frame RGB, letters)."""
    import numpy as np
    a = np.full((1440, 2560, 3), 40, np.uint8)
    for y, state, col, n in cards:
        y += dy
        a[y - 12:y + 50, 70:110] = col                         # coloured frame next to the portrait
        for k in range(n):                                     # name: letters 3 px wide, 6 px apart
            a[y:y + 18, 184 + 9 * k:187 + 9 * k] = 235
        if state == 'alive':
            a[y + 27:y + 31, 186:330] = (150, 60, 230)         # shield
            a[y + 33:y + 43, 185:300] = 240                    # health
        elif state == 'dead':
            for k in range(22):
                a[y + 27:y + 44, 186 + 9 * k:190 + 9 * k] = (160, 196, 64)
    return a


def test_respawn_cards_and_events():
    """Squad cards: alive (bar) and dead (lime status line) are told apart; a teammate dead for a while and then alive
    again in the same game is a respawn; the player's own death = spectating, back in his own view = respawned."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_respawn as P
    teal, orange = (40, 200, 200), (240, 150, 30)
    cs = P.read_cards(_card_frame([(1058, 'alive', teal, 1), (1146, 'dead', orange, 10)]))    # a one-letter name too
    assert [c['state'] for c in cs] == ['alive', 'dead'] and abs(cs[0]['y'] - 1058) <= 3 and abs(cs[1]['y'] - 1146) <= 3, cs
    assert P.hue_name(cs[0]['hue']) == 'teal'
    rows = P.slot_rows([1058, 1059, 1146, 1147, 1146, 1058, 1240])          # the two rows read most (Wildcard: 1035 / 1149)
    assert rows == [1058.0, 1146.0] and P.slot_of(1150, rows) == 1 and P.slot_of(1100, rows) is None, rows
    assert P.read_cards(_card_frame([])) == []
    # the spectator layout puts the cards 85 px higher: read too, but such frames are dropped by own_view
    assert [c['state'] for c in P.read_cards(_card_frame([(1058, 'dead', teal, 4)], dy=-85))] == ['dead']
    A = lambda: {'y': 1146, 'state': 'alive', 'hue': 35}
    D = lambda: {'y': 1146, 'state': 'dead', 'hue': 80}
    T0 = {'y': 1058, 'state': 'alive', 'hue': 180}
    reads = [(t, [dict(T0), D()], False) for t in range(100, 130)]
    reads += [(130, [dict(T0), A()], False)]                     # one odd read: not a respawn yet
    reads += [(t, [dict(T0), D()], False) for t in range(131, 140)]
    reads += [(t, [dict(T0), A()], None) for t in range(140, 150)]   # no ring read on these frames: nearest sample
    reads += [(t, [dict(T0), D()], True) for t in range(150, 160)]   # spectating: someone else's cards
    reads += [(t, [dict(T0), A()], True) for t in range(160, 170)]
    samples = [[float(t), 0, 60.0, t >= 150, 0, 0, 0, 0.0, False, False] for t in range(100, 170, 2)]
    games = [{'tStart': 90.0}, {'tStart': 1000.0}]
    P.attach(games, reads, samples)
    r = games[0]['respawns']
    assert len(r) == 1 and r[0]['t'] == 140 and r[0]['who'] == 'teammate' and r[0]['slot'] == 2, r
    assert r[0]['color'] == 'orange' and r[0]['deadSince'] == 100 and r[0]['lastDeadRead'] == 139, r
    # own death and respawn: spectator layout from 200 to 230, own view again at 236, then a fall from the dropship
    S = [[float(t), 0, 60.0, False, 0, 0, 3000, 0.0] for t in range(150, 200, 2)]
    S += [[float(t), 0, 60.0, True, None, None, None, None] for t in range(200, 232, 2)]
    S += [[float(t), 0, 60.0, False, 0, 0, 9000 - t, 1200.0 if t < 250 else 300.0] for t in range(236, 300, 2)]
    S += [[float(t), 0, 60.0, True, None, None, None, None] for t in range(300, 320, 2)]   # dies again: game over
    deaths, own = P.own_deaths(S, 100, 1000)
    assert deaths == [200.0, 300.0], deaths
    assert own == [{'t': 236.0, 'who': 'self', 'how': 'dropship', 'deadSince': 200.0}], own


def test_banner_pickup_and_beacon_from_status_lines():
    """A dead card's status line says where the banner is (shapes in pcs_banner.json 'status'): DEATHBOX RECOVERY,
    DELIVER TO BEACON (picked up), AWAITING RESPAWN (beacon used), RESPAWNING IN ss.cc (the mode's own respawn); a
    respawn gets bannerAt / banner / beaconAt from 2 reads in a row of each."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import numpy as np
    import pcs_respawn as P
    assert set(P.STATUS) >= {'box', 'held', 'awaiting', 'craft', 'respawning', 'timer'}
    for k in ('box', 'held', 'awaiting', 'craft'):
        m = np.zeros((17, 250), bool); m[:, 6:6 + P.SW] = P.STATUS[k]
        assert P.status_of(m) == k, k
    m = np.zeros((17, 250), bool); m[:, 6:6 + P.SW] = P.STATUS['respawning']; m[3:14, 190:200] = True
    assert P.status_of(m) == 'timer'                                          # the line runs on: a timer
    assert P.status_of(np.zeros((17, 250), bool)) is None
    noise = np.random.default_rng(1).random((17, 250)) > 0.5
    assert P.status_of(noise) is None                                         # grass, not words
    D = lambda t, st: (float(t), st)
    steps = P.banner_steps([D(100, 'box'), D(101, 'box'), D(105, 'held'), D(106, None), D(107, 'held'),
                            D(120, 'awaiting'), D(121, 'awaiting'), D(125, 'respawning')])
    assert steps == {'bannerAt': 105.0, 'banner': 'picked', 'beaconAt': 120.0}, steps
    steps = P.banner_steps([D(100, 'craft'), D(101, 'craft'), D(110, 'held'), D(111, 'held'), D(112, 'awaiting')])
    assert steps == {'bannerAt': 110.0, 'banner': 'crafted'}, steps                 # one awaiting read: not trusted
    assert P.banner_steps([D(1, 'timer'), D(2, 'timer')]) == {'modeRespawn': True}
    assert P.banner_steps([D(1, None), D(2, None)]) == {}


def _own_card_frame(state, tier=(40, 140, 240), shield=0, health=100, dx=0):
    """A 1440p frame with the player's own card bottom left: shield segments (25 points each, 63 px apart from x 236)
    over a white health bar (rows 1344..1359), or the knocked look (red name, thin yellow bleed-out bar)."""
    import numpy as np
    a = np.full((1440, 2560, 3), 40, np.uint8)
    a[1300:1370, 200:600] = (60, 52, 58)                                      # the card's dark plate
    x0 = 236 + dx
    if state == 'alive':
        for k in range(4):                                                    # 4 segments: dark when empty
            a[1328:1340, x0 + 63 * k:x0 + 63 * k + 58] = (70, 70, 75)
        full, part = divmod(shield, 25)
        for k in range(full): a[1328:1340, x0 + 63 * k:x0 + 63 * k + 58] = tier
        if part: a[1328:1340, x0 + 63 * full:x0 + 63 * full + round(58 * part / 25)] = tier
        a[1344:1360, x0:x0 + round(317 * health / 100)] = (250, 250, 250)
    else:
        a[1290:1316, x0:x0 + 160:3] = (230, 30, 30)                            # red name strokes
        a[1348:1356, x0:x0 + 300] = (200, 157, 0)
    return a


def test_own_bars_and_knocks():
    """The player's own card: tier, shield points and health are read from the bars; a knocked card (yellow bleed-out
    bar, red name) is told apart; per game, a knock needs 2 reads, menus inside it are fine, an alive card soon after =
    revived; bar values enter the timeline only when two reads agree."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_bars as B
    r = B.read_own(_own_card_frame('alive', (40, 140, 240), 50, 60))
    assert r['state'] == 'alive' and r['tier'] == 'blue' and abs(r['shield'] - 50) <= 1 and abs(r['health'] - 60) <= 1, r
    r = B.read_own(_own_card_frame('alive', (175, 45, 235), 88, 100, dx=3))   # the card shakes a few px when hit
    assert r['tier'] == 'purple' and abs(r['shield'] - 88) <= 2 and r['health'] == 100, r
    r = B.read_own(_own_card_frame('alive', (198, 196, 200), 50, 100))        # the white tier is a light grey
    assert r['tier'] == 'white' and r['shield'] == 50, r
    r = B.read_own(_own_card_frame('alive', None, 0, 30))
    assert r['tier'] is None and r['shield'] == 0 and abs(r['health'] - 30) <= 1, r
    pink = _own_card_frame('alive', None, 0, 100); pink[1344:1360, 236 + 100:236 + 317] = (249, 207, 208)
    assert B.read_own(pink)['health'] == 100                                  # pink health after a revive counts
    assert B.read_own(_own_card_frame('knocked')) == {'state': 'knocked', 'y': 1348}
    import numpy as np
    assert B.read_own(np.zeros((1440, 2560, 3), np.uint8)) is None
    # teammates: the bars under an alive card found by pcs_respawn.read_cards
    f = _card_frame([(1146, 'alive', (240, 150, 30), 8)])
    f[1146 + 24:1146 + 32, 185:420] = 40
    f[1146 + 24:1146 + 32, 185:185 + 42 + 39] = (40, 140, 240)                # 2 segments blue
    f[1146 + 33:1146 + 43, 185:420] = 40; f[1146 + 33:1146 + 43, 185:185 + 108] = 240    # health half full
    m = B.read_mates(f, [{'y': 1146, 'state': 'alive', 'hue': 35}])
    assert m[0]['tier'] == 'blue' and abs(m[0]['shield'] - 50) <= 1 and abs(m[0]['health'] - 50) <= 1, m
    # knocks per game
    K, A = {'state': 'knocked'}, {'state': 'alive', 'tier': 'blue', 'shield': 75, 'health': 100}
    reads = [(10, A), (11, K), (12, K), (13, None), (20, K), (22, A),        # knocked, a menu, revived
             (40, K), (41, A),                                               # one knocked read: not trusted
             (60, K), (61, K), (62, None)]                                   # never up again: died
    ks = B.own_knocks(reads, 0, 100)
    assert ks == [{'t': 11.0, 'end': 20.0, 'reads': 3, 'outcome': 'revived'},
                  {'t': 60.0, 'end': 61.0, 'reads': 2, 'outcome': 'died'}], ks
    tl = B.timeline([(1, A), (2, dict(A, shield=74)), (3, dict(A, shield=30)), (4, dict(A, shield=40)), (5, dict(A, shield=41)),
                     (6, K), (7, K), (8, dict(A, shield=0)), (9, dict(A, shield=0))])
    assert tl == [[1.0, 'blue', 75, 100], [4.0, 'blue', 40, 100], [6.0, 'knocked'], [8.0, 'blue', 0, 100]], tl
    games = [{'tStart': 0.0}, {'tStart': 50.0}]
    bar_reads = [(float(t), K if 11 <= t <= 13 else A, [{'y': 1058, 'tier': 'blue', 'shield': 75, 'health': 100}], False)
                 for t in range(5, 60)]
    B.attach(games, bar_reads, [])
    assert games[0]['ownKnocks'][0]['t'] == 11.0 and games[0]['ownKnocks'][0]['outcome'] == 'revived', games[0]
    st = games[0]['shieldTimeline']
    assert st['cols'] == ['t', 'tier', 'shield', 'health'] and st['self'][0] == [5.0, 'blue', 75, 100], st
    assert st['mates'] == [[[5.0, 'blue', 75, 100]]] and games[1]['ownKnocks'] == [], (st, games[1])


def test_highlights_key_plays():
    """pcs_highlights: the player's own takedowns need two sources (kill line in his colour + kills number, a fight or
    damage); a line captured twice counts once; a kill line the kills number did not follow is not his; a knock and its
    kill are one enemy; chains of 8 s give multi-kills (3+), quick doubles (2 within 5 s), squad wipes (3 kills),
    clutches (2 enemies while last alive, fight won); Settings regroup them; best first."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_highlights as H
    assert H.parse_rules("8,3,5") == {'chainS': 8.0, 'multiMin': 3, 'doubleS': 5.0} == H.rules()
    assert H.parse_rules("") == H.rules() and H.parse_rules("x,y") == H.rules()
    assert H.parse_rules("99,1,40") == {'chainS': 30.0, 'multiMin': 2, 'doubleS': 30.0}
    # the kills plate only shows once above 0: reads of the strip without it before count as 0
    own = [(t, {'damage': 10}) for t in range(100, 110)] + [(t, {'kills': 1, 'damage': 80}) for t in range(112, 120)] + \
          [(120.0, {'kills': 7, 'damage': 80})] + [(t, {'kills': 3, 'damage': 200}) for t in range(124, 130)]
    assert H.counter_rises(own, 'kills', 0, 1e9) == [(112, 109, 1), (124, 119, 2)]      # one misread 7 never counts
    assert H.counter_rises(own + [(131.0, {'kills': 2}), (132.0, {'kills': 2})], 'kills', 0, 1e9) == []   # went down
    SQ = {'select': {'squad': [{'slot': 1, 'hue': 40, 'self': True}, {'slot': 2, 'hue': 180, 'self': False},
                               {'slot': 3, 'hue': 77, 'self': False}]}}
    def line(t, ev, hue=40, sig='a' * 36, weapon='r301', y=300):
        return {'t': float(t), 'y': y, 'event': ev, 'weapon': weapon, 'killer': {'side': 'squad', 'hue': hue},
                'victim': {'side': 'other'}, 'victimSig': sig}
    fight = lambda a, b, res='won': {'t0': a, 't1': b, 'result': res, 'ours': {'knocks': 1, 'kills': 1}}
    reads = lambda kills: [(float(t), {'kills': v, 'damage': 100 + 50 * v}) for t, v in kills]
    # two knocks 3 s apart (one captured twice), both killed: a quick double; the teammate's kill line is not ours
    lines = [line(200, 'knock', sig='1' * 36), line(202, 'knock', sig='3' * 36), line(203, 'knock', sig='2' * 36, weapon='flatline'),
             line(207, 'kill', sig='1' * 36), line(208, 'kill', sig='2' * 36, weapon='flatline'), line(209, 'kill', hue=180)]
    k = reads([(t, 0) for t in range(190, 206)] + [(t, 2) for t in range(210, 230)])
    g = {'tStart': 0.0, **SQ, 'fights': [fight(199, 209)]}
    td = H.takedowns(g, lines, k, 0, 1e9)
    assert [(e['t'], e['kind']) for e in td] == [(200, 'knock'), (203, 'knock'), (207, 'kill'), (208, 'kill')], td
    assert td[2]['sources'] == ['counter', 'damage', 'feed', 'fight']
    hl = H.find(td, g['fights'], [])
    assert len(hl) == 1 and hl[0]['kind'] == 'double' and hl[0]['count'] == 2 and hl[0]['t0'] == 200 and hl[0]['t1'] == 208
    assert hl[0]['gaps'] == [3.0, 4.0, 1.0] and hl[0]['kills'] == 2 and hl[0]['knocks'] == 2
    assert H.find(td, g['fights'], [], H.rules(8, 3, 2)) == []                            # stricter double: not one
    # a kill line in our colour while the kills number holds still around it: a teammate's (or misread), dropped
    still = reads([(t, 1) for t in range(290, 320)])
    assert H.takedowns(g, [line(300, 'kill')], still, 0, 1e9) == []
    # a single line with no second source never counts
    assert H.takedowns({'tStart': 0.0, **SQ}, [line(300, 'knock')], [], 0, 1e9) == []
    # kills the feed missed: the kills number + damage alone, three in a chain = multi-kill and squad wipe
    k3 = [(float(t), {'kills': 0, 'damage': 100}) for t in range(380, 392)] + \
         [(float(t), {'kills': 1, 'damage': 300}) for t in range(394, 398)] + \
         [(float(t), {'kills': 3, 'damage': 500}) for t in range(401, 410)]
    td3 = H.takedowns({'tStart': 0.0, **SQ}, [], k3, 0, 1e9)
    assert [e['sources'] for e in td3] == [['counter', 'damage']] * 3, td3
    h3 = H.find(td3, [], [])
    assert h3[0]['kind'] == 'wipe' and h3[0]['tags'] == ['multi', 'wipe'] and h3[0]['count'] == 3
    assert h3[0]['score'] > hl[0]['score']
    # clutch: two enemies while no teammate card is alive, in a fight that ends won
    bars = [(float(t), {'state': 'alive'}, [{'y': 1}, {'y': 2}], False) for t in range(500, 510)] + \
           [(float(t), {'state': 'alive'}, [], False) for t in range(510, 530)]
    assert H.last_alive(bars, 0, 1e9) == [[510.0, 529.0]]
    tdc = [{'t': 515.0, 'kind': 'knock', 'sources': ['feed', 'fight']}, {'t': 519.0, 'kind': 'knock', 'sources': ['feed', 'fight']},
           {'t': 522.0, 'kind': 'kill', 'sources': ['counter', 'fight']}]
    assert [h['tags'] for h in H.find(tdc, [fight(512, 524, 'lost')], [[510.0, 529.0]])] == [['double']]   # lost: no clutch
    hc = H.find(tdc, [fight(512, 524)], [[510.0, 529.0]])
    assert hc[0]['kind'] == 'clutch' and hc[0]['tags'] == ['double', 'clutch'] and 'cards' in hc[0]['sources'] and hc[0]['t0'] == 510.0
    # attach adds the fields to every game and never breaks a scan
    games = [{'tStart': 0.0, **SQ, 'fights': [fight(199, 209)]}, {'tStart': 1000.0}]
    H.attach(games, lines, k, [])
    assert games[0]['highlights'][0]['kind'] == 'double' and games[1]['highlights'] == [] and games[1]['takedowns'] == []
    bad = [{'tStart': 0.0, 'select': 'broken'}]
    H.attach(bad, lines, k, [])
    assert bad[0]['highlights'] == [] and 'highlightsError' in bad[0]


def test_fights_and_ring_damage():
    """pcs_fights: ring damage from the red screen edges and health ticks (shield untouched); fights from our squad's
    kill-feed lines, own knocks and hits: kills decide, the player's own death counts, a wiped squad lost; one line
    captured twice counts once; a name in a colour none of our squad has is not ours; third party = more enemy names
    than a squad, or shooting after every opponent died."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import numpy as np
    import pcs_fights as F
    a = np.zeros((1440, 2560, 3), np.uint8); a[...] = (90, 100, 110)
    assert F.read_storm(a) == (0.0, 0.0, 0.0, 0.0) and not F.storm_outside(F.read_storm(a))
    a[..., 0] = 230; a[..., 1] = 90; a[..., 2] = 60                            # red-orange haze everywhere
    assert F.storm_outside(F.read_storm(a))
    b = np.zeros((1440, 2560, 3), np.uint8); b[0:24] = (230, 90, 60)            # only the top strip: low health vignette
    assert not F.storm_outside(F.read_storm(b))
    A = lambda t, s, h: (float(t), {'state': 'alive', 'tier': 'blue', 'shield': s, 'health': h}, [], False)
    # ring: 3 health a tick for 8 reads, shield 50 untouched; a fight hit (shield) is not ring damage
    bars = [A(t, 50, 100) for t in range(100, 110)] + [A(110 + i, 50, 97 - 3 * i) for i in range(8)] +            [A(t, 50, 76) for t in range(118, 125)] + [A(125, 20, 76), A(126, 20, 76)]
    storm = [(float(t), (0.9, 0.9, 0.8, 0.7) if 111 <= t <= 117 else (0.0, 0.0, 0.0, 0.0)) for t in range(100, 127)]
    g = [{'tStart': 90.0, 'ring': [{'round': 1, 'kind': 'closing', 'firstSeen': 100.0, 'endsAt': 300.0}]}]
    F.attach(g, [], bars, storm)
    rd = g[0]['ringDamage']
    assert len(rd['segments']) == 1 and rd['segments'][0]['perTick'] == 3.0 and rd['segments'][0]['round'] == 1, rd
    assert 21 <= rd['measured'] <= 24 and rd['damage'] >= rd['measured'] and 6 <= rd['seconds'] <= 12, rd
    # ticks alone (no storm read), shield up: still ring; one or two drops: not
    g = [{'tStart': 90.0}]
    F.attach(g, [], [A(100 + i, 75, 100 - 3 * i) for i in range(6)], [])
    assert g[0]['ringDamage']['segments'] and g[0]['ringDamage']['segments'][0]['how'] == 'ticks'
    F.attach(g, [], [A(100, 75, 100), A(101, 75, 97), A(102, 75, 97)], [])
    assert g[0]['ringDamage']['segments'] == []
    # fights
    sq = lambda hue: {'side': 'squad', 'hue': hue}
    other = {'side': 'other'}
    sig1, sig2 = '0' * 36, 'f' * 36
    sig3 = '00ff' * 9
    line = lambda t, ev, k, v, ks=None, vs=None, y=290, w='r301': {'t': float(t), 'event': ev, 'weapon': w, 'killer': k,
                                                                     'victim': v, 'killerSig': ks, 'victimSig': vs, 'y': y}
    game = lambda: {'tStart': 0.0, 'tEndGame': 900.0, 'select': {'squad': [{'slot': 1, 'hue': 40, 'self': True},
                                                                            {'slot': 2, 'hue': 77}, {'slot': 3, 'hue': 179}]}}
    feed = [line(100, 'knock', sq(40), other, vs=sig1), line(103, 'knock', sq(40), other, vs=sig1, y=330),   # same line again, lower
            line(110, 'kill', sq(77), other, vs=sig1), line(112, 'knock', other, sq(179), ks=sig2),
            line(130, 'knock', sq(210), other, vs=sig3),                       # blue: none of us
            line(400, 'knock', other, sq(40), ks=sig3)]
    g = [game()]
    g[0]['ownKnocks'] = [{'t': 401.0, 'end': 420.0, 'reads': 10, 'outcome': 'died'}]
    g[0]['ownDeaths'] = [421.0]
    F.attach(g, feed, [A(t, 50, 100) for t in range(90, 140)], [])
    f1, f2 = g[0]['fights']
    assert (f1['ours'], f1['theirs'], f1['result']) == ({'knocks': 1, 'kills': 1}, {'knocks': 1, 'kills': 0}, 'won'), f1
    assert f1['opponents'] == 2 and not f1['thirdParty'] and f1['t0'] <= 100 <= 112 <= f1['t1'] < 130, f1
    assert f2['theirs'] == {'knocks': 1, 'kills': 1} and f2['result'] == 'lost', f2                     # he died in it
    assert F.verdict({'knocks': 3, 'kills': 0}, {'knocks': 1, 'kills': 0}) == 'won'
    assert F.verdict({'knocks': 1, 'kills': 0}, {'knocks': 0, 'kills': 0}) == 'even'
    assert F.verdict({'knocks': 2, 'kills': 2}, {'knocks': 0, 'kills': 0}, wiped=True) == 'lost'
    # third party: a fourth enemy name in one fight
    sigs = ['0' * 36, 'f' * 36, '0f' * 18, 'f0' * 18]
    g = [game()]
    F.attach(g, [line(200 + 5 * k, 'knock', sq(77), other, vs=s_, w='w%d' % k) for k, s_ in enumerate(sigs)], [], [])
    f = g[0]['fights'][0]
    assert f['opponents'] == 4 and f['thirdParty'] and f['thirdPartyWhy'] == 'new enemy', f
    # shooting after every opponent we met died
    g = [game()]
    loud = np.full(int(400 / 0.05), -50.0, np.float32); loud[int(300 / 0.05):int(330 / 0.05)] = -10.0
    F.attach(g, [line(300, 'kill', sq(40), other, vs=sigs[0])], [], [], loud, 0.05)
    f = g[0]['fights'][0]
    assert f['result'] == 'won' and f['thirdParty'] and f['thirdPartyWhy'] == 'gunfire after opponents died', f


def _usage_frame(item=None, bar=(40, 140, 240), p=0.4, tac=None, ult=None):
    """A frame with the heal panel (item icon + progress bar) and / or the ability icons bottom centre."""
    import numpy as np
    a = np.full((1440, 2560, 3), 60, np.uint8)
    if item:
        a[928:930, 1790:2200] = 235; a[1023, 1790:2200] = 235                   # the panel's frame lines
        a[991:1010, 1800:2172] = 25                                            # dark track
        a[991:1010, 1800:1800 + int(p * 372)] = bar                            # fill
        a[945:1012, 1640:1716] = 30
        if item == 'cell': a[958:998, 1660:1692] = (40, 60, 200)
        if item == 'battery': a[951:988, 1660:1692] = (40, 60, 200); a[990:1004, 1658:1696:3] = 240
        if item == 'medkit': a[956:1000, 1647:1708] = (210, 30, 30)
        if item == 'syringe': a[960:996, 1650:1700] = 240; a[975:985, 1672:1680] = (210, 30, 30)
        if item == 'phoenix': a[950:1000, 1655:1700] = 240; a[1000:1008, 1655:1700] = (240, 140, 30)
        if item == 'revive': a[935:1016, 1580:1626] = (40, 90, 230); a[950:1000, 1650:1700] = 240
    def hint(x0, y0=1383):
        a[y0:y0 + 24, x0:x0 + 28] = 240; a[y0 + 6:y0 + 18, x0 + 10:x0 + 18] = 20
    def text(x0, x1, y0=1386):
        a[y0:y0 + 19, x0:x1:3] = 240
    if tac == 'ready': a[1313:1355, 784:827:2] = 240; hint(806)
    if tac == 'cooldown': a[1313:1355, 784:827:2] = 110; text(801, 866)
    if ult == 'ready':
        a[1212:1366, 1193:1200] = (30, 140, 250); a[1212:1366, 1360:1367] = (30, 140, 250); a[1212:1219, 1193:1367] = (30, 140, 250)
        a[1260:1336, 1250:1308:2] = 240; hint(1266)
    if ult == 'charging': a[1260:1336, 1250:1308:2] = 120; text(1256, 1304)
    return a


def test_usage_heals_and_abilities():
    """Heal panel: the item from its icon (cell, battery, syringe, med kit, phoenix), the bar's colour and fill; a revive
    is not a heal. Ult / tactical: ready (key hint) vs charging / cooldown (text). Per game: a heal needs 2 panel reads or
    the bars going up after it; a use = ready, then the other state read twice soon after; spectating frames are left
    out; the legend comes from the legend select screen."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import numpy as np
    import pcs_usage as U
    for item, bar in (('cell', (40, 140, 240)), ('battery', (40, 140, 240)), ('medkit', (250, 10, 10)),
                      ('syringe', (250, 10, 10)), ('phoenix', (40, 140, 240))):
        h = U.read_heal(_usage_frame(item, bar, 0.4))
        assert h and h['item'] == item and abs(h['p'] - 0.4) < 0.02, (item, h)
    assert U.read_heal(_usage_frame('cell', (250, 10, 10)))['item'] is None   # a shield icon with a red bar: not trusted
    assert U.read_heal(_usage_frame('revive'))['item'] == 'revive'
    assert U.read_heal(_usage_frame()) is None and U.read_heal(np.zeros((1440, 2560, 3), np.uint8)) is None
    ab = U.read_abilities(_usage_frame(tac='ready', ult='ready'))
    assert ab['tac'] == 'ready' and ab['ult'] == 'ready' and ab['ultMask'].sum() > 0, ab
    ab = U.read_abilities(_usage_frame(tac='cooldown', ult='charging'))
    assert ab['tac'] == 'cooldown' and ab['ult'] == 'charging', ab
    assert U.read_abilities(_usage_frame()) == {'tac': None, 'ult': None}
    # packing for the scan's chunks keeps the masks
    row = U.read(_usage_frame('cell', tac='ready', ult='ready'), 5.0, {})
    back = U.unpack([row])[0]
    assert back[1]['item'] == 'cell' and back[2]['tacMask'].shape == (U.TAC_ICON[1] - U.TAC_ICON[0], U.TAC_ICON[3] - U.TAC_ICON[2])
    # per game
    C = lambda p: {'item': 'cell', 'bar': 'blue', 'p': p, 'dy': 0}
    B = lambda p: {'item': 'battery', 'bar': 'blue', 'p': p, 'dy': 0}
    R, Ch, T, Cd = {'tac': 'ready', 'ult': 'ready'}, {'tac': 'ready', 'ult': 'charging', 'ultText': 24}, {'tac': 'cooldown', 'tacText': True, 'ult': 'ready'}, {'tac': None, 'ult': None}
    usage = [(10.0, C(0.2), R), (11.0, C(0.6), R), (12.0, None, R),           # a cell, 2 reads
             (20.0, B(0.2), R), (21.0, B(0.4), R), (22.0, B(0.6), R), (23.0, B(0.8), R), (24.0, B(0.1), R), (25.0, B(0.3), R),
             (30.0, C(0.5), R),                                               # one read, no bars: not trusted
             (40.0, None, R), (41.0, None, T), (42.0, None, T), (50.0, None, Cd),    # tactical used at 41
             (60.0, None, R), (61.0, None, Ch), (62.0, None, Ch),                    # ult used at 61
             (70.0, None, R), (71.0, None, T),                                       # one cooldown read only
             (80.0, None, R), (81.0, None, T), (82.0, None, T)]                      # spectating: someone else's HUD
    alive = lambda s: {'state': 'alive', 'tier': 'blue', 'shield': s, 'health': 100}
    bars = [(9.0, alive(25), [], False), (13.0, alive(50), [], False), (19.0, alive(0), [], False), (26.0, alive(100), [], False)] +            [(t, {'state': 'spectating'}, [], True) for t in (80.0, 81.0, 82.0)]
    games = [{'tStart': 0.0, 'select': {'squad': [{'legend': 'seer', 'self': True}, {'legend': 'lifeline', 'self': False}]}},
             {'tStart': 100.0}]
    U.attach(games, usage, bars)
    hs = [(h['item'], h['done']) for h in games[0]['heals']]
    assert hs == [('cell', True), ('battery', True), ('battery', False)], games[0]['heals']   # 2nd battery: no bars, bar 0.3
    assert abs(games[0]['heals'][0]['t'] - 9.5) < 0.01, games[0]['heals'][0]     # started before the first read
    assert games[0]['abilities'] == [{'t': 41.0, 'kind': 'tactical', 'legend': 'seer'}, {'t': 61.0, 'kind': 'ult', 'legend': 'seer'}], games[0]['abilities']
    assert games[1]['heals'] == [] and games[1]['abilities'] == []
    # legend signs: a sign compared with itself is that legend
    leg, iou, margin = U.legend_of('ult', U._bits([s for s in U.SIGNS if s[:2] == ('ult', 'seer')][0][2]))
    assert leg == 'seer' and iou == 1.0, (leg, iou, margin)


def _ammo_frame(mag=None, res=None, heal=None, item=None, hint=False, slot=2):
    """A 1440p frame with the ammo panel and heal slot drawn from the reader's own digit shapes (pcs_banner.json 'ammo'):
    mag 2 cells, reserve 3 (leading zeros dim), heal count right-aligned at x 717."""
    import numpy as np
    from PIL import Image
    import pcs_ammo as A
    a = np.full((1440, 2560, 3), 25, np.uint8)
    def put(font, lab, box, ink=230, back=30):
        labels, S = A.T[font]
        g = S[list(labels).index(lab)].reshape(18, 12)
        x0, x1, y0, y1 = box
        im = np.asarray(Image.fromarray((g * 255).astype(np.uint8)).resize((x1 - x0, y1 - y0), Image.BILINEAR)) / 255.0
        a[y0:y1, x0:x1] = (back + im * (ink - back))[..., None].astype(np.uint8)
    B = A.cell_boxes(a)
    if mag is not None:
        for k, ch in enumerate(f'{mag:02d}'): put('M', ch, B['M'][k], 120 if (k == 0 and ch == '0' and mag) else 230)
    if res is not None:
        s = f'{res:03d}'; lead = len(s) - len(s.lstrip('0'))
        for k, ch in enumerate(s): put('R', ch, B['R'][k], 55 if k < lead and k < 2 else 200)
    if heal is not None:
        x = 717
        for ch in reversed(str(heal)):
            w = 7 if ch == '1' else 12
            put('H', ch, (x - w - 1, x + 1, A.HEAL_ROWS[0], A.HEAL_ROWS[1]), 245, 30); x -= w + 4
    if item == 'shield': a[1322:1360, 656:684] = (40, 70, 200)
    if hint: a[1380:1406, 673:701] = 235
    if slot == 2: a[1390:1402, 2300:2420:2] = 240
    if slot == 1: a[1390:1402, 2080:2240:2] = 240
    return a


def _ban_tiles(d):
    """A fake private icons file with two made-up legend tiles (smooth random grey, no game image) -> (path, tiles)."""
    import numpy as np
    from PIL import Image
    rng = np.random.default_rng(7)
    tiles = {}
    for n in ('alpha', 'beta'):
        low = rng.integers(20, 235, (8, 12)).astype(np.uint8)
        tiles[n] = np.asarray(Image.fromarray(low).resize((95, 64), Image.BICUBIC)).clip(1, 255).astype(np.uint8)
    p = os.path.join(d, 'icons.npz')
    np.savez(p, **{'legend_' + k: v for k, v in tiles.items()}, legend_dummie=tiles['alpha'], r99=np.ones((5, 5), np.uint8))
    return p, tiles


def _ban_frame(a, tile, x0, y0, w, tag=False, sign=True):
    """Draw a banned portrait as the game does: orange-red frame (top edge at y0, x0..x0+w), the tile inside at the
    frame's scale, the round "no" sign under the middle, optionally a chance tag above (a roll candidate)."""
    import numpy as np
    from PIL import Image
    import pcs_bans as B
    OR = (232, 79, 60)
    k = B.TILE / (w / B.SRC_W); h = int(w * 0.95)
    up = np.asarray(Image.fromarray(tile).resize((int(tile.shape[1] / k), int(tile.shape[0] / k)), Image.BICUBIC))
    box = a[y0 + 6:y0 + h, x0 + 6:x0 + w - 6]
    ph, pw = min(box.shape[0], up.shape[0]), min(box.shape[1], up.shape[1])
    box[:ph, :pw] = up[:ph, :pw, None]
    a[y0:y0 + 4, x0:x0 + w] = OR; a[y0:y0 + h, x0:x0 + 4] = OR; a[y0:y0 + h, x0 + w - 4:x0 + w] = OR
    if tag: a[y0 - 28:y0 - 6, x0:x0 + w] = OR
    if sign:
        xm, yc, R = x0 + w // 2, y0 + int(w * 1.12), int(w * 0.2)
        yy, xx = np.ogrid[-R:R, -R:R]; rr = np.hypot(yy, xx)
        reg = a[yc - R:yc + R, xm - R:xm + R]
        reg[rr < R * 0.95] = OR; reg[rr < R * 0.7] = (5, 0, 0)
    return a


def test_legend_bans_reader():
    """Legend bans (pcs_bans): a banned portrait (orange-red frame + "no" sign) in the legend select grid and in the
    BANNED! box is found and named from the icons file's legend tiles; a roll candidate (chance tag above) and a frame
    without the sign are not bans; nothing on an empty frame; the ban phase's bottom bar is told. Per game: 2 reads
    make a ban, `bans` None = not read (no select screen), [] = select screen without bans."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import numpy as np
    import pcs_bans as B
    with tempfile.TemporaryDirectory() as d:
        path, tiles = _ban_tiles(d)
        R = B.Bans(path)
        assert sorted(n for n, *_ in R.T) == ['alpha', 'beta'], [n for n, *_ in R.T]
        empty = np.full((1440, 2560, 3), 40, np.uint8)
        assert B.frames(empty) == [] and R.read_if(empty, True) is None and not B.ban_phase(empty)
        a = np.full((1440, 2560, 3), 40, np.uint8)
        _ban_frame(a, tiles['alpha'], 478, 1116, 102)                      # legend select grid
        _ban_frame(a, tiles['beta'], 920, 1116, 102)
        _ban_frame(a, tiles['beta'], 1300, 1116, 102, tag=True)           # a roll candidate: not a ban
        _ban_frame(a, tiles['alpha'], 1600, 935, 102, sign=False)         # no sign: not a ban
        got = sorted((h, n) for h, n, sc, x, y in R.read(a))
        assert got == [('banned-portrait', 'alpha'), ('banned-portrait', 'beta')], got
        b = np.full((1440, 2560, 3), 40, np.uint8)
        _ban_frame(b, tiles['beta'], 1136, 706, 128)                       # the BANNED! box
        b[1340:1418] = (232, 79, 60)                                         # the ban phase's red bar
        assert B.ban_phase(b) and not B.ban_phase(a)
        r = R.read_if(b, False)
        assert r and [(h, n) for h, n, sc, x, y in r] == [('ban-screen', 'beta')], r
        assert R.read_if(b[:, :, :], False) and R.read_if(a, False) is None    # grid without bar: only with the select screen
        assert B.Bans(os.path.join(d, 'none.npz')).read(a) and all(n is None for _, n, *_ in B.Bans(os.path.join(d, 'none.npz')).read(a))
    reads = [(100.0, [('ban-screen', 'beta', 0.9, 1136)]), (101.0, [('ban-screen', 'beta', 0.9, 1136)]),
             (110.0, [('banned-portrait', 'alpha', 0.9, 478), ('banned-portrait', 'beta', 0.9, 920)]),
             (111.0, [('banned-portrait', 'alpha', 0.9, 478), ('banned-portrait', 'beta', 0.9, 920)]),
             (112.0, [('banned-portrait', 'gamma', 0.6, 1300)]),               # one read only: not a ban
             (900.0, [('banned-portrait', 'alpha', 0.9, 478)])]                 # after the game started: not its ban
    games = [{'tStart': 140.0, 'select': {'tFirst': 112.0, 'tLast': 140.0}},
             {'tStart': 800.0, 'select': {'tFirst': 770.0, 'tLast': 800.0}},
             {'tStart': 1500.0}]
    B.attach(games, reads)
    g0 = {e['legend']: (e['how'], e['reads']) for e in games[0]['bans']}
    assert g0 == {'beta': ('ban-screen', 4), 'alpha': ('banned-portrait', 2)}, games[0]['bans']
    assert games[0]['bansSeen'] is True
    assert games[1]['bans'] == [] and games[1]['bansSeen'] is False         # select screen, no bans shown
    assert games[2]['bans'] is None and games[2]['bansSeen'] is False       # no select screen: not read
    g = [{'tStart': 140.0, 'select': {'tFirst': 112.0, 'tLast': 140.0}}]
    B.attach(g, reads, stretched=True)
    assert g[0]['bans'] is None
    # two banned cells in one column of the grid (rows 1 and 2): two bans, not one
    g = [{'tStart': 140.0, 'select': {'tFirst': 112.0, 'tLast': 140.0}}]
    col = [(t, [('banned-portrait', 'alpha', 0.9, 700, 935), ('banned-portrait', 'beta', 0.9, 705, 1116)]) for t in (120.0, 121.0)]
    B.attach(g, col)
    assert sorted(e['legend'] for e in g[0]['bans']) == ['alpha', 'beta'], g[0]['bans']


def test_ammo_reader_and_events():
    """Ammo panel and heal slot (pcs_ammo): magazine, reserve (dim leading zeros), heal count and item; an empty heal
    slot only with the key hint and the ammo read; nothing on an empty frame. Per game: a fight that starts with less
    than a magazine in reserve is a low-ammo fight; the heal slot empty twice after having heals = out of heals."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import numpy as np
    import pcs_ammo as A
    assert set(A.T) == {'M', 'R', 'H'}, list(A.T)
    assert A.read(np.zeros((1440, 2560, 3), np.uint8), 1.0) is None
    r = A.read(_ammo_frame(24, 72, 12, 'shield'), 5.0)
    assert r == (5.0, 24, 72, 'shield', 12, 2), r
    r = A.read(_ammo_frame(8, 140, 3, 'shield', slot=1), 6.0)
    assert r == (6.0, 8, 140, 'shield', 3, 1), r
    assert A.read(_ammo_frame(30, 210, hint=True), 7.0)[3:5] == ('none', 0)             # heal slot empty
    assert A.read(_ammo_frame(hint=True), 7.0) is None                                  # no ammo read: not trusted
    assert A.read(_ammo_frame(30, 210), 7.0)[3:5] == (None, None)                        # no key hint: unknown
    # per game
    rows = [(10.0, 28, 200, 'shield', 4, 1), (20.0, 28, 180, 'shield', 4, 1), (21.0, 28, 180, 'shield', 4, 1),   # fight 1 at 25: fine
            (40.0, 28, 20, 'medkit', 1, 1),                                              # fight 2 at 45: 20 left < 28
            (50.0, 12, 20, 'none', 0, 1), (52.0, 12, 20, 'none', 0, 1),                  # out of heals at 50 ...
            (70.0, 28, 20, 'syringe', 2, 1),                                             # ... until 70
            (90.0, 28, 0, None, None, 2),                                                # spectating: left out
            (130.0, 6, 0, 'none', 0, 1), (131.0, 6, 0, 'none', 0, 1)]                    # next game: never had heals
    bars = [(90.0, {'state': 'spectating'}, [], True)]
    games = [{'tStart': 0.0, 'tEndGame': 100.0, 'fights': [{'t0': 25.0, 't1': 35.0}, {'t0': 45.0, 't1': 48.0}]},
             {'tStart': 120.0, 'fights': [{'t0': 132.0, 't1': 140.0}]}]
    A.attach(games, rows, bars)
    g = games[0]
    assert [e['kind'] for e in g['ammoEvents']] == ['lowAmmoFight', 'outOfHeals'], g['ammoEvents']
    assert g['ammoEvents'][0]['t'] == 45.0 and g['ammoEvents'][0]['reserve'] == 20 and g['ammoEvents'][0]['magFull'] == 28
    assert g['ammoEvents'][1] == {'t': 50.0, 'kind': 'outOfHeals', 'end': 70.0}, g['ammoEvents'][1]
    assert g['fights'][0]['ammoAtStart']['low'] is False and g['fights'][1]['ammoAtStart']['low'] is True
    assert g['ammoSummary'] == {'reads': 7, 'healReads': 7, 'fightsChecked': 2, 'lowAmmoFights': 1, 'outOfHeals': 1,
                                'outOfHealsSeconds': 20.0}, g['ammoSummary']
    assert [r[0] for r in g['ammoTimeline']['rows']] == [10.0, 20.0, 40.0, 50.0, 70.0], g['ammoTimeline']   # same reads kept once
    assert g['healTimeline']['rows'][0] == [10.0, 'shield', 4]
    h = games[1]
    assert h['ammoEvents'] == [{'t': 132.0, 'kind': 'lowAmmoFight', 'mag': 6, 'reserve': 0, 'magFull': 6, 'fightEnd': 140.0}], h['ammoEvents']
    assert h['ammoSummary']['outOfHeals'] == 0                                           # empty from the start: not "ran out"


def test_scanner_build_stamp():
    """Results say which build made them ('scannerBuild'): a test build between releases is VERSION+dev.N, a release
    build exactly VERSION; 'scanner' stays VERSION (the hub shows it)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import json as _json
    import pcs_scan as SCAN
    V = SCAN.VERSION
    with tempfile.TemporaryDirectory() as d:
        assert SCAN.build_stamp(d) == V + "+source"                            # from source: no build.json
        b = Path(d) / "build.json"
        b.write_text(_json.dumps({"version": "dev-52", "base": V}), encoding="utf-8-sig")   # BOM like PowerShell 5 writes
        assert SCAN.build_stamp(d) == V + "+dev.52"
        b.write_text(_json.dumps({"version": V, "base": V}))
        assert SCAN.build_stamp(d) == V


def test_fast_colour_masks_identical():
    """The faster readers (channel planes instead of .min(2) / .max(2), no whole-frame int16 copies, one integral
    image per feed line) give exactly what the old code gave."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import numpy as np
    import pcs_hud as HUD
    import pcs_scan as SCAN
    rng = np.random.default_rng(3)
    a = rng.integers(0, 256, (60, 90, 3), dtype=np.uint8)
    a[::7] = 230; a[:, ::5] = (240, 235, 250)                                  # some white strokes
    p = a.astype(np.int16)
    mn, mx = HUD.minmax(a)
    assert np.array_equal(mn, p.min(2)) and np.array_equal(mx, p.max(2)) and np.array_equal(mx - mn, np.ptp(p, axis=2))
    assert np.array_equal(HUD.minmax(p)[0], p.min(2))
    for lo in (170, 180):
        assert np.array_equal(HUD.white(a, lo), (p.min(2) > lo) & (np.ptp(p, axis=2) < 50))
    old_text = lambda q: ((q.min(2) > 222) & (np.ptp(q, axis=2) < 40)) | \
        ((q[..., 0] > 210) & (q[..., 1] > 90) & (q[..., 2] < 110) & (q[..., 0] - q[..., 2] > 120)) | \
        ((q.max(2) > 170) & ((q.max(2) - q.min(2)) > 0.45 * q.max(2)) & ~((q[..., 0] > 150) & (q[..., 1] < 100) & (q[..., 2] < 100)))
    assert np.array_equal(HUD.Feed._text(p), old_text(p))
    c = rng.random((40, 120)) * 255                                            # box sums: shared integral image = per-call one
    I = HUD.Weapons._integral(c)
    for h, w in ((5, 7), (12, 30)):
        assert np.array_equal(HUD.Weapons._box(None, h, w, I), HUD.Weapons._box(c, h, w))
    # whole frames: uint8 in = int16 in (the readers make int16 only the parts they compute on)
    img = rng.integers(0, 256, (1440, 2560, 3), dtype=np.uint8)
    img[1362:1430] = 10; img[1375:1420, 80:120] = 230                           # the full-map key bar and 'M' keycap
    img[88:1088:4, 1520:2520:4] = (200, 20, 20)                                 # drop screen gates
    img[1125:1180, 1740:1800] = (20, 200, 20); img[1125:1180, 2020:2075] = (200, 20, 20)
    for st in (False, True):
        assert SCAN.read_dropscreen(img, st) == SCAN.read_dropscreen(img.astype(np.int16), st)
        assert SCAN.MapReader.is_map(img) == SCAN.MapReader.is_map(img.astype(np.int16)) is True
        assert SCAN.MapReader().read(img, st) == SCAN.MapReader().read(img.astype(np.int16), st)
    assert HUD.read_ring(img) == HUD.read_ring(img.astype(np.int16)) and HUD.read_dbg(img) == HUD.read_dbg(img.astype(np.int16))


def test_debug_overlay_misreads():
    """The debug overlay's POS line: a clear line is read; an unreadable cell is never taken for a digit gap: a '?'
    inside a number (15?63 for 15863.63) or where a minus sign or first digit was (?1607 for 11607) gives no position
    from that frame instead of a wrong one. The decimal point read as '-' or '?' and the gap before a minus read as
    '-' are fine."""
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_hud as HUD
    def frame(text, y=22):
        img = np.zeros((1440, 2560, 3), np.uint8)
        for i, ch in enumerate(text):
            x = 3 + 8 * (6 + i)
            if ch == '.':
                img[40 + y + 7:40 + y + 9, x + 2:x + 4] = 255
            elif ch == '#':                                        # an unreadable blob
                img[40 + y:40 + y + 9, x:x + 7] = 255
            elif ch != ' ':
                g = np.array(HUD.DBG_T[ch], bool)
                img[40 + y:40 + y + 9, x:x + 7][g] = 255
        return img
    assert HUD.read_dbg(frame("29619.14 -26234.44 1149.94")) == ([29619.14, -26234.44, 1149.94], None)
    assert HUD.read_dbg(frame("29619#14 -26234.44 1149.94#"))[0] == [29619.14, -26234.44, 1149.94]   # a '?' point
    assert HUD.read_dbg(frame("15#63.82 -5643.20 5882.23"))[0] is None            # not 15.63
    assert HUD.read_dbg(frame("#1607.77 -30548.06 6348.02"))[0] is None           # not 1607.77
    assert HUD.read_dbg(frame("#24217.65 33525.88 867.56"))[0] is None            # a lost minus sign
    assert HUD.read_dbg(frame("100.00 200.00"))[0] is None                        # two numbers only
    for line, xyz in (("28619-14--26234-44 1148-94   ?", (28619.14, -26234.44, 1148.94)),
                      ("-28653?76--18149-44 707-08   -", (-28653.76, -18149.44, 707.08)),
                      ("15830-75 -26232-44 1270-57?-  ", (15830.75, -26232.44, 1270.57))):
        g = HUD.POSRX.match(line).groups()
        assert tuple(float(g[2 * i] + '.' + g[2 * i + 1]) for i in range(3)) == xyz
    for line in ("15?63-82--5643-20-5882-23  ---", "?1607-77--30548-06 6348-02   ?", "?28744-84--19830-08 545-97   -",
                 "-7?77-31-27333-04-8092-86-----"):
        assert HUD.POSRX.match(line) is None
    assert HUD.read_dbg(frame("299.00", y=50))[1] == 299.0                        # VEL line as before


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


def test_landing_place_broken_moon():
    """Broken Moon landings get map coordinates (the game's own minimap transform, checked on own-arrow / POS pairs) and a
    place name only near a POI centre (the map has centres, no outlines; the middle is named since PR #41). Maps without a transform still give None."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_hud as HUD
    import pcs_scan as SCAN
    x, y = HUD.to_map("BM", -13757.28, -22228.53)             # 4 Nov, landed in the domes of Cultivation
    assert 0 < x < 100 and 0 < y < 100 and abs(x - 36.32) < 0.05 and abs(y - 76.41) < 0.05, (x, y)
    assert SCAN.landing_poi("BM", x, y) == "bm-cultivation"
    assert SCAN.landing_poi("BM", *HUD.to_map("BM", -9555.07, 22030.99)) == "bm-the-core"    # 15 Nov, The Core
    assert SCAN.landing_poi("BM", *HUD.to_map("BM", -8772.64, -2841.75)) == "bm-quarantine-zone"  # the middle (PR #41)
    assert SCAN.landing_poi("BM", *HUD.to_map("BM", -5572.87, -10867.06)) == "bm-quarantine-zone"  # 11.5 % away, Hazmat 13.1
    assert SCAN.landing_poi("BM", 95, 95) is None                                            # > 12 % from every centre
    assert "BM" not in HUD.CAL_UNCHECKED and "KC" in HUD.CAL_UNCHECKED
    assert HUD.to_map("OL", 0, 0) is None and HUD.to_map("?", 0, 0) is None
    assert HUD.to_map("WE", 0, 0) == (49.46, 49.82)                                          # fitted maps unchanged
    for mx, my in ((10, 10), (50, 50), (90, 70)):                                           # outlined maps: as poi_at
        assert SCAN.landing_poi("WE", mx, my) == SCAN.poi_at("WE", mx, my)[0]


def test_landing_place_e_district():
    """E-District uses the game's own minimap transform (coach's decision, 16 Nov 2026): the map's centre is world 0, 0,
    own-arrow / POS pairs fit within half a percent, and the known landings keep their place."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_hud as HUD
    import pcs_scan as SCAN
    assert HUD.CAL["ED"] == HUD._game_tx(-43008, 43008, 21) and "ED" not in HUD.CAL_UNCHECKED
    assert HUD.to_map("ED", 0, 0) == (50.0, 50.0)
    ax, ay = HUD.to_map("ED", -11889, 8641)                   # 4 Nov: own arrow on the full map at 36.24, 40.35
    assert abs(ax - 36.24) < 0.5 and abs(ay - 40.35) < 0.5, (ax, ay)
    x, y = HUD.to_map("ED", 26519.45, 8113.98)                # 15 Nov, landed in The Heights (east edge: moved most)
    assert abs(x - 80.83) < 0.05 and abs(y - 40.57) < 0.05, (x, y)
    assert SCAN.landing_poi("ED", x, y) == "ed-heights"
    assert SCAN.landing_poi("ED", *HUD.to_map("ED", 4103.86, 6782.84)) == "ed-energy-bank"     # 4 Nov
    assert SCAN.landing_poi("ED", *HUD.to_map("ED", -23252.34, -8508.78)) == "ed-neon-square"  # 4 Nov
    assert SCAN.landing_poi("ED", *HUD.to_map("ED", -4046.32, -33641.3)) == "ed-shipyard-arcade"  # 3 Nov, south edge


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
    """A Sender stand-in for results an older version sent to Google Drive only: the first upload "goes through"
    without the hub (hub_ok False = "Not on hub yet"), the hub fails with `fail` (a HubError kind) until it is None.
    `blocked` = what Sender.hub_blocked() would say (refused key, 429 wait)."""

    def __init__(self):
        super().__init__()
        self.fail, self.blocked, self.hub_calls = "network", "", []

    def upload(self, files):
        from pcs_recorder.runtime import Sent
        out = Sent(["Google Drive"])
        out.hub_ok, out.hub_why = False, "Hub not reachable"
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
    assert retention.load_sent(r.idx)["k0"]["hub"] == "pending"                        # kept on disk
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
    apex["on"] = False; r.tick(); r.wait_uploads()
    assert len(hub.hub_calls) == 3 and r.hub_state("k0") == {"hub": "sent", "why": ""}
    assert r.idx["k0"].note == "Sent to hub: 2 games"
    assert any(t == "Sent to hub" and "2 games" in m for t, m, *_ in ui.notes), ui.notes
    clock["t"] += 3600; r.tick(); r.wait_uploads(); r._retry_hub()
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
        out = Sent(["the team hub"])
        out.hub_ok, out.hub_note = True, "Sent to hub: 2 games"
        return out
    hub.upload = both
    r._retry_uploads()
    assert r.sent["k0"] == {"hub": "sent", "why": ""}
    r._retry_hub()
    assert not hub.hub_calls
    r, hub, ui, clock = _hub_recorder()
    hub.upload = lambda files: Sent(["somewhere else"])                                # hub not tried
    r._retry_uploads()
    assert r.hub_state("k0")["hub"] == ""


def test_no_hub_results_stay_on_pc():
    """No hub address (Google Drive results removed): the results stay on this PC, the player is told once in
    Activity (no pop-up), the recording says why; joining the hub sends them. An old sent.json with a "drive" field
    still loads."""
    from pcs_recorder.core import NoHub
    from pcs_recorder.runtime import Sender, Sent
    r, hub, ui, clock = _hub_recorder(n=2)
    r.s.hub_url = ""
    sender = Sender(r.s)
    try:
        sender.upload([Path("x.scan.json")])
        raise AssertionError("no NoHub")
    except NoHub as e:
        assert "No team hub yet" in str(e) and "stay on this PC" in str(e)
    said = []
    ui.activity = said.append
    r.hub = sender
    for _ in range(3):
        r.next_upload = 0
        r._retry_uploads()
    assert all(e.status == "scanned" for e in r.idx.values())
    assert r.idx["k0"].note == "not sent yet: no team hub"
    assert len(said) == 1 and "No team hub yet" in said[0] and not ui.notes, (said, ui.notes)   # once, no pop-up
    r.s.hub_url = "http://hub.test:8420"                                              # joined
    hub.upload = lambda files: (lambda o: (setattr(o, "hub_ok", True), o)[1])(Sent(["the team hub"]))
    r.hub = hub
    r.next_upload = 0
    r._retry_uploads(); r._retry_uploads()
    assert all(e.status == "uploaded" for e in r.idx.values()), r.idx
    import json
    retention.SENT_FILE.write_text(json.dumps({"k0": {"hub": "pending", "why": "", "drive": "sent"}}))
    assert retention.load_sent(r.idx)["k0"]["hub"] == "pending"                       # older file still read


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


def test_youtube_video_map_to_hub():
    """After a block is on YouTube, the app tells the hub which recordings the video holds, at what offsets (Watch
    together). No hub: nothing. Same retry rules as the scan results; an older hub (404) is asked again later."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pcs_recorder import hub, youtube
    youtube.STATE_FILE.unlink(missing_ok=True)
    folder = TMP / f"ytmap-{time.time_ns()}"
    folder.mkdir()
    idx = {}
    for key, jump in (("a", 300.5), ("b", 120.0)):
        v = folder / f"Apex Legends_2026.11.13-17.2{key == 'b' and 9 or 4}.mp4"
        v.write_bytes(b"x")
        (v.with_suffix(".scan")).mkdir()
        (v.with_suffix(".scan") / (v.stem + ".scan.json")).write_text(json.dumps(
            {"games": [{"map": "SP", "tStart": 10.0, "landing": {"exitAtExact": jump}}, {"map": "SP", "tStart": 5.0}]}))
        idx[key] = retention.Entry(path=str(v), started=time.time() - 7200, status="scanned")
    seen, answer = [], {"code": 200}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(n))))
            data = b'{"ok": true}'
            self.send_response(answer["code"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    key = "pcs_" + "K" * 43                                                  # an obviously fake key

    class Rec:
        current = None
    rec = Rec()
    rec.idx = idx
    clock = [1000.0]
    s = settings.Settings(player="Moe", folder=str(folder), yt_enabled=True, hub_url="")
    up = youtube.Uploader(s, rec, FakeUI(), lambda: None, lambda: False, clock=lambda: clock[0])
    up.state = {"a": {"keys": ["a", "b"], "status": "done", "video": "AbCdEfGhIjK", "title": "Scrims", "at": 1.0,
                      "offsets": [0.0, 3600.25], "durations": [3600.25, 1800.0], "privacy_used": "unlisted",
                      "hub_video": "pending"}}
    try:
        hub.set_key(None)
        hub.reset()
        assert up.send_to_hub() == 0 and not seen                            # no hub address
        s.hub_url = f"http://127.0.0.1:{srv.server_address[1]}"
        assert up.send_to_hub() == 0 and not seen                            # no key
        hub.set_key(key)
        answer["code"] = 500                                                 # the hub fails: backoff
        assert up.send_to_hub() == 0 and len(seen) == 1 and up.hub_next == clock[0] + 60
        assert up.send_to_hub() == 0 and len(seen) == 1                      # not before the backoff
        clock[0] += 61
        answer["code"] = 404                                                 # a hub from before 0.5
        assert up.send_to_hub() == 0 and len(seen) == 2 and up.state["a"]["hub_video_next"] == clock[0] + 6 * 3600
        assert up.send_to_hub() == 0 and len(seen) == 2
        clock[0] += 6 * 3600 + 1
        answer["code"] = 200
        assert up.send_to_hub() == 1 and up.state["a"]["hub_video"] == "sent"
        path, auth, body = seen[-1]
        assert path == "/api/videos" and auth == "Bearer " + key and key not in json.dumps(body)
        assert body == {"video": "AbCdEfGhIjK", "privacy": "unlisted", "title": "Scrims", "duration": 5400.25,
                        "recordings": [{"file": "Apex Legends_2026.11.13-17.24", "offset": 0.0, "duration": 3600.25},
                                       {"file": "Apex Legends_2026.11.13-17.29", "offset": 3600.25, "duration": 1800.0}],
                        "games": [{"file": "Apex Legends_2026.11.13-17.24", "jumpAt": 300.5, "videoAt": 300.5},
                                  {"file": "Apex Legends_2026.11.13-17.29", "jumpAt": 120.0, "videoAt": 3720.25}]}, body
        assert json.loads(youtube.STATE_FILE.read_text())["a"]["hub_video"] == "sent"
        assert up.send_to_hub() == 0 and len(seen) == 3                      # never twice
        # the hub says no (another player's video): not asked again
        up.state["c"] = dict(up.state["a"], video="ZZZZZZZZZZZ", hub_video="pending", at=2.0)
        answer["code"] = 403
        assert up.send_to_hub() == 0 and up.state["c"]["hub_video"] == "refused" and len(seen) == 4
        assert up.send_to_hub() == 0 and len(seen) == 4
        # recordings gone: nothing to tell
        up.state["d"] = dict(up.state["a"], keys=["x"], video="YYYYYYYYYYY", hub_video="pending", at=3.0)
        assert up.send_to_hub() == 0 and up.state["d"]["hub_video"] == "gone" and len(seen) == 4
        # Apex running: the YouTube tick does nothing at all
        up.state["c"]["hub_video"] = "pending"
        up.is_apex_running = lambda: True
        up.tick()
        assert len(seen) == 4
    finally:
        srv.shutdown()
        srv.server_close()
        hub.set_key(None)
        hub.reset()


def test_audio_tracks_cut_after_recording():
    """The review's separate audio tracks are queued for cutting as soon as a recording ends (not when the review
    opens); at start, recent recordings without tracks are queued again and unfinished .part files removed."""
    class FakeCutter:
        def __init__(self):
            self.added, self.cleaned = [], []

        def add(self, path, asked=False):
            self.added.append((Path(path).name, asked))

        def clean(self, folders=()):
            self.cleaned.append(set(folders))
    r, ui, clock, read = _silence_recorder({"game": [], "discord": [], "mic": []})
    r.cutter = FakeCutter()
    apex = {"on": False}
    r.is_apex_running = lambda: apex["on"]
    r._final_file = lambda p, wait_s=0: p
    apex["on"] = True; r.tick()
    apex["on"] = False; r.tick()
    assert r.cutter.added == [("Apex Legends_1.mp4", False)], r.cutter.added       # background, right away
    # one track only: nothing to cut (the video plays its own sound)
    r2, *_ = _silence_recorder({"game": []}, tracks=("game",), consent=False)
    r2.cutter = FakeCutter()
    r2._queue_tracks(str(TMP / "x.mp4"))
    assert r2.cutter.added == []
    # start of the app: a stopped cut's .part file goes, recent recordings without tracks are queued, old ones not
    folder = Path(r.s.folder)
    old = make_file(folder, "Apex Legends_old.mp4", 1000)
    done = make_file(folder, "Apex Legends_done.mp4", 1000)
    done.with_suffix(".events.json").write_text("{}")
    time.sleep(0.05)
    (folder / "Apex Legends_done.track1.m4a").write_bytes(b"x")
    now = clock["t"]
    r.idx["old"] = retention.Entry(path=str(old), started=now - 3 * 86400, status="uploaded")
    r.idx["done"] = retention.Entry(path=str(done), started=now - 3600, status="uploaded")
    c = r.cutter = FakeCutter()
    r.resume_tracks()
    assert c.cleaned and str(folder) in c.cleaned[0]
    assert c.added == [("Apex Legends_1.mp4", False)], c.added
    from pcs_recorder import tracks
    part = folder / "Apex Legends_1.track2.part.m4a"
    part.write_bytes(b"half")
    assert tracks.clean_parts([folder]) == 1 and not part.exists()
    assert (folder / "Apex Legends_done.track1.m4a").exists()                    # finished tracks stay


# ------------------------------------------------------------------ crash reports to the team hub

def _crash_setup(hub_url="http://hub.test:8420", on=True, key=True):
    """The crash reporter attached to a scratch settings object, with a fake key and a send() that records."""
    from pcs_recorder import crash, hub
    s = settings.Settings(hub_url=hub_url, crash_reports=on, encoder="jim_nvenc")
    s.obs_password = "Obs-Pw-" + "q7" * 6
    log_f = TMP / f"app-{time.time_ns()}.log"
    log_f.write_text("".join(f"2026-11-17 20:00:00 INFO recorder: line {i} " + "x" * 600 + "\n" for i in range(500)),
                     encoding="utf-8")
    crash.R.__init__()
    crash.R.attach(s, "1.10.99", "1.10.98", str(log_f))
    crash.R.sync = True
    sent = []
    crash.R.send = lambda rep: (sent.append(rep), True)[1]
    hub.S.loaded, hub.S.key = True, ("pcs_" + "T3st" * 10 + "abc") if key else None
    return crash, s, sent


def _crash_teardown():
    from pcs_recorder import crash, hub
    crash.R.__init__()
    hub.S.loaded, hub.S.key = False, None


def test_crash_redaction():
    """Keys, tokens, passwords, e-mails, the Windows user / PC name and user folders never leave the PC."""
    crash, s, sent = _crash_setup()
    try:
        key = "pcs_" + "T3st" * 10 + "abc"
        old = os.environ.get("USERNAME"), os.environ.get("COMPUTERNAME")
        os.environ["USERNAME"], os.environ["COMPUTERNAME"] = "Jeanne Testeur", "JEANNE-GAMINGPC"
        try:
            sec = crash.own_secrets(s)
        finally:
            for k, v in zip(("USERNAME", "COMPUTERNAME"), old):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        dirty = [f"hub key {key}", f"obs pw {s.obs_password}", "Authorization: Bearer abcdefghijklmnop1234",
                 "C:\\Users\\Jeanne Testeur\\Videos\\PCS Recordings\\a.mp4", "c:/users/someone/AppData/Local/x",
                 "mail player.one@example.org now", "token=ya29.a0Abc_def-ghi", "refresh 1//0gAbCdEfGhIjKlMnOpQrStUv",
                 "obsws connect password='hunter22'", '{"hub_key": "zzz", "client_secret": "GOCSPX-abcdef"}',
                 "http://hub:8420/login?next=/#key=" + key, "machine JEANNE-GAMINGPC user jeanne testeur",
                 "D:\\OneDrive - Jeanne Testeur\\Videos"]
        out = "\n".join(crash.clean_lines(dirty, sec))
        for bad in (key, s.obs_password, "abcdefghijklmnop1234", "Jeanne", "jeanne", "someone", "player.one@example.org",
                    "ya29.a0Abc", "1//0gAbCd", "hunter22", "zzz", "GOCSPX-abcdef", "GAMINGPC"):
            assert bad not in out, (bad, out)
        assert "Users\\<user>\\Videos" in out and "<email>" in out, out
        plain = "2026-11-17 20:00:01 INFO recorder: encoder jim_av1_nvenc 12 Mb/s, capture window, hotkey ctrl+alt+m"
        assert crash.redact(plain) == plain
        t0 = time.time()
        crash.clean_lines(["y" * 100000] * 200, sec)                  # long lines are cut first: stays fast
        assert time.time() - t0 < 2
    finally:
        _crash_teardown()


def test_crash_report_contents_and_limits():
    """A report: kind, error, versions, Windows, GPU, context, the last 200 log lines (each cut), small in all."""
    import json
    crash, s, sent = _crash_setup()
    crash._sysinfo["gpu"] = "NVIDIA GeForce RTX 3060 Ti"
    try:
        assert crash.report("record_start", "OBS didn't answer on port 4466", "Traceback ...",
                            [("OBS log", ["12:00:00.000: line"] * 500)], {"minutes": 1.5})
        rep = sent[0]
        assert rep["kind"] == "record_start" and rep["error"] == "OBS didn't answer on port 4466"
        assert rep["app"] == "1.10.99" and rep["scanner"] == "1.10.98" and rep["gpu"].startswith("NVIDIA")
        assert rep["windows"] and len(rep["log"]) == crash.LOG_LINES and "line 499" in rep["log"][-1]
        assert all(len(x) <= crash.MAX_LINE for x in rep["log"])
        assert len(rep["attachments"][0]["lines"]) == crash.ATTACH_LINES
        assert rep["context"]["encoder"] == "jim_nvenc" and rep["context"]["minutes"] == 1.5
        assert "capture_apps" in rep["context"]
        assert len(json.dumps(rep).encode()) < 200 * 1024                  # the hub takes up to 256 KB
        assert crash.report("bogus", "x") and sent[-1]["kind"] == "other"
    finally:
        crash._sysinfo.clear()
        _crash_teardown()


def test_crash_reports_only_on_the_hub_and_when_on():
    for kw in ({"hub_url": ""}, {"on": False}, {"key": False}):
        crash, s, sent = _crash_setup(**kw)
        try:
            assert not crash.report("unhandled", "ValueError: x") and sent == [], kw
        finally:
            _crash_teardown()
    from pcs_recorder import crash
    assert not crash.report("unhandled", "not attached")                     # never set up: nothing, no error
    assert settings.Settings().crash_reports is True                         # on by default (used only on the hub)


def test_crash_throttle_and_retry():
    """The same error once per 30 min, 10 a day; a hub that can't be reached gets them later."""
    crash, s, sent = _crash_setup()
    try:
        assert crash.report("unhandled", "KeyError: 12")
        assert not crash.report("unhandled", "KeyError: 13")               # same error (numbers don't count)
        for i in range(12):
            crash.report("scan_failed", f"error {chr(65 + i)}")
        assert len(sent) == crash.PER_DAY, len(sent)
        crash.R.__init__(); crash.R.attach(s, "1", "", None); crash.R.sync = True
        crash.R.send = lambda rep: None                                    # hub not reachable
        assert not crash.report("record_stop", "OBS didn't stop") and len(crash.R.pending) == 1
        got = []
        crash.R.send = lambda rep: (got.append(rep), True)[1]
        crash.R.tick()
        assert len(got) == 1 and crash.R.pending == []
    finally:
        _crash_teardown()


def test_crash_reports_from_the_recorder():
    """Recording that doesn't start / runs without OBS recording / gives no file, and a failed scan, are reported."""
    import subprocess
    crash, s, sent = _crash_setup()
    try:
        retention.INDEX_FILE.unlink(missing_ok=True)
        s.folder = str(TMP / f"vids-{time.time_ns()}")
        s.folder_path.mkdir(parents=True, exist_ok=True)
        apex = {"on": False}
        clock = {"t": time.time()}
        obs = FakeOBS(s.folder_path)

        def bad_start():
            raise RuntimeError("OBS didn't answer on port 4466")
        obs.start = bad_start
        r = Recorder(s, obs, FakeHub(), FakeUI(False), fake_scan, is_apex_running=lambda: apex["on"],
                     clock=lambda: clock["t"])
        r._final_file = lambda p, wait_s=0: p
        apex["on"] = True; r.tick()
        assert sent[-1]["kind"] == "record_start" and "4466" in sent[-1]["error"], sent
        # starts, but OBS never says it records: reported once, 30 s later
        obs.start = lambda: None
        obs.elapsed = lambda: None
        crash.R.sent.clear()
        r.tick(); n = len(sent)
        clock["t"] += 31; r.tick(); r.tick()
        assert len(sent) == n + 1 and "isn't recording" in sent[-1]["error"], sent[-1]
        # Apex closes, OBS gives no file
        obs.stop = lambda: None
        apex["on"] = False; r.tick()
        assert sent[-1]["kind"] == "record_stop" and "OBS wasn't recording" in sent[-1]["error"], sent[-1]
        # a scan that fails
        e = retention.Entry(path=str(make_file(s.folder_path, "Apex Legends_x.mp4", UNIT)), started=clock["t"],
                            status="recorded")
        r.idx["rec-x"] = e
        (s.folder_path / "Apex Legends_x.scan").mkdir()
        (s.folder_path / "Apex Legends_x.scan" / "scan.log").write_text("Traceback\nValueError: bad frame\n")
        r.start_scan = lambda v, o: subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(2)"])
        r._begin_scan("rec-x")
        for _ in range(40):
            r.tick(); time.sleep(0.1)
            if r.scan_proc is None:
                break
        rep = sent[-1]
        assert rep["kind"] == "scan_failed" and "exit code 2" in rep["error"], rep
        assert rep["attachments"][0]["name"] == "scan.log" and "ValueError: bad frame" in rep["attachments"][0]["lines"]
        assert rep["context"]["exit_code"] == 2
    finally:
        _crash_teardown()


def test_failed_recording_never_deletes_other_files():
    """A recording without a file (path "") is older than the keep limit: retention removes the entry only, never the
    files of the folder the app runs in (Path("") is the working folder; its glob matched every file there)."""
    work = TMP / f"workdir-{time.time_ns()}"
    work.mkdir()
    keep = [make_file(work, n, 10) for n in ("PCS-Scanner.exe", "notes.txt", ".gitignore")]
    old_cwd = os.getcwd()
    os.chdir(work)
    try:
        idx = {"f": retention.Entry(path="", started=time.time() - 30 * 86400, status="failed"),
               "g": retention.Entry(path="relative.mp4", started=time.time() - 30 * 86400, status="failed")}
        assert retention.files_of(idx["f"]) == [] and retention.files_of(idx["g"]) == []
        removed = retention.enforce(idx, keep_days=7, max_gb=0.0)
        assert all(p.exists() for p in keep), [p.name for p in work.iterdir()]
        assert [n for n, _ in removed] == [""] and idx == {}                # entries gone, no file deleted
        idx = {"new": retention.Entry(path="", started=time.time() - 3600, status="failed")}
        retention.enforce(idx, keep_days=7, max_gb=0.0)
        assert "new" in idx                                                 # a fresh failure stays listed
    finally:
        os.chdir(old_cwd)


def test_crash_obs_crash_files():
    """A new OBS crash file is reported once (on first run: those of the last 7 days, at most 3)."""
    from pcs_recorder import obs as obsmod
    crash, s, sent = _crash_setup()
    try:
        crashes = obsmod.CONFIG / "crashes"
        crashes.mkdir(parents=True, exist_ok=True)
        old = crashes / "Crash 2026-10-01 10-00-00.txt"
        old.write_text("Unhandled exception: c0000005\nDate/Time: 2026-10-01\n")
        os.utime(old, (time.time() - 30 * 86400,) * 2)
        new = crashes / "Crash 2026-11-17 20-00-00.txt"
        new.write_text("Unhandled exception: c0000005\nFault address: 7FF8 (c:\\users\\someone\\obs.dll)\n")
        crash.STATE_FILE.unlink(missing_ok=True)
        crash.check_obs_crashes.last = 0
        assert crash.check_obs_crashes() == 1
        assert sent[-1]["kind"] == "obs_crash" and "c0000005" in sent[-1]["error"]
        assert "someone" not in str(sent[-1])
        crash.check_obs_crashes.last = 0
        assert crash.check_obs_crashes() == 0                              # not again
        newer = crashes / "Crash 2026-11-17 21-00-00.txt"
        newer.write_text("Unhandled exception: 80000003\n")
        os.utime(newer, (time.time() + 5,) * 2)
        crash.check_obs_crashes.last = 0
        assert crash.check_obs_crashes() == 1
    finally:
        _crash_teardown()


def _scanner():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_scan
    return pcs_scan


def test_scan_resumes_from_finished_chunks():
    """A scan stopped part-way (Apex started: the app stops it) goes on from the chunks it had finished, and the
    merged result is the same as one uninterrupted scan. Chunks are reused only for the same file and cuts; the
    pieces are the same as before (results depend on the cuts), whatever the number of worker processes."""
    import numpy as np
    SCAN = _scanner()
    video = TMP / f"resume-{time.time_ns()}.mp4"
    video.write_bytes(b"x" * 100)
    ran = []

    def seg(job, fail_at=None):
        path, start, end, snap, dec, layout = job
        k = int(round(start / 600))
        if k == fail_at:
            raise KeyboardInterrupt("stopped")                     # the process is ended here
        ran.append(k)
        lab = np.zeros((24, 230), bool); lab[2 * k:2 * k + 4, :20] = True
        return (start, [{"t": start + 1, "kind": "map", "data": {"map": "SP", "k": k}}],
                [[start + 1, 0, 100 - k, False, None, None, None, None, False, False]], [(lab, 1)],
                {"frames": 10, "decode": 0.5, "analysis": 1.0, "_feed": [{"t": start + 2}], "_rank": [(start + 1, False)],
                 "_squads": [], "_own": [], "_votes43": 0, "_votes169": 3})
    real = (SCAN.duration, SCAN.pick_decoder, SCAN._segment)
    try:
        SCAN.duration = lambda p: 3600.0
        SCAN.pick_decoder = lambda p: ("CPU", [])
        assert SCAN.default_chunks(3600) == max(1, min(8, (os.cpu_count() or 2) // 2)) and SCAN.default_chunks(200) == 1
        SCAN._segment = seg
        ev, sm, lb, perf = SCAN.scan_video(str(video), workers=1, chunks=6)            # reference: no stop
        whole = (ev, sm, lb.export(), perf["_feed"], perf["_rank"])
        folder = TMP / f"resume-{time.time_ns()}.partial"
        SCAN._segment = lambda j: seg(j, fail_at=3)
        ran.clear()
        try:
            SCAN.scan_video(str(video), workers=1, chunks=6, resume=str(folder))
            raise AssertionError("should have stopped")
        except KeyboardInterrupt:
            pass
        assert ran == [0, 1, 2] and sorted(p.name for p in folder.glob("chunk*.pkl")) == [
            "chunk00.pkl", "chunk01.pkl", "chunk02.pkl"]
        (folder / "chunk01.pkl").write_bytes(b"half")               # damaged: scanned again
        SCAN._segment = seg
        ran.clear()
        ev, sm, lb, perf = SCAN.scan_video(str(video), workers=1, chunks=6, resume=str(folder))
        assert sorted(ran) == [1, 3, 4, 5], ran
        assert (ev, sm, lb.export(), perf["_feed"], perf["_rank"]) == whole
        assert perf["chunks"] == 6 and perf["chunksResumed"] == 2 and perf["workers"] == 1
        ran.clear()                                                  # other cuts: earlier chunks are not used
        SCAN.scan_video(str(video), workers=1, chunks=4, resume=str(folder))
        assert len(ran) == 4, ran
        ran.clear()
        os.utime(video, (time.time() + 50, time.time() + 50))      # the file changed: not used either
        SCAN.scan_video(str(video), workers=1, chunks=4, resume=str(folder))
        assert len(ran) == 4, ran
        SCAN.Resume(str(folder), str(video), [], ("", []), None).clear()
        assert not folder.exists()
    finally:
        SCAN.duration, SCAN.pick_decoder, SCAN._segment = real


def test_scan_result_files_skip_kept_chunks():
    """The chunks an unfinished scan keeps are never sent with the results."""
    from pcs_recorder.core import result_files
    out = TMP / f"res-{time.time_ns()}.scan"
    (out / "x.partial").mkdir(parents=True)
    (out / "x.partial" / "chunk00.pkl").write_bytes(b"1")
    (out / "x.scan.json").write_text("{}")
    (out / "scan.log").write_text("")
    assert [p.name for p in result_files(out)] == ["scan.log", "x.scan.json"]
    assert result_files(out / "missing") == []


def test_stop_tree_ends_scan_and_workers():
    """Stopping a scan for Apex ends it and every process it started (frees their memory), not a pause."""
    import psutil
    from pcs_recorder.core import stop_tree
    out = TMP / f"stop-{time.time_ns()}"
    out.mkdir()
    p = fake_scan("v.mp4", str(out))
    root = psutil.Process(p.pid)
    for _ in range(50):
        if root.children(recursive=True):
            break
        time.sleep(0.1)
    tree = [root] + root.children(recursive=True)
    assert len(tree) >= 2
    stop_tree(p)
    assert p.poll() is not None
    assert not any(q.is_running() and q.status() != psutil.STATUS_ZOMBIE for q in tree)
    assert not (out / "session.scan.json").exists()
    stop_tree(p)                                                       # already ended: nothing happens


def _test_video(path, secs=4):
    import shutil
    import subprocess
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return False
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=s=320x180:r=30:d={secs}",
                    "-c:v", "mpeg4", "-g", "30", str(path)], check=True)
    return True


def test_keyframes_same_frames_unbuffered():
    """keyframes() reads each frame straight from ffmpeg into its own buffer (the old 100 MB Python buffer cost about
    100 ms and most of the scan's kernel time per keyframe): same bytes and times as ffmpeg gives, read-only frames."""
    import hashlib
    import subprocess
    SCAN = _scanner()
    v = TMP / f"kf-{time.time_ns()}.mp4"
    if not _test_video(v):
        print("  (no ffmpeg: keyframes test skipped)")
        return
    real = SCAN.DECODER
    try:
        SCAN.DECODER = ("CPU", [])
        got = [(t, img.shape, img.flags.writeable, hashlib.sha1(img.tobytes()).hexdigest())
               for t, img in SCAN.keyframes(str(v), 0.0, None)]
        raw = subprocess.run(["ffmpeg", "-v", "error", "-skip_frame", "nokey", "-i", str(v), "-fps_mode", "passthrough",
                              "-vf", "scale=2560:1440", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout
        n = 2560 * 1440 * 3
        ref = [hashlib.sha1(raw[k * n:(k + 1) * n]).hexdigest() for k in range(len(raw) // n)]
        assert [g[0] for g in got] == [0.0, 1.0, 2.0, 3.0], got
        assert [g[3] for g in got] == ref and all(g[1] == (1440, 2560, 3) and g[2] is False for g in got)
        mid = [t for t, _ in SCAN.keyframes(str(v), 1.5, 3.5)]          # a piece in the middle
        assert mid == [2.0, 3.0], mid
    finally:
        SCAN.DECODER = real


def test_stretched_views_fill_reader_boxes():
    """4:3 stretched recordings: the views fill only the boxes the readers look at, with the same pixels as the full
    remapped frames they replace (which were built 3 to 4 times per keyframe)."""
    import numpy as np
    SCAN = _scanner()
    img = np.random.default_rng(1).integers(0, 256, (1440, 2560, 3), dtype=np.uint8)
    old_top = img[SCAN.ROWS_TOP]
    old_map = img.copy(); old_map[:1340] = img[SCAN.ROWS_MAP]
    old_drop = img.copy(); old_drop[88:1088, 1520:2520] = img[SCAN.ROWS_DROP, 1520:2520]
    old_drop[1125:1180, 1740:2075] = img[1140:1195, 1983:2318]
    top, mp, drop = SCAN.view_top(img), SCAN.view_map(img), SCAN.view_drop(img)
    import pcs_hud as HUD
    boxes = {"top": [(417, 563 + 1, 0, 400), (40, 110, 0, 300), (120, 190, 2480, 2545), (HUD.Feed.Y0, HUD.Feed.Y1, HUD.Feed.X0, HUD.Feed.X1),
                     (0, SCAN.TOP_ROWS, 0, 2560)],
             "map": [(0, 1340, 560, 1960), (1362, 1430, 0, 2560)],
             "drop": [(88, 1088, 1520, 2520), (1125, 1180, 1740, 2075)]}
    assert 441 + HUD.SPEC_OFF + 1 <= 563 + 1 and 478 + HUD.SPEC_OFF <= SCAN.TOP_ROWS and HUD.Feed.Y1 <= SCAN.TOP_ROWS
    for name, new, old in (("top", top, old_top), ("map", mp, old_map), ("drop", drop, old_drop)):
        for y0, y1, x0, x1 in boxes[name]:
            assert np.array_equal(new[y0:y1, x0:x1], old[y0:y1, x0:x1]), (name, y0, y1, x0, x1)
    img.flags.writeable = False                                            # frames are read-only: views still work
    assert np.array_equal(SCAN.view_top(img)[:SCAN.TOP_ROWS], old_top[:SCAN.TOP_ROWS])


def _stretch43():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import stretch43
    return stretch43.stretch


def test_stretched_top_readers():
    """4:3 stretched recordings: the playlist emblem, the legend select screen, the loading banner, the squads counter
    and the own stats strip are read on the scanner's remapped top (view_top). Test frames: 16:9 HUD pictures turned
    into what a 4:3 stretched player records (tools/stretch43.py: rows squashed to 75 % towards the top, drawn at the
    1440 x 1080 render size and scaled back up). The 16:9 readers keep their old values."""
    import numpy as np
    from PIL import Image
    SCAN = _scanner()
    import pcs_hud as HUD
    stretch = _stretch43()
    top = lambda img: SCAN.view_top(stretch(img))
    assert (HUD.SquadsLeft.WHITE, HUD.SquadsLeft.BITS, HUD.OwnStats.INK, HUD.OwnStats.INK_K, HUD.Banner.WHITE) == (232, 26, 35, 0.5, 215)

    # playlist emblem (in game and on the waiting screen)
    E = HUD.ModeEmblem
    rng = np.random.default_rng(5)
    for key, mode, rgb, mask in [e for e in E.T if e[1]]:
        for y, x in ((8, 2422), (142, 2420)):
            img = np.full((1440, 2560, 3), 90, np.uint8)
            img[:300, 2160:] = rng.integers(60, 200, (300, 400, 3), dtype=np.uint8)
            h, w = E.SIZE
            big = np.asarray(Image.fromarray(rgb.astype(np.uint8)).resize((w, h), Image.NEAREST))
            m = np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).resize((w, h), Image.NEAREST)) > 0
            img[y:y + h, x:x + w][m] = big[m]
            r = E.read(top(img))
            assert r and r[:2] == (key, mode), (mode, y, r)

    # squads counter: test digits drawn from the table's shapes (one pixel bolder, as the game's font) are not the game's
    # own glyphs, so some come out doubtful once softened; a stretched read is right or nothing, never another number
    def cells(digit):
        bits = next(t for d, t in HUD.SquadsLeft.T if d == digit)
        g = np.array([(bits >> (191 - i)) & 1 for i in range(192)], np.uint8).reshape(16, 12)
        g = np.asarray(Image.fromarray(g * 255).resize((14, 22), Image.NEAREST)) > 127
        g[:, :-1] |= g[:, 1:]
        return g.astype(np.float32)[..., None]

    def hud(number, plate=40):
        img = np.full((1440, 2560, 3), plate, np.uint8)
        for x in range(2140, 2286, 16): img[77:99, x:x + 4] = 255
        img[77:99, 2288:2292] = 255
        x = 2130
        for ch in reversed(str(number)):
            box = img[77:99, x - 14:x].astype(np.float32); img[77:99, x - 14:x] = box * (1 - cells(ch)) + 255 * cells(ch); x -= 18
        return img
    got = {n: HUD.SquadsLeft43.read(top(hud(n))) for n in (20, 17, 13, 9, 4, 1, 58, 36)}
    assert all(v in (n, None) for n, v in got.items()) and {17, 13, 1} <= {n for n, v in got.items() if v == n}, got
    assert HUD.SquadsLeft43.read(top(np.full((1440, 2560, 3), 40, np.uint8))) is None

    # own stats strip
    put = lambda *a, **k: _own_put(HUD, *a, **k)

    def strip(kills=None, assists=None, knocks=None, damage=None, font="B", plate=40):
        img = np.full((1440, 2560, 3), plate, np.uint8)
        d0, _ = put(img, "S", str(damage), x_end=2336)
        img[132:156, d0 - 40:d0 - 3] = 255
        left = d0 - 70
        if knocks is not None:
            img[126:162, left - 105:left] = 250
            img[131:157, left - 80:left - 44] = 20
            put(img, "K", str(knocks), x_start=left - 41)
            left -= 125
        if kills is not None:
            a0 = left - 20 - 15 * len(str(assists))
            put(img, font, str(assists), x_start=a0)
            img[132:157, a0 - 41:a0 - 3] = 255
            k0, _ = put(img, font, str(kills), x_end=a0 - 41 - 24)
            img[132:157, k0 - 34:k0 - 9] = 255
        return img
    # the strip's places come from its icons (the damage icon and the knocks icon fall apart once softened: joined
    # again); the test digits are the table's averaged shapes, softer than the game's, so some stay doubtful: every
    # number read must be right, and the plates must be found
    O = HUD.OwnStats43
    seen = set()
    for kw in ({"damage": 54}, {"damage": 174}, {"knocks": 2, "damage": 160}, {"kills": 3, "assists": 1, "knocks": 2, "damage": 716},
               {"kills": 12, "assists": 10, "knocks": 6, "damage": 2051}, {"kills": 2, "assists": 0, "damage": 589, "font": "S"}):
        want = {k: v for k, v in kw.items() if k != "font"}
        assert HUD.OwnStats.read(strip(**kw)) == want                 # 16:9 reader on the 16:9 picture
        got = O.read(top(strip(**kw)))
        assert set(got.pop("_seen")) == set(want), (kw, got)            # every plate found, read or not
        assert all(want[k] == v for k, v in got.items()), (kw, got)
        seen |= set(got)
    assert seen == {"kills", "assists", "knocks", "damage"}, seen
    assert O.read(top(np.full((1440, 2560, 3), 40, np.uint8))) == {}
    # a game's total: refused when the number was on the strip but not read twice after its last read (it may have
    # grown); read to the end: as before
    rd = [(t, {"damage": 300, "_seen": ("damage",)}) for t in range(10, 20)]
    games = [{"tStart": 0.0, "drop": None, "map": "?", "select": {"tFirst": 0.0}}]
    SCAN.timeline(games, [], own=rd)
    assert games[0]["own"]["damage"] == 300, games[0]
    games = [{"tStart": 0.0, "drop": None, "map": "?", "select": {"tFirst": 0.0}}]
    SCAN.timeline(games, [], own=rd + [(20, {"_seen": ("damage",)}), (21, {"_seen": ("damage",)})])
    assert "own" not in games[0], games[0]
    games = [{"tStart": 0.0, "drop": None, "map": "?", "select": {"tFirst": 0.0}}]
    SCAN.timeline(games, [], own=rd + [(20, {"_seen": ("damage",)})])          # once: a doubtful digit, as in 16:9
    assert games[0]["own"]["damage"] == 300, games[0]

    # legend select screen in a whole scan: read on the remapped top once the layout is known (forced here), and
    # also while nothing is known yet (a chunk that starts in the lobby); a 16:9 scan of the same screens is unchanged
    tiles = {n: np.clip(rng.integers(1, 255, (16, 25)).repeat(4, 0).repeat(4, 1), 1, 255).astype(np.uint8) for n in ("alpha", "bravo", "charlie")}
    f = TMP / "legends-43.npz"
    np.savez_compressed(f, **{"legend_" + n: t for n, t in tiles.items()})
    COL = [(240, 160, 40), (40, 180, 190), (140, 200, 50)]

    def screen():
        img = np.full((1440, 2560, 3), 60, np.uint8)
        for k, n in enumerate(tiles):
            y0 = int(round(172 + 106.5 * k))
            img[y0 + 9:y0 + 73, 215:315] = tiles[n][..., None]
            img[y0 + 60:y0 + 80, 300:328] = COL[k]
            img[y0 + 64:y0 + 76, 312:316] = 255
            img[y0 + 30:y0 + 46, 196:204] = COL[k]
            for x in range(330, 520, 6): img[y0 + 10:y0 + 30, x:x + 3] = COL[k] if k == 1 else (255, 255, 255)
        for y in range(170, 254, 8): img[y:y + 4, 72:156] = 230             # rank badges (ranked)
        return img
    old = os.environ.get("PCS_WEAPON_ICONS"); os.environ["PCS_WEAPON_ICONS"] = str(f); layout = SCAN.LAYOUT
    try:
        flat = screen()
        for lay, img in ((True, stretch(flat)), (None, stretch(flat)), (None, flat), (False, flat)):
            SCAN.LAYOUT = lay
            events, _, _ = SCAN.scan_frames([(100.0 + k, img) for k in range(10)], quiet=True)
            sel = [e for e in events if e["kind"] == "select"]
            assert len(sel) == 10, (lay, [e["kind"] for e in events])
            assert [s["legend"] for s in sel[0]["data"]["squad"]] == ["alpha", "bravo", "charlie"], (lay, sel[0])
            assert [s["self"] for s in sel[0]["data"]["squad"]] == [False, True, False] and sel[0]["data"]["ranked"], (lay, sel[0])
        SCAN.LAYOUT = False                                     # a 16:9 scan never tries the stretched view
        assert not [e for e in SCAN.scan_frames([(100.0, stretch(flat))], quiet=True)[0] if e["kind"] == "select"]
    finally:
        SCAN.LAYOUT = layout
        os.environ.pop("PCS_WEAPON_ICONS", None)
        if old is not None: os.environ["PCS_WEAPON_ICONS"] = old


def test_clean_old_unpacked_copies():
    """At start the app removes the copies of itself left unpacked in %TEMP% (crash, shutdown), and nothing else:
    not the one it runs from, not one being unpacked, not one in use, not another program's folder."""
    import shutil
    import subprocess
    import pcs_core as core
    tmp = TMP / f"temp-{time.time_ns()}"
    tmp.mkdir()
    src = Path(sys.base_prefix) / "python3.dll"
    if not src.exists():
        src = Path(sys.executable).parent / "python3.dll"

    def bundle(name, markers=True):
        d = tmp / name
        (d / "web").mkdir(parents=True)
        shutil.copy(src, d / "python312.dll")
        (d / "lib.pyd").write_bytes(b"x" * 1000)
        if markers:
            for m in ("pcs_maps.json", "pcs_fonts.json", "web/index.html"):
                (d / m).write_text("{}")
            (d / "build.json").write_text('{"version": "1.10.11"}', encoding="utf-8-sig")
        return d
    old = bundle("_MEI1001")
    current = bundle("_MEI1002")
    other = bundle("_MEI1003", markers=False)                             # another PyInstaller program's folder
    busy = bundle("_MEI1004")
    left = tmp / "_MEI1005.pcs-old"                                       # renamed by an earlier start
    left.mkdir(); (left / "a.txt").write_text("x")
    (tmp / "_MEI1006").write_text("a file, not a folder")
    plain = tmp / "_MEIabc"; plain.mkdir()
    user = tmp / "my folder"; user.mkdir()
    holder = subprocess.Popen([sys.executable, "-c", f"import ctypes,time; ctypes.WinDLL(r'{busy / 'python312.dll'}'); "
                               "print('ok', flush=True); time.sleep(30)"], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ok"
        gone = core.clean_old_unpacked(str(tmp), str(current))               # just unpacked: too new to judge,
        assert [Path(p).name for p, _ in gone] == ["_MEI1005.pcs-old"], gone  # except what an earlier start renamed
        gone = core.clean_old_unpacked(str(tmp), str(current), now=time.time() + 3600)
        assert [Path(p).name for p, _ in gone] == ["_MEI1001.pcs-old"], gone
        assert not old.exists() and not left.exists() and current.exists() and other.exists()
        assert busy.exists() and (busy / "python312.dll").exists() and (busy / "lib.pyd").exists()   # in use: untouched
        assert (tmp / "_MEI1006").is_file() and plain.exists() and user.exists()
    finally:
        holder.kill(); holder.wait()
    gone = core.clean_old_unpacked(str(tmp), str(current), now=time.time() + 3600)   # no longer in use: removed
    assert [Path(p).name for p, _ in gone] == ["_MEI1004.pcs-old"] and gone[0][1] > 1000, gone


# ------------------------------------------------------------------ short sessions and Apex crashes (stitch.py)
class ActivityUI(FakeUI):
    def __init__(self, answer=False):
        super().__init__(answer)
        self.lines = []

    def activity(self, text):
        self.lines.append(text)


class VideoOBS(FakeOBS):
    """Gives a real short video (when `real`) or a placeholder file per session."""
    def __init__(self, folder, real=False):
        super().__init__(folder)
        self.real, self.secs = real, [3, 4, 2]

    def stop(self):
        self.rec = False
        self.n += 1
        p = Path(self.folder) / f"Apex Legends_2026.11.18-16.{self.n:02d}.mp4"
        if not (self.real and _test_video(p, self.secs[(self.n - 1) % len(self.secs)])):
            p.write_bytes(b"x" * 1000)
        return str(p)


def _crash_recorder(real=False, minutes=5):
    """A recorder with a fake clock, fake Apex and fake crash signals: `sig` = [(time, reason)] seen by the
    crash probe, `durs` / `ends` = fake length and in-game end per file name (None = can't be read)."""
    from pcs_recorder import stitch
    retention.INDEX_FILE.unlink(missing_ok=True)
    stitch.STATE_FILE.unlink(missing_ok=True)
    s = settings.load()
    s.folder = str(TMP / f"crash-{time.time_ns()}")
    s.enabled, s.min_record_min, s.comms_consent = True, minutes, False
    clock, apex = {"t": 1_800_000_000.0}, {"on": False}
    ui, obs = ActivityUI(False), VideoOBS(s.folder_path, real)
    r = Recorder(s, obs, FakeHub(), ui, fake_scan, is_apex_running=lambda: apex["on"], clock=lambda: clock["t"])
    r._final_file = lambda p, wait_s=0: p
    r.cutter = type("C", (), {"add": lambda self, p, asked=False: None, "clean": lambda self, f=(): None})()
    sig, durs, ends, recycled = [], {}, {}, []
    r.crash_probe = lambda since, until: next((why for t, why in sig if since <= t <= until), "")
    if not real:
        r.duration_probe = lambda p: durs.get(Path(p).name)
    r.end_probe = lambda p, d: ends.get(Path(p).name, False)

    def recycle(path):
        recycled.append(Path(path).name)
        os.remove(path)
    r.recycle = recycle

    def session(mins, crash=None, gap=0.0):
        """Apex runs `mins` minutes, then closes; `crash` = a crash sign at the end; then `gap` minutes pass."""
        apex["on"] = True; r.tick()
        clock["t"] += mins * 60
        if crash:
            sig.append((clock["t"] - 5, crash))
        apex["on"] = False; r.tick()
        clock["t"] += gap * 60
        r.tick()
        return obs.n
    return r, ui, clock, apex, sig, durs, ends, recycled, session


def test_short_recording_not_kept():
    """A session under min_record_min: no scan prompt, nothing sent, and once no restart came in 10 min its files
    go to the Recycle Bin with one Activity line (no pop-up). Longer sessions, min 0 and unreadable files: kept."""
    r, ui, clock, apex, sig, durs, ends, recycled, session = _crash_recorder()
    durs["Apex Legends_2026.11.18-16.01.mp4"] = 170
    session(3)
    (k1,) = r.idx
    assert ui.asked == 0 and r.queue == [] and r.scan_proc is None and r.idx[k1].status == "recorded"
    assert k1 in r.waiting_keys() and not r.rescan(k1)              # waits: not scanned, not cleaned up
    clock["t"] += 9 * 60; r.tick()
    assert k1 in r.idx and recycled == []                           # still within the 10 min
    clock["t"] += 2 * 60; r.tick(); r.tick()
    assert k1 not in r.idx and set(recycled) == {"Apex Legends_2026.11.18-16.01.mp4",
                                                 "Apex Legends_2026.11.18-16.01.events.json"}, recycled
    assert ui.lines == ["Too short: not kept (Apex Legends_2026.11.18-16.01.mp4, 3 min)"], ui.lines
    assert ui.notes == [] and ui.asked == 0 and r.hub.uploads == []
    # a long one: the scan prompt as before
    durs["Apex Legends_2026.11.18-16.02.mp4"] = 20 * 60
    session(20)
    assert ui.asked == 1 and len(r.idx) == 1
    # unreadable file (length unknown): kept
    session(2)
    assert ui.asked == 2 and len(r.idx) == 2
    # 0 = keep every session
    r.s.min_record_min = 0
    durs["Apex Legends_2026.11.18-16.04.mp4"] = 60
    session(1)
    assert ui.asked == 3 and len(r.idx) == 3
    # short, then Apex started again without any crash sign: the short one goes at once, the new one is recorded
    r.s.min_record_min = 5
    durs["Apex Legends_2026.11.18-16.05.mp4"] = 120
    session(2, gap=1)
    apex["on"] = True; r.tick()
    assert "Apex Legends_2026.11.18-16.05.mp4" in recycled and r.current is not None
    assert ui.lines[-1] == "Too short: not kept (Apex Legends_2026.11.18-16.05.mp4, 2 min)"
    assert sum(x.startswith("Too short") for x in ui.lines) == 2
    assert r.s.min_record_min == settings.Settings().min_record_min == 5
    assert settings.Settings(min_record_min=99).clamp().min_record_min == 30


def test_in_progress_imported_and_older_never_touched():
    """The recording in progress, files added by hand and recordings from before this change are never dropped."""
    r, ui, clock, apex, sig, durs, ends, recycled, session = _crash_recorder()
    folder = r.s.folder_path
    by_hand = folder / "my clip.mp4"; by_hand.write_bytes(b"x")
    durs["my clip.mp4"] = 30
    old = folder / "Apex Legends_2026.11.01-10.00.mp4"; old.write_bytes(b"x")
    durs[old.name] = 60
    r.idx["rec-1"] = retention.Entry(path=str(old), started=clock["t"] - 3600, status="recorded")
    retention.save_index(r.idx)
    apex["on"] = True; r.tick()
    cur = r.current
    for _ in range(4):
        clock["t"] += 5 * 60; r.tick()                     # 20 min of play
    assert r.idx[cur].status == "recording" and recycled == []
    durs["Apex Legends_2026.11.18-16.01.mp4"] = 120
    apex["on"] = False; r.tick()                           # Apex closes after the in-progress one: kept (long run)
    clock["t"] += 11 * 60; r.tick()
    assert by_hand.exists() and old.exists() and "rec-1" in r.idx and recycled == []


def test_short_kept_and_joined_after_crash():
    """Apex crashed (crash sign at the end) and was started again within 10 min: the short first piece is kept, the
    next recording continues it, and once that session ends both are joined (stream copy) into one recording with
    the first one's name, checked, scanned; the originals go to the Recycle Bin only after the check."""
    from pcs_recorder import stitch
    import json
    r, ui, clock, apex, sig, durs, ends, recycled, session = _crash_recorder(real=True, minutes=1)
    if not _test_video(TMP / "probe.mp4", 1):
        print("  (no ffmpeg: join test skipped)")
        return
    first = r.s.folder_path / "Apex Legends_2026.11.18-16.01.mp4"
    r.events = []
    session(2, crash="EA Javelin Anticheat: The game r5apex_dx12.exe exited unexpectedly", gap=3)
    (k1,) = r.idx
    assert ui.asked == 0 and k1 in r.js["holds"] and r.idx[k1].note == "Waiting: Apex may restart"
    Path(first).with_suffix(".events.json").write_text(json.dumps({"tracks": ["game"], "events": [
        {"t": 1.5, "kind": "mark"}]}), encoding="utf-8")
    apex["on"] = True; r.tick()                             # restarted 3 min later: the next one continues it
    assert ui.lines == ["Apex restarted after a crash: this recording will be joined with the one before."], ui.lines
    k2 = r.current
    r.events = [{"t": 0.5, "kind": "mark"}]
    clock["t"] += 20 * 60
    apex["on"] = False; r.tick()
    assert ui.asked == 0 and r.js["joins"] == [[k1, k2]], r.js
    for _ in range(200):                                    # ffmpeg runs in the background
        if not r.js["joins"]:
            break
        time.sleep(0.1); r.tick()
    assert not r.js["joins"] and list(r.idx) == [k1], (r.js, r.idx)
    e = r.idx[k1]
    assert Path(e.path) == first and first.exists(), e.path   # the first recording's name, now the whole session
    assert abs(stitch.media_duration(e.path) - 7) < 0.5
    assert {"Apex Legends_2026.11.18-16.01.mp4", "Apex Legends_2026.11.18-16.02.mp4"} <= set(recycled), recycled
    assert not (r.s.folder_path / stitch.JOIN_DIR).exists()
    ev = json.loads(first.with_suffix(".events.json").read_text(encoding="utf-8"))
    ts = [x["t"] for x in ev["events"]]                     # second part's mark moved by the first part's length
    assert len(ts) == 2 and ts[0] == 1.5 and abs(ts[1] - 3.5) < 0.1, ev
    assert ev["joined"][0]["offset"] == 0 and abs(ev["joined"][1]["offset"] - 3.0) < 0.1, ev
    assert ui.asked == 1 and e.note == "Joined after Apex crash (2 parts)"
    assert ui.lines[-1].startswith("Joined after Apex crash: Apex Legends_2026.11.18-16.01.mp4 (2 recordings")
    assert ui.notes == []


def test_join_waits_for_apex_and_failed_join_keeps_both():
    """A join is stopped when Apex starts (its unfinished file removed, started again later); a joined file that
    fails the check (length, playable) is removed and both recordings are kept as they are, even a short one."""
    from pcs_recorder import stitch
    r, ui, clock, apex, sig, durs, ends, recycled, session = _crash_recorder(real=True, minutes=1)
    if not _test_video(TMP / "probe.mp4", 1):
        print("  (no ffmpeg: join test skipped)")
        return
    session(2, crash="Apex crash window (crashmsg.exe)", gap=1)
    apex["on"] = True; r.tick()
    clock["t"] += 10 * 60
    apex["on"] = False; r.tick()                              # the session ends: the join starts in the same tick
    job = r.join_job
    assert job is not None
    r._join_tick(True)                                        # Apex starts: stopped, nothing left behind
    assert r.join_job is None and not job["out"].exists() and len(r.js["joins"]) == 1
    real_check = stitch.check_joined
    stitch.check_joined = lambda out, expected: (False, "the joined file can't be played")
    try:
        for _ in range(200):
            if not r.js["joins"]:
                break
            time.sleep(0.1); r.tick()
    finally:
        stitch.check_joined = real_check
    assert len(r.idx) == 2 and recycled == [] and all(Path(e.path).exists() for e in r.idx.values())
    assert ui.lines[-1] == ("Not joined after Apex crash (the joined file can't be played): the recordings are kept "
                            "as they are."), ui.lines
    assert ui.asked == 2 and not (r.s.folder_path / stitch.JOIN_DIR).exists()


def test_unsure_crash_keeps_recordings():
    """When it can't be told: a crash sign but no restart = kept; the end of a short recording can't be read and
    Apex restarts = both kept as they are; ended in game, no restart, short = not kept."""
    r, ui, clock, apex, sig, durs, ends, recycled, session = _crash_recorder()
    durs["Apex Legends_2026.11.18-16.01.mp4"] = 150
    session(3, crash="Application Error: Faulting application name: r5apex_dx12.exe", gap=11)
    assert len(r.idx) == 1 and recycled == [] and ui.asked == 1          # crashed, never restarted: kept, scanned
    durs["Apex Legends_2026.11.18-16.02.mp4"] = 150
    ends["Apex Legends_2026.11.18-16.02.mp4"] = None                     # its end can't be read
    session(3, gap=2)
    k2 = [k for k in r.idx if r.idx[k].path.endswith("16.02.mp4")][0]
    assert k2 in r.js["holds"]
    apex["on"] = True; r.tick()                                          # restart, no crash sign: unsure
    assert k2 in r.idx and r.idx[k2].status == "waiting" and k2 in r.queue and recycled == []
    k3 = r.current
    durs["Apex Legends_2026.11.18-16.03.mp4"] = 100
    clock["t"] += 2 * 60
    apex["on"] = False; r.tick()
    assert k3 in r.idx and k3 not in r.js["holds"]                       # the second one is kept too
    durs["Apex Legends_2026.11.18-16.04.mp4"] = 150
    ends["Apex Legends_2026.11.18-16.04.mp4"] = True                     # ended in game, never restarted
    session(3, gap=11)
    assert "Apex Legends_2026.11.18-16.04.mp4" in recycled


def test_apex_crash_events_read():
    """The event log lines about Apex (as wevtutil prints them on this PC) are recognised, others are not."""
    from pcs_recorder import stitch
    text = ("Event[0]\n  Log Name: Application\n  Source: EA Javelin Anticheat\n  Date: 2026-11-18T15:06:43.000\n"
            "  Event ID: 1000\n  Level: Error\n  Description: \nThe game r5apex_dx12.exe exited unexpectedly, exit code: "
            "0x40010004\n\nEvent[1]\n  Log Name: Application\n  Source: Application Error\n  Event ID: 1000\n"
            "  Description: \nFaulting application name: KillerNetworkService.exe, version: 3.1122.104.2\n")
    assert stitch.parse_events(text) == ["EA Javelin Anticheat: The game r5apex_dx12.exe exited unexpectedly, "
                                         "exit code: 0x40010004"], stitch.parse_events(text)
    assert stitch.parse_events("") == []
    assert stitch.media_duration(str(TMP / "nothing.mp4")) is None
    assert stitch.ended_in_game(str(TMP / "nothing.mp4"), None) is None


def test_ranked_rp_and_badges():
    """Ranked: the green '+N RP' at the end of our kill lines (and Team Eliminated / Previous Kill Points lines), the
    victim's rank badge, and the game's RP counter under the rank emblem. Digits are drawn from the table's own shapes."""
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_ranked as R
    from PIL import Image
    assert R.FEED_D and set(R.FEED_D.labs) == set("0123456789") and set(R.BADGES) == {"diamond", "platinum", "gold"}

    def shape(table, ch, w, h):                       # a table shape back to a w x h picture (0..1)
        g = R.FEED_D.M if table is None else table.M
        row = next(r for r in (R._T["feedDigits"] if table is None else table.rows) if r[0] == ch)
        a = np.array([int(c, 16) / 15.0 for c in row[1]], np.float32).reshape(18, 12)
        return np.asarray(Image.fromarray((a * 255).astype(np.uint8)).resize((w, h), Image.BILINEAR)) / 255.0

    def paint(img, y, x, ink, colour):
        h, w = ink.shape; reg = img[y:y + h, x:x + w].astype(np.float32)
        img[y:y + h, x:x + w] = (reg * (1 - ink[..., None]) + np.array(colour, np.float32) * ink[..., None]).astype(np.uint8)

    GREEN = (70, 245, 80)
    def feed_line(img, y, value, x_end=2384, badge=None, killer=None, words=None):
        """One feed line whose text rows start at frame row y; '+N RP' ends at x_end."""
        x = x_end - 14; img[y:y + 20, x:x + 14] = GREEN                        # P
        x -= 17; img[y:y + 20, x:x + 14] = GREEN                               # R (gap 3)
        x -= 10
        for ch in reversed(str(value)):
            r = next(r for r in R._T["feedDigits"] if r[0] == ch); w = int(round((r[2] + r[3]) / 2)); x -= w
            paint(img, y, x, shape(None, ch, w, 20), GREEN); x -= 2
        x -= 12; img[y + 9:y + 11, x:x + 10] = GREEN; img[y + 5:y + 15, x + 4:x + 6] = GREEN      # +
        if words:                                                              # white words left of it
            img[y + 2:y + 18, x - 6 - words:x - 6] = 235; img[y + 2:y + 18, x - 6 - words + 3:x - 6:7] = 40
        if badge:
            T = R.BADGES[badge]; v = (T - T.min()) / (T.max() - T.min())
            tint = {"diamond": (0.45, 0.7, 1.0), "platinum": (0.3, 1.0, 0.85), "gold": (1.0, 0.75, 0.3)}[badge]
            bx = x - 150; by = y - 4
            img[by:by + 28, bx:bx + 28] = (40 + 200 * v[..., None] * np.array(tint)).astype(np.uint8)
            img[y + 2:y + 18, bx + 34:bx + 100] = 235; img[y + 2:y + 18, bx + 37:bx + 100:6] = 40           # victim's name, white
            if killer: img[y + 2:y + 18, bx - 300:bx - 200] = killer; img[y + 2:y + 18, bx - 297:bx - 200:6] = 30
        return img

    def blank(): return np.full((1440, 2560, 3), 35, np.uint8)
    for v in (5, 7, 9, 12, 16, 25, 31, 3, 20):
        img = feed_line(blank(), 330, v)
        assert R.feed_rp(img[320:361, 1820:2470]) == v, (v, R.feed_rp(img[320:361, 1820:2470]))
    assert R.feed_rp(blank()[320:361, 1820:2470]) is None
    img = feed_line(blank(), 330, 7, badge="diamond", killer=(240, 160, 40))
    band = img[320:361, 1820:2470]
    assert R.victim_rank(band) == "diamond", R.victim_rank(band)
    assert R.killer_color(band, R.victim_badge(band)[0]) == "orange"
    assert R.victim_rank(feed_line(blank(), 330, 7, badge="platinum")[320:361, 1820:2470]) == "platinum"
    assert R.victim_rank(feed_line(blank(), 330, 7, badge="gold")[320:361, 1820:2470]) == "gold"
    no_name = feed_line(blank(), 330, 7, badge="diamond"); no_name[332:348, 2084 + 34 - 150:2084 + 100 - 150] = 35
    # (the victim's badge must be followed by a white name; a badge followed by nothing is not the victim's)

    # The tracker: a kill line read on 4 keyframes (moving down one place, its value going +8 -> +10 after a bonus),
    # a Team Eliminated line read twice, a line seen once (never counted), a line whose two reads differ (never counted).
    tr = R.Tracker()
    frames = [
        (100, [(288, 8, "diamond", None), (330, 5, None, 193)]),
        (101, [(330, 8, "diamond", None), (373, 5, None, 193)]),
        (102, [(330, 10, "diamond", None), (373, 5, None, 193), (288, 6, None, 383)]),
        (103, [(373, 10, "diamond", None)]),
        (110, [(288, 9, None, 383)]),
    ]
    for t, lines in frames:
        img = blank()
        for y, v, b, words in lines: feed_line(img, y, v, badge=b, killer=(60, 200, 210) if b else None, words=words)
        tr.read(img, t)
    out = tr.finish()
    assert [(e["t"], e["rp"], e["kind"]) for e in out] == [(100, 10, "kill"), (100, 5, "team")], out
    assert out[0]["victimRank"] == "diamond" and out[0]["color"] == "teal" and out[0]["reads"] == 2
    assert 150 <= out[0]["hue"] < 195, out[0]
    f = R.game_feed(out, 0, 200)
    assert f == {"kills": [{"t": 100, "rp": 10, "victimRank": "diamond", "color": "teal", "hue": out[0]["hue"]}], "killRp": 10, "lines": 2, "unknownLines": 0}, f
    assert R.game_feed(out, 200, 300) is None

    # RP counter: red '-48 RP' at the start, white '+237 RP' later; the box's side lines are not digits.
    def panel(value):
        img = blank(); red = value < 0; col = (225, 60, 70) if red else (240, 240, 240)
        img[135:172, 2402:2405] = col; img[135:172, 2520:2523] = col                       # the box's sides
        x = 2420; t = R.PANEL_D["red" if red else "white"]
        if red: img[154:157, x:x + 9] = col
        else: img[149:160, x + 4:x + 6] = col; img[153:156, x:x + 10] = col
        x += 13
        for ch in str(abs(value)):
            wr = next(r for r in R._T["panelWidths"] if r[0] == ch); w = int(round((wr[1] + wr[2]) / 2))
            paint(img, 146, x, shape(t, ch, w, 18), col); x += w + 2
        x += 7; img[146:164, x:x + 12] = col; x += 15; img[146:164, x:x + 12] = col
        return img
    for v in (-48, -35, -2, 8, 52, 237, 94, 110, -17):
        assert R.panel_rp(panel(v)) == v, (v, R.panel_rp(panel(v)))
    assert R.panel_rp(blank()) is None

    # Per game: two reads in a row; a change needs a green feed line from 20 s before to 5 s after it; never lower.
    reads = [(10, -48), (12, -48), (14, -46), (16, -46), (20, -48), (60, -41), (62, -41), (64, -48), (66, -48), (90, 3), (92, 3), (94, 30)]
    g = R.game_rp(reads, 0, 100, seen=[50, 51, 85])
    assert g == {"start": -48, "last": 3, "lastAt": 92, "reads": 12}, g
    assert R.game_rp(reads, 0, 100, seen=[])["last"] == -48             # no green line at all: nothing earned
    assert R.game_rp([(5, 12)], 0, 100) is None
    assert R.last_twice([5, 5, None, 7, 7, 8]) == (7, 2) and R.last_twice([5, None, 5]) == (None, 0)


# ------------------------------------------------------------------ scan on the team server (serverscan.py)

def _ss_video(folder: Path, secs: int = 6) -> Path:
    """A small mp4 with a keyframe every second (30 fps) and one audio track, like a recording."""
    import subprocess
    v = folder / "Apex Legends_2026.11.15-17.09.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=s=320x180:r=30:d={secs}",
                    "-f", "lavfi", "-i", f"sine=440:d={secs}", "-map", "0:v", "-map", "1:a", "-c:v", "mpeg4",
                    "-g", "30", "-bf", "0", "-q:v", "4", "-c:a", "aac", "-shortest", str(v)], check=True)
    return v


def test_server_scan_light_copy_and_clips_give_the_same_frames():
    """The light copy holds only keyframes (same times); a clip cut for a window gives the scanner's 60 fps reader
    exactly the frames the whole recording gives."""
    import shutil
    import subprocess
    if shutil.which("ffmpeg") is None:
        print("  (no ffmpeg: skipped)")
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import numpy as np
    import pcs_hud
    from pcs_recorder import serverscan
    folder = TMP / f"ss-{time.time_ns()}"
    folder.mkdir()
    v = _ss_video(folder)
    light = folder / "light" / v.name
    light.parent.mkdir()
    subprocess.run(serverscan.light_cmd(str(v), light), check=True)
    pk = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                  "packet=pts_time,flags", "-of", "csv=p=0", str(light)]).decode().split()
    assert [p.split(",")[1].startswith("K") for p in pk] == [True] * len(pk) and len(pk) == 6, pk
    assert [round(float(p.split(",")[0]), 2) for p in pk] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    aud = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                                   "-of", "csv=p=0", str(light)]).decode().split()
    assert len(aud) == 1
    clips = folder / "clips"
    clips.mkdir()
    subprocess.run(serverscan.clip_cmd(str(v), 2.0, 3.05, clips / "clip000.mp4"), check=True)
    whole = [(round(t, 3), f.copy()) for t, f in pcs_hud._corner(str(v), 2.0, 3.05)]
    old = pcs_hud.CLIPS, list(pcs_hud.NEEDS), pcs_hud._clip_list
    try:
        pcs_hud.CLIPS, pcs_hud._clip_list = str(clips), None
        pcs_hud.NEEDS[:] = []
        fromclip = [(round(t, 3), f.copy()) for t, f in pcs_hud._corner(str(light), 2.0, 3.05)]
        assert pcs_hud.NEEDS == []
        missing = list(pcs_hud._corner(str(light), 4.0, 4.5))          # no clip for this window: asked for
        assert missing == [] and pcs_hud.NEEDS == [[4.0, 4.5]]
    finally:
        pcs_hud.CLIPS, pcs_hud._clip_list = old[0], old[2]
        pcs_hud.NEEDS[:] = old[1]
    assert len(whole) == len(fromclip) > 20, (len(whole), len(fromclip))
    assert all(a[0] == b[0] and np.array_equal(a[1], b[1]) for a, b in zip(whole, fromclip))


class _FakeHubForScan:
    """The hub's scan-job routes as the app sees them (job: queued -> needs_clips -> done)."""

    def __init__(self, files):
        self.files, self.calls, self.status, self.sent = files, [], "queued", None

    def send_in_parts(self, url, kind, f, fields=None, stop=None, tries=3):
        self.calls.append(("upload", kind, Path(f).name, dict(fields or {})))
        return {"job": {"id": "sj-0000000000000001", "status": "queued"}}

    def request(self, url, method, path, **kw):
        self.calls.append((method, path))

        class R:
            def __init__(s, code, data=None, content=b""):
                s.status_code, s._d, s.content = code, data, content

            def json(s):
                return s._d
        if method == "GET" and "/files/" in path:
            return R(200, content=self.files[path.rsplit("/", 1)[-1]])
        if method == "GET":
            st = self.status
            if st == "queued":
                self.status = "needs_clips"
                return R(200, {"status": "queued"})
            if st == "needs_clips":
                return R(200, {"status": "needs_clips", "needs": [[100.0, 101.05], [200.0, 202.6]]})
            return R(200, {"status": "done", "passes": 2, "seconds": 90, "files": list(self.files)})
        if method == "POST" and path.endswith("/clips"):
            self.sent = [f[1][0] for f in kw["files"]]
            self.status = "done"
            return R(200, {"status": "queued"})
        if method == "DELETE":
            return R(200, {"status": "cancelled"})
        return R(404, {})


def _ss_patch(serverscan, **kw):
    """Swap module functions for fakes; returns a function that puts them back."""
    targets = {"request": serverscan.hub, "send_in_parts": serverscan.hub}
    old = {}
    for k, v in kw.items():
        mod = targets.get(k, serverscan)
        old[k] = (mod, getattr(mod, k))
        setattr(mod, k, v)

    def undo():
        for k, (mod, v) in old.items():
            setattr(mod, k, v)
    return undo


def _ss_run(runs):
    def run(cmd, timeout=900):
        runs.append(cmd)
        Path(cmd[-1]).write_bytes(b"v" * 100)
    return run


def test_server_scan_round_trip_with_clips():
    import json
    from pcs_recorder import serverscan
    folder = TMP / f"ssr-{time.time_ns()}"
    folder.mkdir()
    v = folder / "Apex Legends_2026.11.15-17.09.mp4"
    v.write_bytes(b"x" * 1000)
    out = folder / "out"
    files = {v.stem + ".scan.json": b'{"games": [1]}', v.stem + ".feed.json": b'{"lines": []}'}
    fake = _FakeHubForScan(files)
    runs = []
    undo = _ss_patch(serverscan, request=fake.request, send_in_parts=fake.send_in_parts, _run=_ss_run(runs),
                     media_duration=lambda p: 3213.683, chunks_for=lambda t: 8, scanner_version=lambda: "9.9.9")
    try:
        got = serverscan.ServerScan(str(v), str(out), "http://hub", say=lambda m: None, sleep=lambda s: None).run()
    finally:
        undo()
    assert sorted(p.name for p in got) == sorted(files)
    assert (out / (v.stem + ".scan.json")).read_bytes() == files[v.stem + ".scan.json"]
    up = [c for c in fake.calls if c[0] == "upload"][0]
    assert up[1] == "scan-source" and up[2] == v.name and up[3]["duration"] == 3213.683 and up[3]["chunks"] == 8
    assert up[3]["scanner"] == "9.9.9"
    assert "noise=drop=not(key)" in runs[0] and "-copyts" in runs[0]           # light copy: keyframes, own times
    assert len(runs) == 3 and fake.sent == ["clip000.mp4", "clip001.mp4"]      # one clip per window asked for
    assert runs[1][runs[1].index("-ss") + 1] == "99.950" and runs[1].index("-t") < runs[1].index("-i")
    assert not (out / "server").exists() and not (out / serverscan.STATE).exists()   # light copy and state gone
    assert json.loads((out / (v.stem + ".scan.json")).read_text()) == {"games": [1]}


def test_server_scan_falls_back_to_this_pc():
    """Off, no hub, worker off on the hub, an old hub or a bug: None, and the app scans here."""
    import json
    from pcs_recorder import hub, serverscan, settings as st
    folder = TMP / f"ssf-{time.time_ns()}"
    folder.mkdir()
    v = folder / "rec.mp4"
    v.write_bytes(b"x")
    keep = st.SETTINGS_FILE.read_text(encoding="utf-8") if st.SETTINGS_FILE.exists() else None

    def off(*a, **k):
        raise hub.HubError("the team hub answered 409: off", "http", 409, code="server_scan_off")
    undo = _ss_patch(serverscan, send_in_parts=off, _run=_ss_run([]), media_duration=lambda p: 60.0,
                     chunks_for=lambda t: 1, scanner_version=lambda: "9.9.9")
    try:
        st.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        st.SETTINGS_FILE.write_text(json.dumps({"hub_url": "http://hub", "server_scan": False}), encoding="utf-8")
        assert serverscan.wanted() == (False, "http://hub")
        assert serverscan.scan(str(v), str(folder / "o1"), say=lambda m: None) is None
        st.SETTINGS_FILE.write_text(json.dumps({"hub_url": "", "server_scan": True}), encoding="utf-8")
        assert serverscan.wanted()[0] is False
        st.SETTINGS_FILE.write_text(json.dumps({"hub_url": "http://hub", "server_scan": True}), encoding="utf-8")
        assert serverscan.wanted() == (True, "http://hub")
        said = []
        assert serverscan.scan(str(v), str(folder / "o2"), say=said.append) is None
        assert "Scanning on this PC" in said[-1] and not (folder / "o2" / "server").exists(), said
        serverscan.hub.send_in_parts = lambda *a, **k: None                    # hub without /api/uploads
        assert serverscan.scan(str(v), str(folder / "o3"), say=said.append) is None
        assert "older hub" in said[-1]

        def boom(*a, **k):
            raise RuntimeError("bug")
        serverscan.hub.send_in_parts = boom                                     # never lose a scan to a bug
        assert serverscan.scan(str(v), str(folder / "o4"), say=said.append) is None
    finally:
        undo()
        if keep is None:
            st.SETTINGS_FILE.unlink(missing_ok=True)
        else:
            st.SETTINGS_FILE.write_text(keep, encoding="utf-8")


def test_server_scan_picks_up_its_job_after_a_stop():
    """Stopped while Apex ran: the next try asks the hub for the same job instead of sending the recording again."""
    import json
    from pcs_recorder import serverscan
    folder = TMP / f"ssp-{time.time_ns()}"
    folder.mkdir()
    v = folder / "rec.mp4"
    v.write_bytes(b"x")
    out = folder / "out"
    out.mkdir()
    (out / serverscan.STATE).write_text(json.dumps({"job": "sj-0000000000000001"}), encoding="utf-8")
    fake = _FakeHubForScan({"rec.scan.json": b"{}"})
    fake.status = "done"
    undo = _ss_patch(serverscan, request=fake.request, send_in_parts=fake.send_in_parts, _run=_ss_run([]))
    try:
        got = serverscan.ServerScan(str(v), str(out), "http://hub", say=lambda m: None, sleep=lambda s: None).run()
    finally:
        undo()
    assert [p.name for p in got] == ["rec.scan.json"]
    assert not [c for c in fake.calls if c[0] == "upload"]


def test_server_scan_decoder_family_follows_this_pc():
    import json
    from pcs_recorder import serverscan
    f = serverscan.APP_DIR / "decoder.json"
    keep = f.read_text() if f.exists() else None
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"av1-2560x1440-gpu": ["CPU", []]}))
        assert serverscan.decoder_family() == "cpu"
        f.write_text(json.dumps({"av1-2560x1440-gpu": ["GPU d3d11va", ["-hwaccel", "d3d11va"]]}))
        assert serverscan.decoder_family() == "gpu"
        f.unlink()
        assert serverscan.decoder_family() == "gpu"
    finally:
        if keep is not None:
            f.write_text(keep)
def _check_settings(**kw):
    kw = {"encoder": "av1_texture_amf", "comms_consent": True, **kw}
    s = settings.Settings(folder=str(TMP / f"vids-{time.time_ns()}"), **kw)
    return s.clamp()


class _Usage:
    def __init__(self, free_gb):
        self.free = free_gb * 1e9


def test_rec_check_items():
    """Each item of the recording check: green / red / amber with a short fix line, nothing personal in the summary."""
    from pcs_recorder import precheck as pc
    s = _check_settings()
    # disk: a 4 h session at the chosen bitrate
    need = s.gb_per_hour * pc.SESSION_H
    assert pc.disk_item(s, lambda p: _Usage(need + 50))["state"] == "ok"
    low = pc.disk_item(s, lambda p: _Usage(need / 2))
    assert low["state"] == "bad" and "GB free" in low["text"] and low["fix"]

    def gone(p):
        raise OSError("drive missing")
    assert pc.disk_item(s, gone)["state"] == "bad"
    # other capture apps: Medal red, NVIDIA overlay / AMD Instant Replay / Game Bar amber, our own OBS ignored
    ours = str(pc.obsmod.OBS_EXE)
    assert pc.apps_item([("obs64.exe", ours), ("explorer.exe", "")])["state"] == "ok"
    red = pc.apps_item([("Medal.exe", "C:/x/Medal.exe"), ("MedalEncoder.exe", ""), ("Overwolf.exe", "")])
    assert red["state"] == "bad" and red["text"].startswith("Medal, Overwolf are running") and "Close Medal" in red["fix"]
    amber = pc.apps_item([("NVIDIA Share.exe", "")], amd_replay=True, gamebar_bg=True)
    assert amber["state"] == "warn" and "Alt+Z" in amber["fix"] and "Record what happened" in amber["fix"]
    assert pc.apps_item([("obs64.exe", "C:/Program Files/obs-studio/bin/64bit/obs64.exe")])["state"] == "warn"
    # encoder: the test's result is reused
    assert pc.encoder_item(s, "amd radeon rx 9070 xt")["state"] == "ok"
    assert "AV1 on your AMD card" in pc.encoder_item(s, "amd radeon rx 9070 xt")["text"]
    assert pc.encoder_item(s, "nvidia geforce rtx 3060 ti")["state"] == "bad"          # card changed since the test
    assert pc.encoder_item(s, None, last_failed=True)["state"] == "warn"
    assert pc.encoder_item(s, None, setting_up=True)["state"] == "wait"
    assert pc.encoder_item(_check_settings(encoder=""), None)["state"] == "bad"
    # game sound: only once Apex runs
    assert pc.game_item(s, False)["text"] == "Checked when Apex starts"
    assert pc.game_item(s, True, 0.2)["state"] == "ok" and pc.game_item(s, True, 0.0, done=True)["state"] == "bad"
    assert pc.game_item(_check_settings(game_audio=False), True)["state"] == "off"
    # voice app and mic
    assert pc.voice_item(s, set())["state"] == "bad" and "Discord isn't open" in pc.voice_item(s, set())["text"]
    early = pc.voice_item(s, set(), apex=False)                                         # before Apex: normal, grey
    assert early["state"] == "wait" and "checked when Apex starts" in early["text"]
    assert pc.voice_item(s, {"discord.exe"}, 0.3)["state"] == "ok"
    assert pc.voice_item(s, {"discord.exe"}, 0.0)["state"] == "wait"                   # nobody talks: not red
    assert pc.voice_item(settings.Settings().clamp(), set())["state"] == "off"          # no team consent
    mics = [{"id": "{0.0.1.00000000}.{abc}", "name": "Mic"}]
    assert pc.mic_item(s, mics, True)["state"] == "bad"                                 # Windows privacy blocks it
    s2 = _check_settings(mic_device="{0.0.1.00000000}.{gone}")
    assert pc.mic_item(s2, mics, False)["state"] == "bad" and pc.mic_item(s, [], False)["state"] == "bad"
    assert pc.mic_item(s, mics, False, 0.01)["state"] == "ok" and pc.mic_item(s, mics, False, 0.0)["state"] == "warn"
    # the summary for crash reports: states and app names only
    items = {"obs": pc.obs_item(True, False, "OBS didn't answer on port 4466: refused"), "apps": red,
             "disk": low, "mic": pc.mic_item(s2, mics, False)}
    line = pc.summary(items)
    assert line.startswith("obs:bad(") and "apps:bad(Medal, Overwolf)" in line and "disk:bad" in line, line
    assert "vids-" not in line and "{gone}" not in line and len(line) <= 300


class _FakeAdapter:
    """Stands in for runtime.ObsAdapter: ensure_ready() starts OBS (obs["on"]) when it isn't running."""
    def __init__(self, fail=None, obs=None):
        self.fail, self.calls, self.ready = fail, 0, False
        self.obs = obs if obs is not None else {"on": False}

    def ensure_ready(self):
        self.calls += 1
        self.obs["on"] = True
        if self.fail:
            raise RuntimeError(self.fail)
        self.ready = True


def _check_for(s, rec=None, adapter=None, ui=None, procs=(), peaks=None):
    from pcs_recorder import precheck as pc
    c = pc.RecCheck(s, adapter if adapter is not None else _FakeAdapter(), rec, ui)
    c.threaded = False
    c.procs = lambda: list(procs)
    c.disk = lambda p: _Usage(500)
    c.mics = lambda: [{"id": "{0.0.1.00000000}.{abc}", "name": "Mic"}]
    c.mic_blocked = c.amd_replay = c.gamebar = lambda: False
    c.installed = lambda: True
    c.read_gpu = lambda: "amd radeon rx 9070 xt"
    ad = c.adapter
    obs_ = ad.obs if isinstance(ad, _FakeAdapter) else {"on": False}
    c.obs_state, c.closes = obs_, []
    c.obs_running = lambda: obs_["on"]
    c.close_obs = lambda: (c.closes.append(1), obs_.update(on=False))
    c.listened = []

    def listen(s_, names, seconds, stop=lambda: False, enough=lambda p: False):
        c.listened.append((sorted(set(names.values())), seconds))
        return dict(peaks or {})
    c.listen = listen
    c.confirm_after = 0
    return c


def test_rec_check_full_run():
    """Check again: OBS started and asked (no encoder test, no restart), voice / mic from the level meters."""
    s = _check_settings()
    ad = _FakeAdapter()
    c = _check_for(s, adapter=ad, procs=[("Discord.exe", "")], peaks={"discord": 0.2, "mic": 0.0})
    assert c.start(manual=True)
    pub = c.public()
    st_ = {i["id"]: i["state"] for i in pub["items"]}
    assert ad.calls == 1 and not pub["running"] and pub["at"], pub
    assert c.closes == [1] and not c.obs_state["on"]                       # started by the check: closed again
    assert st_ == {"obs": "ok", "encoder": "ok", "game": "wait", "voice": "ok", "mic": "warn", "disk": "ok",
                   "apps": "ok"}, st_
    assert c.listened == [(["discord", "mic"], c.listen_s)]                 # game sound is not listened before Apex
    assert pub["red"] == 0
    # OBS doesn't answer: red with a fix, the meters aren't asked
    c2 = _check_for(s, adapter=_FakeAdapter("OBS didn't answer on port 4466: refused"), procs=[("Discord.exe", "")])
    c2.start(manual=True)
    obs_ = c2.public()["items"][0]
    assert obs_["id"] == "obs" and obs_["state"] == "bad" and "Check again" in obs_["fix"] and c2.listened == []
    assert c2.public()["red"] == 1


def test_rec_check_leaves_obs_as_found():
    """The automatic run (app start, Windows logon) never starts OBS; Check again starts it only to check and closes
    it again when it wasn't running, never while Apex runs or the encoder test runs."""
    s = _check_settings()

    class _R:
        apex, current = False, None
        idx = {}
    rec = _R()
    ad = _FakeAdapter()
    c = _check_for(s, rec=rec, adapter=ad, peaks={"discord": 0.1, "mic": 0.1})
    c.start()                                                                    # automatic, OBS not running
    st_ = {i["id"]: i for i in c.public()["items"]}
    assert ad.calls == 0 and not c.obs_state["on"] and c.closes == [] and c.listened == []
    assert st_["obs"]["state"] == "wait" and st_["obs"]["text"] == "Checked when Apex starts"
    assert st_["voice"]["state"] == "wait" and st_["game"]["state"] == "wait"   # Discord not open yet: grey
    from pcs_recorder import precheck as pc
    c.last_start = pc_item = pc.item("obs", "ok", "Started fine")
    c.start()
    assert c.public()["items"][0] == pc_item and ad.calls == 0                  # the last real start's result
    c.obs_state["on"] = True                                                     # OBS already running: asked, kept
    c.start()
    assert ad.calls == 0 and c.public()["items"][0]["text"] == "Running" and c.closes == [] and c.listened
    c.start(manual=True)
    assert ad.calls == 1 and c.closes == [] and c.obs_state["on"]               # was running: left running
    c.obs_state["on"] = False
    rec.apex = True                                                              # Apex runs (not recording yet)
    c.start(manual=True)
    assert ad.calls == 1 and not c.obs_state["on"] and c.closes == []
    rec.apex = False
    c.setting_up = lambda: True                                                  # encoder test running
    c.start(manual=True)
    assert ad.calls == 1 and c.public()["items"][0]["state"] == "wait"
    c.setting_up = lambda: False
    # Apex starts while Check again listens: OBS stays open for the recording
    c.listen = lambda *a, **k: (setattr(rec, "apex", True), {})[1]
    c.start(manual=True)
    assert ad.calls == 2 and c.obs_state["on"] and c.closes == []


def test_rec_check_at_apex_start():
    """Apex starts: one message when something is red (once per Apex session), Apex's sound confirmed from the
    meters in the first minutes; no sound = red, a message and a crash report carrying the check's summary."""
    crash, s, sent = _crash_setup()
    try:
        retention.INDEX_FILE.unlink(missing_ok=True)
        s.folder = str(TMP / f"vids-{time.time_ns()}")
        s.comms_consent = True
        apex = {"on": False}
        obs, ui = FakeOBS(s.folder_path), FakeUI(False)
        r = Recorder(s, obs, FakeHub(), ui, fake_scan, is_apex_running=lambda: apex["on"])
        r._final_file = lambda p, wait_s=0: p
        c = _check_for(s, rec=r, ui=ui, procs=[("Medal.exe", ""), ("Discord.exe", "")],
                       peaks={"game": 0.0, "discord": 0.1, "mic": 0.05})
        r.check = c
        r.tick()
        apex["on"] = True; r.tick(); r.tick()
        notes = [n for n in ui.notes if n[0] == "Recording check"]
        assert len(notes) == 2, ui.notes                       # Medal (at start) + no Apex sound (first minutes)
        assert "Medal" in notes[0][1] and "Fix:" in notes[0][1] and notes[0][2] is True
        assert "No Apex sound" in notes[1][1]
        st_ = {i["id"]: i["state"] for i in c.public()["items"]}
        assert st_["game"] == "bad" and st_["voice"] == "ok" and st_["mic"] == "ok" and st_["obs"] == "ok", st_
        assert sent[-1]["kind"] == "record_start" and "No Apex sound" in sent[-1]["error"]
        assert "apps:bad(Medal)" in sent[-1]["context"]["check"] and "game:bad" in sent[-1]["context"]["check"]
        apex["on"] = False; r.tick()
        # next session, sound fine: one message again (Medal still runs), no new report
        n_sent = len(sent)
        c.listen = lambda *a, **k: {"game": 0.3, "discord": 0.1, "mic": 0.05}
        apex["on"] = True; r.tick()
        assert len([n for n in ui.notes if n[0] == "Recording check"]) == 3
        assert {i["id"]: i["state"] for i in c.public()["items"]}["game"] == "ok" and len(sent) == n_sent
        apex["on"] = False; r.tick()
    finally:
        _crash_teardown()


def test_rec_check_in_failed_start_report():
    """Recording doesn't start: the crash report carries the check (what was red), one message with the cause."""
    crash, s, sent = _crash_setup()
    try:
        retention.INDEX_FILE.unlink(missing_ok=True)
        s.folder = str(TMP / f"vids-{time.time_ns()}")
        apex = {"on": False}
        obs, ui = FakeOBS(s.folder_path), FakeUI(False)

        def bad_start():
            raise RuntimeError("OBS didn't answer on port 4466")
        obs.start = bad_start
        r = Recorder(s, obs, FakeHub(), ui, fake_scan, is_apex_running=lambda: apex["on"])
        r.check = _check_for(s, rec=r, ui=ui, procs=[("Outplayed.exe", "")])
        apex["on"] = True; r.tick(); r.tick(); r.tick()          # the recorder tries again every tick
        ctx = sent[0]["context"]["check"]
        assert sent[0]["kind"] == "record_start" and "obs:bad(" in ctx and "apps:bad(Outplayed)" in ctx, ctx
        msgs = [n for n in ui.notes if n[0] == "Recording check"]
        assert len(msgs) == 1 and "Outplayed" in msgs[0][1], ui.notes
    finally:
        _crash_teardown()


def run_all(tests):
    """Run every test, even after a failure, and list the failures at the end. -> exit code (1 if any failed)."""
    import traceback
    failed = []
    for name, fn in tests:
        try:
            fn()
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            failed.append(name)
            print(f"FAIL {name}", flush=True)
            traceback.print_exc()
        else:
            print("ok", name, flush=True)
    print(f"\n{len(tests) - len(failed)} passed, {len(failed)} failed", flush=True)
    for name in failed:
        print("  failed:", name, flush=True)
    return 1 if failed else 0


def test_scanner_speedups_give_the_same_numbers():
    """The faster weapon match, debug overlay glyphs and teammate-card bar runs give exactly the old values
    (checked here on random data; on real recordings: same scan.json / feed.json for 3 recordings)."""
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
    import pcs_hud as H
    import pcs_respawn as R
    rng = np.random.default_rng(7)
    # weapon match: numpy's irfft2 vs its two steps with only the needed rows (bit for bit)
    FH, FW = H.Weapons.FH, H.Weapons.FW
    for h, th, tw in ((44, 29, 80), (64, 13, 88), (36, 32, 40)):
        c = np.zeros((FH, FW)); c[:h, :600] = rng.integers(0, 256, (h, 600))
        t = np.zeros((FH, FW)); t[:th, :tw] = rng.normal(size=(th, tw))
        G, F = np.fft.rfft2(c), np.conj(np.fft.rfft2(t))
        old = np.fft.irfft2(G * F, s=(FH, FW))[:h - th + 1, :600 - tw + 1]
        new = np.fft.irfft(np.fft.ifft(G * F, FH, 0)[:h - th + 1], FW, 1)[:, :600 - tw + 1]
        assert np.array_equal(old, new), (h, th, tw)
    # debug overlay line: every glyph at once vs one at a time
    m = rng.random((70, 300)) > 0.6
    for k, (y, x) in enumerate(((22, 3 + 8 * 8), (50, 3 + 8 * 20))):            # real glyphs in some cells
        m[y:y + 9, x:x + 7] = list(H.DBG_T.values())[k] > 0
    m[22:31, 3 + 8 * 10:3 + 8 * 11] = False                                      # an empty cell
    stack = H._DBG_STACK
    try:
        new = [H._dbg_line(m, y) for y in (22, 50)]
        H._DBG_STACK = None
        old = [H._dbg_line(m, y) for y in (22, 50)]
    finally:
        H._DBG_STACK = stack
    assert new == old and stack is not None, (new, old)
    # teammate cards: white run lengths without the per-row loop
    for _ in range(5):
        a = rng.integers(0, 256, (1440, 2560, 3), dtype=np.uint8)
        a[R.Y0 + 40:R.Y0 + 52, R.NAME_X0 + 5:R.NAME_X0 + 120] = 255                  # a health bar
        white, _ = R.masks(a)
        wl = R._left(white)
        old = np.array([(np.argmin(white[y, wl[y]:]) if not white[y, wl[y]:].all() else white.shape[1] - wl[y])
                        if wl[y] < 999 else 0 for y in range(white.shape[0])])
        after = ~white & (np.arange(white.shape[1])[None, :] >= wl[:, None])
        new = np.where(wl < 999, np.where(after.any(1), after.argmax(1) - wl, white.shape[1] - wl), 0)
        assert np.array_equal(old, new)
        assert R.read_cards(a) is not None



# ------------------------------------------------------------------ clips for Discord (pcs_recorder/clips.py)
def test_clip_size_rules():
    """Discord clips stay under 10 MB: bitrate from the length, picture smaller and 30 fps as the bitrate drops,
    too long refused; H.264 on the card the recorder uses (NVENC / AMF / Quick Sync), else x264."""
    from pcs_recorder import clips
    assert clips.DISCORD_TARGET < clips.DISCORD_BYTES <= 10_000_000
    k30 = clips.discord_video_kbps(30, 1)
    assert 2000 < k30 < 2500 and (k30 + 96) * 30 * 1000 / 8 < clips.DISCORD_TARGET
    assert clips.discord_video_kbps(60, 0) > clips.discord_video_kbps(60, 2)       # no sound = more for the picture
    m = clips.discord_max_s(1)
    assert clips.discord_video_kbps(m, 1) >= clips.MIN_VIDEO_KBPS > clips.discord_video_kbps(m + 5, 1)
    assert clips.out_format(2560, 1440, 60, "discord", 8000) == (1920, 1080, 60)
    assert clips.out_format(2560, 1440, 60, "discord", 2256) == (1280, 720, 30)
    assert clips.out_format(2560, 1440, 60, "discord", 734) == (854, 480, 30)
    assert clips.out_format(1280, 720, 60, "discord", 8000) == (1280, 720, 60)      # never larger than the recording
    assert clips.out_format(1440, 1080, 60, "discord", 2256) == (960, 720, 30)      # 4:3 stays 4:3
    assert clips.out_format(2560, 1440, 60, "full", None) == (2560, 1440, 60)
    assert clips.full_kbps(2560, 1440, 60) == 20000 and 12000 < clips.full_kbps(1920, 1080, 60) < 14000
    assert [clips.hw_encoder(e) for e in ("jim_av1_nvenc", "obs_nvenc_hevc_tex", "av1_texture_amf", "obs_qsv11_v2", "")] == \
        ["h264_nvenc", "h264_nvenc", "h264_amf", "h264_qsv", "libx264"]
    for enc in ("h264_nvenc", "h264_amf", "h264_qsv", "libx264"):
        a = clips.encoder_args(enc, "discord", 2000)
        assert a[a.index("-c:v") + 1] == enc and "-maxrate" in a and "3000k" in a
        assert "-maxrate" in clips.encoder_args(enc, "full", 20000)                 # full quality has a ceiling too


def test_clip_note_spans():
    """Coach notes in clip time, as the review player shows them (t - 0.05 to t + dur); notes without text or
    drawing are skipped; same notes side by side are one picture."""
    from pcs_recorder import clips
    a = {"t": 10, "dur": 5, "text": "Push", "drawings": []}
    b = {"t": 12, "dur": 10, "text": "", "drawings": [{"kind": "pen", "color": "#fff", "pts": [[0, 0]]}]}
    empty = {"t": 11, "dur": 5, "text": " ", "drawings": []}
    far = {"t": 100, "dur": 5, "text": "later"}
    sp = clips.note_spans([a, b, empty, far], 8, 20)
    assert [(round(x, 2), round(y, 2), [n is a for n in on]) for x, y, on in sp] == \
        [(1.95, 3.95, [True]), (3.95, 7.0, [True, False]), (7.0, 12.0, [False])], sp
    assert clips.note_spans([a], 16, 30) == [] and clips.note_spans([], 0, 5) == []
    many = [{"t": i, "dur": 0.5, "text": str(i)} for i in range(100)]
    assert len(clips.note_spans(many, 0, 100)) == clips.MAX_OVERLAYS


def test_clip_teammate_offsets():
    """A teammate's track: their time = this recording's time + off, only inside the game part it belongs to."""
    from pcs_recorder import clips
    parts = [{"start": 50, "end": 1550, "off": 140}, {"start": 1550, "end": None, "off": 140}]
    assert clips.team_inputs(parts, 100, 130) == [(240, 30, 0)]
    assert clips.team_inputs(parts, 1540, 1560) == [(1680, 10, 0), (1690, 10, 10)]       # across two games
    assert clips.team_inputs([{"start": 0, "end": None, "off": -20}], 10, 40) == [(0, 20, 10)]   # they started later
    assert clips.team_inputs([{"start": 0, "end": 30, "off": 5}], 40, 60) == []


def test_clip_ffmpeg_command():
    """One ffmpeg run: video cut at the range, notes laid over for their time, chosen tracks mixed at their levels
    with teammates' tracks delayed to their place; no tracks = no sound."""
    from pcs_recorder import clips
    src = {"codec": "av1", "w": 2560, "h": 1440, "fps": 60}
    cmd = clips.build_cmd("rec.mp4", "out.part", 100, 130, w=1280, h=720, fps=30, src=src, own=[0, 2],
                          team=[("zed.m4a", [{"start": 0, "end": None, "off": -110}])],
                          overlays=[("n0.png", 2, 5)], enc="h264_amf", size="discord", vkbps=2000, hwdec=False)
    s = " ".join(cmd)
    assert "-hwaccel" not in cmd and cmd[cmd.index("-ss") + 1] == "100.000" and cmd[-1] == "out.part"
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "scale=1280:720" in fc and "fps=30" in fc and "between(t,2.000,5.000)" in fc
    assert "[0:a:0]" in fc and "[0:a:2]" in fc and "[0:a:1]" not in fc                 # track 2 (voice app) not chosen
    assert "adelay=10000|10000" in fc and "amix=inputs=4:duration=first:normalize=0" in fc   # silence + 3 tracks
    assert "-ss 0.000 -t 20.000 -i zed.m4a" in s and "-b:a 96k" in s and "-movflags +faststart" in s
    quiet = clips.build_cmd("rec.mp4", "o", 0, 5, w=2560, h=1440, fps=60, src=src, own=[], team=[], overlays=[],
                            enc="libx264", size="full", vkbps=20000, hwdec=True)
    assert "-an" in quiet and "amix" not in " ".join(quiet)
    fc = quiet[quiet.index("-filter_complex") + 1]
    assert "scale" not in fc and "fps=" not in fc                               # full: the recording's size
    if os.name == "nt":
        i = quiet.index("-hwaccel")
        assert quiet[i + 1:i + 4] == ["d3d11va", "-c:v", "av1"]


def test_clip_note_picture():
    """The coach's drawing and note text drawn at the clip's size: lines where they are, see-through elsewhere."""
    from PIL import Image
    from pcs_recorder import clips
    out = TMP / "clipnote" / "n.png"
    note = {"t": 0, "dur": 5, "text": "Rotate now, ring is closing", "drawings": [
        {"kind": "arrow", "color": "#ff3b30", "pts": [[0.1, 0.5], [0.5, 0.5]]},
        {"kind": "rect", "color": "#0a84ff", "pts": [[0.1, 0.7], [0.3, 0.9]]}]}
    clips.render_notes([note], 640, 360, out)
    im = Image.open(out).convert("RGBA")
    assert im.size == (640, 360)
    r, g, b, a = im.getpixel((192, 180))                  # on the arrow
    assert a == 255 and r > 200 and g < 120, (r, g, b, a)
    assert im.getpixel((5, 350))[3] == 0                  # nothing there
    r, g, b, a = im.getpixel((int(0.1 * 640), 290))      # left side of the box
    assert a == 255 and b > 200, (r, g, b, a)
    top_right = [im.getpixel((x, y)) for x in range(400, 640) for y in range(0, 60)]
    assert any(p[0] > 240 and p[1] > 200 and p[2] < 60 and p[3] == 255 for p in top_right)   # the yellow bar
    clips.render_notes([{"text": "x" * 3000, "drawings": [{"kind": "bogus", "color": "nope", "pts": [[2, 2]]}]}],
                       320, 180, out)                     # odd input doesn't break it


def test_clip_clipboard_data():
    """Copy file: the clipboard gets the file as Explorer copies it (CF_HDROP, wide names, ends with two NULs)."""
    import struct
    from pcs_recorder import clips
    p = str(TMP / "Clips" / "a clip.mp4")
    d = clips.hdrop([p])
    assert struct.unpack("<IiiII", d[:20]) == (20, 0, 0, 0, 1)
    assert d[20:].decode("utf-16-le") == os.path.abspath(p) + "\0\0"
    if os.name == "nt" and os.environ.get("CI"):          # the real clipboard only on CI (not a player's clipboard)
        import ctypes
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_bytes(b"x")
        clips.copy_files_to_clipboard([p])
        u32, sh = ctypes.windll.user32, ctypes.windll.shell32
        u32.GetClipboardData.restype = ctypes.c_void_p
        sh.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
        assert u32.OpenClipboard(None)
        try:
            h = u32.GetClipboardData(15)
            buf = ctypes.create_unicode_buffer(520)
            assert h and sh.DragQueryFileW(h, 0, buf, 520) and buf.value == os.path.abspath(p)
        finally:
            u32.CloseClipboard()


def _clip_source(folder, secs=8):
    import subprocess
    folder.mkdir(parents=True, exist_ok=True)
    v = folder / "Apex Legends_clip-test.mp4"
    tones = []
    for f in (300, 500, 700):
        tones += ["-f", "lavfi", "-i", f"sine=f={f}:d={secs}"]
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=640x360:r=30:d={secs}"]
                   + tones + ["-map", "0", "-map", "1", "-map", "2", "-map", "3", "-c:v", "libx264", "-preset",
                              "ultrafast", "-c:a", "aac", str(v)], check=True)
    mate = folder / "mate.m4a"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=f=900:d={secs + 5}", "-c:a", "aac",
                    str(mate)], check=True)
    return v, mate


def _clip_wait(c, s=120):
    end = time.time() + s
    while c.state()["running"] and time.time() < end:
        time.sleep(0.05)
    return c.state()


def test_clip_made_with_ffmpeg():
    """A real clip: saved in Clips next to the recording as H.264 + AAC MP4 (plays in Discord), the range's length,
    one mixed sound track, coach note burned in; under 10 MB; no .part or temp files left; Show / Copy only on it."""
    import json as _json
    import shutil
    import subprocess
    from pcs_recorder import clips
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        print("  (skipped: no ffmpeg)")
        return
    v, mate = _clip_source(TMP / "cliprec")
    c = clips.Clipper()
    note = {"t": 3, "dur": 2, "text": "Here", "drawings": [{"kind": "circle", "color": "#ffd60a", "pts": [[.3, .3], [.7, .7]]}]}
    c.start({"video": str(v), "start": 1.5, "end": 6.5, "size": "discord", "own": [0, 2],
             "team": [(str(mate), [{"start": 0, "end": None, "off": 2}], "Zed")], "notes": [note], "obs_encoder": ""})
    st = _clip_wait(c)
    assert st["step"] == "Done" and not st["error"], st
    f = Path(st["file"])
    assert f.parent == v.parent / "Clips" and f.name == "Apex Legends_clip-test clip 0m01s-0m06s.mp4"
    assert f.stat().st_size < clips.DISCORD_BYTES and st["encoder"] == "libx264"
    pr = _json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                     "stream=codec_type,codec_name,pix_fmt,channels:format=duration", "-of", "json",
                                     str(f)], capture_output=True, text=True).stdout)
    kinds = [(x["codec_type"], x["codec_name"]) for x in pr["streams"]]
    assert kinds == [("video", "h264"), ("audio", "aac")], kinds
    assert pr["streams"][0]["pix_fmt"] == "yuv420p" and pr["streams"][1]["channels"] == 2
    assert abs(float(pr["format"]["duration"]) - 5.0) < 0.15, pr["format"]
    frame = subprocess.run(["ffmpeg", "-v", "error", "-ss", "2", "-i", str(f), "-frames:v", "1", "-f", "rawvideo",
                            "-pix_fmt", "rgb24", "-"], capture_output=True).stdout          # clip 2 s = recording 3.5 s

    def px(x, y):
        i = (y * 640 + x) * 3
        return frame[i:i + 3]
    assert any(p[0] > 200 and p[1] > 170 and p[2] < 90 for p in (px(x, 180) for x in range(185, 200))), "no circle"
    assert not list(f.parent.glob("*.part")) and not list(Path(tempfile.gettempdir()).glob("pcs-clip-*"))
    assert c.own_clip(str(f)) == str(f) and c.own_clip(str(v)) is None                 # Show / Copy: only its clips
    # no sound chosen: no sound track; same name again gets (2)
    c.start({"video": str(v), "start": 1.5, "end": 6.5, "size": "full", "own": [], "team": [], "notes": [],
             "obs_encoder": ""})
    st = _clip_wait(c)
    assert st["name"].endswith(" (2).mp4"), st
    pr = _json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json",
                                     st["file"]], capture_output=True, text=True).stdout)
    assert [x["codec_type"] for x in pr["streams"]] == ["video"]


def test_clip_stops_and_falls_back():
    """Apex starting stops the clip (nothing left behind); a graphics card encoder that fails falls back to the CPU;
    a Discord clip that came out too big is made again smaller."""
    from pcs_recorder import clips
    folder = TMP / "clipfake"
    folder.mkdir(parents=True, exist_ok=True)
    v = folder / "rec.mp4"
    v.write_bytes(b"video")
    src = {"codec": "hevc", "w": 2560, "h": 1440, "fps": 60, "dur": 600}
    busy = {"why": None}
    runs = []
    script = {}

    def run(cmd, length):
        runs.append(cmd)
        enc = cmd[cmd.index("-c:v", cmd.index("-filter_complex")) + 1]
        if enc == "h264_nvenc" and "-hwaccel" in cmd:
            return 1, "decoder failed"
        if script.get("apex"):
            busy["why"] = "Apex is running"
        Path(cmd[-1]).write_bytes(b"x" * script["sizes"].pop(0))
        return 0, ""
    c = clips.Clipper(lambda: busy["why"], run=run)
    spec = {"video": str(v), "start": 10, "end": 40, "size": "discord", "src": src, "own": [0], "team": [], "notes": [],
            "obs_encoder": "jim_nvenc"}
    # card encoder with card decoding fails -> same encoder, CPU decoding; first file too big -> made again lower
    script.update(sizes=[12_000_000, 9_000_000])
    c.start(spec)
    st = _clip_wait(c)
    assert st["step"] == "Done" and st["mb"] == 9.0 and not st["over"], st
    rates = [int(r[r.index("-b:v") + 1].rstrip("k")) for r in runs]
    if os.name == "nt":
        assert len(runs) == 3 and "-hwaccel" in runs[0] and "-hwaccel" not in runs[1], runs
    assert rates[2] < rates[1] * 0.8, rates
    # Apex starts during the clip: stopped, no file, no .part
    runs.clear()
    script.update(sizes=[5_000_000], apex=True)
    spec["end"] = 20
    c.start(spec)
    st = _clip_wait(c)
    assert st["step"] == "Stopped" and "Apex" in st["error"] and not st["file"], st
    assert not list((folder / "Clips").glob("*0m10s-0m20s*")) and not list((folder / "Clips").glob("*.part"))
    # Apex running: refused at once
    try:
        c.start(spec)
        raise AssertionError("not refused")
    except RuntimeError as e:
        assert "Apex" in str(e)
    busy["why"] = None
    script["apex"] = False
    # too long for 10 MB: said so, nothing made
    spec.update(start=0, end=500)
    c.start(spec)
    st = _clip_wait(c)
    assert st["step"] == "Failed" and "Too long for 10 MB" in st["error"], st

# ---------------------------------------------------------------- comms transcripts (pcs_recorder/comms.py)

def _comms_setup(name, mic=True, consent=True):
    """A scanned recording with an events.json, settings with the given answers, and a Comms job on it."""
    import json
    from pcs_recorder import comms
    d = TMP / name
    d.mkdir(parents=True, exist_ok=True)
    rec = d / "Apex Legends_2026.11.19-20.00.mp4"
    rec.write_bytes(b"x" * 100)
    rec.with_suffix(".events.json").write_text(json.dumps({"tracks": ["game", "discord", "mic"] if mic else ["game"]}))
    (d / (rec.stem + ".scan")).mkdir(exist_ok=True)
    s = settings.Settings()
    s.player, s.comms_consent, s.mic_audio, s.transcript_consent, s.hub_url = "tester", True, True, consent, "http://hub"
    idx = {"k1": retention.Entry(path=str(rec), started=time.time() - 600, status="uploaded")}
    sent = []
    job = comms.Comms(lambda: s, lambda: idx, lambda: None, lambda files: sent.append([Path(f).name for f in files]))
    job.st = {"recs": {}}
    return comms, job, s, idx, rec, sent


FAKE_WORKER = r'''
import json, sys, time
a = sys.argv[1:]
out = a[a.index("--out") + 1]
if a[0].endswith("sleep.mp4"):
    time.sleep(60)
print(json.dumps({"done": 10, "total": 20}), flush=True)
json.dump({"v": 1, "kind": "comms", "speaker": a[a.index("--speaker") + 1], "audioSeconds": 20, "seconds": 1,
           "language": "fr", "segments": [{"start": 1.5, "end": 3.0, "text": "il est one shot", "speaker":
           a[a.index("--speaker") + 1], "p": -0.2}]}, open(out, "w"))
print(json.dumps({"finished": True}), flush=True)
'''


def _fake_worker(comms, sleep=False):
    w = TMP / "fake_worker.py"
    w.write_text(FAKE_WORKER)
    comms.__dict__.setdefault("_real_get_runtime", comms.get_runtime)
    comms.__dict__.setdefault("_real_get_model", comms.get_model)
    comms.get_runtime = lambda busy=None, component=None, progress=None: [sys.executable, str(w)]
    comms.get_model = lambda busy=None, progress=None: TMP


def test_comms_only_with_the_players_yes():
    """No transcript without the yes (or without the team's comms consent / the mic), and only recordings with a mic
    track; the transcript never goes to Drive with the results."""
    comms, job, s, idx, rec, _ = _comms_setup("comms-consent", consent=False)
    assert not comms.eligible(s) and job.tick() is False and not job.running
    s.transcript_consent = True
    assert comms.eligible(s)
    s.comms_consent = False
    assert not comms.eligible(s)
    s.comms_consent, s.mic_audio = True, False
    assert not comms.eligible(s)
    s.mic_audio = True
    assert job.todo() == [("k1", str(rec))]
    assert comms.mic_stream(rec) == 2
    idx["k1"].status = "recorded"                      # not scanned yet: waits
    assert job.todo() == []
    idx["k1"].status = "scanned"
    idx["k1"].started = time.time() - 5 * 86400        # too old
    assert job.todo() == []
    # the transcript and its kept parts stay out of the scan's results (Drive, re-sends)
    from pcs_recorder.core import result_files
    out = rec.with_suffix(".scan")
    (out / (rec.stem + ".scan.json")).write_text("{}")
    (out / (rec.stem + ".comms.json")).write_text("{}")
    (out / (rec.stem + ".comms.partial.json")).write_text("{}")
    assert [p.name for p in result_files(out)] == [rec.stem + ".scan.json"]


def test_comms_transcribes_and_sends_to_hub():
    comms, job, s, idx, rec, sent = _comms_setup("comms-run")
    _fake_worker(comms)
    out = job.transcribe(str(rec))
    assert out == comms.comms_path(rec) and out.is_file()
    d = comms.transcript_of(rec)
    assert d["speaker"] == "tester" and d["segments"][0]["text"] == "il est one shot" and d["segments"][0]["start"] == 1.5
    r = job.st["recs"][rec.stem]
    assert r["status"] == "done" and r["segments"] == 1 and r["sent"]
    assert sent == [[rec.stem + ".comms.json"]]
    assert job.todo() == []                             # done once
    # no mic track: nothing to transcribe, never asked again
    comms2, job2, *_ , rec2, sent2 = _comms_setup("comms-nomic", mic=False)
    assert job2.transcribe(str(rec2)) is None and job2.st["recs"][rec2.stem]["status"] == "no_mic" and job2.todo() == []
    # said no after it was made: not sent
    comms3, job3, s3, idx3, rec3, sent3 = _comms_setup("comms-no-after")
    _fake_worker(comms3)
    s3.hub_url = ""
    job3.transcribe(str(rec3))
    assert sent3 == [] and job3.unsent() == []
    s3.hub_url, s3.transcript_consent = "http://hub", False
    assert job3._send(comms3.comms_path(rec3)) is False and sent3 == []


def test_comms_not_sent_to_a_hub_without_comms():
    """A hub whose /api/me doesn't list "comms" (0.7.0 and older) would keep a transcript as a plain file any teammate
    can download: nothing is sent, the file waits, and goes once the hub says it takes transcripts."""
    comms, job, s, idx, rec, sent = _comms_setup("comms-old-hub")
    _fake_worker(comms)
    ok = [False]
    job.hub_ok = lambda: ok[0]
    job.transcribe(str(rec))
    assert sent == [] and job.st["recs"][rec.stem]["sendNote"] == comms.HUB_TOO_OLD
    assert job.unsent() == [comms.comms_path(rec)]
    ok[0] = True
    assert job._send(comms.comms_path(rec)) is True and sent == [[rec.stem + ".comms.json"]]


def test_hub_has_feature():
    from pcs_recorder import hub
    calls = []

    class R:
        def __init__(self, ok, body): self.ok, self._b = ok, body
        def json(self): return self._b
    answers = {"http://new": R(True, {"features": ["comms"]}), "http://old": R(True, {"name": "x"}),
               "http://down": None}

    def fake(url, method, path, **kw):
        calls.append(url)
        if answers[url] is None:
            raise hub.HubError("no answer", "network")
        return answers[url]
    real, hub.request = hub.request, fake
    try:
        hub._features.clear()
        assert hub.has_feature("http://new", "comms") and hub.has_feature("http://new", "comms")
        assert calls.count("http://new") == 1                      # cached
        assert not hub.has_feature("http://old", "comms") and not hub.has_feature("http://down", "comms")
        assert not hub.has_feature("", "comms")
    finally:
        hub.request = real
        hub._features.clear()


def test_comms_stops_when_apex_starts():
    """Apex starting stops the transcriber within seconds; the stop is not counted as a failed try."""
    comms, job, s, idx, rec, sent = _comms_setup("comms-apex")
    slow = rec.with_name("sleep.mp4")
    rec.rename(slow)
    rec.with_suffix(".events.json").rename(slow.with_suffix(".events.json"))
    _fake_worker(comms, sleep=True)
    comms.BUSY_EVERY_S = 0.2
    t0 = time.time()
    job.busy = lambda: "Apex is running" if time.time() - t0 > 1 else None
    try:
        job.transcribe(str(slow))
        raise AssertionError("not stopped")
    except comms.Busy:
        pass
    finally:
        comms.BUSY_EVERY_S = 3
    assert time.time() - t0 < 20 and job.proc is None
    assert job.st["recs"]["sleep"]["tries"] == 0 and not comms.comms_path(slow).exists()


def test_comms_downloads_are_checked():
    """Model and component: https only, size and SHA-256 must match the pinned values, zip paths stay inside."""
    import hashlib
    import io
    import zipfile
    import requests
    from pcs_recorder import comms
    dest = TMP / "dl" / "model.bin"
    try:
        comms.fetch("http://huggingface.co/x", dest, "0" * 64, 1)
        raise AssertionError("http accepted")
    except comms.Unavailable:
        pass
    try:
        comms.fetch("https://example.com/x", dest, "0" * 64, 1)
        raise AssertionError("other host accepted")
    except comms.Unavailable:
        pass
    body = b"model bytes"

    class R:
        url, history = "https://cdn.example/x", []
        def raise_for_status(self): pass
        def iter_content(self, n): yield body
    orig = requests.Session.get
    requests.Session.get = lambda self, *a, **k: R()
    try:
        try:
            comms.fetch("https://huggingface.co/x", dest, "0" * 64, len(body))
            raise AssertionError("wrong SHA-256 accepted")
        except comms.Unavailable:
            pass
        assert not dest.exists() and not dest.with_name("model.bin.part").exists()
        comms.fetch("https://huggingface.co/x", dest, hashlib.sha256(body).hexdigest(), len(body))
        assert dest.read_bytes() == body
    finally:
        requests.Session.get = orig
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.txt", "x")
    try:
        comms.safe_extract(zipfile.ZipFile(buf), TMP / "zx")
        raise AssertionError("path outside accepted")
    except comms.Unavailable:
        pass
    assert not (TMP / "evil.txt").exists()
    # no component pinned (an older build): says so, downloads nothing
    if comms.runtime_cmd() is None:
        try:
            getattr(comms, "_real_get_runtime", comms.get_runtime)(component={})
            raise AssertionError("no component, yet it ran")
        except comms.Unavailable as e:
            assert "isn't published" in str(e)
    # the pinned model is the turbo model's five files
    assert [n for n, *_ in comms.MODEL_FILES][-1] == "model.bin" and all(len(h) == 64 for *_, h in comms.MODEL_FILES)
    assert comms.MODEL_URL.startswith("https://huggingface.co/") and comms.MODEL_COMMIT in comms.MODEL_URL


def test_comms_component_pinned():
    """The Transcripts component is pinned to one published zip of this repo: https, a release tag the app updater can
    never take (not v..., a prerelease never "latest"), its SHA-256 and size; downloaded from github.com only."""
    import io
    import json
    import re
    import urllib.request
    from pcs_recorder import comms
    c = comms.COMPONENT
    assert c and set(c) == {"url", "sha256", "size"}
    m = re.fullmatch(r"https://github\.com/BLAKYY5/pcs-scanner/releases/download/([^/]+)/PCS-Transcripts\.zip", c["url"])
    assert m and not m.group(1).startswith("v"), c["url"]          # a v tag would build and publish an app release
    assert re.fullmatch(r"[0-9a-f]{64}", c["sha256"]) and isinstance(c["size"], int) and 10 << 20 < c["size"] < 200 << 20
    assert comms._https_ok(c["url"], ("github.com",))
    # the app's updater reads only the latest release and takes only a v<version> .exe: this release is never offered
    import pcs_core
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    rel = {"tag_name": "transcripts-1", "prerelease": True, "html_url": "https://github.com/x",
           "assets": [{"name": "PCS-Transcripts.zip", "browser_download_url": c["url"], "size": c["size"]}]}
    real = urllib.request.urlopen, dict(pcs_core.BUILD)
    urllib.request.urlopen = lambda req, timeout=None: (calls.append(req.full_url), Resp(json.dumps(rel).encode()))[1]
    try:
        for b in ({"version": "1.11.0"}, {"version": "dev-5", "base": "1.11.0"}, {"version": "1.10.12"}):
            pcs_core.BUILD.clear()
            pcs_core.BUILD.update(b, repo="BLAKYY5/pcs-scanner")
            assert pcs_core.check_update() is None, b
    finally:
        urllib.request.urlopen = real[0]
        pcs_core.BUILD.clear()
        pcs_core.BUILD.update(real[1])
    assert calls and all(u.endswith("/repos/BLAKYY5/pcs-scanner/releases/latest") for u in calls)


def test_comms_first_download_after_yes_with_progress():
    """Said no: nothing is downloaded. Said yes: the first transcript downloads the component zip (github.com) and
    then the model (Hugging Face), and Settings sees how far (bytes done of the total); nothing left half-way."""
    import io
    import shutil
    import zipfile
    from pcs_recorder import comms
    comms_, job, s, idx, rec, sent = _comms_setup("comms-first-dl", consent=False)
    saved = (comms.get_runtime, comms.get_model, comms.fetch, os.environ.pop("PCS_TRANSCRIBE_PY", None),
             os.environ.pop("PCS_WHISPER_MODEL", None))
    comms.get_runtime = getattr(comms, "_real_get_runtime", comms.get_runtime)
    comms.get_model = getattr(comms, "_real_get_model", comms.get_model)
    shutil.rmtree(comms.base_dir(), ignore_errors=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pcs-transcribe/pcs-transcribe.exe", b"MZ fake")
    got, seen = [], []

    def fake_fetch(url, dest, sha256, size, busy=None, hosts=comms.MODEL_HOSTS, progress=None):
        got.append((url, hosts))
        if url == comms.COMPONENT["url"]:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(buf.getvalue())
        if progress:
            progress(size // 2)
            seen.append(job.public()["download"])
            progress(size - size // 2)
    comms.fetch = fake_fetch
    total = comms.COMPONENT["size"] + sum(size for _, size, _ in comms.MODEL_FILES)
    try:
        assert comms.missing_bytes() == total
        # said no: the app's minute tick does nothing, nothing downloaded
        assert job.tick() is False and got == [] and job.public()["download"] is None and not job.public()["on"]
        # said yes: component first (github.com only), then the model's five files (Hugging Face)
        s.transcript_consent = True
        stop = [False]
        job.busy = lambda: "test stop" if stop[0] else None
        real_model = comms.get_model

        def model_then_stop(busy=None, progress=None):
            d = real_model(busy, progress=progress)
            stop[0] = True                                    # stop before the (fake) transcriber would start
            return d
        comms.get_model = model_then_stop
        try:
            job.transcribe(str(rec))
            raise AssertionError("should stop")
        except comms.Busy:
            pass
        assert got[0] == (comms.COMPONENT["url"], ("github.com",))
        assert [u for u, _ in got[1:]] == [comms.MODEL_URL + n for n, *_ in comms.MODEL_FILES]
        assert all(h == comms.MODEL_HOSTS for _, h in got[1:])
        assert seen[0] == {"done": comms.COMPONENT["size"] // 2, "total": total}
        assert seen[-1]["done"] < total and all(a["done"] < b["done"] for a, b in zip(seen, seen[1:]))
        assert comms.runtime_cmd() and job.public()["download"] is None and job.public()["available"]
        assert job.st["recs"][rec.stem].get("tries", 0) == 0            # a stop before the transcriber is no try
    finally:
        comms.get_runtime, comms.get_model, comms.fetch = saved[:3]
        for k, v in (("PCS_TRANSCRIBE_PY", saved[3]), ("PCS_WHISPER_MODEL", saved[4])):
            if v is not None:
                os.environ[k] = v
        shutil.rmtree(comms.base_dir(), ignore_errors=True)
    page = (Path(__file__).resolve().parent.parent / "app" / "web" / "app.js").read_text(encoding="utf-8")
    assert "tr.download" in page and "syncTranscript()" in page
    assert "t('Downloading for transcripts (first time only): {pct} % of {gb} GB.'" in page


def test_transcribe_worker_parts_and_clean_text():
    """The worker cuts long tracks at a quiet moment, fixes Apex words and drops Whisper's filler lines."""
    import numpy as np
    import pcs_transcribe as T
    rate = T.RATE
    pcm = (np.random.default_rng(1).normal(0, 3000, rate * 1500)).astype(np.int16)
    pcm[rate * 590:rate * 591] = 0                     # a quiet second inside the last 30 s of part 1
    ps = T.parts(pcm)
    assert ps[0][0] == 0 and ps[-1][1] == len(pcm) and all(a[1] == b[0] for a, b in zip(ps, ps[1:]))
    assert rate * 590 <= ps[0][1] <= rate * 591, ps[0][1] / rate
    assert len(T.parts(pcm[:rate * 200])) == 1
    assert T.fix_text("  le  Watson est a Northpad ") == "le Wattson est a North Pad"
    assert not T.keep_segment("Sous-titrage ST' 501", -0.2, 0.1, [])
    assert not T.keep_segment("Merci d'avoir regardé cette vidéo", -0.2, 0.1, [])
    assert not T.keep_segment("euh", -1.5, 0.9, [])
    assert not T.keep_segment("full", -0.2, 0.1, ["full", "full"])
    assert T.keep_segment("il est one shot", -0.3, 0.1, ["full"])
    from pcs_recorder import comms
    assert comms.is_comms_file("a.comms.json") and comms.is_comms_file("a.comms.partial.json")
    assert not comms.is_comms_file("a.scan.json")


if __name__ == "__main__":
    sys.exit(run_all([(n, f) for n, f in list(globals().items()) if n.startswith("test_")]))
