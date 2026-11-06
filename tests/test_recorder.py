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
    assert settings.Settings(hk_mark="shift+x").clamp().hk_mark == ""


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
        assert ww >= 390 and wh >= 60, (ww, wh)                        # a real window, not an empty box
        assert x > sw / 2 and y > sh / 2, (x, y, sw, sh)                # bottom-right corner, not top-left
        assert x + ww <= sw and y + wh <= sh, (x, y, ww, wh, sw, sh)    # fully on screen
        texts = [c.cget("text") for c in w.winfo_children() if isinstance(c, (tk.Label, tk.Button))]
        assert any("expired" in t for t in texts), texts
        assert (action is None) or any(t == "Sign in with Google" for t in texts), texts
    root.destroy()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
