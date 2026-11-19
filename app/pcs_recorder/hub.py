"""The team hub (PCS Hub): every request the app makes to it goes through `request()` here.

The key
- Each player has a personal key from the coach (`pcs_` + 43 characters), sent as the header
  `Authorization: Bearer <key>` on every request. Never in a URL, a log line or a file that gets uploaded.
- Kept in the data folder (`hub_key.dat`), encrypted for this Windows user (DPAPI): another Windows account or
  another PC can't read the file. Not in `recorder.json`, so settings files shared for help never carry it.
- The app's own page only ever gets it masked (`pcs_…abcd`).
- The coach sends players a sign-in link `http://<hub>/login#key=pcs_...`: pasting it gives the key, and the
  address when none is set yet.
- Or a single-use invite link `http://<hub>/join#code=pcsi_...`: the app sends the code to the hub (in the request
  body, no key), gets the player's new key once, and keeps it and the link's address. The code is never logged.

Answers
- 401 (no key / wrong or revoked key): the hub is marked "key refused"; no upload is tried again until the key
  (or the hub address) changes. 429 (too many wrong keys): nothing is sent before `Retry-After` has passed.
  Network errors: the caller retries later, as before.
- A hub from before keys (0.2) ignores the header and has no /api/ping or /api/me: everything else works as
  before, with or without a key set.
- Every request also says which app version sends it (`X-PCS-App: 1.12.0`): the coach's health page shows who still
  runs an older app. Only the version; a hub that doesn't know the header ignores it.

Addresses
- `http://` (home network, Tailscale) and `https://` (the public hub behind Cloudflare, `https://hub.<domain>`)
  both work for every request, the invite / sign-in links and the Watch together websocket (`wss://`). Certificates
  are always checked (requests' and websocket-client's defaults; nothing here ever turns that off).
- Why the hub can't be reached is told in four words a player understands (`reach`): no internet, hub address
  wrong (name not found, or a certificate that isn't the hub's), hub down (nothing answers, or Cloudflare says the
  hub behind it is off), key refused. Settings → Team hub shows the latest one; the recorder writes one Activity
  line per change. "No internet" is told apart from "address wrong" by looking up Windows' own connectivity-check
  name (`www.msftconnecttest.com`, a name lookup only: nothing is sent to it).

Uploads in parts (hub 0.6 `/api/uploads`, contract in the hub's AGENT.md)
- Cloudflare refuses request bodies over 100 MB. A file over `PART_FILE` (50 MB, the hub's part size), or a set of
  small files together over `ONESHOT_MAX` (90 MB), goes in parts of at most 50 MB; smaller ones keep the one-shot
  routes (`POST /api/scans`, `POST /api/session-audio`). A hub without the route (404 / 405) gets the one-shot route.
- A lost connection or a restarted app goes on where it stopped: the upload id is kept in `hub-uploads.json` (the
  file's path, size and time; never the key) and the hub says how much it already has.
- `stop()` (Apex started) is asked between parts: the upload pauses (`HubError` kind `paused`) and goes on later.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .settings import APP_DIR

log = logging.getLogger("hub")
KEY_FILE = APP_DIR / "hub_key.dat"
KEY_RE = re.compile(r"pcs_[A-Za-z0-9_-]{43}")
VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,31}")
TIMEOUT = 10
REFUSED = "The team hub refused your key: paste a new one in Settings → Team hub"
NO_KEY = "The team hub needs your key: paste it in Settings → Team hub"
_ENTROPY = b"PCS Scanner team hub key"


class HubError(Exception):
    """The hub couldn't be reached or said no. `kind`: network | refused | wait | http | paused.
    `reach`: why it couldn't be reached ("offline" | "address" | "down" | "refused"), "" = not known / not that.
    `code`: the hub's error code (`bad_checksum`, `too_many_uploads`...), "" when none."""

    def __init__(self, text: str, kind: str, status: int = 0, reach: str = "", code: str = ""):
        super().__init__(text)
        self.kind, self.status, self.reach, self.code = kind, status, reach, code


# why the hub can't be reached: the text in Settings / Activity, and the few words on a recording
REACH_TEXT = {
    "offline": "No internet connection",
    "address": "Hub address not found: check it in Settings → Team hub",
    "cert": "Hub address wrong: its certificate isn't valid. Check it in Settings → Team hub",
    "down": "Hub down: the hub PC is off or the hub is stopped",
    "refused": "The team hub refused your key: paste a new one in Settings → Team hub",
}
REACH_SHORT = {"offline": "No internet", "address": "Hub address wrong", "down": "Hub down", "refused": "Key refused"}
DOWN_CODES = {502, 503, 504} | set(range(520, 531))     # Cloudflare: the hub behind it is off or not answering


def reason(e: Exception) -> str:
    """Why the hub didn't take a recording's results, as the player reads it on the recording."""
    kind = getattr(e, "kind", "network")
    reach = getattr(e, "reach", "")
    if kind == "refused":
        return "Key refused"
    if kind == "wait":
        return "Hub asks to wait"
    if kind == "paused":
        return "Waits for the game to close"
    if reach in REACH_SHORT:
        return REACH_SHORT[reach]
    if kind == "http":
        return f"Hub error {getattr(e, 'status', '')}".strip()
    return "Hub not reachable"


# ------------------------------------------------------------------ why the hub can't be reached

PROBE_HOST = "www.msftconnecttest.com"        # Windows' own connectivity check name: looked up, never contacted
_probe = {"at": 0.0, "up": True}


def internet_up() -> bool:
    """Does this PC reach the internet at all? A name lookup of Windows' connectivity-check host (no request is
    made to it), at most every 30 s, 3 s at most. Tests replace this function."""
    import socket
    now = time.time()
    if now - _probe["at"] < 30:
        return _probe["up"]
    res = {"up": False}

    def look():
        try:
            socket.getaddrinfo(PROBE_HOST, 443)
            res["up"] = True
        except OSError:
            pass
    t = threading.Thread(target=look, daemon=True)
    t.start()
    t.join(3)
    _probe.update(at=now, up=res["up"])
    return res["up"]


def _chain(e: BaseException) -> list[BaseException]:
    """The exception and those inside it (requests wraps urllib3, which wraps the socket error)."""
    seen, todo = [], [e]
    while todo and len(seen) < 20:
        x = todo.pop(0)
        if not isinstance(x, BaseException) or any(x is y for y in seen):
            continue
        seen.append(x)
        todo += [x.__cause__, x.__context__, getattr(x, "reason", None)] + list(getattr(x, "args", ()))
    return seen


def classify(e: BaseException) -> tuple[str, str]:
    """A failed request's exception → (reach, text for the player): "offline" | "address" | "down"."""
    import socket
    import ssl
    import requests
    parts = _chain(e)
    text = " ".join(f"{x.__class__.__name__} {x}" for x in parts)
    if isinstance(e, requests.exceptions.SSLError) or any(isinstance(x, ssl.SSLError) for x in parts):
        return "address", REACH_TEXT["cert"]
    refused = any(isinstance(x, ConnectionRefusedError) for x in parts) or "10061" in text \
        or "Connection refused" in text
    if refused:                                   # something answered "nobody here": the network works
        return "down", REACH_TEXT["down"]
    name = any(isinstance(x, socket.gaierror) for x in parts) or "NameResolution" in text \
        or "getaddrinfo failed" in text or "Name or service not known" in text or "nodename nor servname" in text
    if not internet_up():
        return "offline", REACH_TEXT["offline"]
    if name:
        return "address", REACH_TEXT["address"]
    return "down", REACH_TEXT["down"]


def _set_reach(url: str, state: str, text: str = "") -> None:
    with S.lock:
        S.reach = {"url": url, "state": state, "text": text, "at": time.time()}


# ------------------------------------------------------------------ the key: parsing, storage

def parse_key(text) -> tuple[str | None, str | None]:
    """A pasted key, or a sign-in link `http(s)://host:port/login#key=pcs_...`: (key, hub address of the link).
    (None, None) when it is neither."""
    t = str(text or "").strip()
    if KEY_RE.fullmatch(t):
        return t, None
    if "://" in t:
        try:
            u = urlsplit(t)
            netloc = u.netloc
        except ValueError:
            return None, None
        m = re.search(r"(?:^|&)key=(pcs_[A-Za-z0-9_-]{43})(?:&|$)", unquote(u.fragment))
        if u.scheme in ("http", "https") and netloc and "@" not in netloc and m:
            return m.group(1), f"{u.scheme}://{netloc}"
    return None, None


def parse_invite(text) -> tuple[str | None, str | None]:
    """A single-use invite link from the coach, `http(s)://host:port/join#code=pcsi_...`: (code, hub address of the
    link). (None, None) when it isn't one. The code is never logged nor repeated anywhere."""
    t = str(text or "").strip()
    if "://" not in t:
        return None, None
    try:
        u = urlsplit(t)
        netloc = u.netloc
    except ValueError:
        return None, None
    m = re.search(r"(?:^|&)code=(pcsi_[A-Za-z0-9_-]{43})(?:&|$)", unquote(u.fragment))
    if u.scheme in ("http", "https") and netloc and "@" not in netloc and m:
        return m.group(1), f"{u.scheme}://{netloc}"
    return None, None


INVITE_USED = "If you opened this link in a browser first, paste the app link it showed you."
TOO_MANY = "Too many tries, try again later."


def _no_secrets(text: str) -> str:
    """A hub's text with anything that looks like a key or an invite code taken out."""
    return re.sub(r"pcsi?_[A-Za-z0-9_-]{8,}", "…", str(text or ""))[:200]


def redeem(url: str, code: str) -> str:
    """Use an invite: POST /api/invites/redeem {"code"} with no key and no cookie. Returns the new key (made by the
    hub now and given only this once). Raises HubError with the text for the player; never the code."""
    import requests
    try:
        r = requests.post(url + "/api/invites/redeem", json={"code": code}, timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException as e:
        reach, text = classify(e)
        _set_reach(url, reach, text)
        raise HubError(text, "network", reach=reach) from None
    if r.status_code in DOWN_CODES:
        _set_reach(url, "down", REACH_TEXT["down"])
        raise HubError(REACH_TEXT["down"], "http", r.status_code, reach="down")
    if r.status_code in (301, 302, 303, 307, 308):
        raise HubError(_redirect_text(url, r), "http", r.status_code, reach="address")
    detail, error = "", ""
    try:
        d = r.json()
        if isinstance(d, dict):
            detail = _no_secrets(d.get("detail")) if isinstance(d.get("detail"), str) else ""
            error = str(d.get("error") or "")
    except ValueError:
        d = None
    if r.status_code == 200:
        key = str((d or {}).get("key") or "") if isinstance(d, dict) else ""
        if not KEY_RE.fullmatch(key):
            raise HubError("The team hub's answer had no key. Ask your coach for a new link.", "http", 200)
        log.info("team hub: invite used")
        return key
    log.info("team hub: invite not taken (%s %s)", r.status_code, error[:40])
    if r.status_code == 429:
        raise HubError(TOO_MANY, "wait", 429)
    if r.status_code == 400 and error == "invalid_code":
        raise HubError(((detail or "This invite link doesn't work.").rstrip() + " " + INVITE_USED).strip(), "http", 400)
    if r.status_code == 409:
        raise HubError(detail or "A key with that name already exists. Ask your coach.", "http", 409)
    if r.status_code == 404:
        raise HubError("This hub doesn't take invite links yet. Ask your coach for the sign-in link.", "http", 404)
    raise HubError(f"the team hub answered {r.status_code}" + (f": {detail}" if detail else ""), "http", r.status_code)


def mask(key: str | None) -> str:
    return f"pcs_…{key[-4:]}" if key else ""


def _dpapi(data: bytes, protect: bool, entropy: bytes = _ENTROPY) -> bytes:
    """Windows' own per-user encryption (the one browsers use for saved passwords). Also used for the Google and
    YouTube sign-ins (pcs_core.read_token), with their own `entropy`."""
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes):
        buf = ctypes.create_string_buffer(b, len(b))
        return BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    src, _keep1 = blob(data)
    ent, _keep2 = blob(entropy)
    out = BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out)):   # 0x1: no UI
        raise OSError("Windows couldn't " + ("protect" if protect else "read") + " the hub key")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.loaded, self.key = False, None
        self.refused: tuple[str, str] | None = None     # (hub address, key fingerprint) the hub said 401 to
        self.wait_until = 0.0                           # 429: nothing before this time
        self.last: dict = {}                            # last Test result (shown in Settings and Team)
        self.old: str | None = None                     # hub address known to be a version without keys
        self.reach: dict = {}                           # last request's outcome: {url, state, text, at}


S = _State()


def _fp(key: str | None) -> str:
    return hashlib.sha256((key or "").encode()).hexdigest()[:16]


def get_key() -> str | None:
    with S.lock:
        if not S.loaded:
            S.loaded, S.key = True, _read_key()
        return S.key


def _read_key() -> str | None:
    try:
        raw = json.loads(KEY_FILE.read_text(encoding="utf-8"))
        if "dpapi" in raw:
            key = _dpapi(base64.b64decode(raw["dpapi"]), False).decode()
        else:
            key = raw.get("plain", "")
        return key if KEY_RE.fullmatch(key or "") else None
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("hub key not readable (%s): paste it again", e.__class__.__name__)
        return None


def set_key(key: str | None) -> None:
    """Save (or with None, remove) the key. A new key clears "refused" and "wait"."""
    if key is not None and not KEY_RE.fullmatch(key):
        raise ValueError("That isn't a hub key. Paste the key or the sign-in link from your coach.")
    with S.lock:
        if key is None:
            KEY_FILE.unlink(missing_ok=True)
        else:
            KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                data = {"dpapi": base64.b64encode(_dpapi(key.encode(), True)).decode()}
            else:                                           # not Windows (never shipped): file in the user's folder
                data = {"plain": key}
            tmp = KEY_FILE.with_suffix(".part")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, KEY_FILE)
        S.loaded, S.key = True, key
        S.refused, S.wait_until, S.last = None, 0.0, {}


def reset() -> None:
    """Forget what the hub said (tests; and a changed hub address)."""
    with S.lock:
        S.refused, S.wait_until, S.last, S.old, S.reach = None, 0.0, {}, None, {}


def public_state(url: str) -> dict:
    """What the app's page may see: never the key itself."""
    key = get_key()
    refused = bool(url and S.refused == (url, _fp(key)))
    reach = S.reach if url and S.reach.get("url") == url else {}
    if refused:
        reach = {"state": "refused", "text": REACH_TEXT["refused"], "at": reach.get("at", 0)}
    return {"url": url or "", "key": mask(key), "refused": refused,
            "reach": {k: reach[k] for k in ("state", "text", "at") if k in reach},
            "wait": max(0, int(S.wait_until - time.time())),
            "last": {k: v for k, v in S.last.items() if k != "url"} if S.last.get("url") == url else {}}


# ------------------------------------------------------------------ requests

def app_version() -> str:
    """This app's version for the `X-PCS-App` header (pcs_core's build.json), "" when not known."""
    core = sys.modules.get("pcs_core")
    v = str((getattr(core, "BUILD", None) or {}).get("version") or "") if core else ""
    return v if VERSION_RE.fullmatch(v) else ""


def request(url: str, method: str, path: str, *, timeout: float = TIMEOUT, force: bool = False, **kw):
    """One request to the hub, with the key. Raises HubError for: no answer (network), 401 (refused), 429 (wait).
    Other answers (200, 404, 500…) are returned. `force` (the Test button) tries even after a refusal."""
    import requests
    if not url:
        raise HubError("No team hub address set", "network")
    key = get_key()
    now = time.time()
    if S.wait_until > now:
        raise HubError(f"The team hub asks to wait {int((S.wait_until - now) // 60) + 1} min (too many wrong keys)",
                       "wait", 429)
    if not force and S.refused == (url, _fp(key)):
        raise HubError(REFUSED if key else NO_KEY, "refused", 401)
    headers = dict(kw.pop("headers", None) or {})
    if key:
        headers["Authorization"] = "Bearer " + key
    if app_version():
        headers.setdefault("X-PCS-App", app_version())
    try:
        r = requests.request(method, url + path, headers=headers, timeout=timeout, allow_redirects=False, **kw)
    except requests.RequestException as e:
        reach, text = classify(e)
        _set_reach(url, reach, text)
        raise HubError(text, "network", reach=reach) from None
    if r.status_code in DOWN_CODES:
        _set_reach(url, "down", REACH_TEXT["down"])
    elif r.status_code != 401:
        _set_reach(url, "ok")
    if r.status_code == 401:
        with S.lock:
            S.refused = (url, _fp(key))
        log.info("team hub: key refused (%s)", _code(r))
        raise HubError(REFUSED if key else NO_KEY, "refused", 401, reach="refused")
    if r.status_code == 429 and _code(r) == "too_many_uploads":
        return r                                     # not the wrong-key limit: the caller says what to do
    if r.status_code == 429:
        try:
            wait = max(1, int(r.headers.get("Retry-After") or 60))
        except ValueError:
            wait = 60
        with S.lock:
            S.wait_until = time.time() + wait
        raise HubError(f"The team hub asks to wait {wait // 60 + 1} min (too many wrong keys)", "wait", 429)
    return r


def _code(r) -> str:
    try:
        d = r.json()
        return str(d.get("error") or d.get("detail") or r.status_code)[:80]
    except Exception:
        return str(r.status_code)


def _check(r) -> None:
    if r.status_code in DOWN_CODES:
        raise HubError(REACH_TEXT["down"], "http", r.status_code, reach="down")
    if r.status_code >= 400:
        detail, code = "", ""
        try:
            d = r.json()
            detail = d.get("detail") if isinstance(d.get("detail"), str) else ""
            code = str(d.get("error") or "")[:40]
        except Exception:
            pass
        raise HubError(f"the team hub answered {r.status_code}" + (f": {detail[:120]}" if detail else ""), "http",
                       r.status_code, code=code)


def _redirect_text(url: str, r) -> str:
    """A 3xx where the hub should answer: http → https of the same name (use the https address), or a page that
    isn't the hub (Cloudflare Access sign-in, a router page...)."""
    to = _https_of(url, r)
    return f"Use {to} as the hub address" if to else "No team hub at this address"


def _https_of(url: str, r) -> str:
    """The https address of the same host when the hub's answer sends there (Cloudflare does for http://), else ""."""
    try:
        loc, here = urlsplit(r.headers.get("Location") or ""), urlsplit(url)
    except ValueError:
        return ""
    if here.scheme == "http" and loc.scheme == "https" and loc.hostname and loc.hostname == here.hostname:
        return f"https://{loc.netloc}"
    return ""


def get_json(url: str, path: str, timeout: float = TIMEOUT):
    r = request(url, "GET", path, timeout=timeout)
    _check(r)
    return r.json()


def report(url: str, data: dict) -> None:
    """Recorder report (recording / recorded / uploaded…): best effort, short timeout."""
    _check(request(url, "POST", "/api/recorder/sessions", json=data, timeout=5))


def upload(url: str, files: list[Path], player: str = "", stop=None) -> dict:
    """The scan results of one recording. Small files go in one request (the hub imports the scan with its feed);
    a file over PART_FILE (a mic track, a huge feed), or small files together over ONESHOT_MAX, go in parts
    (`send_in_parts`; a hub without /api/uploads gets them one-shot). Returns the hub's answers merged. A 200 means
    the hub stored them, even if its import failed: never sent again. `stop()` true = pause between parts."""
    files = [Path(f) for f in files]
    sizes = {f: f.stat().st_size for f in files}
    big = [f for f in files if sizes[f] > PART_FILE]
    small = [f for f in files if f not in big]
    if sum(sizes[f] for f in small) > ONESHOT_MAX:
        big, small = files, []
    answers = []
    for f in big:                                    # parts first: a pause or failure leaves nothing half-imported
        a = send_in_parts(url, "scan", f, {"uploader": player} if player else {}, stop=stop)
        if a is None:                                # hub before 0.6: one request for this file
            small.append(f)
        else:
            answers.append(a)
    if small:
        answers.append(_one_shot(url, small, player))
    _forget_done(url, "scan", big)
    return _merge(answers)


def _one_shot(url: str, files: list[Path], player: str) -> dict:
    handles = [("files", (Path(f).name, open(f, "rb"))) for f in files]
    try:
        r = request(url, "POST", "/api/scans", files=handles, data={"uploader": player}, timeout=300)
    finally:
        for _, (_, h) in handles:
            h.close()
    _check(r)
    try:
        d = r.json()
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _merge(answers: list[dict]) -> dict:
    """Several answers of the hub for one recording as one (lists joined: `scan_ids`, `imports`)."""
    out: dict = {}
    for a in answers:
        for k, v in (a or {}).items():
            if isinstance(v, list):
                out.setdefault(k, []).extend(v)
            else:
                out.setdefault(k, v)
    return out


# ------------------------------------------------------------------ uploads in parts (hub 0.6 /api/uploads)

PART_FILE = 50 * 1024 * 1024          # a file over this goes in parts (the hub's default part size)
ONESHOT_MAX = 90 * 1024 * 1024        # one request stays under this (Cloudflare refuses bodies over 100 MB)
PART_MAX = 50 * 1024 * 1024           # never a bigger part, whatever the hub says
UPLOADS_FILE = APP_DIR / "hub-uploads.json"
PAUSED = "Paused while a game runs: goes on when it closes"
_up_lock = threading.Lock()


def _ident(url: str, kind: str, f: Path) -> str:
    st = f.stat()
    return f"{url}|{kind}|{f.resolve()}|{st.st_size}|{st.st_mtime_ns}"


def _load_ups() -> dict:
    try:
        d = json.loads(UPLOADS_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_up(ident: str, value: dict | None) -> None:
    """Upload ids in progress (and finished ones of a recording not fully sent yet), by file. Never the key."""
    with _up_lock:
        d = _load_ups()
        if value is None:
            d.pop(ident, None)
        else:
            d[ident] = value
        week = time.time() - 7 * 86400              # the hub forgets unfinished parts after 24 h anyway
        d = {k: v for k, v in d.items() if isinstance(v, dict) and float(v.get("at") or 0) > week}
        UPLOADS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = UPLOADS_FILE.with_suffix(".part")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, UPLOADS_FILE)


def _forget_done(url: str, kind: str, files: list[Path]) -> None:
    """Everything of that upload reached the hub: its finished parts uploads are forgotten."""
    for f in files:
        try:
            _save_up(_ident(url, kind, Path(f)), None)
        except OSError:
            pass


def _sha256(f: Path) -> str:
    h = hashlib.sha256()
    with open(f, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _json(r) -> dict:
    try:
        d = r.json()
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


class _Stopped(Exception):
    """Raised from inside a part being sent: stops the request at once (not a network error)."""


class _PartBody:
    """One part's bytes, read by http.client in small blocks while it sends: `stop()` (Apex started) is asked at
    most every 0.2 s and stops the request in flight, so a 50 MB part on a slow line never holds a game start."""

    def __init__(self, data: bytes, stop=None):
        self.data, self.pos, self.stop, self.asked = data, 0, stop, 0.0

    def __len__(self) -> int:                       # requests sends Content-Length (no chunked encoding)
        return len(self.data)

    def read(self, n: int = -1) -> bytes:
        if self.stop is not None and time.monotonic() - self.asked >= 0.2:
            self.asked = time.monotonic()
            if self.stop():
                raise _Stopped()
        n = len(self.data) - self.pos if n is None or n < 0 else n
        out = self.data[self.pos:self.pos + n]
        self.pos += len(out)
        return out


def send_in_parts(url: str, kind: str, f: Path, fields: dict | None = None, stop=None, tries: int = 3):
    """One file through /api/uploads: start (or carry on with the upload id kept from an earlier try), parts of at
    most 50 MB from where the hub is, finish. Returns the finish answer (= the one-shot route's answer), or None when
    the hub has no /api/uploads (404 / 405 at the start: the caller sends it one-shot). Raises HubError: network
    (the id is kept: the next try goes on from there), http (the hub said no), paused (`stop()` said so)."""
    f = Path(f)
    ident = _ident(url, kind, f)
    size = f.stat().st_size
    kept = _load_ups().get(ident) or {}
    if kept.get("done") is not None:                 # finished on an earlier try of the same recording
        return kept["done"]
    uid, received, part = kept.get("id"), 0, PART_MAX
    if uid:
        r = request(url, "GET", f"/api/uploads/{uid}", timeout=TIMEOUT)
        if r.status_code == 200:
            d = _json(r)
            received, part = int(d.get("received") or 0), int(d.get("part_max") or PART_MAX)
            log.info("team hub: %s goes on at %d MB", f.name, received // (1024 * 1024))
        else:                                        # finished, deleted or expired: start again
            uid = None
    if not uid:
        if stop is not None and stop():
            raise HubError(PAUSED, "paused")
        body = {"kind": kind, "name": f.name, "size": size, "sha256": _sha256(f), **(fields or {})}
        r = request(url, "POST", "/api/uploads", json=body, timeout=TIMEOUT)
        if r.status_code == 405 or (r.status_code == 404 and _code(r) != "not_found"):
            return None                              # no /api/uploads (hub before 0.6); not_found = no such session
        if r.status_code == 429:
            raise HubError("The team hub has too many unfinished uploads from this PC: try later", "http", 429,
                           code="too_many_uploads")
        _check(r)
        d = _json(r)
        uid = str(d.get("id") or "")
        if not re.fullmatch(r"up-[0-9a-f]{32}", uid):
            raise HubError("The team hub's answer had no upload id", "http", r.status_code)
        part = int(d.get("part_max") or PART_MAX)
        _save_up(ident, {"id": uid, "at": time.time()})
        log.info("team hub: %s in parts (%d MB)", f.name, size // (1024 * 1024))
    part = max(1024 * 1024, min(part, PART_MAX))
    fails = 0
    with open(f, "rb") as fh:
        while True:
            while received < size:
                if stop is not None and stop():
                    raise HubError(PAUSED, "paused")
                fh.seek(received)
                data = _PartBody(fh.read(min(part, size - received)), stop)
                try:
                    r = request(url, "PUT", f"/api/uploads/{uid}?offset={received}", data=data, timeout=120,
                                headers={"Content-Type": "application/octet-stream"})
                except _Stopped:                     # Apex started during the part: the hub keeps what it got
                    raise HubError(PAUSED, "paused") from None
                except HubError as e:
                    if e.kind != "network" or fails + 1 >= tries:
                        raise                        # the id is kept: the next try goes on from the hub's count
                    fails += 1
                    time.sleep(min(8, 2 ** fails))
                    r = request(url, "GET", f"/api/uploads/{uid}", timeout=TIMEOUT)   # where the hub is now
                    if r.status_code != 200:
                        _check(r)
                        _save_up(ident, None)
                        raise HubError("The upload was lost on the hub: it starts again", "http", r.status_code)
                    received = int(_json(r).get("received") or 0)
                    continue
                d = _json(r)
                if r.status_code in (200, 409) and "received" in d:      # 409 offset_mismatch: go on from there
                    received = int(d.get("received") or 0)
                    continue
                if r.status_code == 404:                                   # expired on the hub: from the start
                    _save_up(ident, None)
                    raise HubError("The upload expired on the hub: it starts again", "http", 404)
                _check(r)
                raise HubError(f"the team hub answered {r.status_code}", "http", r.status_code)
            r = request(url, "POST", f"/api/uploads/{uid}/finish", timeout=300)
            d = _json(r)
            if r.status_code == 409 and d.get("error") == "incomplete" and fails + 1 < tries:
                fails += 1
                received = int(d.get("received") or 0)
                continue
            if r.status_code in (400, 404):
                _save_up(ident, None)                # bad checksum / gone: the next try sends it again
            _check(r)
            _save_up(ident, {"id": uid, "done": d, "at": time.time()})
            log.info("team hub: %s received", f.name)
            return d


def summary(answer: dict) -> str:
    """The hub's answer in one line for the player: "Sent to hub: 11 games", or the hub's note on a failure."""
    imports = answer.get("imports") if isinstance(answer, dict) else None
    if not imports:
        return "Sent to hub"                         # older hub, or a feed alone (imported once its scan is there)
    failed = [i for i in imports if i.get("status") == "failed"]
    if failed:
        why = str(failed[0].get("note") or failed[0].get("error") or "import failed").splitlines()[0][:140]
        return f"Sent to hub, not imported: {why}"
    games = sum(int(i.get("games") or 0) for i in imports if i.get("status") in ("imported", "superseded"))
    return f"Sent to hub: {games} game{'' if games == 1 else 's'}"


# ------------------------------------------------------------------ Test button, hub pages

def test(url: str) -> dict:
    """/api/ping (no key needed) then /api/me. Returns {ok, text, name?, role?, old?}; never the key."""
    res = _test(url)
    with S.lock:
        S.last = dict(res, url=url)
    return res


def _test(url: str) -> dict:
    if not url:
        return {"ok": False, "text": "No hub address"}
    try:
        r = request(url, "GET", "/api/ping", timeout=5, force=True)
    except HubError as e:
        return {"ok": False, "text": str(e), "reach": e.reach}
    if r.status_code in (301, 302, 303, 307, 308):  # http:// of a public hub: Cloudflare sends to https://
        to = _https_of(url, r)
        _set_reach(url, "address", _redirect_text(url, r))
        return {"ok": False, "text": _redirect_text(url, r), "reach": "address", **({"https": to} if to else {})}
    if r.status_code in DOWN_CODES:
        return {"ok": False, "text": REACH_TEXT["down"], "reach": "down"}
    if r.status_code == 404:                         # a hub from before keys has no /api/ping
        try:
            r2 = request(url, "GET", "/api/status", timeout=5, force=True)
            if r2.ok and "version" in r2.json():
                S.old = url
                return {"ok": True, "old": True, "text": "Hub is an older version: no key needed"}
        except Exception:
            pass
        return {"ok": False, "text": "No team hub at this address"}
    if not r.ok:
        return {"ok": False, "text": f"Hub not reachable (error {r.status_code})"}
    S.old = None
    key = get_key()
    try:
        me = request(url, "GET", "/api/me", timeout=5, force=True)
    except HubError as e:
        if e.kind == "refused":
            return {"ok": False, "text": "Key refused" if key else "Key needed: paste the key from your coach"}
        return {"ok": False, "text": str(e), "reach": e.reach}
    if me.status_code == 404:                        # ping but no /api/me: treat as a hub without keys
        return {"ok": True, "old": True, "text": "Hub is an older version: no key needed"}
    if not me.ok:
        return {"ok": False, "text": f"Hub error {me.status_code}"}
    d = me.json()
    with S.lock:
        if S.refused and S.refused[0] == url:
            S.refused = None
    role = "coach" if d.get("role") == "coach" else "player"
    name = str(d.get("name") or "?")[:40]
    return {"ok": True, "name": name, "role": role, "text": f"Connected as {name} ({role})"}


_features: dict[str, tuple[float, frozenset]] = {}


def has_feature(url: str, name: str, max_age: float = 600.0) -> bool:
    """The hub's /api/me lists `name` in its "features" (hub 0.7.1+: "comms"). Asked at most every 10 min per
    address; no answer, an older hub or any error = False (the caller then keeps the file and tries later)."""
    if not url:
        return False
    hit = _features.get(url)
    if hit and time.time() - hit[0] < max_age:
        return name in hit[1]
    try:
        r = request(url, "GET", "/api/me", timeout=5)
        feats = frozenset(str(x) for x in (r.json().get("features") or [])) if r.ok else frozenset()
    except Exception:
        return False                                  # not cached: asked again next time
    _features[url] = (time.time(), feats)
    return name in feats


def is_old(url: str) -> bool:
    """A hub from before keys (no /api/ping). Unknown or unreachable = False."""
    if S.old == url:
        return True
    try:
        r = request(url, "GET", "/api/ping", timeout=3, force=True)
    except HubError:
        return False
    if r.status_code == 404:
        S.old = url
        return True
    return False


def page_url(url: str, path: str = "/") -> str:
    """Address that opens a hub page signed in: the hub's own sign-in page with the key after '#'. The part
    after '#' never leaves this PC (browsers don't send it), and the hub's page removes it from the history at
    once. Without a key, or on a hub from before keys, the page itself."""
    path = path if path.startswith("/") and not path.startswith("//") else "/"
    key = get_key()
    if not key or is_old(url):
        return url + path
    return f"{url}/login?next={quote(path, safe='/')}#key={key}"
