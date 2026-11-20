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
