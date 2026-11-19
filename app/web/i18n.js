/* The page's languages (loaded before app.js). Tables: i18n/<code>.json, English text as the key, {name} for the
   values. t(key, vars) = the text in the player's language, English when the table has none. tx(text) = English
   text the app wrote (Activity lines, notes, error reasons) in the player's language, values kept.
   Game terms (legends, weapons, maps, POIs, ring, knock, Ranked...) stay in English: see i18n/glossary.md.
   Changing the language reloads the page (a second; the app keeps running). */
'use strict';
const I18N = (() => {
  const empty = {lang: 'en', languages: [{code: 'en', name: 'English'}], en: {}, tr: {}};
  try {
    const q = new URLSearchParams(location.search).get('t');
    let tok = q || ''; try { tok = q || sessionStorage.getItem('pcs-t') || ''; } catch (e) {}
    const x = new XMLHttpRequest();
    x.open('GET', '/api/i18n', false);                 // before anything is drawn: one small local request
    x.setRequestHeader('X-PCS-Token', tok);
    x.send();
    if (x.status === 200) return Object.assign(empty, JSON.parse(x.responseText));
  } catch (e) {}
  return empty;
})();
document.documentElement.lang = I18N.lang;

function t(key, vars) {
  let s = (I18N.tr && I18N.tr[key]) || (I18N.en && I18N.en[key]) || key;
  if (vars) s = s.replace(/\{(\w+)\}/g, (m, k) => (k in vars && vars[k] != null ? vars[k] : m));
  return s;
}
let TX_PATTERNS = null;
function txPatterns() {
  if (TX_PATTERNS) return TX_PATTERNS;
  const reEsc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  TX_PATTERNS = Object.keys(I18N.en || {}).filter(k => k.includes('{')).map(k => {
    const parts = k.split(/\{(\w+)\}/), fixed = parts.filter((p, i) => i % 2 === 0).join('');
    if (!fixed.trim()) return null;
    const names = parts.filter((p, i) => i % 2 === 1);
    const rx = new RegExp('^' + parts.map((p, i) => i % 2 === 0 ? reEsc(p) : '([\\s\\S]+?)').join('') + '$');
    return {k, rx, names, n: fixed.length};
  }).filter(Boolean).sort((a, b) => b.n - a.n);
  return TX_PATTERNS;
}
function tx(text, depth) {
  if (typeof text !== 'string' || !text || I18N.lang === 'en') return text;
  const tr = I18N.tr || {};
  if (tr[text]) return tr[text];
  if ((depth || 0) > 2) return text;
  for (const p of txPatterns()) {
    if (!tr[p.k]) continue;
    const m = p.rx.exec(text); if (!m) continue;
    const vals = {}; p.names.forEach((n, i) => { vals[n] = tx(m[i + 1], (depth || 0) + 1); });
    return tr[p.k].replace(/\{(\w+)\}/g, (x, k) => (k in vals ? vals[k] : x));
  }
  return text;
}
/* the fixed words of index.html: data-i18n (text), data-i18n-title, data-i18n-aria */
function translateStatic(root) {
  (root || document).querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
  (root || document).querySelectorAll('[data-i18n-title]').forEach(el => { el.title = t(el.dataset.i18nTitle); });
  (root || document).querySelectorAll('[data-i18n-aria]').forEach(el => { el.setAttribute('aria-label', t(el.dataset.i18nAria)); });
}
document.addEventListener('DOMContentLoaded', () => translateStatic());
if (document.readyState !== 'loading') translateStatic();
