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
  const cb = $('#coachBadge'); if (cb) cb.hidden = !(S && S.coach);
  const u = $('#updateBtn'); u.hidden = !(S && S.update); if (S && S.update) u.textContent = 'Update to ' + S.update.tag;
  $$('#nav a').forEach(a => a.classList.toggle('on', a.dataset.page === (page === 'review' ? 'recordings' : page)));
  const yn = $('#nav a[data-page="youtube"]'); if (yn) yn.hidden = !(S && S.youtube && S.youtube.available);
  const tn = $('#nav a[data-page="team"]'); if (tn) tn.hidden = !(S && S.hub && S.hub.url);   // only with a hub address
}
// the window was opened again (from the tray): check for an update if the last check is old
document.addEventListener('visibilitychange', () => { if (!document.hidden) api('/api/update/check', {}).catch(() => {}); });
setInterval(() => { if (!document.hidden) api('/api/update/check', {}).catch(() => {}); }, 10 * 60 * 1000);   // window open: every 10 min
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
  else if (page === 'youtube') renderYouTube();
  else if (page === 'team') renderTeam();
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
  const n = {}; games.forEach(g => { n[g.map] = (n[g.map] || 0) + 1; });
  return Object.entries(n).map(([m, c]) => `<span class="chip">${esc(mapName(m))}${c > 1 ? ' ×' + c : ''}</span>`).join('');
}

/* ------------------------------------------------------------------ recordings */
let filter = 'all', kindFilter = '', selected = new Set();
const KINDS = {scrims: 'Scrims', ranked: 'Ranked', pubs: 'Pubs', wildcard: 'Wildcard'};
function labelSelect(label, byHand, attrs) {
  const auto = !byHand;
  return `<select class="label k-${label || 'none'} ${auto ? 'auto' : ''}" ${attrs} title="Session type. Auto = from the scan (scrim drop screen → Scrims, ranked badge → Ranked). Pubs and Wildcard are set by hand.">
    <option value="" ${auto ? 'selected' : ''}>${label ? KINDS[label] + ' · auto' : 'Type?'}</option>
    ${Object.entries(KINDS).map(([k, v]) => `<option value="${k}" ${byHand && k === label ? 'selected' : ''}>${v}</option>`).join('')}</select>`;
}
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
        <select id="kindFilt" class="btn" aria-label="Session type"><option value="">All types</option>${Object.entries(KINDS).map(([k, v]) => `<option value="${k}" ${k === kindFilter ? 'selected' : ''}>${v}</option>`).join('')}<option value="none" ${kindFilter === 'none' ? 'selected' : ''}>No type</option></select>
        ${S && S.coach ? '<button class="btn" id="add" title="Coach: import any recording or VOD (other teams, older games) to review and scan it">Import recordings…</button>' : ''}
        ${S && S.youtube && S.youtube.available ? '<button class="btn" id="eecRes" title="The lobby export from EEC (Download button on the lobby page): standing, points and kills go in the YouTube description">EEC results…</button>' : ''}
        <button class="btn" id="delSel" disabled>Delete selected</button>
        <button class="btn brand" id="scanSel" disabled>Scan selected</button>
      </div></div>
    <div class="drop" id="dropzone">${S && S.coach ? 'Drop recordings or folders here to import them' : 'Your games are added by auto-record'}${S && S.youtube && S.youtube.available ? ' · drop an EEC lobby export (.json) here for the YouTube description' : ''}</div>
    <section class="card" style="padding:6px 12px"><div class="list" id="list"><div class="empty">Loading…</div></div></section>
    <div class="row muted" style="font-size:13px"><button class="btn small ghost" id="openRes">Open results folder</button>
      ${S && S.recorder ? '<button class="btn small ghost" id="openRec">Open recordings folder</button>' : ''}</div>`;
  $$('#filt button').forEach(b => b.onclick = () => { filter = b.dataset.f; $$('#filt button').forEach(x => x.classList.toggle('on', x === b)); drawList(); });
  if ($('#add')) $('#add').onclick = async () => { const r = await api('/api/library/pick', {}).catch(e => toast(e.message, true)); if (r) { toast(r.added ? `Added ${r.added} recording(s).` : 'Nothing added.'); loadLibrary(); } };
  $('#kindFilt').onchange = e => { kindFilter = e.target.value; drawList(); };
  $('#scanSel').onclick = () => scan([...selected]);
  $('#delSel').onclick = () => deleteItems([...selected]);
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
  const items = LIB.filter(it => (filter === 'all' || (filter === 'scanned' ? !!it.scan : !it.scan)) &&
    (!kindFilter || (kindFilter === 'none' ? !it.label : it.label === kindFilter)));
  selected = new Set([...selected].filter(id => LIB.some(it => it.id === id)));
  if (!items.length) {
    el.innerHTML = `<div class="empty"><strong>${LIB.length ? 'Nothing here' : 'No recordings yet'}</strong>${LIB.length ? 'Try another filter.' : 'Turn on auto-record, or add recordings with the button or by dropping them here.'}</div>`;
  } else {
    el.innerHTML = `<div class="recrow head"><span></span><span>Recording</span><span class="c3">Recorded</span><span class="c4">Size</span><span class="c5">Status</span><span></span></div>` +
      items.map(it => `<div class="recrow" data-id="${it.id}">
        <input type="checkbox" ${selected.has(it.id) ? 'checked' : ''} aria-label="Select ${esc(it.name)}">
        <div style="min-width:0"><div class="name" title="${esc(it.path)}">${esc(it.name)}</div><div class="sub">${labelSelect(it.label, it.label_by_hand, 'data-label')}${gameChips(it)}${it.auto ? '<span class="faint" style="font-size:12px">auto-recorded</span>' : ''}${it.note ? `<span class="faint" style="font-size:12px">· ${esc(it.note)}</span>` : ''}</div></div>
        <span class="c3 muted">${fmtDate(it.mtime)}</span><span class="c4 mono muted">${fmtGB(it.size)}</span><span class="c5">${it.nogame ? '<span class="pill warn" title="The scan found no game in it (menus, lobby): deleted first, and after the time set in Settings">No game</span>' : statusPill(it.status)}${ytPill(it)}</span>
        <div class="acts">${['scanning', 'waiting', 'recording'].includes(it.status) ? '' : !it.scan ? '<button class="btn small" data-a="scan">Scan</button>' :
          `<button class="btn small ${it.status === 'failed' ? '' : 'ghost'}" data-a="rescan" title="Scan this recording again">Scan again</button>`}
          <a class="btn small ${it.scan ? 'brand' : ''}" href="#review/${it.id}">Review</a>
          ${it.auto && it.yt && !['done', 'uploading'].includes(it.yt.status) ? `<button class="btn small ghost" data-a="eec" title="EEC lobby link of this scrim block (goes in the YouTube description)">${it.yt.res ? `EEC ✓ ${ordinal(it.yt.res.standing)}/${it.yt.res.teams}` : it.yt.eec && it.yt.eec.length ? 'EEC ✓' : 'EEC link'}</button>` : ''}
          ${it.auto && S && S.youtube && S.youtube.available && !(it.yt && ['done', 'waiting', 'uploading'].includes(it.yt.status)) ? '<button class="btn small ghost" data-a="yt" title="Upload this recording\'s scrim block to the team YouTube channel">YouTube</button>' : ''}
          <button class="btn small ghost" data-a="show" title="Open the file">Open</button>
          <button class="btn small ghost danger" data-a="del" title="Delete this recording">Delete</button>
          ${!it.auto ? '<button class="btn small ghost" data-a="forget" title="Remove from this list (the file stays)">✕</button>' : ''}</div></div>`).join('');
  }
  $$('.recrow[data-id]', el).forEach(row => {
    const id = row.dataset.id;
    $('input', row).onchange = e => { e.target.checked ? selected.add(id) : selected.delete(id); syncSel(); };
    const ls = $('select[data-label]', row); if (ls) ls.onchange = async () => {
      try { await api('/api/label', {id, label: ls.value}); toast(ls.value ? `Labelled ${KINDS[ls.value]}.` : 'Type back to automatic.'); loadLibrary(); }
      catch (e) { toast(e.message, true); }
    };
    $$('[data-a]', row).forEach(b => b.onclick = async () => {
      if (b.dataset.a === 'scan') scan([id]);
      if (b.dataset.a === 'rescan') { try { const r = await api('/api/rescan', {ids: [id]}); toast(r.queued ? 'It will be scanned again (pauses by itself while Apex runs).' : 'Already being scanned.'); loadLibrary(); } catch (e) { toast(e.message, true); } }
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
      if (b.dataset.a === 'del') deleteItems([id]);
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
function syncSel() {
  const b = $('#scanSel'); if (b) { b.disabled = !selected.size; b.textContent = selected.size ? `Scan selected (${selected.size})` : 'Scan selected'; }
  const d = $('#delSel'); if (d) { d.disabled = !selected.size; d.textContent = selected.size ? `Delete selected (${selected.size})` : 'Delete selected'; }
}
async function deleteItems(ids) {
  const items = LIB.filter(x => ids.includes(x.id)); if (!items.length) return;
  const own = items.filter(x => x.auto), added = items.filter(x => !x.auto);
  const size = items.reduce((a, x) => a + x.size, 0);
  let msg = `Delete ${items.length} recording${items.length > 1 ? 's' : ''} (${fmtGB(size)})?\n\n`;
  if (own.length) msg += `• ${own.length} made by auto-record: deleted for good, with their scan files and sound tracks. Results already sent to the team stay with the team.\n`;
  if (added.length) msg += `• ${added.length} added by hand: moved to the Recycle Bin (you can restore them).\n`;
  if (!confirmBox(msg)) return;
  try {
    const r = await api('/api/library/delete', {ids});
    toast(r.deleted.length ? `Deleted ${r.deleted.length}, ${fmtGB(r.freed)} freed.` + (r.skipped.length ? ` Not deleted (busy): ${r.skipped.length}.` : '') :
      'Nothing deleted: ' + r.skipped.join(', '), !r.deleted.length);
    ids.forEach(i => selected.delete(i)); loadLibrary();
  } catch (e) { toast(e.message, true); }
}
async function scan(ids) {
  try { const r = await api('/api/scan', {ids}); toast(r.queued ? `${r.queued} recording(s) queued for scanning.` : 'Already scanning or queued.'); selected.clear(); loadLibrary(); }
  catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ review */
const KIND = {drop: ['D', 'Drop map'], jump: ['J', 'Jumped'], land: ['L', 'Landed'], ring: ['R', 'Ring'], ringclose: ['R', 'Ring'], feed: ['', 'Kill feed'], mark: ['★', 'Your mark'], mute: ['', 'Mute'], fight: ['', 'Fight'], call: ['!', 'Loud call']};
const TRACKN = {game: 'Game sound', discord: 'Voice app', mic: 'Microphone'};
const VOICE_NAMES = {discord: 'Discord', teamspeak: 'TeamSpeak'};
function voiceName(s) {          /* the voice app recorded on track 2, as the player calls it */
  if (s.voice_app === 'other') return (s.voice_exe || '').trim().replace(/\.exe$/i, '') || 'Voice app';
  return VOICE_NAMES[s.voice_app] || 'Discord';
}
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
  if (RV && RV.ro) RV.ro.disconnect();
  RV = {d, ev, spans, tab: d.games ? 'events' : 'events', dur: 0};
  $('#main').innerHTML = `
    <div class="pagehead"><div><h1>${esc(fmtDate(d.mtime))}</h1><p class="row" style="gap:8px">${gameChips(d)} ${statusPill(d.status)} <span class="faint mono" title="${esc(d.path)}">${esc(d.name)}</span></p></div>
      <div class="toolbar"><a class="btn ghost" href="#recordings">← Recordings</a>
        ${!d.scan ? '<button class="btn brand" id="rvScan">Scan this recording</button>' : ''}
        <button class="btn" id="rvOpen">Open file</button></div></div>
    <div class="review">
      <div>
        <div class="player" id="player"><video id="vid" controls controlslist="nofullscreen" preload="metadata" src="/video/${d.id}?t=${encodeURIComponent(TOKEN)}"></video>
          <canvas class="ink" id="ink" hidden></canvas><div class="cnote" id="cnote" hidden></div>
          <div class="seen" id="seen" ${d.scan ? '' : 'hidden'}></div>
          <div class="err" id="vidErr" hidden><div><h2>This recording can't play here</h2><p class="muted">Its video format isn't supported by the built-in player (usually HEVC/H.265).<br>Open it in your usual video player instead.</p><button class="btn brand" id="vidOpen">Open file</button></div></div></div>
        <div class="card coachbar" id="coachBar" hidden></div>
        <div class="card" style="margin-top:12px;padding:12px 16px 14px">
          <div class="tl" id="tl"><div class="lanes">
            <div class="lane games" id="laneG"><span class="lane-label">Games</span></div>
            <div class="lane" id="laneE"><span class="lane-label">Drop · jump · landing · rings</span></div>
            <div class="lane" id="laneF"><span class="lane-label">Kill feed</span></div>
            <div class="lane sound" id="laneS"><span class="lane-label">Sound · <span id="soundState">analysing…</span></span></div>
            ${(d.marks || []).length ? '<div class="lane" id="laneM"><span class="lane-label">Your marks · muted sound</span></div>' : ''}
            <div class="lane" id="laneN" hidden><span class="lane-label">Coach notes</span></div>
          </div><div class="head-line" id="playhead" style="left:0"></div><div class="hover-time" id="hoverT" hidden></div></div>
          <div class="row between" style="margin-top:8px">
            <div class="legend"><span><i style="background:#e6ad45"></i>Drop map</span><span><i style="background:#6aa0ff"></i>Jump</span><span><i style="background:#5fd08f"></i>Landing</span><span><i style="background:#c48bff"></i>Ring</span><span><i style="background:var(--accent);border-radius:2px;width:4px"></i>Kill feed</span><span><i style="background:#e6ad45;border-radius:2px"></i>Loud game sound (likely fight)</span><span><i style="background:#6aa0ff"></i>Loud call</span></div>
            <div class="row"><select id="speed" style="width:auto;padding:4px 8px">${[0.25, 0.5, 1, 1.5, 2, 4].map(s => `<option value="${s}" ${s === 1 ? 'selected' : ''}>${s}×</option>`).join('')}</select>
              <button class="btn small" id="rvFull" title="Full screen (F)">Full screen</button></div>
          </div>
          <div id="mixer"></div>
          <div class="keys" style="margin-top:8px"><kbd>Space</kbd> play/pause · <kbd>←</kbd><kbd>→</kbd> 5 s · <kbd>J</kbd><kbd>L</kbd> 10 s · <kbd>,</kbd><kbd>.</kbd> one frame · <kbd>[</kbd><kbd>]</kbd> previous/next event · <kbd>F</kbd> full screen · <kbd>M</kbd> mute · <kbd>V</kbd> scanner view on/off</div>
        </div>
      </div>
      <aside class="card side">
        <div class="tabs" id="rvTabs"><button data-t="events" class="on">Events</button><button data-t="feed">Kill feed (${(d.feed || []).length})</button><button data-t="drop">Drop</button><button data-t="notes" id="tabNotes" hidden>Coach notes</button></div>
        <div class="scroll" id="rvBody"></div>
      </aside>
    </div>`;
  const v = $('#vid');
  $('#rvOpen').onclick = $('#vidOpen').onclick = () => api('/api/open', {what: 'file', id: d.id});
  const rs = $('#rvScan'); if (rs) rs.onclick = () => scan([d.id]);
  v.addEventListener('error', () => { $('#vidErr').hidden = false; });
  setupAudio(d, v);
  v.addEventListener('loadedmetadata', () => { RV.dur = v.duration; drawTimeline(); drawSound(); });
  try { if (localStorage.getItem('pcs-seen') === '0') $('#seen').hidden = true; } catch (x) {}
  v.addEventListener('timeupdate', onTime);
  $('#speed').onchange = e => v.playbackRate = +e.target.value;
  $$('#rvTabs button').forEach(b => b.onclick = () => { RV.tab = b.dataset.t; $$('#rvTabs button').forEach(x => x.classList.toggle('on', x === b)); drawSide(); });
  const tl = $('#tl');
  const tAt = e => { const r = $('#laneG').getBoundingClientRect(); return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * dur(); };
  tl.addEventListener('mousemove', e => { const h = $('#hoverT'); h.hidden = false; h.textContent = fmtT(tAt(e)); h.style.left = (tAt(e) / dur() * 100) + '%'; });
  tl.addEventListener('mouseleave', () => $('#hoverT').hidden = true);
  tl.addEventListener('click', e => { const t = e.target.dataset && e.target.dataset.t; seek(t != null ? +t : tAt(e)); });
  drawTimeline(); drawSide();
  loadLevels(d.id);
  $('#rvFull').onclick = fullScreen;
  setupInk(v);
  loadNotes(d.id);
}
function fullScreen() {           /* the player box, not the bare video: the coach's drawings stay on top */
  const p = $('#player'); if (!p) return;
  document.fullscreenElement ? document.exitFullscreen() : p.requestFullscreen().catch(() => {});
}
function dur() {
  if (RV.dur) return RV.dur;
  const last = Math.max(0, ...RV.ev.map(e => e.t), ...RV.spans.map(s => s.to || 0));
  return last ? last * 1.05 : 600;
}
function seek(t) {
  const v = $('#vid'); if (!v || RV.ed) return;          // drawing a note: stay on its frame
  v.currentTime = Math.max(0, t - (seek.lead || 0)); onTime();
}
function drawTimeline() {
  if (!RV || !$('#laneG')) return;
  const D = dur(), pct = t => (t / D * 100).toFixed(3) + '%';
  $('#laneG').innerHTML = '<span class="lane-label">Games</span>' + (RV.spans.length ? RV.spans.map(s =>
    `<div class="segm" data-t="${s.from}" style="left:${pct(s.from)};width:calc(${pct(Math.max(1, s.to - s.from))})" title="${esc(mapName(s.map))} · ${fmtT(s.from)}–${fmtT(s.to)}">${esc(mapName(s.map))}${s.mode ? ' · ' + esc(s.mode) : ''}</div>`).join('') :
    `<span class="faint" style="position:absolute;left:8px;top:4px;font-size:12px">${RV.d.scan ? 'No game found in this recording' : 'Not scanned yet: scan it to see games and events here'}</span>`);
  $('#laneE').innerHTML = '<span class="lane-label">Drop · jump · landing · rings</span>' + RV.ev.filter(e => !['feed', 'mark', 'mute', 'fight', 'call'].includes(e.k)).map(e =>
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
  const ln = $('#laneN'), notes = (RV.notes && RV.notes.notes) || [];
  if (ln) {
    ln.hidden = !notes.length;
    ln.innerHTML = '<span class="lane-label">Coach notes</span>' + notes.map(n =>
      `<button class="mk note" data-t="${n.t}" style="left:${pct(n.t)}" title="${fmtT(n.t)} · ${esc(noteTitle(n))}">C</button>`).join('');
  }
}
function drawSide() {
  const body = $('#rvBody'); if (!body) return;
  const d = RV.d;
  if (RV.tab === 'notes') { drawNotes(body); onTime(true); return; }
  if (!d.scan) { body.innerHTML = '<div class="empty"><strong>Not scanned yet</strong>Scan this recording to see its drop, landing, rings and kill feed.</div>'; return; }
  if (RV.tab === 'events') {
    let html = '', g = -1;
    RV.ev.forEach((e, i) => {
      if (e.k === 'feed') return;                     // kill feed lines have their own tab
      if (e.g != null && e.g !== g) { g = e.g; const sp = RV.spans[g]; html += `<div class="gamehead">Game ${g + 1} · ${esc(mapName(sp.map))}</div>`; }
      const col = {drop: '#e6ad45', jump: '#6aa0ff', land: '#5fd08f', ring: '#c48bff', ringclose: '#8f5ee0', feed: 'var(--accent)', mark: '#f1efea', mute: '#6d6a63', fight: '#e6ad45', call: '#6aa0ff'}[e.k];
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
  drawSeen(t);
  showNotesAt(t);
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
  if (RV.ed) { if ((e.ctrlKey || e.metaKey) && (e.key === 'z' || e.key === 'Z')) { inkUndo(); e.preventDefault(); } return; }   // drawing a note: the video stays paused
  const k = e.key, step = d => { v.currentTime = Math.max(0, Math.min(v.duration || 1e9, v.currentTime + d)); };
  const times = RV.ev.filter(x => x.k !== 'feed').map(x => x.t);
  if (k === ' ' || k === 'k' || k === 'K') { v.paused ? v.play() : v.pause(); }
  else if (k === 'ArrowLeft') step(-5); else if (k === 'ArrowRight') step(5);
  else if (k === 'j' || k === 'J') step(-10); else if (k === 'l' || k === 'L') step(10);
  else if (k === ',') { v.pause(); step(-1 / 60); } else if (k === '.') { v.pause(); step(1 / 60); }
  else if (k === ']') { const n = times.find(t => t > v.currentTime + 0.3); if (n != null) seek(n); }
  else if (k === '[') { const p = times.filter(t => t < v.currentTime - 1).pop(); seek(p != null ? p : 0); }
  else if (k === 'f' || k === 'F') fullScreen();
  else if (k === 'v' || k === 'V') { const o = $('#seen'); if (o && RV.d.scan) { o.hidden = !o.hidden; try { localStorage.setItem('pcs-seen', o.hidden ? '0' : '1'); } catch (x) {} } }
  else if (k === 'm' || k === 'M') { if (RV.aud) { const all = RV.aud.every(x => x.mute); RV.aud.forEach(x => x.mute = !all); applyMix(); drawMixer(); } else v.muted = !v.muted; }
  else return;
  e.preventDefault();
});

/* ------------------------------------------------------------------ what the scanner sees (overlay on the video) */
function drawSeen(t) {
  const o = $('#seen'); if (!o || o.hidden || !RV.d.games) return;
  const games = RV.d.games, spans = RV.spans;
  let gi = -1;
  spans.forEach((sp, i) => { if (t >= sp.from - 5 && t <= sp.to + 5) gi = i; });
  if (gi < 0) { o.innerHTML = '<b>Lobby / menus</b><span>No game on screen</span>'; return; }
  const g = games[gi], L = g.landing || {};
  // the scanner's reading for this second (at most 3 s old)
  let s = null;
  for (const row of g.seen || []) { if (row[0] <= t + 0.5) s = row; else break; }
  if (s && t - s[0] > 3) s = null;
  let phase = 'In game';
  if (L.exitAtExact != null && t < L.exitAtExact) phase = t < g.tStart ? 'Loading' : 'On the dropship';
  else if (L.landedAtExact != null && t < L.landedAtExact) phase = 'Dropping';
  if (s && s[2]) phase = 'Spectating (eliminated)';
  const rows = [`<b>Game ${gi + 1} · ${esc(mapName(g.map))}</b>`, `<span>${phase}</span>`];
  for (const r of g.ring || []) {
    const from = r.appearedAt ?? r.firstSeen;
    if (r.kind === 'wait' && t >= from && t <= r.endsAt) rows.push(`<span class="ring">Ring${r.round ? ' ' + r.round : ''} starts closing in ${fmtT(r.endsAt - t)}</span>`);
    if (r.kind === 'closing' && t >= r.firstSeen && t <= r.endsAt) rows.push(`<span class="ring">Ring closing · ends in ${fmtT(r.endsAt - t)}</span>`);
  }
  if (s && s[3] != null) {
    const kmh = s[6] != null ? Math.round(s[6] * 0.0254 * 3.6) : null;
    rows.push(`<span class="mono">x ${Math.round(s[3])} · y ${Math.round(s[4])} · z ${Math.round(s[5])}${kmh != null ? ` · ${kmh} km/h` : ''}</span>`);
  }
  const lv = RV.levels;
  if (lv) {
    if ((lv.fights || []).some(([a, b]) => t >= a && t <= b)) rows.push('<span class="hot">Loud game sound: likely a fight</span>');
    if ((lv.calls || []).some(c => t >= c && t < c + 3)) rows.push('<span class="hot blue">Loud call on the mic</span>');
  }
  const f = (RV.d.feed || []).filter(l => l.t <= t && t - l.t < 4).slice(-3);
  if (f.length) rows.push('<span class="feedimgs">' + f.map(l => `<img alt="Kill feed line" src="data:image/jpeg;base64,${l.jpg}">`).join('') + '</span>');
  o.innerHTML = rows.join('');
}

/* ------------------------------------------------------------------ coach notes and drawings (team hub) */
const INK = ['#ff3b30', '#ffd60a', '#30d158', '#0a84ff', '#ffffff'];
const TOOLS = [['pen', 'Pen'], ['arrow', 'Arrow'], ['circle', 'Circle'], ['rect', 'Box']];
const isCoachKey = () => !!(RV && RV.notes && RV.notes.me && RV.notes.me.role === 'coach');
const noteTitle = n => n.text ? n.text.split('\n')[0].slice(0, 80) : 'Drawing';

async function loadNotes(id, player) {
  clearTimeout(loadNotes.h);
  let r;
  try { r = await api(`/api/library/${id}/notes` + (player ? '?player=' + encodeURIComponent(player) : '')); }
  catch (e) { r = {available: false, notes: []}; }
  if (!RV || RV.d.id !== id || page !== 'review') return;
  setNotes(r);
  if (r.offline) loadNotes.h = setTimeout(() => { if (!RV.ed) loadNotes(id, player); }, 60000);   // hub back: refresh
}
function setNotes(r) {
  const first = !RV.notes;
  RV.notes = r; RV.inkKey = null;
  const tab = $('#tabNotes'), n = (r.notes || []).length;
  if (tab) { tab.hidden = !(r.available && (n || isCoachKey())); tab.textContent = `Coach notes (${n})`; }
  if (first && n && tab && !tab.hidden) { RV.tab = 'notes'; $$('#rvTabs button').forEach(x => x.classList.toggle('on', x === tab)); }
  if (RV.tab === 'notes' && tab && tab.hidden) { RV.tab = 'events'; $$('#rvTabs button').forEach(x => x.classList.toggle('on', x.dataset.t === 'events')); }
  drawTimeline(); drawSide(); fitInk();
}
function drawNotes(body) {
  const r = RV.notes || {notes: []}, coach = isCoachKey(), notes = r.notes || [];
  let html = '';
  if (r.offline) html += `<div class="notice" style="margin:4px 0 10px">Hub not reachable: notes saved earlier.</div>`;
  if (coach) {
    const pl = r.players || [];
    html += '<div class="nhead">' + (pl.length > 1 ? `<label class="row" style="gap:6px">Player <select id="nPlayer" style="width:auto;padding:4px 8px">${pl.map(p => `<option ${p === r.player ? 'selected' : ''}>${esc(p)}</option>`).join('')}</select></label>` :
      (r.known ? `<span class="muted">${esc(r.player)}'s recording</span>` : '')) +
      (r.known && !r.offline ? `<button class="btn small brand" id="nNew" ${RV.ed ? 'disabled' : ''}>New note</button>` : '') + '</div>';
    if (!r.known && !r.offline) html += `<div class="notice" style="margin:4px 0 10px">This recording isn't on the hub yet. Its scan must be sent to the hub first, from the app of the player who recorded it (same file name).</div>`;
  }
  html += notes.map(n => {
    const mine = !n.from, seen = n.seenBy ? Object.keys(n.seenBy) : null;
    const sub = [n.from ? `From ${esc(n.from.player)}'s recording` : (n.scope === 'squad' ? 'Squad' : (coach ? 'This recording' : '')),
      n.drawings && n.drawings.length ? 'drawing' : '', `${Math.round(n.dur)} s`,
      coach && seen ? (seen.length ? 'Seen by ' + esc(seen.join(', ')) : 'Not seen yet') : ''].filter(Boolean).join(' · ');
    return `<div class="ev note ${RV.ed && RV.ed.id === n.id ? 'editing' : ''}" data-t="${n.t}" data-id="${esc(n.id)}"><time>${fmtT(n.t)}</time><i style="background:${(n.drawings && n.drawings[0] && n.drawings[0].color) || '#ffd60a'}"></i>
      <span><b>${esc(n.text || 'Drawing')}</b><small>${sub}</small>${coach && mine && !r.offline ? `<span class="row nacts"><button class="btn small" data-a="edit">Edit</button><button class="btn small danger" data-a="del">Delete</button></span>` : ''}</span></div>`;
  }).join('') || `<div class="empty">${coach ? 'No notes yet. Pause where you want one and press New note.' : 'No coach notes on this recording.'}</div>`;
  body.innerHTML = html;
  $$('.ev.note', body).forEach(el => el.onclick = e => {
    const n = notes.find(x => x.id === el.dataset.id), a = e.target.dataset && e.target.dataset.a;
    if (a === 'edit') return openEditor(n);
    if (a === 'del') return deleteNote(n);
    if (!RV.ed) seek(n.t);
    markSeen([n]);
  });
  const nn = $('#nNew', body); if (nn) nn.onclick = () => openEditor(null);
  const np = $('#nPlayer', body); if (np) np.onchange = () => { closeEditor(); loadNotes(RV.d.id, np.value); };
}

/* the drawing layer: a canvas exactly over the picture (the video keeps its shape inside its box) */
function setupInk(v) {
  const c = $('#ink');
  if (window.ResizeObserver) { RV.ro = new ResizeObserver(() => fitInk()); RV.ro.observe(v); }
  v.addEventListener('loadedmetadata', fitInk);
  c.addEventListener('pointerdown', e => {
    if (!RV.ed) return;
    c.setPointerCapture(e.pointerId);
    const p = inkPoint(e); RV.ed.cur = {kind: RV.ed.tool, color: RV.ed.color, pts: [p, p]};
    if (RV.ed.tool === 'pen') RV.ed.cur.pts = [p];
    paintInk();
  });
  c.addEventListener('pointermove', e => {
    const s = RV.ed && RV.ed.cur; if (!s) return;
    const p = inkPoint(e);
    if (s.kind === 'pen') { const q = s.pts[s.pts.length - 1]; if (Math.hypot(p[0] - q[0], p[1] - q[1]) > 0.002 && s.pts.length < 2000) s.pts.push(p); }
    else s.pts[1] = p;
    paintInk();
  });
  const end = () => {
    const s = RV.ed && RV.ed.cur; if (!s) return;
    RV.ed.cur = null;
    const [a, b] = [s.pts[0], s.pts[s.pts.length - 1]];
    if (s.pts.length > 1 && (s.kind === 'pen' || Math.hypot(b[0] - a[0], b[1] - a[1]) > 0.01) && RV.ed.drawings.length < 40) RV.ed.drawings.push(s);
    paintInk();
  };
  c.addEventListener('pointerup', end); c.addEventListener('pointercancel', end);
}
function inkPoint(e) {
  const r = $('#ink').getBoundingClientRect();
  const f = x => Math.round(Math.max(0, Math.min(1, x)) * 10000) / 10000;
  return [f((e.clientX - r.left) / r.width), f((e.clientY - r.top) / r.height)];
}
function fitInk() {
  const v = $('#vid'), c = $('#ink'); if (!v || !c || !RV) return;
  const vw = v.videoWidth, vh = v.videoHeight, bw = v.clientWidth, bh = v.clientHeight;
  const any = RV.ed || ((RV.notes && RV.notes.notes) || []).some(n => n.drawings && n.drawings.length);
  if (!vw || !vh || !bw || !bh || !any) { c.hidden = true; return; }
  const k = Math.min(bw / vw, bh / vh), w = vw * k, h = vh * k, dpr = window.devicePixelRatio || 1;
  Object.assign(c.style, {left: (v.offsetLeft + (bw - w) / 2) + 'px', top: (v.offsetTop + (bh - h) / 2) + 'px', width: w + 'px', height: h + 'px'});
  c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
  c.hidden = false; RV.inkKey = null;
  paintInk();
}
function activeNotes(t) {
  return ((RV.notes && RV.notes.notes) || []).filter(n => t >= n.t - 0.05 && t < n.t + n.dur);
}
function showNotesAt(t) {          /* on each timeupdate: repaints only when the set of notes on screen changes */
  if (!RV || RV.ed) return;
  const act = activeNotes(t), key = act.map(n => n.id + ':' + n.version).join(',');
  if (key === RV.inkKey) return;
  RV.inkKey = key;
  paintInk(act);
  const box = $('#cnote'), txt = act.filter(n => n.text);
  if (box) { box.hidden = !txt.length; box.innerHTML = txt.map(n => `<span><b>Coach</b>${esc(n.text)}</span>`).join(''); }
  if (act.length && !$('#vid').paused) markSeen(act);
}
function paintInk(act) {
  const c = $('#ink'); if (!c || c.hidden) return;
  const g = c.getContext('2d'), W = c.width, H = c.height;
  g.clearRect(0, 0, W, H);
  const shapes = RV.ed ? RV.ed.drawings.concat(RV.ed.cur ? [RV.ed.cur] : []) :
    (act || activeNotes($('#vid').currentTime)).flatMap(n => n.drawings || []);
  const lw = Math.max(2, W * 0.0035);
  g.lineWidth = lw; g.lineCap = g.lineJoin = 'round'; g.shadowColor = 'rgba(0,0,0,.7)'; g.shadowBlur = lw;
  shapes.forEach(s => {
    const P = s.pts.map(([x, y]) => [x * W, y * H]);
    g.strokeStyle = s.color; g.beginPath();
    if (s.kind === 'pen') { P.forEach(([x, y], i) => i ? g.lineTo(x, y) : g.moveTo(x, y)); if (P.length === 1) g.lineTo(P[0][0] + .1, P[0][1]); }
    else {
      const [[x1, y1], [x2, y2]] = [P[0], P[P.length - 1]];
      if (s.kind === 'rect') g.rect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
      else if (s.kind === 'circle') g.ellipse((x1 + x2) / 2, (y1 + y2) / 2, Math.abs(x2 - x1) / 2, Math.abs(y2 - y1) / 2, 0, 0, Math.PI * 2);
      else {
        const a = Math.atan2(y2 - y1, x2 - x1), hl = Math.max(lw * 4, Math.min(W * 0.03, Math.hypot(x2 - x1, y2 - y1) / 2));
        g.moveTo(x1, y1); g.lineTo(x2, y2);
        g.moveTo(x2 - hl * Math.cos(a - 0.45), y2 - hl * Math.sin(a - 0.45)); g.lineTo(x2, y2); g.lineTo(x2 - hl * Math.cos(a + 0.45), y2 - hl * Math.sin(a + 0.45));
      }
    }
    g.stroke();
  });
}
function markSeen(list) {
  if (isCoachKey() || !RV.notes || RV.notes.offline) return;
  RV.seenSent = RV.seenSent || new Set();
  const ids = list.filter(n => !n.seen && !RV.seenSent.has(n.id)).map(n => n.id);
  if (!ids.length) return;
  ids.forEach(i => RV.seenSent.add(i));
  api('/api/notes/seen', {id: RV.d.id, note_ids: ids}).catch(() => ids.forEach(i => RV.seenSent.delete(i)));
}

/* the coach's editor: pause, draw, write, save */
function openEditor(n) {
  const v = $('#vid'); if (!v) return;
  v.pause();
  if (n) v.currentTime = n.t;
  let last = {tool: 'pen', color: INK[0]};
  try { last = JSON.parse(localStorage.getItem('pcs-ink') || 'null') || last; } catch (x) {}
  RV.ed = {id: n ? n.id : null, version: n ? n.version : null, t: n ? n.t : Math.round(v.currentTime * 100) / 100,
           dur: n ? n.dur : 5, text: n ? n.text : '', scope: n ? n.scope : 'recording',
           drawings: n ? JSON.parse(JSON.stringify(n.drawings || [])) : [], tool: last.tool, color: last.color, cur: null};
  $('#player').classList.add('drawing');
  $('#cnote').hidden = true;
  drawEditor(); fitInk(); drawSide();
}
function closeEditor() {
  if (!RV || !RV.ed) return;
  RV.ed = null; RV.inkKey = null;
  const p = $('#player'); if (p) p.classList.remove('drawing');
  const bar = $('#coachBar'); if (bar) { bar.hidden = true; bar.innerHTML = ''; }
  fitInk(); drawSide(); onTime(true);
}
function inkUndo() { if (RV.ed) { RV.ed.drawings.pop(); paintInk(); } }
function drawEditor() {
  const bar = $('#coachBar'), e = RV.ed; if (!bar || !e) return;
  bar.hidden = false;
  bar.innerHTML = `<div class="row" style="gap:10px">
      <b class="edtitle">${e.id ? 'Edit note' : 'New note'} at ${fmtT(e.t)}</b>
      <div class="seg" id="edTool">${TOOLS.map(([k, l]) => `<button data-k="${k}" class="${e.tool === k ? 'on' : ''}">${l}</button>`).join('')}</div>
      <div class="swatches" id="edColor">${INK.map(c => `<button data-c="${c}" class="${e.color === c ? 'on' : ''}" style="background:${c}" aria-label="Colour ${c}"></button>`).join('')}</div>
      <button class="btn small" id="edUndo">Undo</button><button class="btn small" id="edClear">Clear</button></div>
    <textarea id="edText" rows="2" maxlength="2000" placeholder="Note for the player (optional with a drawing)">${esc(e.text)}</textarea>
    <div class="row" style="gap:10px">
      <div class="seg" id="edScope"><button data-s="recording" class="${e.scope === 'recording' ? 'on' : ''}">This recording</button><button data-s="squad" class="${e.scope === 'squad' ? 'on' : ''}">Squad</button></div>
      <label class="row" style="gap:6px">Shows for <input type="number" id="edDur" min="1" max="120" step="1" value="${Math.round(e.dur)}" style="width:70px;padding:5px 8px"> s</label>
      <span style="flex:1"></span>
      <button class="btn small ghost" id="edCancel">Cancel</button><button class="btn small brand" id="edSave">Save</button></div>
    <small class="faint">Draw on the picture. ${e.scope === 'squad' ? 'Squad: the text also shows on your players\' recordings of this game, at the same moment. Drawings stay on this recording.' : 'Only on this recording.'}</small>`;
  const remember = () => { try { localStorage.setItem('pcs-ink', JSON.stringify({tool: e.tool, color: e.color})); } catch (x) {} };
  $$('#edTool button', bar).forEach(b => b.onclick = () => { e.tool = b.dataset.k; remember(); drawEditor(); });
  $$('#edColor button', bar).forEach(b => b.onclick = () => { e.color = b.dataset.c; remember(); drawEditor(); });
  $$('#edScope button', bar).forEach(b => b.onclick = () => { e.scope = b.dataset.s; drawEditor(); });
  $('#edText', bar).oninput = x => e.text = x.target.value;
  $('#edDur', bar).oninput = x => e.dur = Math.max(1, Math.min(120, +x.target.value || 5));
  $('#edUndo', bar).onclick = inkUndo;
  $('#edClear', bar).onclick = () => { e.drawings = []; paintInk(); };
  $('#edCancel', bar).onclick = closeEditor;
  $('#edSave', bar).onclick = saveNote;
}
async function saveNote() {
  const e = RV.ed; if (!e) return;
  if (!e.text.trim() && !e.drawings.length) return toast('Write a note or draw something first.', true);
  const btn = $('#edSave'); btn.disabled = true;
  try {
    const r = await api('/api/notes/save', {id: RV.d.id, player: RV.notes.player,
      note: {id: e.id, version: e.version, t: e.t, dur: e.dur, text: e.text.trim(), scope: e.scope, drawings: e.drawings.map(({kind, color, pts}) => ({kind, color, pts}))}});
    closeEditor(); setNotes(r); toast('Note saved. Your players see it in their review.');
  } catch (x) { btn.disabled = false; toast(x.message, true); }
}
async function deleteNote(n) {
  if (!confirmBox('Delete this note? Players stop seeing it.')) return;
  try { setNotes(await api('/api/notes/delete', {id: RV.d.id, player: RV.notes.player, note_id: n.id})); toast('Note deleted.'); }
  catch (x) { toast(x.message, true); }
}

/* ------------------------------------------------------------------ sound analysis (loudness per track) */
async function loadLevels(id) {
  const st = () => $('#soundState');
  let lv;
  try { lv = await api(`/api/library/${id}/levels`); } catch (e) { if (st()) st().textContent = 'not available'; return; }
  if (!RV || RV.d.id !== id) return;
  RV.levels = lv;
  if (st()) st().textContent = lv.tracks.map(t => t.label).join(' · ') + (lv.talk != null ? ` · mic active ${Math.round(lv.talk * 100)}% of the time` : '');
  (lv.fights || []).forEach(([a, b]) => RV.ev.push({t: a, k: 'fight', title: 'Loud game sound (likely a fight)', sub: `${fmtT(a)}–${fmtT(b)}`}));
  (lv.calls || []).forEach(c => RV.ev.push({t: c, k: 'call', title: 'Loud call on the mic'}));
  RV.ev.sort((a, b) => a.t - b.t);
  drawSound(); drawSide();
}
function drawSound() {
  const lane = $('#laneS'), lv = RV && RV.levels; if (!lane || !lv) return;
  const D = dur(), w = lane.clientWidth, h = lane.clientHeight;
  $$('canvas, .fight, .mk.call', lane).forEach(x => x.remove());
  const cv = document.createElement('canvas'); cv.width = w * 2; cv.height = h * 2; cv.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none';
  const c = cv.getContext('2d'), colors = {game: 'rgba(230,173,69,.55)', discord: 'rgba(196,139,255,.5)', mic: 'rgba(106,160,255,.6)'};
  lv.tracks.forEach((tr, i) => {
    c.fillStyle = colors[tr.key] || 'rgba(200,200,200,.4)'; c.beginPath(); c.moveTo(0, cv.height);
    const n = tr.level.length;
    for (let x = 0; x < cv.width; x++) {
      const t = x / cv.width * D, j = Math.min(n - 1, Math.floor(t / lv.step));
      const v = j >= 0 ? tr.level[j] / 255 : 0;
      c.lineTo(x, cv.height - v * cv.height * 0.95);
    }
    c.lineTo(cv.width, cv.height); c.closePath(); c.fill();
  });
  lane.appendChild(cv);
  const pct = t => (t / D * 100).toFixed(3) + '%';
  lane.insertAdjacentHTML('beforeend', (lv.fights || []).map(([a, b]) => `<div class="fight" data-t="${a}" style="left:${pct(a)};width:${pct(b - a)}" title="${fmtT(a)}–${fmtT(b)} · loud game sound, likely a fight"></div>`).join('') +
    (lv.calls || []).map(c2 => `<button class="mk call" data-t="${c2}" style="left:${pct(c2)}" title="${fmtT(c2)} · loud call">!</button>`).join(''));
}
window.addEventListener('resize', () => { if (page === 'review') drawSound(); });

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
    <small>Click, then press the key(s): e.g. F5, Shift+F8, Ctrl+Alt+M. Backspace = none.</small></label>`;
}
function wireHotkeys() {
  $$('input.hk').forEach(inp => {
    inp.onfocus = () => { inp.value = 'Press keys…'; };
    inp.onblur = () => { inp.value = hotkeyText(inp.dataset.v); };
    inp.onkeydown = e => {
      e.preventDefault();
      if (e.key === 'Backspace' || e.key === 'Delete') { inp.dataset.v = ''; inp.blur(); return; }
      const SPECIAL = {Pause: 'pause', ScrollLock: 'scrolllock', Insert: 'insert', Home: 'home', End: 'end', PageUp: 'pageup',
        PageDown: 'pagedown', NumpadAdd: 'numadd', NumpadSubtract: 'numsub', NumpadMultiply: 'nummul', NumpadDivide: 'numdiv', NumpadDecimal: 'numdot'};
      let key = '', alone = false;
      if (/^Digit\d$/.test(e.code)) key = e.code.slice(5);
      else if (/^Key[A-Z]$/.test(e.code)) key = e.code.slice(3).toLowerCase();
      else if (/^F\d{1,2}$/.test(e.code)) { key = e.code.toLowerCase(); alone = true; }
      else if (/^Numpad\d$/.test(e.code)) { key = 'num' + e.code.slice(6); alone = true; }
      else if (SPECIAL[e.code]) { key = SPECIAL[e.code]; alone = true; }
      if (!key) return;                                  // a modifier on its own: wait for the key
      if (!alone && !(e.ctrlKey || e.altKey || e.metaKey)) {
        toast('Letters and numbers need Ctrl, Alt or Win. F-keys, numpad, Pause, Insert, Home, End, Page Up/Down work alone.', true); return;
      }
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
        <label class="field"><span>Role</span><select id="f-role">${['', 'IGL', 'Fragger', 'Anchor', 'Support', 'Sub', 'Coach'].map(x => `<option ${x === S.role ? 'selected' : ''}>${x}</option>`).join('')}</select>
          <small>${S.coach ? 'Coach tools are on: import any recording or VOD, and more to come.' : 'Coach needs the coach\'s Google account.'}</small></label>
        <div class="field"><span class="muted" style="font-size:12px;letter-spacing:.06em;text-transform:uppercase">Google Drive</span>
          <div class="row" style="margin-top:4px">${S.google === 'ok' ? '<span class="pill good">Signed in</span>' : '<span class="pill warn">Not signed in</span>'}
          <button class="btn small" id="f-signin">${S.google === 'ok' ? 'Sign in again' : 'Sign in with Google'}</button></div></div>
        <label class="field wide"><span>Team link</span><input id="f-team_link" type="text" value="${esc(S.teamLink || '')}" placeholder="Paste the Google Drive link from your coach">
          <small>Lets the scan name the weapons in the kill feed. ${S.icons ? 'Weapon icons: loaded ' + new Date(S.icons * 1000).toLocaleDateString() + '.' : 'Weapon icons: not loaded yet.'} Keep this link inside the team.</small></label>
      </div></section>
    ${r ? `
    <section class="card section"><h2>Team hub</h2>
      <div class="form">
        <label class="field wide"><span>Hub address</span><input id="f-hub_url" type="url" value="${esc(s.hub_url)}" placeholder="http://pcs-hub:8420"><small>From your coach. Empty = results go to Google Drive only.</small></label>
        <div class="field wide hubfield"><span>Key</span><div id="hubKey"></div><small>Your own key, or the sign-in link your coach sent. Don't share it.</small></div>
      </div></section>
    <section class="card section"><h2>Start-up</h2>
      ${switchHTML('f-start_with_windows', s.start_with_windows, 'Start with Windows', 'PCS Scanner waits in the tray (bottom right) from the moment you log in. Recommended: you never have to think about it.')}
      ${switchHTML('f-enabled', r.enabled, 'Auto-record', 'Records the Apex window while the game runs, then offers to scan the session. Your microphone is never recorded.')}
    </section>
    <section class="card section"><h2>Recordings</h2>
      <div class="form">
        <label class="field wide"><span>Folder</span><input id="f-folder" type="text" value="${esc(s.folder)}"><small>Only recordings made by PCS Scanner in this folder are ever deleted.</small></label>
        <label class="field"><span>Keep recordings for (days)</span><input id="f-keep_days" type="number" min="1" max="90" value="${s.keep_days}"><small>1 to 90 days.</small></label>
        <label class="field"><span>Recordings with no game: delete after (hours)</span><input id="f-nogame_keep_h" type="number" min="0" max="720" value="${s.nogame_keep_h ?? 24}">
          <small>Only when the scan is sure nothing was played (no game, not one kill-feed line). 0 = right after the scan. They also go first when space is needed.</small></label>
        <label class="field"><span>Maximum folder size (GB)</span><input id="f-max_gb" type="number" min="20" value="${s.max_gb}"><small>Oldest recordings go first. At least 20 GB.</small></label>
        <label class="field wide"><span>Quality: <b id="q-val">${s.bitrate_mbps} Mb/s</b> · <span id="q-hint"></span></span><input id="f-bitrate_mbps" type="range" min="8" max="60" step="1" value="${s.bitrate_mbps}">
          <small>8 Mb/s is the lowest that keeps the HUD readable for the scanner (Twitch streams are 8–10). 12 is recommended.</small></label>
        <label class="field"><span>Capture</span><select id="f-capture">${[['window', 'Apex window (recommended)'], ['game', 'Game capture'], ['display', 'Whole screen']].map(([v, l]) => `<option value="${v}" ${v === s.capture ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
      </div></section>
    <section class="card section"><h2>Sound and comms</h2>
      <p class="muted" style="margin:0">Each sound is recorded on its own track, so the review can play the game, the voice app and your voice separately.</p>
      ${switchHTML('f-game_audio', s.game_audio, 'Game sound', 'Apex only, never other programs')}
      <div class="notice ${s.comms_consent ? 'info' : ''}">
        ${switchHTML('f-comms_consent', s.comms_consent, 'My team agreed that our voice comms are recorded',
          'Recording the voice app records your teammates too. Turn this on only once everyone in your calls agreed (in Switzerland and many other countries, recording a private conversation needs everyone\'s consent).')}
      </div>
      <div class="form" id="commsBox">
        <div class="field">${switchHTML('f-discord_audio', s.discord_audio, 'Voice app', voiceName(s) + '\'s sound only')}</div>
        <label class="field"><span>Voice app</span><select id="f-voice_app">${[['discord', 'Discord'], ['teamspeak', 'TeamSpeak'], ['other', 'Other program']].map(([v, l]) => `<option value="${v}" ${v === (s.voice_app || 'discord') ? 'selected' : ''}>${l}</option>`).join('')}</select>
          <small>Takes effect the next time Apex starts.</small></label>
        <label class="field" id="voiceExeBox"><span>Program</span><input id="f-voice_exe" type="text" list="voiceApps" value="${esc(s.voice_exe || '')}" placeholder="Mumble.exe" maxlength="64" spellcheck="false" autocomplete="off"><datalist id="voiceApps"></datalist>
          <small>Open the voice app first, then pick it from the list (or type its name).</small></label>
        <div class="field">${switchHTML('f-mic_audio', s.mic_audio, 'My microphone', 'Your voice, on its own track')}</div>
        <label class="field"><span>Microphone</span><select id="f-mic_device"><option value="${esc(s.mic_device || 'default')}">Loading…</option></select>
          <small>Takes effect the next time Apex starts.</small></label>
        <div class="field">${switchHTML('f-send_mic', s.send_mic, 'Send my mic track to the team', 'With the scan results, for the comms review')}</div>
      </div>
      <h3>Hotkeys while recording</h3>
      <p class="muted" style="margin:-6px 0 0">Active only while Apex is being recorded. A short beep confirms (two low tones = muted).
        Controller players can use a single key like F5 or a numpad key. While recording, a key used for a hotkey doesn't reach the game, so keyboard players should pick keys they don't play with.</p>
      <div class="form">
        ${hotkeyField('f-hk_mark', s.hk_mark, 'Mark this moment')}
        ${hotkeyField('f-hk_mute_game', s.hk_mute_game, 'Mute / unmute game sound')}
        ${hotkeyField('f-hk_mute_discord', s.hk_mute_discord, 'Mute / unmute ' + voiceName(s))}
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
        <label class="field"><span>Sound on YouTube</span><select id="f-yt_audio">${[['game', 'Game only'], ['mix', `Game + comms (${voiceName(s)} and mic)`]].map(([v, l]) => `<option value="${v}" ${v === s.yt_audio ? 'selected' : ''} ${v === 'mix' && !s.comms_consent ? 'disabled' : ''}>${l}</option>`).join('')}</select>
          <small>${s.comms_consent ? 'Comms on YouTube means your team\'s voices are on the channel too.' : 'Comms need the team agreement above.'}</small></label>
        <label class="field"><span>Same block if less than (minutes) apart</span><input id="f-yt_gap_min" type="number" min="10" max="240" value="${s.yt_gap_min}"><small>Restarting Apex mid-block stays one video.</small></label>
        <div class="field">${switchHTML('f-yt_scrims_only', s.yt_scrims_only, 'Scrims only', 'Skip blocks labelled ranked, pubs or wildcard (see the YouTube page)')}</div>
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
      </div></section>` : '<div class="notice">Auto-record isn\'t available in this build.</div>'}
    <section class="card section"><h2>About</h2>
      <div class="row between"><span class="muted">PCS Scanner ${esc(S.version)}${r && r.encoder ? ' · encoder ' + esc(r.encoder) : ''}</span>
        <span class="row">${r ? '<button class="btn small ghost" id="f-retest" title="Test the graphics card\'s encoders again, e.g. after a driver or graphics card change">Test encoder again</button><button class="btn small ghost" id="f-obslogs" title="The recorder\'s log files, to send to Blakey if recording fails">Recorder logs</button>' : ''}<button class="btn small ghost" id="f-results">Open results folder</button><button class="btn small ghost" id="f-quit">Quit PCS Scanner</button></span></div></section>`;
  const q = $('#f-bitrate_mbps');
  const qh = () => { if (!q) return; $('#q-val').textContent = q.value + ' Mb/s'; $('#q-hint').textContent = '≈ ' + ((q.value * 1000 + 480) * 3600 / 8 / 1e6).toFixed(1) + ' GB per hour'; };
  if (q) { q.oninput = qh; qh(); }
  $('#f-signin').onclick = () => { api('/api/signin', {}); toast('Your browser opens for the Google sign-in…'); };
  $('#f-results').onclick = () => api('/api/open', {what: 'results'});
  const fl = $('#f-obslogs'); if (fl) fl.onclick = () => api('/api/open', {what: 'obslogs'});
  const ft = $('#f-retest'); if (ft) ft.onclick = async () => {
    try { await api('/api/encoder/retest', {}); toast('Testing the encoders again: watch Home → Activity.'); } catch (e) { toast(e.message, true); }
  };
  $('#f-quit').onclick = () => { if (confirmBox('Quit PCS Scanner? Auto-record stops until you open it again.')) api('/api/quit', {}); };
  $('#save').onclick = saveSettings;
  wireHotkeys();
  wireHubKey();
  const ys = $('#f-ytsignin'); if (ys) ys.onclick = () => { api('/api/youtube/signin', {}); toast('Your browser opens: sign in and pick the team channel.'); };
  const md = $('#f-mic_device'); if (md) api('/api/mics').then(m => {
    md.innerHTML = m.devices.map(d => `<option value="${esc(d.id)}" ${d.id === m.current ? 'selected' : ''}>${esc(d.name)}</option>`).join('');
  }).catch(() => { md.innerHTML = '<option value="default">Windows default microphone</option>'; });
  const va = $('#f-voice_app'), vx = $('#f-voice_exe');
  const syncVoice = () => {
    if (!va) return;
    const n = voiceName({voice_app: va.value, voice_exe: vx.value});
    $('#voiceExeBox').hidden = va.value !== 'other';
    const hint = $('#f-discord_audio').closest('label').querySelector('small'); if (hint) hint.textContent = n + '\'s sound only';
    const hk = $('#f-hk_mute_discord'); if (hk) hk.closest('label').querySelector('span').textContent = 'Mute / unmute ' + n;
    const mix = $('#f-yt_audio option[value="mix"]'); if (mix) mix.textContent = `Game + comms (${n} and mic)`;
  };
  if (va) {
    va.onchange = syncVoice; vx.oninput = syncVoice; syncVoice();
    api('/api/voiceapps').then(v => { $('#voiceApps').innerHTML = (v.running || []).map(a => `<option value="${esc(a.exe)}">${esc(a.title)}</option>`).join(''); }).catch(() => {});
  }
  const cc = $('#f-comms_consent'), box = $('#commsBox');
  const syncComms = () => { if (box) { box.style.opacity = cc.checked ? 1 : .4; $$('input, select', box).forEach(i => i.disabled = !cc.checked); } };
  if (cc) { cc.onchange = syncComms; syncComms(); }
  const en = $('#f-enabled'); if (en) en.onchange = async () => {
    try { S = await api('/api/autorecord', {enabled: en.checked}); toast(en.checked ? 'Auto-record on.' : 'Auto-record off.'); renderTop(); }
    catch (e) { toast(e.message, true); en.checked = !en.checked; }
  };
}
async function saveSettings() {
  const body = {player: $('#f-player').value.trim(), role: $('#f-role').value, team_link: $('#f-team_link').value.trim()};
  if (S.recorder) {
    for (const k of ['folder', 'hub_url', 'capture', 'yt_privacy', 'yt_audio', 'yt_when', 'yt_team', 'mic_device', 'voice_app', 'voice_exe']) { const el = $('#f-' + k); if (el) body[k] = el.value.trim(); }
    for (const k of ['keep_days', 'max_gb', 'bitrate_mbps', 'scan_delay_s', 'yt_gap_min', 'night_from', 'night_to', 'night_idle_min', 'yt_wait_eec_h', 'nogame_keep_h']) { const el = $('#f-' + k); if (el) body[k] = parseInt(el.value, 10); }
    for (const k of ['start_with_windows', 'game_audio', 'auto_scan', 'comms_consent', 'discord_audio', 'mic_audio', 'send_mic', 'hk_sounds', 'yt_enabled', 'yt_scrims_only', 'night_shutdown', 'yt_wait_eec']) { const el = $('#f-' + k); if (el) body[k] = el.checked; }
    for (const k of ['hk_mark', 'hk_mute_game', 'hk_mute_discord', 'hk_mute_mic']) body[k] = $('#f-' + k).dataset.v || '';
    const hk = $('#f-hub_key'); if (hk && hk.value.trim()) body.hub_key = hk.value.trim();   // empty = key unchanged
  }
  try { S = await api('/api/settings', body); if (body.hub_key) hubKeyEdit = false; toast('Settings saved.'); renderSettings(); renderTop(); }
  catch (e) { toast('Not saved: ' + e.message, true); }
}

/* ------------------------------------------------------------------ team hub: key (Settings) and Team page */
// The page never gets the key back from the app: only "pcs_…abcd". A new key is typed, sent once, and forgotten here.
let hubKeyEdit = false;
function hubStatusHTML(h) {
  if (!h) return '';
  if (h.wait) return `<span class="hubmsg warn">Too many wrong keys: wait ${Math.ceil(h.wait / 60)} min</span>`;
  if (h.refused) return '<span class="hubmsg bad">Key refused. Ask your coach for a new key.</span>';
  const l = h.last || {};
  return l.text ? `<span class="hubmsg ${l.ok ? 'good' : 'bad'}">${esc(l.text)}</span>` : '';
}
function wireHubKey() {
  const box = $('#hubKey'); if (!box || !S) return;
  const h = S.hub || {}, edit = hubKeyEdit || !h.key;
  box.innerHTML = `<div class="row">${edit
      ? `<input id="f-hub_key" type="password" autocomplete="off" spellcheck="false" placeholder="Paste your key or sign-in link">${h.key ? '<button class="btn small ghost" id="hk-cancel">Cancel</button>' : ''}`
      : `<span class="keybox">${esc(h.key)}</span><button class="btn small ghost" id="hk-change">Change</button><button class="btn small ghost danger" id="hk-remove">Remove</button>`}
    <button class="btn small" id="hk-test">Test</button></div>
    <div id="hk-result" class="hubres">${hubStatusHTML(h)}</div>`;
  const c = $('#hk-change'); if (c) c.onclick = () => { hubKeyEdit = true; wireHubKey(); $('#f-hub_key').focus(); };
  const x = $('#hk-cancel'); if (x) x.onclick = () => { hubKeyEdit = false; wireHubKey(); };
  const rm = $('#hk-remove'); if (rm) rm.onclick = async () => {
    if (!confirmBox('Remove your hub key from this PC?')) return;
    try { S = await api('/api/settings', {hub_key: null}); toast('Key removed.'); wireHubKey(); renderTop(); } catch (e) { toast(e.message, true); }
  };
  $('#hk-test').onclick = async () => {
    const body = {hub_url: $('#f-hub_url').value.trim()}, k = $('#f-hub_key');
    if (k && k.value.trim()) body.hub_key = k.value.trim();
    $('#hk-test').disabled = true; $('#hk-result').innerHTML = '<span class="hubmsg">Testing…</span>';
    try {
      S = await api('/api/settings', body);                  // the address and a new key are saved first
      if (body.hub_key) hubKeyEdit = false;
      $('#f-hub_url').value = S.recorder.settings.hub_url;    // a sign-in link fills an empty address
      const r = await api('/api/hub/test', {}); S.hub = r.hub;
      wireHubKey(); renderTop();
    } catch (e) { const o = $('#hk-result'); if (o) o.innerHTML = `<span class="hubmsg bad">${esc(e.message)}</span>`; const t = $('#hk-test'); if (t) t.disabled = false; }
  };
}

function renderTeam() {
  if (!S) { $('#main').innerHTML = '<div class="empty"><strong>Starting…</strong></div>'; setTimeout(() => page === 'team' && renderTeam(), 500); return; }
  const h = S.hub || {};
  if (!h.url) { $('#main').innerHTML = '<div class="empty"><strong>No team hub set</strong>Add the address from your coach in <a href="#settings">Settings</a>.</div>'; return; }
  const l = h.last || {};
  $('#main').innerHTML = `
    <div class="pagehead"><div><h1>Team hub</h1><p>The team's pages: Loot Book, scans, games. They open in their own window, signed in with your key.</p></div>
      <div class="toolbar"><a class="btn ghost" href="#settings">Hub settings</a><button class="btn" id="tm-test">Test</button></div></div>
    <section class="card section">
      <div class="muted mono" style="font-size:13px">${esc(h.url)}</div>
      <div id="tm-status">${hubStatusHTML(h) || (h.key ? '<span class="hubmsg">Not tested yet</span>' : '<span class="hubmsg warn">No key yet: add it in <a href="#settings">Settings</a>.</span>')}</div>
      <div class="row">
        <button class="btn brand" data-p="/">Open team hub</button>
        <button class="btn" data-p="/lootbook/">Loot Book</button>
        <button class="btn" data-p="/#/scanner">Scans</button>
        ${l.role === 'coach' ? '<button class="btn ghost" data-p="/keys">Keys</button>' : ''}
      </div></section>`;
  $$('[data-p]').forEach(b => b.onclick = async () => {
    b.disabled = true;
    try { const r = await api('/api/hub/open', {path: b.dataset.p}); toast(r.window ? 'Opened in a new window.' : 'Opened in your browser. If it asks for a key, open the sign-in link from your coach.'); }
    catch (e) { toast(e.message, true); }
    b.disabled = false;
  });
  const test = async () => {
    $('#tm-status').innerHTML = '<span class="hubmsg">Testing…</span>';
    try { const r = await api('/api/hub/test', {}); S.hub = r.hub; if (page === 'team') renderTeam(); }
    catch (e) { $('#tm-status').innerHTML = `<span class="hubmsg bad">${esc(e.message)}</span>`; }
  };
  $('#tm-test').onclick = test;
  if (!l.text && !h.refused && !h.wait && !renderTeam.tested) { renderTeam.tested = true; test(); }   // once per app start
}

/* ------------------------------------------------------------------ youtube queue */
const YT_STATUS = {
  done: ['good', 'On YouTube'], uploading: ['info', 'Uploading'], waiting: ['info', 'Preparing'],
  requested: ['info', 'Next in line'], pending: ['', 'Waiting'], skipped: ['', 'Skipped'],
  failed: ['bad', 'Failed'], 'waiting-retry': ['warn', 'Retrying soon'],
};
let ytEdit = null, ytTimer = null;
function renderYouTube() {
  $('#main').innerHTML = `
    <div class="pagehead"><div><h1>YouTube queue</h1><p>One video per block of games, to the team channel. Change a block's type, upload it now, skip it, or set its title and privacy before it goes up.</p></div>
      <div class="toolbar"><a class="btn ghost" href="#settings">YouTube settings</a><button class="btn" id="ytRefresh">Refresh</button></div></div>
    <div id="ytNote"></div>
    <section class="card" style="padding:6px 12px"><div class="list" id="ytList"><div class="empty">Loading…</div></div></section>`;
  $('#ytRefresh').onclick = loadQueue;
  clearInterval(ytTimer);
  ytTimer = setInterval(() => { if (page !== 'youtube') return clearInterval(ytTimer); if (!ytEdit && !document.hidden) loadQueue(); }, 5000);
  loadQueue();
}
async function loadQueue() {
  let q; try { q = await api('/api/youtube/queue'); } catch (e) { return; }
  if (page !== 'youtube') return;
  const note = $('#ytNote'), el = $('#ytList');
  note.innerHTML = !q.available ? '<div class="notice">YouTube upload isn\'t available in this build.</div>'
    : !q.enabled ? '<div class="notice">YouTube upload is off: blocks only go up when you press Upload now. Turn it on in <a href="#settings">Settings → YouTube</a>.</div>'
    : !q.signed_in ? '<div class="notice warn">Not signed in to the team channel yet: <a href="#settings">Settings → YouTube</a>.</div>'
    : q.now ? `<div class="notice info">${esc(q.now)}</div>` : '';
  if (!q.blocks.length) { el.innerHTML = '<div class="empty"><strong>No blocks yet</strong>Auto-recorded games appear here, grouped in blocks.</div>'; return; }
  el.innerHTML = q.blocks.map(b => {
    const [cls, word] = YT_STATUS[b.status] || ['', b.status];
    const busy = ['uploading', 'waiting'].includes(b.status), done = b.status === 'done';
    const acts = done ? `<a class="btn small brand" href="${esc(b.url)}" target="_blank" rel="noopener">Watch ↗</a>`
      : busy ? '' : [
        b.status !== 'requested' ? `<button class="btn small ${b.status === 'skipped' ? '' : 'brand'}" data-a="upload">${b.status === 'skipped' ? 'Upload anyway' : b.status === 'failed' ? 'Try again' : 'Upload now'}</button>` : '',
        b.status === 'skipped' ? (b.why === 'skipped by you' ? '<button class="btn small ghost" data-a="unskip" title="Back to the automatic rules">Put back</button>' : '')
          : '<button class="btn small ghost" data-a="skip" title="This block won\'t go to YouTube">Skip</button>',
        '<button class="btn small ghost" data-a="edit">Title & privacy</button>'].join('');
    const res = b.res ? ` · ${ordinal(b.res.standing)}/${b.res.teams}, ${b.res.pts} pts` : '';
    const custom = b.custom_title || b.privacy ? `<div class="sub faint" style="font-size:12px">${b.custom_title ? 'Title: ' + esc(b.custom_title) : ''}${b.custom_title && b.privacy ? ' · ' : ''}${b.privacy ? esc(b.privacy) : ''}</div>` : '';
    const form = ytEdit === b.id ? `<div class="ytform form">
        <label class="field wide"><span>Title (empty = automatic)</span><input id="ye-title" maxlength="100" value="${esc(b.custom_title)}" placeholder="${esc(b.title || 'Scrims dd.mm.yyyy hh:mm · team · player POV · …')}"></label>
        <label class="field"><span>Privacy</span><select id="ye-privacy">${[['', 'As in Settings (' + q.privacy + ')'], ['unlisted', 'Unlisted'], ['private', 'Private'], ['public', 'Public']].map(([v, l]) => `<option value="${v}" ${v === b.privacy ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
        <div class="row"><button class="btn small brand" data-a="save">Save</button><button class="btn small ghost" data-a="cancel">Cancel</button></div></div>` : '';
    return `<div class="ytrow" data-id="${esc(b.id)}">
      <div style="min-width:0"><div class="name">${fmtDate(b.start)} <span class="faint">→ ${new Date(b.end * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}</span></div>
        <div class="sub">${done || busy ? (b.label ? `<span class="chip">${KINDS[b.label]}</span>` : '') : labelSelect(b.label, b.label_by_hand, 'data-label')}
          <span class="faint" style="font-size:12px">${b.games} game${b.games === 1 ? '' : 's'}${b.maps.length ? ' · ' + esc(b.maps.join(', ')) : ''} · ${b.recordings} recording${b.recordings === 1 ? '' : 's'}${res}${b.eec && !b.res ? ' · EEC link' : ''}</span></div>${custom}</div>
      <div class="ytstate"><span class="pill ${cls}">${word}</span><small class="muted">${esc(busy ? b.progress : b.why)}</small></div>
      <div class="acts">${acts}</div>${form}</div>`;
  }).join('');
  $$('.ytrow', el).forEach(row => {
    const id = row.dataset.id;
    const act = async (action, extra) => {
      try { await api('/api/youtube/block', {id, action, ...(extra || {})}); ytEdit = null; loadQueue(); }
      catch (e) { toast(e.message, true); }
    };
    $$('[data-a]', row).forEach(b => b.onclick = () => {
      const a = b.dataset.a;
      if (a === 'edit') { ytEdit = ytEdit === id ? null : id; loadQueue(); }
      else if (a === 'cancel') { ytEdit = null; loadQueue(); }
      else if (a === 'save') act('edit', {title: $('#ye-title').value, privacy: $('#ye-privacy').value});
      else act(a);
    });
    const ls = $('select[data-label]', row); if (ls) ls.onchange = () => act('label', {title: ls.value});
  });
}

poll();
route();
