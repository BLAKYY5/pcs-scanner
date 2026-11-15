# PCS Scanner: notes for Claude working on this repo

Windows app for the Apex Legends team Project Conquerors (PCS), coached by Blakey. It records Apex with its own
private OBS, scans the recordings (drop, landing, rings, harvesters, kill feed, sound cues), sends the results to
the team's Drive or hub, has a review player, and uploads scrim blocks to the team's YouTube channel.

Blakey is the coach and the only person who decides what ships. He is a capable Python reader but not the one
writing this code: explain outcomes in plain words, not internals.

## Rules that never bend

1. **Releases only on Blakey's word.** A push to `main` with a new `VERSION` publishes a release that every
   player's app installs. Work on a branch, open a PR, run the Windows build on the branch, and merge only when
   Blakey says "push the update" (or equivalent). Docs-only merges don't release (same version = test build).
2. **Competitive integrity.** Never use LiveAPI or an observer slot in official scrim lobbies, and never use any
   live information from a lobby the team is playing. Analysis is after the fact, from the team's own recordings.
3. **EEC (eternalesports.club) forbids bots and scrapers.** The app must never fetch the EEC site. EEC data only
   comes from the export file a user downloaded, a link a user pasted, or the team hub.
4. **Game files.** Reading asset files at rest (RSX exports: images, text, sounds, datatables) is OK for private
   use. Never decompile or patch the game executable, never touch the running game's memory (anti-cheat ban
   risk), and never commit extracted or recorded game assets: this repo is public. Derived numbers (a tone
   frequency, a template hash, a glyph table we built) are fine; EA's files are not.
5. **Privacy.** No personal emails, tokens or `client.json` in the repo. Discord and mic are recorded only after
   the player confirmed team consent in Settings (`comms_consent`).
6. **Only delete what the app made.** Retention and "delete" act on the app's own recordings (to the recycle bin),
   never on files a user added by hand.

## Layout

```
scanner/pcs_scan.py     the video scanner (CLI + library). VERSION lives here: the app's version.
scanner/pcs_hud.py      HUD reading helpers (ring clock, layout 16:9 / 4:3 stretched, glyphs)
scanner/pcs_audio.py    sound cues on the game track (ring-moving alarm so far)
scanner/pcs_maps.json, pcs_fonts.json, rank_rp.npy   reference data the scanner ships with
app/pcs_app.py          entry point: single instance, tray, window (pywebview), Tk toasts, updater finish
app/pcs_core.py         paths, settings, Google Drive / YouTube sign-in, update check + install, run_scan
app/pcs_server.py       localhost HTTP API + static web UI (token-protected), Backend class
app/pcs_levels.py       loudness per track, fight and mic-peak detection for the review player
app/pcs_recorder/       auto-record: settings, obs (portable OBS + obs-websocket), core (Recorder),
                        runtime (AutoRecorder, hotkeys, toasts), retention, youtube (blocks, queue, upload),
                        eec (results from export/hub), night (night uploads, shutdown), labels (session types)
app/web/                the UI: index.html, app.css, app.js (no build step, no framework)
tests/test_recorder.py  recorder, youtube, eec, labels, audio, encoder logic (fake OBS and game)
tests/test_ui.py        server API, settings, scan queue, coach role, updater, single instance
.github/workflows/build.yml   Windows build, tests, PyInstaller onefile exe, selftest, release
```

Data on a player's PC: `%LOCALAPPDATA%\PCS Scanner\` (`app.log`, `recorder.json`, `youtube.json`, `labels.json`,
`update.log`, the portable OBS). `PCS_DATA_DIR` overrides it (tests use this).

## Run, test, build

```
pip install -r app/requirements.txt          # Python 3.12
python tests/test_recorder.py                # needs ffmpeg + ffprobe on PATH for some tests
python tests/test_ui.py
python scanner/pcs_scan.py "<recording>" --out <dir> [--cpu] [--snaps]     # scan one file by hand
python app/pcs_app.py                        # run the app from source (Windows)
```

The tests are plain scripts: each `test_*` function runs from `__main__`, prints `ok <name>`, exits non-zero on
failure. Add a test with every behaviour change; CI runs both files on Windows.

Build = `build.yml`. `gh workflow run build.yml --ref <branch>` gives a `dev-N` test exe as a run artifact.
A push to `main` builds too, and publishes release `v<VERSION>` if that tag doesn't exist yet. New modules need
a `--hidden-import` (or `--collect-submodules`) in `build.yml` and a line in the selftest import in `pcs_app.py`,
or the exe breaks while the tests pass.

Bump `VERSION` in `scanner/pcs_scan.py` once per release PR (currently 1.10.7 is released).

## How the pieces work (things that bit us)

- **OBS**: portable OBS driven over obs-websocket (port 4466). The encoder is read only at OBS start, so the
  encoder test restarts OBS per candidate; only encoders of GPU vendors actually present are tried (otherwise a
  "missing encoder" dialog blocks). AV1 only on RTX 40/50, RX 7000/9000, Arc. Other capture software running
  (Medal, etc.) can make recording start fail. Sources removed are awaited before being re-created.
- **Audio tracks**: 1 = Apex only (process capture), 2 = Discord only, 3 = microphone (`mic_device`). A settings
  change that alters `config_signature` restarts OBS at the next Apex start.
- **Recording file**: OBS writes mkv, then remuxes to mp4. Use the mp4 only when it's ≥ 0.98 × the mkv size and
  stable, else the mkv. A half-written mp4 once produced a "0 games" scan.
- **Scan validity**: `read_nothing` (no layout votes, no games) = failed scan, retry. `no_game` needs the video
  to have been read, no games and no feed lines; those recordings are deleted first by retention.
- **Game mode**: the ranked badge share alone is unreliable. A scrim needs proof: the scrim drop screen (all 20
  spots). Labels (scrims / ranked / pubs / wildcard) come from the scan or are set by hand. Wildcard and pubs
  come from the loading banner's mode word (pubs: trios only, word TRIOS); duos or other playlists show another
  word and stay `unknown` (with `banner.modeSig`) until that word is named in the table.
- **YouTube**: blocks = recordings less than `yt_gap_min` apart, one video per block per player, ffmpeg concat
  with stream copy, chapters per game. State in `youtube.json` keyed by the block's first recording.
  `ready_blocks()` has side effects (marks skipped, asks for EEC once); `queue()` is the read-only view.
  Uploads pause while Apex runs; night mode gates them unless the block was requested by hand.
- **Updater**: downloads `PCS-Scanner.update.exe`, spawns it detached with `--finish-update`, which waits for
  the old process, copies itself over the old exe and restarts. Versions ≤ 1.10.1 need one manual install.
  Dev builds are offered releases ≥ their `base`.
- **Single instance**: named mutex + a local port to ask the running instance to show its window.
- **Hotkeys**: `RegisterHotKey`, only while recording. Letters/digits need a modifier; F-keys, numpad and a few
  others may be used alone (controller players).
- **Coach role**: unlocked when the signed-in Google account matches the build's coach address (a CI variable,
  never in the repo). Coach-only: importing recordings by hand.
- **Scanner robustness**: never trust a single read. Ring clock via countdown consistency, harvesters only if
  seen in ≥ 25 % of map opens, feed lines matched by time AND names. 4:3 stretched recordings are supported.
- **Kill-feed weapons**: `pcs_hud.Weapons` compares each new feed line, at full size, with the game's own weapon
  icons (the feed draws them mirrored) and adds `weapon`, `weaponName`, `weaponScore`, `arrow`, `event` to the line
  (red arrow = knock, none = kill). The icons are a private file, `pcs_weapons.npz`: git-ignored, never bundled,
  built on the coach's PC. Players get it through Settings → Team link (a Google Drive "Anyone with the link"
  file, re-checked every 6 hours, saved in the data folder). `drive.file` sign-in can't read a file someone else
  shared, which is why it is a pasted link. No file = no weapon names, scan otherwise unchanged.
- **Legend select screen**: `pcs_hud.Legends` recognises the squad roster at the top left before a game (ranked:
  rank badges left of the slots; other modes: no badges, roster 114 px further left). A screen seen for 8 s to
  2 min starts a game (`games[].select`: `tFirst`, `tLast`, `ranked`, `squad[]` with `slot`, `legend`, `self`,
  `hue`), so games no longer merge when nobody opens the map. Legend names need the portraits, stored in the same
  private file as the weapon icons under `legend_<name>`; without them the screen still starts a game. A slot shows
  the legend a player is looking at before their turn: the last read with a coloured frame wins. Look-alikes: the
  lobby's party list (rejected by position) and odd single frames (rejected by duration). 16:9 only so far.
- **Loading banner**: `pcs_hud.Banner` reads the mode word and the map word shown at the top left while players
  connect, by letter shape against `scanner/pcs_banner.json` (a table built from our own recordings, English
  only: ranked, wildcard, pubs (the word TRIOS of pubs trios); SP, ED, BM so far). The table's mode keys are the
  scanner's mode values, not the on-screen words. It gives `games[].banner`, the map when nobody opens it, and the
  `wildcard` / `pubs` mode and label. An unknown word is kept as `modeSig` / `mapSig`: name it in the table to
  teach it (pubs duos and other playlists are still unknown).
- **Squads left**: `pcs_hud.SquadsLeft` reads the number in front of "SQUADS LEFT" (top right) once a second,
  digit shapes in `pcs_banner.json` (`digits`). `games[].squadsLeft` = `[[t, n], ...]`, each lower count the first
  time it was read twice; the count never goes up inside a game. The last entry while the squad is alive is close
  to the placement. 16:9 only.
- **Own stats**: `pcs_hud.OwnStats` reads the recording player's kills, assists and knocks from the strip under the
  squads counter (same digit table; knocks are dark digits on a white plate). `games[].own` = for each, the value
  shown most among the last 25 reads of the game (+ `...At`, `...LastRead`); reads taken while spectating are
  dropped. The numbers only grow, so a value whose last read is long before the game's end is a floor, not the total. Damage is
  in the same strip but not read: its 17 px digits were misread too often at the end of games (see the class note).
- **Sound cues**: correlation on mel spectrograms and Shazam-style hashing both failed in the 3D game mix.
  What works: narrowband tone strength against its ±30 Hz neighbourhood (ring alarm = 374 + 533 Hz pulses).

## Conventions

- Commit messages: a plain summary line, then what and why. PRs: what changed for the players, how it was tested.
- UI text is short and literal ("Upload now", "Skip"). No jargon in anything a player reads.
- Keep the web UI dependency-free. Check new UI at 1320 px and 900 px wide.
- Results the hub or the Loot Book consume (`*.scan.json`, `*.feed.json`) are a contract: add fields, don't
  rename or remove them without checking the hub's importer.

## Where the rest of the context lives

The private handoff folder on Blakey's PC (`D:\tools\pcs-handoff\`) has the project notes that can't be public:
`HANDOFF.md` (state of the work, roadmap, what to do next) and the reference notes on the HUD, sound cues, RSX
datatables, LiveAPI and the team hub. Read `HANDOFF.md` first.
