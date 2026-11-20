"""Scan on the team server (opt-in, Settings > Team hub > "Scan on the team server").

Instead of decoding the recording on the player's PC, the app sends the team hub a light copy and takes the results
back; the hub runs the same scanner at low priority (hub/scanjobs.py). Same result files, same upload afterwards.

- **Light copy**: a stream copy of the recording with only its keyframes (one per second: all the scanner reads) and
  track 1 (Apex's sound, for the ring alarm). No decoding, no Discord, no mic: about 0.7 GB per hour of 1440p60 AV1
  (13-14 % of the recording), made in seconds, sent in resumable parts of 50 MB.
- **Clips**: the scanner's frame-exact reads (jump, landing, ring 1 start) need the 60 fps frames between two
  keyframes. The hub's first pass lists those windows; the app cuts them from the recording (stream copy, a few MB
  in all) and sends them; the second pass re-uses the first pass's work and only redoes the end.
- **Fallback**: anything wrong (hub off or old, worker off, refused, failed, no answer for too long) and the scan
  runs on this PC as before. A scan never waits more than `WAIT_S` in the hub's queue.

It runs inside the headless scan process (`pcs_app.py --scan`), so the recorder stops it when Apex starts, like a
local scan: nothing is uploaded while Apex runs. The upload goes on where it stopped the next time (hub-uploads.json),
and a job already on the hub is picked up again (server-scan.json in the results folder).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from . import hub
from .settings import SETTINGS_FILE, APP_DIR

log = logging.getLogger("pcs.serverscan")
POLL_S = 10
WAIT_S = 3 * 3600            # a job not done after this long (queue + scan): give up, scan here
STATE = "server-scan.json"
NO_WINDOW = 0x08000000


class Fallback(Exception):
    """Scan on this PC instead (the reason goes to the scan log)."""


def wanted() -> tuple[bool, str]:
    """(on, hub address) from the saved settings, read only (the app itself owns that file)."""
    try:
        s = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, ""
    url = str(s.get("hub_url") or "")
    return bool(s.get("server_scan")) and bool(url), url


def _run(cmd: list[str], timeout: float = 900) -> None:
    flags = (0x00004000 | NO_WINDOW) if os.name == "nt" else 0       # below normal, no window
    subprocess.run(cmd, check=True, capture_output=True, timeout=timeout, creationflags=flags)


def light_cmd(video: str, out: Path) -> list[str]:
    """Keyframes + track 1, stream copy, the recording's own times (-copyts: the clips are cut the same way)."""
    return ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", str(video), "-map", "0:v:0",
            "-map", "0:a:0?", "-c", "copy", "-copyts", "-bsf:v", "noise=drop=not(key)", str(out)]


def clip_cmd(video: str, a: float, b: float, out: Path) -> list[str]:
    """Video only, stream copy, with the recording's times: from the keyframe at or before a (the windows start on a
    keyframe) to just after b. -ss and -t go before -i: with -copyts an output -t gives an empty file."""
    return ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-ss", f"{max(0.0, a - 0.05):.3f}",
            "-t", f"{b - a + 0.3:.3f}", "-i", str(video), "-map", "0:v:0", "-c", "copy", "-copyts", str(out)]


def media_duration(video: str) -> float:
    o = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 str(video)], creationflags=NO_WINDOW if os.name == "nt" else 0)
    return float(o.decode().strip())


def decoder_family() -> str:
    """'cpu' when this PC's scanner chose CPU decoding (decoder.json), else 'gpu': the hub decodes the same way."""
    try:
        d = json.loads((APP_DIR / "decoder.json").read_text(encoding="utf-8"))
        names = [v[0] for k, v in d.items() if k.endswith("-gpu")]
        return "cpu" if names and all(n == "CPU" for n in names) else "gpu"
    except (OSError, ValueError, TypeError, IndexError):
        return "gpu"


def scanner_version() -> str:
    """This app's scanner version: the hub scans only with the same one (else it answers 409 and this PC scans)."""
    import pcs_scan
    return pcs_scan.VERSION


def chunks_for(total: float) -> int:
    """The number of chunks this PC would cut the recording into: results depend on it."""
    import pcs_scan
    return pcs_scan.default_chunks(total)


class ServerScan:
    """One recording's scan on the hub. `run()` -> the result files in `out`, or raises Fallback."""

    def __init__(self, video: str, out: str, url: str, say=print, sleep=time.sleep, clock=time.time):
        self.video, self.out, self.url = str(video), Path(out), url
        self.say, self.sleep, self.clock = say, sleep, clock
        self.work = self.out / "server"
        self.name = Path(video).name
        self.stem = Path(video).stem

    # ------------------------------------------------------------- state kept between tries
    def _state(self) -> dict:
        try:
            return json.loads((self.out / STATE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, **kw) -> None:
        st = {**self._state(), **kw}
        (self.out / STATE).write_text(json.dumps(st), encoding="utf-8")

    def _forget(self) -> None:
        (self.out / STATE).unlink(missing_ok=True)

    # ------------------------------------------------------------- steps
    def light(self) -> Path:
        self.work.mkdir(parents=True, exist_ok=True)
        f = self.work / self.name                   # same file name: the scanner names its results after it
        if not (f.exists() and self._state().get("light") == f.stat().st_size):
            t = time.time()
            _run(light_cmd(self.video, f))
            self._save(light=f.stat().st_size)
            self.say(f"server scan: light copy {f.stat().st_size / 2**20:.0f} MB in {time.time() - t:.0f} s")
        return f

    def send(self, light: Path) -> str:
        jid = self._state().get("job")
        if jid:
            r = hub.request(self.url, "GET", f"/api/scan-jobs/{jid}")
            if r.status_code == 200 and r.json().get("status") not in ("failed", "cancelled"):
                return jid                          # picked up again after a stop (Apex) or an app restart
            self._save(job=None)
        total = media_duration(self.video)
        fields = {"duration": total, "chunks": chunks_for(total), "layout": "auto", "decoder": decoder_family(),
                  "scanner": scanner_version()}
        try:
            ans = hub.send_in_parts(self.url, "scan-source", light, fields, stop=None)
        except hub.HubError as e:
            if e.code in ("server_scan_off", "scanner_version", "too_many_jobs") or e.status in (400, 409, 413, 503, 507):
                raise Fallback(str(e)) from None
            raise Fallback(f"couldn't send the recording: {e}") from None
        if ans is None:
            raise Fallback("this team hub can't scan (older hub)")
        jid = str((ans.get("job") or {}).get("id") or "")
        if not jid:
            raise Fallback("the hub didn't start a scan")
        self._save(job=jid)
        return jid

    def clips(self, jid: str, needs: list) -> None:
        d = self.work / "clips"
        d.mkdir(parents=True, exist_ok=True)
        ext = Path(self.video).suffix or ".mp4"
        files = []
        for i, (a, b) in enumerate(needs[:40]):
            f = d / f"clip{i:03d}{ext}"
            _run(clip_cmd(self.video, float(a), float(b), f), timeout=300)
            files.append(f)
        handles = [open(f, "rb") for f in files]
        try:
            r = hub.request(self.url, "POST", f"/api/scan-jobs/{jid}/clips", timeout=300,
                            files=[("files", (f.name, h, "video/mp4")) for f, h in zip(files, handles)])
        finally:
            for h in handles:
                h.close()
        if r.status_code != 200:
            raise Fallback(f"the hub refused the clips ({r.status_code})")
        self.say(f"server scan: {len(files)} clip(s) sent, {sum(f.stat().st_size for f in files) / 2**20:.1f} MB")

    def fetch(self, jid: str, files: list[str]) -> list[Path]:
        got = []
        for n in files:
            if n not in (f"{self.stem}.scan.json", f"{self.stem}.feed.json"):
                continue
            r = hub.request(self.url, "GET", f"/api/scan-jobs/{jid}/files/{n}", timeout=120)
            if r.status_code != 200:
                raise Fallback(f"couldn't fetch {n} ({r.status_code})")
            tmp = self.out / (n + ".part")
            tmp.write_bytes(r.content)
            os.replace(tmp, self.out / n)
            got.append(self.out / n)
        if not any(p.name.endswith(".scan.json") for p in got):
            raise Fallback("the hub's scan has no results file")
        return got

    def run(self) -> list[Path]:
        self.out.mkdir(parents=True, exist_ok=True)
        try:
            light = self.light()
        except (OSError, subprocess.SubprocessError) as e:
            raise Fallback(f"couldn't make the light copy: {e}") from None
        jid = self.send(light)
        start = self._state().get("since") or self.clock()
        self._save(since=start)
        sent_clips = False
        while True:
            try:
                r = hub.request(self.url, "GET", f"/api/scan-jobs/{jid}")
            except hub.HubError as e:
                if self.clock() - start > WAIT_S:
                    raise Fallback(f"no answer from the hub: {e}") from None
                self.sleep(POLL_S)
                continue
            if r.status_code == 404:
                self._save(job=None)
                raise Fallback("the hub lost the scan")
            j = r.json()
            st = j.get("status")
            if st == "done":
                files = self.fetch(jid, j.get("files") or [])
                self.say(f"server scan: done in {j.get('passes')} pass(es), {j.get('seconds', '?')} s on the hub")
                self.cleanup()
                return files
            if st in ("failed", "cancelled"):
                self._save(job=None)
                raise Fallback(f"the hub's scan {st}: {j.get('note') or ''}".strip())
            if st == "needs_clips" and not sent_clips:
                try:
                    self.clips(jid, j.get("needs") or [])
                except (OSError, subprocess.SubprocessError) as e:
                    raise Fallback(f"couldn't cut the clips: {e}") from None
                sent_clips = True
                continue
            if self.clock() - start > WAIT_S:
                try:
                    hub.request(self.url, "DELETE", f"/api/scan-jobs/{jid}")
                except hub.HubError:
                    pass
                self._save(job=None)
                raise Fallback("the hub took too long")
            self.sleep(POLL_S)

    def cleanup(self) -> None:
        import shutil
        shutil.rmtree(self.work, ignore_errors=True)
        self._forget()


def scan(video: str, out: str, say=print) -> list[Path] | None:
    """Results in `out` from the team server, or None: scan on this PC (the reason is said)."""
    on, url = wanted()
    if not on:
        return None
    s = ServerScan(video, out, url, say=say)
    try:
        say(f"server scan: sending {Path(video).name} to the team hub")
        return s.run()
    except Fallback as e:
        say(f"server scan: {e}. Scanning on this PC.")
    except Exception as e:                          # never lose a scan to the server path
        log.exception("server scan")
        say(f"server scan failed ({type(e).__name__}: {e}). Scanning on this PC.")
    try:
        import shutil
        shutil.rmtree(s.work, ignore_errors=True)   # the light copy and clips; the job id stays for a later try
    except OSError:
        pass
    return None
