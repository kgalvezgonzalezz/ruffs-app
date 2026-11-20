"""RUFFS: la lectura automática lee solo la respuesta final y, si trae tablas,
código o es larga, un resumen hablado que no se muestra ni se guarda."""

import asyncio
from pathlib import Path

from src import spoken_summary as ss

ROOT = Path(__file__).resolve().parents[1]
TABLA = ("Aquí tienes las ventas del trimestre:\n\n| Mes | Ventas |\n|---|---|\n| Enero | 1200 |\n"
         "| Febrero | 980 |\n\nMarzo fue el mejor mes. Más en https://ejemplo.com/reporte")


def test_needs_summary():
    assert ss.needs_summary(TABLA)
    assert ss.needs_summary("Usa esto:\n```python\nprint(1)\n```")
    assert ss.needs_summary("Pasos:\n1. uno\n2. dos\n3. tres\n4. cuatro")
    assert ss.needs_summary("Frase larga de prueba. " * 40)
    assert not ss.needs_summary("Mañana estará soleado, con 27 grados.")
    assert not ss.needs_summary("<think>" + "razono mucho " * 200 + "</think>Hola, ¿en qué te ayudo?")


def test_local_spoken_is_clean():
    out = ss.local_spoken(TABLA)
    assert "|" not in out and "http" not in out and "  " not in out
    assert out.endswith("Te dejé el detalle en pantalla.")
    assert "Puedes ver más en" not in out
    assert ss.local_spoken("Mañana estará soleado.") == "Mañana estará soleado."


def test_spoken_version_modes(monkeypatch):
    calls = []

    async def fake_llm(cands, messages, **kw):
        calls.append(messages)
        return "<think>pienso</think>«Las ventas subieron en marzo. El detalle está en pantalla.»"

    import src.llm_core as llm
    monkeypatch.setattr(llm, "llm_call_async_with_fallback", fake_llm)
    monkeypatch.setattr(ss, "_candidates", lambda owner: [("http://x", "m", {})])
    r = asyncio.run(ss.spoken_version(TABLA, "smart", owner="k"))
    assert r == {"speech": "Las ventas subieron en marzo. El detalle está en pantalla.", "summarized": True, "source": "llm"}
    assert calls[0][1]["content"].startswith("Aquí tienes las ventas")
    # corta → tal cual, sin llamar al modelo
    r = asyncio.run(ss.spoken_version("Hola, ¿qué tal?", "smart", owner="k"))
    assert r["source"] == "original" and len(calls) == 1
    # siempre resumen
    assert asyncio.run(ss.spoken_version("Hola, ¿qué tal?", "summary", owner="k"))["source"] == "llm"
    # completa: sin modelo, sin tablas ni enlaces
    r = asyncio.run(ss.spoken_version(TABLA, "full", owner="k"))
    assert r["source"] == "original" and "|" not in r["speech"] and "http" not in r["speech"]

    # el modelo falla → resumen local
    async def boom(*a, **k):
        raise RuntimeError("401")
    monkeypatch.setattr(llm, "llm_call_async_with_fallback", boom)
    r = asyncio.run(ss.spoken_version(TABLA, "smart", owner="k"))
    assert r["source"] == "local" and r["speech"].endswith("Te dejé el detalle en pantalla.")


def test_wiring():
    chat = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    assert "const streamingTTS = false;" in chat                       # no habla mientras trabaja
    assert "window.aiTTSManager.speakFinal(_finalText, ttsBtn, resetFn);" in chat
    assert "footerTarget.querySelector('.ai-tts-button')" in chat       # modo agente: última burbuja
    tts = (ROOT / "static/js/tts-ai.js").read_text(encoding="utf-8")
    assert "async speakFinal(text, button, resetFn)" in tts and "/api/tts/spoken" in tts
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    assert 'id="set-ttsAutoPlayMode"' in html
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["tts_auto_play_mode"] == "smart"
    src = (ROOT / "routes/tts_routes.py").read_text(encoding="utf-8")
    assert '@router.post("/spoken")' in src
