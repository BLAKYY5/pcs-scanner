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
app/web/                the UI: index.html, app.css, app.js, session.js/.css (Watch together) (no build step, no framework)
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

Bump `VERSION` in `scanner/pcs_scan.py` once per release PR (currently 1.10.14 is released).

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
  spots). Labels (scrims / ranked / pubs / wildcard) come from the scan or are set by hand (by hand always wins).
  Per game, `games[].modeSeen` (`pcs_scan.mode_seen`) = {mode, from: the signs that named it (badge = RP panel share,
  drop, select = rank badges at legend select, banner = loading banner word, emblem), conflict, emblem (asset id),
  emblemReads}; `games[].mode` = its mode or `unknown`. Order kept from before: RP panel -> ranked, drop screen ->
  scrim; then the other signs must agree. Two signs naming different modes, or the emblem of a playlist we can't
  name, = no mode (unlabelled), never a guess (an unnamed emblem is ignored next to ranked
  signs: rank badges are round too). Wildcard and pubs come from the loading banner's word (pubs: trios
  only, word TRIOS) or the playlist emblem (`pcs_hud.ModeEmblem`; pubs duos likely shows the same "unranked"
  emblem: not seen yet). The block label (`labels.explain_block`) takes scrims, ranked, wildcard, pubs in that order.
- **Playlist emblem** (`pcs_hud.ModeEmblem`, every `EMBLEM_EVERY` = 3 s, ~3 ms a read, 16:9 only): the round emblem at
  the top right (x 2422..2539, y 8..123 at 1440p in a game; 134 px lower on the "waiting for players" screen). Pubs =
  red "unranked" emblem, Wildcard = teal; ranked shows a rank badge there instead. Compared as 16 x 16 colour cells on
  the cells the emblem fully covers with `pcs_banner.json` `emblems` (derived from the game's own emblem images by
  `tools/mode_emblems.py` from a private RSX export of ui.rpak; the images are never in the repo; 7 more round
  playlist emblems are in the table with mode null so their games are not taken for pubs). Per game: the reads from
  150 s before its start to 150 s before the next; one emblem >= 3 reads and >= 90 % of them.
  Checked 19 Nov 2026 on full scans of 9 recordings (38 ranked, 4 Wildcard, 4 pubs trios games, read by eye; dev =
  13 Nov 17.24 + 15 Nov, held = the rest): every game's mode right, none unlabelled; the emblem alone named all 8
  Wildcard / pubs games (69 to 369 reads each, never the other one) and was read once in 38 ranked games.
- **YouTube**: blocks = recordings less than `yt_gap_min` apart, one video per block per player, ffmpeg concat
  with stream copy, chapters per game. State in `youtube.json` keyed by the block's first recording.
  `ready_blocks()` has side effects (marks skipped, asks for EEC once); `queue()` is the read-only view.
  Uploads pause while Apex runs; night mode gates them unless the block was requested by hand.
- **EEC drops** (`eec.py`, coach's rule 18 Nov 2026: "EEC is correct 95% of the time"): drops come from the export's
  `draft` joined to each row's `map_pick_id` (as hub/eec.py), or from hub documents imported with the fixed drops
  (`draft` present; old Loot Book drops are not used). `eec.check_block` compares a block's scanned games with the
  lobby: same map, jump within 15 min of EEC's match start, EEC drop vs our landing POI (`same` / `near` /
  `different`; a drop not in our map table is not compared). More agree = `match`: label scrims, source `eec`. More
  differ, or no game at the lobby's times = `mismatch`: the lobby is not attached to that block (e.g. a tournament
  the same evening), the label comes from the scan alone, the reason is kept (`eec_rejected`, `label_why`). Nothing
  to compare = old behaviour. Display: EEC's drop wins, ours is shown next to it when different ("scan: ...").
- **Updater**: downloads `PCS-Scanner.update.exe`, spawns it detached with `--finish-update`, which waits for
  the old process, copies itself over the old exe and restarts. Versions ≤ 1.10.1 need one manual install.
  Dev builds are offered releases ≥ their `base`. The download must be an https address of this repo's releases
  (redirects: GitHub's file hosts, https only) and match the SHA-256 GitHub lists for the file (`digest`); a release
  without a digest gets the size check only. The restart is in the tray (`--background --updated`: a corner
  message says so, the app never closes itself) unless Update was pressed in the open window (`--show`).
  While it runs, `Backend.update_state` (`updateState` in `/api/state`: step download / check / restart / failed,
  bytes done / total, error) drives a full-window "Updating PCS Scanner" screen that locks the whole UI (also when
  the window is opened or reloaded mid-update), and the tray's Update item is greyed "Updating…". One download at a
  time (`_update_lock`); failed = the reason + Back (`/api/update/back`).
- **Quiet window** (a slower Apex session, 17 Nov 2026): WebView2 does not stop drawing when its window goes to the
  tray. A hidden window with the old pulsing dots drew 358 frames/s on a 360 Hz screen (~70 % of a CPU core plus GPU)
  for whole sessions. Now: no endless CSS animation (a test checks `app.css`), the web view is set invisible and
  suspended when hidden or minimized (`Window._seen`), and the page polls `/api/state` every 2 s only while seen,
  every 10 s while Apex runs unless the player uses the window, never while hidden.
- **First-run setup** (Home → "Get set up", `setupHTML` in `app.js`): name, "Your choices", team hub (one paste of the
  coach's sign-in link → `/api/hub/join`: address + key saved, tested at once; the link's address replaces an old one; or a single-use invite link `<hub>/join#code=pcsi_…`: `hub.redeem` POSTs the code in the body, no key, and keeps the key the hub gives once; `400 invalid_code` also says to paste the app link the browser showed, if the link was opened there first; the code is never logged, and `pcs_core.redact` masks `pcs_` / `pcsi_` strings in app.log),
  Google (optional once on the hub). Choices (coach's decisions 18 Nov 2026), each applied at once by
  `/api/setup/choice`: auto-record On / Off (on by default for a new install: no `recorder.json` yet, and the first
  start runs the OBS download + encoder test), "Share mic tracks with my team" Yes / No (yes = `comms_consent`,
  mic recorded, "Send my mic track" on, `share_tracks`), comms transcripts Yes / No (`transcript_consent`, default
  off; only the answer is kept, nothing is built yet). Shown until name + (hub or Google) + both questions answered
  (`share_asked`, `transcript_asked`; changing either switch in Settings counts too), or hidden.
- **Teammates' mic tracks** (`pcs_recorder/teamtracks.py`, hub `/api/team-tracks`, `PUT /api/me/sharing`): with
  `share_tracks` (plus comms consent, mic on, "Send my mic track" on) the answer goes to the hub and the teammates'
  shared mic tracks of the app's own uploaded recordings (last 7 days; asked again hourly for 3 days) are downloaded
  into `team-tracks/<recording>/<player>-<scan id>.mic.m4a` in the data folder: one at a time, background priority,
  never while Apex runs, records or scans (Apex starting stops the download, partial removed). Said no = the hub is
  told (teammates then get 403 for this player's track); never answered = nothing sent. Removed with the recording.
  The review player plays them in the mixer ("<name> mic"), each game of the recording with its own offset
  (their jump − own jump), only inside that game. A Tailscale hub (100.64.0.0/10, `*.ts.net`) that doesn't answer gets `tailscale_state`: missing /
  off / on, looked up on this PC only (install folder, a 100.x address), never a request.
- **Idle in the tray**: started with `--background`, the web view is told it is hidden once the page loaded
  (`Window._loaded`; pywebview's hidden start left it "seen": ~2 % of a core in WebView2's browser and GPU processes
  all day, 0.2 % for the whole app after). Hotkey thread: 20 looks a
  second only while hotkeys are registered, else 1 (`Hotkeys.delay`); Tk message pump 5 a second for 10 s after a
  message, else 1 (`UI._pump_ms`).
- **No secrets in app.log**: `obsws_python` logs its password at INFO; it is set to WARNING and every log line goes
  through `pcs_core.redact`. `scrub_log` cleans old lines at start; the obs-websocket password is renewed once
  (`obs.renew_password`, only while our OBS is closed).
- **Secrets at rest**: the hub key (`hub_key.dat`) and the Google / YouTube sign-ins (`google_token.json`,
  `youtube_token.json`, via `pcs_core.read_token` / `write_token`) are encrypted for the Windows user (DPAPI); a
  sign-in file in clear from an older version is encrypted when first read. Never write them any other way.
- **Results to hub and Drive**: with a hub address set, results go to the hub only (Drive is not used, even signed
  in: leaving-Drive plan, coach's decision 18 Nov 2026; Home shows "Results: Team hub", setup has no Drive step,
  Settings hides the Drive sign-in and the coach's address); without one, to Drive. A recording is "uploaded" once its
  destination took its results; where they went is kept per recording in `sent.json` (not in `recordings.json`: older versions refuse unknown fields there). Results
  that reached only Drive (older versions) stay "Not on hub yet" and are sent to the hub on their own (`Recorder._retry_hub`: not
  while Apex runs; key refused = wait for a new key or address; 429 = Retry-After; network or hub errors = 1, 2, 4,
  8, then every 15 min). Recordings sent before `sent.json` existed: hub retried only if recorded in the last 3 days.
- **Public hub** (hub 0.6 behind Cloudflare Tunnel, `https://hub.<domain>`; plan of 18 Nov 2026): every hub request,
  the invite / sign-in links and Watch together's websocket (`wss://`, `watch.sslopt`: certificate and name always
  checked, certifi's list as requests) work with `https://`; home network / Tailscale `http://` unchanged. An address
  typed without a scheme gets `https://` for a public name, `http://` for an IP, a port, a one-word name or
  `.ts.net` / `.local` (`settings.hub_address`); Test switches an `http://` address the hub redirects to `https://`.
  Cloudflare refuses bodies over 100 MB: a file over 50 MB (mic tracks, review talk), or small files together over
  90 MB, goes through `/api/uploads` in parts of ≤ 50 MB (`hub.send_in_parts`), resumable after a lost answer or a
  restart (ids in `hub-uploads.json`, never the key). Hub uploads run in their own thread (`Recorder._kick_uploads`),
  never inside the tick: a game start is never delayed, and Apex starting stops the part in flight (`hub._PartBody`
  asks `Sender.stop` while sending; the next try goes on from the hub's count); a hub
  without the route (404 without `not_found`, or 405) gets the one-shot request. Why the hub can't be reached
  (`hub.classify`, `reach` in the hub state): no internet (a name lookup of `www.msftconnecttest.com`, nothing sent),
  address wrong (name not found, bad certificate, a redirect that isn't to https), hub down (connection refused,
  timeouts, Cloudflare 502-504 / 520-530), key refused. Results waiting for the hub: one Activity line per reason
  (`UI.activity`, no corner message), the reason on the recording, the latest in Settings → Team hub.
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
- **Coach role**: unlocked when the team hub says the hub key is the coach's (`/api/me` role, via `hubbackup.role`).
  No coach address or account name is built into the exe (a security review found one in `build.json`; removed
  18 Nov 2026, `test_no_personal_email_in_app` guards it). The scans folder on Drive is shared with the address the
  player types in Settings (`coach_email`, once per address: `shared_with`). Coach-only: importing recordings by hand.
- **Coach notes** (`pcs_recorder/notes.py`, hub 0.4 `/api/annotations`, contract in the hub's `AGENT.md`): notes and
  drawings at a moment of one player's recording, kept on the hub. A recording = its file name without extension +
  the player who sent its scan, so a recording needs its scan on the hub, and the coach annotates a player's file
  imported by hand with its original name (or his own recording of the game). The hub key's role (`me.role` in the
  answer), not the app's Coach role, decides who may write. Drawings are 0..1 coordinates of the video frame, drawn
  on a canvas fitted to the picture (`fitInk`), repainted only when the set of notes on screen changes. "Squad"
  notes show on teammates' recordings of the same game (`gameKey`) at the same time after the jump; drawings
  never. Last answer cached in `notes-cache/` for offline. Full screen is the player box (F / button), the video's
  own full-screen button is hidden, or the drawings would not show.
- **Watch together** (`pcs_recorder/watch.py`, `web/session.js` + `session.css`, hub 0.5 `/api/videos`, `/api/sessions`,
  contract in the hub's `AGENT.md`): the coach and the squad watch one game together, every player's view side by
  side, the coach in control (play, pause, seek, speed, large view, drawings live). Never during a game: start / join
  are refused while Apex runs ("Close Apex to watch together"), and when Apex starts during a session the app leaves
  it (`Watch.check_apex`, every 3 s in the websocket thread; hub session goes on for the others, review talk stops),
  the page hears `ended: "apex"` through its long poll (also when hidden) and stops its players. The views are the players' YouTube
  videos: after a block upload `youtube.Uploader.send_to_hub` tells the hub which recordings the video holds at what
  offsets (`offsets` / `durations` kept in `youtube.json`; same retry rules as the scan results; hub before 0.5 = asked
  again after 6 h). Game time 0 = the jump; a view's video time = its `videoAt` + game time. The window never holds the
  key: the app keeps one websocket to the hub per session (key in the header) and the page long-polls
  `/api/watch/poll`; ping / pong gives the hub clock (`offset`). Each page keeps its YouTube players within 0.5 s of the
  shared moment (seek with a learned lead per player; paused = same frame), so a slow PC never holds the others; it
  reports its position and the coach sees who is behind. Players' sound and volume are their own (remembered); the
  page starts muted when the browser refuses autoplay with sound ("Sound on"). YouTube's IFrame API is the only outside
  script; embeds play from the `http://127.0.0.1` page (checked in Chromium; `origin` = the page's own). Private videos
  can't be embedded for others: the default upload privacy is unlisted. Iframes are never moved in the page (that
  reloads them): only their boxes change when the large view changes. Entry: Team tab (coach: games with videos,
  Start session; players: Join) and a join bar on every page while a session runs.
  A player may pause / step (5 s, 10 s, one 1/60 s frame with , and .) their own view: only theirs leaves the group
  ("Paused by you · Back to coach", Esc), reported as `browsing`. Drawings stay while paused and fade over 300 ms when
  the moment moves (timer-based, also in a hidden window); the hub saves them as a squad coach note on the large
  view's recording (no text: they stay on that recording). Coach: pointer dot (hidden after 2 s idle), bookmarks (B,
  one-line note), players' "Look at this" flags (F) with Go. Afterwards the review player shows "Session bookmarks"
  and a "Review talk" switch (`web/review-extras.js`, `/api/library/<id>/extras`, audio downloaded once to
  `talk-cache/` and served at `/talk/<id>`): talk follows the video at the session's speed; talk while paused holds
  the video, plays, then goes on.
  Session plan (hub `/api/coach/plans`, made on the hub's Coach → Session plans): the coach's ordered moments (game,
  time after the jump, one-line note). Team tab: "Start with plan" (coach); in the session a bar above the views shows
  "Moment n of m" with the note on every screen, the coach has Previous / Next (P / N) and Go per moment in the list
  (control action `item`: the hub takes everyone to its game, time and view, paused; a player browsing on their own
  comes back), and "Use a plan…" when the session has none. A hub without plans: no plans listed, nothing else
  changes. When the session ends the hub keeps a summary (who was there and how long, length, moments covered /
  skipped, bookmarks, flags, notes, drawings): the review player's "Watch together" box shows it under the session
  bookmarks, with the plan's moments of that recording's games as buttons (skipped ones dashed).
- **Review talk recording** (`pcs_recorder/talk.py`, coach's app only, only with `comms_consent`): the private OBS
  records the mic and voice-app tracks (the only per-program audio capture available; ffmpeg has none) into
  `talk/` (never the recordings folder), then the two tracks are copied out and the temporary video deleted. Never
  while Apex runs or records: refused then, and `Recorder.tick` stops it before starting a game recording. OBS's
  folder and file name are set back after; if that fails, the next game start prepares OBS again. Segments
  (`talk.segments`) map clock time to game time from the session states seen while recording; uploads wait in
  `talk/pending.json` until the hub takes them (4xx = kept, not resent).
- **Map panel** (`app/web/minimap.js`, `pcs_recorder/minimaps.py`, hub 0.5.2 collection `minimaps`): the review player
  draws the player's path (the scan's `seen` rows: debug-overlay x, y, not while spectating, from the jump to `tEndGame`;
  without the overlay the HUD minimap's reads from `minimapTrack`, `pcs_server.seen_rows`, `seenFrom: "minimap"`)
  on the game's own minimap, with the marker at the video's moment, the landing and the harvesters (map % = picture
  fraction). World -> picture: `(x - pos_x) / scale / size`, `(pos_y - y) / scale / size` (the game's transform, from the
  hub document). The pictures are EA's: the coach uploads them to the hub (`tools/upload_minimaps.py` in the hub repo),
  each app downloads them with the key into the data folder (`minimaps/`), never in this repo. No hub / no key: "Minimap
  needs the team hub"; no picture (Olympus) or map `?`: "No minimap for this map yet". Drawn on changes only (time,
  view, size), never in an animation loop. Key M = map (mute moved to U).
  Also on the map: ring circles when the scan has `games[].ringCircles` ({t, x, y, r} world, or {t, mapX, mapY, mapR} in
  map %, or [t, x, y, r]; passed through `GAME_FIELDS`; the last one bright, the one before faint), our squad's kill
  feed lines (so far) and the coach's notes, both placed where the player was at that moment, and with the Squad switch
  the teammates' paths (`pcs_recorder/squadpaths.py`, `/api/squadpaths`): only from what the hub already gives any key
  (`/api/db/timings` gameKey + `recording.jumpAt`, `/api/scans` + `/api/scans/<id>/file` samples), in game time, cached
  in `squad-cache/` for offline. Watch together shows the large view's player on the same map (`wtMapSetup` /
  `wtMapTick` from `session.js`'s 4-per-second tick). Redraws: at most 4 per second, none while the window is hidden.
- **Hub backup copy** (`pcs_recorder/hubbackup.py`, coach's hub key only, hub 0.5.4 `GET`/`HEAD /api/backup/latest`):
  a second copy of the hub's nightly zip (`pcs-hub-<yyyymmdd>.zip`) on the coach's main PC (coach's decision, 17 Nov
  2026: "Yes, also on E:"). Settings → Team hub shows "Hub backup copy" only when `/api/me` says coach (asked at most
  every 6 h, or by Test). Folder default `E:\PCS Hub backups` when an E: drive exists, else off; never the Windows
  drive (refused). Once a day from 04:00 (or the next app start after), only while Apex is closed and nothing records,
  scans or records review talk (not even a request then): HEAD, skip if that date is there, download at background
  priority to `.partial`, check size + zip, rename; Apex starting meanwhile stops it (not a failure). Keeps the newest
  N (14) files named exactly `pcs-hub-<8 digits>.zip`, nothing else touched. No pop-up: one Activity line
  "Hub backup copy failed: <reason>" after failures on 3 days in a row. State in `hub-backup.json` (never the key).
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
- **Scan plumbing** (18 Nov 2026, to keep the scan unnoticeable): `keyframes()` reads ffmpeg's pipe unbuffered
  (`bufsize=0`, `readinto` one new buffer per frame). The old `bufsize=10**8` + `.read(n)` cost 134 ms and 0.18 CPU-s
  per keyframe, mostly kernel time; now 25-30 ms and 0.08 CPU-s, same bytes. A 127.0.0.1 socket was measured too: no
  faster, ~12 % more CPU, so not used (the study blamed the pipe; it was the buffer). A recording is cut into the same chunks as
  before (`default_chunks`: half the CPU threads, max 8; results depend on the cuts) but only `WORKERS` (2) run at a time (~360 MB each; whole scan < 1 GB).
  Each finished chunk is kept in `<out>/<name>.partial/` (pickle, keyed by file size/time, cuts, decoder, layout,
  build) until the scan completes: when Apex starts, the app stops the scan (`core.stop_tree`, nothing stays in
  memory) instead of suspending it, and the next run goes on from the chunks still to do. `result_files` never
  sends `.partial`. 4:3 stretched views fill only the boxes their readers look at (`view_top` rows 0..640).
- **Leftover unpacked copies**: the onefile exe unpacks ~600 MB to `%TEMP%\_MEI*` per start (and per scan); a crash or
  forced stop leaves it. At start the app removes such folders only when they hold its own files (`BUNDLE_MARKERS`),
  are not its own, are over 10 min old and not in use (Python's DLL, loaded by any running copy, can't be deleted:
  it goes first, a refusal keeps the folder) (`pcs_core.clean_old_unpacked`).
- **Debug overlay POS reads** (`pcs_hud.read_dbg`, 18 Nov 2026): the glyphs are matched as 9 x 7 black / white cells,
  and a cell that fits no digit is '?'. The POS line must be three numbers from the line's start with a gap cell
  between them (`POSRX`): the decimal point may read '-' or '?', the gap '-'; any other '?' makes the line unread.
  Before, '?' could stand for the decimal point anywhere (15?63 read as 15.63 for 15863.63), a '?' in place of a
  minus or first digit was skipped (?1607 for 11607, lost signs), and numbers were picked from anywhere in the line.
  Checked against the HUD minimap position on 4 recordings (4,950 moments): wrong POS reads (> 1500 units off) 82 ->
  15 (1.9 % -> 0.4 %), POS read at 89 % -> 83 % of those moments; no value that both versions read differs.
  8 / 9 confusions (3-4 pixels apart; ties are common) are still possible: 10 of the 15 left are on 7 Nov.
  `landing` takes VEL also from frames whose POS was refused (the free-fall start was found through them), and
  `clean_pos` no longer flips the sign of values within 3000 of 0 (a landing at x 748 was turned to -748).
  47 landings on the 4 recordings: no place name changed, one landing 6,471 units off is now 202 off, two moved by
  0.5 % within the same place.
- **Landing place**: the debug overlay's world position (`POS`) at landing → map % (`pcs_hud.to_map`, `CAL` per map)
  → POI (`pcs_scan.landing_poi`). The app's map pictures (`pcs_maps.json` space) are framed like the game's own
  minimap, so a map's game transform (`_game_tx`) works directly. WE / SP use values fitted in Oct 2026, BM the
  game transform checked on 12 own-arrow / POS pairs (rms < 0.8 %), ED the game transform since 16 Nov 2026 (coach's
  decision: 13 pairs, rms 0.33 / 0.49 % against 0.74 / 0.69 % for the Oct fit; none of the 16 known ED landings changed
  place; Oct values kept in a comment), KC the game transform unchecked
  (`mapCalibrated: false`), OL none yet. Each `games[].calib` entry (own arrow on the full map) paired with `POS`
  just before and after it is a calibration pair; the arrow blob is sometimes wrong (reject outliers). BM and KC
  have POI centres only: a landing more than `POI_NEAR` (12 %) from every centre gets no name (Broken Moon's middle
  has POIs since PR #41: Quarantine Zone and others).
- **Kill-feed weapons**: `pcs_hud.Weapons` compares each new feed line, at full size, with the game's own weapon
  icons (the feed draws them mirrored) and adds `weapon`, `weaponName`, `weaponScore`, `arrow`, `event` to the line
  (red arrow = knock, none = kill). The icons are a private file, `pcs_weapons.npz`: git-ignored, never bundled,
  built on the coach's PC. Players get it from the team hub when it has one (`pcs_recorder/weaponicons.py`:
  `/api/db/weaponicons/current`, 404 = none; `/_blob/<asset>` downloaded with the key only when the asset id changes,
  checked with `icons_valid`, a refused asset is not downloaded again; the Team link is then optional), else through
  Settings → Team link (a Google Drive "Anyone with the link" file). Re-checked every 6 hours, saved in the data folder. `drive.file` sign-in can't read a file someone else
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
  only: ranked (RANKED, and RANKED - DROPZONE read as one word since 17 Nov 2026), wildcard, pubs (the word TRIOS
  of pubs trios); SP, ED, BM so far). The table's mode keys are the
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
- **Ranked RP and rank badges** (`pcs_ranked.py`, table `pcs_ranked.json`, 16:9 only). What Ranked shows (checked on
  the 17 / 18 Nov Ranked recordings): our kills that give RP are kill lines `[killer badge] killer [weapon | Bleed Out |
  ability] [victim badge] victim +N RP` with `+N RP` in green at the feed's right edge; two other green lines are
  "Team Eliminated +N RP" (placement) and "All Previous Kill Points Increased +N RP" (and the kill lines' own numbers
  grow on screen when that shows: +8 -> +10). The feed badge shows the tier only, never the division. Under the player's
  rank emblem (top right) a counter shows the game's RP so far: "-48 RP" red (entry cost) rising to "+328 RP" white; it
  does not include what the match summary adds after the game (a won game: +328 in game, +392 on the summary).
  `Tracker` follows the green lines across keyframes (same number start, at most three places up or down, words that
  look alike, one to one; pieces of one line joined at the end, also a kill line whose number grew), reads the number
  (digit shapes from our recordings, as OwnStats) and the victim badge (28 x 28 shapes of the tiers seen so far:
  diamond, platinum, gold; rightmost badge followed by a white name, colour must agree); a value counts when read on
  two keyframes in a row. Lines go to `feed.json` `rpLines` [{t, y, rp, reads, kind: kill | team | bonus | unknown,
  victimRank?, color?, hue? (our killer's squad colour, hue in degrees)}]; per game `rpFeed` {kills [{t, rp, victimRank?, color?, hue?}], killRp,
  lines, unknownLines} and `rp` {start, last, lastAt, reads, laterLines?} from the counter: two reads in a row, never
  lower, and a change only with a green line seen from 20 s before to 5 s after (a red 8 misread as 6, -48 -> -46,
  has none). `laterLines` = green lines after the counter's last read (the player was dead / spectating: the total
  misses them). Check (truths by eye; dev 17 Nov, held-out 18 Nov 22:28, split before tuning): every green number
  given was right (126 / 126 dev, 44 / 44 held); held-out kill lines with RP: 18 of the 26 on screen reported, all 18
  right (number and victim tier; the killer colour was not checked one by one), the other 8 counted as `unknown`
  lines or seen once / faded; counter's last value right in 17 / 17 and 8 / 8 games.
  Tiers never seen in our recordings (rookie, bronze, silver, master, predator) are not read. ~1.5 ms per keyframe.
- **Ring circles** (`pcs_ring.py`): on the unzoomed full map the area outside the current ring is red-tinted with a
  dark line on the ring; the next ring is a see-through white disc (everything under it moved towards white by one
  fraction, 0.14..0.26 measured, alike in R, G, B). Edges that fit each look vote for a centre then a radius (Hough),
  a local search sits on the edge, and a circle counts only when most of its visible arc shows the contrast. Refused:
  a current ring whose outside is not red-tinted all around (cyan / purple discs of other map features, blue water
  beside purple land), a next ring of another colour, circles under 2.9 % of the map (round icons; the last small rings
  are not read), a next ring under 0.3 x the current one (12 % of the map when no current ring shows: round 1). One read per 3 s of map (`RING_EVERY`, ~2 s a read); the next ring found just before is tried as a
  start. Per game: `ringCircles` (per ring stage and phase: current circle cx, cy, r in map % + `mapX/mapY/mapR` for
  the review map, `next`, `conf`), `ringEnds` (end circle of ring k: the next ring shown in round k and/or the current
  circle in round k + 1's wait; `rUnits` from the map scale in `CAL`), `ringSpeed` (`closing`: radius change per
  second while the player watches it close; `ends`: radius drop / closing time). Stage = the ring phase running at
  that moment (`games[].ring`), so rounds must be numbered for stages (None otherwise).
- **Care packages and strongholds** (`pcs_mapicons.py`, every `ICONS_EVERY` = 2 s of full map, ~0.35 s a read):
  care package = a light diamond (|dx| + |dy| ~ 23.5 px at 1440p, same size zoomed or not) with a helmet inside; the
  helmet is compared with `CARE_SIGNS` (12 x 12 signs derived from the RSX `care_package_icon`, never the image).
  Looks: white or yellow outline on a dark fill, and "opened" (yellow, a line across, see-through fill; glyph match only
  0.45-0.58, accepted with the line). Strongholds: the red fill of `stronghold_map_available_icon` (can be taken; drawn
  larger for a while, up to ~66 px) or its door light on black (taken), by `DOOR_SIGNS` (normalised correlation <= -0.5
  plus a dark fill / light door test). The map's crosshair lines cross icons: the red mask is closed before labelling.
  Per game: `carePackages` (grouped within 1.5 %, reads before ring 1 starts closing dropped, `n` 1-4 by the ring
  phase of the first sight, `nSure` when first seen in its own announcement round; packages first seen together:
  opened ones count as older), `strongholds` (within 2 %; a taken one never seen red needs 2 reads),
  `carePackageRingCheck`. In scrims and pubs the packages are on the map from the drop; in ranked only once revealed
  (17 Nov ranked: 1 game of 17). Checked by eye (3, 4, 7, 15, 17 Nov, 222 map frames): no false care package or
  stronghold; every visible care package found (20 of 20 at the last frame of each map open; one hidden under player
  arrows), strongholds 7 of 7 (one, drawn large, was missed until the size limit was raised).
  First data on the coach's rule (packages 1-2 inside ring 2): in our scrims they landed inside ring 1's end circle
  and outside ring 2's (see the PR); keep `inRings` for every read ring.
- **Respawns** (`pcs_respawn.py`): teammate cards bottom left (names at x 184, rows 1058 / 1146 at 1440p in own
  view and on the full map; 85 px higher while spectating). Dead = a lime status line under the name (DEATHBOX
  RECOVERY, RECOVER BANNER, CRAFT BANNER, DELIVER TO BEACON, AWAITING RESPAWN, RESPAWNING); alive = the health bar.
  A slot read dead >= 3 times then alive >= 3 times = respawn (`who: teammate`, slot, card colour). Cards are used
  only from own-view frames (while spectating they are the spectated squad's). The player's own death = a run of
  spectator-layout ring reads (`ownDeaths`); own view again later in the same game = respawned (`who: self`,
  `how: dropship` when a free fall follows). The card can't tell a beacon from a mobile beacon or a mode's own
  respawn (`how: card`). 16:9 only. `games[].respawns` is always present (empty list = none seen).
  Banner and beacon (plan item 7): each dead card's status line is matched (`status_of`) on its first 96 px against
  shapes from our recordings (`pcs_banner.json` `status`: DEATHBOX RECOVERY, RECOVER BANNER, CRAFT BANNER, DELIVER TO
  BEACON, AWAITING RESPAWN, RESPAWNING..., RESPAWNING IN ss.cc; IoU ≥ 0.45 with a margin, else none). A teammate respawn
  gets `bannerAt` (first of 2 DELIVER TO BEACON reads in a row: a teammate holds the banner), `banner` (picked / crafted
  by what came before), `beaconAt` (first AWAITING RESPAWN / RESPAWNING... run), `modeRespawn` (Wildcard timer); only
  the steps read. Held-out (7 / 17 Nov): status reads 56 of 60 sampled named, all right, 4 left unnamed; beacon use
  4 of 4 within 1 s; banner pickup 1 of 1 where the DELIVER line showed (in 3 respawns box -> awaiting came between
  keyframes or off screen). GPU vs CPU: 89 of 92 keyframes the same, the rest named vs unnamed, never two names.
  Review Events: "... respawned · back after m:ss · banner picked up at +m:ss · beacon at +m:ss".
  Cards are found by the line under the name (one-letter names exist); a status line is 17 rows from x 186 with many
  colour changes (grass behind the cards gives lime too). Card rows per game = the two rows read most (Wildcard sits
  at 1035 / 1149). A frame without a ring-clock read counts as own view unless inside a spectating run (Wildcard shows
  no clock most of the time); after the player's last death with no way back, cards are someone else's. Check set
  (`pcs-handoff\checkset\checkset-respawn-ring.json`, 15 / 13 / 17 / 7 Nov): teammates 23 of 25 found, self 15 of 18,
  none wrong. Misses: a respawn just before the player's own, a card read dead only twice.
  The review player shows each respawn like a kill (coach's decision, 18 Nov 2026): a lime tick with a round head on
  the kill feed lane and a line in Events ("You respawned", "<legend> respawned" by the card colour against the legend
  select's hues, else "Teammate respawned"), click = seek. `respawns` is in `GAME_FIELDS` (`app/pcs_server.py`).
  Rings, frames checked by eye (7, 15, 17 Nov): current ring 97 of 114 visible read right, next ring 164 of 180, none
  wrong after the small-circle rule (the first run took a white map-feature circle on E-District for a next ring 5 times).
- **Own knocks, shields and health** (`pcs_bars.py`, 16:9 only): the player's own card bottom left. Health bar = 16 rows
  from x 236 (top row 1344 at 1440p), white or light pink after a revive (red = health just lost, tan = a med kit still
  healing, green = regenerating: not counted). Shield bar 16 rows above it, segments of 25 points 63 px apart, filled in
  the tier colour (white tier = light grey ~196; blue, purple measured; red / gold not seen yet, reference colours
  unchecked). How many segments show is the armour's capacity, not tied to the colour (4 blue segments exist).
  Knocked = a thin yellow bleed-out bar (rows 1348..1355) with the name in red (gold while being revived: still
  knocked). A card 85 px higher (top row 1259) is the spectated player's: read as `spectating`, never as the player.
  Teammates: the same bars on the alive cards `pcs_respawn.read_cards` finds (segments 42 px apart from x 185, health
  216 px), the bar's top row searched near the card row (the card row jitters ±3 px between decoders).
  Per game (`attach`): `ownKnocks` = [{t, end, reads, outcome: revived | died}], always present; a knock needs 2
  knocked reads (Wildcard knocks last 1-3 s), menus inside it are fine, an alive card within 5 s of the last knocked
  read = revived (a death shows a recap or spectating first). `shieldTimeline` = {cols [t, tier, shield, health],
  self, mates [slot 1, slot 2]}: a value is kept when the next read agrees (±3) and it differs from the last kept one;
  `[t, 'knocked']` rows for knocks. Squad wiped while the player was last alive = no knock state at all (none to find).
  Cost ~0.6 ms per keyframe (`read_cards` was already there). Check set (`pcs-handoff\checkset\checkset-own-state.json`,
  dev 13 / 15 Nov, held 7 Nov + 17 Nov games 1-6): knocks dev 22 / 22 found, outcome 22 right; held 22 / 22, outcome
  22 right (one truth corrected on 1 s frames after the reader disagreed), none false, start within 3 s. Own bars on
  sampled frames, held: state 20 / 20 (4 knocked), tier 15 / 15, shield ±5 16 / 16, health ±5 16 / 16; teammates (7 Nov,
  17 cards): 16 found (a card at 2 % health is not found by read_cards), tier 16 / 16, shield and health ±5 15 / 16
  (a striped segment being recharged counts as filled). GPU vs CPU decoding (15 Nov 880-1700 s, 7 Nov 3000-3800 s):
  own state and tier the same on all 1620 keyframes, values within 2 on 1617 (3 health reads 3-7 apart on a damaged
  bar); teammates 1552 of 1620 (mostly a card found by one decoder only, or health at a damage flash). The review player shows own knocks like respawns (a grey tick with a
  square head on the kill feed lane, "You were knocked" in Events with "back up after m:ss" / "did not get up").
- **Fights and ring damage** (`pcs_fights.py`, plan items 3, 4, 12; numbers in the PR): per game `fights` = [{t0, t1,
  opponents (enemy names told apart by shape), opponentNames (the shapes), ours {knocks, kills}, theirs {knocks, kills},
  result won | lost | even, thirdParty, thirdPartyAt?, thirdPartyWhy?, wiped?, damageTaken, feedLines [t]}] and
  `ringDamage` = {seconds, damage (estimate), measured (health drops read), segments [{t0, t1, seconds, ticks, perTick,
  damage, measured, round, how}]} (None when the own card was never read). Evidence of a fight: our squad's kill-feed
  lines (a line captured again lower in the feed within 10 s counts once; a "squad" name in a colour none of our squad
  has is not ours), own knocks (and their end; a knock within 8 s after a stretch outside is the ring's), shield +
  health lost; joined when < 25 s apart. Result: kills decide (the player's own death in the fight counts as a kill on
  us), then a lead of 2 knocks; a squad wiped in it = lost. Third party: more than 3 enemy names, a name first seen
  >= 20 s after the first enemy line knocking one of us, or hits / gunfire (`pcs_audio` loudness 10 dB over the game's
  median in 35 % of 3..15 s) after every opponent met died. Loudness does not find or join fights: 10 dB windows are
  29 % of fight time and 12 % elsewhere (7 Nov). Ring: a stretch outside needs ring ticks (health drops of one size,
  shield unchanged: ring damage skips shields; 3+ ticks with the shield up, 4+ without, or 2+ inside red edges; 1.5 s
  tick from the deathfield script; 3-4 a tick seen); `read_storm` (share of red-tinted pixels in four thin edge strips,
  own card alive only) only widens it: Storm Point's red grass and E-District's red-lit streets tint the strips like the
  ring does. Damage = ticks lasted x tick size. Truths `pcs-handoff\checkset\checkset-fights.json` (dev 7 Nov, held
  17 Nov, labelled on the feed line images + own card, frames for the ring): held fights found 18 / 19, result 18 / 18,
  third party 12 / 15 (3 false); ring stretches 2 / 2, one false 8 s stretch of 1-point ticks. Weak spot: our own kill
  lines on a red plate (+RP) are read with both sides 'squad' (pcs_hud.kill_parts): such a fight is missed.
  The review player draws fights as bands on the kill feed lane (green won, grey lost, amber even, dashed = third party)
  and time outside the ring as an orange bar on the rings lane, both in Events.
- **Heals, ult and tactical uses** (`pcs_usage.py`, 16:9 only, ~1 ms per keyframe, read only on own-HUD frames: no map,
  drop or select screen). Heal panel right of centre (frame lines rows 928 / 1023, progress bar from x 1800 rows 991..1009,
  icon x 1636..1718): the item by its icon's colours and shape (cell = narrow blue down to row ~997, battery = blue
  951..987 with white stripes, syringe = white with a small red cross, med kit = red, phoenix = orange), never by its
  name (players' languages differ); the bar's colour must suit the item. A revive uses the same panel with a blue / green
  frame: not a heal. Per game `heals` = [{t, item, done, reads}]: a heal needs 2 panel reads (runs split at a gap over
  3.5 s, a bar that fills slower than half its usual rate, or starts again: a queued second heal); t = first read minus
  the part already filled; done = the bar would be full within 1.2 s of its last read (else it vanished with time left:
  cancelled). Measured fill times (S31): cell 2.5 s, battery 5, syringe 4, med kit 8. The own shield / health bars
  can't confirm a heal: shields regenerate on their own and were seen rising during cancelled heals. Single-read heals
  (mostly cancelled ones and cells caught once) are not reported.
  Abilities, bottom centre: a key hint box (about 28 px, more than half white, centred under the icon) = ready; the
  charge / cooldown text under it (sparse, any language) = charging / cooldown; tactical icon dimmed with the hint still
  shown = in use (Seer's wind-up, the countdown follows); ult glow (any saturated colour: blue for Seer, red for Sparrow)
  without hint or text = running. The full map's black key bar hides them (checked first). Per game `abilities` =
  [{t, kind: ult | tactical, legend}]: tactical = ready, then within 4 s a dimmed read, 2 reads before it is ready
  again, a cooldown text among them; ult = ready, then running / unseen, then the charge text twice within 90 s, with the
  running glow seen or the first charge text one digit wide ('1%'); t = the first read after the last ready one.
  Legend: the legend select screen's own slot, else the icon signs (`SIGNS`: 12 x 12 signs of the white shape of each
  `ultimate_*` / `tactical_*` UI image, derived once with a private tool; the images are not in the repo) against the
  averaged ready icon of the game (IoU >= 0.5, 0.15 ahead of the next legend). Abilities with charges (Sparrow's ult)
  stay "ready" after a use while a charge is left: not detected. Spectating frames are left out. Region cache boxes
  `heal` and `abil`; check set `pcs-handoff\checkset\checkset-usage.json` (split before tuning: dev 17 Nov, held
  7 + 13 Nov). Held-out: heals 29 of 37 found, item 29 / 29, done 28 / 28, no false heal; the 8 missed were each on one
  keyframe only (6 cancelled). Before a fix found on held-out data (the panel's edge glows blue as a cell finishes and
  read as a revive) it was 23 of 36. Tactical 3 / 3, ult 2 / 2, none false (small sample). Dev: heals 26 / 33, tactical
  4 / 5 (Seer's tactical read 10 s late once: its icon over a white wall still looked ready), ult 2 / 2. On 7 Nov the
  Bangalore ult showed its glow without a key hint while ready, so no ult use could be read there. CPU vs GPU decoding
  (17 Nov, 360 keyframes): 352 the same, the rest read vs not read on one keyframe, never two items. The review player draws
  heals (small teal dots), ult uses (blue diamonds) and tacticals (small grey dots) on the events lane; heals and ults
  also in Events ("Shield battery", "Ultimate used · Seer").
- **Minimap position** (`pcs_minimap.py`): where the player is, every few seconds, without the debug overlay (only
  the coach has it). The HUD minimap (top left, 16:9, centre (226.5, 226) at 1440p) is north-up (only the arrow
  turns) and its centre is the player. Its zoom is one of a few fixed levels, the same world scale on every map:
  78, 62.5, 56.5 or 45 world units per screen pixel (it changes inside a game; cause unknown). Scan side: every 3 s
  of normal HUD (`EVERY`) the box rows 104..384, x 70..386 (below the debug overlay's lines) is kept as 70 x 79 grey
  cells + a mask of icon cells (saturated or very bright: arrows, pings, ring lines) when the minimap's white corner
  brackets are at their normal place (spectating moves the HUD 85 px down: someone else's minimap; menus, map open,
  death screens: no brackets). `attach` then matches each game's reads on the map's own minimap picture (local
  contrast both sides, masked NCC by FFT; near the last position at the last zoom first, the other zooms there,
  the whole map at most every 9 s while lost; the picture is padded with the dark plain the HUD shows beyond a map
  edge). A read counts when its score is >= 0.33 and the read before or after it (<= 12 s) agrees within the
  fastest move. `games[].minimapTrack` = {v, every, reads, pts: [[t, mapX, mapY, q]]} (map % = picture fraction x
  100, as `landing.mapX`; q = score x 100), or `noPicture: true` with no points. Pictures are EA's: `PCS_MINIMAPS`
  (an RSX export folder) for developers, else the app's data folder `minimaps/` (downloaded from the hub by
  `pcs_recorder.minimaps`; `Backend.minimaps_tick` fetches all maps every 6 h while Apex is closed), only when the
  hub's transform is the one in `MAPS`. Checked against the coach's debug-overlay POS on region caches (box `minimap`;
  split fixed before tuning: dev 17 + 4 Nov, held-out 7 + 15 Nov). Held-out, 15 games: of the moments every 3 s from
  the jump to the game's end in own view, 83 % have a minimap position within 2 s (dev 92 / 77 %); error median 0.06 %
  of the map, 99th percentile 0.3-1.4 %, 98.7 % within 1 %, one point of 979 wrong (107 %: dropship off the map's edge).
  The POS reader itself misreads now and then (a minus sign lost, 9 read as 8, a dropped digit): the minimap was
  right in every case looked at; those POS rows were the "errors".
  Cost: ~1 ms per read at scan time (0.3 ms per keyframe), matching ~3-8 ms per read after the chunks are done.
- **Sound cues**: correlation on mel spectrograms and Shazam-style hashing both failed in the 3D game mix.
  What works: narrowband tone strength against its ±30 Hz neighbourhood (ring alarm = 374 + 533 Hz pulses).
  Screen and sound check each other (`pcs_audio.attach`, branch `audio-cross`): a countdown end read on screen lets a
  weaker alarm count right after it (`soundWeak`, 7 dB for 0.3 s within -0.5..+3.5 s); an alarm heard while the ring
  clock shows the ring closing or a countdown far from its end is not a ring start (`ringSoundOther`, e.g. the "Loot
  Bins and Arsenals Reset" event plays the same tones); a damage total resting on a single read is refused when the
  game was quiet between the read before it (`damagePrevReadAt`) and it (`damageRefused: "quiet"`). Measured on the
  7 / 15 / 17 Nov recordings: rings heard 38 -> 42 of 47, unexplained alarms 3 -> 0, real single damage reads 7 of 7
  kept. Never use the voice / mic tracks for detection. Developer tool: `tools/audio_cross.py` (scores the ring alarm
  and the damage check against a scan; game track audio, no video decode).

## Developer tools (`tools/`, not in the exe)

- **Region cache** (`tools/region_cache.py`): decodes a recording once with the scanner's own `keyframes()` and keeps,
  per keyframe, the small boxes the readers look at (`BOXES`: debug overlay, HUD minimap (`minimap`), top-left block for legend select /
  banner / ring clock, rest of the banner words, top-right counters, kill feed, squad cards + own bars `squad`, added
  18 Nov 2026: older caches lack it; `--boxes squad,left` for the cards, bars and spectating; `heal`, `abil` for
  pcs_usage), as lossless WebP (bit-identical: lossy
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
