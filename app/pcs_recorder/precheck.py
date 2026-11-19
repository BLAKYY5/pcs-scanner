"""Recording check: green / red items the player sees before the first game (Home and the setup card).

- Recorder: our private OBS starts and answers on its remote control (obs-websocket).
- Encoder: the encoder test's result is reused (never run again here, OBS is not restarted for it): tested, same
  graphics card, last session recorded.
- Game sound (track 1): Apex's sound can only be heard once Apex runs. Before: "checked when Apex starts"; then it
  is confirmed in the first minutes of the recording, from OBS's own level meters.
- Voice app (track 2) and microphone (track 3): OBS's level meters for our own sources, a few seconds.
- Disk space on the recordings drive against one session at the chosen bitrate.
- Other capture programs (Medal, Outplayed, Overwolf…) that are known to stop OBS's recording from starting.

Only process names, our own OBS and Windows (registry values, disk space) are read. Nothing touches the running game.

When Apex starts, the quick items are checked again and one corner message lists what is red (once per Apex session).
If the recording then fails, the crash report to the hub carries a one-line summary of the check (`summary()`):
item names, states and the names of the capture programs found, nothing personal.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time

from . import obs as obsmod

log = logging.getLogger("recorder.check")

SESSION_H = 4                  # a session's length for the disk space check (a scrim block is about 3 h)
SIGNAL = 1e-5                  # any sound at all (-100 dBFS); a source that gets nothing gives exact zeros
LISTEN_S = 6                   # "Check again": seconds the meters are listened to
CONFIRM_AFTER_S = 15           # after the recording starts, wait this long, then listen for Apex's sound…
CONFIRM_MAX_S = 180            # …for at most this long
ORDER = ("obs", "encoder", "game", "voice", "mic", "disk", "apps")
LABELS = {"obs": "Recorder", "encoder": "Encoder", "game": "Game sound", "voice": "Voice app", "mic": "Microphone",
          "disk": "Disk space", "apps": "Other capture apps"}

# Programs known to make OBS's recording fail to start while they run (they hold the game capture or the encoder).
BLOCKERS = {"medal.exe": "Medal", "medalencoder.exe": "Medal", "outplayed.exe": "Outplayed", "overwolf.exe": "Overwolf",
            "insights capture.exe": "Insights Capture", "streamlabs obs.exe": "Streamlabs",
            "streamlabs desktop.exe": "Streamlabs", "xsplit.core.exe": "XSplit", "bdcam.exe": "Bandicam",
            "action.exe": "Action!", "twitchstudio.exe": "Twitch Studio"}
# Programs that can, on some PCs (the coach's PC records fine with AMD Instant Replay on): shown in amber only.
MAYBE = {"nvidia share.exe": "NVIDIA overlay", "obs64.exe": "another OBS", "obs32.exe": "another OBS"}


def item(id_: str, state: str, text: str, fix: str = "") -> dict:
    """state: ok (green) | bad (red) | warn (amber: can be fine) | wait (not checked yet) | off (not recorded)."""
    return {"id": id_, "label": LABELS[id_], "state": state, "text": text, "fix": fix}


# ------------------------------------------------------------------ what Windows says (read only)

def processes() -> list[tuple[str, str]]:
    """(name, exe path) of every running program. Names only are used, plus the path to tell our own OBS apart."""
    import psutil
    out = []
    for p in psutil.process_iter(["name", "exe"]):
        try:
            out.append((p.info.get("name") or "", p.info.get("exe") or ""))
        except Exception:
            pass
    return out


def _reg(root: str, path: str, name: str):
    try:
        import winreg
        with winreg.OpenKey(getattr(winreg, root), path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except (ImportError, OSError):
        return None


def amd_instant_replay() -> bool:
    """AMD Software's Instant Replay (ReLive) is on."""
    return _reg("HKEY_CURRENT_USER", r"Software\AMD\DVR", "DvrEnabled") == 1 and \
        _reg("HKEY_CURRENT_USER", r"Software\AMD\DVR", "InstantReplayEnabled") == 1


def gamebar_background() -> bool:
    """Windows' "Record what happened" (Xbox Game Bar background recording) is on."""
    return _reg("HKEY_CURRENT_USER", r"Software\Microsoft\Windows\CurrentVersion\GameDVR",
                "HistoricalCaptureEnabled") == 1


def mic_blocked() -> bool:
    """Windows' privacy setting keeps desktop programs (like OBS) from using the microphone."""
    base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
    return any(str(v).lower() == "deny" for v in (_reg("HKEY_CURRENT_USER", base, "Value"),
                                                   _reg("HKEY_LOCAL_MACHINE", base, "Value"),
                                                   _reg("HKEY_CURRENT_USER", base + r"\NonPackaged", "Value")))


def listen_levels(s, names: dict[str, str], seconds: float, stop=lambda: False, enough=lambda peaks: False) -> dict:
    """OBS's level meters for our own sources: {track: loudest level 0..1, or None when OBS never showed that
    source}. names: OBS input name -> track. Own connection to OBS, only meter events; stops early on stop() or
    enough(peaks)."""
    import obsws_python as obsws
    peaks: dict[str, float] = {t: 0.0 for t in set(names.values())}
    seen: set[str] = set()

    def on_input_volume_meters(data):
        inputs = getattr(data, "inputs", None)
        if inputs is None and isinstance(data, dict):
            inputs = data.get("inputs")
        for i in inputs or []:
            t = names.get(i.get("inputName"))
            if t is None:
                continue
            seen.add(t)
            v = max((float(x) for ch in i.get("inputLevelsMul") or [] for x in ch), default=0.0)
            peaks[t] = max(peaks[t], v)

    c = obsws.EventClient(host="127.0.0.1", port=s.obs_port, password=s.obs_password, timeout=5,
                          subs=obsws.Subs.INPUTVOLUMEMETERS)
    try:
        c.callback.register(on_input_volume_meters)
        end = time.time() + seconds
        while time.time() < end and not stop() and not enough({t: peaks[t] for t in seen}):
            time.sleep(0.25)
    finally:
        try:
            c.disconnect()
        except Exception:
            pass
    return {t: (peaks[t] if t in seen else None) for t in peaks}


def input_names(s) -> dict[str, str]:
    """Our OBS sources per track: input name -> track key."""
    out = {}
    for t in s.tracks():
        if t == "discord":
            for n, _ in obsmod.voice_sources(s):
                out[n] = t
        else:
            out[obsmod.TRACK_INPUTS[t]] = t
    return out


# ------------------------------------------------------------------ the items

def encoder_label(enc: str) -> str:
    e = (enc or "").lower()
    kind = "AV1" if "av1" in e else "HEVC" if ("hevc" in e or "h265" in e) else "H.264"
    card = "AMD" if "amf" in e else "NVIDIA" if "nvenc" in e else "Intel" if "qsv" in e else ""
    return f"{kind} on your {card} card" if card else kind


def obs_item(installed: bool, ok: bool, error: str = "", recording: bool = False) -> dict:
    if not installed:
        return item("obs", "bad", "Not set up yet", "Turn on Auto-record on Home.")
    if recording:
        return item("obs", "ok", "Recording now")
    if ok:
        return item("obs", "ok", "Starts and answers")
    why = (error or "No answer").split(":")[0][:80]
    return item("obs", "bad", f"Didn't start: {why}",
                "Press Check again. Still red: Settings → Test encoder again, then send the Recorder logs to your coach.")


def encoder_item(s, gpu_names: str | None, setting_up: bool = False, last_failed: bool = False) -> dict:
    if setting_up:
        return item("encoder", "wait", "Testing your graphics card now…")
    fix = "Settings → Test encoder again (Apex closed)."
    if not s.encoder:
        return item("encoder", "bad", "Not tested yet", fix)
    if gpu_names and s.encoder not in obsmod.encoder_candidates(obsmod.gpu_vendor(gpu_names), gpu_names):
        return item("encoder", "bad", "Graphics card changed since the test", fix)
    if last_failed:
        return item("encoder", "warn", "Tested, but your last session wasn't recorded",
                    "Close other capture apps. Still failing: " + fix)
    return item("encoder", "ok", encoder_label(s.encoder) + " (tested)")


def game_item(s, apex: bool, peak: float | None = None, done: bool = False) -> dict:
    """peak: Apex's loudest level so far in this recording (None = not listened yet); done = listening is over."""
    if "game" not in s.tracks():
        return item("game", "off", "Off in Settings")
    if not apex:
        return item("game", "wait", "Checked when Apex starts")
    if peak is not None and peak > SIGNAL:
        return item("game", "ok", "Apex sound is recorded")
    if done:
        return item("game", "bad", "No Apex sound in the recording",
                    "Check that Apex isn't muted in the Windows volume mixer. Your coach gets a report by itself.")
    return item("game", "wait", "Checking in the first minutes of the recording…")


def voice_item(s, procs: set[str], peak: float | None = None) -> dict:
    if "discord" not in s.tracks():
        return item("voice", "off", "Off in Settings" if s.comms_consent else "Off (team consent not given)")
    name = s.voice_name()
    it = item("voice", "ok", "")
    it["label"] = name
    if not any(e.lower() in procs for e in s.voice_exes()):
        it.update(state="bad", text=f"{name} isn't open", fix=f"Open {name} before the game.")
    elif peak is None:
        it.update(state="wait", text=f"{name} is open")
    elif peak > SIGNAL:
        it.update(text=f"{name} sound gets through")
    else:
        it.update(state="wait", text="Open, no sound yet (fine if nobody talks)")
    return it


def mic_item(s, mics: list[dict] | None, blocked: bool, peak: float | None = None) -> dict:
    if "mic" not in s.tracks():
        return item("mic", "off", "Off in Settings" if s.comms_consent else "Off (team consent not given)")
    if blocked:
        return item("mic", "bad", "Windows blocks the microphone",
                    "Windows Settings → Privacy & security → Microphone: let desktop apps use it.")
    if mics is not None:
        if not mics:
            return item("mic", "bad", "No microphone found", "Plug in your mic, then press Check again.")
        if s.mic_device and s.mic_device != "default" and s.mic_device not in {m["id"] for m in mics}:
            return item("mic", "bad", "Your mic isn't plugged in", "Plug it in, or pick another in Settings → Microphone.")
    if peak is None:
        return item("mic", "wait", "Found")
    if peak > SIGNAL:
        return item("mic", "ok", "Gets sound")
    return item("mic", "warn", "No sound from your mic", "Check it isn't muted, say something, then press Check again.")


def disk_item(s, usage=shutil.disk_usage) -> dict:
    fix = "Free some space on that drive, or pick another drive in Settings → Recordings folder."
    try:
        free = usage(str(s.folder_path)).free / 1e9
    except OSError:
        return item("disk", "bad", "Recordings folder can't be reached",
                    "Settings → Recordings folder: pick a folder on a drive that is plugged in.")
    need = s.gb_per_hour * SESSION_H
    if free < need:
        return item("disk", "bad", f"Only {free:.0f} GB free · a {SESSION_H} h session needs ~{need:.0f} GB", fix)
    return item("disk", "ok", f"{free:.0f} GB free · a {SESSION_H} h session needs ~{need:.0f} GB")


def apps_item(procs: list[tuple[str, str]], amd_replay: bool = False, gamebar_bg: bool = False) -> dict:
    ours = tuple({os.path.normcase(str(obsmod.OBS_ROOT)), os.path.normcase(os.path.realpath(obsmod.OBS_ROOT))})
    bad, maybe = [], []
    for name, exe in procs:
        n = (name or "").lower()
        if n in BLOCKERS and BLOCKERS[n] not in bad:
            bad.append(BLOCKERS[n])
        elif n in MAYBE and not (exe and os.path.normcase(exe).startswith(ours)) and MAYBE[n] not in maybe:
            maybe.append(MAYBE[n])
    if amd_replay:
        maybe.append("AMD Instant Replay")
    if gamebar_bg:
        maybe.append("Windows background recording")
    if bad:
        return item("apps", "bad", ", ".join(bad) + (" is" if len(bad) == 1 else " are") + " running: can stop the recording",
                    "Close " + (" and ".join(bad)) + " (also in the tray), or turn off its game recording, before Apex.")
    if maybe:
        fixes = {"NVIDIA overlay": "NVIDIA: Alt+Z → Instant Replay off",
                 "another OBS": "close your other OBS or stop its recording / replay buffer",
                 "AMD Instant Replay": "AMD Software → Record & Stream → Instant Replay off",
                 "Windows background recording": "Windows Settings → Gaming → Captures → Record what happened off"}
        return item("apps", "warn", ", ".join(maybe) + ": fine on most PCs",
                    "If recording fails: " + "; ".join(fixes[m] for m in maybe) + ".")
    return item("apps", "ok", "None running")


def summary(items: dict) -> str:
    """One short line for a crash report: item states and the capture programs found. Nothing personal."""
    parts = []
    for k in ORDER:
        it = items.get(k)
        if not it:
            continue
        extra = ""
        if k == "apps" and it["state"] in ("bad", "warn"):
            extra = "(" + it["text"].split(":")[0].replace(" is running", "").replace(" are running", "") + ")"
        elif k == "obs" and it["state"] == "bad":
            extra = "(" + it["text"][:60] + ")"
        parts.append(f"{k}:{it['state']}{extra}")
    return " ".join(parts)[:300]


# ------------------------------------------------------------------ the check

class RecCheck:
    """Holds the last result. run() = the full check ("Check again", app start); apex_started() / record_failed()
    are called by the recorder. Every probe can be replaced (tests)."""

    def __init__(self, s, adapter=None, rec=None, ui=None, setting_up=lambda: False):
        self.s, self.adapter, self.rec, self.ui = s, adapter, rec, ui
        self.setting_up = setting_up
        self.lock = threading.Lock()
        self.items: dict[str, dict] = {}
        self.at: float | None = None
        self.running = False
        self.toasted = False                    # one corner message per Apex session
        self.game_reported = False
        self.session = 0                        # counts Apex sessions (a confirm thread stops when it changes)
        # probes
        self.procs = processes
        self.disk = shutil.disk_usage
        self.mics = obsmod.mic_devices
        self.mic_blocked = mic_blocked
        self.amd_replay = amd_instant_replay
        self.gamebar = gamebar_background
        self.installed = obsmod.installed
        self.listen = listen_levels
        self.gpu: str | None = None             # graphics card names, read once (PowerShell, about a second)
        self.read_gpu = obsmod.gpu_names
        self.confirm_after, self.confirm_max, self.listen_s = CONFIRM_AFTER_S, CONFIRM_MAX_S, LISTEN_S
        self.threaded = True                    # tests: run in the calling thread

    # -- shared bits
    def _apex(self) -> bool:
        r = self.rec
        return bool(r is not None and getattr(r, "apex", False))

    def _recording(self) -> bool:
        r = self.rec
        return bool(r is not None and getattr(r, "current", None) is not None)

    def _last_failed(self) -> bool:
        idx = getattr(self.rec, "idx", None) or {}
        done = [e for e in idx.values() if getattr(e, "status", "") not in ("recording",)]
        if not done:
            return False
        return max(done, key=lambda e: e.started).status == "failed"

    def _quick(self, apex: bool) -> dict:
        """Everything that needs neither OBS nor listening (fast: process list, registry, disk)."""
        try:
            procs = self.procs()
        except Exception:
            procs = []
        names = {n.lower() for n, _ in procs}
        try:
            mics = self.mics() if (os.name == "nt" or self.mics is not obsmod.mic_devices) else None
        except Exception:
            mics = None
        prev = self.items
        out = {
            "encoder": encoder_item(self.s, self.gpu, self.setting_up(), self._last_failed()),
            "game": game_item(self.s, apex) if not (apex and prev.get("game", {}).get("state") in ("ok", "bad"))
            else prev["game"],
            "voice": voice_item(self.s, names),
            "mic": mic_item(self.s, mics, self._safe(self.mic_blocked)),
            "disk": disk_item(self.s, self.disk),
            "apps": apps_item(procs, self._safe(self.amd_replay), self._safe(self.gamebar)),
        }
        # a level already heard keeps its green (voice / mic) until the next full check
        for k in ("voice", "mic"):
            if prev.get(k, {}).get("state") == "ok" and out[k]["state"] == "wait":
                out[k] = prev[k]
        self._names = names
        return out

    @staticmethod
    def _safe(fn) -> bool:
        try:
            return bool(fn())
        except Exception:
            return False

    def _set(self, items: dict) -> None:
        with self.lock:
            self.items = {**self.items, **items}
            self.at = time.time()

    def public(self) -> dict:
        with self.lock:
            items = [self.items[k] for k in ORDER if k in self.items]
            return {"running": self.running, "at": self.at, "items": items,
                    "red": sum(1 for i in items if i["state"] == "bad")}

    def summary(self) -> str:
        with self.lock:
            return summary(self.items)

    def reds(self) -> list[dict]:
        with self.lock:
            return [self.items[k] for k in ORDER if k in self.items and self.items[k]["state"] == "bad"]

    def _go(self, fn, *a) -> None:
        if self.threaded:
            threading.Thread(target=fn, args=a, daemon=True, name="rec-check").start()
        else:
            fn(*a)

    # -- the full check
    def start(self) -> bool:
        """Run the full check in the background. False when one runs already."""
        with self.lock:
            if self.running:
                return False
            self.running = True
        self._go(self._run)
        return True

    def _run(self) -> None:
        try:
            if self.gpu is None:
                try:
                    self.gpu = self.read_gpu() or ""
                except Exception:
                    self.gpu = ""
            apex = self._apex()
            items = self._quick(apex)
            items["obs"] = self._obs()
            self._set(items)
            if items["obs"]["state"] == "ok":
                names = input_names(self.s)
                listen = {n: t for n, t in names.items() if t in ("discord", "mic")}
                if listen:
                    try:
                        peaks = self.listen(self.s, listen, self.listen_s)
                    except Exception as e:
                        log.info("level meters not read: %s", e)
                        peaks = {}
                    self._set(self._levels(peaks, apex))
            log.info("recording check: %s", self.summary())
        except Exception:
            log.exception("recording check failed")
        finally:
            with self.lock:
                self.running = False
                self.at = time.time()

    def _levels(self, peaks: dict, apex: bool, done: bool = False) -> dict:
        out = {}
        names = getattr(self, "_names", set())
        if "discord" in peaks:
            out["voice"] = voice_item(self.s, names, peaks["discord"])
        if "mic" in peaks:
            out["mic"] = mic_item(self.s, None, False, peaks["mic"])
            if self.items.get("mic", {}).get("state") == "bad":       # blocked / unplugged stays red
                out.pop("mic")
        if "game" in peaks:
            out["game"] = game_item(self.s, apex, peaks["game"], done)
        return out

    def _obs(self) -> dict:
        """Our OBS starts and answers. While a recording runs, it is only asked (nothing restarted)."""
        if not self.installed():
            return obs_item(False, False)
        if self._recording():
            return obs_item(True, True, recording=True)
        if self.adapter is None or self.setting_up():
            return item("obs", "wait", "Set up running…") if self.setting_up() else obs_item(True, False, "no recorder")
        lock = getattr(self.rec, "lock", None)
        got = lock.acquire(timeout=60) if lock is not None else True
        if not got:
            return item("obs", "wait", "Busy, press Check again")
        try:
            if self._recording():
                return obs_item(True, True, recording=True)
            self.adapter.ensure_ready()            # starts OBS if it isn't running (as at Apex start), else only asks
            return obs_item(True, True)
        except Exception as e:
            log.info("recording check: OBS didn't answer: %s", e)
            return obs_item(True, False, str(e))
        finally:
            if lock is not None:
                lock.release()

    # -- called by the recorder
    def apex_closed(self) -> None:
        if self.toasted or self.game_reported:
            self.toasted = self.game_reported = False
        if self.items.get("game", {}).get("state") in ("wait",) and self.items["game"]["text"].startswith("Checking"):
            self._set({"game": game_item(self.s, False)})

    def record_failed(self, error) -> None:
        """Recording didn't start: the quick items now (the crash report carries them), one message with the causes."""
        items = self._quick(True)
        items["obs"] = obs_item(self.installed(), False, str(error))
        self._set(items)
        self._toast(skip={"obs"})

    def apex_started(self) -> None:
        """Recording started: quick items, one message if something is red, then Apex's sound (and the voice app
        and mic) from the level meters in the first minutes of the recording."""
        self.session += 1
        items = self._quick(True)
        items["obs"] = obs_item(True, True, recording=True)
        items["game"] = game_item(self.s, True)
        self._set(items)
        self._toast()
        self._go(self._confirm, self.session)

    def _toast(self, skip=()) -> None:
        reds = [i for i in self.reds() if i["id"] not in skip]
        if not reds or self.toasted or self.ui is None:
            return
        self.toasted = True
        text = " ".join(f"{i['label']}: {i['text']}." for i in reds[:3])
        fix = reds[0].get("fix") or ""
        self.ui.notify("Recording check", (text + (" Fix: " + fix if fix else ""))[:300], True)

    def _confirm(self, session: int) -> None:
        names = input_names(self.s)
        if not names:
            return
        t0 = time.time()
        while time.time() - t0 < self.confirm_after:
            if session != self.session or not self._recording():
                return
            time.sleep(0.5)
        want_game = "game" in names.values()

        def enough(peaks):
            return all((peaks.get(t) or 0) > SIGNAL for t in set(names.values()))
        try:
            peaks = self.listen(self.s, names, self.confirm_max,
                                stop=lambda: session != self.session or not self._recording(), enough=enough)
        except Exception as e:
            log.info("level meters not read during the recording: %s", e)
            return
        if session != self.session or not self._recording():
            return
        self._set(self._levels(peaks, True, done=True))
        log.info("recording check (first minutes): %s", self.summary())
        if want_game and not ((peaks.get("game") or 0) > SIGNAL) and not self.game_reported:
            self.game_reported = True
            from . import crash
            crash.report("record_start", "No Apex sound in the first minutes of the recording", "",
                         crash.obs_log_tail(), {"check": self.summary()})
            if self.ui is not None:
                self.ui.notify("Recording check", "No Apex sound in the recording. Check that Apex isn't muted in "
                               "the Windows volume mixer.", True)
