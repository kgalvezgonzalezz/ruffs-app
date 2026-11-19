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

  navigator.mediaDevices.getUserMedia({ audio: true })
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
        const provider = _sttProvider;

        if (provider === 'browser') {
          const transcript = await stopBrowserSTT();
          if (transcript) {
            insertTranscription(transcript, showToast);
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
              insertTranscription(transcript, showToast);
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
      };

      mediaRecorder.start();
      isRecording = true;
      recordingStartTime = new Date();

      // Start browser STT if that's the provider
      if (_sttProvider === 'browser') {
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
  get _sttProvider() { return _sttProvider; },
  set _sttProvider(v) { _sttProvider = v; },
};

export default voiceRecorderModule;
