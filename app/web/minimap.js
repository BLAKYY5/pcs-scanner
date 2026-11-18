/* Map panel of the review player: the game's own minimap with the player's path in the game on screen, a marker at
   the video's moment, the landing spot, the harvesters, the ring circles (when the scan has `ringCircles`), our kill
   feed events and the coach's notes placed where the player was, and (Squad switch) the teammates' paths from the
   team hub. Also the map of Watch together (wtMapSetup / wtMapTick, called from session.js).
   Loaded after app.js (uses $, esc, api, TOKEN, page, pageArg, RV, gameAt, seek, mapName, poiName, fmtT, feedSide,
   feedEvent).

   The pictures come from the team hub (EA's files: never in the app). The app downloads one with the player's key the
   first time it is needed and keeps it in its data folder (/api/minimap/<map> says whether it is there, /minimap/<map>
   serves it), so the panel works offline once a map was seen. Positions are the debug overlay's world x, y read about
   once a second (`seen` rows of the scan), placed with the game's own transform:
   fraction across = (x - pos_x) / scale / size, down = (pos_y - y) / scale / size.

   Light on the PC: drawn on a canvas only when something changes (the video's moment, at most MM_EVERY_MS apart; the
   view; the size), never while the window is hidden, no animation loop. */
'use strict';

const MM = {
  on: true, squad: false, d: null, v: null, gi: -2, info: {}, img: {}, game: null, view: null, t: -1, hover: null,
  drag: null, ro: null, last: 0, timer: null, dirty: false, sq: {},
};
const MM_GAP_S = 6, MM_FAST = 3000;                   // a gap in the path: no read for 6 s, or faster than 3000 units/s
const MM_ZMAX = 16, MM_HIT_PX = 10, MM_EVERY_MS = 250;
const MM_SQUAD = ['#4fc3f7', '#ff9f43', '#9be564', '#f78fb3'];
try { MM.on = localStorage.getItem('pcs-map') !== '0'; MM.squad = localStorage.getItem('pcs-map-squad') === '1'; } catch (e) {}

function minimapSetup(d, v) {
  if (MM.ro) MM.ro.disconnect();
  clearTimeout(MM.timer);
  Object.assign(MM, {d, v, gi: -2, game: null, view: null, t: -1, hover: null, drag: null, last: 0, timer: null, dirty: false, sq: {}});
  const side = $('.review .side'); if (!side) return;
  const box = document.createElement('div');
  box.className = 'mm'; box.id = 'mmPanel'; box.hidden = !MM.on;
  box.innerHTML = `<div class="mm-head"><b>Map</b><span class="mm-cap faint" id="mmCap"></span>
      <button class="btn small ghost${MM.squad ? ' on' : ''}" id="mmSquadBtn" title="Your teammates' paths, from the team hub">Squad</button>
      <button class="btn small ghost" id="mmReset" title="Back to the whole path">Reset</button>
      <button class="btn small ghost" id="mmHide" title="Hide the map (M)">Hide</button></div>
    <div class="mm-box" id="mmBox"><canvas id="mmCv"></canvas><div class="mm-msg" id="mmMsg" hidden></div></div>
    <div class="mm-squad faint" id="mmSquad" hidden></div>
    <div class="mm-legend faint" id="mmLegend"><span><i class="mm-k now"></i>Now</span><span><i class="mm-k land"></i>Landing</span><span><i class="mm-k harv"></i>Harvester</span><span><i class="mm-k ring"></i>Ring</span><span><i class="mm-k care"></i>Care package</span><span><i class="mm-k sh"></i>Stronghold</span><span><i class="mm-k kill"></i>Knock / kill</span><span><i class="mm-k down"></i>We went down</span><span><i class="mm-k note"></i>Coach note</span><span>Click the path or a mark to go there · wheel to zoom · drag to move</span></div>`;
  side.prepend(box);
  // the Map button beside Theatre / Full screen
  const th = $('#rvTheatre');
  if (th && !$('#rvMap')) {
    const b = document.createElement('button');
    b.className = 'btn small' + (MM.on ? ' brand' : ''); b.id = 'rvMap'; b.title = 'Your path on the map (M)'; b.textContent = 'Map';
    b.onclick = () => minimapToggle();
    th.before(b);
  }
  $('#mmReset').onclick = () => { MM.view = null; mmDraw(); };
  $('#mmHide').onclick = () => minimapToggle(false);
  $('#mmSquadBtn').onclick = () => mmSquadToggle();
  const cv = $('#mmCv');
  mmPointer(cv, () => MM.game && mmScene(), t => seek(t), () => mmDraw());
  MM.ro = new ResizeObserver(() => mmDraw());
  MM.ro.observe($('#mmBox'));
  v.addEventListener('timeupdate', mmTime);
  v.addEventListener('seeked', mmTime);
  mmTime();
}

function minimapToggle(on) {
  const p = $('#mmPanel'); if (!p) return;
  MM.on = on == null ? p.hidden : !!on;
  p.hidden = !MM.on;
  const b = $('#rvMap'); if (b) b.classList.toggle('brand', MM.on);
  try { localStorage.setItem('pcs-map', MM.on ? '1' : '0'); } catch (e) {}
  if (MM.on) { MM.t = -1; mmTime(); }
}
function mmSquadToggle() {
  MM.squad = !MM.squad;
  try { localStorage.setItem('pcs-map-squad', MM.squad ? '1' : '0'); } catch (e) {}
  const b = $('#mmSquadBtn'); if (b) b.classList.toggle('on', MM.squad);
  if (MM.squad && MM.game) mmLoadSquad(MM.game); else { mmSquadLine(); mmDraw(); }
}

function mmLive() { return MM.on && MM.v && document.contains(MM.v) && page === 'review' && MM.d && pageArg === MM.d.id; }

/* the video's moment changed: at most every MM_EVERY_MS (one more draw after the last change), nothing while hidden */
function mmTime() {
  if (!mmLive()) return;
  if (document.hidden) { MM.dirty = true; return; }
  const wait = MM.last + MM_EVERY_MS - Date.now();
  if (wait > 0) { if (!MM.timer) MM.timer = setTimeout(() => { MM.timer = null; mmTime(); }, wait); return; }
  MM.last = Date.now();
  const t = MM.v.currentTime, games = MM.d.games || [];
  const gi = games.length ? gameAt(t) : -1;
  if (gi !== MM.gi) { MM.gi = gi; mmGame(gi); return; }
  if (Math.abs(t - MM.t) < 0.2) return;
  MM.t = t;
  mmDraw();
}
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && MM.dirty) { MM.dirty = false; MM.t = -1; mmTime(); }
  if (!document.hidden && WM.dirty) { WM.dirty = false; wmDraw(); }
});

function mmMsg(text) {
  const m = $('#mmMsg'); if (!m) return;
  m.hidden = !text; m.textContent = text || '';
  const lg = $('#mmLegend'); if (lg) lg.hidden = !!text;
}

/* a map's picture and transform (the hub's, kept by the app); null when there is none, with the reason */
async function mmInfo(map) {
  let info = MM.info[map];
  if (!info) {
    try { info = await api('/api/minimap/' + encodeURIComponent(map)); }
    catch (e) { info = {available: false, reason: 'hub', text: 'Minimap needs the team hub'}; }
    if (info.available) MM.info[map] = info;            // a "not there" answer is asked again next time
  }
  return info;
}
function mmImage(map, info, onload, onerror) {
  let img = MM.img[map];
  if (!img || img.dataset.asset !== info.asset) {
    img = new Image();
    img.dataset.asset = info.asset;
    img.src = `/minimap/${encodeURIComponent(map)}?t=${encodeURIComponent(TOKEN)}&v=${info.asset}`;
    MM.img[map] = img;
  }
  if (img.complete && img.naturalWidth) onload();
  else {
    img.addEventListener('load', onload, {once: true});
    img.addEventListener('error', () => { delete MM.img[map]; delete MM.info[map]; onerror(); }, {once: true});
  }
  return img;
}

/* the game on screen changed: its path, then its map picture (from the hub, once) */
async function mmGame(gi) {
  MM.game = null; MM.view = null; MM.hover = null;
  const cap = $('#mmCap');
  mmSquadLine();
  if (!MM.d.scan || !MM.d.games) { mmMsg('Scan this recording to see your path on the map'); if (cap) cap.textContent = ''; mmDraw(); return; }
  const g = MM.d.games[gi];
  if (!g) { mmMsg('No game in this recording'); if (cap) cap.textContent = ''; mmDraw(); return; }
  if (cap) cap.textContent = `Game ${gi + 1} · ${mapName(g.map)}`;
  if (!g.map || g.map === '?') { mmMsg('No minimap for this map yet'); mmDraw(); return; }
  mmMsg('Loading the map…');
  const info = await mmInfo(g.map);
  if (!mmLive() || MM.gi !== gi) return;
  if (!info.available) { mmMsg(info.text || 'No minimap for this map yet'); mmDraw(); return; }
  const game = mmPath(g, info);
  game.gi = gi;
  game.img = mmImage(g.map, info, () => { if (MM.game === game) mmDraw(); },
    () => { if (MM.game === game) mmMsg('Minimap needs the team hub'); });
  MM.game = game;
  mmMsg(game.pts.length ? '' : 'No positions read in this game');
  if (MM.squad) mmLoadSquad(game);
  mmDraw();
}

/* world x, y -> fraction of the picture (0..1 from the top left) */
function mmFrac(info, x, y) { return [(x - info.pos_x) / info.scale / info.size, (info.pos_y - y) / info.scale / info.size]; }
const mmJump = g => { const L = g.landing || {}; return L.exitAtExact ?? L.exitAt ?? null; };

/* the player's positions in this game: from the jump to the game's end, not while spectating, split at gaps */
function mmPath(g, info) {
  const L = g.landing || {};
  const from = mmJump(g) ?? g.tStart ?? -Infinity;
  const to = g.tEndGame != null ? g.tEndGame + 2 : Infinity;
  const pts = [];
  for (const r of g.seen || []) {
    const [t, , spec, x, y] = r;
    if (t < from - 1 || t > to || spec || x == null || y == null) continue;
    const [fx, fy] = mmFrac(info, x, y);
    if (fx < -0.02 || fx > 1.02 || fy < -0.02 || fy > 1.02) continue;        // the drop ship, the lobby
    const p = pts[pts.length - 1];
    const brk = p && (t - p.t > MM_GAP_S || Math.hypot(x - p.x, y - p.y) / Math.max(0.5, t - p.t) > MM_FAST);
    pts.push({t, x, y, fx, fy, brk: !!brk});
  }
  mmDespike(pts);
  const land = L.x != null && L.y != null ? mmFrac(info, L.x, L.y) : null;
  // harvesters are read on the full map in map %: the game's minimap is framed the same way
  const harv = (g.harvesters || []).filter(h => h.x != null && h.y != null).map(h => ({fx: h.x / 100, fy: h.y / 100, ok: h.reliable !== false, poi: h.poi}));
  return {g, info, pts, land, landPoi: L.poi || null, harv, rings: mmRings(g, info), icons: mmIcons(g)};
}

/* care packages and strongholds read on the full map (`games[].carePackages`, `games[].strongholds`, map %): shown
   from the moment the scanner first saw them; click = go there */
function mmIcons(g) {
  const out = [], ok = e => e && typeof e.t === 'number' && Number.isFinite(e.mapX) && Number.isFinite(e.mapY);
  for (const e of g.carePackages || []) {
    if (!ok(e)) continue;
    const ring = e.inNextRing === true ? ' · inside ring ' + e.nextRing : e.inNextRing === false ? ' · outside ring ' + e.nextRing : '';
    out.push({kind: 'care', t: e.t, fx: e.mapX / 100, fy: e.mapY / 100, n: e.n || null, opened: e.openedAt,
              label: 'Care package' + (e.n ? ' ' + e.n : '') + ring});
  }
  for (const e of g.strongholds || []) {
    if (!ok(e)) continue;
    out.push({kind: 'sh', t: e.t, fx: e.mapX / 100, fy: e.mapY / 100, state: e.state, until: e.availableLast,
              label: 'Stronghold' + (e.state === 'unavailable' ? ' (taken)' : '')});
  }
  return out;
}

/* a misread number gives a lone point far away (a spike out and back): dropped, the path joins again */
function mmDespike(pts) {
  const far = (u, v) => Math.hypot(u.x - v.x, u.y - v.y) / Math.max(0.5, Math.abs(u.t - v.t)) > MM_FAST;
  for (let i = pts.length - 2; i >= 1; i--) {
    const a = pts[i - 1], p = pts[i], b = pts[i + 1];
    if (far(a, p) && far(p, b) && !far(a, b) && b.t - a.t <= MM_GAP_S) { pts.splice(i, 1); b.brk = false; }
  }
  return pts;
}

/* ring circles (`games[].ringCircles`, newer scans): each {t (or at / firstSeen), until?, x, y, r} in world units, or
   {t, mapX, mapY, mapR} in map %, or [t, x, y, r]. Older scans have none: nothing is drawn. */
function mmRings(g, info) {
  const out = [];
  for (const c of g.ringCircles || []) {
    let t, until = null, fx, fy, fr;
    if (Array.isArray(c)) {
      if (c.length < 4 || c.some(v => typeof v !== 'number')) continue;
      t = c[0]; [fx, fy] = mmFrac(info, c[1], c[2]); fr = c[3] / info.scale / info.size;
    } else if (c && typeof c === 'object') {
      t = c.t ?? c.at ?? c.from ?? c.firstSeen ?? c.appearedAt;
      until = c.until ?? c.to ?? c.endsAt ?? null;
      const r = c.r ?? c.radius;
      if (c.x != null && c.y != null && r != null) { [fx, fy] = mmFrac(info, c.x, c.y); fr = r / info.scale / info.size; }
      else if (c.mapX != null && c.mapY != null && (c.mapR ?? c.mapRadius) != null) { fx = c.mapX / 100; fy = c.mapY / 100; fr = (c.mapR ?? c.mapRadius) / 100; }
      else continue;
    } else continue;
    if (typeof t !== 'number' || ![fx, fy, fr].every(Number.isFinite) || fr <= 0) continue;
    out.push({t, until, fx, fy, fr, round: c.round ?? null});
  }
  return out.sort((a, b) => a.t - b.t);
}
/* the circles to show at t: the last one seen (the ring now) bright, the one before it faint */
function mmRingsAt(rings, t) {
  const seen = rings.filter(c => c.t <= t && (c.until == null || c.until >= t - 1));
  return seen.slice(-2);
}

/* where a path is at t: between two reads close in time, else none */
function mmAt(P, t) {
  if (!P.length || t < P[0].t - 1 || t > P[P.length - 1].t + 2) return null;
  let i = 0;
  while (i + 1 < P.length && P[i + 1].t <= t) i++;
  const a = P[i], b = P[i + 1];
  if (b && !b.brk && t > a.t) { const k = (t - a.t) / (b.t - a.t); return [a.fx + (b.fx - a.fx) * k, a.fy + (b.fy - a.fy) * k]; }
  return t - a.t <= MM_GAP_S || t < a.t ? [a.fx, a.fy] : null;
}
/* where a mark goes: the position at t, else the last one read before it (within 20 s: a note while spectating) */
function mmPlace(P, t) {
  const p = mmAt(P, t); if (p) return p;
  let q = null;
  for (const x of P) { if (x.t <= t) q = x; else break; }
  return q && t - q.t <= 20 ? [q.fx, q.fy] : null;
}

/* kill feed events with our squad and the coach's notes in this game, where the player was at that moment */
function mmMarks(game, t) {
  const g = game.g, P = game.pts, marks = [];
  if (!P.length) return marks;
  const from = P[0].t - 1, to = P[P.length - 1].t + 20;
  if (MM.d.feedSides && window.feedSide && window.feedEvent) {
    for (const l of MM.d.feed || []) {
      if (l.dup || l.t < from || l.t > to || l.t > t + 0.25) continue;        // so far only
      const e = feedEvent(l, g); if (!e) continue;
      const p = mmPlace(P, l.t); if (!p) continue;
      marks.push({kind: e.lost ? 'down' : 'kill', t: l.t, fx: p[0], fy: p[1], label: e.title});
    }
  }
  const notes = (RV && RV.notes && RV.notes.notes) || [];
  for (const n of notes) {
    if (n.t < from || n.t > to) continue;
    const p = mmPlace(P, n.t); if (!p) continue;
    marks.push({kind: 'note', t: n.t, fx: p[0], fy: p[1], label: 'Coach: ' + (n.text ? n.text.split('\n')[0].slice(0, 60) : 'drawing'), later: n.t > t + 0.25});
  }
  return marks;
}

/* ---- teammates' paths (team hub, `/api/squadpaths`): same game, lined up by the jump */
async function mmLoadSquad(game) {
  const gi = game.gi, jump = mmJump(game.g);
  if (MM.sq[gi] && !MM.sq[gi].loading) { mmSquadLine(); mmDraw(); return; }
  if (jump == null) { MM.sq[gi] = {available: false, text: 'No jump in this game'}; mmSquadLine(); return; }
  MM.sq[gi] = {loading: true};
  mmSquadLine();
  let r;
  try { r = await api(`/api/squadpaths?id=${encodeURIComponent(MM.d.id)}&jump=${jump}`); }
  catch (e) { r = {available: false, text: 'Squad paths need the team hub'}; }
  if (!r.available) MM.sq[gi] = r;
  else MM.sq[gi] = Object.assign(r, {lines: mmSquadLines(r.players, game.info, jump)});
  if (MM.game === game) { mmSquadLine(); mmDraw(); }
  if (!r.available || r.offline) setTimeout(() => { if (MM.sq[gi] === r) delete MM.sq[gi]; }, 60000);   // ask again later
}
/* hub paths ([[game time, x, y] | null, ...]) -> panel points at this recording's times */
function mmSquadLines(players, info, jump) {
  return (players || []).map((p, i) => {
    const pts = [];
    let brk = false;
    for (const r of p.path || []) {
      if (!r) { brk = true; continue; }
      const [fx, fy] = mmFrac(info, r[1], r[2]);
      if (fx < -0.02 || fx > 1.02 || fy < -0.02 || fy > 1.02) { brk = true; continue; }
      const q = pts[pts.length - 1], t = r[0] + jump;
      pts.push({t, x: r[1], y: r[2], fx, fy, brk: !!q && (brk || t - q.t > MM_GAP_S || Math.hypot(r[1] - q.x, r[2] - q.y) / Math.max(0.5, t - q.t) > MM_FAST)});
      brk = false;
    }
    mmDespike(pts);
    return {name: p.player, color: MM_SQUAD[i % MM_SQUAD.length], pts, land: p.land ? mmFrac(info, p.land[0], p.land[1]) : null};
  });
}
function mmSquadLine() {
  const el = $('#mmSquad'); if (!el) return;
  const s = MM.squad && MM.game ? MM.sq[MM.game.gi] : null;
  el.hidden = !s;
  if (!s) return;
  if (s.loading) { el.textContent = 'Loading your teammates\' paths…'; return; }
  if (!s.available) { el.textContent = s.text || 'Squad paths need the team hub'; return; }
  const L = s.lines || [];
  el.innerHTML = !L.length ? 'No teammate\'s recording of this game on the hub' :
    L.map(l => `<span><i class="mm-k" style="background:${l.color}"></i>${esc(l.name)}${l.pts.length ? '' : ' (no positions)'}</span>`).join('') +
    (s.offline ? '<span>Hub not reachable: paths saved earlier</span>' : '');
}

/* ---- drawing: one painter for the review panel and Watch together */
function mmScene() {
  const game = MM.game, t = MM.v ? MM.v.currentTime : 0, sq = MM.squad ? MM.sq[game.gi] : null;
  return {
    img: game.img, view: MM.view || (MM.view = mmDefaultView(game.pts, game.land)), t,
    main: {pts: game.pts, color: '#ffd60a'}, land: game.land, harv: game.harv, rings: mmRingsAt(game.rings, t),
    others: sq && sq.lines ? sq.lines : [], marks: (game.icons || []).filter(k => k.t <= t + 0.25).concat(mmMarks(game, t)), hover: MM.hover,
    setView: v => { MM.view = v; }, setHover: h => { MM.hover = h; },
  };
}
function mmDraw() {
  const cv = $('#mmCv'), box = $('#mmBox');
  if (!cv || !box || !MM.on || $('#mmPanel').hidden) return;
  if (document.hidden) { MM.dirty = true; return; }
  const game = MM.game;
  const sc = game && game.img && game.img.complete && game.img.naturalWidth ? mmScene() : null;
  const now = mmPaint(cv, box, sc);
  const cap = $('#mmCap');
  if (cap && sc) {
    const t = sc.t, from = mmJump(game.g);
    let row = null;                                     // what the scanner saw at this moment (spectating?)
    for (const r of game.g.seen || []) { if (r[0] <= t + 0.5) row = r; else break; }
    const spec = row && row[2] && t - row[0] < 5;
    const where = !game.pts.length || now ? '' : (from != null && t < from) ? ' · before the jump' :
      spec ? ' · spectating' : t < game.pts[0].t ? ' · not on the map yet' : ' · no position read now';
    cap.textContent = `Game ${MM.gi + 1} · ${mapName(game.g.map)}${game.landPoi ? ' · landed at ' + poiName(game.landPoi) : ''}${where}`;
  }
}

/* the view: centre (fraction) and zoom; by default the whole path with a margin */
function mmDefaultView(pts, land) {
  const P = pts.length ? pts : land ? [{fx: land[0], fy: land[1]}] : [];
  if (!P.length) return {cx: 0.5, cy: 0.5, z: 1};
  let x0 = 1, x1 = 0, y0 = 1, y1 = 0;
  P.forEach(p => { x0 = Math.min(x0, p.fx); x1 = Math.max(x1, p.fx); y0 = Math.min(y0, p.fy); y1 = Math.max(y1, p.fy); });
  const span = Math.min(1, Math.max(0.22, Math.max(x1 - x0, y1 - y0) * 1.3));
  return mmClamp({cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, z: 1 / span});
}
function mmClamp(vw) {
  vw.z = Math.max(1, Math.min(MM_ZMAX, vw.z));
  const h = 0.5 / vw.z;
  vw.cx = Math.max(h, Math.min(1 - h, vw.cx)); vw.cy = Math.max(h, Math.min(1 - h, vw.cy));
  return vw;
}

/* paints a scene on the canvas; returns where the main path is now (or null) */
function mmPaint(cv, box, sc) {
  const dpr = window.devicePixelRatio || 1, W = Math.round(box.clientWidth * dpr), H = Math.round(box.clientHeight * dpr);
  if (!W || !H) return null;
  if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
  const ctx = cv.getContext('2d');
  ctx.fillStyle = '#0b0b0a'; ctx.fillRect(0, 0, W, H);
  if (!sc) return null;
  const vw = sc.view, S = Math.min(W, H) * vw.z, t = sc.t;
  const X = fx => (fx - vw.cx) * S + W / 2, Y = fy => (fy - vw.cy) * S + H / 2;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(sc.img, X(0), Y(0), S, S);
  ctx.fillStyle = 'rgba(0,0,0,.25)'; ctx.fillRect(0, 0, W, H);       // the path stands out on bright maps (Storm Point)
  const lw = Math.max(1.5, 2 * dpr);
  ctx.lineJoin = ctx.lineCap = 'round';
  // ring circles: the ring now (white), the one before it (faint, dashed)
  sc.rings.forEach((c, i) => {
    const last = i === sc.rings.length - 1;
    ctx.beginPath(); ctx.arc(X(c.fx), Y(c.fy), c.fr * S, 0, 2 * Math.PI);
    ctx.setLineDash(last ? [] : [6 * dpr, 5 * dpr]);
    ctx.strokeStyle = last ? 'rgba(255,255,255,.9)' : 'rgba(255,255,255,.4)'; ctx.lineWidth = last ? lw : dpr; ctx.stroke();
  });
  ctx.setLineDash([]);
  const line = (pts, color, width) => {
    if (!pts.length) return;
    ctx.beginPath();
    pts.forEach((p, i) => { if (!i || p.brk) ctx.moveTo(X(p.fx), Y(p.fy)); else ctx.lineTo(X(p.fx), Y(p.fy)); });
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.stroke();
  };
  const dot = (fx, fy, r, fill, stroke) => {
    ctx.beginPath(); ctx.arc(X(fx), Y(fy), r * dpr, 0, 2 * Math.PI);
    ctx.fillStyle = fill; ctx.fill(); if (stroke) { ctx.lineWidth = 1.5 * dpr; ctx.strokeStyle = stroke; ctx.stroke(); }
  };
  // a path split at the moment: done (bright) and still to come (faint, only for the main one)
  const split = P => {
    let k = -1;
    while (k + 1 < P.length && P[k + 1].t <= t) k++;
    const now = mmAt(P, t), here = now && k >= 0 ? [{fx: now[0], fy: now[1]}] : [];
    return {now, done: [...P.slice(0, k + 1), ...here], rest: [...here, ...P.slice(k + 1)]};
  };
  const others = sc.others.map(o => Object.assign(split(o.pts), {o}));
  others.forEach(s => { line(s.done, 'rgba(0,0,0,.5)', lw + 2 * dpr); line(s.done, s.o.color, lw * 0.8); });
  const m = split(sc.main.pts);
  line(m.rest, 'rgba(241,239,234,.45)', lw);
  line(m.done, 'rgba(0,0,0,.6)', lw + 2 * dpr);
  line(m.done, sc.main.color, lw);
  sc.harv.forEach(h => {
    const x = X(h.fx), y = Y(h.fy), r = 5 * dpr;
    ctx.beginPath(); ctx.moveTo(x, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r); ctx.lineTo(x - r, y); ctx.closePath();
    ctx.fillStyle = h.ok ? '#c48bff' : 'rgba(196,139,255,.45)'; ctx.fill(); ctx.lineWidth = dpr; ctx.strokeStyle = '#121211'; ctx.stroke();
  });
  others.forEach(s => { if (s.o.land) dot(s.o.land[0], s.o.land[1], 4, s.o.color, '#121211'); });
  if (sc.land) dot(sc.land[0], sc.land[1], 6, '#5fd08f', '#121211');
  // marks: knocks / kills (cross), went down (grey cross), coach notes (yellow square; later ones faint)
  sc.marks.forEach(k => {
    const x = X(k.fx), y = Y(k.fy), r = 4.5 * dpr;
    if (k.kind === 'care') {             // care package: white diamond (faint once opened), its number by timing
      const q = 6 * dpr;
      ctx.globalAlpha = k.opened != null && k.opened <= t ? 0.5 : 1;
      ctx.beginPath(); ctx.moveTo(x, y - q); ctx.lineTo(x + q, y); ctx.lineTo(x, y + q); ctx.lineTo(x - q, y); ctx.closePath();
      ctx.fillStyle = '#121211'; ctx.fill(); ctx.lineWidth = 2 * dpr; ctx.strokeStyle = '#f1efea'; ctx.stroke();
      if (k.n) {
        ctx.font = `700 ${9 * dpr}px Consolas, monospace`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillStyle = '#f1efea'; ctx.fillText(String(k.n), x, y + 0.5 * dpr);
        ctx.textAlign = 'start'; ctx.textBaseline = 'alphabetic';
      }
      ctx.globalAlpha = 1;
      return;
    }
    if (k.kind === 'sh') {               // stronghold: octagon, red while it can be taken, grey after
      const q = 6.5 * dpr, taken = k.state === 'unavailable' && (k.until == null || k.until < t);
      ctx.beginPath();
      for (let i = 0; i < 8; i++) { const a = Math.PI / 8 + i * Math.PI / 4; ctx[i ? 'lineTo' : 'moveTo'](x + q * Math.cos(a), y + q * Math.sin(a)); }
      ctx.closePath(); ctx.fillStyle = taken ? '#55524c' : '#e8365a'; ctx.fill();
      ctx.lineWidth = 1.5 * dpr; ctx.strokeStyle = '#121211'; ctx.stroke();
      return;
    }
    if (k.kind === 'note') {
      ctx.globalAlpha = k.later ? 0.45 : 1;
      ctx.fillStyle = '#ffd60a'; ctx.fillRect(x - r, y - r, 2 * r, 2 * r);
      ctx.lineWidth = dpr; ctx.strokeStyle = '#121211'; ctx.strokeRect(x - r, y - r, 2 * r, 2 * r);
      ctx.globalAlpha = 1;
      return;
    }
    ctx.beginPath(); ctx.moveTo(x - r, y - r); ctx.lineTo(x + r, y + r); ctx.moveTo(x + r, y - r); ctx.lineTo(x - r, y + r);
    ctx.strokeStyle = '#121211'; ctx.lineWidth = 4 * dpr; ctx.stroke();
    ctx.strokeStyle = k.kind === 'down' ? '#b8b5ad' : '#ff453a'; ctx.lineWidth = 2 * dpr; ctx.stroke();
  });
  others.forEach(s => {
    if (!s.now) return;
    dot(s.now[0], s.now[1], 5, s.o.color, '#121211');
    ctx.font = `600 ${11 * dpr}px Consolas, monospace`;
    ctx.fillStyle = '#f1efea'; ctx.strokeStyle = 'rgba(0,0,0,.8)'; ctx.lineWidth = 3 * dpr;
    const lb = s.o.name.slice(0, 12), x = X(s.now[0]) + 7 * dpr, y = Y(s.now[1]) + 4 * dpr;
    ctx.strokeText(lb, x, y); ctx.fillText(lb, x, y);
  });
  const hv = sc.hover;
  if (hv) {
    dot(hv.fx, hv.fy, 4, '#fff', '#121211');
    const label = fmtT(hv.t) + (hv.label ? ' · ' + hv.label : ''), x = X(hv.fx) + 8 * dpr, y = Y(hv.fy) - 8 * dpr;
    ctx.font = `${12 * dpr}px Consolas, monospace`;
    const w = ctx.measureText(label).width + 8 * dpr, x0 = Math.min(x, W - w - 2);
    ctx.fillStyle = 'rgba(18,18,17,.85)'; ctx.fillRect(x0, y - 14 * dpr, w, 18 * dpr);
    ctx.fillStyle = '#f1efea'; ctx.fillText(label, x0 + 4 * dpr, y);
  }
  if (m.now) { dot(m.now[0], m.now[1], 9, 'rgba(229,57,45,.35)'); dot(m.now[0], m.now[1], 5.5, '#e5392d', '#fff'); }
  return m.now;
}

/* ---- pointer: wheel zoom about the pointer, drag to move, click on a mark or the path to go to that moment.
   `scene()` gives the scene now (or null), `go(t)` seeks, `redraw()` paints again. */
function mmPointer(cv, scene, go, redraw) {
  let drag = null;
  const point = (e, vw) => {
    const r = cv.getBoundingClientRect(), S = Math.min(r.width, r.height) * vw.z;
    return {fx: (e.clientX - r.left - r.width / 2) / S + vw.cx, fy: (e.clientY - r.top - r.height / 2) / S + vw.cy, S};
  };
  const nearest = (sc, p) => {
    let best = null, bd = (MM_HIT_PX + 2) / p.S;
    for (const k of sc.marks) { const d = Math.hypot(k.fx - p.fx, k.fy - p.fy); if (d < bd) { bd = d; best = k; } }
    if (best) return best;
    bd = MM_HIT_PX / p.S;
    for (const q of sc.main.pts) { const d = Math.hypot(q.fx - p.fx, q.fy - p.fy); if (d < bd) { bd = d; best = q; } }
    for (const o of sc.others) for (const q of o.pts) {
      const d = Math.hypot(q.fx - p.fx, q.fy - p.fy); if (d < bd) { bd = d; best = Object.assign({}, q, {label: o.name}); }
    }
    return best;
  };
  cv.addEventListener('wheel', e => {
    const sc = scene(); if (!sc) return;
    e.preventDefault();
    const vw = Object.assign({}, sc.view), p = point(e, vw), z = Math.max(1, Math.min(MM_ZMAX, vw.z * (e.deltaY < 0 ? 1.25 : 0.8)));
    // keep the spot under the pointer where it is
    vw.cx = p.fx - (p.fx - vw.cx) * vw.z / z; vw.cy = p.fy - (p.fy - vw.cy) * vw.z / z; vw.z = z;
    sc.setView(mmClamp(vw)); redraw();
  }, {passive: false});
  cv.addEventListener('pointerdown', e => {
    const sc = scene(); if (!sc || e.button !== 0) return;
    drag = {x: e.clientX, y: e.clientY, cx: sc.view.cx, cy: sc.view.cy, z: sc.view.z, moved: false};
    try { cv.setPointerCapture(e.pointerId); } catch (x) {}
  });
  cv.addEventListener('pointermove', e => {
    const sc = scene(); if (!sc) return;
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) < 4) return;
      drag.moved = true;
      const S = point(e, sc.view).S;
      sc.setView(mmClamp({cx: drag.cx - dx / S, cy: drag.cy - dy / S, z: drag.z}));
      cv.style.cursor = 'grabbing';
      sc.setHover(null); redraw();
      return;
    }
    const h = nearest(sc, point(e, sc.view));
    cv.style.cursor = h ? 'pointer' : 'grab';
    const old = sc.hover;
    if ((h && h.t) !== (old && old.t) || (h && h.fx) !== (old && old.fx)) { sc.setHover(h); redraw(); }
  });
  cv.addEventListener('pointerup', e => {
    const dr = drag; drag = null;
    cv.style.cursor = 'grab';
    const sc = scene();
    if (!dr || dr.moved || !sc) return;
    const h = nearest(sc, point(e, sc.view));
    if (h) go(h.t);
  });
  cv.addEventListener('pointercancel', () => { drag = null; });
  cv.addEventListener('pointerleave', () => { const sc = scene(); if (sc && sc.hover && !drag) { sc.setHover(null); redraw(); } });
}

/* ------------------------------------------------------------------ Watch together: the large view's player's path */
const WM = {on: true, key: '', st: null, data: null, info: null, img: null, lines: [], view: null, viewFor: '', hover: null,
            t: null, last: 0, dirty: false, seek: null, box: null, ro: null};
try { WM.on = localStorage.getItem('pcs-wt-map') !== '0'; } catch (e) {}

/* called by session.js when the session's state changes: (re)builds the panel in `box` for that game */
function wtMapSetup(box, st, go) {
  if (!box || !st) return;
  WM.st = st; WM.seek = go;
  if (WM.box !== box) {
    WM.box = box; WM.key = '';
    box.className = 'mm wt-map';
    box.innerHTML = `<div class="mm-head"><b>Map</b><span class="mm-cap faint" id="wmCap"></span>
        <button class="btn small ghost" id="wmReset" title="Back to the whole path">Reset</button>
        <button class="btn small ghost" id="wmHide">${WM.on ? 'Hide' : 'Show'}</button></div>
      <div class="mm-box" id="wmBox"${WM.on ? '' : ' hidden'}><canvas id="wmCv"></canvas><div class="mm-msg" id="wmMsg" hidden></div></div>
      <div class="mm-squad faint" id="wmSquad"${WM.on ? '' : ' hidden'}></div>`;
    $('#wmReset').onclick = () => { WM.view = null; wmDraw(); };
    $('#wmHide').onclick = () => {
      WM.on = !WM.on;
      try { localStorage.setItem('pcs-wt-map', WM.on ? '1' : '0'); } catch (e) {}
      $('#wmHide').textContent = WM.on ? 'Hide' : 'Show';
      $('#wmBox').hidden = $('#wmSquad').hidden = !WM.on;
      if (WM.on) wmDraw();
    };
    mmPointer($('#wmCv'), () => wmScene(), t => WM.seek && WM.seek(t), () => wmDraw());
    if (WM.ro) WM.ro.disconnect();
    WM.ro = new ResizeObserver(() => wmDraw());
    WM.ro.observe($('#wmBox'));
  }
  const key = st.gameKey;
  if (key !== WM.key) { WM.key = key; WM.data = WM.info = WM.img = null; WM.lines = []; WM.view = null; wmLoad(st); }
  else { wmLegend(); wmDraw(); }
}
/* called by session.js on its own 4-per-second tick (not while hidden) with the moment shown (game time) */
function wtMapTick(t) {
  if (!WM.box || !WM.on || !document.contains(WM.box)) return;
  if (document.hidden) { WM.dirty = true; return; }
  if (WM.t != null && Math.abs(t - WM.t) < 0.2) return;
  WM.t = t;
  wmDraw();
}
function wmMsg(text) { const m = $('#wmMsg'); if (m) { m.hidden = !text; m.textContent = text || ''; } }
async function wmLoad(st) {
  const key = st.gameKey, map = st.game && st.game.map;
  if (!map || map === '?') { wmMsg('No minimap for this map yet'); return; }
  wmMsg('Loading the map…');
  const info = await mmInfo(map);
  if (WM.key !== key) return;
  if (!info.available) { wmMsg(info.text || 'No minimap for this map yet'); return; }
  let r;
  try { r = await api('/api/squadpaths?gameKey=' + encodeURIComponent(key)); }
  catch (e) { r = {available: false, text: 'Squad paths need the team hub'}; }
  if (WM.key !== key) return;
  WM.info = info; WM.data = r;
  WM.lines = r.available ? mmSquadLines(r.players, info, 0) : [];
  WM.img = mmImage(map, info, () => { if (WM.key === key) { wmMsg(''); wmDraw(); } }, () => { if (WM.key === key) wmMsg('Minimap needs the team hub'); });
  if (!r.available) wmMsg(r.text || 'Squad paths need the team hub');
  wmLegend(); wmDraw();
}
function wmFocus() {
  const f = WM.st && WM.st.focus, L = WM.lines;
  return L.find(l => l.name === f) || L.find(l => l.name.toLowerCase() === String(f || '').toLowerCase()) || null;
}
function wmLegend() {
  const el = $('#wmSquad'); if (!el) return;
  const f = wmFocus(), cap = $('#wmCap');
  if (cap) cap.textContent = WM.st ? `${mapName(WM.st.game && WM.st.game.map)} · ${f ? f.name : (WM.st.focus || '')}` : '';
  if (!WM.data || !WM.data.available) { el.textContent = ''; return; }
  el.innerHTML = WM.lines.map(l => `<span><i class="mm-k" style="background:${l === f ? '#ffd60a' : l.color}"></i>${esc(l.name)}${l.pts.length ? '' : ' (no positions)'}</span>`).join('') +
    (WM.data.offline ? '<span>Hub not reachable: paths saved earlier</span>' : '') +
    '<span>Click a path to go there</span>';
}
function wmScene() {
  if (!WM.img || !WM.img.complete || !WM.img.naturalWidth || !WM.data || !WM.data.available) return null;
  const f = wmFocus(), t = WM.t ?? 0;
  if (WM.viewFor !== (f ? f.name : '')) { WM.view = null; WM.viewFor = f ? f.name : ''; }
  const main = f ? f.pts : [];
  return {
    img: WM.img, view: WM.view || (WM.view = mmDefaultView(main.length ? main : WM.lines.flatMap(l => l.pts), f && f.land)), t,
    main: {pts: main, color: '#ffd60a'}, land: f ? f.land : null, harv: [], rings: [], marks: [],
    others: WM.lines.filter(l => l !== f), hover: WM.hover,
    setView: v => { WM.view = v; }, setHover: h => { WM.hover = h; },
  };
}
function wmDraw() {
  const cv = $('#wmCv'), box = $('#wmBox');
  if (!cv || !box || !WM.on || box.hidden) return;
  if (document.hidden) { WM.dirty = true; return; }
  const sc = wmScene();
  mmPaint(cv, box, sc);
  if (sc && !sc.main.pts.length) wmMsg(`No positions for ${WM.st.focus} in this game`);
  else if (sc) wmMsg('');
}
