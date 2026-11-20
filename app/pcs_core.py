"""PCS Scanner shared logic (no window): paths, settings, YouTube sign-in, updates, scanning.
Used by the app window, the tray recorder and the headless --scan mode."""
import os, sys, json, time, subprocess, urllib.request, urllib.error, logging

if os.name == 'nt':   # no console windows popping up for ffmpeg / ffprobe (also applies in the scanner's worker processes)
    _Popen = subprocess.Popen
    class _QuietPopen(_Popen):
        def __init__(self, *a, **k):
            k.setdefault('creationflags', 0x08000000)
            super().__init__(*a, **k)
    subprocess.Popen = _QuietPopen

APP = 'PCS Scanner'
EXE_URL = 'https://github.com/BLAKYY5/pcs-scanner/releases/latest/download/PCS-Scanner.exe'
HERE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
if not getattr(sys, 'frozen', False):   # run from the source code: find the scanner next to the app folder
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scanner'))
DATA = os.environ.get('PCS_DATA_DIR') or os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'PCS Scanner')
OUT = os.path.join(DATA, 'scans')
os.makedirs(OUT, exist_ok=True)
os.environ['PCS_DATA_DIR'] = DATA
# bundled ffmpeg / ffprobe first on PATH
for d in (os.path.join(HERE, 'ffmpeg'), HERE):
    if os.path.exists(os.path.join(d, 'ffmpeg.exe')): os.environ['PATH'] = d + os.pathsep + os.environ.get('PATH', '')

def load_json(p, default):
    try: return json.load(open(p, encoding='utf-8'))
    except Exception: return default

BUILD = load_json(os.path.join(HERE, 'build.json'), {'version': 'dev', 'repo': ''})
SETTINGS_F = os.path.join(DATA, 'settings.json')
SETTINGS = load_json(SETTINGS_F, {'player': '', 'role': '', 'uploaded': [], 'library': []})
SETTINGS.setdefault('library', [])   # videos added by hand (the Recordings page lists them)
def save_settings(): json.dump(SETTINGS, open(SETTINGS_F, 'w', encoding='utf-8'), indent=1)

# ---------------- log (never a password in it) ----------------
# The OBS remote-control library logs its connection settings, password included, at INFO ("Connecting with
# parameters: ... password='...'"). Up to 1.10.10 that went to app.log. Now: that library logs warnings only, every
# line is passed through redact() on its way to the file, and lines already written are cleaned at start.
LOG_F = os.path.join(DATA, 'app.log')
import re as _re
_SECRET = _re.compile(r"""(password\s*[=:]\s*)('[^']*'|"[^"]*"|[^\s,;)'"]+)""", _re.IGNORECASE)

_HUBSECRET = _re.compile(r"pcsi?_[A-Za-z0-9_-]{43}")

# Google sign-in: an email address, an ID token (JWT), an access token (ya29.) or a refresh token (1//) never in the log.
_EMAIL = _re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63})+")
_GTOKEN = _re.compile(r"eyJ[\w-]{8,}\.[\w-]+\.[\w-]*|ya29\.[\w.-]+|1//[\w.-]{8,}")

def redact(text):
    """password=... / password: ... values replaced by ***; a hub key or invite code (pcs_… / pcsi_…), an email
    address and a Google token too."""
    text = _GTOKEN.sub('<token>', _EMAIL.sub('<email>', text))
    return _HUBSECRET.sub('pcs_***', _SECRET.sub(r'\1***', text))

class RedactFilter(logging.Filter):
    def filter(self, record):
        try: msg = record.getMessage()
        except Exception: return True
        clean = redact(msg)
        if clean != msg: record.msg, record.args = clean, None
        return True

def scrub_log(path=None):
    """Clean password values out of a log written by an older version. The file is rewritten next to itself and
    swapped in one step; if another program holds it open (Windows refuses the swap), it is left as it is and
    tried again at the next start. -> number of lines cleaned."""
    path = path or LOG_F
    tmp = path + '.scrub'
    try:
        with open(path, 'rb') as f: data = f.read()
    except OSError:
        return 0
    if b'password' not in data.lower(): return 0
    lines, n = data.decode('utf-8', errors='surrogateescape').splitlines(keepends=True), 0
    for i, ln in enumerate(lines):
        if 'password' in ln.lower():
            clean = redact(ln)
            if clean != ln: lines[i], n = clean, n + 1
    if not n: return 0
    try:
        with open(tmp, 'wb') as f: f.write(''.join(lines).encode('utf-8', errors='surrogateescape'))
        os.replace(tmp, path)
    except OSError:
        try: os.remove(tmp)
        except OSError: pass
        return 0
    return n

def setup_logging():
    if not any(a in sys.argv for a in ('--scan', '--finish-update', '--selftest')):
        scrub_log()                                            # before the log is opened by this process
    logging.basicConfig(filename=LOG_F, level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    for h in logging.getLogger().handlers:
        if not any(isinstance(f, RedactFilter) for f in h.filters): h.addFilter(RedactFilter())
    logging.getLogger('obsws_python').setLevel(logging.WARNING)
setup_logging()


# ---------------- sign-in tokens (YouTube) ----------------
# Kept encrypted for this Windows user (DPAPI, like the hub key): a copy of the data folder (sent for help, a
# backup) doesn't carry a working sign-in. A file in clear from an older version is encrypted the first time it is
# read. Same file names as before, so "signed in / expired / none" checks are unchanged.
_TOKEN_ENTROPY = b'PCS Scanner Google sign-in'

def read_token(path):
    """The saved sign-in (the dict google-auth wrote), or None when there is none or it can't be read here."""
    import base64
    try:
        with open(path, encoding='utf-8') as f: raw = f.read()
    except FileNotFoundError:
        return None
    try:
        d = json.loads(raw)
        if isinstance(d, dict) and 'dpapi' in d:
            from pcs_recorder.hub import _dpapi
            d = json.loads(_dpapi(base64.b64decode(d['dpapi']), False, _TOKEN_ENTROPY).decode('utf-8'))
        elif isinstance(d, dict) and os.name == 'nt':
            try: write_token(path, raw)                        # written in clear by an older version
            except Exception as e: logging.warning('sign-in not encrypted (%s)', e.__class__.__name__)
    except Exception as e:
        logging.warning('sign-in not readable (%s): sign in again', e.__class__.__name__)
        return None
    return d if isinstance(d, dict) else None

def write_token(path, text):
    """Save a sign-in (google-auth's JSON text), encrypted for this Windows user."""
    import base64
    if os.name == 'nt':
        from pcs_recorder.hub import _dpapi
        text = json.dumps({'dpapi': base64.b64encode(_dpapi(text.encode('utf-8'), True, _TOKEN_ENTROPY)).decode()})
    tmp = path + '.part'
    with open(tmp, 'w', encoding='utf-8') as f: f.write(text)
    os.replace(tmp, path)

def _saved_creds(path, scopes):
    from google.oauth2.credentials import Credentials
    info = read_token(path)
    if not info: return None
    try: return Credentials.from_authorized_user_info(info, scopes)
    except Exception: return None

YT_SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
# One Google sign-in: who the player is (openid + email: the team hub learns which Google account each key belongs
# to) and YouTube upload, in one consent. No Drive (results go to the hub only).
ID_SCOPES = ['openid', 'https://www.googleapis.com/auth/userinfo.email']
SIGNIN_SCOPES = ID_SCOPES + YT_SCOPES
YT_TOKEN_F = os.path.join(DATA, 'youtube_token.json')

def granted_scopes(info):
    """The scopes a saved sign-in was given. A file without them can only be a YouTube-only sign-in (older versions
    asked nothing else in this file)."""
    s = (info or {}).get('scopes') or YT_SCOPES
    return s.split() if isinstance(s, str) else list(s)

def _signin_creds():
    """The saved sign-in with the scopes it was granted: refreshing never asks for more, so a YouTube-only sign-in
    from 1.11.0 or older keeps uploading as it is."""
    from google.oauth2.credentials import Credentials
    info = read_token(YT_TOKEN_F)
    if not info: return None
    try: return Credentials.from_authorized_user_info(info, granted_scopes(info))
    except Exception: return None

def signin_kind():
    """'none' (no sign-in), 'youtube' (YouTube only: from an older version, no identity) or 'full' (identity too)."""
    info = read_token(YT_TOKEN_F)
    if not info: return 'none'
    return 'full' if 'openid' in granted_scopes(info) else 'youtube'

def signin_json(creds):
    """google-auth's JSON for a new sign-in, its "scopes" set to what Google granted (Google lets the player untick a
    permission; google-auth would save the scopes asked for)."""
    info = json.loads(creds.to_json())
    got = getattr(creds, 'granted_scopes', None)
    if got:
        info['scopes'] = got.split() if isinstance(got, str) else list(got)
    return json.dumps(info)

def youtube_service(interactive=True, fresh=False):
    """YouTube upload, through the one Google sign-in (the player picks the team channel at sign-in). `fresh`: sign
    in again even with a working sign-in (a YouTube-only one from an older version; it is kept if that fails)."""
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    creds = None if fresh and interactive else _signin_creds()
    if creds and YT_SCOPES[0] not in (creds.scopes or []): creds = None   # YouTube unticked at sign-in
    if creds and creds.expired and creds.refresh_token:
        try: creds.refresh(Request())
        except Exception: creds = None
    if not creds or not creds.valid:
        if not interactive: return None
        client = os.path.join(HERE, 'client.json')
        if not os.path.exists(client): raise RuntimeError('This build has no Google sign-in ID (client.json). Ask your coach for a new build.')
        os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'          # a permission unticked on Google's page is no crash
        creds = InstalledAppFlow.from_client_secrets_file(client, SIGNIN_SCOPES).run_local_server(port=0, open_browser=True,
            success_message='PCS Scanner is signed in with Google and can upload to the team YouTube channel. You can close this tab.')
        write_token(YT_TOKEN_F, signin_json(creds))
        if YT_SCOPES[0] not in granted_scopes(read_token(YT_TOKEN_F)):
            raise RuntimeError('YouTube upload was not allowed on Google\'s page. Sign in again and tick YouTube.')
    return build('youtube', 'v3', credentials=creds, cache_discovery=False)

def google_id_token():
    """A fresh Google ID token of the player's sign-in (about an hour valid), for the team hub to learn which Google
    account this player is; None without a sign-in, with a YouTube-only one, or when Google can't be reached.
    The token and the email in it are never logged."""
    from google.auth.transport.requests import Request
    creds = _signin_creds()
    if not creds or 'openid' not in (creds.scopes or []) or not creds.refresh_token:
        return None
    try:
        creds.refresh(Request())
    except Exception as e:
        logging.info('Google sign-in not refreshed for the hub (%s)', e.__class__.__name__)
        return None
    try: write_token(YT_TOKEN_F, signin_json(creds))
    except Exception: pass
    return getattr(creds, 'id_token', None) or None

# ---------------- the old Drive sign-in (removed at start) ----------------
# Up to 1.10.x results went to Google Drive with a sign-in of their own (the file below) holding only the
# drive.file permission. Drive results are gone since 1.11.0; the coach agreed (20 Nov 2026) to remove that file.
# Only that file, and only while it is plainly the old kind: every permission in it is a Drive one. A file with any
# other permission (YouTube, an account sign-in), without a list of permissions, or that can't be read here is left
# alone, and youtube_token.json is never touched.
OLD_DRIVE_TOKEN_F = os.path.join(DATA, 'google_token.json')
_OLD_DRIVE_SCOPE = 'https://www.googleapis.com/auth/drive'

def remove_old_drive_signin(path=None):
    """Remove the old Drive-only sign-in file. Returns True when it was removed."""
    path = path or OLD_DRIVE_TOKEN_F
    if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(YT_TOKEN_F)): return False
    if not os.path.isfile(path): return False
    info = read_token(path)
    scopes = (info or {}).get('scopes')
    if isinstance(scopes, str): scopes = scopes.split()
    if not isinstance(scopes, list) or not scopes: return False
    if not all(isinstance(s, str) and (s == _OLD_DRIVE_SCOPE or s.startswith(_OLD_DRIVE_SCOPE + '.')) for s in scopes):
        return False
    try:
        os.remove(path)
    except OSError as e:
        logging.warning('old Drive sign-in not removed (%s)', e.__class__.__name__)
        return False
    logging.info('old Google Drive sign-in removed (not used since 1.11.0)')
    return True

# ---------------- team file: weapon icons for the kill feed ----------------
# The scanner names kill-feed weapons from a small private file the coach builds from the game's own icons. It is
# not in this (public) app: the coach shares it on Google Drive as "Anyone with the link" and each player pastes that
# link once in Settings. The app's Google sign-in can't be used for this: its drive.file permission only sees files
# the same person's app created.
ICONS_F = os.path.join(DATA, 'pcs_weapons.npz')
ICONS_MAX = 2 * 1024 * 1024

def drive_link_id(link):
    """The file ID in a Google Drive share link, or None (only Google Drive addresses are accepted)."""
    import re, urllib.parse
    try: u = urllib.parse.urlparse((link or '').strip())
    except Exception: return None
    if u.scheme != 'https' or u.hostname not in ('drive.google.com', 'docs.google.com'): return None
    m = re.search(r'/d/([A-Za-z0-9_-]{20,})', u.path) or re.search(r'(?:^|&)id=([A-Za-z0-9_-]{20,})(?:&|$)', u.query)
    return m.group(1) if m else None

def http_get(url, limit):
    req = urllib.request.Request(url, headers={'User-Agent': 'PCS-Scanner'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(limit + 1)

def icons_valid(data):
    """True for a weapon icons file: a small numpy archive of little grey pictures, nothing else."""
    import io
    import numpy as np
    try:
        if not data or len(data) > ICONS_MAX or data[:2] != b'PK': return False
        z = np.load(io.BytesIO(data), allow_pickle=False)
        if not 1 <= len(z.files) <= 300: return False
        return all(z[n].ndim == 2 and z[n].dtype == np.uint8 and z[n].shape[0] <= 64 and z[n].shape[1] <= 704 for n in z.files)
    except Exception:
        return False

def fetch_icons(link=None):
    """Download the weapon icons from the team link. -> (status, text for the player);
    status: none (no link) / bad_link / error / same / updated."""
    link = SETTINGS.get('team_link', '') if link is None else link
    if not (link or '').strip(): return 'none', ''
    fid = drive_link_id(link)
    if not fid: return 'bad_link', "That isn't a Google Drive link. Paste the link your coach sent."
    try:
        data = http_get(f'https://drive.google.com/uc?export=download&id={fid}', ICONS_MAX)
    except Exception as e:
        logging.info('team link: download failed (%s)', type(e).__name__)
        return 'error', "The team link couldn't be reached. It is tried again later."
    if not icons_valid(data):
        return 'error', "The team link didn't give the weapon icons. Ask your coach to check the link."
    try:
        if os.path.exists(ICONS_F) and open(ICONS_F, 'rb').read() == data: return 'same', 'Weapon icons are up to date.'
        tmp = ICONS_F + '.part'
        with open(tmp, 'wb') as f: f.write(data)
        os.replace(tmp, ICONS_F)
    except Exception:
        logging.exception('team link: saving the icons')
        return 'error', "The weapon icons couldn't be saved on this PC."
    return 'updated', 'Weapon icons updated: new scans name the weapons in the kill feed.'

# ---------------- updates ----------------
def vtuple(v):
    try: return tuple(int(x) for x in v.lstrip('v').split('.'))
    except Exception: return (0,)

def check_update():
    """Latest GitHub release of the app repo; returns (version, exe_url, page) or None."""
    repo = BUILD.get('repo')
    current = str(BUILD.get('version', 'dev'))
    if current.startswith('dev'):
        # a test build is offered a real release made from the same code or newer (never an older one)
        current = BUILD.get('base')
        if not repo or not current: return None
        older_or_same = lambda tag: vtuple(tag) < vtuple(current)
    else:
        older_or_same = lambda tag: vtuple(tag) <= vtuple(current)
    if not repo: return None
    try:
        req = urllib.request.Request(f'https://api.github.com/repos/{repo}/releases/latest', headers={'Accept': 'application/vnd.github+json', 'User-Agent': APP})
        rel = json.load(urllib.request.urlopen(req, timeout=8))
        tag = rel.get('tag_name', '')
        if older_or_same(tag): return None
        asset = next((a for a in rel.get('assets', []) if a['name'].lower().endswith('.exe')), None)
        if not asset or not update_url_ok(asset.get('browser_download_url')): return None
        return tag, asset['browser_download_url'], rel.get('html_url'), asset.get('size'), asset.get('digest')
    except Exception:
        return None

UPDATE_LOG = os.path.join(DATA, 'update.log')
# The new exe comes only from this app's GitHub releases, over HTTPS (redirects included), and must match the SHA-256
# GitHub lists for the release file (`digest`), so a changed or damaged download is never started.
UPDATE_HOSTS = ('github.com',)                                   # where the release link may point
UPDATE_REDIRECT_HOSTS = ('github.com', 'objects.githubusercontent.com', 'release-assets.githubusercontent.com')

def update_url_ok(url, hosts=UPDATE_HOSTS):
    """An https address of this app's own releases (the repo the build was made from)."""
    import urllib.parse
    try: u = urllib.parse.urlsplit(str(url or ''))
    except ValueError: return False
    if u.scheme != 'https' or (u.hostname or '').lower() not in hosts or u.username or u.password: return False
    if hosts is UPDATE_HOSTS:
        repo = str(BUILD.get('repo') or '').strip('/').lower()
        if not repo or not u.path.lower().startswith(f'/{repo}/releases/download/'): return False
    return True

class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not update_url_ok(newurl, UPDATE_REDIRECT_HOSTS):
            raise urllib.error.URLError('update download redirected to an unexpected address')
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def download(url, dest, progress=None, size=None):
    """Download to dest over HTTPS only. -> SHA-256 (hex) of what was written. progress(done, total) after each
    chunk (total: the server's length, else size, else 0 = unknown)."""
    import hashlib
    opener = urllib.request.build_opener(_HttpsOnlyRedirect)
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={'User-Agent': APP})
    with opener.open(req, timeout=60) as r, open(dest, 'wb') as out:
        try: total = int(r.headers.get('Content-Length') or 0) or int(size or 0)
        except (TypeError, ValueError, AttributeError): total = int(size or 0)
        done = 0
        if progress: progress(0, total)
        while True:
            chunk = r.read(1 << 20)
            if not chunk: break
            h.update(chunk)
            out.write(chunk)
            done += len(chunk)
            if progress: progress(done, total)
    return h.hexdigest()

def ulog(msg):
    try:
        with open(UPDATE_LOG, 'a', encoding='utf-8') as f: f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {msg}\n')
    except OSError: pass

def staged_path(cur=None):
    cur = cur or sys.executable
    return os.path.join(os.path.dirname(cur), 'PCS-Scanner.update.exe')

def install_update(url, log, size=None, show=False, digest=None, step=None):
    """Download the new exe next to the current one and start it with --finish-update: once this app has
    closed, the new exe copies itself over the old one and starts it (log in update.log). show = the player pressed
    Update in the open window: the new version opens its window; otherwise it starts in the tray. digest = GitHub's
    "sha256:<hex>" of the release file: the download must match it. step(name, done=0, total=0) tells the window's
    "Updating" screen where it is: 'download' (bytes so far, of total), 'check' (size + SHA-256), 'restart'."""
    step = step or (lambda name, done=0, total=0: None)
    if not getattr(sys, 'frozen', False): return False
    if not update_url_ok(url):
        ulog(f'refused: not a release address of this app: {url}')
        raise RuntimeError("The update address isn't this app's GitHub release. Download it from the release page.")
    want = str(digest or '').lower()
    want = want[7:] if want.startswith('sha256:') else ''
    cur = sys.executable
    new = staged_path(cur)
    log('Downloading the update…')
    ulog(f'download {url} -> {new}')
    tmp = new + '.part'
    try:
        sha = download(url, tmp, progress=lambda done, total: step('download', done, total), size=size)
    except Exception:
        if os.path.exists(tmp): os.remove(tmp)
        raise
    got = os.path.getsize(tmp)
    step('check', got, size or got)
    with open(tmp, 'rb') as f: head = f.read(2)
    if head != b'MZ' or (size and got != size):
        os.remove(tmp)
        ulog(f'bad download: {got} bytes (expected {size}), header {head!r}')
        raise RuntimeError('The download was incomplete. Try Update again.')
    if want and sha != want:
        os.remove(tmp)
        ulog(f'bad download: SHA-256 {sha}, the release says {want}')
        raise RuntimeError("The download doesn't match the release. Try Update again later.")
    ulog(f'SHA-256 {sha}' + (' matches the release' if want else ' (the release lists none: size checked only)'))
    os.replace(tmp, new)
    ulog(f'downloaded {got} bytes; starting the finisher')
    step('restart', got, size or got)
    spawn_detached([new, '--finish-update', cur, str(os.getpid())] + (['--show'] if show else []))
    log('Update downloaded: PCS Scanner restarts in a few seconds.')
    return True

def spawn_detached(cmd):
    """Start a process that outlives this one, even if this app runs inside a Windows job that closes its
    children when it exits (the old updater's restart could be killed that way)."""
    DETACHED, NEW_GROUP, BREAKAWAY = 0x00000008, 0x00000200, 0x01000000
    try:
        return subprocess.Popen(cmd, creationflags=DETACHED | NEW_GROUP | BREAKAWAY, close_fds=True)
    except OSError:
        return subprocess.Popen(cmd, creationflags=DETACHED | NEW_GROUP, close_fds=True)

def relaunch_args(cur, ok, show):
    """How the finisher starts the app again. Updated: in the tray (--background) with --updated (the new version
    says so in a corner message and never closes itself), or with its window when the player pressed Update in the
    open window. Copy failed: the old version, with its window (it wasn't updated: no "updated" message)."""
    if not ok or show: return [cur]
    return [cur, '--background', '--updated']

def finish_update(cur, old_pid, show=False):
    """Runs in the new exe: wait for the old app to close, copy over it, start it. Never leaves the player
    without an app: if the copy fails, the old version is started again. Without --show (Update pressed in the corner
    message or the tray) the new version starts in the tray (the corner message tells the player). Installed versions
    (1.10.12+) start every new release with this command line: keep `--finish-update <old exe> <old pid> [--show]`
    and one .exe file (PCS-Scanner.exe) per release, which they take as the update."""
    import shutil
    ulog(f'finisher started (waiting for {old_pid})')
    try:
        import psutil
        try: psutil.Process(int(old_pid)).wait(timeout=60)
        except psutil.NoSuchProcess: pass
    except Exception as e:
        ulog(f'wait: {e}'); time.sleep(8)
    ok = False
    for i in range(60):                                    # the old exe can stay locked a moment after exiting
        try:
            shutil.copy2(sys.executable, cur)
            ok = True
            break
        except OSError as e:
            if i == 0 or i % 10 == 9: ulog(f'copy attempt {i + 1}: {e}')
            time.sleep(1)
    args = relaunch_args(cur, ok, show)
    ulog(('copied, starting the new version' if ok else 'copy failed: starting the old version again')
         + (' (window)' if len(args) == 1 else ' (tray)'))
    spawn_detached(args)

def cleanup_staged():
    """Remove the downloaded installer once the new version runs from its usual place."""
    p = staged_path()
    if getattr(sys, 'frozen', False) and os.path.abspath(sys.executable) != os.path.abspath(p) and os.path.exists(p):
        try: os.remove(p)
        except OSError: pass

BUNDLE_MARKERS = ('pcs_maps.json', 'pcs_fonts.json', 'build.json', os.path.join('web', 'index.html'))

def clean_old_unpacked(tmp=None, current=None, min_age_s=600, now=None):
    """The exe unpacks itself (~600 MB) to a temporary folder (_MEI + digits) at every start and removes it when it
    ends; a crash, a forced stop or Windows shutting down leaves it on C:. At start, the app removes such leftovers,
    and only its own: a folder named like that, holding this app's files (BUNDLE_MARKERS and Python's DLL), not the one
    this process runs from, unpacked more than min_age_s ago (not one being unpacked right now), and not in use: a
    running copy of the app keeps Python's DLL loaded, which Windows refuses to delete, so that DLL goes first and a
    refusal leaves the folder untouched. Returns [(folder, bytes)] removed."""
    import re, shutil, tempfile
    tmp = tmp or tempfile.gettempdir()
    current = os.path.normcase(os.path.abspath(current if current is not None else getattr(sys, '_MEIPASS', '') or '?'))
    now = now if now is not None else time.time()
    out = []
    try: names = os.listdir(tmp)
    except OSError: return out
    for name in names:
        p = os.path.join(tmp, name)
        ours = re.fullmatch(r'_MEI\d+\.pcs-old', name)       # renamed by an earlier start, removal unfinished
        if not (ours or re.fullmatch(r'_MEI\d+', name)) or not os.path.isdir(p) or os.path.islink(p): continue
        if os.path.normcase(os.path.abspath(p)) == current: continue
        if not ours:
            try:
                st = os.stat(p)
                if now - getattr(st, 'st_birthtime', st.st_ctime) < min_age_s: continue
                if not all(os.path.isfile(os.path.join(p, m)) for m in BUNDLE_MARKERS): continue    # not this app's
                dlls = [f for f in os.listdir(p) if re.fullmatch(r'python3\d*\.dll', f, re.I)]
                if not dlls: continue
                json.load(open(os.path.join(p, 'build.json'), encoding='utf-8-sig'))['version']
            except Exception: continue
            try:
                for f in dlls: os.remove(os.path.join(p, f))       # in use by a running copy: refused, folder kept
            except OSError: continue
            try: os.rename(p, p + '.pcs-old'); p += '.pcs-old'
            except OSError: pass
        size = 0
        for root, _, files in os.walk(p):
            for f in files:
                try: size += os.path.getsize(os.path.join(root, f))
                except OSError: pass
        shutil.rmtree(p, ignore_errors=True)
        if not os.path.exists(p): out.append((p, size))
    if out:
        logging.info('removed %d leftover unpacked copies of the app (%.0f MB)', len(out), sum(s for _, s in out) / 2 ** 20)
    return out

# ---------------- scanning ----------------
def run_scan(files, log, out=OUT):
    import pcs_scan
    argv = sys.argv
    try:                                 # the player's highlight rules (Settings -> Highlights)
        from pcs_recorder import settings as rec_settings
        rules = ['--highlights', rec_settings.saved_highlight_rules()]
    except Exception:
        rules = []
    try:
        sys.argv = ['pcs_scan'] + list(files) + ['--out', out] + rules
        pcs_scan.main()
    finally:
        sys.argv = argv
    outs = []
    for f in files:
        base = os.path.splitext(os.path.basename(f))[0]
        for ext in ('.scan.json', '.feed.json'):
            p = os.path.join(out, base + ext)
            if os.path.exists(p): outs.append(p)
    return outs
