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
from .core import NeedsSignIn, Recorder

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

    def mute(self, track: str, muted: bool) -> bool:
        return self.obs.mute(track, muted)

    def elapsed(self):
        return self.obs.elapsed()


class Hotkeys:
    """Global hotkeys, registered only while a recording runs (so they never take a key away otherwise).
    Uses Windows RegisterHotKey, the same mechanism as Discord's keybinds: no keyboard hook, nothing
    injected into the game."""

    ACTIONS = (("hk_mute_game", "mute_game"), ("hk_mute_discord", "mute_discord"), ("hk_mute_mic", "mute_mic"),
               ("hk_mark", "mark"))
    SOUNDS = {"mark": (1320, 90), "muted": (440, 140), "unmuted": (880, 90)}

    def __init__(self, s: st.Settings, rec: Recorder):
        self.s, self.rec = s, rec
        self.thread = None

    def start(self) -> None:
        if os.name != "nt" or self.thread:
            return
        self.thread = threading.Thread(target=self._loop, daemon=True, name="hotkeys")
        self.thread.start()

    def _loop(self) -> None:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        active: dict[int, str] = {}          # hotkey id -> action
        signature = None
        while True:
            want = None
            if self.s.enabled and self.rec.current is not None:
                want = tuple((getattr(self.s, k), a) for k, a in self.ACTIONS if getattr(self.s, k))
            if want != signature:
                for i in active:
                    user32.UnregisterHotKey(None, i)
                active = {}
                for n, (combo, action) in enumerate(want or (), 1):
                    parsed = st.parse_hotkey(combo)
                    if parsed and user32.RegisterHotKey(None, n, parsed[0], parsed[1]):
                        active[n] = action
                    elif parsed:
                        log.info("hotkey %s is used by another program", combo)
                signature = want
            while user32.PeekMessageW(ctypes.byref(msg), None, 0x0312, 0x0312, 1):   # WM_HOTKEY
                action = active.get(msg.wParam)
                if action:
                    threading.Thread(target=self._fire, args=(action,), daemon=True).start()
            time.sleep(0.05)

    def _fire(self, action: str) -> None:
        try:
            what = self.rec.hotkey(action)
        except Exception:
            log.exception("hotkey failed")
            return
        if what and self.s.hk_sounds:
            try:
                import winsound
                winsound.Beep(*self.SOUNDS[what])
                if what == "muted":
                    winsound.Beep(330, 140)                   # two low tones = muted
            except Exception:
                pass


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
                raise NeedsSignIn("Your Google sign-in has expired or is missing, so the results couldn't go to "
                                  "Drive")
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

def work_area(root) -> tuple[int, int, int, int]:
    """The screen minus the taskbar (Windows), or the whole screen."""
    if os.name == "nt":
        try:
            from ctypes import wintypes
            r = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0):      # SPI_GETWORKAREA
                return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight() - 60


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

    def notify(self, title: str, text: str, sticky: bool = False, action: str | None = None) -> None:
        """Tell the player what's happening: a small window in the corner (Windows notifications are
        often hidden during and after games), plus the app's log. Errors stay until closed."""
        log.info("%s: %s", title, text)
        if self.log_line:
            try:
                self.log_line(f"[Auto-record] {title}: {text}")
            except Exception:
                pass
        self.on_main(lambda: self._toast(title, text, sticky, action))

    actions: dict = {}          # name -> (button label, callback), e.g. "signin" -> ("Sign in with Google", fn)

    # ------------------------------------------------------------------ corner messages
    TOAST_W = 380
    TOAST_S = 7            # seconds a message stays (10 with a button); hovering it pauses the countdown
    TOAST_ACTION_S = 10
    MAX_TOASTS = 3

    @staticmethod
    def _kind(title: str, sticky: bool) -> str:
        t = title.lower()
        if any(w in t for w in ("failed", "not sent", "didn't", "error", "no hardware")):
            return "error"
        if sticky or any(w in t for w in ("waiting", "turning off", "update available", "eec")):
            return "attention"
        if any(w in t for w in ("sent", "finished", "on youtube", "is on", "stays on")):
            return "done"
        return "info"

    def _toast(self, title: str, text: str, sticky: bool, action: str | None = None) -> None:
        """A card in the app's style, bottom right: accent bar by kind, title, text, optional button, a thin
        bar that runs out, then it fades away. Several messages stack (newest at the bottom, max 3)."""
        accent = {"error": "#e5392d", "attention": "#e6ad45", "done": "#5fd08f", "info": "#6aa0ff"}[self._kind(title, sticky)]
        border, panel, panel2 = "#3a3833", PANEL, "#23221f"
        title_font = ("Bahnschrift SemiCondensed", 12, "bold")
        body_font = ("Segoe UI", 10)

        self.toasts = [t for t in getattr(self, "toasts", []) if t.winfo_exists()]
        while len([t for t in self.toasts if not getattr(t, "_prompt", False)]) >= self.MAX_TOASTS:
            oldest = next(t for t in self.toasts if not getattr(t, "_prompt", False))
            self.toasts.remove(oldest)
            self._close_toast(oldest, fade=False)

        w = tk.Toplevel(self.root)
        self.toast_win = w
        w.overrideredirect(True)
        w.configure(bg=border)
        w.attributes("-topmost", True)
        try:
            w.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        w.withdraw()
        card = tk.Frame(w, bg=panel)
        card.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Frame(card, bg=accent, width=3).pack(side="left", fill="y")
        main = tk.Frame(card, bg=panel)
        main.pack(side="left", fill="both", expand=True)

        head = tk.Frame(main, bg=panel)
        head.pack(fill="x", padx=(12, 8), pady=(10, 0))
        mark = tk.Canvas(head, width=16, height=16, bg=panel, highlightthickness=0)
        mark.create_rectangle(1, 1, 15, 15, fill=RED, outline=RED)
        mark.create_oval(4, 4, 12, 12, outline="white", width=1.6)
        mark.create_oval(7, 7, 9, 9, fill="white", outline="white")
        mark.pack(side="left", padx=(0, 7))
        tk.Label(head, text=title.upper(), bg=panel, fg=INK, font=title_font, anchor="w").pack(side="left")
        close = tk.Label(head, text="✕", bg=panel, fg=MUTED, font=("Segoe UI", 10), cursor="hand2", padx=4)
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: self._close_toast(w))
        close.bind("<Enter>", lambda _e: close.config(fg=INK))
        close.bind("<Leave>", lambda _e: close.config(fg=MUTED))

        body = tk.Label(main, text=text, bg=panel, fg="#d9d6cf", font=body_font, wraplength=self.TOAST_W - 50,
                        justify="left", anchor="w")
        body.pack(fill="x", padx=(12, 12), pady=(4, 10))
        if action in self.actions:
            label, fn = self.actions[action]
            row = tk.Frame(main, bg=panel)
            row.pack(fill="x", padx=12, pady=(0, 10))
            btn = tk.Label(row, text=label.upper(), bg=RED, fg="white", font=("Bahnschrift SemiCondensed", 10, "bold"),
                           padx=12, pady=4, cursor="hand2")
            btn.pack(side="right")
            btn.bind("<Button-1>", lambda _e: (self._close_toast(w, fade=False), fn()))
            btn.bind("<Enter>", lambda _e: btn.config(bg="#e5392d"))
            btn.bind("<Leave>", lambda _e: btn.config(bg=RED))
        else:
            for wid in (body, main, head):
                wid.bind("<Button-1>", lambda _e: self._close_toast(w))      # click the message to close it
        bar = tk.Canvas(main, height=2, bg=panel2, highlightthickness=0)
        bar.pack(fill="x", side="bottom")
        line = bar.create_rectangle(0, 0, self.TOAST_W, 2, fill=accent, outline=accent)

        w.update_idletasks()
        w.geometry(f"{self.TOAST_W}x{w.winfo_reqheight()}+0+0")
        self.toasts.append(w)
        self._layout_toasts()
        w.deiconify()
        self._round_corners(w)
        self._fade(w, 0.0, 0.97)

        total = (self.TOAST_ACTION_S if action in self.actions else self.TOAST_S) * 1000
        state = {"left": total, "hover": False}

        def enter(_e):
            state["hover"] = True

        def leave(_e):
            # only when the pointer really left the card (not just moved onto a child widget)
            x, y = w.winfo_pointerx() - w.winfo_rootx(), w.winfo_pointery() - w.winfo_rooty()
            if not (0 <= x < w.winfo_width() and 0 <= y < w.winfo_height()):
                state["hover"] = False
        w.bind("<Enter>", enter)
        w.bind("<Leave>", leave)

        def tick():
            if not w.winfo_exists():
                return
            if not state["hover"]:
                state["left"] -= 50
            frac = max(0.0, state["left"] / total)
            bar.coords(line, 0, 0, bar.winfo_width() * frac, 2)
            if state["left"] <= 0:
                return self._close_toast(w)
            w.after(50, tick)
        w.after(50, tick)

    def _close_toast(self, w, fade: bool = True) -> None:
        if not w.winfo_exists() or getattr(w, "_closing", False):
            return
        w._closing = True

        def gone():
            try:
                w.destroy()
            except tk.TclError:
                pass
            self.toasts = [t for t in getattr(self, "toasts", []) if t is not w and t.winfo_exists()]
            self._layout_toasts()
        if fade:
            self._fade(w, 0.97, 0.0, done=gone)
        else:
            gone()

    def _fade(self, w, a: float, b: float, done=None, steps: int = 8) -> None:
        def step(i):
            if not w.winfo_exists():
                return
            try:
                w.attributes("-alpha", a + (b - a) * i / steps)
            except tk.TclError:
                pass
            if i < steps:
                w.after(18, step, i + 1)
            elif done:
                done()
        step(1)

    def _layout_toasts(self) -> None:
        """Bottom right of the work area (above the taskbar), newest at the bottom."""
        left, top, right, bottom = work_area(self.root)
        y = bottom - 16
        for t in reversed([t for t in getattr(self, "toasts", []) if t.winfo_exists()]):
            h = t.winfo_reqheight()
            y -= h
            t.geometry(f"{self.TOAST_W}x{h}+{right - self.TOAST_W - 16}+{y}")
            y -= 10

    @staticmethod
    def _round_corners(w) -> None:
        """Windows 11 rounds the corners like its own pop-ups (no effect elsewhere)."""
        if os.name != "nt":
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(w.winfo_id()) or w.winfo_id()
            pref = ctypes.c_int(2)                              # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        except Exception:
            pass

    def ask_scan(self, minutes: float, gb: float, auto: bool, delay: int) -> bool:
        done = threading.Event()
        answer = {"scan": auto}

        def build():
            panel = PANEL
            w = tk.Toplevel(self.root)
            w.title("PCS Scanner")
            w.overrideredirect(True)
            w.configure(bg="#3a3833")
            w.attributes("-topmost", True)
            card = tk.Frame(w, bg=panel)
            card.pack(fill="both", expand=True, padx=1, pady=1)
            tk.Frame(card, bg=RED, width=3).pack(side="left", fill="y")
            main = tk.Frame(card, bg=panel)
            main.pack(side="left", fill="both", expand=True)
            head = tk.Frame(main, bg=panel)
            head.pack(fill="x", padx=12, pady=(10, 0))
            mark = tk.Canvas(head, width=16, height=16, bg=panel, highlightthickness=0)
            mark.create_rectangle(1, 1, 15, 15, fill=RED, outline=RED)
            mark.create_oval(4, 4, 12, 12, outline="white", width=1.6)
            mark.create_oval(7, 7, 9, 9, fill="white", outline="white")
            mark.pack(side="left", padx=(0, 7))
            tk.Label(head, text="SESSION RECORDED", bg=panel, fg=INK,
                     font=("Bahnschrift SemiCondensed", 12, "bold")).pack(side="left")
            tk.Label(main, text=f"{int(minutes // 60)} h {int(minutes % 60):02d} min  ·  {gb:.1f} GB", bg=panel, fg=INK,
                     font=("Bahnschrift SemiCondensed", 18, "bold")).pack(anchor="w", padx=12, pady=(2, 0))
            msg = tk.Label(main, bg=panel, fg="#d9d6cf", font=("Segoe UI", 10), anchor="w", justify="left")
            msg.pack(fill="x", padx=12, pady=(0, 10))
            row = tk.Frame(main, bg=panel)
            row.pack(fill="x", padx=12, pady=(0, 12))
            left = [delay]

            def finish(v):
                if done.is_set():
                    return
                answer["scan"] = v
                w.destroy()
                self.toasts = [t for t in getattr(self, "toasts", []) if t is not w and t.winfo_exists()]
                self._layout_toasts()
                done.set()

            def button(text, cmd, primary):
                bg = RED if primary else "#2b2a26"
                b = tk.Label(row, text=text.upper(), bg=bg, fg="white" if primary else INK, padx=14, pady=5,
                             font=("Bahnschrift SemiCondensed", 10, "bold"), cursor="hand2")
                b.bind("<Button-1>", lambda _e: cmd())
                b.bind("<Enter>", lambda _e: b.config(bg="#e5392d" if primary else "#3a3833"))
                b.bind("<Leave>", lambda _e: b.config(bg=bg))
                return b
            button("Scan now" if auto else "Scan", lambda: finish(True), True).pack(side="right")
            button("Skip" if auto else "Not now", lambda: finish(False), False).pack(side="right", padx=(0, 8))
            bar = tk.Canvas(main, height=2, bg="#23221f", highlightthickness=0)
            bar.pack(fill="x", side="bottom")
            line = bar.create_rectangle(0, 0, 400, 2, fill=RED, outline=RED)
            w.update_idletasks()
            w._prompt = True                                  # stacks with the messages, never trimmed
            self.toasts = [t for t in getattr(self, "toasts", []) if t.winfo_exists()] + [w]
            self._layout_toasts()
            self._round_corners(w)

            def count():
                if done.is_set():
                    return
                if auto:
                    msg.config(text=f"The scan starts in {left[0]} s, then the results are sent.")
                    bar.coords(line, 0, 0, bar.winfo_width() * max(0, left[0]) / max(1, delay), 2)
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
    if not s.encoder:
        say("Testing your graphics card's encoders (a few seconds)…")
        s.encoder = obsmod.pick_encoder(s, w, h, say)       # leaves OBS running with that encoder
    else:
        obsmod.write_config(s, s.encoder, w, h)
        obsmod.launch()
    st.save(s)
    set_start_with_windows(s.start_with_windows)
    say(f"Auto-record is on. Recordings go to {s.folder_path} (kept {s.keep_days} days, max {s.max_gb} GB).")


class AutoRecorder:
    """Owns the Recorder and its loop; created once per app process."""

    def __init__(self, root: tk.Tk, drive_upload, say=print, signin=None, youtube_service=None, yt_signin=None):
        self.root, self.say = root, say
        self.s = st.load()
        self.ui = UI(root, say)
        if signin:
            self.ui.actions["signin"] = ("Sign in with Google", signin)
        self.sender = Sender(self.s, drive_upload)
        self.rec = Recorder(self.s, ObsAdapter(self.s), self.sender, self.ui, scan_process)
        self.thread = None
        self.hotkeys = Hotkeys(self.s, self.rec)
        if youtube_service:
            from .youtube import Uploader
            from .core import apex_running
            from .night import Night
            self.rec.youtube = Uploader(self.s, self.rec, self.ui, youtube_service, apex_running)
            self.rec.night = self.rec.youtube.night = Night(self.s, self.rec, self.ui)
            self.ui.actions["cancelshutdown"] = ("Cancel", lambda: self.rec.night.cancel("Cancelled: the PC stays on."))
        if yt_signin:
            self.ui.actions["ytsignin"] = ("Sign in to YouTube", yt_signin)

    def run_loop(self) -> None:
        if self.thread:
            return
        self.rec.cleanup()
        self.hotkeys.start()

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

    def tray(self, show_window, quit_app, open_settings=None) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            log.exception("tray icon unavailable")
            return
        icon = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "assets" / "pcs.png"
        try:
            img = Image.open(icon)                       # the app's icon (same as the window's logo)
        except OSError:
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse((6, 6, 58, 58), fill=(186, 28, 20))
            d.ellipse((24, 24, 40, 40), fill=(255, 255, 255))
        menu = pystray.Menu(
            pystray.MenuItem(lambda _: ("● " if self.s.enabled else "○ ") + self.rec.status, None, enabled=False),
            pystray.MenuItem("Open PCS Scanner", lambda: self.ui.on_main(show_window), default=True),
            pystray.MenuItem("Settings…", lambda: self.ui.on_main(open_settings or self.open_settings)),
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
                self.say("Auto-record is off.")
        settings_window(self.root, self.s, self.rec.usage(), saved)

    def enable(self) -> None:
        if getattr(self, "_setting_up", False):       # pressed twice: one setup at a time (two would fight)
            return
        self._setting_up = True

        def go():
            try:
                first_time_setup(self.s, self.say)
                self.run_loop()
            except Exception as e:
                self.s.enabled = False
                st.save(self.s)
                self.say(f"Auto-record setup failed: {e}")
            finally:
                self._setting_up = False
        threading.Thread(target=go, daemon=True).start()
