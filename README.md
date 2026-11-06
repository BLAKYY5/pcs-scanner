# PCS Scanner

Scans Apex Legends recordings for Project Conquerors (PCS): drop spots, landing, ring timings, EVO harvesters,
ranked/scrim mode and the kill feed. Results upload to the team's Google Drive and are imported into the
Conquerors Loot Book overnight.

## For players

> **Download the app here: [PCS-Scanner.exe](https://github.com/BLAKYY5/pcs-scanner/releases/latest/download/PCS-Scanner.exe)**
> Do NOT use the green "Code → Download ZIP" button: that is the source code, not the app, and it will not work.

1. Download `PCS-Scanner.exe` with the link above (always the newest version; the app updates itself afterwards).
2. Open it, type your EEC name (Home page), then **Settings → Sign in with Google** once.
3. On **Home**, turn on **Auto-record**. That's it. **Start with Windows** is on by default, so the app is always
   waiting in the tray (red dot, bottom right); closing the window keeps it there, **Quit** is in the tray menu.

The window (1.9+) has four parts:
- **Home**: is it recording, ready or scanning; the two switches; storage used; Google status; recent activity.
- **Recordings**: every recording and its scan status. **Add recordings…** or drop files/folders on the window
  (VODs too), tick several and **Scan selected**. Scans wait by themselves while Apex is running.
- **Review**: plays a recording with the scan on a timeline under it: games, drop map, jump, landing, ring
  timers and every kill-feed line. Click a marker (or an event on the right) to jump there. Keys: Space, ←/→ 5 s,
  J/L 10 s, `,`/`.` one frame, `[`/`]` previous/next event, F full screen, M mute. Tabs: events, kill-feed images,
  drop spots of all squads.
- **Settings**: name, role, Google, start with Windows, auto-record limits and quality, team hub address.

### Auto-record (1.8+): never forget to record
The first time it's turned on, it downloads its own copy of OBS (your own OBS, if you have one, is never touched)
and tests your graphics card for a few seconds. From then on:

- **Apex starts** → records the Apex window only (no microphone, no Discord, nothing else on screen).
- **During the game** → nothing else: no overlay, no pop-up, no upload.
- **Apex closes** → a small window: "Scanning starts in 60 s" with **Scan now** / **Skip**. The scan runs at
  low priority and pauses by itself if you start Apex again. Only the results are sent, never the video.
- **Your limits** (Settings): keep recordings N days (default 7) and a maximum folder size
  (default 100 GB, oldest deleted first). Only recordings the app made are ever deleted.
- **Sound on separate tracks**: track 1 = Apex only, track 2 = Discord only, track 3 = your microphone. Discord
  and mic are recorded only after you confirm in Settings that your team agreed (recording a call records
  everyone in it). Review plays each track with its own volume and mute. Your mic track is sent with the scan
  results for the comms review (can be turned off).
- **Hotkeys while recording** (only active while Apex is recorded): Ctrl+Alt+M marks a moment for the review,
  Ctrl+Alt+1/2/3 mute or unmute game / Discord / mic. A short beep confirms. All can be changed in Settings.
- **Video quality**: constant bitrate, default 12 Mbit/s (about 5.4 GB per hour at 1440p60), from 8 to 60.
  8 is the lowest that keeps the HUD readable for the scanner; Twitch VODs are usually 8-10.

The app only has Google's `drive.file` permission: it can see and change only the files it creates
(a "PCS Scans" folder in your Drive, shared read-only with the coach).

## Layout
- `scanner/` — the scanner (`pcs_scan.py`, `pcs_hud.py`) and its data files. Also runs from the command line:
  `python scanner/pcs_scan.py recording.mp4 --out results`
- `app/` — the Windows app. `pcs_app.py` starts it (native window via pywebview/Edge WebView2, tray, scan mode),
  `pcs_core.py` has paths, settings, Google Drive, updates; `pcs_server.py` is the local server the window talks
  to (127.0.0.1, random port and token); `web/` is the interface. API tests: `python tests/test_ui.py`.
- `app/pcs_recorder/` — Auto-record: Apex detection, private OBS driven over obs-websocket, scan prompt,
  folder limits, uploads to the team hub and/or Drive. Logic tests: `python tests/test_recorder.py`.
- `.github/workflows/build.yml` — builds `PCS-Scanner.exe` on every push; a `v*` tag publishes a release.

## Build settings (repository Settings → Secrets and variables → Actions)
- Secret `GOOGLE_OAUTH_CLIENT`: the Desktop OAuth client JSON from Google Cloud.
- Variable `COACH_EMAIL`: the Google account the players' "PCS Scans" folders are shared with.
