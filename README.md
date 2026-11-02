# PCS Scanner

Scans Apex Legends recordings for Project Conquerors (PCS): drop spots, landing, ring timings, EVO harvesters,
ranked/scrim mode and the kill feed. Results upload to the team's Google Drive and are imported into the
Conquerors Loot Book overnight.

## For players

> **Download the app here: [PCS-Scanner.exe](https://github.com/BLAKYY5/pcs-scanner/releases/latest/download/PCS-Scanner.exe)**
> Do NOT use the green "Code → Download ZIP" button: that is the source code, not the app, and it will not work.

1. Download `PCS-Scanner.exe` with the link above (always the newest version; the app updates itself afterwards).
2. Open it, type your EEC name, pick your role, press **Sign in with Google** once.
3. **Add recordings…** → **Scan and upload**.

### Auto-record (1.8+): never forget to record
Press **Auto-record…** in the app, tick **Auto-record Apex**, Save. The first time it downloads its own copy of
OBS (your own OBS, if you have one, is never touched) and tests your graphics card for a few seconds.
From then on PCS Scanner starts with Windows in the tray (red dot, bottom right) and:

- **Apex starts** → records the Apex window only (no microphone, no Discord, nothing else on screen).
- **During the game** → nothing else: no overlay, no pop-up, no upload.
- **Apex closes** → a small window: "Scanning starts in 60 s" with **Scan now** / **Skip**. The scan runs at
  low priority and pauses by itself if you start Apex again. Only the results are sent, never the video.
- **Your limits** (Auto-record… window): keep recordings N days (default 7) and a maximum folder size
  (default 100 GB, oldest deleted first). Only recordings the app made are ever deleted.
- **Video quality**: constant bitrate, default 12 Mbit/s (about 5.4 GB per hour at 1440p60), from 8 to 60.
  8 is the lowest that keeps the HUD readable for the scanner; Twitch VODs are usually 8-10.

The app only has Google's `drive.file` permission: it can see and change only the files it creates
(a "PCS Scans" folder in your Drive, shared read-only with the coach).

## Layout
- `scanner/` — the scanner (`pcs_scan.py`, `pcs_hud.py`) and its data files. Also runs from the command line:
  `python scanner/pcs_scan.py recording.mp4 --out results`
- `app/` — the Windows app (Tkinter) that wraps the scanner, Google sign-in, Drive upload and updates.
- `app/pcs_recorder/` — Auto-record: Apex detection, private OBS driven over obs-websocket, scan prompt,
  folder limits, uploads to the team hub and/or Drive. Logic tests: `python tests/test_recorder.py`.
- `.github/workflows/build.yml` — builds `PCS-Scanner.exe` on every push; a `v*` tag publishes a release.

## Build settings (repository Settings → Secrets and variables → Actions)
- Secret `GOOGLE_OAUTH_CLIENT`: the Desktop OAuth client JSON from Google Cloud.
- Variable `COACH_EMAIL`: the Google account the players' "PCS Scans" folders are shared with.
