/* PCS Scanner interface. Talks only to the app's own local server (token in the address). */
'use strict';

const TOKEN = (() => {
  const t = new URLSearchParams(location.search).get('t');
  try { if (t) sessionStorage.setItem('pcs-t', t); return t || sessionStorage.getItem('pcs-t') || ''; }
  catch (e) { return t || ''; }
})();
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const MAPS = {WE: "World's Edge", SP: 'Storm Point', ED: 'E-District', OL: 'Olympus', BM: 'Broken Moon', KC: 'Kings Canyon'};
const mapName = m => MAPS[m] || m || '?';
const poiName = p => (p || '').replace(/^[a-z]{2}-/, '').split('-').map(w => w ? w[0].toUpperCase() + w.slice(1) : w).join(' ');

async function api(path, body) {
  const r = await fetch(path, {method: body === undefined ? 'GET' : 'POST', headers: {'X-PCS-Token': TOKEN, 'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
}

function fmtT(s) {
  if (s == null || isNaN(s)) return '–';
  s = Math.max(0, s); const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = Math.floor(s % 60);
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0');
}
const fmtGB = b => b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : Math.max(1, Math.round(b / 1e6)) + ' MB';
function fmtDate(sec) {
  const d = new Date(sec * 1000), now = new Date();
  const day = d.toDateString() === now.toDateString() ? 'Today' :
    (new Date(now - 864e5).toDateString() === d.toDateString() ? 'Yesterday' : d.toLocaleDateString(undefined, {day: 'numeric', month: 'short'}));
  return day + ' ' + d.toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'});
}
function toast(text, bad) {
  const t = $('#toast'); t.textContent = text; t.className = 'toast' + (bad ? ' bad' : ''); t.hidden = false;
  clearTimeout(toast.h); toast.h = setTimeout(() => t.hidden = true, 3500);
}
const STATUS = {
  'scanned': ['Scanned', 'good'], 'uploaded': ['Sent', 'good'], 'not scanned': ['Not scanned', ''], 'recorded': ['Not scanned', ''],
  'skipped': ['Skipped', ''], 'waiting': ['Waiting', 'info'], 'scanning': ['Scanning', 'info'], 'failed': ['Failed', 'bad'], 'recording': ['Recording', 'bad'],
};
const statusPill = s => { const [t, c] = STATUS[s] || [s, '']; return `<span class="pill ${c}">${esc(t)}</span>`; };

let S = null, LIB = [], page = '', pageArg = '';

/* ------------------------------------------------------------------ top bar + polling */
function renderTop() {
  const pill = $('#pill'), r = S && S.recorder;
  let cls = 'live', text = 'Auto-record off';
  if (!S) text = 'Connecting…';
  else if (!r) text = 'Scanner';
  else if (r.recording) { cls += ' rec'; text = 'Recording'; }
  else if (r.scanning) { cls += ' on'; text = 'Scanning'; }
  else if (r.enabled) { cls += ' on'; text = 'Ready · waiting for Apex'; }
  if (S && S.scans.now) { cls = 'live on'; text = 'Scanning'; }
  pill.className = cls; pill.querySelector('span').textContent = text;
  const u = $('#updateBtn'); u.hidden = !(S && S.update); if (S && S.update) u.textContent = 'Update to ' + S.update.tag;
  $$('#nav a').forEach(a => a.classList.toggle('on', a.dataset.page === (page === 'review' ? 'recordings' : page)));
}
// the window was opened again (from the tray): check for an update if the last check is old
document.addEventListener('visibilitychange', () => { if (!document.hidden) api('/api/update/check', {}).catch(() => {}); });
$('#updateBtn').onclick = async () => {
  if (!confirmBox('Download the new version now? The app restarts by itself.')) return;
  toast('Downloading the update…'); await api('/api/update', {}).catch(e => toast(e.message, true));
};
function confirmBox(msg) { return window.confirm(msg); }

async function poll() {
  try {
    const prev = S; S = await api('/api/state');
    renderTop();
    if (page === 'home') renderHome(prev);
    if (page === 'recordings' && (!prev || JSON.stringify(prev.scans) !== JSON.stringify(S.scans) ||
        (prev.recorder && S.recorder && prev.recorder.status !== S.recorder.status))) loadLibrary();
  } catch (e) { S = null; renderTop(); }
  setTimeout(poll, 2000);
}

/* ------------------------------------------------------------------ router */
function route() {
  const [p, arg] = (location.hash.slice(1) || 'home').split('/');
  const changed = p !== page || arg !== pageArg;
  page = p; pageArg = arg || '';
  if (!changed) return;
  renderTop();
  const main = $('#main'); main.innerHTML = ''; window.scrollTo(0, 0);
  if (page === 'home') renderHome(null, true);
  else if (page === 'recordings') renderRecordings();
  else if (page === 'review') renderReview(pageArg);
  else if (page === 'settings') renderSettings();
  else location.hash = '#home';
}
window.addEventListener('hashchange', route);

/* ------------------------------------------------------------------ home */
function homeKey(s) { return JSON.stringify([s.recorder, s.google, s.player, s.messages.length, s.scans, s.update]); }
function renderHome(prev, force) {
  if (!S) { $('#main').innerHTML = '<div class="empty"><strong>Starting…</strong></div>'; return; }
  if (!force && prev && homeKey(prev) === homeKey(S)) return;
  if (document.activeElement && $('#main').contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return;
  const r = S.recorder;
  let state = 'off', title = 'Auto-record is off', sub = 'Turn it on and every Apex session is recorded and scanned by itself.';
  if (!r) { title = 'Scanner'; sub = 'Auto-record isn\'t available in this build. You can still add and scan recordings.'; }
  else if (r.recording) { state = 'rec'; title = 'Recording Apex'; sub = 'Recording stops when you close Apex. Then the scan starts (you can skip it).'; }
  else if (r.scanning || S.scans.now) { state = 'busy'; title = 'Scanning'; sub = (S.scans.now ? S.scans.now : 'Your last session') + ' · it pauses by itself if you start Apex.'; }
  else if (r.enabled) { state = 'on'; title = 'Ready'; sub = r.status === 'Waiting for Apex' ? 'Waiting for Apex. Recording starts by itself when the game opens.' : r.status; }
  const used = r ? r.used : 0, max = r ? r.settings.max_gb * 1e9 : 1, pct = Math.min(100, used / max * 100);
  const g = {ok: ['Signed in', 'good'], expired: ['Sign-in expired', 'bad'], none: ['Not signed in', 'warn'], checking: ['Checking…', '']}[S.google] || ['?', ''];
  const recent = LIB.slice(0, 4);
  $('#main').innerHTML = `
    ${!S.player ? `<div class="notice row between"><span><b>Type your EEC name</b> so your scans are labelled for the coach.</span>
      <span class="row"><input id="pname" type="text" placeholder="EEC name" style="width:200px"><button class="btn brand small" id="psave">Save</button></span></div>` : ''}
    <section class="card hero ${state}">
      <div class="dot"><i></i></div>
      <div><h1>${esc(title)}</h1><p>${esc(sub)}</p></div>
      <div class="toggles">
        ${r ? switchHTML('t-auto', r.enabled, 'Auto-record', 'Records Apex when it opens') : ''}
        ${r ? switchHTML('t-boot', r.settings.start_with_windows, 'Start with Windows', 'Waits in the tray, nothing to remember') : ''}
      </div>
    </section>
    <div class="kpis">
      <div class="kpi"><span>Recordings</span><b>${r ? r.count : LIB.length}</b><small>kept ${r ? r.settings.keep_days + ' days' : '—'}</small></div>
      <div class="kpi"><span>Storage used</span><b>${r ? fmtGB(used) : '—'}</b><small>of ${r ? r.settings.max_gb + ' GB' : '—'}</small>
        <div class="meter ${pct > 90 ? 'full' : pct > 70 ? 'warn' : ''}"><i style="width:${pct.toFixed(1)}%"></i></div></div>
      <div class="kpi"><span>Quality</span><b>${r ? r.settings.bitrate_mbps + ' Mb/s' : '—'}</b><small>${r ? '≈ ' + r.gbPerHour + ' GB per hour' : ''}</small></div>
      <div class="kpi"><span>Google Drive</span><b style="font-size:22px;line-height:1.6"><span class="pill ${g[1]}" style="font-size:13px">${g[0]}</span></b>
        <small>${S.google === 'ok' ? 'Results go to “PCS Scans”' : `<a href="#" id="signin">Sign in with Google</a>`}</small></div>
    </div>
    <div class="grid g2">
      <section class="card"><div class="row between"><h2>Latest recordings</h2><a class="btn small ghost" href="#recordings">All recordings</a></div>
        <div class="list" style="margin-top:8px">${recent.length ? recent.map(it => `
          <div class="row between" style="padding:9px 2px;border-bottom:1px solid var(--line)">
            <div style="min-width:0"><div class="name" style="font-weight:600">${esc(fmtDate(it.mtime))}${it.auto ? '' : ` <span class="faint" style="font-weight:400">· ${esc(it.name)}</span>`}</div>
              <div class="row" style="gap:6px;margin-top:2px">${gameChips(it)} ${statusPill(it.status)}</div></div>
            <a class="btn small" href="#review/${it.id}">Review</a></div>`).join('') :
          '<div class="empty"><strong>No recordings yet</strong>Play Apex with auto-record on, or add recordings by hand.</div>'}</div></section>
      <section class="card"><h2>Activity</h2><div class="log">${S.messages.slice().reverse().map(m =>
        `<div><time>${new Date(m.t * 1000).toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'})}</time><span>${esc(m.text)}</span></div>`).join('') ||
        '<div class="muted">Nothing yet.</div>'}</div></section>
    </div>`;
  const ta = $('#t-auto'); if (ta) ta.onchange = async () => {
    ta.disabled = true;
    try { S = await api('/api/autorecord', {enabled: ta.checked}); toast(ta.checked ? 'Auto-record on. First time: a private recorder is set up (a minute).' : 'Auto-record off.'); }
    catch (e) { toast(e.message, true); ta.checked = !ta.checked; }
    ta.disabled = false; renderTop(); renderHome(null, true);
  };
  const tb = $('#t-boot'); if (tb) tb.onchange = async () => {
    try { S = await api('/api/settings', {start_with_windows: tb.checked}); toast(tb.checked ? 'PCS Scanner starts with Windows.' : 'PCS Scanner no longer starts with Windows.'); }
    catch (e) { toast(e.message, true); tb.checked = !tb.checked; }
  };
  const si = $('#signin'); if (si) si.onclick = e => { e.preventDefault(); api('/api/signin', {}); toast('Your browser opens for the Google sign-in…'); };
  const ps = $('#psave'); if (ps) ps.onclick = async () => {
    const v = $('#pname').value.trim(); if (!v) return;
    S = await api('/api/settings', {player: v}); toast('Saved.'); renderHome(null, true);
  };
  if (!LIB.length && !renderHome.loaded) { renderHome.loaded = true; loadLibrary(); }
}
function switchHTML(id, on, label, hint) {
  return `<label class="switch"><input type="checkbox" id="${id}" ${on ? 'checked' : ''}><span class="track"></span><span><b>${esc(label)}</b><small>${esc(hint)}</small></span></label>`;
}
function gameChips(it) {
  const games = (it.scan && it.scan.games) || [];
  if (!it.scan) return '';
  if (!games.length) return '<span class="faint" style="font-size:12px">no game found</span>';
  return games.map(g => `<span class="chip">${esc(mapName(g.map))}</span>`).join('');
}

/* ------------------------------------------------------------------ recordings */
let filter = 'all', selected = new Set();
async function loadLibrary() {
  try { LIB = await api('/api/library'); } catch (e) { return; }
  if (page === 'recordings') drawList();
  if (page === 'home') renderHome(null, true);
}
function renderRecordings() {
  $('#main').innerHTML = `
    <div class="pagehead"><div><h1>Recordings</h1><p>Your recordings and their scans. Review a recording to see drops, rings and fights on the timeline.</p></div>
      <div class="toolbar">
        <div class="seg" id="filt">${['all', 'scanned', 'not scanned'].map(f => `<button data-f="${f}" class="${f === filter ? 'on' : ''}">${f}</button>`).join('')}</div>
        <button class="btn" id="add">Add recordings…</button>
        ${S && S.youtube && S.youtube.available ? '<button class="btn" id="eecRes" title="The lobby export from EEC (Download button on the lobby page): standing, points and kills go in the YouTube description">EEC results…</button>' : ''}
        <button class="btn brand" id="scanSel" disabled>Scan selected</button>
      </div></div>
    <div class="drop" id="dropzone">Drop recordings or folders here to add them${S && S.youtube && S.youtube.available ? ' · or an EEC lobby export (.json) for the YouTube description' : ''}</div>
    <section class="card" style="padding:6px 12px"><div class="list" id="list"><div class="empty">Loading…</div></div></section>
    <div class="row muted" style="font-size:13px"><button class="btn small ghost" id="openRes">Open results folder</button>
      ${S && S.recorder ? '<button class="btn small ghost" id="openRec">Open recordings folder</button>' : ''}</div>`;
  $$('#filt button').forEach(b => b.onclick = () => { filter = b.dataset.f; $$('#filt button').forEach(x => x.classList.toggle('on', x === b)); drawList(); });
  $('#add').onclick = async () => { const r = await api('/api/library/pick', {}).catch(e => toast(e.message, true)); if (r) { toast(r.added ? `Added ${r.added} recording(s).` : 'Nothing added.'); loadLibrary(); } };
  $('#scanSel').onclick = () => scan([...selected]);
  const er = $('#eecRes'); if (er) er.onclick = async () => {
    try { const r = await api('/api/youtube/results', {}); if (r.added.length) toast('EEC results added: ' + r.added.join(' · ')); loadLibrary(); }
    catch (e) { toast(e.message, true); }
  };
  $('#openRes').onclick = () => api('/api/open', {what: 'results'});
  const orc = $('#openRec'); if (orc) orc.onclick = () => api('/api/open', {what: 'recordings'});
  const dz = $('#dropzone');
  ['dragenter', 'dragover'].forEach(ev => document.addEventListener(ev, e => { if (page === 'recordings') { e.preventDefault(); dz.classList.add('over'); } }));
  ['dragleave', 'drop'].forEach(ev => document.addEventListener(ev, e => { dz.classList.remove('over'); if (ev === 'drop') { e.preventDefault(); setTimeout(loadLibrary, 800); setTimeout(loadLibrary, 2500); } }));
  loadLibrary();
}
function drawList() {
  const el = $('#list'); if (!el) return;
  const items = LIB.filter(it => filter === 'all' || (filter === 'scanned' ? !!it.scan : !it.scan));
  selected = new Set([...selected].filter(id => LIB.some(it => it.id === id)));
  if (!items.length) {
    el.innerHTML = `<div class="empty"><strong>${LIB.length ? 'Nothing here' : 'No recordings yet'}</strong>${LIB.length ? 'Try another filter.' : 'Turn on auto-record, or add recordings with the button or by dropping them here.'}</div>`;
  } else {
    el.innerHTML = `<div class="recrow head"><span></span><span>Recording</span><span class="c3">Recorded</span><span class="c4">Size</span><span class="c5">Status</span><span></span></div>` +
      items.map(it => `<div class="recrow" data-id="${it.id}">
        <input type="checkbox" ${selected.has(it.id) ? 'checked' : ''} aria-label="Select ${esc(it.name)}">
        <div style="min-width:0"><div class="name" title="${esc(it.path)}">${esc(it.name)}</div><div class="sub">${gameChips(it)}${it.auto ? '<span class="faint" style="font-size:12px">auto-recorded</span>' : ''}</div></div>
        <span class="c3 muted">${fmtDate(it.mtime)}</span><span class="c4 mono muted">${fmtGB(it.size)}</span><span class="c5">${statusPill(it.status)}${ytPill(it)}</span>
        <div class="acts">${!it.scan && !['scanning', 'waiting', 'recording'].includes(it.status) ? '<button class="btn small" data-a="scan">Scan</button>' : ''}
          <a class="btn small ${it.scan ? 'brand' : ''}" href="#review/${it.id}">Review</a>
          ${it.auto && it.yt && !['done', 'uploading'].includes(it.yt.status) ? `<button class="btn small ghost" data-a="eec" title="EEC lobby link of this scrim block (goes in the YouTube description)">${it.yt.res ? `EEC ✓ ${ordinal(it.yt.res.standing)}/${it.yt.res.teams}` : it.yt.eec && it.yt.eec.length ? 'EEC ✓' : 'EEC link'}</button>` : ''}
          ${it.auto && S && S.youtube && S.youtube.available && !(it.yt && ['done', 'waiting', 'uploading'].includes(it.yt.status)) ? '<button class="btn small ghost" data-a="yt" title="Upload this recording\'s scrim block to the team YouTube channel">YouTube</button>' : ''}
          <button class="btn small ghost" data-a="show" title="Open the file">Open</button>
          ${!it.auto ? '<button class="btn small ghost" data-a="forget" title="Remove from this list (the file stays)">✕</button>' : ''}</div></div>`).join('');
  }
  $$('.recrow[data-id]', el).forEach(row => {
    const id = row.dataset.id;
    $('input', row).onchange = e => { e.target.checked ? selected.add(id) : selected.delete(id); syncSel(); };
    $$('[data-a]', row).forEach(b => b.onclick = async () => {
      if (b.dataset.a === 'scan') scan([id]);
      if (b.dataset.a === 'show') api('/api/open', {what: 'file', id});
      if (b.dataset.a === 'eec') {
        const it = LIB.find(x => x.id === id), cur = (it && it.yt && it.yt.eec || []).join(' ');
        const v = window.prompt('EEC lobby link of this scrim block (e.g. https://eternalesports.club/lobbies/4/e9483b).\nSeveral lobbies: separate with spaces. Empty = remove.', cur);
        if (v === null) return;
        try { const r = await api('/api/youtube/eec', {id, links: v.split(/\s+/).filter(Boolean)}); toast(r.eec.length ? 'EEC link saved: it goes in the YouTube description.' : 'EEC link removed.'); loadLibrary(); }
        catch (e) { toast(e.message, true); }
      }
      if (b.dataset.a === 'yt') { try { const r = await api('/api/youtube/upload', {id}); toast(`Block of ${r.queued} recording(s) queued for YouTube.`); loadLibrary(); } catch (e) { toast(e.message, true); } }
      if (b.dataset.a === 'forget') { await api('/api/library/forget', {id}); loadLibrary(); }
    });
  });
  syncSel();
}
function ordinal(n) { return n + ((n % 100 >= 11 && n % 100 <= 13) ? 'th' : ({1: 'st', 2: 'nd', 3: 'rd'}[n % 10] || 'th')); }
function ytPill(it) {
  const y = it.yt; if (!y || !y.status) return '';
  if (y.status === 'done') return ` <a class="pill good" href="${esc(y.url)}" target="_blank" rel="noopener" title="${esc(y.title || '')}">YouTube ↗</a>`;
  if (y.status === 'uploading' || y.status === 'waiting') return ' <span class="pill info">YouTube…</span>';
  if (y.status === 'requested' || y.status === 'waiting-retry') return ' <span class="pill info">YouTube soon</span>';
  if (y.status === 'failed') return ` <span class="pill bad" title="${esc(y.error || '')}">YouTube failed</span>`;
  return '';
}
function syncSel() { const b = $('#scanSel'); if (b) { b.disabled = !selected.size; b.textContent = selected.size ? `Scan selected (${selected.size})` : 'Scan selected'; } }
async function scan(ids) {
  try { const r = await api('/api/scan', {ids}); toast(r.queued ? `${r.queued} recording(s) queued for scanning.` : 'Already scanning or queued.'); selected.clear(); loadLibrary(); }
  catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ review */
const KIND = {drop: ['D', 'Drop map'], jump: ['J', 'Jumped'], land: ['L', 'Landed'], ring: ['R', 'Ring'], ringclose: ['R', 'Ring'], feed: ['', 'Kill feed'], mark: ['★', 'Your mark'], mute: ['', 'Mute']};
const TRACKN = {game: 'Game sound', discord: 'Discord', mic: 'Microphone'};
function buildEvents(d) {
  const ev = [];
  (d.games || []).forEach((g, gi) => {
    const map = mapName(g.map), spots = (g.drop && g.drop.spots) || [];
    const ours = spots.find(s => s.kind === 'ours');
    if (g.tStart != null) ev.push({t: g.tStart, k: 'drop', g: gi, title: 'Drop map', sub: `${map} · ${spots.length} squads seen`});
    const L = g.landing || {};
    if (L.exitAtExact != null) ev.push({t: L.exitAtExact, k: 'jump', g: gi, title: 'Jumped from the ship'});
    if (L.landedAtExact != null) ev.push({t: L.landedAtExact, k: 'land', g: gi, title: 'Landed', sub: ours ? 'at ' + poiName(ours.poi) : ''});
    (g.ring || []).forEach(r => {
      const n = r.round ? ' ' + r.round : '';
      if (r.kind === 'wait') {
        ev.push({t: r.appearedAt ?? r.firstSeen, k: 'ring', g: gi, title: `Ring${n} announced`, sub: r.endsAt ? 'closes at ' + fmtT(r.endsAt) : ''});
        if (r.endsAt) ev.push({t: r.endsAt, k: 'ringclose', g: gi, title: `Ring${n} starts closing`});
      } else {
        ev.push({t: r.firstSeen, k: 'ringclose', g: gi, title: `Ring${n} closing`, sub: r.endsAt ? 'until ' + fmtT(r.endsAt) : ''});
      }
    });
  });
  const groups = new Map();
  (d.feed || []).forEach(l => { const k = l.t.toFixed(2); groups.set(k, (groups.get(k) || 0) + 1); });
  groups.forEach((n, k) => ev.push({t: +k, k: 'feed', title: 'Kill feed', sub: n > 1 ? n + ' lines' : ''}));
  (d.marks || []).forEach(m => {
    if (m.kind === 'mark') ev.push({t: m.t, k: 'mark', title: 'Marked moment', sub: 'hotkey pressed in game'});
    else if (m.kind === 'mute') ev.push({t: m.t, k: 'mute', title: `${TRACKN[m.track] || m.track} ${m.muted ? 'muted' : 'back on'}`});
  });
  ev.sort((a, b) => a.t - b.t);
  return ev;
}
function gameSpans(d) {
  return (d.games || []).map(g => {
    const tr = g.track || [];
    const end = Math.max(g.tEnd || 0, tr.length ? tr[tr.length - 1][0] : 0, ...(g.ring || []).map(r => r.lastSeen || 0));
    return {from: g.tStart, to: end, map: g.map, mode: g.mode};
  });
}

let RV = null;   // review state
async function renderReview(id) {
  $('#main').innerHTML = '<div class="empty"><strong>Loading…</strong></div>';
  let d;
  try { d = await api('/api/library/' + id); } catch (e) { $('#main').innerHTML = `<div class="empty"><strong>Not found</strong>${esc(e.message)}<br><br><a class="btn" href="#recordings">Back</a></div>`; return; }
  if (page !== 'review' || pageArg !== id) return;
  const ev = buildEvents(d), spans = gameSpans(d);
  RV = {d, ev, spans, tab: d.games ? 'events' : 'events', dur: 0};
  $('#main').innerHTML = `
    <div class="pagehead"><div><h1>${esc(fmtDate(d.mtime))}</h1><p class="row" style="gap:8px">${gameChips(d)} ${statusPill(d.status)} <span class="faint mono" title="${esc(d.path)}">${esc(d.name)}</span></p></div>
      <div class="toolbar"><a class="btn ghost" href="#recordings">← Recordings</a>
        ${!d.scan ? '<button class="btn brand" id="rvScan">Scan this recording</button>' : ''}
        <button class="btn" id="rvOpen">Open file</button></div></div>
    <div class="review">
      <div>
        <div class="player"><video id="vid" controls preload="metadata" src="/video/${d.id}?t=${encodeURIComponent(TOKEN)}"></video>
          <div class="err" id="vidErr" hidden><div><h2>This recording can't play here</h2><p class="muted">Its video format isn't supported by the built-in player (usually HEVC/H.265).<br>Open it in your usual video player instead.</p><button class="btn brand" id="vidOpen">Open file</button></div></div></div>
        <div class="card" style="margin-top:12px;padding:12px 16px 14px">
          <div class="tl" id="tl"><div class="lanes">
            <div class="lane games" id="laneG"><span class="lane-label">Games</span></div>
            <div class="lane" id="laneE"><span class="lane-label">Drop · jump · landing · rings</span></div>
            <div class="lane" id="laneF"><span class="lane-label">Kill feed</span></div>
            ${(d.marks || []).length ? '<div class="lane" id="laneM"><span class="lane-label">Your marks · muted sound</span></div>' : ''}
          </div><div class="head-line" id="playhead" style="left:0"></div><div class="hover-time" id="hoverT" hidden></div></div>
          <div class="row between" style="margin-top:8px">
            <div class="legend"><span><i style="background:#e6ad45"></i>Drop map</span><span><i style="background:#6aa0ff"></i>Jump</span><span><i style="background:#5fd08f"></i>Landing</span><span><i style="background:#c48bff"></i>Ring</span><span><i style="background:var(--accent);border-radius:2px;width:4px"></i>Kill feed</span></div>
            <div class="row"><select id="speed" style="width:auto;padding:4px 8px">${[0.25, 0.5, 1, 1.5, 2, 4].map(s => `<option value="${s}" ${s === 1 ? 'selected' : ''}>${s}×</option>`).join('')}</select></div>
          </div>
          <div id="mixer"></div>
          <div class="keys" style="margin-top:8px"><kbd>Space</kbd> play/pause · <kbd>←</kbd><kbd>→</kbd> 5 s · <kbd>J</kbd><kbd>L</kbd> 10 s · <kbd>,</kbd><kbd>.</kbd> one frame · <kbd>[</kbd><kbd>]</kbd> previous/next event · <kbd>F</kbd> full screen · <kbd>M</kbd> mute</div>
        </div>
      </div>
      <aside class="card side">
        <div class="tabs" id="rvTabs"><button data-t="events" class="on">Events</button><button data-t="feed">Kill feed (${(d.feed || []).length})</button><button data-t="drop">Drop</button></div>
        <div class="scroll" id="rvBody"></div>
      </aside>
    </div>`;
  const v = $('#vid');
  $('#rvOpen').onclick = $('#vidOpen').onclick = () => api('/api/open', {what: 'file', id: d.id});
  const rs = $('#rvScan'); if (rs) rs.onclick = () => scan([d.id]);
  v.addEventListener('error', () => { $('#vidErr').hidden = false; });
  setupAudio(d, v);
  v.addEventListener('loadedmetadata', () => { RV.dur = v.duration; drawTimeline(); });
  v.addEventListener('timeupdate', onTime);
  $('#speed').onchange = e => v.playbackRate = +e.target.value;
  $$('#rvTabs button').forEach(b => b.onclick = () => { RV.tab = b.dataset.t; $$('#rvTabs button').forEach(x => x.classList.toggle('on', x === b)); drawSide(); });
  const tl = $('#tl');
  const tAt = e => { const r = $('#laneG').getBoundingClientRect(); return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * dur(); };
  tl.addEventListener('mousemove', e => { const h = $('#hoverT'); h.hidden = false; h.textContent = fmtT(tAt(e)); h.style.left = (tAt(e) / dur() * 100) + '%'; });
  tl.addEventListener('mouseleave', () => $('#hoverT').hidden = true);
  tl.addEventListener('click', e => { const t = e.target.dataset && e.target.dataset.t; seek(t != null ? +t : tAt(e)); });
  drawTimeline(); drawSide();
}
function dur() {
  if (RV.dur) return RV.dur;
  const last = Math.max(0, ...RV.ev.map(e => e.t), ...RV.spans.map(s => s.to || 0));
  return last ? last * 1.05 : 600;
}
function seek(t) {
  const v = $('#vid'); if (!v) return;
  v.currentTime = Math.max(0, t - (seek.lead || 0)); onTime();
}
function drawTimeline() {
  if (!RV || !$('#laneG')) return;
  const D = dur(), pct = t => (t / D * 100).toFixed(3) + '%';
  $('#laneG').innerHTML = '<span class="lane-label">Games</span>' + (RV.spans.length ? RV.spans.map(s =>
    `<div class="segm" data-t="${s.from}" style="left:${pct(s.from)};width:calc(${pct(Math.max(1, s.to - s.from))})" title="${esc(mapName(s.map))} · ${fmtT(s.from)}–${fmtT(s.to)}">${esc(mapName(s.map))}${s.mode ? ' · ' + esc(s.mode) : ''}</div>`).join('') :
    `<span class="faint" style="position:absolute;left:8px;top:4px;font-size:12px">${RV.d.scan ? 'No game found in this recording' : 'Not scanned yet: scan it to see games and events here'}</span>`);
  $('#laneE').innerHTML = '<span class="lane-label">Drop · jump · landing · rings</span>' + RV.ev.filter(e => !['feed', 'mark', 'mute'].includes(e.k)).map(e =>
    `<button class="mk ${e.k}" data-t="${e.t}" style="left:${pct(e.t)}" title="${fmtT(e.t)} · ${esc(e.title)}${e.sub ? ' · ' + esc(e.sub) : ''}">${KIND[e.k][0]}</button>`).join('');
  const lm = $('#laneM');
  if (lm) {
    const marks = RV.d.marks || [], spans = [], open = {};
    marks.filter(m => m.kind === 'mute').forEach(m => { if (m.muted) open[m.track] = m.t; else if (open[m.track] != null) { spans.push([m.track, open[m.track], m.t]); delete open[m.track]; } });
    Object.entries(open).forEach(([tr, t]) => spans.push([tr, t, D]));
    lm.innerHTML = '<span class="lane-label">Your marks · muted sound</span>' +
      spans.map(([tr, a, b]) => `<div class="muted-span ${tr}" data-t="${a}" style="left:${pct(a)};width:${pct(b - a)}" title="${esc(TRACKN[tr] || tr)} muted ${fmtT(a)}–${fmtT(b)}"></div>`).join('') +
      marks.filter(m => m.kind === 'mark').map(m => `<button class="mk mark" data-t="${m.t}" style="left:${pct(m.t)}" title="${fmtT(m.t)} · marked moment">★</button>`).join('');
  }
  $('#laneF').innerHTML = '<span class="lane-label">Kill feed</span>' + RV.ev.filter(e => e.k === 'feed').map(e =>
    `<div class="tick" data-t="${e.t}" style="left:${pct(e.t)}" title="${fmtT(e.t)} · kill feed${e.sub ? ' (' + e.sub + ')' : ''}"></div>`).join('');
}
function drawSide() {
  const body = $('#rvBody'); if (!body) return;
  const d = RV.d;
  if (!d.scan) { body.innerHTML = '<div class="empty"><strong>Not scanned yet</strong>Scan this recording to see its drop, landing, rings and kill feed.</div>'; return; }
  if (RV.tab === 'events') {
    let html = '', g = -1;
    RV.ev.forEach((e, i) => {
      if (e.k === 'feed') return;                     // kill feed lines have their own tab
      if (e.g != null && e.g !== g) { g = e.g; const sp = RV.spans[g]; html += `<div class="gamehead">Game ${g + 1} · ${esc(mapName(sp.map))}</div>`; }
      const col = {drop: '#e6ad45', jump: '#6aa0ff', land: '#5fd08f', ring: '#c48bff', ringclose: '#8f5ee0', feed: 'var(--accent)', mark: '#f1efea', mute: '#6d6a63'}[e.k];
      html += `<div class="ev" data-i="${i}" data-t="${e.t}"><time>${fmtT(e.t)}</time><i style="background:${col}"></i><span><b>${esc(e.title)}</b>${e.sub ? `<small>${esc(e.sub)}</small>` : ''}</span></div>`;
    });
    body.innerHTML = html || '<div class="empty">No events found.</div>';
  } else if (RV.tab === 'feed') {
    const f = d.feed || [];
    body.innerHTML = f.length ? '<div class="feed">' + f.map(l => `<div class="ln" data-t="${l.t}"><time class="mono muted">${fmtT(l.t)}</time><img alt="Kill feed line at ${fmtT(l.t)}" src="data:image/jpeg;base64,${l.jpg}"></div>`).join('') + '</div>' :
      '<div class="empty">No kill feed lines in this recording.</div>';
  } else {
    body.innerHTML = (d.games || []).map((g, i) => dropHTML(g, i)).join('') || '<div class="empty">No drop map found.</div>';
  }
  $$('[data-t]', body).forEach(el => el.onclick = () => seek(+el.dataset.t));
  onTime(true);
}
function dropHTML(g, i) {
  const spots = (g.drop && g.drop.spots) || [];
  if (!spots.length) return `<div class="gamehead">Game ${i + 1} · ${esc(mapName(g.map))}</div><div class="muted" style="padding:6px">The drop map wasn't seen in this game.</div>`;
  const byPoi = {};
  spots.forEach(s => { const k = poiName(s.poi) || 'Open ground'; (byPoi[k] = byPoi[k] || {n: 0, ours: false}).n++; if (s.kind === 'ours') byPoi[k].ours = true; });
  const rows = Object.entries(byPoi).sort((a, b) => b[1].ours - a[1].ours || b[1].n - a[1].n);
  const grid = [20, 40, 60, 80].map(p => `<line x1="${p}" y1="0" x2="${p}" y2="100"/><line x1="0" y1="${p}" x2="100" y2="${p}"/>`).join('');
  const dots = spots.slice().sort((a, b) => (a.kind === 'ours') - (b.kind === 'ours')).map(s =>
    `<circle cx="${s.x}" cy="${s.y}" r="${s.kind === 'ours' ? 2.6 : 1.7}" fill="${s.kind === 'ours' ? '#e5392d' : '#d9d6cf'}" stroke="#121211" stroke-width=".6"><title>${esc(poiName(s.poi) || 'Open ground')}${s.kind === 'ours' ? ' (us)' : ''}</title></circle>`).join('');
  return `<div class="gamehead">Game ${i + 1} · ${esc(mapName(g.map))} · ${spots.length} squads</div>
    <svg class="dropmap" viewBox="0 0 100 100" role="img" aria-label="Drop spots"><g stroke="#24231f" stroke-width=".3">${grid}</g>${dots}</svg>
    <div class="faint" style="font-size:12px;margin-top:4px">Positions on the map as shown on the drop screen. Red = your squad.</div>
    <div class="pois">${rows.map(([k, v]) => `<div class="${v.ours ? 'ours' : ''}"><span>${esc(k)}${v.ours ? ' · you' : ''}</span><span class="mono">${v.n} squad${v.n > 1 ? 's' : ''}</span></div>`).join('')}</div>`;
}
function onTime(force) {
  const v = $('#vid'); if (!v || !RV) return;
  const t = v.currentTime;
  $('#playhead').style.left = (t / dur() * 100) + '%';
  const body = $('#rvBody'); if (!body) return;
  let best = null;
  $$('[data-t]', body).forEach(el => { if (+el.dataset.t <= t + 0.25) best = el; });
  if (best !== onTime.last || force === true) {
    $$('.now', body).forEach(el => el.classList.remove('now'));
    if (best) { best.classList.add('now'); if (!v.paused) best.scrollIntoView({block: 'nearest'}); }
    onTime.last = best;
  }
}
document.addEventListener('keydown', e => {
  if (page !== 'review') return;
  if (['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;
  const v = $('#vid'); if (!v) return;
  const k = e.key, step = d => { v.currentTime = Math.max(0, Math.min(v.duration || 1e9, v.currentTime + d)); };
  const times = RV.ev.filter(x => x.k !== 'feed').map(x => x.t);
  if (k === ' ' || k === 'k' || k === 'K') { v.paused ? v.play() : v.pause(); }
  else if (k === 'ArrowLeft') step(-5); else if (k === 'ArrowRight') step(5);
  else if (k === 'j' || k === 'J') step(-10); else if (k === 'l' || k === 'L') step(10);
  else if (k === ',') { v.pause(); step(-1 / 60); } else if (k === '.') { v.pause(); step(1 / 60); }
  else if (k === ']') { const n = times.find(t => t > v.currentTime + 0.3); if (n != null) seek(n); }
  else if (k === '[') { const p = times.filter(t => t < v.currentTime - 1).pop(); seek(p != null ? p : 0); }
  else if (k === 'f' || k === 'F') { document.fullscreenElement ? document.exitFullscreen() : v.requestFullscreen(); }
  else if (k === 'm' || k === 'M') { if (RV.aud) { const all = RV.aud.every(x => x.mute); RV.aud.forEach(x => x.mute = !all); applyMix(); drawMixer(); } else v.muted = !v.muted; }
  else return;
  e.preventDefault();
});

/* ------------------------------------------------------------------ separate sound tracks in review */
function setupAudio(d, v) {
  clearInterval(setupAudio.h); RV.aud = null;
  const tracks = d.audio || [];
  if (tracks.length < 2) return;                       // one track: the video plays it itself
  v.muted = true;
  RV.aud = tracks.map(t => ({t, a: new Audio(`/audio/${d.id}/${t.n}?t=${encodeURIComponent(TOKEN)}`), vol: 1, mute: false}));
  RV.aud.forEach(x => { x.a.preload = 'auto'; x.a.addEventListener('error', () => { x.err = true; drawMixer(); }); });
  const all = f => RV.aud.forEach(x => { try { f(x.a); } catch (e) {} });
  v.addEventListener('play', () => all(a => { a.currentTime = v.currentTime; a.playbackRate = v.playbackRate; a.play().catch(() => {}); }));
  v.addEventListener('pause', () => all(a => a.pause()));
  v.addEventListener('seeking', () => all(a => a.currentTime = v.currentTime));
  v.addEventListener('ratechange', () => all(a => a.playbackRate = v.playbackRate));
  v.addEventListener('volumechange', () => { if (!v.muted) v.muted = true; });   // sound comes from the tracks
  setupAudio.h = setInterval(() => {
    if (page !== 'review' || !RV || !RV.aud) { clearInterval(setupAudio.h); all(a => a.pause()); return; }
    if (!v.paused) all(a => { if (Math.abs(a.currentTime - v.currentTime) > 0.2) a.currentTime = v.currentTime; });
  }, 500);
  applyMix(); drawMixer();
}
function applyMix() { (RV.aud || []).forEach(x => { x.a.volume = x.vol; x.a.muted = x.mute; }); }
function drawMixer() {
  const m = $('#mixer'); if (!m || !RV.aud) return;
  m.innerHTML = '<div class="mixer">' + RV.aud.map((x, i) => `<div class="trk ${x.mute ? 'off' : ''}">
      <button class="btn small ${x.mute ? '' : 'brand'}" data-i="${i}" data-a="mute" title="Mute / unmute">${esc(x.t.label)}</button>
      <input type="range" min="0" max="1" step="0.05" value="${x.vol}" data-i="${i}" aria-label="${esc(x.t.label)} volume">
      ${x.err ? '<span class="pill bad">not available</span>' : ''}</div>`).join('') + '</div>';
  $$('[data-a=mute]', m).forEach(b => b.onclick = () => { const x = RV.aud[+b.dataset.i]; x.mute = !x.mute; applyMix(); drawMixer(); });
  $$('input[type=range]', m).forEach(r => r.oninput = () => { RV.aud[+r.dataset.i].vol = +r.value; applyMix(); });
}

/* ------------------------------------------------------------------ hotkey fields */
function hotkeyText(v) { return v ? v.split('+').map(p => p.length <= 2 || /^f\d/.test(p) ? p.toUpperCase() : p[0].toUpperCase() + p.slice(1)).join(' + ') : 'none'; }
function hotkeyField(id, value, label) {
  return `<label class="field"><span>${esc(label)}</span><input class="hk" id="${id}" type="text" readonly data-v="${esc(value)}" value="${esc(hotkeyText(value))}">
    <small>Click, then press the keys. Backspace = none.</small></label>`;
}
function wireHotkeys() {
  $$('input.hk').forEach(inp => {
    inp.onfocus = () => { inp.value = 'Press keys…'; };
    inp.onblur = () => { inp.value = hotkeyText(inp.dataset.v); };
    inp.onkeydown = e => {
      e.preventDefault();
      if (e.key === 'Backspace' || e.key === 'Delete') { inp.dataset.v = ''; inp.blur(); return; }
      let key = '';
      if (/^Digit\d$/.test(e.code)) key = e.code.slice(5);
      else if (/^Key[A-Z]$/.test(e.code)) key = e.code.slice(3).toLowerCase();
      else if (/^F\d{1,2}$/.test(e.code)) key = e.code.toLowerCase();
      else if (/^Numpad\d$/.test(e.code)) key = 'num' + e.code.slice(6);
      if (!key) return;
      if (!(e.ctrlKey || e.altKey || e.metaKey)) { toast('Use Ctrl, Alt or Win with the key, so it never takes a game key.', true); return; }
      inp.dataset.v = [e.ctrlKey && 'ctrl', e.altKey && 'alt', e.shiftKey && 'shift', e.metaKey && 'win', key].filter(Boolean).join('+');
      inp.blur();
    };
  });
}

/* ------------------------------------------------------------------ settings */
function renderSettings() {
  if (!S) { $('#main').innerHTML = '<div class="empty"><strong>Starting…</strong></div>'; setTimeout(() => page === 'settings' && renderSettings(), 500); return; }
  const r = S.recorder, s = r ? r.settings : {};
  $('#main').innerHTML = `
    <div class="pagehead"><div><h1>Settings</h1><p>Saved on this PC. Changes apply right away.</p></div><button class="btn brand" id="save">Save changes</button></div>
    <section class="card section"><h2>You</h2>
      <div class="form">
        <label class="field"><span>EEC name</span><input id="f-player" type="text" value="${esc(S.player)}" placeholder="Your in-game name"><small>Labels your scans for the coach.</small></label>
        <label class="field"><span>Role</span><select id="f-role">${['', 'IGL', 'Fragger', 'Anchor', 'Support', 'Sub'].map(x => `<option ${x === S.role ? 'selected' : ''}>${x}</option>`).join('')}</select></label>
        <div class="field"><span class="muted" style="font-size:12px;letter-spacing:.06em;text-transform:uppercase">Google Drive</span>
          <div class="row" style="margin-top:4px">${S.google === 'ok' ? '<span class="pill good">Signed in</span>' : '<span class="pill warn">Not signed in</span>'}
          <button class="btn small" id="f-signin">${S.google === 'ok' ? 'Sign in again' : 'Sign in with Google'}</button></div></div>
      </div></section>
    ${r ? `
    <section class="card section"><h2>Start-up</h2>
      ${switchHTML('f-start_with_windows', s.start_with_windows, 'Start with Windows', 'PCS Scanner waits in the tray (bottom right) from the moment you log in. Recommended: you never have to think about it.')}
      ${switchHTML('f-enabled', r.enabled, 'Auto-record', 'Records the Apex window while the game runs, then offers to scan the session. Your microphone is never recorded.')}
    </section>
    <section class="card section"><h2>Recordings</h2>
      <div class="form">
        <label class="field wide"><span>Folder</span><input id="f-folder" type="text" value="${esc(s.folder)}"><small>Only recordings made by PCS Scanner in this folder are ever deleted.</small></label>
        <label class="field"><span>Keep recordings for (days)</span><input id="f-keep_days" type="number" min="1" max="90" value="${s.keep_days}"><small>1 to 90 days.</small></label>
        <label class="field"><span>Maximum folder size (GB)</span><input id="f-max_gb" type="number" min="20" value="${s.max_gb}"><small>Oldest recordings go first. At least 20 GB.</small></label>
        <label class="field wide"><span>Quality: <b id="q-val">${s.bitrate_mbps} Mb/s</b> · <span id="q-hint"></span></span><input id="f-bitrate_mbps" type="range" min="8" max="60" step="1" value="${s.bitrate_mbps}">
          <small>8 Mb/s is the lowest that keeps the HUD readable for the scanner (Twitch streams are 8–10). 12 is recommended.</small></label>
        <label class="field"><span>Capture</span><select id="f-capture">${[['window', 'Apex window (recommended)'], ['game', 'Game capture'], ['display', 'Whole screen']].map(([v, l]) => `<option value="${v}" ${v === s.capture ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
      </div></section>
    <section class="card section"><h2>Sound and comms</h2>
      <p class="muted" style="margin:0">Each sound is recorded on its own track, so the review can play the game, Discord and your voice separately.</p>
      ${switchHTML('f-game_audio', s.game_audio, 'Game sound', 'Apex only, never other programs')}
      <div class="notice ${s.comms_consent ? 'info' : ''}">
        ${switchHTML('f-comms_consent', s.comms_consent, 'My team agreed that our voice comms are recorded',
          'Recording Discord records your teammates too. Turn this on only once everyone in your calls agreed (in Switzerland and many other countries, recording a private conversation needs everyone\'s consent).')}
      </div>
      <div class="form" id="commsBox">
        <div class="field">${switchHTML('f-discord_audio', s.discord_audio, 'Discord', 'Discord\'s sound only')}</div>
        <div class="field">${switchHTML('f-mic_audio', s.mic_audio, 'My microphone', 'Your default Windows microphone')}</div>
        <div class="field">${switchHTML('f-send_mic', s.send_mic, 'Send my mic track to the team', 'With the scan results, for the comms review')}</div>
      </div>
      <h3>Hotkeys while recording</h3>
      <p class="muted" style="margin:-6px 0 0">Active only while Apex is being recorded. A short beep confirms (two low tones = muted).</p>
      <div class="form">
        ${hotkeyField('f-hk_mark', s.hk_mark, 'Mark this moment')}
        ${hotkeyField('f-hk_mute_game', s.hk_mute_game, 'Mute / unmute game sound')}
        ${hotkeyField('f-hk_mute_discord', s.hk_mute_discord, 'Mute / unmute Discord')}
        ${hotkeyField('f-hk_mute_mic', s.hk_mute_mic, 'Mute / unmute microphone')}
        <div class="field">${switchHTML('f-hk_sounds', s.hk_sounds, 'Beep on hotkeys', 'Only you hear it; it isn\'t recorded')}</div>
      </div></section>
    ${S.youtube && S.youtube.available ? `<section class="card section"><h2>YouTube</h2>
      <p class="muted" style="margin:0">One video per scrim block: your recordings of the block joined together, with a chapter for each game and your marked moments in the description. Uploads only while Apex is closed. Old recordings are not uploaded unless you press “YouTube” on them.</p>
      <div class="row">${{ok: '<span class="pill good">Signed in</span>', expired: '<span class="pill bad">Sign-in expired</span>'}[S.youtube.signin] || '<span class="pill warn">Not signed in</span>'}
        <button class="btn small" id="f-ytsignin">${S.youtube.signin === 'ok' ? 'Sign in again' : 'Sign in to the team channel'}</button>
        <span class="faint" style="font-size:12px">At sign-in, pick the team channel (you need to be a manager of it).</span>
        ${S.youtube.status ? `<span class="pill info">${esc(S.youtube.status)}</span>` : ''}</div>
      ${switchHTML('f-yt_enabled', s.yt_enabled, 'Upload my scrim blocks', 'After the block, once every recording in it is scanned')}
      <div class="form">
        <label class="field"><span>Who can watch</span><select id="f-yt_privacy">${[['unlisted', 'Unlisted (anyone with the link)'], ['private', 'Private (accounts you share it with)']].map(([v, l]) => `<option value="${v}" ${v === s.yt_privacy ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
        <label class="field"><span>Sound on YouTube</span><select id="f-yt_audio">${[['game', 'Game only'], ['mix', 'Game + comms (Discord and mic)']].map(([v, l]) => `<option value="${v}" ${v === s.yt_audio ? 'selected' : ''} ${v === 'mix' && !s.comms_consent ? 'disabled' : ''}>${l}</option>`).join('')}</select>
          <small>${s.comms_consent ? 'Comms on YouTube means your team\'s voices are on the channel too.' : 'Comms need the team agreement above.'}</small></label>
        <label class="field"><span>Same block if less than (minutes) apart</span><input id="f-yt_gap_min" type="number" min="10" max="240" value="${s.yt_gap_min}"><small>Restarting Apex mid-block stays one video.</small></label>
        <div class="field">${switchHTML('f-yt_scrims_only', s.yt_scrims_only, 'Scrims only', 'Skip blocks with only ranked or pubs')}</div>
        <label class="field"><span>Team name (titles)</span><input id="f-yt_team" type="text" value="${esc(s.yt_team)}" maxlength="30"></label>
        <div class="field">${switchHTML('f-yt_wait_eec', s.yt_wait_eec, 'Wait for the EEC lobby link', 'You get a message after the block; paste the link in Recordings')}</div>
        <label class="field"><span>…but at most (hours)</span><input id="f-yt_wait_eec_h" type="number" min="1" max="72" value="${s.yt_wait_eec_h}"><small>Then it uploads without the link.</small></label>
      </div>
      <h3>Night uploads</h3>
      <div class="form">
        <label class="field"><span>When to upload</span><select id="f-yt_when">${[['after_block', 'Right after the block'], ['night', 'At night, when nobody uses the PC']].map(([v, l]) => `<option value="${v}" ${v === s.yt_when ? 'selected' : ''}>${l}</option>`).join('')}</select>
          <small>At night your connection is free and the PC is kept awake until it's done.</small></label>
        <label class="field"><span>Night from / to (hour)</span><span class="row" style="flex-wrap:nowrap"><input id="f-night_from" type="number" min="0" max="23" value="${s.night_from}"><span class="muted">→</span><input id="f-night_to" type="number" min="0" max="23" value="${s.night_to}"></span></label>
        <label class="field"><span>Start after no mouse/keyboard for (min)</span><input id="f-night_idle_min" type="number" min="5" max="180" value="${s.night_idle_min}"></label>
      </div>
      <div class="notice info">${switchHTML('f-night_shutdown', s.night_shutdown, 'Turn the PC off when the night uploads are done',
        'Only after a night upload, once nothing is left to send and the PC is still unused. Windows warns 1 minute before; moving the mouse cancels it.')}</div>
      </section>` : ''}
    <section class="card section"><h2>After a session</h2>
      <div class="form">
        <div class="field">${switchHTML('f-auto_scan', s.auto_scan, 'Scan automatically', 'Starts after a countdown unless you press Skip')}</div>
        <label class="field"><span>Countdown (seconds)</span><input id="f-scan_delay_s" type="number" min="5" max="600" value="${s.scan_delay_s}"></label>
        <label class="field wide"><span>Team hub address (optional)</span><input id="f-hub_url" type="url" value="${esc(s.hub_url)}" placeholder="http://pcs-hub:8420"><small>Leave empty to send results to Google Drive only.</small></label>
      </div></section>` : '<div class="notice">Auto-record isn\'t available in this build.</div>'}
    <section class="card section"><h2>About</h2>
      <div class="row between"><span class="muted">PCS Scanner ${esc(S.version)}${r && r.encoder ? ' · encoder ' + esc(r.encoder) : ''}</span>
        <span class="row"><button class="btn small ghost" id="f-results">Open results folder</button><button class="btn small ghost" id="f-quit">Quit PCS Scanner</button></span></div></section>`;
  const q = $('#f-bitrate_mbps');
  const qh = () => { if (!q) return; $('#q-val').textContent = q.value + ' Mb/s'; $('#q-hint').textContent = '≈ ' + ((q.value * 1000 + 480) * 3600 / 8 / 1e6).toFixed(1) + ' GB per hour'; };
  if (q) { q.oninput = qh; qh(); }
  $('#f-signin').onclick = () => { api('/api/signin', {}); toast('Your browser opens for the Google sign-in…'); };
  $('#f-results').onclick = () => api('/api/open', {what: 'results'});
  $('#f-quit').onclick = () => { if (confirmBox('Quit PCS Scanner? Auto-record stops until you open it again.')) api('/api/quit', {}); };
  $('#save').onclick = saveSettings;
  wireHotkeys();
  const ys = $('#f-ytsignin'); if (ys) ys.onclick = () => { api('/api/youtube/signin', {}); toast('Your browser opens: sign in and pick the team channel.'); };
  const cc = $('#f-comms_consent'), box = $('#commsBox');
  const syncComms = () => { if (box) { box.style.opacity = cc.checked ? 1 : .4; $$('input', box).forEach(i => i.disabled = !cc.checked); } };
  if (cc) { cc.onchange = syncComms; syncComms(); }
  const en = $('#f-enabled'); if (en) en.onchange = async () => {
    try { S = await api('/api/autorecord', {enabled: en.checked}); toast(en.checked ? 'Auto-record on.' : 'Auto-record off.'); renderTop(); }
    catch (e) { toast(e.message, true); en.checked = !en.checked; }
  };
}
async function saveSettings() {
  const body = {player: $('#f-player').value.trim(), role: $('#f-role').value};
  if (S.recorder) {
    for (const k of ['folder', 'hub_url', 'capture', 'yt_privacy', 'yt_audio', 'yt_when', 'yt_team']) { const el = $('#f-' + k); if (el) body[k] = el.value.trim(); }
    for (const k of ['keep_days', 'max_gb', 'bitrate_mbps', 'scan_delay_s', 'yt_gap_min', 'night_from', 'night_to', 'night_idle_min', 'yt_wait_eec_h']) { const el = $('#f-' + k); if (el) body[k] = parseInt(el.value, 10); }
    for (const k of ['start_with_windows', 'game_audio', 'auto_scan', 'comms_consent', 'discord_audio', 'mic_audio', 'send_mic', 'hk_sounds', 'yt_enabled', 'yt_scrims_only', 'night_shutdown', 'yt_wait_eec']) { const el = $('#f-' + k); if (el) body[k] = el.checked; }
    for (const k of ['hk_mark', 'hk_mute_game', 'hk_mute_discord', 'hk_mute_mic']) body[k] = $('#f-' + k).dataset.v || '';
  }
  try { S = await api('/api/settings', body); toast('Settings saved.'); renderSettings(); renderTop(); }
  catch (e) { toast('Not saved: ' + e.message, true); }
}

poll();
route();
