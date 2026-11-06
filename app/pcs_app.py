"""PCS Scanner app: records Apex, scans recordings, sends the results to the team, and plays them back.

Since 1.9 the window is a web interface (app/web/) shown in a native window (Edge WebView2 through pywebview),
backed by a local server (pcs_server.py). The corner messages and the scan prompt are small Tk windows that run
in their own thread, so they work while the main window is hidden. The app lives in the tray: closing the window
hides it; "Quit" in the tray menu ends it.

Command line:  PCS-Scanner.exe                 the window (+ tray)
               PCS-Scanner.exe --background    tray only (used at Windows start-up)
               PCS-Scanner.exe --scan FILE... [--out DIR]   scan without a window (used for every scan)
               PCS-Scanner.exe --selftest OUT  used by the build to check the .exe

Results (.scan.json + .feed.json) go to the team hub and/or a "PCS Scans" folder in the player's own Drive, shared
with the coach. The app only asks for Google's drive.file permission: it can see and change only the files it
created itself.
"""
import os, sys, threading, traceback, logging, multiprocessing, subprocess, webbrowser

import pcs_core as core
from pcs_core import APP, OUT, BUILD, SETTINGS


def selftest(out):
    """Used by the build: proves the libraries, the scanner and the interface files are inside the exe."""
    try:
        import numpy, scipy.ndimage, PIL.Image, pcs_scan, pcs_hud, tkinter   # noqa: F401
        import psutil, obsws_python, pystray, requests, pcs_recorder.runtime, pcs_server, webview   # noqa: F401
        numpy.zeros((4, 4)).sum(); scipy.ndimage.label(numpy.ones((3, 3)))
        r = tkinter.Tk(); r.withdraw(); r.destroy()
        ff = os.path.join(getattr(sys, '_MEIPASS', ''), 'ffmpeg', 'ffmpeg.exe')
        web = all(os.path.exists(os.path.join(pcs_server.WEB, f)) for f in ('index.html', 'app.css', 'app.js'))
        s = pcs_server.Server(pcs_server.Backend()); s.start()            # the local server answers
        import urllib.request
        with urllib.request.urlopen(f'http://127.0.0.1:{s.port}/api/state?t={s.token}', timeout=5) as resp:
            api = resp.status == 200
        s.stop()
        msg = (f'OK numpy {numpy.__version__} scipy {scipy.__version__} scanner {pcs_scan.VERSION} '
               f'ffmpeg {os.path.exists(ff)} upload {os.path.exists(os.path.join(core.HERE, "client.json"))} '
               f'ui {web and api} autorecord ok')
        if not (web and api):
            msg = 'FAIL interface files or local server missing: ' + msg
    except Exception:
        msg = 'FAIL ' + traceback.format_exc()
    open(out, 'w').write(msg)


def headless_scan(args):
    """Scan without a window (started by the recorder and by the Scan button). Exit 0 = results written."""
    out = OUT
    if '--out' in args:
        i = args.index('--out'); out = args[i + 1]; args = args[:i] + args[i + 2:]
    os.makedirs(out, exist_ok=True)
    sys.stdout = sys.stderr = open(os.path.join(out, 'scan.log'), 'a', encoding='utf-8')
    try:
        res = core.run_scan(args, print, out)
        print(f'done: {len(res)} result file(s)')
        return 0 if res else 2
    except Exception:
        traceback.print_exc()
        return 1


# ------------------------------------------------------------------------------------------- window

def edge_exe():
    for base in (os.environ.get('ProgramFiles(x86)', ''), os.environ.get('ProgramFiles', ''), os.environ.get('LOCALAPPDATA', '')):
        p = os.path.join(base, 'Microsoft', 'Edge', 'Application', 'msedge.exe')
        if base and os.path.exists(p):
            return p
    return None


class Window:
    """The main window. Normally a native window (pywebview + Edge WebView2); if that can't start, the same
    interface opens as an Edge app window (no tabs, no address bar)."""

    def __init__(self, url, hidden):
        self.url, self.hidden = url, hidden
        self.w = None
        self.quitting = False
        self.done = threading.Event()
        self.on_drop = lambda paths: None

    def show(self, page=None):
        if self.w is not None:
            try:
                self.w.show()
                self.w.restore()
                if page:
                    self.w.evaluate_js(f"location.hash = '#{page}'")
                return
            except Exception:
                logging.exception('show failed')
        url = self.url + (f'#{page}' if page else '')
        e = edge_exe()
        if e:
            subprocess.Popen([e, f'--app={url}', f'--user-data-dir={os.path.join(core.DATA, "edge")}',
                              '--window-size=1320,860'])
        else:
            webbrowser.open(url)

    def run(self):
        """Blocks (on the main thread) until the app quits."""
        try:
            import webview
            self.w = webview.create_window(f'{APP} {BUILD.get("version", "")}', self.url, width=1320, height=860,
                                           min_size=(980, 640), hidden=self.hidden, background_color='#121211')
            self.w.events.closing += self._closing
            self.w.events.loaded += self._hook_drop
            webview.start(private_mode=False, storage_path=os.path.join(core.DATA, 'webview'))
            return
        except Exception:
            logging.exception('native window unavailable; using an Edge app window')
            self.w = None
        if not self.hidden:
            self.show()
        self.done.wait()

    def _closing(self):
        if self.quitting:
            return True
        threading.Thread(target=self.w.hide, daemon=True).start()   # closing = back to the tray
        return False

    def _hook_drop(self):
        """Dropped files: the page doesn't get their location, pywebview does."""
        try:
            from webview.dom import DOMEventHandler

            def dropped(e):
                files = (e.get('dataTransfer') or {}).get('files') or []
                paths = [f.get('pywebviewFullPath') for f in files if f.get('pywebviewFullPath')]
                if paths:
                    self.on_drop(paths)
            self.w.dom.document.events.drop += DOMEventHandler(dropped, True, True)
        except Exception:
            logging.exception('drag and drop unavailable')

    def pick_files(self):
        if self.w is not None:
            try:
                import webview
                r = self.w.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                                              file_types=('Recordings (*.mp4;*.mkv;*.mov)', 'All files (*.*)'))
                return list(r or [])
            except Exception:
                logging.exception('file dialog failed')
        return None                                        # caller falls back to the Tk dialog

    def quit(self):
        self.quitting = True
        if self.w is not None:
            try:
                self.w.destroy()
            except Exception:
                pass
        self.done.set()


class TkThread:
    """Tk in its own thread: corner messages, the scan prompt, the fallback file dialog."""

    def __init__(self, setup):
        self.root = None
        ready = threading.Event()

        def run():
            import tkinter as tk
            self.root = tk.Tk()
            self.root.withdraw()
            try:
                setup(self.root)
            except Exception:
                logging.exception('tray/recorder setup failed')
            ready.set()
            self.root.mainloop()
        threading.Thread(target=run, daemon=True, name='tk').start()
        ready.wait(20)

    def call(self, fn, timeout=600):
        """Run fn in the Tk thread and return its result."""
        box, done = [], threading.Event()

        def go():
            try:
                box.append(fn())
            finally:
                done.set()
        self.root.after(0, go)
        done.wait(timeout)
        return box[0] if box else None

    def pick_files(self):
        from tkinter import filedialog
        return list(self.call(lambda: filedialog.askopenfilenames(
            title='Pick Apex recordings', filetypes=[('Recordings', '*.mp4 *.mkv *.mov'), ('All files', '*.*')])) or [])


def main():
    background = '--background' in sys.argv
    try:
        from pcs_recorder import runtime as autorec
    except Exception:
        autorec = None
        logging.exception('auto-record unavailable')
    if autorec and autorec.ask_running_instance_to_show():
        return                                             # already running: it shows its window instead

    import pcs_server
    b = pcs_server.Backend()
    server = pcs_server.Server(b)
    server.start()
    win = Window(server.url, hidden=background)
    b.say(f'{APP} {BUILD.get("version")} started. Scan results are saved in {OUT}')

    def setup(root):
        if not autorec:
            return
        b.auto = autorec.AutoRecorder(root, core.drive_upload_quiet, b.say,
                                      signin=lambda: (win.show(), b.signin()),
                                      youtube_service=lambda: core.youtube_service(interactive=False),
                                      yt_signin=lambda: (win.show('settings'), b.yt_signin()))
        if not b.auto.s.player:
            b.auto.s.player = SETTINGS.get('player', '')
        b.auto.ui.actions["eeclink"] = ("Paste the link", lambda: win.show('recordings'))
        autorec.hold_guard(lambda: win.show())
        b.auto.tray(lambda: win.show(), lambda: b.quit(), open_settings=lambda: win.show('settings'))
        if b.auto.s.enabled:
            b.auto.run_loop()
    tk_thread = TkThread(setup)

    def quit_app():
        try:
            if b.auto and b.auto.ui.icon:
                b.auto.ui.icon.stop()
        except Exception:
            pass
        win.quit()
        try:
            tk_thread.root.after(0, tk_thread.root.quit)
        except Exception:
            pass
        threading.Timer(3, lambda: os._exit(0)).start()   # never leave a hidden process behind

    b.quit = quit_app

    def pick():
        r = win.pick_files()
        return tk_thread.pick_files() if r is None else r
    b.pick_files = pick
    win.on_drop = lambda paths: b.say(f'Added {b.add_paths(paths)} recording(s).')

    if autorec and os.name == 'nt':
        def apply_start_with_windows(on):
            try:
                autorec.set_start_with_windows(on)
            except Exception:
                logging.exception('start with Windows')
        b.set_start_with_windows = lambda on: threading.Thread(target=apply_start_with_windows, args=(on,), daemon=True).start()
        b.set_start_with_windows(b.auto.s.start_with_windows if b.auto else True)   # keeps the shortcut on this exe

    def background_checks():
        b.check_google()
        b.check_youtube()
        u = core.check_update()
        if u:
            b.update = u
            b.say(f'Version {u[0]} is available: press "Update" at the top of the window.')
    threading.Thread(target=background_checks, daemon=True).start()

    if background and not (b.auto and (b.auto.s.enabled or b.auto.s.start_with_windows)):
        quit_app(); return                                 # start-up shortcut left over: nothing to do
    win.run()
    quit_app()


if __name__ == '__main__':
    multiprocessing.freeze_support()   # the scanner splits recordings into parallel chunks
    if '--selftest' in sys.argv:
        selftest(sys.argv[sys.argv.index('--selftest') + 1]); sys.exit(0)
    if '--scan' in sys.argv:
        sys.exit(headless_scan(sys.argv[sys.argv.index('--scan') + 1:]))
    main()
