/* Clips for Discord from the review player (pcs_recorder/clips.py does the work). Loaded after app.js (uses $, $$,
   esc, api, toast, fmtT, RV, page, pageArg, RX from review-extras.js when it has session bookmarks).

   Pick a range (start / end buttons or keys I / O, or a moment from the list with seconds before and after), the
   sound tracks (the voice-app track is never offered: teammates' voices go in only through their own shared mic
   tracks), the coach's drawings and notes, and the size (under 10 MB for Discord, or full quality). The clip is saved
   next to the recording in a "Clips" folder; nothing is uploaded. */
'use strict';

const CLIP = {open: false, a: 0, b: 30, size: 'discord', own: null, team: null, notes: true, before: 10, after: 5,
              timer: null, last: null, d: null, v: null, picked: false};
// same numbers as clips.py: DISCORD_TARGET, MIN_VIDEO_KBPS, audio 96 kb/s (test_clip_page_matches_server)
const CLIP_TARGET = 9000000, CLIP_MIN_KBPS = 300, CLIP_AUDIO_KBPS = 96, CLIP_FULL_MAX = 15 * 60;

function clipSetup(d, v) {
  clearInterval(CLIP.timer);
  Object.assign(CLIP, {open: false, d, v, own: null, team: null, last: null, picked: false, mom: null});
  const row = $('#rvTheatre') && $('#rvTheatre').parentNode;
  if (row && !$('#rvClip')) row.insertAdjacentHTML('afterbegin', '<button class="btn small" id="rvClip" title="Save a part of this recording as a video for Discord (C)">Clip</button>');
  const anchor = $('#coachBar');
  if (anchor && !$('#clipBar')) anchor.insertAdjacentHTML('afterend', '<div class="card clipbar" id="clipBar" hidden></div>');
  const b = $('#rvClip'); if (b) b.onclick = () => CLIP.open ? clipClose() : clipOpen();
  api('/api/clips/state').then(st => { if (st.running && page === 'review') { clipOpen(); clipPoll(); } }).catch(() => {});
}

function clipDur() { const v = CLIP.v; return v && v.duration ? v.duration : (RV ? dur() : 0); }
function clipTracks() {          /* the recording's own tracks a clip may use: not the voice app's */
  const t = (CLIP.d.audio || []).filter(x => x.key !== 'discord');
  return t.map(x => ({...x, label: (CLIP.d.audio || []).length <= 1 ? 'Recording sound' : x.key === 'mic' ? 'Your mic' : x.label}));
}
function clipOpen() {
  if (!CLIP.d) return;
  const v = CLIP.v, t = v ? v.currentTime : 0;
  CLIP.open = true;
  if (CLIP.own === null) CLIP.own = new Set(clipTracks().filter(x => x.key === 'game' || (CLIP.d.audio || []).length <= 1).map(x => x.n));
  if (CLIP.team === null) CLIP.team = new Set();
  if (!CLIP.picked) { CLIP.a = Math.max(0, t - 10); CLIP.b = Math.min(clipDur() || t + 20, t + 20); CLIP.picked = true; }   // first open: around here
  const b = $('#rvClip'); if (b) b.classList.add('brand');
  clipDraw();
}
function clipClose() {
  CLIP.open = false;
  const bar = $('#clipBar'); if (bar) { bar.hidden = true; bar.innerHTML = ''; }
  const b = $('#rvClip'); if (b) b.classList.remove('brand');
  clipRange();
}
function clipParse(s) {          /* "1:02:03", "12:05", "75" -> seconds, or null */
  const p = String(s).trim().split(':');
  if (!p.length || p.some(x => !/^\d+(\.\d+)?$/.test(x)) || p.length > 3) return null;
  return p.reduce((a, x) => a * 60 + +x, 0);
}
function clipMoments() {         /* events, own marks, coach notes and session bookmarks to start a clip from */
  const out = [];
  (RV.ev || []).forEach(e => { if (e.title && !e.laneOnly && !['fight', 'call', 'mute'].includes(e.k)) out.push({t: e.t, t1: e.t1, title: e.title, sub: e.sub || ''}); });
  ((RV.notes && RV.notes.notes) || []).forEach(n => out.push({t: n.t, t1: n.t + (n.dur || 5), title: 'Coach note', sub: (n.text || 'drawing').slice(0, 60)}));
  if (typeof RX !== 'undefined' && RX.data) RX.data.games.forEach(g => (g.bookmarks || []).forEach(b => out.push({t: g.jumpAt + b.t, title: 'Session bookmark', sub: (b.text || '').slice(0, 60)})));
  return out.filter(m => typeof m.t === 'number').sort((a, b) => a.t - b.t);
}
function clipMaxDiscord(nAudio) { return Math.floor(CLIP_TARGET * 8 / 1000 * 0.98 / (CLIP_MIN_KBPS + (nAudio ? CLIP_AUDIO_KBPS : 0))); }
function clipNotesIn() {
  return ((RV.notes && RV.notes.notes) || []).filter(n => (n.text || (n.drawings || []).length) && n.t + (n.dur || 5) > CLIP.a && n.t - 0.05 < CLIP.b);
}
function clipDraw() {
  const bar = $('#clipBar'); if (!bar || !CLIP.open) return;
  const st = CLIP.last, busy = st && st.running;
  const len = CLIP.b - CLIP.a, own = clipTracks(), team = CLIP.d.teamAudio || [];
  const nAudio = CLIP.own.size + CLIP.team.size, maxD = clipMaxDiscord(nAudio), notesIn = clipNotesIn();
  const voice = (CLIP.d.audio || []).find(x => x.key === 'discord');
  const tooLong = CLIP.size === 'discord' ? len > maxD : len > CLIP_FULL_MAX;
  const moments = clipMoments();
  bar.hidden = false;
  bar.innerHTML = `<div class="row between"><b class="cliptitle">Clip for Discord</b><button class="btn small ghost" id="clX" ${busy ? 'disabled' : ''}>Close</button></div>
    <div class="cliprow">
      <label class="clipt"><span>Start</span><input id="clA" type="text" value="${fmtT(CLIP.a)}" ${busy ? 'disabled' : ''}><button class="btn small" id="clAnow" title="Start here (I)" ${busy ? 'disabled' : ''}>Here</button></label>
      <label class="clipt"><span>End</span><input id="clB" type="text" value="${fmtT(CLIP.b)}" ${busy ? 'disabled' : ''}><button class="btn small" id="clBnow" title="End here (O)" ${busy ? 'disabled' : ''}>Here</button></label>
      <span class="cliplen ${tooLong ? 'bad' : ''}">${len > 0 ? fmtT(len) : '–'} long</span>
      <button class="btn small ghost" id="clPlay" title="Play the clip's range">Play range</button>
    </div>
    ${moments.length ? `<div class="cliprow"><label class="clipm"><span>Around a moment</span><select id="clMom" ${busy ? 'disabled' : ''}><option value="">Pick an event, note or bookmark…</option>${moments.map((m, i) => `<option value="${i}">${fmtT(m.t)} · ${esc(m.title)}${m.sub ? ' · ' + esc(m.sub) : ''}</option>`).join('')}</select></label>
      <label class="clipt"><span>Before</span><select id="clBefore">${[3, 5, 10, 15, 20, 30].map(s => `<option ${s === CLIP.before ? 'selected' : ''}>${s}</option>`).join('')}</select> s</label>
      <label class="clipt"><span>After</span><select id="clAfter">${[3, 5, 10, 15, 20, 30].map(s => `<option ${s === CLIP.after ? 'selected' : ''}>${s}</option>`).join('')}</select> s</label></div>` : ''}
    <div class="cliprow"><span class="cliplab">Sound</span>
      ${own.map(x => `<label class="clipck"><input type="checkbox" data-own="${x.n}" ${CLIP.own.has(x.n) ? 'checked' : ''} ${busy ? 'disabled' : ''}> ${esc(x.label)}</label>`).join('')}
      ${team.map(x => `<label class="clipck" title="Only if ${esc(x.player)} still shares it: the team hub is asked when the clip is made"><input type="checkbox" data-team="${esc(x.id)}" ${CLIP.team.has(x.id) ? 'checked' : ''} ${busy ? 'disabled' : ''}> ${esc(x.player)}'s mic</label>`).join('')}
      ${!own.length && !team.length ? '<span class="faint">No sound tracks</span>' : ''}</div>
    ${voice ? `<small class="faint">${esc(voice.label)} sound is never in a clip: it holds your teammates' voices. Their own mic tracks can go in when they share them.</small>` : ''}
    <div class="cliprow">
      ${notesIn.length ? `<label class="clipck"><input type="checkbox" id="clNotes" ${CLIP.notes ? 'checked' : ''} ${busy ? 'disabled' : ''}> Coach drawings and notes (${notesIn.length})</label>` : ''}
      <div class="seg" id="clSize"><button data-s="discord" class="${CLIP.size === 'discord' ? 'on' : ''}" ${busy ? 'disabled' : ''}>Discord · under 10 MB</button><button data-s="full" class="${CLIP.size === 'full' ? 'on' : ''}" ${busy ? 'disabled' : ''}>Full quality</button></div>
      <span style="flex:1"></span>
      ${busy ? '<button class="btn small" id="clStop">Stop</button>' : `<button class="btn small brand" id="clGo" ${tooLong || len < 1 ? 'disabled' : ''}>Make clip</button>`}
    </div>
    ${tooLong ? `<div class="notice">${CLIP.size === 'discord' ? `Too long for 10 MB: ${fmtT(maxD)} at most. Make it shorter or pick Full quality.` : `A clip can be at most ${CLIP_FULL_MAX / 60} minutes long.`}</div>` : ''}
    ${clipStateHTML(st)}`;
  const set = (k, val) => { CLIP[k] = val; clipDraw(); clipRange(); };
  $('#clX', bar).onclick = clipClose;
  const ia = $('#clA', bar), ib = $('#clB', bar);
  ia.onchange = () => { const x = clipParse(ia.value); x == null ? clipDraw() : set('a', Math.max(0, Math.min(x, clipDur()))); };
  ib.onchange = () => { const x = clipParse(ib.value); x == null ? clipDraw() : set('b', Math.max(0, Math.min(x, clipDur()))); };
  $('#clAnow', bar).onclick = () => clipHere('a');
  $('#clBnow', bar).onclick = () => clipHere('b');
  $('#clPlay', bar).onclick = () => { const v = CLIP.v; if (!v) return; v.currentTime = CLIP.a; v.play().catch(() => {}); clipWatch(); };
  const mom = $('#clMom', bar);
  const fromMoment = () => { const m = moments[+mom.value]; if (!m) return; CLIP.a = Math.max(0, m.t - CLIP.before); CLIP.b = Math.min(clipDur() || 1e9, (m.t1 && m.t1 > m.t ? m.t1 : m.t) + CLIP.after); CLIP.mom = mom.value; clipDraw(); clipRange(); if (CLIP.v) CLIP.v.currentTime = CLIP.a; };
  if (mom) { if (CLIP.mom != null) mom.value = CLIP.mom; mom.onchange = fromMoment; }
  const bf = $('#clBefore', bar), af = $('#clAfter', bar);
  if (bf) bf.onchange = () => { CLIP.before = +bf.value; mom && mom.value ? fromMoment() : clipDraw(); };
  if (af) af.onchange = () => { CLIP.after = +af.value; mom && mom.value ? fromMoment() : clipDraw(); };
  $$('[data-own]', bar).forEach(c => c.onchange = () => { const n = +c.dataset.own; c.checked ? CLIP.own.add(n) : CLIP.own.delete(n); clipDraw(); });
  $$('[data-team]', bar).forEach(c => c.onchange = () => { const n = c.dataset.team; c.checked ? CLIP.team.add(n) : CLIP.team.delete(n); clipDraw(); });
  const nt = $('#clNotes', bar); if (nt) nt.onchange = () => { CLIP.notes = nt.checked; };
  $$('#clSize button', bar).forEach(b => b.onclick = () => set('size', b.dataset.s));
  const go = $('#clGo', bar); if (go) go.onclick = clipMake;
  const stop = $('#clStop', bar); if (stop) stop.onclick = () => api('/api/clips/stop', {}).catch(e => toast(e.message, true));
  $$('[data-act]', bar).forEach(b => b.onclick = async () => {
    try { await api('/api/clips/' + b.dataset.act, {file: st.file}); if (b.dataset.act === 'copy') toast('Clip copied. Paste it in Discord with Ctrl+V.'); }
    catch (e) { toast(e.message, true); }
  });
  clipRange();
}
function clipStateHTML(st) {
  if (!st || !st.step || st.step === 'Starting' && !st.running) return '';
  const left = (st.leftOut || []).length ? `<small class="faint">Left out: ${st.leftOut.map(esc).join(' · ')}</small>` : '';
  if (st.running) return `<div class="clipprog"><span>${esc(st.step)}… ${st.pct || 0}%</span><div class="bar"><i style="width:${st.pct || 0}%"></i></div></div>${left}`;
  if (st.error) return `<div class="notice">${esc(st.error)}</div>${left}`;
  if (!st.file) return '';
  return `<div class="clipdone"><span class="pill good">Saved</span> <b>${esc(st.name)}</b> <span class="faint">${st.mb} MB</span>
      <span style="flex:1"></span><button class="btn small" data-act="show">Show in folder</button><button class="btn small brand" data-act="copy">Copy file</button></div>
    ${st.over ? '<div class="notice">Still over 10 MB: Discord may refuse it. Make the clip shorter.</div>' : ''}${left}`;
}
function clipHere(k) {
  const v = CLIP.v; if (!v) return;
  CLIP[k] = v.currentTime;
  if (k === 'a' && CLIP.b <= CLIP.a) CLIP.b = Math.min(clipDur(), CLIP.a + 20);
  if (k === 'b' && CLIP.b <= CLIP.a) CLIP.a = Math.max(0, CLIP.b - 20);
  CLIP.mom = null;
  clipDraw();
}
function clipWatch() {           /* Play range: stop at the end */
  const v = CLIP.v; if (!v) return;
  const h = () => { if (v.currentTime >= CLIP.b) { v.pause(); v.removeEventListener('timeupdate', h); } };
  v.addEventListener('timeupdate', h);
  v.addEventListener('seeking', () => v.removeEventListener('timeupdate', h), {once: true});
}
function clipRange() {           /* the range on the timeline */
  const tl = $('#tl'); if (!tl) return;
  let r = $('#clipRange');
  if (!CLIP.open || !(CLIP.b > CLIP.a)) { if (r) r.remove(); return; }
  if (!r) { tl.insertAdjacentHTML('beforeend', '<div class="cliprange" id="clipRange"></div>'); r = $('#clipRange'); }
  const D = dur();
  r.style.left = (CLIP.a / D * 100) + '%'; r.style.width = ((CLIP.b - CLIP.a) / D * 100) + '%';
}
async function clipMake() {
  const body = {id: CLIP.d.id, start: Math.round(CLIP.a * 1000) / 1000, end: Math.round(CLIP.b * 1000) / 1000, size: CLIP.size,
                tracks: [...CLIP.own], team: [...CLIP.team], notes: CLIP.notes && clipNotesIn().length > 0,
                player: (RV.notes && RV.notes.player) || null};
  try { CLIP.last = await api('/api/clips/make', body); } catch (e) { toast(e.message, true); return; }
  clipDraw(); clipPoll();
}
function clipPoll() {
  clearInterval(CLIP.timer);
  CLIP.timer = setInterval(async () => {
    if (page !== 'review') { clearInterval(CLIP.timer); return; }
    let st; try { st = await api('/api/clips/state'); } catch (e) { return; }
    CLIP.last = st;
    if (!st.running) clearInterval(CLIP.timer);
    if (CLIP.open) {
      const bar = $('#clipBar'), prog = bar && $('.clipprog', bar);
      if (st.running && prog) { prog.querySelector('span').textContent = `${st.step}… ${st.pct || 0}%`; prog.querySelector('i').style.width = (st.pct || 0) + '%'; }
      else clipDraw();
    }
  }, 700);
}
document.addEventListener('keydown', e => {
  if (page !== 'review' || !CLIP.d || (RV && RV.ed)) return;
  if (['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName) || e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === 'c') { CLIP.open ? clipClose() : clipOpen(); }
  else if (CLIP.open && !(CLIP.last && CLIP.last.running) && (k === 'i' || k === 'o')) clipHere(k === 'i' ? 'a' : 'b');
  else return;
  e.preventDefault();
});
window.addEventListener('resize', () => { if (page === 'review') clipRange(); });
