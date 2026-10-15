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

The app only has Google's `drive.file` permission: it can see and change only the files it creates
(a "PCS Scans" folder in your Drive, shared read-only with the coach).

## Layout
- `scanner/` — the scanner (`pcs_scan.py`, `pcs_hud.py`) and its data files. Also runs from the command line:
  `python scanner/pcs_scan.py recording.mp4 --out results`
- `app/` — the Windows app (Tkinter) that wraps the scanner, Google sign-in, Drive upload and updates.
- `.github/workflows/build.yml` — builds `PCS-Scanner.exe` on every push; a `v*` tag publishes a release.

## Build settings (repository Settings → Secrets and variables → Actions)
- Secret `GOOGLE_OAUTH_CLIENT`: the Desktop OAuth client JSON from Google Cloud.
- Variable `COACH_EMAIL`: the Google account the players' "PCS Scans" folders are shared with.
