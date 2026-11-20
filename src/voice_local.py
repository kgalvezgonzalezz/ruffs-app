"""RUFFS: voz local sin internet (Vosk) — dictado y palabra de activación.

Por qué: dentro de la ventana de RUFFS (WebView2 en Windows, WKWebView en Mac)
el reconocimiento de voz del navegador (Web Speech API) arranca, oye el audio y
a los ~0,5 s falla con «network»: el servicio en la nube de Edge no existe para
ventanas integradas. Así el dictado y la palabra de activación nunca podían
funcionar ahí. Vosk reconoce en el propio equipo (modelos pequeños de ~40 MB,
se descargan la primera vez) y sirve para las dos cosas.

Flujo: el navegador manda audio PCM 16 kHz mono en trozos de ~0,25 s
(POST /api/voice/session/{id}/audio). Cada sesión lleva:
* un reconocedor libre (el texto del dictado, con tiempos por palabra);
* en modo «wake», además un reconocedor con gramática limitada a palabras
  que suenan como la palabra clave: «RUFFS» no es una palabra del idioma y el
  reconocedor libre la omite o la cambia; con gramática sale «raf» con
  confianza 1,0 (y lo demás como [unk]). Al detectarla, la sesión pasa a
  dictado sin cortar el audio, y del texto se queda solo lo dicho DESPUÉS de
  la palabra clave (por los tiempos de cada palabra).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
import zipfile
from typing import Optional

logger = logging.getLogger(__name__)

MODELS = {
    "es": ("vosk-model-small-es-0.42", 39817833),
    "en": ("vosk-model-small-en-us-0.15", 41205931),
    "pt": ("vosk-model-small-pt-0.3", 32453112),
}
BASE_URL = "https://alphacephei.com/vosk/models/"
SAMPLE_RATE = 16000
KEYWORD_CONF = 0.75
SESSION_IDLE_SECONDS = 90
MAX_SESSION_SECONDS = 10 * 60

# Palabras del vocabulario de cada modelo que suenan como la palabra clave
# (las que no estén en el vocabulario, Vosk las ignora sin fallar).
KEYWORD_ALIASES = {
    # Sin «rafa»/«rufo»: con el audio procesado del micrófono salían sobre
    # palabras normales («comprar pan» → «rafa», confianza 0,98).
    "ruffs": ["raf", "rufus", "ruff", "rough", "roughs", "ruffs"],
    "odysseus": ["odiseo", "ulises", "odysseus"],
}

_lock = threading.Lock()
_models: dict = {}
_downloads: dict = {}
_sessions: dict = {}


class VoiceError(Exception):
    pass


# ─── Motor y modelos ────────────────────────────────────────────────────────

def engine_available() -> bool:
    try:
        import vosk  # noqa: F401
        return True
    except Exception:
        return False


def model_lang(stt_language: str = "") -> str:
    code = (stt_language or "").strip().lower()[:2]
    return code if code in MODELS else "es"


def _base_dir() -> str:
    from src.constants import DATA_DIR
    d = os.path.join(DATA_DIR, "vosk")
    os.makedirs(d, exist_ok=True)
    return d


def model_dir(lang: str) -> str:
    return os.path.join(_base_dir(), MODELS[model_lang(lang)][0])


def model_ready(lang: str) -> bool:
    return os.path.isfile(os.path.join(model_dir(lang), "conf", "model.conf"))


def status(lang: str = "") -> dict:
    lang = model_lang(lang)
    st = dict(_downloads.get(lang) or {})
    ready = model_ready(lang)
    return {
        "engine": engine_available(),
        "lang": lang,
        "model": MODELS[lang][0],
        "size_mb": round(MODELS[lang][1] / 1e6),
        "ready": ready,
        "download": st if not ready else {"status": "ready", "progress": 1.0},
    }


def start_download(lang: str = "") -> dict:
    """Descarga el modelo en segundo plano (una sola vez)."""
    lang = model_lang(lang)
    if model_ready(lang):
        return status(lang)
    with _lock:
        cur = _downloads.get(lang) or {}
        if cur.get("status") == "downloading":
            return status(lang)
        _downloads[lang] = {"status": "downloading", "progress": 0.0, "error": ""}
    threading.Thread(target=_download, args=(lang,), daemon=True, name=f"vosk-dl-{lang}").start()
    return status(lang)


def _download(lang: str) -> None:
    name, size = MODELS[lang]
    base = _base_dir()
    tmp_zip = os.path.join(base, f".{name}.zip.part")
    tmp_dir = os.path.join(base, f".{name}.tmp")
    try:
        import httpx
        got = 0
        with httpx.stream("GET", BASE_URL + name + ".zip", timeout=60.0, follow_redirects=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or size)
            with open(tmp_zip, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
                    got += len(chunk)
                    _downloads[lang]["progress"] = min(0.99, got / max(total, 1))
        shutil.rmtree(tmp_dir, ignore_errors=True)
        with zipfile.ZipFile(tmp_zip) as z:
            for m in z.namelist():
                target = os.path.realpath(os.path.join(tmp_dir, m))
                if not target.startswith(os.path.realpath(tmp_dir) + os.sep):
                    raise VoiceError("zip con rutas no permitidas")
            z.extractall(tmp_dir)
        inner = os.path.join(tmp_dir, name)
        src = inner if os.path.isdir(inner) else tmp_dir
        final = os.path.join(base, name)
        shutil.rmtree(final, ignore_errors=True)
        shutil.move(src, final)
        _downloads[lang] = {"status": "ready", "progress": 1.0, "error": ""}
        logger.info("Modelo de voz %s listo en %s", name, final)
    except Exception as e:  # noqa: BLE001
        logger.warning("Descarga del modelo de voz %s falló: %s", name, e)
        _downloads[lang] = {"status": "error", "progress": 0.0,
                            "error": f"No se pudo descargar el modelo de voz: {str(e)[:200]}"}
    finally:
        for p in (tmp_zip,):
            try:
                os.remove(p)
            except OSError:
                pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _get_model(lang: str):
    lang = model_lang(lang)
    with _lock:
        m = _models.get(lang)
        if m is None:
            if not model_ready(lang):
                raise VoiceError("model_missing")
            import vosk
            vosk.SetLogLevel(-1)
            m = vosk.Model(model_dir(lang))
            _models[lang] = m
        return m


# ─── Palabra clave ───────────────────────────────────────────────────────────

def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def phonetic(word: str) -> str:
    """Forma «como suena» para comparar con tolerancia (ruffs≈rufs≈rufz)."""
    w = re.sub(r"[^a-zñ]", "", _fold(word))
    for a, b in (("ph", "f"), ("qu", "k"), ("ll", "y"), ("gh", "g"), ("ce", "se"), ("ci", "si"),
                 ("v", "b"), ("z", "s"), ("c", "k"), ("w", "u"), ("h", "")):
        w = w.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", w)


def _lev(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def keyword_grammar(keyword: str) -> list[str]:
    kw = _fold(keyword).strip()
    words = [w for w in re.split(r"\s+", kw) if w]
    out = list(KEYWORD_ALIASES.get(kw.replace(" ", ""), []))
    out += words
    seen, res = set(), []
    for w in out:
        if w and w not in seen:
            seen.add(w)
            res.append(w)
    return res + ["[unk]"]


def fuzzy_keyword_end(words: list[dict], keyword: str) -> Optional[float]:
    """En el texto libre (con tiempos), dónde termina la palabra clave."""
    key = phonetic(keyword.replace(" ", ""))
    if len(key) < 3:
        return None
    tol = 1 if len(key) <= 6 else 2
    for i, w in enumerate(words):
        for n in (1, 2):
            if i + n > len(words):
                break
            cand = phonetic("".join(x["word"] for x in words[i:i + n]))
            if cand and cand[0] == key[0] and _lev(cand, key) <= tol:
                return words[i + n - 1]["end"]
    return None


# ─── Sesiones de audio ───────────────────────────────────────────────────────

class _Session:
    def __init__(self, owner, mode, lang, keyword):
        import vosk
        model = _get_model(lang)
        vosk.SetLogLevel(-1)   # sin avisos por palabras de la gramática fuera del vocabulario
        self.id = uuid.uuid4().hex
        self.owner = owner or ""
        self.mode = mode
        self.keyword = keyword or "RUFFS"
        self.free = vosk.KaldiRecognizer(model, SAMPLE_RATE)
        self.free.SetWords(True)
        self.grammar = None
        if mode == "wake":
            self.grammar = vosk.KaldiRecognizer(model, SAMPLE_RATE, json.dumps(keyword_grammar(self.keyword)))
            self.grammar.SetWords(True)
            self.grammar.SetPartialWords(True)
        self.words: list[dict] = []        # palabras finales del reconocedor libre
        self.partial: list[dict] = []
        self.kw_end: Optional[float] = None
        self.created = self.last = time.time()
        self.lock = threading.Lock()

    def text(self) -> str:
        ws = self.words + self.partial
        if self.kw_end is not None:
            ws = [w for w in ws if w.get("start", 0) >= self.kw_end - 0.05]
            # Los parciales aún no tienen tiempos: quitar la palabra clave
            # («raf que…») del principio mientras el segmento no se cierra.
            aliases = set(keyword_grammar(self.keyword)) - {"[unk]"}
            key = phonetic(self.keyword.replace(" ", ""))
            while ws and ws[0].get("start", 0) >= 1e8 and (
                    ws[0]["word"] in aliases or (len(key) >= 3 and _lev(phonetic(ws[0]["word"]), key) <= 2)):
                ws = ws[1:]
        return " ".join(w["word"] for w in ws if w.get("word") and w["word"] != "[unk]").strip()

    def _check_keyword(self, chunk_result: list[dict]) -> bool:
        # Solo resultados con confianza real: los parciales de Vosk no la traen
        # y aceptarlos daba falsos positivos («…pan y leche» → «rafa»).
        for w in chunk_result:
            if w.get("word") != "[unk]" and "conf" in w and w["conf"] >= KEYWORD_CONF:
                self.kw_end = w["end"]
                return True
        end = fuzzy_keyword_end(self.words, self.keyword)   # solo palabras con tiempos reales
        if end is not None:
            self.kw_end = end
            return True
        return False

    def feed(self, pcm: bytes) -> dict:
        with self.lock:
            self.last = time.time()
            if self.free.AcceptWaveform(pcm):
                self.words += json.loads(self.free.Result()).get("result", [])
                self.partial = []
            else:
                self.partial = _partial_words(self.free)
            out = {"mode": self.mode}
            if self.mode == "wake":
                if self.grammar.AcceptWaveform(pcm):
                    g = json.loads(self.grammar.Result()).get("result", [])
                else:
                    g = json.loads(self.grammar.PartialResult()).get("partial_result", [])
                if self._check_keyword(g):
                    self.mode = "dictate"      # sigue la misma grabación como dictado
                    out.update(wake=True, mode="dictate")
                else:
                    # En espera solo importa lo reciente: no acumular horas de texto.
                    if len(self.words) > 200:
                        self.words = self.words[-50:]
                    out["heard"] = " ".join(w["word"] for w in (self.words + self.partial)[-8:])
                    return out
            out["text"] = self.text()
            return out

    def finish(self) -> str:
        with self.lock:
            self.words += json.loads(self.free.FinalResult()).get("result", [])
            self.partial = []
            return self.text() if self.mode == "dictate" else ""


def _partial_words(rec) -> list[dict]:
    """Palabras del resultado parcial (sin tiempos fiables: se usa el fin de la
    última palabra final como inicio aproximado)."""
    txt = json.loads(rec.PartialResult()).get("partial", "")
    return [{"word": w, "start": 1e9, "end": 1e9} for w in txt.split()]


def _cleanup() -> None:
    now = time.time()
    for sid, s in list(_sessions.items()):
        if now - s.last > SESSION_IDLE_SECONDS or now - s.created > MAX_SESSION_SECONDS:
            _sessions.pop(sid, None)


def open_session(owner: str, mode: str, lang: str = "", keyword: str = "RUFFS") -> str:
    if mode not in ("dictate", "wake"):
        raise VoiceError("modo no válido")
    if not engine_available():
        raise VoiceError("engine_missing")
    _cleanup()
    s = _Session(owner, mode, lang, keyword)
    _sessions[s.id] = s
    return s.id


def _get(sid: str, owner: str) -> _Session:
    s = _sessions.get(sid)
    if not s or s.owner != (owner or ""):
        raise VoiceError("session_not_found")
    return s


def feed(sid: str, owner: str, pcm: bytes) -> dict:
    if len(pcm) % 2:
        pcm = pcm[:-1]
    return _get(sid, owner).feed(pcm)


def finish(sid: str, owner: str) -> str:
    s = _get(sid, owner)
    try:
        return s.finish()
    finally:
        _sessions.pop(sid, None)


def close(sid: str, owner: str) -> None:
    s = _sessions.get(sid)
    if s and s.owner == (owner or ""):
        _sessions.pop(sid, None)
