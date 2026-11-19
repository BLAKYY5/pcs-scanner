/* What a Watch together session leaves in the review player (team hub 0.5): the coach's session bookmarks for the
   games of this recording, and the review talk (his mic and voice app recorded during the session) played over the
   player's own video at the same game time. Loaded after app.js (uses $, $$, esc, api, toast, fmtT, TOKEN, page, pageArg).

   Game time 0 = the jump; this recording's time = its jump (jumpAt) + game time. The talk's segments say which part
   of the audio goes with which game time: g0 < g1 = the session was playing (the audio follows the video, at the
   session's speed); g0 == g1 = the coach talked while paused there: the video pauses, the talk plays, then the
   video goes on.
   Since session plans (hub with plans): each session's summary (when, how long, who was there, plan moments covered,
   bookmarks, flags, notes, drawings) and the plan's moments of these games (covered or skipped), as buttons. */
'use strict';

const RX = {data: null, audios: [], on: false, timer: null, held: null, done: new Set(), lastT: 0};
const PAUSE_TALK_MIN_S = 1;        // shorter talk while paused isn't worth stopping the video for

async function reviewExtras(d, v) {
  stopTalk();
  RX.data = null;
  const anchor = $('#coachBar'); if (!anchor) return;
  const box = document.createElement('div');
  box.id = 'rvExtras'; box.className = 'card rv-extras'; box.hidden = true;
  anchor.after(box);
  let r;
  try { r = await api(`/api/library/${d.id}/extras`); } catch (e) { return; }
  if (page !== 'review' || pageArg !== d.id || !r.available) return;
  const games = (r.games || []).filter(g => g.bookmarks.length || g.talk.length || (g.sessions || []).length || (g.plan || []).length);
  if (!games.length) return;
  RX.data = {games, v};
  const marks = games.flatMap(g => g.bookmarks.map(b => ({...b, rt: g.jumpAt + b.t}))).sort((a, b) => a.rt - b.rt);
  const talk = games.some(g => g.talk.length);
  const plan = games.flatMap(g => (g.plan || []).map(m => ({...m, rt: g.jumpAt + m.t}))).sort((a, b) => a.rt - b.rt);
  const seen = new Set(), sessions = games.flatMap(g => g.sessions || []).filter(x => !seen.has(x.id) && seen.add(x.id))
    .sort((a, b) => (b.started || 0) - (a.started || 0));
  box.hidden = false;
  box.innerHTML = `<div class="row between"><h3>Watch together</h3>
      ${talk ? `<label class="row rx-switch"><input type="checkbox" id="rxTalk"> <b>Review talk</b> <span class="faint">the coach's review over your video</span></label>` : ''}</div>
    <div class="rx-talkstate faint" id="rxState" hidden></div>
    ${marks.length ? `<div class="rx-marks"><span class="faint">Session bookmarks</span>${marks.map(b => `<button class="rx-mark" data-t="${b.rt}">
      <span class="mono">${fmtT(b.rt)}</span> ${esc(b.text || 'Bookmark')}${b.focus ? ` <span class="faint">· ${esc(b.focus)}'s view</span>` : ''}</button>`).join('')}</div>` : ''}
    ${plan.length ? `<div class="rx-marks"><span class="faint">Session plan</span>${plan.map(m => `<button class="rx-mark ${m.covered ? '' : 'skipped'}" data-t="${m.rt}" title="${m.covered ? 'Watched in the session' : 'Skipped in the session'}">
      <span class="mono">${fmtT(m.rt)}</span> ${esc(m.note || 'Moment')}${m.covered ? '' : ' <span class="faint">· skipped</span>'}</button>`).join('')}</div>` : ''}
    ${sessions.length ? `<div class="rx-sessions">${sessions.map(sessionLine).join('')}</div>` : ''}`;
  $$('.rx-mark', box).forEach(b => b.onclick = () => { v.currentTime = +b.dataset.t; v.pause(); });
  const sw = $('#rxTalk');
  if (sw) sw.onchange = () => sw.checked ? startTalk() : stopTalk();
}

/* one line per session: when, how long, who was there, what it covered */
function sessionLine(x) {
  const mins = sec => sec == null ? '' : sec < 60 ? 'under 1 min' : `${Math.round(sec / 60)} min`;
  const when = x.started ? new Date(x.started * 1000).toLocaleString(undefined, {weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}) : 'Session';
  const bits = [];
  if (x.length != null) bits.push(mins(x.length));
  if (x.attendance && x.attendance.length) bits.push(x.attendance.map(a => `${esc(a.name)} ${mins(a.seconds)}`).join(', '));
  if (x.plan) bits.push(`${x.plan.covered} of ${x.plan.total} planned moments${x.plan.skipped ? `, ${x.plan.skipped} skipped` : ''}`);
  const n = (v, w) => v ? `${v} ${w}${v === 1 ? '' : 's'}` : '';
  const counts = [n(x.bookmarks, 'bookmark'), n(x.flags, 'flag'), n(x.notes, 'note'), n(x.drawings, 'drawing')].filter(Boolean);
  if (counts.length) bits.push(counts.join(' · '));
  return `<div class="rx-session"><b>${esc(when)}</b> <span class="faint">${bits.join(' · ')}</span></div>`;
}

function talkTracks() {
  return RX.data ? RX.data.games.flatMap(g => g.talk.map(t => ({...t, jumpAt: g.jumpAt}))) : [];
}
function startTalk() {
  const v = RX.data && RX.data.v; if (!v) return;
  stopTalk();
  RX.on = true; RX.done = new Set(); RX.lastT = v.currentTime;
  RX.audios = talkTracks().map(t => {
    const a = new Audio(`/talk/${t.id}?t=${encodeURIComponent(TOKEN)}`);
    a.preload = 'auto';
    a.addEventListener('error', () => setState('Review talk can\'t be loaded (the team hub may be out of reach).'));
    return {a, t};
  });
  v.addEventListener('seeking', onSeekBack);
  v.addEventListener('play', onUserPlay);
  RX.timer = setInterval(talkTick, 200);
  talkTick();
}
function stopTalk() {
  clearInterval(RX.timer); RX.timer = null;
  RX.audios.forEach(x => { try { x.a.pause(); x.a.removeAttribute('src'); x.a.load(); } catch (e) {} });
  RX.audios = []; RX.on = false; RX.held = null;
  const v = RX.data && RX.data.v;
  if (v) { v.removeEventListener('seeking', onSeekBack); v.removeEventListener('play', onUserPlay); }
  setState('');
}
function setState(text) { const el = $('#rxState'); if (el) { el.hidden = !text; el.textContent = text; } }
function onSeekBack() { const v = RX.data.v; if (v.currentTime < RX.lastT - 0.5) RX.done = new Set(); RX.held = null; }
function onUserPlay() { if (RX.held && !RX.held.auto) RX.held = null; }

/* where each talk track should be for the video's moment: (segment, audio second) or null */
function talkAt(t, rt) {
  const g = rt - t.jumpAt;
  for (const s of t.segments) if (s.g1 > s.g0 && g >= s.g0 && g < s.g1) return {s, pos: s.a0 + (g - s.g0) / s.speed};
  return null;
}
function talkTick() {
  const v = RX.data && RX.data.v;
  if (!RX.on || !v || !document.contains(v)) return stopTalk();
  const rt = v.currentTime;
  // the coach talked while paused at a moment the video just passed: hold the video, play that talk, go on
  if (RX.held) {
    const h = RX.held, a = RX.audios[0] && RX.audios[0].a;
    if (!a || a.currentTime >= h.s.a1 - 0.05 || a.ended) {
      RX.audios.forEach(x => x.a.pause());
      RX.held = null; setState('');
      v.play().catch(() => {});
    }
    RX.lastT = rt;
    return;
  }
  if (!v.paused) {
    for (const t of talkTracks().slice(0, 1)) {
      const g0 = RX.lastT - t.jumpAt, g1 = rt - t.jumpAt;
      const s = t.segments.find(x => x.g0 === x.g1 && x.a1 - x.a0 >= PAUSE_TALK_MIN_S && x.g0 > g0 && x.g0 <= g1 + 0.01 && !RX.done.has(x.a0));
      if (s && g1 - g0 < 2) {
        RX.done.add(s.a0);
        v.pause();
        RX.held = {s, auto: true};
        RX.audios.forEach(x => { x.a.currentTime = s.a0; x.a.playbackRate = 1; x.a.play().catch(() => {}); });
        setState(`The coach talked here with the game paused (${Math.round(s.a1 - s.a0)} s). The video goes on after.`);
        RX.lastT = rt;
        return;
      }
    }
  }
  RX.lastT = rt;
  let any = false;
  RX.audios.forEach(({a, t}) => {
    const at = talkAt(t, rt);
    if (!at || v.paused) { if (!a.paused) a.pause(); return; }
    any = true;
    const rate = Math.min(4, Math.max(0.25, v.playbackRate / at.s.speed));
    if (Math.abs(a.playbackRate - rate) > 0.01) a.playbackRate = rate;
    if (Math.abs(a.currentTime - at.pos) > 0.3) a.currentTime = at.pos;
    if (a.paused) a.play().catch(() => {});
  });
  setState(any ? '' : (v.paused ? '' : 'No review talk at this moment.'));
}
