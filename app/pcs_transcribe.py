"""Comms transcript of one player's own mic track (the "Transcripts" component, not in the main exe).

Runs in its own process, started by the app (`pcs_recorder.comms`) after a session, only for a player who said yes
to transcripts, never while Apex runs (the app stops this process when Apex starts; finished parts are kept and the
next run goes on from there). Below-normal priority. Needs faster-whisper (CTranslate2 on the CPU, int8) and a
Whisper model folder (large-v3-turbo, downloaded by the app with a SHA-256 check); not bundled in PCS-Scanner.exe
because it adds ~200 MB that every start would unpack.

    pcs_transcribe.py RECORDING --stream N --ffmpeg PATH --model DIR --out FILE.comms.json --speaker NAME
                      [--language auto|fr|en|...] [--prompt TEXT] [--threads N] [--device cpu|cuda]

--device cuda (the team server's NVIDIA GPU, coach's decision 20 Nov 2026): float16 on the GPU; needs CTranslate2's
CUDA 12 libraries (pip nvidia-cublas-cu12, nvidia-cudnn-cu12 in the same Python: their DLL folders are added here).

Reads audio stream N of the recording (the mic track: one voice, the player's own) through ffmpeg as 16 kHz mono,
cuts it into parts of about PART_S seconds at a quiet moment, and writes FILE.comms.json:

    {"v": 1, "kind": "comms", "recording", "speaker", "track": "mic", "model", "language", "languageProb",
     "audioSeconds", "speechSeconds", "seconds" (wall), "cpuSeconds", "threads", "device", "computeType", "createdAt",
     "transcriber",
     "segments": [{"start", "end" (recording seconds, as scan.json), "text", "speaker", "p" (avg log prob)}]}

Progress: one JSON line per finished part on stdout ({"done": s, "total": s}). Finished parts are kept in
FILE.comms.partial.json until the whole track is done. Exit 0 = written, 2 = bad input, 3 = no faster-whisper.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

FORMAT = 1
RATE = 16000
PART_S = 600                 # transcribe in parts of about 10 min (memory, and a stop loses at most one part)
QUIET_LOOK_S = 30            # cut a part at the quietest half second in its last 30 s
MODEL_NAME = "large-v3-turbo"

# Apex words given to Whisper as the start of the text it continues: it then spells them the game's way. Kept short:
# a long list made the model invent lists of names on unclear audio (trial, 18 Nov 2026). Map POIs are added by the app.
PROMPT = ("Apex Legends comms. Wattson, Pathfinder, Bangalore, Bloodhound, Seer, Valkyrie, Newcastle, Loba, Crypto, "
          "Sparrow. Nemesis, Volt, CAR, Havoc, Prowler, Longbow, Sentinel, Kraber. One shot, cracked, knock, full, "
          "batts, cells, ult, Evo, zone, ring.")

# Spellings Whisper keeps getting wrong (whole words, case kept from the table).
FIXES = [(r"\bWatson\b", "Wattson"), (r"\bNorthpad\b", "North Pad"), (r"\bLaunchpad\b", "Launch Pad"),
         (r"\bPassfinder\b", "Pathfinder"), (r"\bLong beau\b", "Longbow"), (r"\bTraber\b", "Kraber")]

# Lines Whisper writes on silence or noise (subtitle credits it learnt from films). Never something a player says.
HALLUCINATIONS = re.compile(
    r"sous-titr|merci d'avoir regard|abonnez-vous|amara\.org|thanks for watching|subtitles by|"
    r"^\W*(merci|thank you)\W*$|^\W*$", re.I)


def fix_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    for pat, rep in FIXES:
        text = re.sub(pat, rep, text, flags=re.I)
    return text


def keep_segment(text: str, p: float | None, nospeech: float | None, prev: list[str]) -> bool:
    """False for Whisper's known filler lines, low-confidence lines on silence, and the same line repeated over and
    over (a loop on noise)."""
    if HALLUCINATIONS.search(text):
        return False
    if nospeech is not None and p is not None and nospeech > 0.6 and p < -1.0:
        return False
    if len(prev) >= 2 and prev[-1] == text and prev[-2] == text:
        return False
    return True


def read_pcm(ffmpeg: str, path: str, stream: int):
    """The whole audio stream as 16 kHz mono int16 (about 115 MB per hour)."""
    import numpy as np
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-i", path, "-map", f"0:a:{stream}", "-ac", "1", "-ar", str(RATE),
           "-f", "s16le", "-"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    p = subprocess.run(cmd, capture_output=True, creationflags=flags)
    if p.returncode != 0:
        raise ValueError(f"ffmpeg could not read audio track {stream}: {p.stderr.decode(errors='replace')[-300:]}")
    return np.frombuffer(p.stdout, dtype=np.int16)


def parts(pcm, part_s: int = PART_S, look_s: int = QUIET_LOOK_S) -> list[tuple[int, int]]:
    """[start, end) sample ranges of about part_s seconds, each cut at the quietest half second of its last look_s
    seconds (so a part rarely ends inside a word)."""
    import numpy as np
    n, out, a = len(pcm), [], 0
    step, half = part_s * RATE, RATE // 2
    while a < n:
        b = a + step
        if b >= n - look_s * RATE:              # the rest is short: one last part
            out.append((a, n))
            break
        lo = b - look_s * RATE
        win = np.abs(pcm[lo:b].astype(np.int32))
        k = len(win) // half
        loud = win[:k * half].reshape(k, half).mean(axis=1)
        cut = lo + int(np.argmin(loud)) * half + half // 2
        out.append((a, cut))
        a = cut
    return out


def low_priority() -> None:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)  # below normal
        except Exception:
            pass
    else:
        try:
            os.nice(5)
        except OSError:
            pass


def _no_av() -> None:
    """faster-whisper imports PyAV and huggingface_hub at start but needs neither here (ffmpeg decodes, the app
    downloads the model): stand-ins keep ~70 MB out of the component."""
    import types
    for name in ("av", "huggingface_hub"):
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = types.ModuleType(name)


def cuda_dlls() -> list[str]:
    """The CUDA libraries from pip (site-packages/nvidia/<lib>/bin) where CTranslate2 finds them on Windows."""
    import glob
    import site
    found = []
    for sp in list(site.getsitepackages()) + [site.getusersitepackages()]:
        for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
            if os.path.isdir(d) and d not in found:
                found.append(d)
    for d in found:
        try:
            os.add_dll_directory(d)
        except (AttributeError, OSError):
            pass
    if found:
        os.environ["PATH"] = os.pathsep.join(found + [os.environ.get("PATH", "")])
    return found


def cpu_seconds() -> float:
    t = os.times()
    return t.user + t.system


def transcribe(args) -> dict:
    low_priority()
    _no_av()
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        print(json.dumps({"error": f"faster-whisper missing: {e}"}), flush=True)
        sys.exit(3)
    import numpy as np
    t0, c0 = time.time(), cpu_seconds()
    pcm = read_pcm(args.ffmpeg, args.recording, args.stream)
    total = round(len(pcm) / RATE, 2)
    partial_f = args.out[:-len(".comms.json")] + ".comms.partial.json" if args.out.endswith(".comms.json") else args.out + ".partial"
    ident = {"recording": os.path.basename(args.recording), "size": os.path.getsize(args.recording),
             "stream": args.stream, "model": MODEL_NAME, "samples": len(pcm), "language": args.language}
    done: dict = {}
    try:
        with open(partial_f, encoding="utf-8") as f:
            old = json.load(f)
        if old.get("ident") == ident:
            done = old.get("parts") or {}
    except (OSError, ValueError):
        pass
    threads = args.threads or max(2, (os.cpu_count() or 4) // 2)
    if args.device == "cuda":
        cuda_dlls()
        compute = args.compute_type or "float16"
        model = WhisperModel(args.model, device="cuda", compute_type=compute, cpu_threads=min(threads, 4))
    else:
        compute = args.compute_type or "int8"
        model = WhisperModel(args.model, device="cpu", compute_type=compute, cpu_threads=threads)
    lang = None if args.language in ("", "auto") else args.language
    prompt = args.prompt or PROMPT
    for i, (a, b) in enumerate(parts(pcm)):
        key = f"{i}:{a}"
        if key in done:
            continue
        audio = pcm[a:b].astype(np.float32) / 32768.0
        segs, info = model.transcribe(audio, language=lang, beam_size=5, vad_filter=True,
                                      vad_parameters={"min_silence_duration_ms": 500},
                                      condition_on_previous_text=False, initial_prompt=prompt)
        out, prev = [], []
        for s in segs:
            text = fix_text(s.text)
            if not keep_segment(text, s.avg_logprob, s.no_speech_prob, prev):
                continue
            prev.append(text)
            out.append({"start": round(a / RATE + s.start, 2), "end": round(a / RATE + s.end, 2), "text": text,
                        "p": round(s.avg_logprob, 3)})
        done[key] = {"segments": out, "language": info.language, "languageProb": round(info.language_probability, 3),
                     "speech": round(info.duration_after_vad, 2)}
        if lang is None and info.language_probability >= 0.8 and info.duration_after_vad > 20:
            lang = info.language            # the rest of the track in the language heard clearly in this part
        tmp = partial_f + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ident": ident, "parts": done}, f, ensure_ascii=False)
        os.replace(tmp, partial_f)
        print(json.dumps({"done": round(b / RATE, 1), "total": total}), flush=True)
    langs: dict = {}
    for p in done.values():
        langs[p["language"]] = langs.get(p["language"], 0) + p["speech"]
    language = max(langs, key=langs.get) if langs else None
    segments = [dict(s, speaker=args.speaker) for k in sorted(done, key=lambda k: int(k.split(":")[0]))
                for s in done[k]["segments"]]
    res = {"v": FORMAT, "kind": "comms", "recording": os.path.splitext(os.path.basename(args.recording))[0],
           "speaker": args.speaker, "track": "mic", "model": MODEL_NAME, "language": language,
           "languageProb": max((p["languageProb"] for p in done.values() if p["language"] == language), default=None),
           "audioSeconds": total, "speechSeconds": round(sum(p["speech"] for p in done.values()), 1),
           "seconds": round(time.time() - t0, 1), "cpuSeconds": round(cpu_seconds() - c0, 1), "threads": threads,
           "device": args.device, "computeType": compute, "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "transcriber": args.version, "segments": segments}
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=0)
    os.replace(tmp, args.out)
    try:
        os.remove(partial_f)
    except OSError:
        pass
    return res


def selftest() -> int:
    """The component's libraries load and the voice-activity model is inside (build check)."""
    _no_av()
    try:
        import ctranslate2
        import onnxruntime  # noqa: F401
        from faster_whisper.vad import get_vad_model
        get_vad_model()
    except Exception as e:
        print(f"FAIL {e.__class__.__name__}: {e}", flush=True)
        return 1
    print(f"OK ctranslate2 {ctranslate2.__version__}", flush=True)
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--selftest"]:
        return selftest()
    ap = argparse.ArgumentParser(description="Comms transcript of one mic track")
    ap.add_argument("recording")
    ap.add_argument("--stream", type=int, required=True, help="audio stream index (0 = first audio track)")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--model", required=True, help="Whisper model folder (CTranslate2 format)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--speaker", default="")
    ap.add_argument("--language", default="auto")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--version", default="")
    ap.add_argument("--device", default="cpu", choices=("cpu", "cuda"), help="cuda = NVIDIA GPU (team server)")
    ap.add_argument("--compute-type", default="", help="CTranslate2 compute type (default int8 on cpu, float16 on cuda)")
    args = ap.parse_args(argv)
    if not os.path.isfile(args.recording) or not os.path.isdir(args.model):
        print(json.dumps({"error": "recording or model folder missing"}), flush=True)
        return 2
    try:
        res = transcribe(args)
    except ValueError as e:
        print(json.dumps({"error": str(e)}), flush=True)
        return 2
    print(json.dumps({"finished": True, "segments": len(res["segments"]), "seconds": res["seconds"]}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
