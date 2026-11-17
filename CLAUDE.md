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

The tests are plain scripts: each `test_*` function runs from `__main__` (`run_all`), prints `ok <name>`; a failure
doesn't stop the others: all failures are listed at the end and the script exits non-zero. A test that can't run
on this PC raises `Skipped` (test_ui.py: `test_single_instance` while another PCS Scanner runs). Add a test with every behaviour change; CI runs both files on Windows.

Build = `build.yml`. `gh workflow run build.yml --ref <branch>` gives a `dev-N` test exe as a run artifact.
A push to `main` builds too, and publishes release `v<VERSION>` if that tag doesn't exist yet. New modules need
a `--hidden-import` (or `--collect-submodules`) in `build.yml` and a line in the selftest import in `pcs_app.py`,
or the exe breaks while the tests pass.

**ffmpeg** (bundled in the exe) is pinned in `build.yml` (`FFMPEG_TAG`, `FFMPEG_ZIP`, `FFMPEG_SHA256`): a BtbN
FFmpeg-Builds master `win64-gpl` build, checked by SHA-256, so releases don't change ffmpeg by accident (the scanner
parses its `pts_time` output and relies on its GPU decoding). Pin a month-end build (`autobuild-YYYY-MM-<last day>-…`):
BtbN deletes daily builds after about two weeks, month-end ones stay. To bump:
`gh release list --repo BtbN/FFmpeg-Builds`, then
`gh api repos/BtbN/FFmpeg-Builds/releases/tags/<tag> --jq '.assets[] | select(.name|endswith("win64-gpl.zip")) | [.name, .digest]'`
gives the zip name and its `sha256:`; put all three in `build.yml`, and before merging scan a recording with the test
build and compare it with a scan from the previous build (same games and values).

Bump `VERSION` in `scanner/pcs_scan.py` once per release PR (currently 1.10.9 is released).

## How the pieces work (things that bit us)

- **OBS**: portable OBS driven over obs-websocket (port 4466). The encoder is read only at OBS start, so the
  encoder test restarts OBS per candidate; only encoders of GPU vendors actually present are tried (otherwise a
  "missing encoder" dialog blocks). AV1 only on RTX 40/50, RX 7000/9000, Arc. Other capture software running
  (Medal, etc.) can make recording start fail. Sources removed are awaited before being re-created.
- **Audio tracks**: 1 = Apex only (process capture), 2 = the voice app only (`voice_app`: Discord by default,
  TeamSpeak = classic `ts3client_win64.exe` and new `TeamSpeak.exe` both captured on track 2, or another program
  by exe name `voice_exe`; keys `discord_audio`, `hk_mute_discord`, track key `discord` and OBS input
  "PCS Discord" kept for compatibility), 3 = microphone (`mic_device`). A settings change that alters
  `config_signature` restarts OBS at the next Apex start.
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
- **Results to hub and Drive**: a recording is "uploaded" once the hub OR Drive took its results; where they went is
  kept per recording in `sent.json` (not in `recordings.json`: older versions refuse unknown fields there). Results
  that reached only Drive stay "Not on hub yet" and are sent to the hub on their own (`Recorder._retry_hub`: not
  while Apex runs; key refused = wait for a new key or address; 429 = Retry-After; network or hub errors = 1, 2, 4,
  8, then every 15 min). Recordings sent before `sent.json` existed: hub retried only if recorded in the last 3 days.
- **Silent voice / mic track**: after a recording of 5 min or more, tracks 2 and 3 (when on, with consent, not muted
  by hotkey) are read (audio only, `pcs_levels.track_db`); loudest half second under -60 dBFS = silent, told once
  per setting (`silence.json`).
- **Review audio tracks** (`pcs_recorder/tracks.py`): the review plays each track as its own `.trackN.m4a` (stream
  copy, all tracks in one read of the file). Cut in the background right after a recording ends (low priority, never
  while Apex runs; recordings of the last 24 h without tracks are queued again at start), or first in line when the
  review asks for a track that isn't there: `/audio/<id>/<n>` answers 503 "preparing" at once and the page asks again
  (a 3 h recording takes minutes to cut: the coach once heard no game sound because the request waited for it).
  Written as `.part.m4a`, renamed when complete; stale `.part` files removed at start.
- **Single instance**: named mutex + a local port to ask the running instance to show its window.
- **Hotkeys**: `RegisterHotKey`, only while recording. Letters/digits need a modifier; F-keys, numpad and a few
  others may be used alone (controller players).
- **Coach role**: unlocked when the signed-in Google account matches the build's coach address (a CI variable,
  never in the repo). Coach-only: importing recordings by hand.
- **Coach notes** (`pcs_recorder/notes.py`, hub 0.4 `/api/annotations`, contract in the hub's `AGENT.md`): notes and
  drawings at a moment of one player's recording, kept on the hub. A recording = its file name without extension +
  the player who sent its scan, so a recording needs its scan on the hub, and the coach annotates a player's file
  imported by hand with its original name (or his own recording of the game). The hub key's role (`me.role` in the
  answer), not the app's Coach role, decides who may write. Drawings are 0..1 coordinates of the video frame, drawn
  on a canvas fitted to the picture (`fitInk`), repainted only when the set of notes on screen changes. "Squad"
  notes show on teammates' recordings of the same game (`gameKey`) at the same time after the jump; drawings
  never. Last answer cached in `notes-cache/` for offline. Full screen is the player box (F / button), the video's
  own full-screen button is hidden, or the drawings would not show.
- **Scanner robustness**: never trust a single read. Ring clock via countdown consistency, harvesters only if
  seen in ≥ 25 % of map opens, feed lines matched by time AND names. 4:3 stretched recordings are supported.
- **Game end**: `games[].tEnd` is the last drop screen / legend select / full map frame (often = `tStart`), kept for
  compatibility. `tEndGame` (1.10.9+) = the last in-game HUD read (ring clock, squads counter, own strip, full map)
  before the next game's start; the debug overlay alone does not count (it also shows in the firing range). Ring
  phases first seen after the next game started are dropped from a game; a countdown ending after `tEndGame` has
  `endsAfterGame: true` (the player died before that ring time). `scannerBuild` says which build made a scan
  (`1.10.9+dev.52` for a test build, `+source` from source); `scanner` stays the plain version.
- **Scanner speed**: readers take the uint8 frame and make int16 only the small areas they compute on; per-pixel
  min / max over the colours use `pcs_hud.minmax` (channel planes), never `.min(2)` / `.max(2)` / `np.ptp(axis=2)`,
  which are about 8x slower. A speed change must give identical results: compare every reader old vs new on the
  region caches (0 differences) and the check set table.
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
  the legend a player is looking at before their turn: the last read with a coloured frame wins. The coloured frame
  and the player's own (coloured) name are found by pixels of the number badge's colour, never by a median hue over
  a window: the screen's background is often a saturated orange right behind them, and the hue test flipped with tiny
  pixel differences between the CPU and GPU decoders (7 Nov, one game: octane on CPU, pathfinder on GPU; the screen
  says octane). Look-alikes: the lobby's party list (rejected by position), odd single frames (rejected by duration),
  and the orange background filling the badge window at the wrong roster position (rejected by size). 16:9 only so far.
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
- **Own stats**: `pcs_hud.OwnStats` reads the recording player's kills, assists, knocks and damage from the strip under
  the squads counter. Nothing there is at a fixed place (plates come and go, the group shifts left as numbers grow,
  pubs has no knocks plate and smaller kills / assists digits): the strip is cut into white shapes (brighter than the
  plate behind them) and dark shapes on bright plates, and each number is found next to its icon (damage digits end at
  x = 2336). Digits are compared as grey 12 x 18 cells, each at its own contrast, with the shapes in `pcs_banner.json`
  (`strip`: per size 'S' 17 px, 'B' 22 px, 'K' knocks; 'X' = not a digit); a doubtful digit = no number. Kills need
  the skull icon left of them. `games[].own` = for each number the value of the last two reads of the game, which must
  agree (+ `...At`, `...LastRead`); none when values read twice went down, or when the number stopped being read
  > 10 s before the strip did. Damage often grows in the last second of a game (final fight), read once: by the
  coach's decision (16 Nov 2026) that last read counts when it is above every earlier read of the game and at most
  `SINGLE_JUMP` (250) above the read before it (largest real rise between two reads seen: 178); then
  `damageSingleRead` is true. Kills, assists and knocks keep the two-read rule (no last-second single reads to check
  the rule on). Reads taken while spectating are dropped. Check set (dev and held-out games): no wrong read; damage
  totals right in 15 of 15 games (5 rest on a single read). A misread units or tens digit that comes out higher
  would still pass the single-read rule: it relies on the digit test (no misread seen in ~9,000 reads).
  The previous "damage misreads" at game endings (355/375, 262/297, 1123/1151) were real: the number grew 1 s later.
- **Sound cues**: correlation on mel spectrograms and Shazam-style hashing both failed in the 3D game mix.
  What works: narrowband tone strength against its ±30 Hz neighbourhood (ring alarm = 374 + 533 Hz pulses).

## Developer tools (`tools/`, not in the exe)

- **Region cache** (`tools/region_cache.py`): decodes a recording once with the scanner's own `keyframes()` and keeps,
  per keyframe, the five small boxes the readers look at (`BOXES`: debug overlay, top-left block for legend select /
  banner / ring clock, rest of the banner words, top-right counters, kill feed), as lossless WebP (bit-identical: lossy
  formats were tried and changed reader results). `build "<rec>" --verify` (about 1.5 GB per hour of recording),
  then `run "<rec>" --readers squads,own --per game` re-runs readers in seconds to a minute; `verify` compares with the
  reads taken on the real frames. `Cache(dir).frames()` gives full-size frames (black outside the boxes), so readers
  run unchanged. Cache folder: `--cache` or `PCS_REGION_CACHE` (the coach's: `D:\tools\pcs-test-exe\region-cache`).
  It is game footage: never in the repo. 16:9 recordings only. Caches built before 16 Nov 2026 cut the counters box
  at x = 1990, which hides the skull of ranked kills: OwnStats leaves those kills unread on them (real frames are fine).
- **Check set** (`tools/check_set.py score <file.json>`): truths read by eye at given times (squads left, kills,
  assists, knocks, damage, legends, mode, map), scored against the readers on the region cache: per field, truths /
  read / correct at the nearest keyframe and within 3 s, misses listed. Format in the module and
  `tools/checkset_example.json`; the real set references private recordings and lives in the handoff folder.
  Build a new reader against it, and add a truth only when the value was read on an extracted frame. Truths carry a
  `split` (`dev` / `held`, whole games, decided before tuning): tune on `--split dev`, report `--split held` too.

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
