"""Clips from the review player: a part of a recording as an MP4 to post in Discord (idea #8, 19 Nov 2026).

- Range: start / end in the recording's time (set in the review player: start and end buttons, or an event or
  bookmark with some seconds before and after).
- Sound: any of the recording's own tracks except the voice-app track (track 2 holds teammates' voices; their voices
  go into a clip only through their own mic tracks, which they agreed to share), plus downloaded teammates' mic
  tracks (pcs_recorder.teamtracks), each at its own time offset. A teammate's track is used only when the team hub
  says at that moment that they still share it (they may have said no since the download) AND that they allow their
  voice in teammates' clips (`share_clips`, their own separate yes, off by default: coach, 20 Nov 2026; no answer or
  an older hub = left out); hub out of reach = teammates' tracks left out. Mixed at their recorded levels.
- Coach notes: the drawings and texts on screen in the range are drawn (Pillow) on transparent pictures at the clip's
  size and laid over the video for the time they show in the review player.
- Size: "discord" = under 10 MB (bitrate from the length; size and frame rate lowered to fit; encoded again lower if
  it came out too big), or "full" = the recording's size and frame rate at high quality.
- Encoder: H.264 (plays everywhere, Discord included) on the graphics card the recorder's encoder test found
  (NVENC / AMF / Quick Sync), else x264 on the CPU; a hardware encoder that fails falls back to x264.
- When: never while Apex runs or the recorder records (refused); Apex starting stops it (partial file removed).
  ffmpeg runs at below normal priority, one clip at a time.
- Where: next to the recording, in a "Clips" folder. Written as .part, renamed when complete. Nothing is uploaded.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("clips")

DISCORD_BYTES = 10_000_000          # Discord's upload limit without Nitro: 10 MB
DISCORD_TARGET = 9_000_000          # aimed at, leaves room for the encoder's overshoot (AMF: +6 % seen)
MIN_VIDEO_KBPS = 300                # below this the picture is mush: too long for 10 MB
MAX_VIDEO_KBPS = 8000               # a short Discord clip needs no more
FULL_MAX_S = 15 * 60                # a full-quality clip is at most 15 minutes
MIN_S = 1.0
MAX_OVERLAYS = 40
FOLDER = "Clips"
OWN_TRACKS_LEFT_OUT = ("discord",)  # the voice-app track: teammates' voices without their own consent
APEX_EVERY_S = 2


def _quiet() -> int:
    return 0x08000000 | 0x00004000 if os.name == "nt" else 0       # no window, below-normal priority


# ---------------------------------------------------------------- what the clip will be
def audio_kbps(n_tracks: int, size: str) -> int:
    if not n_tracks:
        return 0
    return 96 if size == "discord" else 192


def discord_video_kbps(length: float, n_tracks: int) -> float:
    """Video bitrate that keeps a clip of this length under the Discord limit (2 % for the MP4 container)."""
    total = DISCORD_TARGET * 8 / 1000 / max(length, 0.1) * 0.98
    return total - audio_kbps(n_tracks, "discord")


def discord_max_s(n_tracks: int) -> int:
    """The longest clip that still fits in 10 MB at a watchable quality."""
    return int(DISCORD_TARGET * 8 / 1000 * 0.98 / (MIN_VIDEO_KBPS + audio_kbps(n_tracks, "discord")))


def even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def out_format(src_w: int, src_h: int, src_fps: float, size: str, vkbps: float | None) -> tuple[int, int, float]:
    """(width, height, fps) of the clip: the recording's for "full"; for Discord smaller as the bitrate gets lower."""
    fps = src_fps if src_fps and src_fps > 0 else 60.0
    if size != "discord":
        return even(src_w), even(src_h), fps
    k = vkbps or 0
    h = 1080 if k >= 4500 else 720 if k >= 2000 else 540 if k >= 900 else 480
    h = min(h, src_h)
    w = src_w * h / src_h
    return even(w), even(h), min(fps, 60.0 if k >= 3000 else 30.0)


def hw_encoder(obs_encoder: str) -> str:
    """The ffmpeg H.264 encoder on the same graphics card as the recorder's OBS encoder, else x264."""
    e = (obs_encoder or "").lower()
    if "nvenc" in e:
        return "h264_nvenc"
    if "amf" in e:
        return "h264_amf"
    if "qsv" in e:
        return "h264_qsv"
    return "libx264"


def encoder_args(enc: str, size: str, vkbps: float | None) -> list[str]:
    if size == "discord":
        b = f"{int(vkbps)}k"
        mx, buf = f"{int(vkbps * 1.5)}k", f"{int(vkbps * 2)}k"
        if enc == "h264_nvenc":
            return ["-c:v", enc, "-preset", "p5", "-rc", "vbr", "-b:v", b, "-maxrate", mx, "-bufsize", buf]
        if enc == "h264_amf":
            return ["-c:v", enc, "-quality", "quality", "-rc", "vbr_peak", "-b:v", b, "-maxrate", mx, "-bufsize", buf]
        if enc == "h264_qsv":
            return ["-c:v", enc, "-preset", "slow", "-b:v", b, "-maxrate", mx, "-bufsize", buf]
        return ["-c:v", "libx264", "-preset", "veryfast", "-b:v", b, "-maxrate", mx, "-bufsize", buf]
    # full quality: high quality with a ceiling (a 1440p60 game clip at a fixed quality came out at 58 Mbit/s)
    b, mx, buf = f"{int(vkbps)}k", f"{int(vkbps * 1.5)}k", f"{int(vkbps * 2)}k"
    if enc == "h264_nvenc":
        return ["-c:v", enc, "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", b, "-maxrate", mx, "-bufsize", buf]
    if enc == "h264_amf":
        return ["-c:v", enc, "-quality", "quality", "-rc", "vbr_peak", "-b:v", b, "-maxrate", mx, "-bufsize", buf]
    if enc == "h264_qsv":
        return ["-c:v", enc, "-preset", "slow", "-b:v", b, "-maxrate", mx, "-bufsize", buf]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-maxrate", mx, "-bufsize", buf]


def full_kbps(w: int, h: int, fps: float) -> int:
    """Full quality: about 20 Mbit/s for 1440p60, 12 for 1080p60, 6 for 720p60 (H.264), less at 30 fps."""
    px = w * h * min(fps or 60, 60) / (2560 * 1440 * 60)
    return int(max(4000, min(30000, 20000 * px ** 0.75)))


def note_spans(notes: list[dict], start: float, end: float) -> list[tuple[float, float, list[dict]]]:
    """Which notes show when, in clip time: [(t0, t1, notes on screen)], as in the review player (a note shows from
    its t - 0.05 to t + dur). Neighbouring spans with the same notes are joined; at most MAX_OVERLAYS."""
    length = end - start
    shown = []
    for n in notes or []:
        if not (n.get("drawings") or (n.get("text") or "").strip()):
            continue
        try:
            a, b = float(n["t"]) - 0.05 - start, float(n["t"]) + float(n.get("dur") or 5) - start
        except (KeyError, TypeError, ValueError):
            continue
        if b > 0 and a < length:
            shown.append((max(0.0, a), min(length, b), n))
    cuts = sorted({0.0, length} | {x for a, b, _ in shown for x in (a, b)})
    spans = []
    for a, b in zip(cuts, cuts[1:]):
        mid = (a + b) / 2
        on = [n for x, y, n in shown if x <= mid < y]
        if not on or b - a < 1e-3:
            continue
        if spans and spans[-1][1] == a and [id(x) for x in spans[-1][2]] == [id(x) for x in on]:
            spans[-1] = (spans[-1][0], b, on)
        else:
            spans.append((a, b, on))
    return spans[:MAX_OVERLAYS]


# ---------------------------------------------------------------- coach notes as pictures
def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for name in (("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")):
        try:
            return ImageFont.truetype(os.path.join(windir, "Fonts", name), size)
        except OSError:
            continue
    for name in ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def _rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    c = str(color or "#ffffff").lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", c):
        c = "ffffff"
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha


def _shape(d, s: dict, W: int, H: int, lw: int, color) -> None:
    """One drawing, as drawShapes in app.js (0..1 frame coordinates)."""
    import math
    pts = [(float(x) * W, float(y) * H) for x, y in (s.get("pts") or [])[:4000]]
    if not pts:
        return
    kind = s.get("kind")
    if kind == "pen":
        if len(pts) == 1:
            x, y = pts[0]
            d.ellipse([x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2], fill=color)
        else:
            d.line(pts, fill=color, width=lw, joint="curve")
            for x, y in (pts[0], pts[-1]):                   # round ends
                d.ellipse([x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2], fill=color)
        return
    (x1, y1), (x2, y2) = pts[0], pts[-1]
    box = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
    if kind == "rect":
        d.rectangle(box, outline=color, width=lw)
    elif kind == "circle":
        d.ellipse(box, outline=color, width=lw)
    else:                                                    # arrow
        a = math.atan2(y2 - y1, x2 - x1)
        hl = max(lw * 4, min(W * 0.03, math.hypot(x2 - x1, y2 - y1) / 2))
        d.line([(x1, y1), (x2, y2)], fill=color, width=lw)
        for s_ in (-0.45, 0.45):
            d.line([(x2 - hl * math.cos(a + s_), y2 - hl * math.sin(a + s_)), (x2, y2)], fill=color, width=lw)
        for x, y in ((x1, y1), (x2, y2)):
            d.ellipse([x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2], fill=color)


def _wrap(d, text: str, font, width: float, most: int = 12) -> list[str]:
    """The note's text in lines that fit the box (a word too long for a line is cut); at most `most` lines."""
    fits = lambda x: d.textlength(x, font=font) <= width                     # noqa: E731
    lines = []
    for para in text.splitlines() or [""]:
        cur = ""
        for word in para.split(" "):
            t = (cur + " " + word) if cur else word
            if fits(t):
                cur = t
                continue
            if cur:
                lines.append(cur)
            cur = word
            while not fits(cur) and len(cur) > 1 and len(lines) < most:      # one very long word
                lo, hi = 1, len(cur) - 1                                     # longest start of it that fits
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    lo, hi = (mid, hi) if fits(cur[:mid]) else (lo, mid - 1)
                lines.append(cur[:lo])
                cur = cur[lo:]
            if len(lines) >= most:
                return lines[:most]
        lines.append(cur)
    return lines[:most]


def render_notes(notes: list[dict], W: int, H: int, out: Path) -> Path:
    """The drawings and texts of the notes on screen together, on a transparent W x H picture (PNG)."""
    from PIL import Image, ImageDraw, ImageFilter
    lw = max(2, round(W * 0.0035))
    shapes = [s for n in notes for s in (n.get("drawings") or []) if isinstance(s, dict)]
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if shapes:                                               # a soft dark shadow under the lines (shadowBlur)
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ds = ImageDraw.Draw(sh)
        for s in shapes:
            _shape(ds, s, W, H, lw + 2, (0, 0, 0, 180))
        img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(max(1, lw / 1.5))))
        d = ImageDraw.Draw(img)
        for s in shapes:
            _shape(d, s, W, H, lw, _rgba(s.get("color")))
    texts = [(n.get("text") or "").strip() for n in notes if (n.get("text") or "").strip()]
    if texts:                                                # the note boxes at the top right, as .cnote
        d = ImageDraw.Draw(img)
        fs = max(12, round(H * 0.024))
        body, label = _font(fs), _font(max(10, round(fs * 0.85)), bold=True)
        pad, gap, bar, margin = round(fs * 0.7), round(fs * 0.5), max(3, round(fs * 0.2)), round(H * 0.018)
        maxw = W * 0.42 - 2 * pad - bar
        y = margin
        for t in texts:
            lines = _wrap(d, t[:2000], body, maxw)
            lh = round(fs * 1.3)
            tw = max([d.textlength(x, font=body) for x in lines] + [d.textlength("COACH", font=label)])
            bw, bh = tw + 2 * pad + bar, pad * 2 + round(fs * 1.15) + lh * len(lines)
            x0 = W - margin - bw
            if y + bh > H - margin:
                break
            d.rounded_rectangle([x0, y, x0 + bw, y + bh], radius=round(fs * 0.4), fill=(10, 10, 9, 210),
                                outline=(255, 255, 255, 30))
            d.rectangle([x0, y, x0 + bar, y + bh], fill=(255, 214, 10, 255))
            d.text((x0 + bar + pad, y + pad), "COACH", font=label, fill=(255, 214, 10, 255))
            ty = y + pad + round(fs * 1.15)
            for line in lines:
                d.text((x0 + bar + pad, ty), line, font=body, fill=(241, 239, 234, 255))
                ty += lh
            y += bh + gap
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return out


# ---------------------------------------------------------------- the ffmpeg command
def probe(video: str) -> dict:
    """Width, height, frame rate and length of the recording's video."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,width,height,avg_frame_rate,r_frame_rate:format=duration", "-of", "json", video],
                       capture_output=True, text=True, timeout=30, creationflags=_quiet())
    d = json.loads(r.stdout or "{}")
    st = (d.get("streams") or [{}])[0]

    def rate(x):
        try:
            a, _, b = str(x).partition("/")
            return float(a) / float(b or 1)
        except (ValueError, ZeroDivisionError):
            return 0.0
    fps = rate(st.get("avg_frame_rate")) or rate(st.get("r_frame_rate")) or 60.0
    return {"codec": str(st.get("codec_name") or ""), "w": int(st.get("width") or 0), "h": int(st.get("height") or 0),
            "fps": round(min(fps, 240.0), 3),
            "dur": float((d.get("format") or {}).get("duration") or 0)}


def team_inputs(parts: list[dict], start: float, end: float) -> list[tuple[float, float, float]]:
    """For one teammate track: [(their time to start at, length, delay in the clip)] for each game part of the
    recording that overlaps the clip (their time = this recording's time + off, inside start..end of the part)."""
    out = []
    for p in parts or []:
        a = max(start, p.get("start") if p.get("start") is not None else start)
        b = min(end, p.get("end") if p.get("end") is not None else end)
        off = float(p.get("off") or 0)
        if a + off < 0:                                      # their recording starts later
            a = -off
        if b - a >= 0.2:
            out.append((round(a + off, 3), round(b - a, 3), round(a - start, 3)))
    return out


def hw_decode(codec: str) -> list[str]:
    """Decoding on the graphics card (DirectX 11: AMD, NVIDIA, Intel). For AV1 '-hwaccel' alone is ignored (ffmpeg
    takes the CPU decoder), so the native decoder is named, as the scanner does. AV1 1440p60, 30 s: 14 s on the CPU,
    9.5 s on an RX 9070 XT."""
    if os.name != "nt":
        return []
    return ["-hwaccel", "d3d11va"] + (["-c:v", codec] if codec in ("av1", "hevc", "h264") else [])


def build_cmd(video: str, out: str, start: float, end: float, *, w: int, h: int, fps: float, src: dict,
              own: list[int], team: list[tuple[str, list[dict]]], overlays: list[tuple[str, float, float]],
              enc: str, size: str, vkbps: float | None, hwdec: bool = True) -> list[str]:
    """One ffmpeg run: video (+ overlays), the chosen tracks mixed, H.264 + AAC in MP4."""
    L = round(end - start, 3)
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-progress", "pipe:1"]
    cmd += hw_decode(src.get("codec", "")) if hwdec else []
    cmd += ["-ss", f"{start:.3f}", "-t", f"{L:.3f}", "-i", video]
    k = 1
    vf = []
    chain = "[0:v]"
    if (w, h) != (src["w"], src["h"]):
        vf.append(f"scale={w}:{h}:flags=lanczos")
    if src.get("fps") and fps < src["fps"] - 0.5:
        vf.append(f"fps={fps:g}")
    filt = []
    if vf:
        filt.append(chain + ",".join(vf) + "[v0]")
        chain = "[v0]"
    for i, (png, t0, t1) in enumerate(overlays):
        cmd += ["-i", png]
        nxt = f"[v{i + 1}]"
        filt.append(f"[{k}:v]scale={w}:{h}[o{i}]")
        filt.append(f"{chain}[o{i}]overlay=0:0:eof_action=repeat:enable='between(t,{t0:.3f},{t1:.3f})'{nxt}")
        chain = nxt
        k += 1
    filt.append(f"{chain}format=yuv420p[vout]")
    amix = [f"[0:a:{n}]" for n in own]
    for path, parts in team:
        for ts, tl, delay in team_inputs(parts, start, end):
            cmd += ["-ss", f"{ts:.3f}", "-t", f"{tl:.3f}", "-i", path]
            ms = int(round(delay * 1000))
            filt.append(f"[{k}:a:0]adelay={ms}|{ms}[d{k}]")
            amix.append(f"[d{k}]")
            k += 1
    if amix:
        ins = []
        for i, a in enumerate(amix):
            filt.append(f"{a}aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
            ins.append(f"[a{i}]")
        filt.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{L:.3f}[silence]")
        filt.append(f"[silence]{''.join(ins)}amix=inputs={len(ins) + 1}:duration=first:normalize=0[aout]")
    cmd += ["-filter_complex", ";".join(filt), "-map", "[vout]"]
    cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", f"{audio_kbps(len(amix), size)}k"] if amix else ["-an"]
    cmd += encoder_args(enc, size, vkbps)
    if enc != "h264_qsv":
        cmd += ["-pix_fmt", "yuv420p"]
    cmd += ["-r", f"{fps:g}", "-t", f"{L:.3f}", "-movflags", "+faststart", "-f", "mp4", out]
    return cmd


def clip_name(video: str, start: float, end: float) -> str:
    def t(x):
        x = int(x)
        return f"{x // 3600}h{x % 3600 // 60:02d}m{x % 60:02d}s" if x >= 3600 else f"{x // 60}m{x % 60:02d}s"
    return f"{Path(video).stem} clip {t(start)}-{t(end)}.mp4"


def free_name(folder: Path, name: str) -> Path:
    p = folder / name
    i = 2
    while p.exists():
        p = folder / f"{Path(name).stem} ({i}).mp4"
        i += 1
    return p


# ---------------------------------------------------------------- clipboard (Windows): the file, as Explorer copies it
def hdrop(paths: list[str]) -> bytes:
    """CF_HDROP data: a DROPFILES header (20 bytes, wide names) then the names, each ending in NUL, then NUL."""
    names = "".join(os.path.abspath(p) + "\0" for p in paths) + "\0"
    return struct.pack("<IiiII", 20, 0, 0, 0, 1) + names.encode("utf-16-le")


def copy_files_to_clipboard(paths: list[str]) -> None:
    import ctypes
    from ctypes import wintypes
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    u32.SetClipboardData.restype = wintypes.HANDLE
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]

    def put(fmt: int, data: bytes) -> None:
        h = k32.GlobalAlloc(0x0042, len(data))               # GMEM_MOVEABLE | GMEM_ZEROINIT
        if not h:
            raise OSError("no memory for the clipboard")
        p = k32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k32.GlobalUnlock(h)
        if not u32.SetClipboardData(fmt, h):
            k32.GlobalFree(h)
            raise OSError("the clipboard refused the file")

    for _ in range(10):                                      # another program may hold the clipboard a moment
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise OSError("the clipboard is in use by another program")
    try:
        u32.EmptyClipboard()
        put(15, hdrop(paths))                                # CF_HDROP
        put(u32.RegisterClipboardFormatW("Preferred DropEffect"), struct.pack("<I", 1))   # copy, not move
    finally:
        u32.CloseClipboard()


def show_in_folder(path: str) -> None:
    if os.name == "nt":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])


# ---------------------------------------------------------------- the job
class Stopped(Exception):
    pass


class Clipper:
    """One clip at a time, in a thread. `state()` is what the review page shows."""

    def __init__(self, busy=lambda: None, run=None):
        self.busy = busy                                     # a reason not to run now (Apex, recording), or None
        self.lock = threading.Lock()
        self.job: dict | None = None
        self.made: set[str] = set()                          # clips made in this run of the app (Show / Copy)
        self.proc = None
        self._run_ffmpeg = run or self._ffmpeg

    def state(self) -> dict:
        with self.lock:
            j = dict(self.job or {})
        return {k: j.get(k) for k in ("running", "step", "pct", "error", "file", "name", "mb", "leftOut", "encoder",
                                      "seconds", "over")}

    def start(self, spec: dict) -> dict:
        """spec: video, start, end, size, own [track n], team [(path, parts, player)], notes [note], obs_encoder."""
        with self.lock:
            if self.job and self.job.get("running"):
                raise RuntimeError("A clip is being made. Wait for it to finish.")
            why = self.busy()
            if why:
                raise RuntimeError(why)
            self.job = {"running": True, "step": "Starting", "pct": 0, "leftOut": spec.get("leftOut") or [],
                        "seconds": round(spec["end"] - spec["start"], 1), "stop": False}
        threading.Thread(target=self._run, args=(spec,), daemon=True, name="clip").start()
        return self.state()

    def stop(self) -> None:
        with self.lock:
            if self.job and self.job.get("running"):
                self.job["stop"] = True
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def _set(self, **kw) -> None:
        with self.lock:
            if self.job is not None:
                self.job.update(kw)

    def _stopped(self) -> str | None:
        with self.lock:
            if self.job and self.job.get("stop"):
                return "Stopped."
        why = self.busy()
        return f"Stopped: {why}. Make the clip again later." if why else None

    def _ffmpeg(self, cmd: list[str], length: float) -> tuple[int, str]:
        """Runs ffmpeg, reports progress, stops it when asked or when Apex starts. (exit code, error text)"""
        p = self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                         creationflags=_quiet())
        err = []
        threading.Thread(target=lambda: err.extend(p.stderr.read().splitlines()[-20:]), daemon=True).start()
        state = {"looked": time.monotonic()}

        def watch():                                         # Apex / Stop also while ffmpeg prints nothing
            while p.poll() is None:
                time.sleep(0.5)
                if time.monotonic() - state["looked"] >= APEX_EVERY_S:
                    state["looked"] = time.monotonic()
                    if self._stopped():
                        try:
                            p.kill()
                        except OSError:
                            pass
                        return
        threading.Thread(target=watch, daemon=True).start()
        for line in p.stdout:
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    t = int(line.split("=", 1)[1]) / 1e6
                    self._set(pct=max(0, min(99, int(t / max(length, 0.1) * 100))))
                except ValueError:
                    pass
        p.wait()
        self.proc = None
        return p.returncode, "\n".join(err)

    def _run(self, spec: dict) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="pcs-clip-"))
        part = None
        try:
            video, start, end, size = spec["video"], float(spec["start"]), float(spec["end"]), spec["size"]
            src = spec.get("src") or probe(video)
            if not src["w"] or not src["h"]:
                raise RuntimeError("The recording's picture can't be read.")
            L = end - start
            n_audio = len(spec.get("own") or []) + len(spec.get("team") or [])
            vkbps = None
            if size == "discord":
                vkbps = min(MAX_VIDEO_KBPS, discord_video_kbps(L, n_audio))
                if vkbps < MIN_VIDEO_KBPS:
                    raise RuntimeError(f"Too long for 10 MB. Make it {discord_max_s(n_audio)} s or shorter, "
                                       "or pick Full quality.")
            w, h, fps = out_format(src["w"], src["h"], src["fps"], size, vkbps)
            if size != "discord":
                vkbps = full_kbps(w, h, fps)
            overlays = []
            if spec.get("notes"):
                self._set(step="Drawing the coach's notes")
                for i, (t0, t1, on) in enumerate(note_spans(spec["notes"], start, end)):
                    png = render_notes(on, w, h, tmp / f"note{i}.png")
                    overlays.append((str(png), t0, t1))
            folder = Path(video).parent / FOLDER
            folder.mkdir(parents=True, exist_ok=True)
            final = free_name(folder, clip_name(video, start, end))
            part = final.with_name(final.stem + ".part")
            enc = hw_encoder(spec.get("obs_encoder") or "")
            # graphics card encode + decode, then the card's encoder with CPU decoding, then all on the CPU
            tries = [(enc, True), (enc, False), ("libx264", False)] if enc != "libx264" else [("libx264", False)]
            for attempt in range(3):                          # Discord: again lower when it came out too big
                code, err = 1, ""
                for e, hw in tries:
                    why = self._stopped()
                    if why:
                        raise Stopped(why)
                    self._set(step="Making the clip" if attempt == 0 else "Making it smaller", pct=0, encoder=e)
                    cmd = build_cmd(video, str(part), start, end, w=w, h=h, fps=fps, src=src,
                                    own=spec.get("own") or [], team=[(p, parts) for p, parts, _ in spec.get("team") or []],
                                    overlays=overlays, enc=e, size=size, vkbps=vkbps, hwdec=hw)
                    code, err = self._run_ffmpeg(cmd, L)
                    why = self._stopped()
                    if why:
                        raise Stopped(why)
                    if code == 0 and part.exists() and part.stat().st_size > 0:
                        tries = [(e, hw)]                    # keep the encoder that worked
                        break
                    log.warning("clip with %s failed (%s): %s", e, code, err[-400:])
                log.info("clip try %d: %s, %d x %d, %g fps, %s kbit/s", attempt + 1, tries[0][0], w, h, fps,
                         int(vkbps) if vkbps else "-")
                if code != 0:
                    raise RuntimeError("ffmpeg could not make the clip.")
                got = part.stat().st_size
                if size != "discord" or got <= DISCORD_BYTES:
                    break
                if attempt == 2:
                    self._set(over=True)
                    break
                vkbps = vkbps * DISCORD_TARGET / got * 0.92
                if vkbps < MIN_VIDEO_KBPS * 0.6:
                    self._set(over=True)
                    break
                w, h, fps = out_format(src["w"], src["h"], src["fps"], size, vkbps)
                if overlays:                                 # the pictures follow the new size
                    overlays = [(str(render_notes(on, w, h, tmp / f"note{i}.png")), t0, t1)
                                for i, (t0, t1, on) in enumerate(note_spans(spec["notes"], start, end))]
            os.replace(part, final)
            with self.lock:
                self.made.add(os.path.normcase(os.path.abspath(str(final))))
            mb = round(final.stat().st_size / 1e6, 1)
            log.info("clip made: %s (%.1f MB, %s)", final.name, mb, tries[0][0])
            self._set(running=False, step="Done", pct=100, file=str(final), name=final.name, mb=mb)
        except Stopped as e:
            self._set(running=False, step="Stopped", error=str(e))
        except Exception as e:
            log.warning("clip failed: %s", e)
            self._set(running=False, step="Failed", error=str(e))
        finally:
            self.proc = None
            if part is not None:
                part.unlink(missing_ok=True)
            shutil.rmtree(tmp, ignore_errors=True)

    def own_clip(self, path: str) -> str | None:
        """A clip this app made (Show in folder / Copy act on nothing else), still on disk."""
        p = os.path.normcase(os.path.abspath(str(path or "")))
        with self.lock:
            ok = p in self.made
        return path if ok and os.path.isfile(path) else None
