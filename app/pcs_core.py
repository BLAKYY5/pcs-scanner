"""PCS Scanner shared logic (no window): paths, settings, Google Drive, updates, scanning.
Used by the app window, the tray recorder and the headless --scan mode."""
import os, sys, json, subprocess, urllib.request, logging

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

BUILD = load_json(os.path.join(HERE, 'build.json'), {'version': 'dev', 'repo': '', 'coach': ''})
SETTINGS_F = os.path.join(DATA, 'settings.json')
SETTINGS = load_json(SETTINGS_F, {'player': '', 'role': '', 'uploaded': [], 'library': []})
SETTINGS.setdefault('library', [])   # videos added by hand (the Recordings page lists them)
def save_settings(): json.dump(SETTINGS, open(SETTINGS_F, 'w', encoding='utf-8'), indent=1)
logging.basicConfig(filename=os.path.join(DATA, 'app.log'), level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

SCOPES = ['https://www.googleapis.com/auth/drive.file']
TOKEN_F = os.path.join(DATA, 'google_token.json')
FOLDER = 'PCS Scans'

# ---------------- Google Drive ----------------
def drive_service(interactive=True):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(TOKEN_F, SCOPES) if os.path.exists(TOKEN_F) else None
    if creds and creds.expired and creds.refresh_token:
        try: creds.refresh(Request())
        except Exception: creds = None
    if not creds or not creds.valid:
        if not interactive: return None
        client = os.path.join(HERE, 'client.json')
        if not os.path.exists(client): raise RuntimeError('This build has no Google sign-in ID (client.json). Ask Blakey for a new build.')
        creds = InstalledAppFlow.from_client_secrets_file(client, SCOPES).run_local_server(port=0, open_browser=True,
            success_message='PCS Scanner is signed in. You can close this tab.')
        open(TOKEN_F, 'w').write(creds.to_json())
    return build('drive', 'v3', credentials=creds, cache_discovery=False)

YT_SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
YT_TOKEN_F = os.path.join(DATA, 'youtube_token.json')

def youtube_service(interactive=True):
    """YouTube upload, signed in separately from Drive (the player picks the team channel at sign-in)."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(YT_TOKEN_F, YT_SCOPES) if os.path.exists(YT_TOKEN_F) else None
    if creds and creds.expired and creds.refresh_token:
        try: creds.refresh(Request())
        except Exception: creds = None
    if not creds or not creds.valid:
        if not interactive: return None
        client = os.path.join(HERE, 'client.json')
        if not os.path.exists(client): raise RuntimeError('This build has no Google sign-in ID (client.json). Ask Blakey for a new build.')
        creds = InstalledAppFlow.from_client_secrets_file(client, YT_SCOPES).run_local_server(port=0, open_browser=True,
            success_message='PCS Scanner can now upload to the team YouTube channel. You can close this tab.')
        open(YT_TOKEN_F, 'w').write(creds.to_json())
    return build('youtube', 'v3', credentials=creds, cache_discovery=False)

def scans_folder(svc):
    q = f"name = '{FOLDER}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    r = svc.files().list(q=q, fields='files(id,name)', spaces='drive').execute().get('files', [])
    if r: return r[0]['id']
    fid = svc.files().create(body={'name': FOLDER, 'mimeType': 'application/vnd.google-apps.folder'}, fields='id').execute()['id']
    if BUILD.get('coach'):
        svc.permissions().create(fileId=fid, sendNotificationEmail=False,
                                 body={'type': 'user', 'role': 'reader', 'emailAddress': BUILD['coach']}).execute()
    return fid

def upload(svc, folder, path, log):
    from googleapiclient.http import MediaFileUpload
    name = (SETTINGS.get('player') or 'player') + '__' + os.path.basename(path)
    q = f"name = '{name}' and '{folder}' in parents and trashed = false"
    old = svc.files().list(q=q, fields='files(id)').execute().get('files', [])
    mime = 'audio/mp4' if path.lower().endswith('.m4a') else 'text/plain' if path.lower().endswith('.log') else 'application/json'
    media = MediaFileUpload(path, mimetype=mime, resumable=True, chunksize=8 * 1024 * 1024)
    meta = {'appProperties': {'player': SETTINGS.get('player', ''), 'role': SETTINGS.get('role', ''), 'app': BUILD.get('version', '')}}
    if old: svc.files().update(fileId=old[0]['id'], media_body=media, body=meta).execute()
    else: svc.files().create(body={'name': name, 'parents': [folder], **meta}, media_body=media, fields='id').execute()
    log(f'  uploaded {name}')

def drive_upload_quiet(paths):
    """Upload without any sign-in prompt (used by the recorder). False if the player isn't signed in."""
    svc = drive_service(interactive=False)
    if svc is None: return False
    folder = scans_folder(svc)
    for p in paths:
        if p.endswith('.json'): upload(svc, folder, p, logging.info)
    return True

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
        exe = next((a['browser_download_url'] for a in rel.get('assets', []) if a['name'].lower().endswith('.exe')), None)
        return tag, exe, rel.get('html_url')
    except Exception:
        return None

def install_update(url, log):
    """Download the new exe next to the current one, swap it in with a tiny batch file, restart."""
    if not getattr(sys, 'frozen', False): return False
    cur = sys.executable; new = cur + '.new'
    log('Downloading update ...')
    urllib.request.urlretrieve(url, new)
    bat = os.path.join(DATA, 'update.bat')
    open(bat, 'w').write(f'@echo off\r\nping -n 6 127.0.0.1 >nul\r\nmove /y "{new}" "{cur}" >nul\r\nstart "" "{cur}"\r\ndel "%~f0"\r\n')
    subprocess.Popen(['cmd', '/c', bat], creationflags=0x08000000)
    return True

# ---------------- scanning ----------------
def run_scan(files, log, out=OUT):
    import pcs_scan
    argv = sys.argv
    try:
        sys.argv = ['pcs_scan'] + list(files) + ['--out', out]
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
