"""RUFFS: rondas vacías del agente — el motivo real de parada llega al agente
y, tras una ronda vacía, se reintenta sin «pensamiento» (OpenRouter)."""

import asyncio
import json
from pathlib import Path

from src.autocontinue import stream_with_autocontinue

ROOT = Path(__file__).resolve().parents[1]


def _collect(gen):
    async def run():
        return [c async for c in gen]
    return asyncio.run(run())


def test_autocontinue_forwards_real_finish_reason():
    async def fake_stream(url, model, messages, headers=None, **kw):
        yield 'data: {"delta": "pensando", "thinking": true}\n\n'
        yield 'data: {"type": "tool_calls", "calls": [{"id": "1", "name": "web_search", "arguments": "{}"}]}\n\n'
        yield 'data: {"type": "finish", "reason": "tool_calls"}\n\n'
        yield "data: [DONE]\n\n"

    out = _collect(stream_with_autocontinue(fake_stream, "u", "m", [], emit_finish=True))
    finishes = [json.loads(c[6:]) for c in out if '"type": "finish"' in c]
    assert finishes == [{"type": "finish", "reason": "tool_calls"}]
    assert out[-1] == "data: [DONE]\n\n"
    # sin emit_finish (otros usos) sigue sin reenviarlo
    out2 = _collect(stream_with_autocontinue(fake_stream, "u", "m", []))
    assert not any('"type": "finish"' in c for c in out2)


def test_stream_llm_sends_reasoning_only_to_openrouter_and_agent_retries_without_it():
    llm = (ROOT / "src/llm_core.py").read_text(encoding="utf-8")
    assert "tools: Optional[List[Dict]] = None, reasoning: Optional[Dict] = None):" in llm
    assert 'if reasoning:\n                payload["reasoning"] = reasoning' in llm.replace("\r\n", "\n")
    agent = (ROOT / "src/agent_loop.py").read_text(encoding="utf-8")
    assert '**({"reasoning": {"enabled": False}} if _empty_rounds >= 1 else {}),' in agent
    assert "_empty_rounds = 0  # RUFFS: volvió a actuar" in agent
    assert "next attempt without reasoning. Thought:" in agent


def test_announce_without_acting_is_not_a_final_answer():
    """«Voy a buscar en GitHub…» sin llamar herramientas cerraba el turno."""
    import src.agent_tools  # noqa: F401
    from src.agent_loop import _looks_like_announcement as f
    assert f("Voy a buscar en GitHub varios repos relacionados.")
    assert f("Déjame revisar eso un momento.") and f("Let me search GitHub for that.")
    assert not f("Encontré 3 repos:\n- a\n- b")
    assert not f("¿Quieres que busque también en GitLab?")
    assert not f("Hecho: volví al micrófono predeterminado.")
    assert not f("Listo. Marzo fue el mejor mes; si quieres, después haré un gráfico con los datos del trimestre.")
    agent = (ROOT / "src/agent_loop.py").read_text(encoding="utf-8")
    assert "_announce_nudges < _ANNOUNCE_MAX" in agent and "Do not announce again." in agent
