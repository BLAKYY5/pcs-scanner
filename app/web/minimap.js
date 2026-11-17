/* Map panel of the review player: the game's own minimap with the player's path in the game on screen, a marker at
   the video's moment, the landing spot and the harvesters. Loaded after app.js (uses $, esc, api, TOKEN, page,
   pageArg, RV, gameAt, seek, mapName, poiName, fmtT).

   The pictures come from the team hub (EA's files: never in the app). The app downloads one with the player's key the
   first time it is needed and keeps it in its data folder (/api/minimap/<map> says whether it is there, /minimap/<map>
   serves it). Positions are the debug overlay's world x, y read about once a second (`seen` rows of the scan), placed
   with the game's own transform: fraction across = (x - pos_x) / scale / size, down = (pos_y - y) / scale / size.

   Drawn on a canvas only when something changes (the video's moment, the view, the size): no animation loop. */
'use strict';

const MM = {
  on: true, d: null, v: null, gi: -2, info: {}, img: {}, game: null, view: null, t: -1, hover: null, drag: null, ro: null,
};
const MM_GAP_S = 6, MM_FAST = 3000;                   // a gap in the path: no read for 6 s, or faster than 3000 units/s
const MM_ZMAX = 16, MM_HIT_PX = 10;
try { MM.on = localStorage.getItem('pcs-map') !== '0'; } catch (e) {}

function minimapSetup(d, v) {
  if (MM.ro) MM.ro.disconnect();
  Object.assign(MM, {d, v, gi: -2, game: null, view: null, t: -1, hover: null, drag: null});
  const side = $('.review .side'); if (!side) return;
  const box = document.createElement('div');
  box.className = 'mm'; box.id = 'mmPanel'; box.hidden = !MM.on;
  box.innerHTML = `<div class="mm-head"><b>Map</b><span class="mm-cap faint" id="mmCap"></span>
      <button class="btn small ghost" id="mmReset" title="Back to the whole path">Reset</button>
      <button class="btn small ghost" id="mmHide" title="Hide the map (M)">Hide</button></div>
    <div class="mm-box" id="mmBox"><canvas id="mmCv"></canvas><div class="mm-msg" id="mmMsg" hidden></div></div>
    <div class="mm-legend faint" id="mmLegend"><span><i class="mm-k now"></i>Now</span><span><i class="mm-k land"></i>Landing</span><span><i class="mm-k harv"></i>Harvester</span><span>Click the path to go there · wheel to zoom · drag to move</span></div>`;
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
  const cv = $('#mmCv');
  cv.addEventListener('wheel', mmWheel, {passive: false});
  cv.addEventListener('pointerdown', mmDown);
  cv.addEventListener('pointermove', mmMove);
  cv.addEventListener('pointerup', mmUp);
  cv.addEventListener('pointercancel', () => { MM.drag = null; });
  cv.addEventListener('pointerleave', () => { if (MM.hover && !MM.drag) { MM.hover = null; mmDraw(); } });
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

function mmLive() { return MM.on && MM.v && document.contains(MM.v) && page === 'review' && MM.d && pageArg === MM.d.id; }

/* the video's moment changed: pick the game, then redraw if the marker or the travelled part moved */
function mmTime() {
  if (!mmLive()) return;
  const t = MM.v.currentTime, games = MM.d.games || [];
  const gi = games.length ? gameAt(t) : -1;
  if (gi !== MM.gi) { MM.gi = gi; mmGame(gi); return; }
  if (Math.abs(t - MM.t) < 0.2) return;
  MM.t = t;
  mmDraw();
}

function mmMsg(text) {
  const m = $('#mmMsg'); if (!m) return;
  m.hidden = !text; m.textContent = text || '';
  const lg = $('#mmLegend'); if (lg) lg.hidden = !!text;
}

/* the game on screen changed: its path, then its map picture (from the hub, once) */
async function mmGame(gi) {
  MM.game = null; MM.view = null; MM.hover = null;
  const cap = $('#mmCap');
  if (!MM.d.scan || !MM.d.games) { mmMsg('Scan this recording to see your path on the map'); if (cap) cap.textContent = ''; mmDraw(); return; }
  const g = MM.d.games[gi];
  if (!g) { mmMsg('No game in this recording'); if (cap) cap.textContent = ''; mmDraw(); return; }
  if (cap) cap.textContent = `Game ${gi + 1} · ${mapName(g.map)}`;
  if (!g.map || g.map === '?') { mmMsg('No minimap for this map yet'); mmDraw(); return; }
  mmMsg('Loading the map…');
  let info = MM.info[g.map];
  if (!info) {
    try { info = await api('/api/minimap/' + encodeURIComponent(g.map)); }
    catch (e) { info = {available: false, reason: 'hub', text: 'Minimap needs the team hub'}; }
    if (info.available) MM.info[g.map] = info;           // a "not there" answer is asked again next time
  }
  if (!mmLive() || MM.gi !== gi) return;
  if (!info.available) { mmMsg(info.text || 'No minimap for this map yet'); mmDraw(); return; }
  const game = mmPath(g, info);
  let img = MM.img[g.map];
  if (!img || img.dataset.asset !== info.asset) {
    img = new Image();
    img.dataset.asset = info.asset;
    img.src = `/minimap/${encodeURIComponent(g.map)}?t=${encodeURIComponent(TOKEN)}&v=${info.asset}`;
    MM.img[g.map] = img;
  }
  game.img = img;
  MM.game = game;
  mmMsg(game.pts.length ? '' : 'No positions read in this game');
  if (img.complete && img.naturalWidth) mmDraw();
  else {
    img.onload = () => { if (MM.game === game) mmDraw(); };
    img.onerror = () => { if (MM.game === game) { delete MM.img[g.map]; delete MM.info[g.map]; mmMsg('Minimap needs the team hub'); } };
  }
}

/* world x, y -> fraction of the picture (0..1 from the top left) */
function mmFrac(info, x, y) { return [(x - info.pos_x) / info.scale / info.size, (info.pos_y - y) / info.scale / info.size]; }

/* the player's positions in this game: from the jump to the game's end, not while spectating, split at gaps */
function mmPath(g, info) {
  const L = g.landing || {};
  const from = L.exitAtExact ?? L.exitAt ?? g.tStart ?? -Infinity;
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
  // a misread number gives a lone point far away (a spike out and back): dropped, the path joins again
  for (let i = pts.length - 2; i >= 1; i--) {
    const a = pts[i - 1], p = pts[i], b = pts[i + 1];
    const far = (u, v) => Math.hypot(u.x - v.x, u.y - v.y) / Math.max(0.5, Math.abs(u.t - v.t)) > MM_FAST;
    if (far(a, p) && far(p, b) && !far(a, b) && b.t - a.t <= MM_GAP_S) { pts.splice(i, 1); b.brk = false; }
  }
  const land = L.x != null && L.y != null ? mmFrac(info, L.x, L.y) : null;
  // harvesters are read on the full map in map %: the game's minimap is framed the same way
  const harv = (g.harvesters || []).filter(h => h.x != null && h.y != null).map(h => ({fx: h.x / 100, fy: h.y / 100, ok: h.reliable !== false, poi: h.poi}));
  return {g, info, pts, land, landPoi: L.poi || null, harv};
}

/* the view: centre (fraction) and zoom; by default the whole path with a margin */
function mmDefaultView(game) {
  const P = game.pts.length ? game.pts : game.land ? [{fx: game.land[0], fy: game.land[1]}] : [];
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

/* where the player is at t: between two reads close in time, else none */
function mmAt(game, t) {
  const P = game.pts; if (!P.length || t < P[0].t - 1 || t > P[P.length - 1].t + 2) return null;
  let i = 0;
  while (i + 1 < P.length && P[i + 1].t <= t) i++;
  const a = P[i], b = P[i + 1];
  if (b && !b.brk && t > a.t) { const k = (t - a.t) / (b.t - a.t); return [a.fx + (b.fx - a.fx) * k, a.fy + (b.fy - a.fy) * k]; }
  return t - a.t <= MM_GAP_S || t < a.t ? [a.fx, a.fy] : null;
}

function mmDraw() {
  const cv = $('#mmCv'), box = $('#mmBox');
  if (!cv || !box || !MM.on || $('#mmPanel').hidden) return;
  const dpr = window.devicePixelRatio || 1, W = Math.round(box.clientWidth * dpr), H = Math.round(box.clientHeight * dpr);
  if (!W || !H) return;
  if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
  const ctx = cv.getContext('2d');
  ctx.fillStyle = '#0b0b0a'; ctx.fillRect(0, 0, W, H);
  const game = MM.game;
  if (!game || !game.img.complete || !game.img.naturalWidth) return;
  if (!MM.view) MM.view = mmDefaultView(game);
  const vw = MM.view, S = Math.min(W, H) * vw.z;
  const X = fx => (fx - vw.cx) * S + W / 2, Y = fy => (fy - vw.cy) * S + H / 2;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(game.img, X(0), Y(0), S, S);
  ctx.fillStyle = 'rgba(0,0,0,.25)'; ctx.fillRect(0, 0, W, H);       // the path stands out on bright maps (Storm Point)
  const t = MM.v ? MM.v.currentTime : 0, lw = Math.max(1.5, 2 * dpr);
  ctx.lineJoin = ctx.lineCap = 'round';
  // the path: done (bright, up to the marker) and still to come (faint)
  const P = game.pts, now = mmAt(game, t);
  let k = -1;
  while (k + 1 < P.length && P[k + 1].t <= t) k++;
  const line = (pts, color, width) => {
    ctx.beginPath();
    pts.forEach((p, i) => { if (!i || p.brk) ctx.moveTo(X(p.fx), Y(p.fy)); else ctx.lineTo(X(p.fx), Y(p.fy)); });
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.stroke();
  };
  const here = now && k >= 0 ? [{fx: now[0], fy: now[1]}] : [];
  const nx = P[k + 1];
  line([...here, ...(nx ? [nx] : []), ...P.slice(k + 2)], 'rgba(241,239,234,.45)', lw);
  const done = [...P.slice(0, k + 1), ...here];
  line(done, 'rgba(0,0,0,.6)', lw + 2 * dpr);
  line(done, '#ffd60a', lw);
  const dot = (fx, fy, r, fill, stroke) => {
    ctx.beginPath(); ctx.arc(X(fx), Y(fy), r * dpr, 0, 2 * Math.PI);
    ctx.fillStyle = fill; ctx.fill(); if (stroke) { ctx.lineWidth = 1.5 * dpr; ctx.strokeStyle = stroke; ctx.stroke(); }
  };
  game.harv.forEach(h => {
    const x = X(h.fx), y = Y(h.fy), r = 5 * dpr;
    ctx.beginPath(); ctx.moveTo(x, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r); ctx.lineTo(x - r, y); ctx.closePath();
    ctx.fillStyle = h.ok ? '#c48bff' : 'rgba(196,139,255,.45)'; ctx.fill(); ctx.lineWidth = dpr; ctx.strokeStyle = '#121211'; ctx.stroke();
  });
  if (game.land) dot(game.land[0], game.land[1], 6, '#5fd08f', '#121211');
  if (MM.hover) {
    dot(MM.hover.fx, MM.hover.fy, 4, '#fff', '#121211');
    const label = fmtT(MM.hover.t), x = X(MM.hover.fx) + 8 * dpr, y = Y(MM.hover.fy) - 8 * dpr;
    ctx.font = `${12 * dpr}px Consolas, monospace`;
    const w = ctx.measureText(label).width + 8 * dpr;
    ctx.fillStyle = 'rgba(18,18,17,.85)'; ctx.fillRect(x, y - 14 * dpr, w, 18 * dpr);
    ctx.fillStyle = '#f1efea'; ctx.fillText(label, x + 4 * dpr, y);
  }
  if (now) { dot(now[0], now[1], 9, 'rgba(229,57,45,.35)'); dot(now[0], now[1], 5.5, '#e5392d', '#fff'); }
  const cap = $('#mmCap');
  if (cap) {
    const L = game.g.landing || {}, from = L.exitAtExact ?? L.exitAt;
    let row = null;                                     // what the scanner saw at this moment (spectating?)
    for (const r of game.g.seen || []) { if (r[0] <= t + 0.5) row = r; else break; }
    const spec = row && row[2] && t - row[0] < 5;
    const where = !game.pts.length || now ? '' : (from != null && t < from) ? ' · before the jump' :
      spec ? ' · spectating' : t < game.pts[0].t ? ' · not on the map yet' : ' · no position read now';
    cap.textContent = `Game ${MM.gi + 1} · ${mapName(game.g.map)}${game.landPoi ? ' · landed at ' + poiName(game.landPoi) : ''}${where}`;
  }
}

/* ---- view: wheel zoom about the pointer, drag to move, click on the path to go to that moment */
function mmPoint(e) {
  const cv = $('#mmCv'), r = cv.getBoundingClientRect(), vw = MM.view;
  const S = Math.min(r.width, r.height) * vw.z;
  return {px: e.clientX - r.left, py: e.clientY - r.top, fx: (e.clientX - r.left - r.width / 2) / S + vw.cx, fy: (e.clientY - r.top - r.height / 2) / S + vw.cy, S};
}
function mmWheel(e) {
  if (!MM.game || !MM.view) return;
  e.preventDefault();
  const p = mmPoint(e), vw = MM.view, z = Math.max(1, Math.min(MM_ZMAX, vw.z * (e.deltaY < 0 ? 1.25 : 0.8)));
  // keep the spot under the pointer where it is
  vw.cx = p.fx - (p.fx - vw.cx) * vw.z / z; vw.cy = p.fy - (p.fy - vw.cy) * vw.z / z; vw.z = z;
  mmClamp(vw); mmDraw();
}
function mmDown(e) {
  if (!MM.game || !MM.view || e.button !== 0) return;
  MM.drag = {x: e.clientX, y: e.clientY, cx: MM.view.cx, cy: MM.view.cy, moved: false};
  e.target.setPointerCapture(e.pointerId);
}
function mmNearest(p) {
  let best = null, bd = MM_HIT_PX / p.S;
  for (const q of MM.game.pts) { const d = Math.hypot(q.fx - p.fx, q.fy - p.fy); if (d < bd) { bd = d; best = q; } }
  return best;
}
function mmMove(e) {
  if (!MM.game || !MM.view) return;
  const dr = MM.drag;
  if (dr) {
    const dx = e.clientX - dr.x, dy = e.clientY - dr.y;
    if (!dr.moved && Math.hypot(dx, dy) < 4) return;
    dr.moved = true;
    const S = mmPoint(e).S;
    MM.view.cx = dr.cx - dx / S; MM.view.cy = dr.cy - dy / S; mmClamp(MM.view);
    e.target.style.cursor = 'grabbing';
    MM.hover = null; mmDraw();
    return;
  }
  const h = mmNearest(mmPoint(e));
  e.target.style.cursor = h ? 'pointer' : 'grab';
  if (h !== MM.hover) { MM.hover = h; mmDraw(); }
}
function mmUp(e) {
  const dr = MM.drag; MM.drag = null;
  e.target.style.cursor = 'grab';
  if (!dr || dr.moved || !MM.game) return;
  const h = mmNearest(mmPoint(e));
  if (h) seek(h.t);
}
