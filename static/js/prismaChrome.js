// RUFFS — Prisma interface chrome.
//
// Builds the pieces of the Prisma design that plain CSS can't: the chat
// header (title + "model · N messages · $cost" + context bar) and the
// composer toolbar (attach / web / shell / document, model picker next to
// the Agent/Chat switch; Nobody mode goes in the "more tools" menu). Everything is applied only while
// <html> has .skin-prisma and is undone when the user switches to Classic,
// so the original layout stays exactly as it was.

import { getSessionCost } from './chatRenderer.js';

const SKIN_CLASS = 'skin-prisma';
const $ = (id) => document.getElementById(id);

const ICONS = {
  clip: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11.5l-8.2 8.2a4.8 4.8 0 0 1-6.8-6.8l8.6-8.6a3.2 3.2 0 0 1 4.5 4.5l-8.4 8.4a1.6 1.6 0 0 1-2.3-2.3l7.6-7.6"/></svg>',
  doc: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 3.5h7l4.5 4.5v12.5H7z"/><path d="M14 3.5V8h4.5"/></svg>',
  eyeoff: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 3.5l17 17M10.4 6.2A9 9 0 0 1 12 6c5.8 0 9 6 9 6a15.6 15.6 0 0 1-2.7 3.4M6.7 6.7C4.2 8.3 3 12 3 12s3.2 6 9 6a8.6 8.6 0 0 0 4.1-1"/></svg>',
  globe: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5a13 13 0 0 1 0 17 13 13 0 0 1 0-17z"/></svg>',
};

let _applied = false;
const _undo = [];          // functions that revert what apply() did
let _statsTimer = null;

// ── Formatting ──
function _fmtCost(v) {
  if (!(v > 0)) return '';
  return '$' + (v < 0.01 ? v.toFixed(4) : v < 1 ? v.toFixed(3) : v.toFixed(2));
}
function _shortModel(label) {
  const t = String(label || '').trim();
  if (!t || /^select model$/i.test(t)) return '';
  return t.split('/').pop();
}

// ── Session stats (read from the rendered messages) ──
function _sessionStats() {
  const hist = $('chat-history');
  const out = { count: 0, cost: 0, ctx: null };
  if (!hist) return out;
  out.count = hist.querySelectorAll(':scope > .msg:not(.tool-splash)').length;
  let sum = 0;
  hist.querySelectorAll('[data-cost-usd]').forEach(el => {
    const v = parseFloat(el.dataset.costUsd);
    if (v > 0) sum += v;
  });
  // Cost accumulated live in this browser (covers turns whose history lacks
  // usage data); take whichever is larger so nothing is double counted.
  let live = 0;
  try { live = getSessionCost() || 0; } catch (_) {}
  out.cost = Math.max(sum, live);
  const withCtx = hist.querySelectorAll('[data-ctx-pct]');
  if (withCtx.length) {
    const v = parseFloat(withCtx[withCtx.length - 1].dataset.ctxPct);
    if (!isNaN(v)) out.ctx = v;
  }
  return out;
}

function _renderStats() {
  _statsTimer = null;
  if (!_applied) return;
  const sub = $('prisma-sub');
  const ctxWrap = $('prisma-ctx');
  const st = _sessionStats();
  if (sub) {
    const parts = [];
    const model = _shortModel(($('model-picker-label') || {}).textContent);
    if (model) parts.push(model);
    if (st.count) parts.push(`${st.count} message${st.count === 1 ? '' : 's'}`);
    const c = _fmtCost(st.cost);
    if (c) parts.push(c);
    sub.textContent = parts.join(' · ');
    sub.title = c ? `Session cost (real, reported by the provider): ${c}` : '';
  }
  if (ctxWrap) {
    const welcome = ($('chat-container') || {}).classList?.contains('welcome-active');
    let ctxVal = st.ctx;
    let estimated = false;
    // Older chats (or replies without usage data): estimate from the visible
    // conversation text against the model's real window size.
    if (ctxVal == null && !welcome) {
      const len = _contextLength();
      if (len > 0) {
        const chars = (($('chat-history') || {}).innerText || '').length;
        ctxVal = Math.min(100, Math.round((chars / 4 / len) * 1000) / 10);
        estimated = true;
      }
    }
    const pct = ctxVal == null ? 0 : Math.max(0, Math.min(100, ctxVal));
    ctxWrap.hidden = !!welcome;
    const label = ctxWrap.querySelector('.prisma-ctx-label');
    const bar = ctxWrap.querySelector('.prisma-ctx-bar i');
    const shown = pct < 1 && pct > 0 ? pct.toFixed(1) : String(Math.round(pct));
    if (label) label.textContent = ctxVal == null ? 'Context —' : `Context ${estimated ? '~' : ''}${shown}%`;
    if (bar) bar.style.width = Math.max(pct, pct > 0 ? 1.5 : 0) + '%';
    ctxWrap.classList.toggle('warn', pct >= 70 && pct < 85);
    ctxWrap.classList.toggle('danger', pct >= 85);
    ctxWrap.title = ctxVal == null
      ? 'Context window used by this chat (shown after the first reply)'
      : `${estimated ? 'About ' : ''}${shown}% of the model's ${(_contextLength() || 0).toLocaleString()}-token context window`
        + (estimated ? ' (estimated from the visible text; exact after the next reply)' : '');
  }
}

// Context window size of the current chat's model (cached per session).
const _ctxLenCache = new Map();
function _contextLength() {
  const sm = window.sessionModule;
  const sid = sm && sm.getCurrentSessionId ? sm.getCurrentSessionId() : null;
  if (!sid) return 0;
  const cached = _ctxLenCache.get(sid);
  if (typeof cached === 'number') return cached;
  if (cached === undefined) {
    _ctxLenCache.set(sid, null);  // in flight
    fetch(`/api/session/${encodeURIComponent(sid)}/context_info`, { credentials: 'same-origin' })
      .then(r => (r.ok ? r.json() : null))
      .then(d => { _ctxLenCache.set(sid, (d && d.context_length) || 0); _scheduleStats(); })
      .catch(() => _ctxLenCache.set(sid, 0));
  }
  return 0;
}
function _scheduleStats() {
  if (_statsTimer) return;
  _statsTimer = requestAnimationFrame(_renderStats);
}

// ── Helpers that remember how to undo ──
function _move(node, newParent, before) {
  if (!node || !newParent) return;
  const oldParent = node.parentNode, oldNext = node.nextSibling;
  newParent.insertBefore(node, before || null);
  _undo.push(() => { if (oldParent) oldParent.insertBefore(node, oldNext); });
}
function _add(node, parent, before) {
  parent.insertBefore(node, before || null);
  _undo.push(() => node.remove());
}
function _swapHtml(node, html) {
  if (!node) return;
  const prev = node.innerHTML;
  node.innerHTML = html;
  _undo.push(() => { node.innerHTML = prev; });
}
function _observe(target, opts, fn) {
  if (!target) return;
  const mo = new MutationObserver(fn);
  mo.observe(target, opts);
  _undo.push(() => mo.disconnect());
}
function _listen(target, ev, fn) {
  target.addEventListener(ev, fn);
  _undo.push(() => target.removeEventListener(ev, fn));
}

function _toolBtn(id, title, icon, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.id = id;
  b.className = 'input-icon-btn prisma-tool-btn';
  b.title = title;
  b.setAttribute('aria-label', title);
  b.innerHTML = icon;
  b.addEventListener('click', onClick);
  return b;
}

// ── Apply / revert ──
function apply() {
  if (_applied) return;
  _applied = true;

  // 1) Header: subtitle + context bar
  const overlay = document.querySelector('.chat-meta-overlay');
  const topBar = document.querySelector('.chat-top-bar');
  if (overlay) {
    const sub = document.createElement('span');
    sub.id = 'prisma-sub';
    sub.className = 'prisma-sub';
    _add(sub, overlay);
  }
  if (topBar) {
    const ctx = document.createElement('div');
    ctx.id = 'prisma-ctx';
    ctx.className = 'prisma-ctx';
    ctx.innerHTML = '<span class="prisma-ctx-label">Context —</span><span class="prisma-ctx-bar"><i></i></span>';
    _add(ctx, topBar);
  }

  // 2) Composer toolbar, in the order of the design:
  //    [more] attach · web · shell · document  …  model · Agent/Chat · send
  //    (Nobody mode: inside [more])
  const left = document.querySelector('.chat-input-left');
  const right = document.querySelector('.chat-input-right');
  const webBtn = $('web-toggle-btn');
  const bashBtn = $('bash-toggle-btn');
  if (left) {
    const attach = _toolBtn('prisma-attach-btn', 'Attach files', ICONS.clip, () => {
      const b = $('overflow-attach-btn'); if (b) b.click(); else { const fi = $('file-input'); if (fi) fi.click(); }
    });
    _add(attach, left, webBtn || null);

    const doc = _toolBtn('prisma-doc-btn', 'Documents', ICONS.doc, () => { const b = $('overflow-doc-btn'); if (b) b.click(); });
    const after = bashBtn ? bashBtn.nextSibling : null;
    _add(doc, left, after);

    // Nobody mode lives inside the "more tools" menu (^) instead of the
    // toolbar: on a phone the extra icon crowded the model picker.
    const menu = $('overflow-menu');
    const inc = document.createElement('button');
    inc.type = 'button';
    inc.id = 'overflow-incognito-btn';
    inc.className = 'overflow-menu-item';
    inc.innerHTML = ICONS.eyeoff + '<span>Nobody mode</span><span class="overflow-active-dot"></span>';
    // Same as the menu's own items: don't steal focus from the message box
    // (keeps the phone keyboard up), and close the menu after picking.
    inc.addEventListener('pointerdown', (e) => { e.preventDefault(); });
    inc.addEventListener('click', () => {
      if (inc.getAttribute('aria-disabled') !== 'true') { const b = $('incognito-btn'); if (b) b.click(); }
      const plus = $('overflow-plus-btn');
      if (plus && !inc.closest('.overflow-menu')?.classList.contains('hidden')) plus.click();
    });
    if (menu) _add(inc, menu);

    // Mirror on/off state of the proxied controls.
    const syncStates = () => {
      const docSrc = $('overflow-doc-btn');
      const docOn = !!(docSrc && (docSrc.classList.contains('active') || docSrc.getAttribute('aria-pressed') === 'true'));
      doc.setAttribute('aria-pressed', docOn ? 'true' : 'false');
      doc.classList.toggle('active', docOn);
      const chk = $('incognito-toggle');
      const incOn = !!(chk && chk.checked);
      inc.setAttribute('aria-pressed', incOn ? 'true' : 'false');
      inc.classList.toggle('active', incOn);
      // Nobody mode can only be switched on before the chat starts.
      const ws = $('welcome-screen');
      const locked = !incOn && ws && ws.classList.contains('hidden');
      inc.setAttribute('aria-disabled', locked ? 'true' : 'false');
      inc.title = locked ? 'Nobody mode can be turned on from a new chat'
                         : (incOn ? 'Nobody mode is on — click to turn off' : 'Nobody mode — no memory, no history saved');
      // Light up the dot on the ^ button when Nobody mode is on (app.js counts
      // the active items of the menu).
      document.dispatchEvent(new Event('overflow-state-change'));
    };
    syncStates();
    _observe($('overflow-doc-btn'), { attributes: true, attributeFilter: ['class', 'aria-pressed'] }, syncStates);
    _observe($('incognito-btn'), { attributes: true, attributeFilter: ['class', 'style'] }, syncStates);
    _observe($('welcome-screen'), { attributes: true, attributeFilter: ['class'] }, syncStates);
  }
  // Web search uses the globe icon in the design.
  if (webBtn) {
    const svg = webBtn.querySelector('svg');
    if (svg) {
      const holder = document.createElement('span');
      holder.innerHTML = ICONS.globe;
      const globe = holder.firstChild;
      webBtn.replaceChild(globe, svg);
      _undo.push(() => { if (globe.parentNode === webBtn) webBtn.replaceChild(svg, globe); });
    }
  }
  // Model picker moves from the textarea corner into the toolbar.
  const picker = $('model-picker-wrap');
  const modeToggle = right && right.querySelector('.mode-toggle');
  if (picker && right) _move(picker, right, modeToggle || right.firstChild);

  // 3) Keep the header stats live.
  _observe($('chat-history'), { childList: true, subtree: true, attributes: true, attributeFilter: ['data-ctx-pct', 'data-cost-usd'] }, _scheduleStats);
  _observe($('model-picker-label'), { childList: true, characterData: true, subtree: true }, _scheduleStats);
  _observe($('chat-container'), { attributes: true, attributeFilter: ['class'] }, _scheduleStats);
  _observe($('session-cost-display'), { childList: true, characterData: true, subtree: true }, _scheduleStats);
  _listen(window, 'ruffs:metrics', _scheduleStats);
  _scheduleStats();
}

function revert() {
  if (!_applied) return;
  _applied = false;
  while (_undo.length) {
    const fn = _undo.pop();
    try { fn(); } catch (e) { console.warn('[prisma] revert step failed', e); }
  }
}

function sync() {
  if (document.documentElement.classList.contains(SKIN_CLASS)) apply();
  else revert();
}

function init() {
  // The rail logo reloads RUFFS (handy in the desktop window, which has no
  // browser reload button). New chat stays on the "+" button below it.
  const logo = $('rail-logo');
  if (logo) logo.addEventListener('click', () => { window.location.reload(); });
  sync();
  new MutationObserver(sync).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
else init();

export default { sync };
