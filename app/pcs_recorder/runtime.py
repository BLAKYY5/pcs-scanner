"""Runs the auto-recorder inside PCS Scanner: tray icon, scan prompt, settings window,
first-time setup, start with Windows, and uploads (team hub and/or Google Drive)."""
from __future__ import annotations

import ctypes
import logging
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from . import obs as obsmod
from . import settings as st
from .core import Recorder

log = logging.getLogger("recorder")
PANEL, INK, MUTED, RED = "#1b1a18", "#f1efea", "#a39f96", "#ba1c14"
GUARD_PORT = 4467          # localhost only: one background recorder per PC, and "show window" requests


def screen_size() -> tuple[int, int]:
    try:
        u = ctypes.windll.user32
        u.SetProcessDPIAware()
        return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    except Exception:
        return 2560, 1440


# ------------------------------------------------------------------ single instance

def ask_running_instance_to_show() -> bool:
    """True if a background PCS Scanner is already running (it is asked to show its window)."""
    try:
        with socket.create_connection(("127.0.0.1", GUARD_PORT), timeout=1) as c:
            c.sendall(b"show")
        return True
    except OSError:
        return False


def hold_guard(on_show) -> socket.socket | None:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", GUARD_PORT))
    except OSError:
        return None
    s.listen(2)

    def serve():
        while True:
            try:
                c, _ = s.accept()
                if c.recv(16) == b"show":
                    on_show()
                c.close()
            except OSError:
                return
    threading.Thread(target=serve, daemon=True).start()
    return s


# ------------------------------------------------------------------ start with Windows

def startup_link() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "PCS Scanner.lnk"


def set_start_with_windows(on: bool) -> None:
    lnk = startup_link()
    if not on:
        lnk.unlink(missing_ok=True)
        return
    if getattr(sys, "frozen", False):
        target, args, wd = sys.executable, "--background", str(Path(sys.executable).parent)
    else:
        target = str(Path(sys.executable).with_name("pythonw.exe"))
        script = str(Path(__file__).resolve().parent.parent / "pcs_app.py")
        args, wd = f'"{script}" --background', str(Path(script).parent)
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}'); $s.TargetPath='{target}'; "
          f"$s.Arguments='{args}'; $s.WorkingDirectory='{wd}'; $s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=0x08000000, timeout=30)


# ------------------------------------------------------------------ OBS, hub, uploads

class ObsAdapter:
    def __init__(self, s: st.Settings):
        self.s = s
        self.obs = obsmod.OBS(s)
        self.ready = False

    def ensure_ready(self) -> None:
        if not obsmod.installed():
            raise RuntimeError("OBS isn't set up yet: open PCS Scanner and turn on Auto-record")
        sig = obsmod.config_signature(self.s)
        if obsmod.is_running() and getattr(self, "sig", None) != sig:
            # settings changed, or OBS was left running by an older start: restart it with the current
            # settings (this happens as Apex starts, before recording, and takes a few seconds)
            obsmod.close()
            self.ready = False
        if not obsmod.is_running():
            self.ready = False
            w, h = screen_size()
            obsmod.write_config(self.s, self.s.encoder or "av1_texture_amf", w, h)
            obsmod.launch()
            self.sig = sig
        if not self.ready:
            self.obs.connect()
            w, h = screen_size()
            self.obs.prepare(w, h, self.s.encoder or None)
            self.ready = True

    def start(self):
        self.obs.start()

    def stop(self):
        return self.obs.stop()


class Sender:
    """Results go to the team hub when one is set, and to Google Drive when the player is signed in.
    It succeeds if at least one of them took the files; otherwise the recorder retries later."""

    def __init__(self, s: st.Settings, drive_upload):
        self.s, self.drive_upload = s, drive_upload

    def report(self, data: dict) -> None:
        if not self.s.hub_url:
            return
        import requests
        try:
            requests.post(f"{self.s.hub_url}/api/recorder/sessions", json=data, timeout=5)
        except Exception as e:
            log.info("hub report not sent: %s", e)

    def upload(self, files: list[Path]) -> None:
        if not files:
            return []
        sent, errors, where = False, [], []
        if self.s.hub_url:
            import requests
            handles = [("files", (f.name, open(f, "rb"))) for f in files]
            try:
                r = requests.post(f"{self.s.hub_url}/api/scans", files=handles, data={"uploader": self.s.player}, timeout=300)
                r.raise_for_status()
                sent = True
            except Exception as e:
                errors.append(f"the team hub didn't answer ({e.__class__.__name__})")
            finally:
                for _, (_, h) in handles:
                    h.close()
            if sent:
                where.append("the team hub")
        try:
            if self.drive_upload([str(f) for f in files]):
                sent = True
                where.append("Google Drive")
            elif not self.s.hub_url:
                errors.append("Not signed in with Google: open PCS Scanner and press Sign in with Google")
        except Exception as e:
            errors.append(f"Google Drive upload failed ({e})")
        if not sent:
            raise ConnectionError("; ".join(errors) or "No destination set")
        return where


def scan_process(video: str, out: str) -> subprocess.Popen:
    """Run this same app in headless scan mode, at low priority, without a window."""
    BELOW_NORMAL, NO_WINDOW = 0x00004000, 0x08000000
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--scan", video, "--out", out]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve().parent.parent / "pcs_app.py"), "--scan", video, "--out", out]
    return subprocess.Popen(cmd, creationflags=BELOW_NORMAL | NO_WINDOW if os.name == "nt" else 0)


# ------------------------------------------------------------------ UI bits

class UI:
    """Tray icon + small dialogs. Tk calls are passed to the main thread through a queue."""

    def __init__(self, root: tk.Tk, log_line=None):
        self.root = root
        self.calls: queue.Queue = queue.Queue()
        self.icon = None
        self.log_line = log_line        # the app window's log, when it exists
        self.toast_win = None
        root.after(200, self._pump)

    def _pump(self):
        while not self.calls.empty():
            try:
                self.calls.get()()
            except Exception:
                log.exception("ui call failed")
        self.root.after(200, self._pump)

    def on_main(self, fn):
        self.calls.put(fn)

    def notify(self, title: str, text: str, sticky: bool = False) -> None:
        """Tell the player what's happening: a small window in the corner (Windows notifications are
        often hidden during and after games), plus the app's log. Errors stay until closed."""
        log.info("%s: %s", title, text)
        if self.log_line:
            try:
                self.log_line(f"[Auto-record] {title}: {text}")
            except Exception:
                pass
        self.on_main(lambda: self._toast(title, text, sticky))

    def _toast(self, title: str, text: str, sticky: bool) -> None:
        if self.toast_win is not None:
            try:
                self.toast_win.destroy()
            except tk.TclError:
                pass
        w = tk.Toplevel(self.root)
        self.toast_win = w
        w.overrideredirect(True)
        w.configure(bg=PANEL, highlightthickness=1, highlightbackground=RED if sticky else "#302e2a")
        w.attributes("-topmost", True)
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        w.geometry(f"390x+{sw - 410}+{sh - 240}")
        head = tk.Frame(w, bg=PANEL)
        head.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(head, text=title.upper(), bg=PANEL, fg=RED, font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Button(head, text="✕", command=w.destroy, bg=PANEL, fg=MUTED, relief="flat", bd=0,
                  activebackground=PANEL).pack(side="right")
        tk.Label(w, text=text, bg=PANEL, fg=INK, font=("Segoe UI", 10), wraplength=360, justify="left").pack(
            anchor="w", padx=14, pady=(2, 12))
        if not sticky:
            w.after(12000, lambda: w.winfo_exists() and w.destroy())

    def ask_scan(self, minutes: float, gb: float, auto: bool, delay: int) -> bool:
        done = threading.Event()
        answer = {"scan": auto}

        def build():
            w = tk.Toplevel(self.root)
            w.title("PCS Scanner")
            w.configure(bg=PANEL)
            w.attributes("-topmost", True)
            w.resizable(False, False)
            sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
            w.geometry(f"390x150+{sw - 410}+{sh - 220}")
            tk.Label(w, text="SESSION RECORDED", bg=PANEL, fg=RED, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=16, pady=(14, 0))
            tk.Label(w, text=f"{int(minutes // 60)} h {int(minutes % 60):02d} min · {gb:.1f} GB", bg=PANEL, fg=INK,
                     font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=16)
            msg = tk.Label(w, bg=PANEL, fg=MUTED, font=("Segoe UI", 9))
            msg.pack(anchor="w", padx=16, pady=(2, 8))
            row = tk.Frame(w, bg=PANEL)
            row.pack(anchor="e", padx=16)
            left = [delay]

            def finish(v):
                if done.is_set():
                    return
                answer["scan"] = v
                w.destroy()
                done.set()
            tk.Button(row, text="Skip" if auto else "Not now", command=lambda: finish(False), bg=PANEL, fg=INK,
                      relief="flat", padx=12).pack(side="left", padx=6)
            tk.Button(row, text="Scan now" if auto else "Scan", command=lambda: finish(True), bg=RED, fg="white",
                      relief="flat", padx=12).pack(side="left")
            w.protocol("WM_DELETE_WINDOW", lambda: finish(False))

            def count():
                if done.is_set():
                    return
                if auto:
                    msg.config(text=f"Scanning starts in {left[0]} s, then the results are sent.")
                    if left[0] <= 0:
                        return finish(True)
                    left[0] -= 1
                else:
                    msg.config(text="Scan it now and send the results?")
                w.after(1000, count)
            count()
        self.on_main(build)
        done.wait(timeout=delay + 3600)
        return answer["scan"]


def settings_window(root: tk.Tk, s: st.Settings, usage: tuple[int, int], on_save) -> None:
    w = tk.Toplevel(root)
    w.title("Auto-record settings")
    w.resizable(False, False)
    f = ttk.Frame(w, padding=14)
    f.pack(fill="both")
    used, n = usage
    ttk.Label(f, text=f"{n} recording(s) · {used / 1e9:.1f} GB of {s.max_gb} GB used", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
    r = [1]

    def row(label, widget, hint=""):
        ttk.Label(f, text=label).grid(row=r[0], column=0, sticky="w", pady=3)
        widget.grid(row=r[0], column=1, sticky="we", pady=3)
        if hint:
            ttk.Label(f, text=hint, foreground="#888").grid(row=r[0], column=2, sticky="w", padx=8)
        r[0] += 1

    en = tk.BooleanVar(value=s.enabled)
    row("Auto-record Apex", ttk.Checkbutton(f, variable=en), "records while Apex runs")
    hub = tk.StringVar(value=s.hub_url)
    row("Team hub address", ttk.Entry(f, textvariable=hub, width=34), "from Blakey; empty = Drive only")
    folder = tk.StringVar(value=s.folder)
    fr = ttk.Frame(f)
    ttk.Entry(fr, textvariable=folder, width=28).pack(side="left")
    ttk.Button(fr, text="…", width=3, command=lambda: folder.set(filedialog.askdirectory() or folder.get())).pack(side="left", padx=4)
    row("Recordings folder", fr)
    kd = ttk.Spinbox(f, from_=1, to=st.MAX_KEEP_DAYS, width=6)
    kd.set(s.keep_days)
    row("Keep recordings for (days)", kd, "older ones are deleted")
    mg = ttk.Spinbox(f, from_=st.MIN_MAX_GB, to=4000, increment=10, width=6)
    mg.set(s.max_gb)
    row("Maximum folder size (GB)", mg, "oldest deleted first")
    br = ttk.Spinbox(f, from_=st.MIN_MBPS, to=st.MAX_MBPS, increment=1, width=6)
    br.set(s.bitrate_mbps)
    hint = ttk.Label(f, foreground="#888")
    row("Video quality (Mbit/s)", br)
    hint.grid(row=r[0] - 1, column=2, sticky="w", padx=8)

    def update_hint(*_):
        try:
            mbps = max(st.MIN_MBPS, min(st.MAX_MBPS, int(br.get())))
            gb_h = mbps * 3600 / 8 / 1000 * 1.02
            hint.config(text=f"~{gb_h:.1f} GB per hour · ~{int(mg.get()) / gb_h:.0f} h fit in the folder")
        except (ValueError, ZeroDivisionError):
            hint.config(text="")
    for wdg in (br, mg):
        wdg.config(command=update_hint)
        wdg.bind("<KeyRelease>", update_hint)
    update_hint()
    ga = tk.BooleanVar(value=s.game_audio)
    row("Record game sound", ttk.Checkbutton(f, variable=ga), "never the microphone")
    au = tk.BooleanVar(value=s.auto_scan)
    row("Scan automatically", ttk.Checkbutton(f, variable=au), "after Apex closes, with Skip")
    cap = ttk.Combobox(f, values=["window", "game", "display"], state="readonly", width=10)
    cap.set(s.capture)
    row("Capture method", cap, "change only if the video is black")

    def save():
        s.enabled, s.hub_url, s.folder = en.get(), hub.get(), folder.get()
        s.keep_days, s.max_gb = int(kd.get() or 7), int(mg.get() or 100)
        s.bitrate_mbps = int(br.get() or 12)
        s.game_audio, s.auto_scan, s.capture = ga.get(), au.get(), cap.get()
        st.save(s)
        w.destroy()
        on_save()
    ttk.Button(f, text="Save", command=save).grid(row=r[0], column=1, sticky="e", pady=(10, 0))


# ------------------------------------------------------------------ setup and start

def first_time_setup(s: st.Settings, say) -> None:
    """Private OBS, its settings, encoder test, start with Windows. Runs in a worker thread."""
    if not obsmod.installed():
        obsmod.install(say)
    w, h = screen_size()
    obsmod.write_config(s, s.encoder or "av1_texture_amf", w, h)
    obsmod.launch()
    if not s.encoder:
        say("Testing your graphics card's encoders (a few seconds)…")
        s.encoder = obsmod.pick_encoder(s, w, h, say)
        obsmod.write_config(s, s.encoder, w, h)
    st.save(s)
    set_start_with_windows(True)
    say(f"Auto-record is on. Recordings go to {s.folder_path} (kept {s.keep_days} days, max {s.max_gb} GB).")


class AutoRecorder:
    """Owns the Recorder and its loop; created once per app process."""

    def __init__(self, root: tk.Tk, drive_upload, say=print):
        self.root, self.say = root, say
        self.s = st.load()
        self.ui = UI(root, say)
        self.sender = Sender(self.s, drive_upload)
        self.rec = Recorder(self.s, ObsAdapter(self.s), self.sender, self.ui, scan_process)
        self.thread = None

    def run_loop(self) -> None:
        if self.thread:
            return
        self.rec.cleanup()

        def loop():
            while True:
                if self.s.enabled:
                    try:
                        self.rec.tick()
                    except Exception:
                        log.exception("tick failed")
                time.sleep(3)
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def tray(self, show_window, quit_app) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            log.exception("tray icon unavailable")
            return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((6, 6, 58, 58), fill=(186, 28, 20))
        d.ellipse((24, 24, 40, 40), fill=(255, 255, 255))
        menu = pystray.Menu(
            pystray.MenuItem(lambda _: ("● " if self.s.enabled else "○ ") + self.rec.status, None, enabled=False),
            pystray.MenuItem("Open PCS Scanner", lambda: self.ui.on_main(show_window), default=True),
            pystray.MenuItem("Auto-record settings…", lambda: self.ui.on_main(self.open_settings)),
            pystray.MenuItem("Open recordings folder", lambda: os.startfile(self.s.folder_path) if os.name == "nt" else None),
            pystray.MenuItem("Quit", lambda: (self.ui.icon.stop(), self.ui.on_main(quit_app))),
        )
        self.ui.icon = pystray.Icon("PCS Scanner", img, "PCS Scanner", menu)
        self.ui.icon.run_detached()

    def open_settings(self) -> None:
        was = self.s.enabled

        def saved():
            removed = self.rec.cleanup()
            if removed:
                self.ui.notify("PCS Scanner", f"{len(removed)} old recording(s) removed to stay within your limits.")
            if self.s.enabled and not was:
                self.enable()
            elif was and not self.s.enabled:
                set_start_with_windows(False)
                self.say("Auto-record is off.")
        settings_window(self.root, self.s, self.rec.usage(), saved)

    def enable(self) -> None:
        def go():
            try:
                first_time_setup(self.s, self.say)
                self.run_loop()
            except Exception as e:
                self.s.enabled = False
                st.save(self.s)
                self.say(f"Auto-record setup failed: {e}")
        threading.Thread(target=go, daemon=True).start()
