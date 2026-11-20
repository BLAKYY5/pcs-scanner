/* Watch together: the coach and his squad watch one game together, from every player's view (team hub 0.5).
   Loaded after app.js (uses $, $$, esc, api, toast, fmtT, mapName, INK, TOOLS, drawShapes, confirmBox, page, S).

   Each view is that player's YouTube video of the game, lined up by the jump from the dropship: game time 0 = the
   jump, video time = videoAt + game time. The hub keeps "game time t at hub time at, playing, speed"; this page
   computes the same moment from it (hub clock estimated by the app: snap.offset) and keeps every YouTube player
   within DRIFT_S of it. Buffering on one PC never holds the others: each app follows the clock on its own and
   reports where it is, so the coach sees who is behind. The only external script is YouTube's player API.

   A player may leave the group's moment for a while (pause, steps, frames: "browsing"): only their own view moves,
   the coach sees "<player> browsing", and "Back to coach" rejoins the shared moment. */
'use strict';

const WT = {snap: null, seq: -1, polling: false, list: null, yt: {}, ytKey: '', vol: 100, muted: false,
            tool: 'pen', color: INK[0], cur: null, sendAt: 0, sendTimer: null, loop: null, lastReport: 0,
            barDismissed: '', listTimer: null, local: null, fade: null, ptrSent: 0, ptrTimer: null, talk: null};
try { WT.muted = localStorage.getItem('pcs-wt-muted') === '1'; WT.vol = +(localStorage.getItem('pcs-wt-vol') || 100); } catch (e) {}
const saveSound = () => { try { localStorage.setItem('pcs-wt-muted', WT.muted ? '1' : '0'); localStorage.setItem('pcs-wt-vol', String(WT.vol)); } catch (e) {} };
const DRIFT_S = 0.5;         // further than this from the shared moment while playing: seek
const PAUSED_DRIFT_S = 0.1;   // paused: everyone on the same picture
const FRAME_S = 1 / 60;       // one frame of a 60 fps recording (frame steps while paused)
const FADE_MS = 300;          // drawings fade out this long when play resumes (fixed, not tied to video frames)
const POINTER_IDLE_MS = 2000;
const YT_PLAYING = 1, YT_PAUSED = 2, YT_BUFFERING = 3, YT_CUED = 5;

/* ------------------------------------------------------------------ shared clock, and a player's own moment */
const hubNow = () => Date.now() / 1000 + ((WT.snap && WT.snap.offset) || 0);
function gameTime(st, at) {
  if (!st) return 0;
  const t = st.playing ? st.t + ((at ?? hubNow()) - st.at) * st.speed : st.t;
  return Math.min(Math.max(t, st.range[0]), st.range[1]);
}
const clampT = (st, t) => Math.min(Math.max(t, st.range[0]), st.range[1]);
function localTime(st) {           // a browsing player's own moment (their own clock)
  const l = WT.local; if (!l) return gameTime(st);
  return clampT(st, l.playing ? l.t + (performance.now() / 1000 - l.wall) * st.speed : l.t);
}
const viewTime = st => WT.local ? localTime(st) : gameTime(st);
const viewPlaying = st => WT.local ? WT.local.playing : st.playing;
const fmtG = t => (t < 0 ? '−' : '') + fmtT(Math.abs(t));        // game time: before the jump is negative
const fmtF = t => fmtG(t) + '.' + String(Math.floor(((Math.abs(t) % 1) * 60) + 1e-6)).padStart(2, '0');   // + frame
const isCoach = () => !!(WT.snap && WT.snap.me && WT.snap.me.role === 'coach');
const inSession = () => !!(WT.snap && WT.snap.sid && !WT.snap.ended);
const gameTitle = g => g ? `${mapName(g.map)} · ${g.date ? new Date(g.date + 'T12:00').toLocaleDateString(undefined, {day: 'numeric', month: 'short'}) : ''} ${(g.jumpClock || '').slice(0, 5)}`.trim() : t('Game');

/* ------------------------------------------------------------------ Team tab card */
async function watchCard(box) {
  if (!box) return;
  box.innerHTML = `<section class="card section wt-card"><h2>${t('Watch together')}</h2><p class="muted">${t('Loading…')}</p></section>`;
  let l;
  try { l = WT.list = await api('/api/watch/list?fresh=1'); }
  catch (e) { box.innerHTML = `<section class="card section wt-card"><h2>${t('Watch together')}</h2><p class="muted">${esc(tx(e.message))}</p></section>`; return; }
  if (!l.available) { box.innerHTML = ''; return; }
  const running = (l.sessions || []).map(s => `<div class="wt-row"><div><b>${esc(gameTitle(s.game))}</b>
      <span class="faint">${t('Coach {coach} · {members} in', {coach: esc(s.coach), members: s.members.length})}</span></div>
      <button class="btn small brand" data-join="${esc(s.id)}">${l.current === s.id ? t('Open') : t('Join')}</button></div>`).join('');
  const games = l.role === 'coach' ? (l.games || []).map(g => `<div class="wt-row"><div><b>${esc(gameTitle(g))}</b>
      <span class="faint">${t('{videos} of {views} views on YouTube: {names}', {videos: g.videos.length, views: new Set(g.players).size, names: esc([...new Set(g.videos)].join(', '))})}</span></div>
      <button class="btn small" data-start="${esc(g.gameKey)}">${t('Start session')}</button></div>`).join('') : '';
  const plans = l.role === 'coach' ? (l.plans || []).filter(p => p.moments).slice(0, 8).map(p => `<div class="wt-row"><div><b>${esc(p.title)}</b>
      <span class="faint">${p.moments === 1 ? t('1 moment') : t('{n} moments', {n: p.moments})} · ${p.games === 1 ? t('1 game') : t('{n} games', {n: p.games})}</span></div>
      <button class="btn small" data-plan="${esc(p.id)}">${t('Start with plan')}</button></div>`).join('') : '';
  box.innerHTML = `<section class="card section wt-card"><h2>${t('Watch together')}</h2>
    <p class="muted">${l.role === 'coach' ? t('Watch a game with your players: you control it, they see the same moment from every player\'s view, and what you draw.') : t('Your coach plays a game for the squad: you see what he shows and draws.')}</p>
    ${running ? `<h3>${t('Running now')}</h3>${running}` : `<p class="faint">${l.role === 'coach' ? t('No session running.') : t('No session running. Your coach starts one.')}</p>`}
    ${plans ? `<h3>${t('Session plans')}</h3>${plans}` : ''}
    ${l.role === 'coach' ? (games ? `<h3>${t('Games with YouTube videos')}</h3>${games}` : '<p class="faint">' + t('No game with a YouTube video yet. Videos show here once a player\'s scrim block is on YouTube.') + '</p>') : ''}
    ${l.role === 'coach' ? '<p class="faint">' + t('Plan the moments to show on the team hub: Coach → Session plans.') + '</p>' : ''}
  </section>`;
  $$('[data-join]', box).forEach(b => b.onclick = () => joinSession(b.dataset.join, b));
  $$('[data-start]', box).forEach(b => b.onclick = () => startSession(b.dataset.start, b));
  $$('[data-plan]', box).forEach(b => b.onclick = () => startSession('', b, b.dataset.plan));
}
async function startSession(gameKey, btn, plan) {
  if (btn) btn.disabled = true;
  try { WT.local = null; applySnap(await api('/api/watch/start', plan ? {gameKey, plan} : {gameKey})); location.hash = '#watch'; }
  catch (e) { toast(e.message, true); }
  if (btn) btn.disabled = false;
}
async function joinSession(id, btn) {
  if (btn) btn.disabled = true;
  try {
    if (!(WT.snap && WT.snap.sid === id && !WT.snap.ended)) { WT.local = null; applySnap(await api('/api/watch/join', {id})); }
    location.hash = '#watch';
  } catch (e) { toast(e.message, true); }
  if (btn) btn.disabled = false;
}

/* ------------------------------------------------------------------ "Join" bar on the other pages */
async function checkSessions() {
  clearTimeout(WT.listTimer); WT.listTimer = null;
  if (document.hidden) return;                       // hidden (tray, minimized): nothing until shown (visibilitychange)
  WT.listTimer = setTimeout(checkSessions, 15000);
  // while Apex runs nobody reviews: the hub isn't asked (quiet window, see app.js)
  if ((S && S.apex) || !S || !S.hub || !S.hub.url || !S.hub.key || S.hub.refused) return showBar(null);
  try { WT.list = await api('/api/watch/list'); } catch (e) { return; }
  const mine = inSession() ? (WT.list.sessions || []).find(s => s.id === WT.snap.sid) : null;
  const other = (WT.list.sessions || []).find(s => s.canJoin && s.id !== WT.barDismissed && (!inSession() || s.id === WT.snap.sid));
  showBar(page === 'watch' ? null : (mine || other || null));
}
function showBar(s) {
  let bar = $('#wtBar');
  if (!s) { if (bar) bar.remove(); return; }
  if (!bar) { bar = document.createElement('div'); bar.id = 'wtBar'; bar.className = 'wt-joinbar'; document.body.insertBefore(bar, $('#main')); }
  const back = inSession() && WT.snap.sid === s.id;
  bar.innerHTML = `<span><b>${t('Watch together')}</b> ${back ? t('Your session is still running: {game}', {game: esc(gameTitle(s.game))}) : t('Coach {coach} is reviewing: {game}', {coach: esc(s.coach), game: esc(gameTitle(s.game))})}</span>
    <button class="btn small brand" id="wtBarJoin">${back ? t('Back to it') : t('Join')}</button>${back ? '' : '<button class="btn small ghost" id="wtBarNo">' + t('Not now') + '</button>'}`;
  $('#wtBarJoin').onclick = () => joinSession(s.id, $('#wtBarJoin'));
  const no = $('#wtBarNo'); if (no) no.onclick = () => { WT.barDismissed = s.id; showBar(null); };
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) checkSessions(); });
setTimeout(checkSessions, 3000);

/* ------------------------------------------------------------------ the session page */
async function renderWatch() {
  if (!inSession()) {                 // the page was reloaded: the app still knows the session
    try { const s = await api('/api/watch/poll?seq=-2&wait=0'); if (s.sid && !s.ended) { WT.snap = s; WT.seq = s.seq; } } catch (e) {}
  }
  if (page !== 'watch') return;
  if (!inSession()) {
    $('#main').innerHTML = `<div class="empty"><strong>${t('No session open')}</strong>${t('Join or start one in {team}.', {team: '<a href="#team">' + t('Team') + '</a>'})}</div>`;
    return;
  }
  showBar(null);
  $('#main').innerHTML = `<div class="wt" id="wt">
    <div class="pagehead"><div><h1 id="wtTitle">${t('Watch together')}</h1><p id="wtSub" class="muted"></p></div>
      <div class="toolbar"><span id="wtConn" class="pill"></span><button class="btn small ghost" id="wtLeave">${t('Leave')}</button><button class="btn small danger" id="wtEnd" hidden>${t('End session')}</button></div></div>
    <div class="wt-plan" id="wtPlan" hidden></div>
    <div class="wt-own" id="wtOwn" hidden></div>
    <div class="wt-stage" id="wtStage"><canvas class="wt-ink" id="wtInk"></canvas><div class="wt-dot" id="wtDot" hidden></div></div>
    <div class="card wt-bar" id="wtControls"></div>
    <div class="wt-side">
      <div id="wtMap"></div>
      <div class="wt-members" id="wtMembers"></div>
      <div class="wt-lists" id="wtLists"></div>
    </div>
    <div id="wtTalk"></div>
  </div>`;
  $('#wtLeave').onclick = leaveSession;
  $('#wtEnd').onclick = endSession;
  setupInkInput();
  if (window.ResizeObserver) { WT.ro = new ResizeObserver(() => layout()); WT.ro.observe($('#wtStage')); }
  WT.ytKey = '';
  drawAll();
  pollLoop();
  loadTalk();
}
window.addEventListener('hashchange', () => { if (page !== 'watch') { stopPlayers(); checkSessions(); } });
window.addEventListener('keydown', e => {
  if (page !== 'watch' || !inSession() || /INPUT|TEXTAREA|SELECT/.test((document.activeElement || {}).tagName || '') || e.ctrlKey && !/^[zZ]$/.test(e.key)) return;
  const st = WT.snap.state, coach = isCoach(), k = e.key, big = e.shiftKey ? 10 : 5;
  let done = true;
  if (k === ' ') coach ? control(st.playing ? 'pause' : 'play') : ownToggle();
  else if (k === 'ArrowLeft' || k === 'ArrowRight') step((k === 'ArrowLeft' ? -1 : 1) * big);
  else if (k === ',' || k === '.') frame(k === ',' ? -1 : 1);
  else if (coach && (e.ctrlKey || e.metaKey) && (k === 'z' || k === 'Z')) draw({op: 'undo'});
  else if (coach && (k === 'b' || k === 'B')) addBookmark();
  else if (!coach && (k === 'f' || k === 'F')) sendFlag();
  else if (coach && planItems(st).length && /^[nNpP]$/.test(k)) planStep(/^[nN]$/.test(k) ? 1 : -1);
  else if (!coach && k === 'Escape' && WT.local) backToCoach();
  else done = false;
  if (done) e.preventDefault();
});

async function pollLoop() {
  if (WT.polling) return;
  WT.polling = true;
  while (page === 'watch' && inSession()) {
    // a long poll to the app on this PC: it waits there, costs nothing, and still hears at once when the session
    // ends (e.g. the app left it because Apex started), so the players stop even in a hidden window
    try { applySnap(await api(`/api/watch/poll?seq=${WT.seq}&wait=${document.hidden ? 25 : 15}`)); }
    catch (e) { await new Promise(r => setTimeout(r, 1500)); }
  }
  WT.polling = false;
  if (page === 'watch' && WT.snap && WT.snap.ended) {
    stopPlayers();
    const why = {ended: t('The coach ended the session.'), idle: t('The session ended (the coach left).'), replaced: t('The coach started another session.'), left: t('You left the session.'), apex: t('Apex started, so you left the session. The others keep watching.')}[WT.snap.ended] || t('The session ended.');
    $('#main').innerHTML = `<div class="empty"><strong>${t('Session over')}</strong>${esc(why)} <a href="#team">${t('Team')}</a></div>`;
  }
}
function applySnap(s) {
  if (!s) return;
  const before = WT.snap && WT.snap.state;
  WT.snap = s; WT.seq = s.seq;
  const st = s.state;
  if (before && st) {
    if (before.focus !== st.focus) WT.cur = null;
    // drawings that left the screen because the moment moved on (play, seek, other view) fade out; a clear is at once
    const gone = (before.strokes || []).filter(x => !(st.strokes || []).some(y => y.id === x.id));
    const moved = st.playing && !before.playing || st.t !== before.t || st.focus !== before.focus || st.gameKey !== before.gameKey;
    // the coach went to another moment of the plan: everyone there, also a player browsing on their own
    if (planAt(st) !== planAt(before) && planAt(st) >= 0) { WT.local = null; WT.exact = false; }
    if (gone.length && moved && !WT.local) startFade(gone);
  }
  if (page !== 'watch') return;
  if (before && st && JSON.stringify(before) === JSON.stringify(st)) return drawPointer();   // only the pointer moved
  drawAll();
}

/* ------------------------------------------------------------------ drawing the page from the state */
function drawAll() {
  const st = WT.snap && WT.snap.state; if (!st || !$('#wt')) return;
  $('#wtTitle').textContent = t('Watch together') + ' · ' + gameTitle(st.game);
  $('#wtSub').textContent = isCoach() ? t('You lead: everyone who follows sees what you play, pause and draw.') : t('Coach {coach} leads. Pause or step on your own any time: only your view leaves the group.', {coach: st.coach});
  const c = $('#wtConn');
  c.className = 'pill ' + (WT.snap.connected ? 'good' : 'warn');
  c.textContent = WT.snap.connected ? t('Live') : t('Reconnecting');
  c.title = tx(WT.snap.error) || (WT.snap.rtt != null ? t('Hub round trip {ms} ms', {ms: Math.round(WT.snap.rtt * 1000)}) : '');
  $('#wtEnd').hidden = !isCoach();
  $('#wtLeave').textContent = isCoach() ? t('Close') : t('Leave');
  buildPlayers(st);
  if (!WT.loop) WT.loop = setInterval(syncTick, 250);       // (re)built players: keep them on the moment
  drawPlan(st);
  drawOwn(st);
  drawControls(st);
  drawMembers(st);
  drawLists(st);
  drawPointer();
  layout();
  // the large view's player on the map (minimap.js): a click there moves the coach's moment, or a player's own view
  if (window.wtMapSetup) wtMapSetup($('#wtMap'), st, t => isCoach() ? control('seek', {t}) : ownSet(t, viewPlaying(WT.snap.state)));
}

function povKey(st) { return st.gameKey + '|' + st.povs.map(p => p.player + ':' + (p.video || '')).join(','); }
function buildPlayers(st) {
  const key = povKey(st);
  if (key === WT.ytKey) return;
  stopPlayers();
  WT.ytKey = key;
  WT.local = null;                    // another game: everyone back on the coach's moment
  const stage = $('#wtStage');
  st.povs.forEach((p, i) => {
    const d = document.createElement('div');
    d.className = 'wt-pov'; d.dataset.p = p.player;
    d.innerHTML = `<div class="wt-yt" id="wtyt${i}"></div><div class="wt-shield"></div>
      <div class="wt-name">${esc(p.player)}</div><div class="wt-msg" hidden></div>`;
    stage.insertBefore(d, $('#wtInk'));
    d.querySelector('.wt-shield').onclick = () => { if (isCoach() && WT.snap.state.focus !== p.player) control('focus', {player: p.player}); };
    const msg = d.querySelector('.wt-msg');
    if (!p.video) { msg.hidden = false; msg.textContent = t('No video for {player}', {player: p.player}); return; }
    if (p.privacy === 'private') { msg.hidden = false; msg.textContent = t('{player}\'s video is private on YouTube', {player: p.player}); }
    loadYT().then(() => {
      if (WT.ytKey !== key) return;
      const t0 = Math.max(0, Math.floor(p.videoAt + gameTime(WT.snap.state)));
      const y = new YT.Player('wtyt' + i, {
        videoId: p.video, width: '100%', height: '100%',
        playerVars: {mute: 1, controls: 0, disablekb: 1, fs: 0, rel: 0, iv_load_policy: 3, playsinline: 1, modestbranding: 1, start: t0, origin: location.origin},
        events: {
          onReady: () => { WT.yt[p.player] = {y, ready: true, pov: p, seekAt: 0, lead: 0.25}; y.mute(); syncTick(); },
          onError: e => { msg.hidden = false; msg.textContent = ytError(p.player, e.data); delete WT.yt[p.player]; },
        }});
      WT.yt[p.player] = {y, ready: false, pov: p, seekAt: 0, lead: 0.25};
    }).catch(() => { msg.hidden = false; msg.textContent = t('YouTube can\'t be reached'); });
  });
}
function ytError(name, code) {
  if (code === 100) return t('{name}\'s video is private or removed', {name});
  if (code === 101 || code === 150 || code === 153) return t('{name}\'s video can\'t be played inside the app (embedding off)', {name});
  return t('{name}\'s video can\'t be played (YouTube error {code})', {name, code});
}
function stopPlayers() {
  clearInterval(WT.loop); WT.loop = null;
  Object.values(WT.yt).forEach(x => { try { x.y.destroy(); } catch (e) {} });
  WT.yt = {}; WT.ytKey = '';
  $$('.wt-pov').forEach(d => d.remove());
}
function loadYT() {
  if (window.YT && window.YT.Player) return Promise.resolve();
  if (!WT.ytLoad) WT.ytLoad = new Promise((ok, bad) => {
    const prev = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => { if (prev) prev(); ok(); };
    const s = document.createElement('script');
    s.src = 'https://www.youtube.com/iframe_api';
    s.onerror = () => { WT.ytLoad = null; bad(new Error(t('YouTube not reachable'))); };
    document.head.appendChild(s);
  });
  return WT.ytLoad;
}

/* the focused view large, the others small beside it (below it on a narrow window); iframes never move in the
   page (moving one reloads it): only their boxes change */
function layout() {
  const st = WT.snap && WT.snap.state, stage = $('#wtStage'); if (!st || !stage) return;
  const W = stage.clientWidth, povs = st.povs, others = povs.filter(p => p.player !== st.focus);
  const side = others.length && W >= 1000, gap = 10;
  let bigW = others.length ? (side ? Math.round(W * 0.76) : W) : W;
  let bigH = Math.round(bigW * 9 / 16);
  const maxH = Math.max(240, window.innerHeight - 300);
  if (bigH > maxH) { bigH = maxH; bigW = Math.round(bigH * 16 / 9); }
  const boxes = {};
  boxes[st.focus] = {x: 0, y: 0, w: bigW, h: bigH};
  let H = bigH;
  if (others.length) {
    if (side) {
      const sw = W - bigW - gap, sh = Math.round(sw * 9 / 16);
      others.forEach((p, i) => boxes[p.player] = {x: bigW + gap, y: i * (sh + gap), w: sw, h: sh});
      H = Math.max(bigH, others.length * (sh + gap) - gap);
    } else {
      const n = Math.max(2, others.length), sw = Math.floor((W - gap * (n - 1)) / n), sh = Math.round(sw * 9 / 16);
      others.forEach((p, i) => boxes[p.player] = {x: i * (sw + gap), y: bigH + gap, w: sw, h: sh});
      H = bigH + gap + sh;
    }
  }
  stage.style.height = H + 'px';
  $$('.wt-pov', stage).forEach(d => {
    const b = boxes[d.dataset.p]; if (!b) { d.hidden = true; return; }
    d.hidden = false;
    Object.assign(d.style, {left: b.x + 'px', top: b.y + 'px', width: b.w + 'px', height: b.h + 'px'});
    d.classList.toggle('big', d.dataset.p === st.focus);
    d.classList.toggle('pick', isCoach() && d.dataset.p !== st.focus);
  });
  const ink = $('#wtInk'), b = boxes[st.focus], dpr = window.devicePixelRatio || 1;
  WT.bigBox = b;
  Object.assign(ink.style, {left: b.x + 'px', top: b.y + 'px', width: b.w + 'px', height: b.h + 'px'});
  ink.width = Math.round(b.w * dpr); ink.height = Math.round(b.h * dpr);
  ink.classList.toggle('coach', isCoach());
  paint(); drawPointer();
}

/* ------------------------------------------------------------------ session plan: the coach's moments, in order */
const planItems = st => st && st.plan && Array.isArray(st.plan.items) ? st.plan.items : [];
const planAt = st => planItems(st).length && Number.isInteger(st.plan.at) ? st.plan.at : -1;
function planStep(d) {
  const st = WT.snap && WT.snap.state, n = planItems(st).length; if (!n) return;
  const at = planAt(st), i = Math.min(n - 1, Math.max(0, at + d));
  if (i !== at) control('item', {index: i});
}
function planLabel(it) {
  return `${esc(it.label || t('Game'))} · <span class="mono">${fmtG(Number(it.t) || 0)}</span>${it.player ? ' · ' + t('{player}\'s view', {player: esc(it.player)}) : ''}`;
}
/* the bar above the views: the moment everyone is on; Previous / Next for the coach */
function drawPlan(st) {
  const box = $('#wtPlan'); if (!box) return;
  const items = planItems(st), at = planAt(st), coach = isCoach();
  const plans = coach && WT.list && WT.list.plans ? WT.list.plans.filter(p => p.moments) : [];
  const sig = JSON.stringify([coach, st.plan && st.plan.id, at, items.length, plans.length]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  if (!items.length) {
    box.hidden = !plans.length;
    box.innerHTML = plans.length ? `<span class="faint">${t('No session plan')}</span><select id="wtPlanPick" title="${t('Use a session plan')}"><option value="">${t('Use a plan…')}</option>${plans.map(p => `<option value="${esc(p.id)}">${esc(p.title)} (${p.moments})</option>`).join('')}</select>` : '';
    const pick = $('#wtPlanPick'); if (pick) pick.onchange = () => { if (pick.value) control('plan', {plan: pick.value}); };
    return;
  }
  box.hidden = false;
  const it = items[at];
  box.innerHTML = `<div class="wt-plan-now"><span class="pill ${at >= 0 ? 'info' : ''}">${at >= 0 ? t('Moment {n} of {total}', {n: at + 1, total: items.length}) : t('Plan · {n} moments', {n: items.length})}</span>
      ${it ? `<b>${esc(it.note || t('No note'))}</b><span class="faint">${planLabel(it)}</span>` : `<span class="faint">${esc(st.plan.title || t('Session plan'))}${coach ? ': ' + t('Next goes to the first moment') : ''}</span>`}</div>
    ${coach ? `<div class="row wt-plan-nav"><button class="btn small" id="wtPrev" title="P" ${at <= 0 ? 'disabled' : ''}>${t('Previous')}</button>
      <button class="btn small brand" id="wtNext" title="N" ${at >= items.length - 1 ? 'disabled' : ''}>${at < 0 ? t('First moment') : t('Next')}</button></div>` : ''}`;
  if (coach) { $('#wtPrev').onclick = () => planStep(-1); $('#wtNext').onclick = () => planStep(1); }
}

/* "Paused by you · Back to coach" */
function drawOwn(st) {
  const box = $('#wtOwn'); if (!box) return;
  const l = WT.local;
  box.hidden = !l || isCoach();
  if (box.hidden) return;
  box.innerHTML = `<span><b>${l.playing ? t('Watching on your own') : t('Paused by you')}</b> · ${t('the group goes on with the coach')}</span>
    <button class="btn small brand" id="wtBack">${t('Back to coach')}</button><span class="faint">${t('Esc')}</span>`;
  $('#wtBack').onclick = backToCoach;
}

function drawControls(st) {
  const box = $('#wtControls'); if (!box) return;
  const coach = isCoach(), focus = st.povs.find(p => p.player === st.focus), playing = viewPlaying(st);
  const games = coach && WT.list && WT.list.games ? WT.list.games : [];
  const sig = JSON.stringify([coach, st.playing, playing, !!WT.local, st.speed, st.range, st.gameKey, games.length, WT.tool, WT.color, WT.muted, WT.blocked, WT.vol, st.focus, planItems(st).length]);
  if (box.dataset.sig === sig) return updateTime();
  box.dataset.sig = sig;
  const steps = `<button class="btn small ghost" data-step="-10">−10 s</button><button class="btn small ghost" data-step="-5">−5 s</button>
    <button class="btn small ghost" data-frame="-1" title="${t('One frame back (,)')}" ${playing ? 'disabled' : ''}>◂ ${t('1 frame')}</button>
    <button class="btn small ghost" data-frame="1" title="${t('One frame forward (.)')}" ${playing ? 'disabled' : ''}>${t('1 frame')} ▸</button>
    <button class="btn small ghost" data-step="5">+5 s</button><button class="btn small ghost" data-step="10">+10 s</button>`;
  box.innerHTML = `<div class="row wt-transport">
      <button class="btn small brand wt-play" id="wtPlay" title="${t('Space')}">${playing ? t('Pause') : t('Play')}</button>
      ${!coach && !WT.local ? `<span class="pill ${st.playing ? 'good' : ''}">${st.playing ? t('Coach: playing') : t('Coach: paused')}</span>` : ''}
      <span class="mono wt-time" id="wtTime"></span>
      <input type="range" class="wt-seek" id="wtSeek" min="${st.range[0]}" max="${st.range[1]}" step="0.5">
      <span class="mono faint" id="wtEndT">${fmtG(st.range[1])}</span>
      ${coach ? `<select id="wtSpeed" title="${t('Speed')}">${[0.25, 0.5, 0.75, 1, 1.25, 1.5, 2].map(x => `<option value="${x}" ${x === st.speed ? 'selected' : ''}>${x}×</option>`).join('')}</select>` : (st.speed !== 1 ? `<span class="pill">${st.speed}×</span>` : '')}
    </div>
    <div class="row wt-steps">${steps}</div>
    <div class="row wt-tools">
      ${coach ? `<div class="seg" id="wtTool">${TOOLS.map(([k, l]) => `<button data-k="${k}" class="${WT.tool === k ? 'on' : ''}">${l}</button>`).join('')}</div>
        <div class="swatches" id="wtColor">${INK.map(c => `<button data-c="${c}" class="${WT.color === c ? 'on' : ''}" style="background:${c}" aria-label="${t('Colour {c}', {c})}"></button>`).join('')}</div>
        <button class="btn small" id="wtUndo">${t('Undo')}</button><button class="btn small" id="wtClear">${t('Clear')}</button>
        <button class="btn small" id="wtMark" title="B">${t('Bookmark')}</button>` :
        `<span class="faint">${t('Large:')} <b>${esc(st.focus)}</b>${focus && !focus.video ? ' (' + t('no video') + ')' : ''}</span>
         <button class="btn small" id="wtFlag" title="F">${t('Look at this')}</button>`}
      <span style="flex:1"></span>
      <button class="btn small ${WT.blocked ? 'brand' : 'ghost'}" id="wtMute">${WT.muted || WT.blocked ? t('Sound on') : t('Mute')}</button>
      <input type="range" id="wtVol" min="0" max="100" value="${WT.vol}" title="${t('Volume')}" class="wt-vol">
      ${coach && games.length ? `<select id="wtGame" title="${t('Change game')}"><option value="">${t('Change game…')}</option>${games.map(g => `<option value="${esc(g.gameKey)}" ${g.gameKey === st.gameKey ? 'disabled' : ''}>${esc(gameTitle(g))}</option>`).join('')}</select>` : ''}
    </div>
    <small class="faint">${coach ? t('Click a small view to make it large. Draw on the large view: everyone sees it; drawings are kept as a coach note when play goes on. Your mouse shows as a dot for the others.') + ' ' +
        (planItems(st).length ? t('Space = play / pause, arrows = 5 s (Shift: 10 s), , and . = one frame, B = bookmark, N / P = next / previous moment.') : t('Space = play / pause, arrows = 5 s (Shift: 10 s), , and . = one frame, B = bookmark.'))
      : t('Space = pause / play your view, arrows = 5 s (Shift: 10 s), , and . = one frame, F = look at this, Esc = back to the coach.')}</small>`;
  const seek = $('#wtSeek');
  $('#wtPlay').onclick = () => coach ? control(WT.snap.state.playing ? 'pause' : 'play') : ownToggle();
  $$('[data-step]', box).forEach(b => b.onclick = () => step(+b.dataset.step));
  $$('[data-frame]', box).forEach(b => b.onclick = () => frame(+b.dataset.frame));
  seek.oninput = () => { WT.seeking = true; $('#wtTime').textContent = fmtG(+seek.value); };
  seek.onchange = () => { WT.seeking = false; coach ? control('seek', {t: +seek.value}) : ownSet(+seek.value, false); };
  if (coach) {
    $('#wtSpeed').onchange = e => control('speed', {speed: +e.target.value});
    $$('#wtTool button').forEach(b => b.onclick = () => { WT.tool = b.dataset.k; drawControls(WT.snap.state); });
    $$('#wtColor button').forEach(b => b.onclick = () => { WT.color = b.dataset.c; drawControls(WT.snap.state); });
    $('#wtUndo').onclick = () => draw({op: 'undo'});
    $('#wtClear').onclick = () => draw({op: 'clear'});
    $('#wtMark').onclick = addBookmark;
    const g = $('#wtGame'); if (g) g.onchange = () => { if (g.value) control('game', {gameKey: g.value}); };
  } else {
    $('#wtFlag').onclick = sendFlag;
  }
  $('#wtMute').onclick = () => { if (WT.blocked) { WT.blocked = false; WT.muted = false; } else WT.muted = !WT.muted; saveSound(); drawControls(WT.snap.state); syncTick(); };
  $('#wtVol').oninput = e => { WT.vol = +e.target.value; WT.muted = false; saveSound(); syncTick(); };
  updateTime();
}
function updateTime() {
  const st = WT.snap && WT.snap.state, el = $('#wtTime'), seek = $('#wtSeek'); if (!st || !el) return;
  const t = viewTime(st);
  if (!WT.seeking) { el.textContent = viewPlaying(st) ? fmtG(t) : fmtF(t); seek.value = t; }
}

function drawMembers(st) {
  const box = $('#wtMembers'); if (!box) return;
  const now = hubNow();
  box.innerHTML = Object.entries(st.members || {}).sort((a, b) => (a[1].role === 'coach' ? -1 : 0) - (b[1].role === 'coach' ? -1 : 0) || a[0].localeCompare(b[0])).map(([name, m]) => {
    let cls = 'good', txt = t('in sync');
    if (!m.connected) { cls = ''; txt = 'away'; }
    else if (m.browsing) { cls = 'info'; txt = 'browsing'; }
    else if (m.buffering) { cls = 'warn'; txt = 'loading'; }
    else if (m.t != null && m.seen) {
      const d = gameTime(st, m.seen) - m.t;
      if (now - m.seen > 6) { cls = ''; txt = t('no news'); }
      else if (Math.abs(d) > 0.75) { cls = 'warn'; txt = `${Math.abs(d).toFixed(1)} s ${d > 0 ? 'behind' : 'ahead'}`; }
    } else if (m.t == null) { cls = ''; txt = 'joining'; }
    return `<span class="wt-member"><b>${esc(name)}</b>${m.role === 'coach' ? ' <span class="faint">coach</span>' : ''} <span class="pill ${cls}">${esc(name === (WT.snap.me || {}).name && m.browsing ? 'browsing' : txt)}</span></span>`;
  }).join('');
}

/* flags ("Look at this") for the coach, bookmarks for everyone */
function drawLists(st) {
  const box = $('#wtLists'); if (!box) return;
  const coach = isCoach();
  const flags = (st.flags || []).filter(f => f.gameKey === st.gameKey || coach);
  const items = planItems(st), at = planAt(st), covered = (st.plan && st.plan.covered) || {};
  const sig = JSON.stringify([coach, flags, st.bookmarks, st.plan && st.plan.id, at, Object.keys(covered)]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  const pl = items.length ? `<div class="wt-list wt-planlist"><h3>${esc(st.plan.title || t('Session plan'))}</h3>${items.map((it, i) => `<div class="wt-item ${i === at ? 'now' : ''} ${covered[it.id] && i !== at ? 'wt-seen' : ''}">
      <span class="wt-pn">${i + 1}</span><span class="wt-pt">${esc(it.note || t('No note'))}<small class="faint">${planLabel(it)}</small></span>
      ${i === at ? '<span class="pill info">' + t('Now') + '</span>' : coach ? `<button class="btn small ghost" data-item="${i}">${t('Go')}</button>` : ''}</div>`).join('')}</div>` : '';
  const fl = coach && flags.length ? `<div class="wt-list"><h3>${t('Look at this')}</h3>${flags.slice().reverse().map(f => `<div class="wt-item ${f.seen ? 'wt-seen' : ''}">
      <b>${esc(f.player)}</b> <span class="mono">${fmtF(f.t)}</span>
      <button class="btn small ${f.seen ? 'ghost' : 'brand'}" data-go="${esc(f.id)}">${t('Go')}</button><button class="btn small ghost" data-drop="${esc(f.id)}" aria-label="${t('Remove')}">×</button></div>`).join('')}</div>` : '';
  const bm = (st.bookmarks || []).length ? `<div class="wt-list"><h3>${t('Bookmarks')}</h3>${st.bookmarks.map(b => `<div class="wt-item">
      <span class="mono">${fmtF(b.t)}</span> ${esc(b.text || t('(no note)'))}${coach ? ` <button class="btn small ghost" data-seek="${b.t}">${t('Go')}</button>` : ''}</div>`).join('')}</div>` : '';
  box.innerHTML = pl + fl + bm;
  $$('[data-item]', box).forEach(b => b.onclick = () => control('item', {index: +b.dataset.item}));
  $$('[data-go]', box).forEach(b => b.onclick = () => control('flag', {id: b.dataset.go}));
  $$('[data-drop]', box).forEach(b => b.onclick = () => control('unflag', {id: b.dataset.drop}));
  $$('[data-seek]', box).forEach(b => b.onclick = () => control('seek', {t: +b.dataset.seek}));
}

/* ------------------------------------------------------------------ keeping the YouTube players on the moment */
function syncTick() {
  const st = WT.snap && WT.snap.state; if (!st || page !== 'watch' || document.hidden) return;   // hidden: idle
  const T = viewTime(st), playing = viewPlaying(st), now = performance.now() / 1000;
  updateTime();
  if (window.wtMapTick) wtMapTick(T);                       // this tick is 4 per second and stops while hidden
  for (const [name, x] of Object.entries(WT.yt)) {
    if (!x.ready) continue;
    const y = x.y, p = x.pov, big = name === st.focus;
    let ys, cur;
    try { ys = y.getPlayerState(); cur = y.getCurrentTime(); } catch (e) { continue; }
    const dur = y.getDuration() || Infinity, vt = p.videoAt + T;
    const msg = $(`.wt-pov[data-p="${CSS.escape(name)}"] .wt-msg`);
    if (big && !WT.muted && !WT.blocked) { if (y.isMuted()) y.unMute(); if (y.getVolume() !== WT.vol) y.setVolume(WT.vol); }
    else if (!y.isMuted()) y.mute();
    if (y.getPlaybackRate() !== st.speed) y.setPlaybackRate(st.speed);
    if (vt < 0 || vt > dur - 0.3) {                        // this player's video doesn't cover this moment
      if (ys === YT_PLAYING || ys === YT_BUFFERING) y.pauseVideo();
      if (msg) { msg.hidden = false; msg.textContent = vt < 0 ? t('{name}\'s video starts later', {name}) : t('{name}\'s video has ended', {name}); }
      continue;
    }
    if (msg && /starts later|has ended/.test(msg.textContent)) msg.hidden = true;
    const drift = cur - vt;
    x.buffering = ys === YT_BUFFERING;
    if (ys === YT_PLAYING || ys === YT_BUFFERING || !playing) x.playAsk = 0;
    if (ys === YT_BUFFERING) x.bufSince = x.bufSince || now; else x.bufSince = 0;
    if (playing) {
      if (ys === YT_BUFFERING) {                            // waiting for the video: the others go on
        if (now - x.bufSince > 5 && Math.abs(drift) > 2) {  // stuck (YouTube sometimes never resumes): ask again
          y.seekTo(vt + x.lead * st.speed, true); y.playVideo(); x.seekAt = now; x.bufSince = now;
        }
        continue;
      }
      if (ys !== YT_PLAYING) {
        if (Math.abs(drift) > DRIFT_S) { y.seekTo(vt + x.lead * st.speed, true); x.seekAt = now; }
        y.playVideo();
        // the browser may refuse to start a video with sound before a click on the page: go on muted, offer sound
        if (!x.playAsk) x.playAsk = now;
        else if (now - x.playAsk > 3 && !y.isMuted()) { WT.blocked = true; y.mute(); drawControls(st); }
      } else if (Math.abs(drift) > DRIFT_S && now - x.seekAt > 2) {
        // after a seek the video starts late: learn how much (per player) and aim that far ahead next time
        if (x.seekAt && now - x.seekAt < 6) x.lead = Math.min(3, Math.max(0, x.lead - drift));
        y.seekTo(vt + x.lead * st.speed, true); x.seekAt = now;
      }
    } else {
      if (ys === YT_PLAYING || ys === YT_BUFFERING) y.pauseVideo();
      // paused: the exact picture (frame steps are 1/60 s, so the paused tolerance is below one frame)
      if (Math.abs(drift) > (WT.exact ? 0.008 : PAUSED_DRIFT_S) && now - x.seekAt > 0.15) { y.seekTo(vt, true); x.seekAt = now; }
    }
  }
  // where this app's large view is: the coach sees who is behind or browsing
  const f = WT.yt[st.focus];
  if (f && f.ready && Date.now() - WT.lastReport > 1000) {
    WT.lastReport = Date.now();
    try { api('/api/watch/report', {t: f.y.getCurrentTime() - f.pov.videoAt, buffering: f.y.getPlayerState() === YT_BUFFERING, browsing: !!WT.local}).catch(() => {}); } catch (e) {}
  }
  if (Date.now() - (WT.memAt || 0) > 1000) { WT.memAt = Date.now(); drawMembers(st); }
}

/* ------------------------------------------------------------------ stepping: the coach moves everyone, a player only their own view */
function ownSet(t, playing) {
  const st = WT.snap.state;
  WT.local = {t: clampT(st, t), wall: performance.now() / 1000, playing};
  WT.exact = !playing;
  drawAll(); syncTick();
}
function ownToggle() {
  const st = WT.snap.state, t = viewTime(st);
  ownSet(t, !viewPlaying(st));
}
function backToCoach() {
  WT.local = null; WT.exact = false;
  drawAll(); syncTick();
  api('/api/watch/report', {t: gameTime(WT.snap.state), browsing: false}).catch(() => {});
}
function step(s) {
  const st = WT.snap.state;
  if (isCoach()) return control('seek', {t: gameTime(st) + s});
  ownSet(viewTime(st) + s, viewPlaying(st));
}
function frame(n) {
  const st = WT.snap.state;
  if (viewPlaying(st)) return;                      // frames: while paused
  if (isCoach()) { WT.exact = true; return control('seek', {t: Math.round((gameTime(st) + n * FRAME_S) * 1000) / 1000}); }
  ownSet(viewTime(st) + n * FRAME_S, false);
}

/* ------------------------------------------------------------------ coach actions */
async function control(action, extra) {
  const st = WT.snap && WT.snap.state; if (!st) return;
  const body = Object.assign({action}, extra || {});
  if ((action === 'play' || action === 'pause') && body.t == null) {
    const f = WT.yt[st.focus];                       // the coach's own picture doesn't jump
    try { body.t = f && f.ready && action === 'pause' ? f.y.getCurrentTime() - f.pov.videoAt : gameTime(st); } catch (e) { body.t = gameTime(st); }
  }
  if (action === 'play') WT.exact = false;
  if (action === 'seek' && !st.playing && extra && extra.t != null) WT.exact = Math.abs(extra.t - gameTime(st)) < 0.05;
  try { applySnap(await api('/api/watch/control', body)); }
  catch (e) { toast(e.message, true); }
}
async function draw(body) {
  if (body.op === 'undo' || body.op === 'clear') { WT.done = null; paint(); }
  try { await api('/api/watch/draw', body); } catch (e) { toast(e.message, true); }
}
async function addBookmark() {
  const st = WT.snap.state, at = gameTime(st);
  const text = window.prompt(t('Bookmark at {time}. Note (optional, one line):', {time: fmtF(at)}), '');
  if (text === null) return;
  try { await api('/api/watch/bookmark', {text: text.trim().slice(0, 200), t: at}); toast(t('Bookmarked: the players find it in their review.')); }
  catch (e) { toast(e.message, true); }
}
async function sendFlag() {
  const st = WT.snap.state;
  try { await api('/api/watch/flag', {t: Math.round(viewTime(st) * 1000) / 1000}); toast(t('Sent to the coach: "Look at this" at {time}', {time: fmtF(viewTime(st))})); }
  catch (e) { toast(e.message, true); }
}

/* the coach draws on the large view; a stroke is sent while it is drawn (every 100 ms) and once more at the end.
   His mouse over the large view shows as a dot for the others (not kept, hidden after 2 s without moving). */
function setupInkInput() {
  const c = $('#wtInk');
  const pt = e => { const r = c.getBoundingClientRect(), f = v => Math.round(Math.max(0, Math.min(1, v)) * 10000) / 10000;
    return [f((e.clientX - r.left) / r.width), f((e.clientY - r.top) / r.height)]; };
  c.addEventListener('pointerdown', e => {
    if (!isCoach()) return;
    c.setPointerCapture(e.pointerId);
    const p = pt(e);
    WT.cur = {id: 'k' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6), kind: WT.tool, color: WT.color,
              pts: WT.tool === 'pen' ? [p] : [p, p], sent: false};
    paint();
  });
  c.addEventListener('pointermove', e => {
    if (!isCoach()) return;
    const s = WT.cur, p = pt(e);
    sendPointer(p);
    if (!s) return;
    if (s.kind === 'pen') { const q = s.pts[s.pts.length - 1]; if (Math.hypot(p[0] - q[0], p[1] - q[1]) > 0.002 && s.pts.length < 2000) s.pts.push(p); }
    else s.pts[1] = p;
    paint(); sendCur(false);
  });
  c.addEventListener('pointerleave', () => { if (isCoach()) hidePointer(); });
  const end = () => {
    const s = WT.cur; if (!s) return;
    const [a, b] = [s.pts[0], s.pts[s.pts.length - 1]];
    const ok = s.kind === 'pen' ? s.pts.length > 1 : Math.hypot(b[0] - a[0], b[1] - a[1]) > 0.01;
    if (ok) sendCur(true);
    else if (s.sent) draw({op: 'delete', id: s.id});
    WT.done = ok ? s : null; WT.doneAt = Date.now(); WT.cur = null;     // shown until the hub's copy arrives
    paint();
  };
  c.addEventListener('pointerup', end); c.addEventListener('pointercancel', end);
}
function sendPointer(p) {
  clearTimeout(WT.ptrTimer);
  WT.ptrTimer = setTimeout(hidePointer, POINTER_IDLE_MS);
  if (Date.now() - WT.ptrSent < 120) return;        // about 8 a second
  WT.ptrSent = Date.now(); WT.ptrShown = true;
  api('/api/watch/pointer', {x: p[0], y: p[1]}).catch(() => {});
}
function hidePointer() {
  clearTimeout(WT.ptrTimer);
  if (!WT.ptrShown) return;
  WT.ptrShown = false;
  api('/api/watch/pointer', {hide: true}).catch(() => {});
}
function drawPointer() {
  const d = $('#wtDot'), p = WT.snap && WT.snap.pointer, b = WT.bigBox; if (!d) return;
  clearTimeout(WT.dotTimer);
  const age = p && p.at ? Date.now() / 1000 - p.at : 99;
  if (!p || p.hide || !b || isCoach() || WT.local || age > POINTER_IDLE_MS / 1000) { d.hidden = true; return; }
  d.hidden = false;
  d.style.left = (b.x + p.x * b.w) + 'px'; d.style.top = (b.y + p.y * b.h) + 'px';
  WT.dotTimer = setTimeout(() => { d.hidden = true; }, POINTER_IDLE_MS - age * 1000);
}
function sendCur(final) {
  const s = WT.cur; if (!s) return;
  const go = () => {
    WT.sendTimer = null; WT.sendAt = Date.now();
    const pts = s.kind === 'pen' ? s.pts : [s.pts[0], s.pts[s.pts.length - 1]];
    if (pts.length < (s.kind === 'pen' ? 1 : 2)) return;
    s.sent = true;
    draw({op: 'put', stroke: {id: s.id, kind: s.kind, color: s.color, pts}});
  };
  clearTimeout(WT.sendTimer);
  if (final || Date.now() - WT.sendAt > 100) go(); else WT.sendTimer = setTimeout(go, 100 - (Date.now() - WT.sendAt));
}
/* drawings: solid while on screen; when play resumes (or the moment moves) they fade out over FADE_MS */
function startFade(shapes) {
  const f = WT.fade = {shapes, start: performance.now()};
  const tick = () => { if (WT.fade !== f) return; paint(); if (performance.now() - f.start < FADE_MS) requestAnimationFrame(tick); else { WT.fade = null; paint(); } };
  requestAnimationFrame(tick);
  // a hidden window gets no animation frames: the fade still ends on time
  setTimeout(() => { if (WT.fade === f) { WT.fade = null; paint(); } }, FADE_MS + 30);
}
function paint() {
  const c = $('#wtInk'), st = WT.snap && WT.snap.state; if (!c || !st) return;
  const g = c.getContext('2d'), strokes = WT.local ? [] : (st.strokes || []);     // browsing: the coach's moment isn't on screen
  g.clearRect(0, 0, c.width, c.height);
  if (WT.fade) {
    const k = 1 - (performance.now() - WT.fade.start) / FADE_MS;
    if (k > 0) { g.save(); g.globalAlpha = k; drawShapes(g, c.width, c.height, WT.fade.shapes); g.restore(); }
  }
  // the coach's last stroke shows at once, until the hub's copy is in the state (or 3 s, if it was refused)
  if (WT.done && (strokes.some(x => x.id === WT.done.id) || Date.now() - WT.doneAt > 3000)) WT.done = null;
  const local = [WT.done, WT.cur].filter(Boolean), ids = new Set(local.map(s => s.id));
  drawShapes(g, c.width, c.height, strokes.filter(s => !ids.has(s.id)).concat(local));
}

/* ------------------------------------------------------------------ review talk (coach's app only, with consent) */
async function loadTalk() {
  const box = $('#wtTalk'); if (!box || !isCoach()) { if (box) box.innerHTML = ''; return; }
  try { WT.talk = await api('/api/watch/talk'); } catch (e) { WT.talk = null; }
  drawTalk();
}
function drawTalk() {
  const box = $('#wtTalk'), tk = WT.talk; if (!box || !isCoach()) return;
  if (!tk) { box.innerHTML = ''; return; }
  const on = tk.active;
  box.innerHTML = `<section class="card section wt-talk"><h2>${t('Review talk')}</h2>
    <p class="muted">${t('Records your microphone and your voice app while this session runs, on this PC only, and sends it to the team hub when you stop. Your players can then play it over their own recording of this game. The voice app records everyone in your call: only with your team\'s agreement to record comms (Settings → Recording).')}</p>
    <div class="row">${on ? `<span class="pill bad">${t('● Recording talk')}</span><button class="btn small" id="wtTalkBtn">${t('Stop and send')}</button>`
      : `<button class="btn small brand" id="wtTalkBtn" ${tk.why ? 'disabled' : ''}>${t('Record talk')}</button>`}
      <span class="faint">${esc(tx(tk.why) || (tk.tracks && tk.tracks.length ? t('Tracks: {list}', {list: tk.tracks.map(x => x === 'mic' ? t('your mic') : t('your voice app')).join(' + ')}) : ''))}${tk.pending ? ' · ' + t('{pending} waiting for the hub', {pending: tk.pending}) : ''}</span></div>
    ${tk.status && !on ? `<small class="faint">${esc(tx(tk.status))}</small>` : ''}</section>`;
  const b = $('#wtTalkBtn');
  if (b) b.onclick = async () => {
    b.disabled = true;
    try { WT.talk = await api('/api/watch/talk', {on: !on}); toast(on ? t('Review talk saved: it goes to the hub now.') : t('Recording your review talk.')); }
    catch (e) { toast(e.message, true); }
    drawTalk();
  };
}

/* ------------------------------------------------------------------ leaving */
async function leaveSession() {
  if (isCoach()) { location.hash = '#team'; return; }        // the coach closes the page; the session goes on
  try { applySnap(await api('/api/watch/leave', {})); } catch (e) { toast(e.message, true); }
  stopPlayers(); location.hash = '#team';
}
async function endSession() {
  if (!confirmBox(t('End the session for everyone?') + (WT.talk && WT.talk.active ? ' ' + t('Your review talk is saved and sent.') : ''))) return;
  try { applySnap(await api('/api/watch/end', {})); } catch (e) { toast(e.message, true); }
  stopPlayers(); location.hash = '#team';
}

// app.js routed before this file loaded: draw the session page now
if (page === 'watch') renderWatch();
