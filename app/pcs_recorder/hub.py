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
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .settings import APP_DIR

log = logging.getLogger("hub")
KEY_FILE = APP_DIR / "hub_key.dat"
KEY_RE = re.compile(r"pcs_[A-Za-z0-9_-]{43}")
TIMEOUT = 10
REFUSED = "The team hub refused your key: paste a new one in Settings → Team hub"
NO_KEY = "The team hub needs your key: paste it in Settings → Team hub"
_ENTROPY = b"PCS Scanner team hub key"


class HubError(Exception):
    """The hub couldn't be reached or said no. `kind`: network | refused | wait | http."""

    def __init__(self, text: str, kind: str, status: int = 0):
        super().__init__(text)
        self.kind, self.status = kind, status


def reason(e: Exception) -> str:
    """Why the hub didn't take a recording's results, as the player reads it on the recording."""
    kind = getattr(e, "kind", "network")
    if kind == "refused":
        return "Key refused"
    if kind == "wait":
        return "Hub asks to wait"
    if kind == "http":
        return f"Hub error {getattr(e, 'status', '')}".strip()
    return "Hub not reachable"


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
        raise HubError(f"the team hub didn't answer ({e.__class__.__name__})", "network") from None
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
        S.refused, S.wait_until, S.last, S.old = None, 0.0, {}, None


def public_state(url: str) -> dict:
    """What the app's page may see: never the key itself."""
    key = get_key()
    return {"url": url or "", "key": mask(key),
            "refused": bool(url and S.refused == (url, _fp(key))),
            "wait": max(0, int(S.wait_until - time.time())),
            "last": {k: v for k, v in S.last.items() if k != "url"} if S.last.get("url") == url else {}}


# ------------------------------------------------------------------ requests

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
    try:
        r = requests.request(method, url + path, headers=headers, timeout=timeout, allow_redirects=False, **kw)
    except requests.RequestException as e:
        raise HubError(f"the team hub didn't answer ({e.__class__.__name__})", "network") from None
    if r.status_code == 401:
        with S.lock:
            S.refused = (url, _fp(key))
        log.info("team hub: key refused (%s)", _code(r))
        raise HubError(REFUSED if key else NO_KEY, "refused", 401)
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
    if r.status_code >= 400:
        detail = ""
        try:
            d = r.json()
            detail = d.get("detail") if isinstance(d.get("detail"), str) else ""
        except Exception:
            pass
        raise HubError(f"the team hub answered {r.status_code}" + (f": {detail[:120]}" if detail else ""), "http",
                       r.status_code)


def get_json(url: str, path: str, timeout: float = TIMEOUT):
    r = request(url, "GET", path, timeout=timeout)
    _check(r)
    return r.json()


def report(url: str, data: dict) -> None:
    """Recorder report (recording / recorded / uploaded…): best effort, short timeout."""
    _check(request(url, "POST", "/api/recorder/sessions", json=data, timeout=5))


def upload(url: str, files: list[Path], player: str = "") -> dict:
    """The scan results of one recording, all files in one request (the hub imports the scan with its feed).
    Returns the hub's answer. A 200 means the hub stored them, even if its import failed: never sent again."""
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


def send_google(url: str, id_token: str) -> dict:
    """Tell the hub which Google account this key's player signed in with (hub 0.6 `POST /api/me/google`): the hub
    checks the ID token with Google and keeps the account id and email on the key. Returns {"email", ...}.
    Raises HubError (404 = older hub, 503 = the coach hasn't turned it on). The token is never logged."""
    r = request(url, "POST", "/api/me/google", json={"id_token": id_token}, timeout=20)
    _check(r)
    d = r.json()
    return d if isinstance(d, dict) else {}


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
        return {"ok": False, "text": "Hub not reachable" if e.kind == "network" else str(e)}
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
        return {"ok": False, "text": "Hub not reachable" if e.kind == "network" else str(e)}
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
