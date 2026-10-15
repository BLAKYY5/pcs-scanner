"""PCS Scanner app: scan Apex recordings and upload the results to the team's Google Drive.

Players pick recordings, press Scan; results (.scan.json + .feed.json) go to a "PCS Scans" folder in the player's
own Drive, shared with the coach. The app only asks for Google's drive.file permission: it can see and change only
the files it created itself.
"""
import os, sys, json, threading, queue, traceback, datetime, subprocess, multiprocessing, urllib.request, webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

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
    try:
        import numpy, scipy, PIL, google.oauth2   # noqa: F401  (all bundled in the .exe)
    except ImportError:
        try:
            r = tk.Tk(); r.withdraw()
            if messagebox.askyesno(APP, 'This is the source code of PCS Scanner, not the app.\n\n'
                                        'Download PCS-Scanner.exe instead (it contains everything it needs).\n\nOpen the download now?'):
                webbrowser.open(EXE_URL)
        except Exception:
            print('This is the source code, not the app. Download PCS-Scanner.exe: ' + EXE_URL)
        sys.exit(1)
DATA = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'PCS Scanner')
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
SETTINGS = load_json(SETTINGS_F, {'player': '', 'role': '', 'uploaded': []})
def save_settings(): json.dump(SETTINGS, open(SETTINGS_F, 'w', encoding='utf-8'), indent=1)

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
    media = MediaFileUpload(path, mimetype='application/json', resumable=True)
    meta = {'appProperties': {'player': SETTINGS.get('player', ''), 'role': SETTINGS.get('role', ''), 'app': BUILD.get('version', '')}}
    if old: svc.files().update(fileId=old[0]['id'], media_body=media, body=meta).execute()
    else: svc.files().create(body={'name': name, 'parents': [folder], **meta}, media_body=media, fields='id').execute()
    log(f'  uploaded {name}')

# ---------------- updates ----------------
def vtuple(v):
    try: return tuple(int(x) for x in v.lstrip('v').split('.'))
    except Exception: return (0,)

def check_update():
    """Latest GitHub release of the app repo; returns (version, exe_url, page) or None."""
    repo = BUILD.get('repo')
    if not repo or BUILD.get('version') == 'dev': return None
    try:
        req = urllib.request.Request(f'https://api.github.com/repos/{repo}/releases/latest', headers={'Accept': 'application/vnd.github+json', 'User-Agent': APP})
        rel = json.load(urllib.request.urlopen(req, timeout=8))
        tag = rel.get('tag_name', '')
        if vtuple(tag) <= vtuple(BUILD['version']): return None
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
    open(bat, 'w').write(f'@echo off\r\nping -n 3 127.0.0.1 >nul\r\nmove /y "{new}" "{cur}" >nul\r\nstart "" "{cur}"\r\ndel "%~f0"\r\n')
    subprocess.Popen(['cmd', '/c', bat], creationflags=0x08000000)
    return True

# ---------------- scanning ----------------
def run_scan(files, log):
    import pcs_scan
    argv = sys.argv
    try:
        sys.argv = ['pcs_scan'] + list(files) + ['--out', OUT]
        pcs_scan.main()
    finally:
        sys.argv = argv
    outs = []
    for f in files:
        base = os.path.splitext(os.path.basename(f))[0]
        for ext in ('.scan.json', '.feed.json'):
            p = os.path.join(OUT, base + ext)
            if os.path.exists(p): outs.append(p)
    return outs

class Writer:
    """Send the scanner's print() output to the app's log."""
    def __init__(self, q): self.q = q
    def write(self, s):
        if s.strip(): self.q.put(s.rstrip())
    def flush(self): pass

try:                                   # drag and drop of recordings (and folders) onto the window
    from tkinterdnd2 import TkinterDnD, DND_FILES
    Base = TkinterDnD.Tk
except Exception:
    TkinterDnD = None; Base = tk.Tk

VIDEO_EXT = ('.mp4', '.mkv', '.mov')

class App(Base):
    def __init__(self):
        super().__init__()
        self.title(f'{APP} {BUILD.get("version", "")}'); self.geometry('760x560'); self.minsize(620, 460)
        self.q = queue.Queue(); self.files = []; self.busy = False
        pad = {'padx': 12, 'pady': 6}
        top = ttk.Frame(self); top.pack(fill='x', **pad)
        ttk.Label(top, text='Your EEC name').grid(row=0, column=0, sticky='w')
        self.player = tk.StringVar(value=SETTINGS.get('player', '')); ttk.Entry(top, textvariable=self.player, width=24).grid(row=0, column=1, sticky='w', padx=6)
        ttk.Label(top, text='Role').grid(row=0, column=2, sticky='w', padx=(16, 0))
        self.role = tk.StringVar(value=SETTINGS.get('role', '')); ttk.Combobox(top, textvariable=self.role, values=['IGL', 'Fragger', 'Anchor', 'Support', 'Sub'], width=12, state='readonly').grid(row=0, column=3, sticky='w', padx=6)
        self.gbtn = ttk.Button(top, text='Sign in with Google', command=self.signin); self.gbtn.grid(row=0, column=4, padx=(16, 0))
        mid = ttk.Frame(self); mid.pack(fill='x', **pad)
        ttk.Button(mid, text='Add recordings…', command=self.add).pack(side='left')
        ttk.Button(mid, text='Clear', command=self.clear).pack(side='left', padx=6)
        self.scanbtn = ttk.Button(mid, text='Scan and upload', command=self.start); self.scanbtn.pack(side='right')
        self.lst = tk.Listbox(self, height=6); self.lst.pack(fill='x', **pad)
        self.hint = ttk.Label(self.lst, text='Drag and drop recordings or folders here', foreground='#888')
        if TkinterDnD:
            self.drop_target_register(DND_FILES); self.dnd_bind('<<Drop>>', self.dropped)
            self.dnd_bind('<<DropEnter>>', lambda e: (self.lst.configure(background='#e6f2ff'), e.action)[1])
            self.dnd_bind('<<DropLeave>>', lambda e: (self.lst.configure(background='white'), e.action)[1])
            self.hint.place(relx=0.5, rely=0.5, anchor='center')
        self.prog = ttk.Progressbar(self, mode='indeterminate'); self.prog.pack(fill='x', **pad)
        self.log = tk.Text(self, height=14, wrap='word', state='disabled', font=('Consolas', 9)); self.log.pack(fill='both', expand=True, **pad)
        bot = ttk.Frame(self); bot.pack(fill='x', **pad)
        ttk.Button(bot, text='Open results folder', command=lambda: os.startfile(OUT) if os.name == 'nt' else None).pack(side='left')
        self.status = ttk.Label(bot, text=''); self.status.pack(side='right')
        self.after(100, self.pump)
        self.after(400, self.startup)

    def say(self, s): self.q.put(s)
    def pump(self):
        try:
            while True:
                s = self.q.get_nowait(); self.log.configure(state='normal'); self.log.insert('end', s + '\n'); self.log.see('end'); self.log.configure(state='disabled')
        except queue.Empty: pass
        self.after(150, self.pump)

    def startup(self):
        signed = os.path.exists(TOKEN_F)
        self.gbtn.configure(text='Google: signed in' if signed else 'Sign in with Google')
        self.say(f'{APP} {BUILD.get("version")} — results are saved in {OUT}')
        def upd():
            u = check_update()
            if u:
                tag, exe, page = u
                if exe and messagebox.askyesno(APP, f'Version {tag} is available. Update now? The app restarts by itself.'):
                    if install_update(exe, self.say): self.after(500, self.destroy)
                elif page: self.say(f'Update {tag} available: {page}')
        threading.Thread(target=upd, daemon=True).start()

    def signin(self):
        def go():
            try: drive_service(True); self.say('Signed in with Google.'); self.gbtn.configure(text='Google: signed in')
            except Exception as e: self.say(f'Google sign-in failed: {e}')
        threading.Thread(target=go, daemon=True).start()

    def add(self):
        self.add_files(filedialog.askopenfilenames(title='Pick Apex recordings', filetypes=[('Videos', '*.mp4 *.mkv *.mov'), ('All files', '*.*')]))
    def add_files(self, fs):
        skipped = 0
        for f in fs:
            f = os.path.normpath(f)
            if f.lower().endswith(VIDEO_EXT):
                if f not in self.files: self.files.append(f); self.lst.insert('end', os.path.basename(f))
            else: skipped += 1
        if skipped: self.say(f'Skipped {skipped} file(s) that are not .mp4, .mkv or .mov recordings.')
        if self.files: self.hint.place_forget()
    def dropped(self, e):
        self.lst.configure(background='white')
        fs = []
        for p in self.tk.splitlist(e.data):
            if os.path.isdir(p):           # a dropped folder adds every recording inside it (not subfolders)
                fs += sorted(os.path.join(p, n) for n in os.listdir(p) if n.lower().endswith(VIDEO_EXT))
            else: fs.append(p)
        if self.busy: self.say('A scan is running; dropped recordings are queued for the next scan.')
        self.add_files(fs); return e.action
    def clear(self):
        self.files = []; self.lst.delete(0, 'end')
        if TkinterDnD: self.hint.place(relx=0.5, rely=0.5, anchor='center')

    def start(self):
        if self.busy: return
        if not self.files: messagebox.showinfo(APP, 'Add one or more recordings first.'); return
        if not self.player.get().strip(): messagebox.showinfo(APP, 'Type your EEC name first (so your scans are labelled).'); return
        SETTINGS['player'] = self.player.get().strip(); SETTINGS['role'] = self.role.get(); save_settings()
        self.busy = True; self.scanbtn.configure(state='disabled'); self.prog.start(12)
        threading.Thread(target=self.work, args=(list(self.files),), daemon=True).start()

    def work(self, files):
        old = sys.stdout, sys.stderr; sys.stdout = sys.stderr = Writer(self.q)
        try:
            outs = []
            for f in files:
                outs += run_scan([f], self.say)
            sys.stdout, sys.stderr = old
            self.say('Uploading to Google Drive ...')
            try:
                svc = drive_service(True); folder = scans_folder(svc)
                for p in outs: upload(svc, folder, p, self.say)
                SETTINGS['uploaded'] = (SETTINGS.get('uploaded', []) + [os.path.basename(p) for p in outs])[-500:]; save_settings()
                self.say(f'Done: {len(outs)} file(s) uploaded. The Loot Book picks them up overnight.')
            except Exception as e:
                self.say(f'Upload failed ({e}). The results are still saved in {OUT}; press Scan again later to retry.')
        except Exception:
            sys.stdout, sys.stderr = old
            self.say('Scan failed:\n' + traceback.format_exc())
        finally:
            sys.stdout, sys.stderr = old
            self.busy = False; self.after(0, lambda: (self.prog.stop(), self.scanbtn.configure(state='normal')))

if __name__ == '__main__':
    multiprocessing.freeze_support()   # the scanner splits recordings into parallel chunks
    if '--selftest' in sys.argv:       # used by the build: proves the scanner's libraries load inside the exe
        out = sys.argv[sys.argv.index('--selftest') + 1]
        try:
            import numpy, scipy.ndimage, PIL.Image, pcs_scan, pcs_hud, tkinterdnd2
            numpy.zeros((4, 4)).sum(); scipy.ndimage.label(numpy.ones((3, 3)))
            w = TkinterDnD.Tk(); w.withdraw(); w.drop_target_register(DND_FILES); w.destroy()   # loads the drag-and-drop Tk extension
            ff = os.path.join(getattr(sys, '_MEIPASS', ''), 'ffmpeg', 'ffmpeg.exe')
            msg = f'OK numpy {numpy.__version__} scipy {scipy.__version__} scanner {pcs_scan.VERSION} ffmpeg {os.path.exists(ff)} upload {os.path.exists(os.path.join(HERE, "client.json"))} dragdrop {TkinterDnD is not None}'
        except Exception:
            msg = 'FAIL ' + traceback.format_exc()
        open(out, 'w').write(msg); sys.exit(0)
    App().mainloop()
