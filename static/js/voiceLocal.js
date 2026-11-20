// static/js/voiceLocal.js
//
// RUFFS: voz local sin internet (Vosk en el servidor de RUFFS).
//
// Dentro de la ventana de RUFFS el reconocimiento de voz del navegador no
// funciona (WebView2/WKWebView: «network» al medio segundo), así que el audio
// del micrófono se manda a /api/voice/* en trozos de ~0,25 s (PCM 16 kHz
// mono) y el servidor devuelve el texto. Aquí también se detecta el silencio:
// cuando dejas de hablar, la grabación termina sola.

const API = '/api/voice';
const CHUNK_MS = 250;

function _floatTo16(f32) {
  const out = new Int16Array(f32.length);
  for (let i = 0; i < f32.length; i++) {
    const s = Math.max(-1, Math.min(1, f32[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function _downsample(f32, inRate, outRate) {
  if (inRate === outRate) return f32;
  const ratio = inRate / outRate;
  const len = Math.floor(f32.length / ratio);
  const out = new Float32Array(len);
  for (let i = 0; i < len; i++) {
    const start = Math.floor(i * ratio), end = Math.min(f32.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end; j++) sum += f32[j];
    out[i] = sum / Math.max(1, end - start);
  }
  return out;
}

async function _json(r) { try { return await r.json(); } catch (_) { return {}; } }

export async function status(lang) {
  const r = await fetch(`${API}/status${lang ? '?lang=' + encodeURIComponent(lang) : ''}`, { credentials: 'same-origin' });
  return _json(r);
}

/** Deja el modelo listo (lo descarga la primera vez, ~40 MB). */
export async function ensureModel({ lang = '', onProgress } = {}) {
  let st = await status(lang);
  if (!st.engine) throw new Error('El motor de voz local no está instalado en este RUFFS');
  if (st.ready) return st;
  await fetch(`${API}/model`, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ lang }),
  });
  const t0 = Date.now();
  while (Date.now() - t0 < 15 * 60 * 1000) {
    await new Promise(r => setTimeout(r, 1000));
    st = await status(lang);
    if (st.ready) return st;
    const d = st.download || {};
    if (d.status === 'error') throw new Error(d.error || 'No se pudo descargar el modelo de voz');
    if (onProgress) onProgress(d.progress || 0, st);
  }
  throw new Error('La descarga del modelo de voz tardó demasiado');
}

/**
 * Una escucha con el micrófono: modo «dictate» (dictado) o «wake» (espera la
 * palabra clave y sigue como dictado).
 *
 * opts: { mode, keyword, lang, silenceMs, maxWaitMs, onWake, onPartial,
 *         onHeard, onLevel, shouldIgnoreWake }
 * Devuelve { done: Promise<string>, stop(), cancel() }.
 */
export function listen(opts = {}) {
  const mode0 = opts.mode || 'dictate';
  const silenceMs = Math.max(600, opts.silenceMs || 1500);
  const maxWaitMs = opts.maxWaitMs || 8000;      // sin voz este tiempo → termina vacío
  let sid = null, ctx = null, stream = null, node = null, source = null;
  let closed = false, finishing = false, mode = mode0;
  let queue = [], sending = false, pending = new Float32Array(0), inRate = 16000;
  let lastVoice = 0, heardSpeech = false, dictateStart = 0, lastText = '';
  let floor = 0.01;
  let resolveDone, rejectDone;
  const done = new Promise((res, rej) => { resolveDone = res; rejectDone = rej; });

  async function openSession() {
    const r = await fetch(`${API}/session`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: mode0, keyword: opts.keyword || 'RUFFS', lang: opts.lang || '' }),
    });
    const j = await _json(r);
    if (!r.ok) throw new Error(j.detail || 'No se pudo iniciar la voz local');
    return j.id;
  }

  async function pump() {
    if (sending) return;
    sending = true;
    try {
      while (queue.length && !closed) {
        const chunk = queue.shift();
        const r = await fetch(`${API}/session/${sid}/audio`, {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/octet-stream' }, body: chunk,
        });
        if (!r.ok) throw new Error('La sesión de voz se cerró');
        const j = await _json(r);
        if (j.wake && mode === 'wake') {
          if (opts.shouldIgnoreWake && opts.shouldIgnoreWake()) {
            // RUFFS hablando: su propia voz no cuenta. Reinicia la espera.
            await restartWake();
            continue;
          }
          mode = 'dictate';
          dictateStart = Date.now();
          lastVoice = Date.now();
          heardSpeech = false;
          if (opts.onWake) opts.onWake();
        } else if (j.heard !== undefined && opts.onHeard) {
          opts.onHeard(j.heard);
        }
        if (mode === 'dictate' && typeof j.text === 'string') {
          if (j.text && j.text !== lastText) { lastVoice = Date.now(); heardSpeech = true; }
          lastText = j.text;
          if (opts.onPartial) opts.onPartial(j.text);
        }
      }
    } catch (e) {
      fail(e);
    } finally {
      sending = false;
    }
  }

  async function restartWake() {
    try { await fetch(`${API}/session/${sid}`, { method: 'DELETE', credentials: 'same-origin' }); } catch (_) {}
    queue = [];
    sid = await openSession();
  }

  function onAudio(f32) {
    if (closed) return;
    // Nivel de voz (RMS) con suelo de ruido adaptativo
    let sum = 0;
    for (let i = 0; i < f32.length; i++) sum += f32[i] * f32[i];
    const rms = Math.sqrt(sum / Math.max(1, f32.length));
    const thr = Math.max(0.012, floor * 2.5);
    if (rms > thr) { if (mode === 'dictate') { lastVoice = Date.now(); heardSpeech = true; } }
    else floor = floor * 0.95 + rms * 0.05;
    if (opts.onLevel) opts.onLevel(rms, rms > thr);

    const d = _downsample(f32, inRate, 16000);
    const merged = new Float32Array(pending.length + d.length);
    merged.set(pending); merged.set(d, pending.length);
    const per = Math.round(16000 * CHUNK_MS / 1000);
    let off = 0;
    while (merged.length - off >= per) {
      queue.push(_floatTo16(merged.subarray(off, off + per)).buffer);
      off += per;
    }
    pending = merged.slice(off);
    pump();

    if (mode === 'dictate' && !finishing) {
      const now = Date.now();
      const since = dictateStart || now;
      if (heardSpeech && now - lastVoice > silenceMs) finish();
      else if (!heardSpeech && now - since > maxWaitMs) finish();
    }
  }

  function release() {
    try { node && node.disconnect(); } catch (_) {}
    try { source && source.disconnect(); } catch (_) {}
    try { stream && stream.getTracks().forEach(t => t.stop()); } catch (_) {}
    try { ctx && ctx.close(); } catch (_) {}
  }

  async function finish() {
    if (finishing || closed) return;
    finishing = true;
    // Espera a que salga lo que queda en la cola y pide el texto final.
    if (pending.length) { queue.push(_floatTo16(pending).buffer); pending = new Float32Array(0); }
    const t0 = Date.now();
    pump();
    while ((queue.length || sending) && Date.now() - t0 < 5000) await new Promise(r => setTimeout(r, 40));
    closed = true;
    release();
    try {
      const r = await fetch(`${API}/session/${sid}/end`, { method: 'POST', credentials: 'same-origin' });
      const j = await _json(r);
      resolveDone((j.text || lastText || '').trim());
    } catch (e) { resolveDone((lastText || '').trim()); }
  }

  function fail(e) {
    if (closed) return;
    closed = true;
    release();
    if (sid) fetch(`${API}/session/${sid}`, { method: 'DELETE', credentials: 'same-origin' }).catch(() => {});
    rejectDone(e instanceof Error ? e : new Error(String(e)));
  }

  (async () => {
    try {
      sid = await openSession();
      // Sin cancelación de eco ni ganancia automática: con esos filtros Windows
      // trata el micrófono como una llamada y BAJA un 80 % (o silencia) los
      // demás sonidos mientras está abierto — la voz de RUFFS no se oía.
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 },
      });
      const AC = window.AudioContext || window.webkitAudioContext;
      try { ctx = new AC({ sampleRate: 16000 }); } catch (_) { ctx = new AC(); }
      inRate = ctx.sampleRate;
      source = ctx.createMediaStreamSource(stream);
      node = ctx.createScriptProcessor(4096, 1, 1);
      node.onaudioprocess = (ev) => onAudio(ev.inputBuffer.getChannelData(0));
      source.connect(node);
      node.connect(ctx.destination);   // necesario para que corra el procesador (sale en silencio)
      if (ctx.state === 'suspended') { try { await ctx.resume(); } catch (_) {} }
      dictateStart = mode === 'dictate' ? Date.now() : 0;
      lastVoice = Date.now();
    } catch (e) {
      fail(e);
    }
  })();

  return {
    done,
    get mode() { return mode; },
    stop() { if (!closed) finish(); },
    cancel() { if (!closed) fail(new Error('cancelado')); },
  };
}

const voiceLocal = { status, ensureModel, listen };
window.ruffsVoiceLocal = voiceLocal;
export default voiceLocal;
