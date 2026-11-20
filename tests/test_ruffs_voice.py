"""RUFFS 1.1.13: voz local (Vosk), envío al dejar de hablar, palabra de activación,
«Export as same format», Tidy que recuerda las carpetas y «Guardar como»."""

import io
import os
import time
import zipfile
from pathlib import Path

import pytest

from src import voice_local as vl

ROOT = Path(__file__).resolve().parents[1]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# ── Palabra clave ────────────────────────────────────────────────────────────

def test_phonetic_and_keyword_grammar():
    assert vl.phonetic("RUFFS") == "rufs" and vl.phonetic("Rafs") == "rafs"
    assert vl.phonetic("Vosk") == vl.phonetic("bosk")
    g = vl.keyword_grammar("RUFFS")
    assert g[-1] == "[unk]" and "raf" in g
    # alias de dos sílabas que daban falsos positivos con el audio del micrófono
    assert "rafa" not in g and "rufo" not in g
    assert vl.keyword_grammar("Hola Ruffs")[-1] == "[unk]"


def test_fuzzy_keyword_end_uses_word_times():
    words = [{"word": w, "start": i, "end": i + 0.5} for i, w in enumerate("hoy compré pan rufs qué hora es".split())]
    assert vl.fuzzy_keyword_end(words, "RUFFS") == 3.5
    words2 = [{"word": w, "start": i, "end": i + 0.5} for i, w in enumerate("la estructura es rara".split())]
    assert vl.fuzzy_keyword_end(words2, "RUFFS") is None


def test_model_lang_and_status_without_model(tmp_path, monkeypatch):
    import src.constants as c
    monkeypatch.setattr(c, "DATA_DIR", str(tmp_path))
    assert vl.model_lang("es-CL") == "es" and vl.model_lang("") == "es" and vl.model_lang("en") == "en"
    assert vl.model_lang("xx") == "es"
    st = vl.status("es")
    assert st["ready"] is False and st["model"] == vl.MODELS["es"][0]
    if vl.engine_available():
        with pytest.raises(vl.VoiceError, match="model_missing"):
            vl.open_session("k", "dictate", "es")
    with pytest.raises(vl.VoiceError):
        vl.feed("no-existe", "k", b"\0\0")


def test_model_download_extracts_and_blocks_zip_slip(tmp_path, monkeypatch):
    import src.constants as c
    import httpx
    monkeypatch.setattr(c, "DATA_DIR", str(tmp_path))
    name = vl.MODELS["es"][0]

    def make_zip(members):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for m, data in members.items():
                z.writestr(m, data)
        return buf.getvalue()

    payload = {"data": make_zip({f"{name}/conf/model.conf": "--min-active=200", f"{name}/am/final.mdl": "x"})}

    class FakeStream:
        def __init__(self, *a, **k):
            self.headers = {"content-length": str(len(payload["data"]))}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self, n):
            d = payload["data"]
            for i in range(0, len(d), 1000):
                yield d[i:i + 1000]

    monkeypatch.setattr(httpx, "stream", FakeStream)
    vl._downloads.clear()
    vl.start_download("es")
    for _ in range(100):
        if vl.model_ready("es"):
            break
        time.sleep(0.05)
    assert vl.model_ready("es") and vl.status("es")["download"]["status"] == "ready"
    assert not any(n.startswith(".") for n in os.listdir(os.path.join(str(tmp_path), "vosk")))

    # Un zip con rutas «../» no se extrae
    import shutil
    shutil.rmtree(os.path.join(str(tmp_path), "vosk"))
    payload["data"] = make_zip({"../fuera.txt": "x", f"{name}/conf/model.conf": "x"})
    vl._downloads.clear()
    vl.start_download("es")
    for _ in range(100):
        if (vl._downloads.get("es") or {}).get("status") == "error":
            break
        time.sleep(0.05)
    assert vl._downloads["es"]["status"] == "error" and not vl.model_ready("es")
    assert not (tmp_path / "fuera.txt").exists()


@pytest.mark.skipif(not os.environ.get("RUFFS_VOSK_TEST_MODEL"), reason="sin modelo de voz local para la prueba real")
def test_real_model_wake_and_dictation(monkeypatch):
    """Con un modelo y audios reales (RUFFS_VOSK_TEST_MODEL=<carpeta con vosk/ y wav/>)."""
    import wave
    import src.constants as c
    base = os.environ["RUFFS_VOSK_TEST_MODEL"]
    monkeypatch.setattr(c, "DATA_DIR", base)

    def run(wav, mode):
        w = wave.open(os.path.join(base, "wav", wav)); pcm = w.readframes(w.getnframes())
        sid = vl.open_session("k", mode, "es", "RUFFS"); woke = False
        for i in range(0, len(pcm), 8000):
            r = vl.feed(sid, "k", pcm[i:i + 8000])   # siempre: el audio sigue tras despertar
            woke = woke or bool(r.get("wake"))
        return woke, vl.finish(sid, "k")

    assert run("wake1.wav", "wake") == (True, "qué tiempo hace mañana en santiago")
    assert run("nada.wav", "wake") == (False, "")
    assert run("dictado.wav", "dictate")[1].startswith("hola esto es una prueba")


# ── Integración ──────────────────────────────────────────────────────────────

def test_routes_settings_and_requirements():
    from routes.voice_routes import setup_voice_routes
    paths = {r.path for r in setup_voice_routes().routes}
    assert {"/api/voice/status", "/api/voice/model", "/api/voice/session",
            "/api/voice/session/{sid}/audio", "/api/voice/session/{sid}/end"} <= paths
    assert "setup_voice_routes()" in _read("app.py")
    assert "vosk>=0.3.44" in _read("requirements.txt")
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["stt_auto_send"] is True and DEFAULT_SETTINGS["stt_silence_ms"] == 1500


def test_frontend_voice_wiring():
    rec = _read("static/js/voiceRecorder.js")
    # dentro de la ventana de RUFFS «browser» usa la voz local
    assert "if (_sttProvider === 'browser' && !_browserSttWorks()) return 'vosk';" in rec
    assert "window.chrome && window.chrome.webview" in rec
    # el envío automático nunca detiene una respuesta en curso
    assert "btn.dataset.mode === 'streaming'" in rec and "window._ruffsSubmitChat" in rec
    app = _read("static/app.js")
    assert "window._ruffsSubmitChat = () => handleSubmit(" in app
    assert "voiceRecorderModule.configure({" in app and "wake: s.tts_wake_word_enabled === true" in app
    # la palabra de activación ya no depende de abrir Ajustes ni usa «odysseus»
    assert "this.keyword = 'ruffs';" in _read("static/js/wakeWord.js")
    st = _read("static/js/settings.js")
    assert "window.wakeWordDetector.onActivate" not in st and "set-sttAutoSendToggle" in st
    html = _read("static/index.html")
    assert '<option value="vosk">RUFFS local (sin internet)</option>' in html and 'id="set-sttAutoSendToggle"' in html


# ── Export as same format, Tidy y Guardar como ───────────────────────────────

def test_source_format_and_export_menu():
    from routes.document_helpers import clean_source_format
    assert clean_source_format(".PY") == ".py" and clean_source_format("xlsx") == ".xlsx"
    assert clean_source_format("../x") is None and clean_source_format("") is None
    from core.database import Document
    assert hasattr(Document, "source_format")
    assert '"source_format":           getattr(doc, "source_format", None)' in _read("routes/document_helpers.py")
    doc_js = _read("static/js/document.js")
    assert "label: 'Export as same format', fn: exportSameFormat" in doc_js
    assert "async function exportSameFormat()" in doc_js and "XLSX.write(wb, { bookType" in doc_js
    assert "source_format: ext" in _read("static/js/documentLibrary.js")
    assert 'source_format=".docx"' in _read("src/docx_doc.py")


def test_tidy_keeps_folder_view_and_save_as_enabled():
    app = _read("static/app.js")
    assert "sessionModule.setSortMode('group');" in app
    assert "sessionModule.setSortMode(null); // clear sort — tidy creates manual folder order" not in app
    desk = _read("ruffs_desktop.py")
    assert "webview.settings['ALLOW_DOWNLOADS'] = True" in desk
