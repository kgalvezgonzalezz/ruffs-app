// RUFFS — aviso de nueva versión publicada en GitHub (solo en copias instaladas).
//
// Al abrir RUFFS (y cada 12 h) pregunta a /api/ruffs/update. Si hay una versión
// más nueva muestra: "Salió una nueva versión de RUFFS en GitHub. ¿Deseas
// instalarla ahora?" con el número de versión y sus novedades. Botones:
// Instalar ahora · Más tarde (24 h) · Omitir esta versión. También añade un
// botón junto a Ajustes para buscar actualizaciones a mano.
import { mdToHtml } from './markdown.js';

const SKIP_KEY = 'ruffs-update-skip';
const SNOOZE_KEY = 'ruffs-update-snooze-until';
const CHECK_EVERY_MS = 12 * 3600 * 1000;

function store(key, value) {
  try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch {}
}
function read(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function toast(msg) {
  if (typeof window.showToast === 'function') window.showToast(msg);
  else console.info(msg);
}

async function fetchInfo(force = false) {
  const r = await fetch('/api/ruffs/update' + (force ? '?force=true' : ''), { credentials: 'same-origin' });
  if (!r.ok) return null;
  return r.json();
}

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const c of children) if (c != null) n.append(c);
  return n;
}

function closeModal(overlay) {
  overlay.remove();
  document.removeEventListener('keydown', overlay._onKey);
}

async function pollInstall(statusEl, buttons) {
  for (let i = 0; i < 600; i++) {
    await new Promise(r => setTimeout(r, 1000));
    let st = null;
    try { st = await (await fetch('/api/ruffs/update/status', { credentials: 'same-origin' })).json(); }
    catch { st = { state: 'closing' }; }  // el servidor ya se cerró para instalar
    if (st.state === 'error') {
      statusEl.textContent = st.detail || 'No se pudo actualizar.';
      buttons.forEach(b => (b.disabled = false));
      return;
    }
    if (st.state === 'closing' || st.state === 'launching') {
      statusEl.textContent = 'Abriendo el instalador… RUFFS se cerrará ahora y se volverá a abrir al terminar. '
        + 'Tus chats, memorias y ajustes se conservan.';
      if (st.state === 'closing') return;
    } else if (st.detail) {
      statusEl.textContent = st.detail;
    }
  }
}

function showUpdateModal(info) {
  document.getElementById('ruffs-update-overlay')?.remove();
  const notes = el('div', { class: 'ruffs-update-notes', html: mdToHtml(info.notes || '_(Esta versión no trae notas.)_') });
  const status = el('p', { class: 'ruffs-update-status' });
  const installBtn = el('button', { class: 'confirm-btn confirm-btn-primary' }, 'Instalar ahora');
  const laterBtn = el('button', { class: 'confirm-btn confirm-btn-secondary' }, 'Más tarde');
  const skipBtn = el('button', { class: 'confirm-btn confirm-btn-secondary' }, 'Omitir esta versión');
  const overlay = el('div', { id: 'ruffs-update-overlay', class: 'modal' },
    el('div', { class: 'modal-content styled-confirm-box ruffs-update-box', role: 'dialog', 'aria-modal': 'true',
                'aria-labelledby': 'ruffs-update-title' },
      el('div', { class: 'modal-header' }, el('h4', { id: 'ruffs-update-title' }, 'Nueva versión de RUFFS')),
      el('div', { class: 'modal-body' },
        el('p', {}, 'Salió una nueva versión de RUFFS en GitHub: ', el('strong', {}, 'v' + info.latest),
           ` (tienes la v${info.current}). ¿Deseas instalarla ahora?`),
        el('p', { class: 'ruffs-update-sub' }, 'Novedades:'),
        notes,
        info.can_install ? null : el('p', { class: 'ruffs-update-why' },
          (info.why_not || 'No se puede instalar desde aquí.') + ' ',
          info.release_url ? el('a', { href: info.release_url, target: '_blank', rel: 'noopener noreferrer' }, 'Ver en GitHub') : null),
        status),
      el('div', { class: 'modal-footer' }, skipBtn, laterBtn, installBtn)));
  installBtn.disabled = !info.can_install;
  const buttons = [installBtn, laterBtn, skipBtn];
  overlay._onKey = e => { if (e.key === 'Escape') { store(SNOOZE_KEY, String(Date.now() + 24 * 3600 * 1000)); closeModal(overlay); } };
  document.addEventListener('keydown', overlay._onKey);
  laterBtn.addEventListener('click', () => { store(SNOOZE_KEY, String(Date.now() + 24 * 3600 * 1000)); closeModal(overlay); });
  skipBtn.addEventListener('click', () => { store(SKIP_KEY, info.latest); closeModal(overlay); });
  installBtn.addEventListener('click', async () => {
    buttons.forEach(b => (b.disabled = true));
    status.textContent = 'Descargando la nueva versión…';
    try {
      const r = await fetch('/api/ruffs/update/install', { method: 'POST', credentials: 'same-origin' });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        status.textContent = d.detail || 'No se pudo iniciar la actualización.';
        buttons.forEach(b => (b.disabled = false));
        installBtn.disabled = !info.can_install;
        return;
      }
      await pollInstall(status, buttons);
    } catch {
      status.textContent = 'RUFFS se está cerrando para instalar la actualización…';
    }
  });
  document.body.appendChild(overlay);
  installBtn.focus();
}

function setBadge(on) {
  for (const id of ['ruffs-update-btn', 'ruffs-update-rail-btn']) {
    document.getElementById(id)?.classList.toggle('has-update', !!on);
  }
}

function ensureButton(info) {
  if (!info || !info.installed || document.getElementById('ruffs-update-btn')) return;
  const bar = document.querySelector('#sidebar-user-bar .user-bar-actions');
  if (!bar) return;
  const btn = el('button', { type: 'button', class: 'user-bar-btn', id: 'ruffs-update-btn',
                             title: 'Buscar actualizaciones de RUFFS', 'aria-label': 'Buscar actualizaciones de RUFFS',
                             html: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>' });
  btn.addEventListener('click', () => checkForUpdates({ manual: true }));
  bar.prepend(btn);
  // También en la barra de iconos (menú lateral contraído), sobre el engranaje.
  const railSettings = document.getElementById('rail-settings');
  if (railSettings && !document.getElementById('ruffs-update-rail-btn')) {
    const rb = el('button', { type: 'button', class: 'icon-rail-btn', id: 'ruffs-update-rail-btn',
                              title: 'Buscar actualizaciones de RUFFS', 'aria-label': 'Buscar actualizaciones de RUFFS',
                              html: btn.innerHTML });
    rb.addEventListener('click', () => checkForUpdates({ manual: true }));
    railSettings.parentNode.insertBefore(rb, railSettings);
  }
}

export async function checkForUpdates({ manual = false } = {}) {
  let info = null;
  try { info = await fetchInfo(manual); } catch { info = null; }
  if (!info) { if (manual) toast('No se pudo consultar GitHub ahora. Inténtalo más tarde.'); return; }
  ensureButton(info);
  if (!info.installed || !info.enabled) return;
  setBadge(info.newer);
  if (!info.newer) {
    if (manual) toast(info.error ? info.error : `Tienes la última versión de RUFFS (v${info.current}).`);
    return;
  }
  if (!manual) {
    if (read(SKIP_KEY) === info.latest) return;
    if (Number(read(SNOOZE_KEY) || 0) > Date.now()) return;
  }
  showUpdateModal(info);
}

function start() {
  setTimeout(() => checkForUpdates(), 6000);
  setInterval(() => checkForUpdates(), CHECK_EVERY_MS);
  window.ruffsCheckForUpdates = () => checkForUpdates({ manual: true });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
else start();
