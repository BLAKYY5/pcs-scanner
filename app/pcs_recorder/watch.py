"""Watch together: shared review sessions on the team hub (hub 0.5, `/api/sessions`).

The coach starts a session for a game; the players of that game join. Everyone's app plays the players' YouTube
videos of the game, lined up by the jump from the dropship (game time 0 = the jump), and follows the coach: play,
pause, seek, speed, which view is large, and his drawings.

The window can't hold the hub key, so this module talks to the hub for it:
- requests go through `hub.request` (key in the Authorization header, never in an address or a file);
- one websocket to the hub per session (`/api/sessions/<id>/ws`, same header) receives the coach's changes the
  moment they happen; the window asks this module with a long poll (`poll`), on this PC only;
- ping / pong on that websocket estimates the hub's clock (`offset` = hub time - this PC's time), so every
  app computes the same position from the hub's "game time t at hub time at".
After the YouTube upload of a block, `youtube.Uploader` sends the hub which recordings the video holds
(`POST /api/videos`); that is what makes a player's view available here.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from pathlib import Path

from . import hub
from .settings import APP_DIR

log = logging.getLogger("watch")
AUDIO_CACHE = APP_DIR / "talk-cache"
PING_EVERY = 5.0
APEX_CHECK_S = 3.0                                 # in a session: is Apex running? (then this app leaves it)
LIST_CACHE_S = 8.0


class Watch:
    def __init__(self, url_fn, clock=time.time):
        self.url_fn = url_fn                       # () -> the hub address now ("" = none)
        self.clock = clock
        self.cond = threading.Condition()
        self.seq = 0                               # bumped on every change the window should see
        self.sid: str | None = None
        self.state: dict | None = None
        self.me: dict = {}
        self.connected = False
        self.ended: str | None = None
        self.error = ""
        self.samples: list[tuple[float, float]] = []   # (round trip, offset) from pongs
        self.ws = None
        self.thread: threading.Thread | None = None
        self.gen = 0                               # a newer session stops the older websocket thread
        self._list: tuple[float, dict] | None = None
        self.pointer_at: dict | None = None        # the coach's pointer: {x, y, at} or {hide, at}
        self.on_state = None                       # called with each new state (review talk timeline)
        # Apex started while a session is open: this app leaves it (the hub session goes on for the others), so a
        # review never shares the PC with a game. Set by the app: () -> bool; on_apex_left: () -> None.
        self.is_apex_running = None
        self.on_apex_left = None
        self._apex_at = 0.0

    # ------------------------------------------------------------ hub requests
    def _url(self) -> str:
        url = self.url_fn()
        if not url:
            raise RuntimeError("No team hub address set (Settings → Team hub)")
        if not hub.get_key():
            raise RuntimeError(hub.NO_KEY)
        return url

    def _call(self, method: str, path: str, body=None, timeout: float = 8):
        r = hub.request(self._url(), method, path, json=body, timeout=timeout)
        if r.status_code == 404 and not path.startswith("/api/sessions/"):
            raise RuntimeError("The team hub is an older version: Watch together needs hub 0.5")
        if r.status_code == 404:
            raise RuntimeError("This session has ended")
        hub._check(r)
        return r.json()

    def listing(self, fresh: bool = False) -> dict:
        """Sessions running that this key may join; for a coach key also the newest games with YouTube videos."""
        if not fresh and self._list and self.clock() - self._list[0] < LIST_CACHE_S:
            return self._list[1]
        if not self.url_fn() or not hub.get_key():
            return {"available": False, "sessions": [], "games": [], "role": ""}
        me = self._call("GET", "/api/me")
        role = "coach" if me.get("role") == "coach" else "player"
        out = {"available": True, "role": role, "name": me.get("name", ""),
               "sessions": self._call("GET", "/api/sessions").get("sessions", []), "games": []}
        if role == "coach":
            out["games"] = self._call("GET", "/api/videos", timeout=10).get("games", [])
            out["plans"] = self.plans()
        out["current"] = self.sid if not self.ended else None
        self._list = (self.clock(), out)
        return out

    def plans(self) -> list:
        """Coach: his session plans (hub `/api/coach/plans`); a hub without them, or any trouble: none."""
        try:
            r = hub.request(self._url(), "GET", "/api/coach/plans", timeout=8)
            if r.status_code != 200:
                return []
            rows = r.json().get("plans") or []
        except Exception as e:
            log.info("plans: %s", e)
            return []
        return [{"id": str(p.get("id") or ""), "title": str(p.get("title") or "")[:80],
                 "moments": int(p.get("moments") or 0), "games": int(p.get("games") or 0), "updated": p.get("updated")}
                for p in rows if isinstance(p, dict) and p.get("id")]

    def start(self, game_key: str, plan: str | None = None) -> dict:
        """A new session on a game, or on a session plan's first moment (the coach's plan; hub with plans)."""
        body = {"gameKey": str(game_key or "")}
        if plan:
            body["plan"] = str(plan)[:40]
        st = self._call("POST", "/api/sessions", body)
        self._attach(st)
        return self.snapshot()

    def join(self, sid: str) -> dict:
        st = self._call("POST", f"/api/sessions/{_sid(sid)}/join")
        self._attach(st)
        return self.snapshot()

    def control(self, body: dict) -> dict:
        st = self._call("POST", f"/api/sessions/{self._need()}/control", _pick(body, ("action", "t", "speed", "player", "gameKey", "id", "index", "plan")))
        self._apply({"type": "state", "state": st})
        return self.snapshot()

    def draw(self, body: dict) -> dict:
        return self._call("POST", f"/api/sessions/{self._need()}/draw", _pick(body, ("op", "stroke", "id")))

    def end(self) -> None:
        sid = self._need()
        try:
            self._call("POST", f"/api/sessions/{sid}/end")
        finally:
            self._detach("ended")

    def check_apex(self) -> bool:
        """In a session and Apex runs: leave it (why "apex"). Looked at most every APEX_CHECK_S. -> True if left."""
        if not self.sid or self.ended or self.is_apex_running is None:
            return False
        now = self.clock()
        if now - self._apex_at < APEX_CHECK_S:
            return False
        self._apex_at = now
        try:
            if not self.is_apex_running():
                return False
        except Exception:
            return False
        log.info("Apex started: leaving the session")
        if self.on_apex_left:                      # first, like a leave by hand (review talk stops, then leave)
            try:
                self.on_apex_left()
            except Exception:
                log.exception("before leaving for Apex")
        self.leave("apex")
        return True

    def leave(self, why: str = "left") -> None:
        sid = self.sid
        self._detach(why)
        if sid:
            try:
                self._call("POST", f"/api/sessions/{sid}/leave", timeout=4)
            except Exception as e:
                log.info("leave: %s", e)

    def report(self, t, buffering: bool, browsing: bool = False) -> None:
        """Where this app's large view is (game time), and whether the player is on their own moment (browsing):
        the coach sees who is behind or browsing."""
        ws = self.ws
        if ws is None or t is None:
            return
        try:
            ws.send(json.dumps({"type": "report", "t": round(float(t), 2), "buffering": bool(buffering),
                                "browsing": bool(browsing)}))
        except Exception:
            pass

    def pointer(self, body: dict) -> dict:
        """Coach: the dot following his mouse on the large view (or hidden)."""
        b = {"hide": True} if body.get("hide") else {"x": float(body.get("x", 0)), "y": float(body.get("y", 0))}
        return self._call("POST", f"/api/sessions/{self._need()}/pointer", b, timeout=4)

    def flag(self, t) -> dict:
        """Player: "Look at this" at their moment."""
        return self._call("POST", f"/api/sessions/{self._need()}/flag", {} if t is None else {"t": float(t)})

    def bookmark(self, text: str, t=None) -> dict:
        """Coach: mark this moment, with an optional one-line note."""
        return self._call("POST", f"/api/sessions/{self._need()}/bookmark",
                          {"text": str(text or "")[:200], **({} if t is None else {"t": float(t)})})

    # ------------------------------------------------------------ afterwards, in the review player
    def extras(self, path: str, player: str | None = None) -> dict:
        """Session bookmarks and review talk for the games of this recording (hub 0.5); none without a hub."""
        from .notes import recording_name
        url = self.url_fn()
        if not url or not hub.get_key():
            return {"available": False, "games": []}
        try:
            r = hub.request(url, "GET", "/api/review-extras", timeout=8,
                            params={"recording": recording_name(path), **({"player": player} if player else {})})
        except hub.HubError as e:
            return {"available": False, "games": [], "error": str(e)}
        if r.status_code == 404:                       # a hub before 0.5
            return {"available": False, "games": []}
        hub._check(r)
        d = r.json()
        d = d if isinstance(d, dict) else {}
        from .notes import num
        games = []
        for g in d.get("games") or []:                 # times go straight into the page's markup: numbers only
            if not isinstance(g, dict):
                continue
            games.append({**g, "jumpAt": num(g.get("jumpAt")),
                          "bookmarks": [{**b, "t": num(b.get("t"))} for b in g.get("bookmarks") or [] if isinstance(b, dict)],
                          "talk": [t for t in g.get("talk") or [] if isinstance(t, dict)],
                          "sessions": [_session_summary(x) for x in g.get("sessions") or [] if isinstance(x, dict)],
                          "plan": [{"t": num(x.get("t"), None), "note": str(x.get("note") or "")[:200], "covered": bool(x.get("covered")),
                                    "player": str(x.get("player") or "")[:40] or None}
                                   for x in g.get("plan") or [] if isinstance(x, dict) and num(x.get("t"), None) is not None]})
        return {**d, "games": games, "available": True}

    def audio(self, aid: str) -> Path:
        """One review talk track, downloaded once into the data folder (the window's player reads it from here)."""
        import re
        if not re.fullmatch(r"ta-[0-9a-f]{12}", aid or ""):
            raise ValueError("bad talk id")
        f = AUDIO_CACHE / f"{aid}.m4a"
        if f.exists():
            return f
        r = hub.request(self._url(), "GET", f"/api/session-audio/{aid}", timeout=120, stream=True)
        hub._check(r)
        AUDIO_CACHE.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".part")
        with open(tmp, "wb") as out:
            for chunk in r.iter_content(1 << 20):
                out.write(chunk)
        tmp.replace(f)
        return f

    def _need(self) -> str:
        if not self.sid or self.ended:
            raise RuntimeError("Not in a session")
        return self.sid

    # ------------------------------------------------------------ what the window reads
    @property
    def offset(self) -> float:
        """Hub clock - this PC's clock, from the pong with the shortest round trip (most exact)."""
        if not self.samples:
            return 0.0
        return min(self.samples)[1]

    def snapshot(self) -> dict:
        with self.cond:
            return {"seq": self.seq, "sid": self.sid, "state": self.state, "me": self.me, "pointer": self.pointer_at,
                    "connected": self.connected, "ended": self.ended, "error": self.error,
                    "offset": round(self.offset, 4), "rtt": round(min(self.samples)[0], 4) if self.samples else None}

    def poll(self, seq: int, wait: float = 15) -> dict:
        end = self.clock() + max(0.0, min(wait, 25))
        with self.cond:
            while self.seq == seq and self.clock() < end:
                self.cond.wait(max(0.05, end - self.clock()))
        return self.snapshot()

    def _bump(self) -> None:
        with self.cond:
            self.seq += 1
            self.cond.notify_all()

    # ------------------------------------------------------------ the websocket
    def _attach(self, st: dict) -> None:
        with self.cond:
            self.gen += 1
            old = self.ws
            self.sid, self.state, self.ended, self.error = st["id"], st, None, ""
            self.connected, self.samples, self.ws = False, [], None
        if old is not None:
            _close(old)
        self._list = None
        self.thread = threading.Thread(target=self._run, args=(self.gen, st["id"]), daemon=True, name="watch")
        self.thread.start()
        self._bump()

    def _detach(self, why: str) -> None:
        with self.cond:
            self.gen += 1
            old, self.ws = self.ws, None
            self.ended, self.connected = why, False
        if old is not None:
            _close(old)
        self._list = None
        self._bump()

    def _run(self, gen: int, sid: str) -> None:
        import websocket                          # websocket-client (comes with obsws-python)
        wait = 1.0
        while gen == self.gen:
            ws = None
            try:
                ws_url = ws_address(self._url()) + f"/api/sessions/{_sid(sid)}/ws"
                ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True, sslopt=sslopt(),
                                                 header=["Authorization: Bearer " + (hub.get_key() or "")])
                ws.settimeout(0.5)
                with self.cond:
                    if gen != self.gen:
                        _close(ws)
                        return
                    self.ws, self.connected, self.error = ws, True, ""
                self._bump()
                wait, pings, next_ping = 1.0, 0, 0.0
                while gen == self.gen:
                    if self.check_apex():
                        return
                    now = self.clock()
                    if now >= next_ping:                     # 5 quick pings, then one every PING_EVERY s
                        ws.send(json.dumps({"type": "ping", "c": now}))
                        pings += 1
                        next_ping = now + (0.3 if pings < 5 else PING_EVERY)
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        raise ConnectionError("closed")
                    self._apply(json.loads(raw))
                    if self.ended:
                        return
            except Exception as e:
                if gen != self.gen:
                    return
                msg = str(e) or e.__class__.__name__
                if "403" in msg or "4403" in msg:
                    self.error = "The hub refused: only the players of this game can join"
                elif "404" in msg or "4404" in msg:
                    self.ended = "ended"
                else:
                    self.error = "Connection to the hub lost: trying again"
                log.info("watch websocket: %s", msg[:200])
            finally:
                with self.cond:
                    if self.ws is ws:
                        self.ws, self.connected = None, False
                if ws is not None:
                    _close(ws)
            self._bump()
            if self.ended or gen != self.gen or self.check_apex():
                return
            time.sleep(wait)
            wait = min(wait * 2, 10)
            try:                                      # a late reconnect: the whole state again (rev may jump)
                st = self._call("GET", f"/api/sessions/{_sid(sid)}")
                if gen == self.gen:
                    self._apply({"type": "state", "state": st})
            except Exception as e:
                if "ended" in str(e):
                    self.ended = "ended"
                    self._bump()
                    return

    def _apply(self, m: dict) -> None:
        """One message from the hub (or an answer to the coach's own action)."""
        kind = m.get("type")
        refetch = False
        with self.cond:
            st = self.state
            if kind == "state" and isinstance(m.get("state"), dict):
                new = m["state"]
                if new.get("id") == self.sid and (st is None or new.get("rev", 0) >= st.get("rev", 0)):
                    self.state = new
                if m.get("me"):
                    self.me = m["me"]
            elif kind == "stroke" and st is not None:
                if m.get("rev") == st.get("rev", 0) + 1:
                    strokes = list(st.get("strokes") or [])
                    op = m.get("op")
                    if op == "put" and isinstance(m.get("stroke"), dict):
                        i = next((i for i, x in enumerate(strokes) if x.get("id") == m["stroke"].get("id")), None)
                        if i is None:
                            strokes.append(m["stroke"])
                        else:
                            strokes[i] = m["stroke"]
                    elif op in ("undo", "delete"):
                        strokes = [x for x in strokes if x.get("id") != m.get("id")]
                    elif op == "clear":
                        strokes = []
                    self.state = {**st, "strokes": strokes, "rev": m["rev"]}
                elif m.get("rev", 0) > st.get("rev", 0) + 1:
                    refetch = True                    # missed one: take the whole state
            elif kind == "members" and st is not None:
                self.state = {**st, "members": m.get("members") or {}}
            elif kind == "ended":
                self.ended = m.get("reason") or "ended"
            elif kind == "pointer":
                self.pointer_at = {"hide": True} if m.get("hide") else {"x": m.get("x"), "y": m.get("y")}
                self.pointer_at["at"] = self.clock()
            elif kind == "pong":
                try:
                    t1, c, s = self.clock(), float(m["c"]), float(m["s"])
                    self.samples = (self.samples + [(t1 - c, s - (c + t1) / 2)])[-12:]
                except (KeyError, TypeError, ValueError):
                    return
                return                                # nothing for the window to redraw
            else:
                return
            st = self.state
        self._bump()
        if self.on_state and st is not None and kind == "state":
            try:
                self.on_state(st, self.offset)
            except Exception:
                log.exception("review talk timeline")
        if refetch:
            try:
                self._apply({"type": "state", "state": self._call("GET", f"/api/sessions/{self._need()}")})
            except Exception as e:
                log.info("watch refetch: %s", e)


def ws_address(url: str) -> str:
    """The hub's websocket address: wss:// for an https:// hub (the public address), ws:// for http://."""
    return ("wss://" + url[8:]) if url.startswith("https://") else "ws://" + url.split("://", 1)[-1]


def sslopt() -> dict:
    """wss://: the certificate and the name are always checked, against the same CA list as the app's https
    requests (certifi, as requests uses). Never turned off."""
    import ssl
    opt = {"cert_reqs": ssl.CERT_REQUIRED, "check_hostname": True}
    try:
        import certifi
        opt["ca_certs"] = certifi.where()
    except ImportError:                               # websocket-client then loads Windows' own list
        pass
    return opt


def _session_summary(x: dict) -> dict:
    """One Watch together session's summary (hub with plans) for the review player: numbers checked, names as text."""
    from .notes import num

    def n(v):
        f = num(v, None)
        return None if f is None else int(round(f))

    def epoch(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if 0 < f < 1e11 else None
    p = x.get("plan") if isinstance(x.get("plan"), dict) else None
    return {"id": str(x.get("id") or "")[:40], "started": epoch(x.get("started")), "length": n(x.get("length")),
            "attendance": [{"name": str(a.get("name") or "")[:40], "seconds": n(a.get("seconds"))}
                           for a in x.get("attendance") or [] if isinstance(a, dict)],
            **{k: n(x.get(k)) for k in ("bookmarks", "flags", "notes", "drawings", "talk")},
            "plan": {"title": str(p.get("title") or "")[:80], **{k: n(p.get(k)) for k in ("total", "covered", "skipped")}}
            if p else None}


def _sid(sid) -> str:
    s = str(sid or "")
    if not s or len(s) > 40 or not all(c.isalnum() or c in "-_" for c in s):
        raise ValueError("bad session id")
    return s


def _pick(body: dict, keys: tuple) -> dict:
    return {k: body[k] for k in keys if isinstance(body, dict) and k in body}


def _close(ws) -> None:
    try:
        ws.close()
    except Exception:
        pass
