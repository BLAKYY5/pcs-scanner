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
            raise ConnectionError("hub offline")
        self.uploads.append([f.name for f in files])


class FakeUI:
    def __init__(self, answer=True):
        self.answer, self.asked, self.notes = answer, 0, []

    def ask_scan(self, *a):
        self.asked += 1
        return self.answer

    def notify(self, t, m):
        self.notes.append(m)


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
    for _ in range(30):
        r.tick(); time.sleep(0.5)
        if len(hub.uploads) == 2:
            break
    assert len(hub.uploads) == 2 and all(sorted(u) == ["game1.feed.json", "scan.log", "session.scan.json"] or
                                         sorted(u) == ["game1.feed.json", "session.scan.json"] for u in hub.uploads), hub.uploads
    assert [e.status for e in r.idx.values()] == ["uploaded", "uploaded"], {k: vars(v) for k, v in r.idx.items()}
    assert hub.reports[:3] == ["recording", "recorded", "uploaded"] or "uploaded" in hub.reports


def test_skip_and_offline_hub():
    r, apex, hub, ui = run_session(scan_answer=False)
    assert r.scan_proc is None and list(r.idx.values())[0].status == "skipped"
    r2, apex2, hub2, ui2 = run_session(hub_down=True)
    for _ in range(8):
        r2.tick(); time.sleep(0.5)
    assert list(r2.idx.values())[0].status == "scanned" and not hub2.uploads   # kept for retry
    hub2.down = False; r2.tick()
    assert list(r2.idx.values())[0].status == "uploaded"


def test_settings_limits():
    s = settings.Settings(keep_days=0, max_gb=5, capture="weird").clamp()
    assert (s.keep_days, s.max_gb, s.capture) == (1, settings.MIN_MAX_GB, "window")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
