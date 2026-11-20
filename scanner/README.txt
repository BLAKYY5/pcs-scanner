PCS RECORDING SCANNER v1.7.0
Reads dropship spots (scrim/custom drop screen), Evo harvesters (full map, M key), landing time/spot
and every ring phase timing from Apex recordings.

SETUP (once)
1. Python 3.9+ from python.org (tick "Add python.exe to PATH" during install).
2. Double-click setup.bat (installs numpy, pillow, scipy and ffmpeg).

SCAN
- Unzip the folder anywhere, then drag one or more recordings (from any folder) onto scan.bat.
- Only keyframes are decoded (1 per second). At the start the scanner times the CPU and GPU decoders on your
  file and keeps the fastest, then scans the recording in parallel chunks (half your CPU threads, max 8).
  A 2-hour 1440p block should take a few minutes. The end of the run prints a timing line: send it if it is slow.
  Options: --cpu (never use the GPU), --workers N (number of parallel chunks).
- GPUs: DirectX 11 (AMD, NVIDIA, Intel) and CUDA (NVIDIA) are tested against the CPU on the first run; the winner
  is remembered per video format in decoder.json (delete that file to test again, e.g. after changing GPU).
- Results: pcs_scan\<recording>.scan.json and <recording>.feed.json (every kill / event feed line as a small
  image, read by Claude at import). Send both to Claude in the Loot Book chat to import.
- Optional: python pcs_scan.py --snaps "<file>"  also saves a JPEG of every frame it used, for checking.

WHAT IT PICKS UP
- Drop screen (scrims/customs: whole map with every dropship): all red + green markers, map identified
  automatically, each matched to its POI. Our drop = green.
- Full map opened during the game: every Evo harvester icon. Open the map at least once after ring 1 starts
  moving (harvesters appear 10 s after) so they get recorded. Each map open is one sample; 2+ opens = more reliable.
- Games are split automatically (new drop screen, map change, or a different harvester layout).
- Game times come from the recording name (e.g. "Apex Legends_2026.10.02-22.04"), so keep your recorder's
  default naming; that's how games get linked to EEC lobbies.

- Ring timings: the ring clock under the minimap is read on every frame (also while spectating). Each phase's
  end time is pinned to within a fraction of a second; the last 10 seconds (s.cc format) give it to 1/100 s.
  Spectating runs about 0.7 s behind your own view, so your own view is used whenever available.
- Ring clock styles: the round icon + 'mm:ss' shown before the ring is drawn, and the bar + 'ROUND n - ...'
  label after. Each phase gets a frame-exact 'appeared at' and a precise end time.
- Landing: uses the NAME / POS / ANG / VEL overlay in the top-left corner (keep it switched on). The scanner
  finds when the dropship stops at the drop-off, the jump, and the touchdown, each frame-exact (60 fps),
  plus the landing spot and POI.
- Ranked / pubs: the full map opened in the dropship shows the nearby drops; those are read too.

Maps: World's Edge, E-District, Storm Point, Olympus, Kings Canyon, Broken Moon (Broken Moon data is out of date).

WHAT'S NEW IN 1.7.0
- Detects ranked games (rank badge / RP panel top right) and marks each game "ranked" or "scrim" in the .scan.json.
  Force it with --mode ranked or --mode scrim if needed.
- Kill feed: lines with squad-coloured names (your teammates' colours) are now captured in full, so our own
  kills, knocks and deaths can be recognised. In ranked games only our squad's names are kept.
