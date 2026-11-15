// RUFFS — pick up the automatic chat title.
//
// The server names a chat in the background right after a reply finishes
// (and refreshes the name as the conversation grows — see
// routes/chat_helpers.py:should_auto_title). That happens after the stream
// has closed, so the page re-reads the session list a few seconds later to
// show the new title in the header and the sidebar.

const RECHECK_DELAYS_MS = [3500, 9000, 20000];
const RETITLE_AT = new Set([4, 10]);   // user messages — keep in sync with the server
const _timers = new Map();

// Default name the page gives a new chat: "<model> <locale time>".
const DEFAULT_NAME_RE = /^.+ \S{0,4}\d{1,2}:\d{2}:\d{2}(\s*([ap]\.?\s?m\.?|午前|午後|上午|下午))?$/i;

function _currentName() {
  const el = document.getElementById('current-meta');
  return el ? (el.textContent || '').trim() : '';
}

function _userMessageCount() {
  const hist = document.getElementById('chat-history');
  return hist ? hist.querySelectorAll(':scope > .msg-user').length : 0;
}

function _titleMayChange() {
  const name = _currentName();
  if (!name || name === 'RUFFS Chat' || name === 'Chat' || DEFAULT_NAME_RE.test(name)) return true;
  return RETITLE_AT.has(_userMessageCount());
}

function _schedule(sessionId) {
  const sm = window.sessionModule;
  if (!sm || !sm.loadSessions || !_titleMayChange()) return;
  (_timers.get(sessionId) || []).forEach(clearTimeout);
  const startName = _currentName();
  const ids = RECHECK_DELAYS_MS.map((ms, i) => setTimeout(async () => {
    // Stop once the title changed or the user moved to another chat.
    if (sm.getCurrentSessionId && sm.getCurrentSessionId() !== sessionId) return;
    if (i > 0 && _currentName() !== startName) return;
    try { await sm.loadSessions(); } catch (_) { /* offline — try next tick */ }
  }, ms));
  _timers.set(sessionId, ids);
}

// Older chats that still carry the placeholder name get a title the first
// time they are opened (one request per chat per page load).
const _backfilled = new Set();
let _backfillTimer = null;
function _maybeBackfill() {
  clearTimeout(_backfillTimer);
  _backfillTimer = setTimeout(async () => {
    const sm = window.sessionModule;
    const sid = sm && sm.getCurrentSessionId ? sm.getCurrentSessionId() : null;
    const name = _currentName();
    if (!sid || _backfilled.has(sid) || !DEFAULT_NAME_RE.test(name)) return;
    if (_userMessageCount() === 0) return;
    if (document.querySelector('.send-btn[data-mode="streaming"]')) return;  // a reply is running: the normal path will name it
    _backfilled.add(sid);
    try {
      const r = await fetch(`/api/session/${encodeURIComponent(sid)}/auto-title`, { method: 'POST', credentials: 'same-origin' });
      const d = r.ok ? await r.json() : null;
      if (d && d.changed && sm.loadSessions) await sm.loadSessions();
    } catch (_) { /* offline / model unavailable — try again next load */ }
  }, 1200);
}
function _watchHeader() {
  const meta = document.getElementById('current-meta');
  if (!meta) return;
  new MutationObserver(_maybeBackfill).observe(meta, { childList: true, characterData: true, subtree: true });
  _maybeBackfill();
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _watchHeader, { once: true });
else _watchHeader();

window.addEventListener('ruffs:stream-done', (e) => {
  const sid = (e && e.detail && e.detail.sessionId) ||
    (window.sessionModule && window.sessionModule.getCurrentSessionId && window.sessionModule.getCurrentSessionId());
  if (sid) _schedule(sid);
});

export default { _titleMayChange };
