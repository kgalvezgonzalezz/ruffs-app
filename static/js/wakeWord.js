/**
 * wakeWord.js - JARVIS-style wake-word detection using Web Speech API.
 * Listens continuously in the background for a configurable keyword
 * (default: 'RUFFS'). When detected, activates the voice recorder
 * and sends the transcribed command to the chat.
 *
 * Zero cost, zero dependencies - uses browser SpeechRecognition API.
 * Works on Chrome, Edge, Brave, Opera. NOT on Firefox.
 */

class WakeWordDetector {
  constructor() {
    this.keyword = 'ruffs';
    this.enabled = false;
    this.listening = false;
    this.recognition = null;
    this.onActivate = null;
    this.onStatusChange = null;
    this._restartTimer = null;
    this._lang = 'es-CL';
  }

  static isSupported() {
    return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  }


  start() {
    if (!WakeWordDetector.isSupported()) {
      console.warn('[WakeWord] SpeechRecognition not supported');
      return false;
    }
    if (this.listening) return true;
    
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    this.recognition = new SpeechRecognition();
    this.recognition.continuous = true;
    this.recognition.interimResults = true;
    this.recognition.lang = this._lang;
    this.recognition.onresult = (event) => this._onResult(event);
    this.recognition.onerror = (event) => this._onError(event);
    this.recognition.onend = () => this._onEnd();
    
    try {
      // EDGE FIX: Asegurar que haya sido iniciado con user activation
      // En Edge, SpeechRecognition.start() fallapor 'not-allowed' si no hay activacion previa
      this.recognition.start();
      this.listening = true;
      this._setStatus('listening');
      console.log('[WakeWord] Listening for: "' + this.keyword + '"');
      return true;
    } catch (e) {
      console.error('[WakeWord] Failed to start:', e);
      // En Edge, si falla, hay que retry después de que el usuario active el micrófono
      this._scheduleRestart();
      return false;
    }
  }


  stop() {
    this.listening = false;
    if (this._restartTimer) { clearTimeout(this._restartTimer); this._restartTimer = null; }
    if (this.recognition) {
      try { this.recognition.stop(); } catch (e) {}
      this.recognition = null;
    }
    this._setStatus('idle');
  }

  setKeyword(keyword) {
    this.keyword = keyword.toLowerCase().trim();
    console.log('[WakeWord] Keyword: "' + this.keyword + '"');
  }

  setLanguage(lang) { this._lang = lang; }

  _onResult(event) {
    let finalText = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const r = event.results[i];
      if (r.isFinal) finalText += r[0].transcript;
    }
    const text = finalText.toLowerCase().trim();
    if (!text) return;
    // RUFFS: el reconocedor suele escribir «RUFFS» como rafs/rufus/ruff…
    const variants = [this.keyword].concat(this.keyword === 'ruffs'
      ? ['rufs', 'rafs', 'raf', 'rufus', 'ruff', 'rough', 'roughs', 'rugs'] : []);
    // Palabra a palabra (sin signos): la clave puede ser de una o varias palabras.
    const clean = (s) => s.toLowerCase().replace(/[^a-z0-9áéíóúüñ\s]/g, ' ').split(/\s+/).filter(Boolean);
    const words = clean(finalText);
    let after = -1;
    for (const v of variants) {
      const kw = clean(v);
      for (let i = 0; kw.length && i + kw.length <= words.length; i++) {
        if (kw.every((w, j) => words[i + j] === w)) { if (after < 0 || i + kw.length < after) after = i + kw.length; break; }
      }
    }
    if (after < 0) return;
    console.log('[WakeWord] Detected! Text:', finalText);
    const command = words.slice(after).join(' ');
    this.stop();
    this._setStatus('active');
    // Solo lo dicho DESPUÉS de la palabra clave ('' = empezar a escuchar).
    if (this.onActivate) this.onActivate(command);
  }

  _onError(event) {
    console.log('[WakeWord] Error:', event.error);
    if (event.error === 'not-allowed') {
      console.error('[WakeWord] Mic denied. Edge may require checking browser permissions.');
      // No hacer stop aquí, simplemente reintentar después de delay
      this._scheduleRestart();
      return;
    }
    if (event.error === 'network') {
      console.warn('[WakeWord] Network error, retrying...');
      this._scheduleRestart();
      return;
    }
    if (event.error === 'no-speech') {
      console.log('[WakeWord] No speech detected, continuing...');
      return;
    }
    // Para otros errores, también reintentar
    this._scheduleRestart();
  }

  _onEnd() {
    this.listening = false;
    if (this.enabled && this.recognition) this._scheduleRestart();
  }

  _scheduleRestart() {
    if (this._restartTimer) return;
    this._restartTimer = setTimeout(() => {
      this._restartTimer = null;
      if (this.enabled && !this.listening) this.start();
    }, 500);
  }

  _setStatus(s) { this._status = s; if (this.onStatusChange) this.onStatusChange(s); }
}

// Singleton global
window.wakeWordDetector = new WakeWordDetector();
console.log('[WakeWord] Initialized. Supported:', WakeWordDetector.isSupported());
