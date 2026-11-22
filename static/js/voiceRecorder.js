// static/js/voiceRecorder.js

/**
 * Voice recording with optional Speech-to-Text transcription.
 *
 * STT providers:
 *   "disabled"       — record audio as file attachment (original behavior)
 *   "browser"        — use Web Speech API for real-time transcription
 *   "local"          — send recording to server /api/stt/transcribe (Whisper)
 *   "endpoint:<id>"  — send recording to server /api/stt/transcribe (API)
 */

import voiceLocal from './voiceLocal.js';

// ── RUFFS: voz local, envío automático y palabra de activación ──────────────
// Dentro de la ventana de RUFFS (WebView2 / WKWebView) el reconocimiento del
// navegador falla siempre con «network»: ahí «browser» usa la voz local
// (Vosk, sin internet). En Chrome/Edge normales sigue siendo el del navegador.
const _prefs = { autoSend: true, silenceMs: 1500, lang: '', wake: false, keyword: 'RUFFS' };
let _cb = {};            // showToast / showError / onFileCreated de la última grabación
let _local = null;       // dictado local en curso
let _wake = null;        // escucha de la palabra clave en curso
let _wakeTimer = null;
let _wakeErrorShown = false;
let _vad = null;         // detector de silencio para los otros proveedores

function _isEmbeddedWindow() {
  return !!(window.pywebview || (window.chrome && window.chrome.webview)
    || (window.webkit && window.webkit.messageHandlers));
}

function _browserSttWorks() {
  return !!(window.SpeechRecognition || window.webkitSpeechRecognition) && !_isEmbeddedWindow();
}

// RUFFS: en la app de Android el micrófono llega por el reconocedor nativo del
// teléfono (puente de la app); la página no recibe un flujo de audio real, así
// que la voz local (Vosk), que lo necesita, no puede funcionar ahí.
function _inAndroidApp() {
  return !!(window.__ruffs && window.__ruffs.isApp);
}

export function effectiveProvider() {
  if (_inAndroidApp() && (_sttProvider === 'vosk' || _sttProvider === 'browser') && _browserSttWorks()) return 'browser';
  if (_sttProvider === 'vosk') return 'vosk';
  if (_sttProvider === 'browser' && !_browserSttWorks()) return 'vosk';
  return _sttProvider;
}

function _syncUI() { if (window._updateSendBtnIcon) window._updateSendBtnIcon(); }

function _ruffsIsSpeaking() {
  return !!((window.aiTTSManager && window.aiTTSManager.isPlaying)
    || (window.speechSynthesis && window.speechSynthesis.speaking));
}

// Mientras RUFFS habla, la palabra de activación suelta el micrófono (no se
// oye a sí misma y Windows no baja el volumen de la voz) y vuelve al terminar.
let _speechWatch = null;
let _pausedForSpeech = false;
function _watchRuffsSpeech() {
  if (_speechWatch) return;
  _speechWatch = setInterval(() => {
    if (!_prefs.wake) { clearInterval(_speechWatch); _speechWatch = null; _pausedForSpeech = false; return; }
    const speaking = _ruffsIsSpeaking();
    if (speaking && _wake && !_wake.woken && _wake.mode !== 'dictate') {
      _pausedForSpeech = true;
      _pauseWake();
    } else if (!speaking && _pausedForSpeech && !_wake && !_local && !isRecording) {
      _pausedForSpeech = false;
      _resumeWakeSoon(700);
    }
  }, 300);
}

function _beep() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    const ctx = new AC();
    [660, 880].forEach((f, i) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = f; o.connect(g); g.connect(ctx.destination);
      const t = ctx.currentTime + i * 0.12;
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.12, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.11);
      o.start(t); o.stop(t + 0.12);
    });
    setTimeout(() => ctx.close().catch(() => {}), 600);
  } catch (_) {}
}

function _toast(msg, ms) { if (_cb.showToast) _cb.showToast(msg, ms); else if (window.uiModule?.showToast) window.uiModule.showToast(msg, ms); }
function _error(msg) { if (_cb.showError) _cb.showError(msg); else if (window.uiModule?.showError) window.uiModule.showError(msg); }

function _autoSend() {
  const btn = document.querySelector('.send-btn');
  if (btn && btn.dataset.mode === 'streaming') {
    // Pulsar enviar ahora DETENDRÍA la respuesta en curso: se deja escrito.
    _toast('Texto listo en el cuadro: envíalo cuando RUFFS termine de responder');
    return;
  }
  if (typeof window._ruffsSubmitChat === 'function') window._ruffsSubmitChat();
}

function _afterTranscript(text, showToast) {
  if (!text) return;
  insertTranscription(text, _prefs.autoSend ? null : showToast);
  if (_prefs.autoSend) _autoSend();
}

function _showPartial(base, text) {
  const input = document.getElementById('message');
  if (!input) return;
  input.value = base ? (text ? base + ' ' + text : base) : text;
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

let _modelPromise = null;
async function _ensureLocalModel({ quiet = false } = {}) {
  if (!_modelPromise) {
    let lastPct = -1;
    _modelPromise = voiceLocal.ensureModel({
      lang: _prefs.lang,
      onProgress: (p, st) => {
        const pct = Math.floor(p * 10) * 10;
        if (!quiet && pct !== lastPct) { lastPct = pct; _toast(`Descargando la voz local (${st.size_mb || 40} MB, una sola vez)… ${pct}%`, 4000); }
      },
    });
    _modelPromise.catch(() => { _modelPromise = null; });
  }
  return _modelPromise;
}

async function _startLocalDictation() {
  _pauseWake();
  isRecording = true;
  _syncUI();
  try {
    await _ensureLocalModel();
  } catch (e) {
    isRecording = false; _syncUI();
    _error(e.message || String(e));
    _resumeWakeSoon();
    return;
  }
  const input = document.getElementById('message');
  const base = input ? input.value.trim() : '';
  const auto = _prefs.autoSend;
  _local = voiceLocal.listen({
    mode: 'dictate', lang: _prefs.lang,
    silenceMs: auto ? _prefs.silenceMs : 60 * 60 * 1000,
    maxWaitMs: auto ? 8000 : 60 * 60 * 1000,
    onPartial: (t) => _showPartial(base, t),
  });
  _toast(auto ? 'Te escucho… al terminar de hablar se envía solo' : 'Te escucho… pulsa el micrófono para terminar');
  try {
    const text = await _local.done;
    _showPartial(base, '');
    if (text) _afterTranscript(text, _cb.showToast);
    else _toast('No se oyó nada');
  } catch (e) {
    _showPartial(base, '');
    if ((e && e.message) !== 'cancelado') _error('Voz local: ' + (e.message || e));
  } finally {
    _local = null;
    _resetRecordingUI();
    _resumeWakeSoon();
  }
}

// Detector de silencio para los demás proveedores (navegador en Chrome,
// Whisper local o por API): al callar se termina la grabación sola.
function _watchSilence(stream) {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    const ctx = new AC();
    const src = ctx.createMediaStreamSource(stream);
    const an = ctx.createAnalyser();
    an.fftSize = 2048;
    src.connect(an);
    const buf = new Float32Array(an.fftSize);
    let floor = 0.01, heard = false, last = Date.now();
    const t0 = Date.now();
    const timer = setInterval(() => {
      an.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      if (rms > Math.max(0.012, floor * 2.5)) { heard = true; last = Date.now(); }
      else floor = floor * 0.95 + rms * 0.05;
      const now = Date.now();
      if ((heard && now - last > _prefs.silenceMs) || (!heard && now - t0 > 8000)) {
        stop();
        if (isRecording) stopRecording();
      }
    }, 100);
    const stop = () => { clearInterval(timer); try { ctx.close(); } catch (_) {} };
    return { stop };
  } catch (_) { return null; }
}

// ── Palabra de activación ──
export function getWakeState() {
  if (!_wake) return 'off';
  return _wake.mode === 'dictate' || _wake.woken ? 'dictating' : 'listening';
}

function _pauseWake() {
  clearTimeout(_wakeTimer);
  if (!_wake) return;
  if (_wake.browser) { try { window.wakeWordDetector && window.wakeWordDetector.stop(); } catch (_) {} _wake = null; }
  else if (_wake.mode !== 'dictate') { const w = _wake; _wake = null; w.cancel(); }
  _syncUI();
}

function _resumeWakeSoon(ms = 900) {
  clearTimeout(_wakeTimer);
  if (!_prefs.wake || _sttProvider === 'disabled') return;
  _wakeTimer = setTimeout(_startWake, ms);
}

async function _startWake() {
  if (_wake || _local || isRecording || !_prefs.wake || _sttProvider === 'disabled') return;
  _watchRuffsSpeech();
  if (_ruffsIsSpeaking()) { _pausedForSpeech = true; return; }   // vuelve al terminar de hablar
  const kw = (_prefs.keyword || 'RUFFS').trim() || 'RUFFS';
  if (effectiveProvider() !== 'vosk' && window.wakeWordDetector && _browserSttWorks()) {
    // Chrome/Edge normales: detector del navegador.
    const det = window.wakeWordDetector;
    det.setKeyword(kw);
    if (_prefs.lang) det.setLanguage(_prefs.lang);
    det.enabled = true;
    det.onActivate = (command) => {
      _wake = null;
      _beep();
      const words = (command || '').trim().split(/\s+/).filter(Boolean);
      if (words.length >= 2 && (command || '').toLowerCase().indexOf(kw.toLowerCase()) === -1) {
        _afterTranscript(command.trim(), null);
        _resumeWakeSoon(1500);
      } else {
        startRecording(_cb.onFileCreated, _cb.showToast, _cb.showError);
      }
    };
    _wake = { browser: true, mode: 'wake', cancel() { det.stop(); } };
    det.start();
    _syncUI();
    return;
  }
  try {
    await _ensureLocalModel({ quiet: true });
  } catch (e) {
    if (!_wakeErrorShown) { _wakeErrorShown = true; _error('Palabra de activación: ' + (e.message || e)); }
    return;
  }
  if (_wake || _local || isRecording || !_prefs.wake) return;
  let base = '';
  const ctl = voiceLocal.listen({
    mode: 'wake', keyword: kw, lang: _prefs.lang,
    silenceMs: _prefs.silenceMs, maxWaitMs: 6000,
    shouldIgnoreWake: _ruffsIsSpeaking,
    onWake: () => {
      ctl.woken = true;
      isRecording = true;
      const input = document.getElementById('message');
      base = input ? input.value.trim() : '';
      _beep();
      _toast(`Te escucho… (dijiste «${kw}»)`);
      _syncUI();
    },
    onPartial: (t) => _showPartial(base, t),
  });
  _wake = ctl;
  _syncUI();
  ctl.done.then((text) => {
    if (!ctl.woken) return;
    _showPartial(base, '');
    if (text) _afterTranscript(text, null);
    else _toast('No se oyó ninguna orden');
  }).catch((e) => {
    if ((e && e.message) !== 'cancelado' && !_wakeErrorShown) { _wakeErrorShown = true; _error('Palabra de activación: ' + (e.message || e)); }
  }).finally(() => {
    if (_wake === ctl) _wake = null;
    if (ctl.woken) { isRecording = false; _resetRecordingUI(); }
    _syncUI();
    _resumeWakeSoon(ctl.woken ? 1500 : 3000);
  });
}

/** Preferencias de voz (las carga app.js al iniciar y Ajustes al cambiarlas). */
export function configure(p = {}) {
  const wasWake = _prefs.wake, oldKw = _prefs.keyword;
  Object.assign(_prefs, Object.fromEntries(Object.entries(p).filter(([, v]) => v !== undefined)));
  _prefs.silenceMs = Math.max(600, parseInt(_prefs.silenceMs, 10) || 1500);
  if (!_prefs.wake || (_wake && oldKw !== _prefs.keyword)) _pauseWake();
  if (_prefs.wake && (!wasWake || !_wake)) { _wakeErrorShown = false; _resumeWakeSoon(300); }
}

export function getPrefs() { return { ..._prefs }; }

/** Tras cambiar de micrófono: la palabra de activación vuelve a abrirlo. */
export function restartWake() {
  if (_wake && !_wake.woken && _wake.mode !== 'dictate') {
    _pauseWake();
    _resumeWakeSoon(300);
  }
}

let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;
let recordingStartTime = null;
let recordingInterval = null;
let _sttProvider = 'disabled';

// Browser STT state
let _recognition = null;
let _mediaStream = null; // EDGE FIX: Store stream for browser STT
let _browserTranscript = '';
let _browserInterim = '';
let _silenceTimer = null;

function _clearSilenceTimer() {
  if (_silenceTimer) {
    clearTimeout(_silenceTimer);
    _silenceTimer = null;
  }
}

/**
 * Initialize SpeechRecognition early (before getUserMedia) to warm up mic permissions in Edge
 * This prevents "network error" in Edge when SpeechRecognition is created after getUserMedia
 */
function _initBrowserSTTEarly() {
  if (_recognition) return; // Already initialized
  
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) return;
  
  try {
    _recognition = new SpeechRecognition();
    _recognition.continuous = true;
    _recognition.interimResults = true;
    _recognition.lang = '';
    
    _recognition.onresult = (event) => {
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const transcript = event.results[i][0].transcript;
        if (event.results[i].isFinal) {
          _browserTranscript += transcript + ' ';
        } else {
          _browserInterim = transcript;
        }
      }
    };
    
    _recognition.onerror = (e) => {
      console.warn('Browser STT error (early init):', e.error);
    };
    
    // DO NOT call .start() here - wait for user activation in startBrowserSTT()
    console.log('[VoiceRecorder] STT pre-initialized (will start in startBrowserSTT)');
  } catch (err) {
    console.warn('[VoiceRecorder] Failed to initialize SpeechRecognition:', err);
  }
}


/**
 * Format seconds as MM:SS
 */
function formatTime(seconds) {
  const mins = Math.floor(seconds / 60).toString().padStart(2, '0');
  const secs = (seconds % 60).toString().padStart(2, '0');
  return `${mins}:${secs}`;
}

/**
 * Reset UI state after recording ends
 */
function _resetRecordingUI() {
  isRecording = false;
  if (_vad) { _vad.stop(); _vad = null; }
  if (recordingInterval) {
    clearInterval(recordingInterval);
    recordingInterval = null;
  }
  // RUFFS: el micrófono tiene su propio botón; _updateSendBtnIcon lo
  // refresca y deja en paz el botón principal mientras está en «detener».
  const micBtn = document.getElementById('chat-mic-btn');
  if (micBtn) micBtn.classList.remove('recording');
  if (window._updateSendBtnIcon) {
    setTimeout(window._updateSendBtnIcon, 50);
  }
}

/**
 * Start browser speech recognition alongside recording
 */
function startBrowserSTT() {
  // Si no existe, inicializar ahora (fallback si _initBrowserSTTEarly no se llamo)
  if (!_recognition) {
    _initBrowserSTTEarly();
  }
  
  if (!_recognition) return; // SpeechRecognition no soportado
  
  _browserTranscript = '';
  _browserInterim = '';
  
  // Reutilizar la instancia existente, solo iniciar
  try {
    _recognition.start();
    console.log('[VoiceRecorder] STT started from early initialization');
  } catch (err) {
    console.warn('[VoiceRecorder] Failed to start STT:', err);
  }
}

function stopBrowserSTT() {
  _clearSilenceTimer();
  
  if (!_recognition) {
    return Promise.resolve(_browserTranscript.trim() || _browserInterim.trim());
  }
  
  return new Promise(resolve => {
    let resolved = false;
    
    _recognition.onend = () => {
      if (!resolved) {
        resolved = true;
        // En Edge, esperar un poco más antes de retornar
        const delay = /Edg/.test(navigator.userAgent) ? 300 : 100;
        setTimeout(() => {
          const result = _browserTranscript.trim() || _browserInterim.trim();
          _recognition = null;
          resolve(result);
        }, delay);
      }
    };
    
    // Timeout más largo para Edge
    const timeoutMs = /Edg/.test(navigator.userAgent) ? 2000 : 500;
    const timeoutId = setTimeout(() => {
      if (!resolved) {
        resolved = true;
        const result = _browserTranscript.trim() || _browserInterim.trim();
        if (_recognition) {
          _recognition = null;
        }
        resolve(result);
      }
    }, timeoutMs);
    
    try {
      _recognition.stop();
    } catch (e) {
      if (!resolved) {
        resolved = true;
        clearTimeout(timeoutId);
        const result = _browserTranscript.trim() || _browserInterim.trim();
        _recognition = null;
        resolve(result);
      }
    }
  });
}



async function transcribeOnServer(audioBlob) {
  const formData = new FormData();
  formData.append('file', audioBlob, 'audio.webm');

  const res = await fetch('/api/stt/transcribe', {
    method: 'POST',
    credentials: 'same-origin',
    body: formData,
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail?.message || 'Transcription failed');
  }

  const data = await res.json();
  return data.text || '';
}

/**
 * Insert transcribed text into the chat input
 */
function insertTranscription(text, showToast) {
  if (!text) return;
  const input = document.getElementById('message');
  if (!input) return;

  const existing = input.value.trim();
  input.value = existing ? existing + ' ' + text : text;

  // Trigger auto-resize and icon update
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.focus();

  if (showToast) showToast('Transcribed');
}

/**
 * Start voice recording
 */
export function startRecording(onFileCreated, showToast, showError) {
  _cb = { onFileCreated, showToast, showError };
  if (effectiveProvider() === 'vosk') {
    _startLocalDictation();
    return;
  }
  _pauseWake();
  // Check for secure context (getUserMedia requires HTTPS or localhost)
  if (!window.isSecureContext) {
    if (showError) showError('Microphone requires HTTPS. Use a reverse proxy with SSL or access via localhost.');
    _resetRecordingUI();
    return;
  }

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    if (showError) showError('Microphone not supported in this browser.');
    _resetRecordingUI();
    return;
  }

  audioChunks = [];

  voiceLocal.getMicStream()   // RUFFS: el micrófono elegido en el menú ▾
    .then(stream => {
      _mediaStream = stream; // EDGE FIX: Store stream for browser STT
      mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });

      mediaRecorder.ondataavailable = event => {
        if (event.data.size > 0) {
          audioChunks.push(event.data);
        }
      };

      mediaRecorder.onstop = async () => {
        stream.getTracks().forEach(track => track.stop());

        const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
        const provider = effectiveProvider();

        if (provider === 'browser') {
          const transcript = await stopBrowserSTT();
          if (transcript) {
            _afterTranscript(transcript, showToast);
          } else {
            if (showToast) showToast('No speech detected');
            const audioFile = new File([audioBlob], `voice-message-${Date.now()}.webm`, { type: 'audio/webm' });
            if (onFileCreated) onFileCreated(audioFile);
          }
        } else if (provider === 'local' || provider.startsWith('endpoint:')) {
          // Show "Transcribing..." feedback
          if (showToast) showToast('Transcribing...', 5000);
          try {
            const transcript = await transcribeOnServer(audioBlob);
            if (transcript) {
              _afterTranscript(transcript, showToast);
            } else {
              if (showToast) showToast('No speech detected');
            }
          } catch (e) {
            console.error('STT transcription error:', e);
            if (showError) showError('Transcription failed: ' + e.message);
            // Fallback: attach as file
            const audioFile = new File([audioBlob], `voice-message-${Date.now()}.webm`, { type: 'audio/webm' });
            if (onFileCreated) onFileCreated(audioFile);
          }
        } else {
          // STT disabled — attach audio file
          const audioFile = new File([audioBlob], `voice-message-${Date.now()}.webm`, { type: 'audio/webm' });
          if (onFileCreated) onFileCreated(audioFile);
        }

        _resetRecordingUI();
        _resumeWakeSoon();
      };

      mediaRecorder.start();
      if (_prefs.autoSend && _sttProvider !== 'disabled') _vad = _watchSilence(stream);
      isRecording = true;
      recordingStartTime = new Date();

      // Start browser STT if that's the provider
      if (effectiveProvider() === 'browser') {
        startBrowserSTT();
      }

      if (showToast) {
        showToast('Recording...');
      }
    })
    .catch(error => {
      console.error('Microphone access error:', error);
      if (showError) {
        if (error.name === 'NotAllowedError') {
          showError('Microphone access denied. Check browser permissions.');
        } else if (error.name === 'NotFoundError') {
          showError('No microphone found.');
        } else {
          showError('Microphone error: ' + error.message);
        }
      }
      _resetRecordingUI();
    });
}

/**
 * Stop voice recording
 */
export function stopRecording() {
  if (_local) { _local.stop(); return; }
  if (_wake && !_wake.browser && _wake.mode === 'dictate') { _wake.stop(); return; }
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
    // isRecording will be set to false in _resetRecordingUI called from onstop
  } else {
    _resetRecordingUI();
  }
}

/**
 * Check if currently recording
 */
export function getIsRecording() {
  return isRecording;
}

/**
 * Refresca el proveedor de STT (Speech-to-Text) leyendo la configuración
 */
function refreshSttProvider() {
  try {
    // Intenta leer la configuración guardada (por defecto 'disabled')
    // Nota: Si RUFFS usa otra clave diferente a 'sttProvider', puedes cambiarla aquí.
    const savedProvider = localStorage.getItem('sttProvider');
    _sttProvider = savedProvider ? savedProvider : 'disabled';
  } catch (e) {
    _sttProvider = 'disabled';
  }
}

/**
 * Initialize recording state
 */
export function init() {
  isRecording = false;
  _initBrowserSTTEarly(); // Warm up mic permissions in Edge before getUserMedia
  refreshSttProvider();
}

const voiceRecorderModule = {
  startRecording,
  stopRecording,
  getIsRecording,
  init,
  refreshSttProvider,
  configure,
  getPrefs,
  getWakeState,
  effectiveProvider,
  restartWake,
  get _sttProvider() { return _sttProvider; },
  set _sttProvider(v) { _sttProvider = v; },
};

window.ruffsVoice = voiceRecorderModule;
export default voiceRecorderModule;
