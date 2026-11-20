"""The release file PCS-Scanner.exe (see pcs_install.py): installs the app folder it carries and starts it.

  PCS-Scanner.exe [args]                          install if needed, start the app with args
  PCS-Scanner.exe --finish-update CUR PID [--show]  (every updater since 1.10.2) wait for PID to close, install, start
  PCS-Scanner.exe --selftest OUT                  install, run the installed app's self-test, wait for it

Standard library only. The app folder is under `payload/` inside this exe, the launcher in it as `launcher.exe`."""
import json
import os
import sys
import time

import pcs_install

HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(HERE, "payload")
LAUNCHER = os.path.join(PAYLOAD, "launcher.exe")


def version() -> str:
    try:
        with open(os.path.join(PAYLOAD, "_internal", "build.json"), encoding="utf-8-sig") as f:
            return str(json.load(f)["version"])
    except (OSError, ValueError, KeyError):
        return "unknown"


def log(msg: str) -> None:
    try:
        d = os.environ.get("PCS_DATA_DIR") or os.path.join(os.environ.get("LOCALAPPDATA", ""), "PCS Scanner")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "update.log"), "a", encoding="utf-8") as f:
            f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} installer: {msg}\n')
    except OSError:
        pass


def wait_pid(pid: str, timeout: float = 60) -> None:
    """Wait for the old app to close (no psutil here: OpenProcess + WaitForSingleObject)."""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, int(pid))         # SYNCHRONIZE
        if h:
            k.WaitForSingleObject(h, int(timeout * 1000))
            k.CloseHandle(h)
    except Exception as e:
        log(f"wait: {e}")
        time.sleep(8)


def main(argv: list[str]) -> int:
    args = list(argv)
    launcher = sys.executable                                 # the player's own file: this one, when run by hand
    show = False
    if "--finish-update" in args:
        i = args.index("--finish-update")
        launcher, pid = args[i + 1], args[i + 2]
        show = "--show" in args
        log(f"update to {version()}: waiting for {pid}")
        wait_pid(pid)
        args = [] if show else ["--background", "--updated"]   # as pcs_core.relaunch_args
    ver = version()
    try:
        cur = pcs_install.current()
        if not cur or cur.get("version") != ver:
            t = time.time()
            pcs_install.install(PAYLOAD, ver)
            log(f"installed {ver} in {time.time() - t:.1f} s")
    except OSError as e:
        log(f"install failed: {e}")
        if launcher != sys.executable and os.path.isfile(launcher):    # an update: never leave the player without an app
            log("starting the old version again")
            import subprocess
            subprocess.Popen([launcher], creationflags=0x00000008 | 0x00000200, close_fds=True)
            return 1
        pcs_install.message(f"PCS Scanner couldn't be installed on this PC: {e}")
        return 1
    if launcher != sys.executable:      # an update: the old exe is closed, put the launcher there
        ok = pcs_install.adopt_launcher(launcher, LAUNCHER, wait_s=60)
        log(f"launcher {'placed' if ok else 'not placed (the app tries again)'} at {launcher}")
    wait = "--selftest" in args or "--scan" in args
    try:
        code = pcs_install.run_app(args, launcher, wait=wait)
    except OSError as e:
        log(f"start failed: {e}")
        pcs_install.message(f"PCS Scanner couldn't start: {e}")
        return 1
    return code or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
