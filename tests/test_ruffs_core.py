"""Tests de los cambios de núcleo de RUFFS respecto a Odysseus.

Cubren:
- detección del corte por límite de tokens en stream_llm (evento ``finish``);
- continuación automática y empalme sin texto repetido;
- continuación en llamadas sin streaming (llm_call_async);
- opciones específicas de OpenRouter (coste, max_tokens del catálogo, caché);
- memoria de herramientas entre turnos.
"""
import asyncio
import json

import pytest

from src import autocontinue, llm_core, tool_memory
from src.autocontinue import (
    CONTINUE_PROMPT, normalize_finish_reason, stream_with_autocontinue, trim_overlap,
)


# ── utilidades ───────────────────────────────────────────────────────────

def _sse(obj) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _collect(agen):
    async def run():
        return [c async for c in agen]
    return asyncio.run(run())


def _text(chunks):
    out = ""
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            d = json.loads(c[6:])
            if "delta" in d and not d.get("thinking"):
                out += d["delta"]
    return out


class _FakeStream:
    """Simula stream_llm: cada llamada devuelve el siguiente guion."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls = []

    def __call__(self, url, model, messages, headers=None, **kw):
        self.calls.append({"messages": messages, "kwargs": kw})
        script = self.scripts.pop(0)

        async def gen():
            for item in script:
                yield item
        return gen()


# ── normalización y solapamiento ─────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("length", "length"), ("max_tokens", "length"), ("MAX_TOKENS", "length"),
    ("stop", "stop"), ("end_turn", "stop"), ("tool_calls", "tool_calls"),
    ("tool_use", "tool_calls"), (None, None), ("", None),
])
def test_normalize_finish_reason(raw, expected):
    assert normalize_finish_reason(raw) == expected


def test_trim_overlap_removes_repeated_tail():
    prev = "La función calcula el total de la factura y luego"
    cont = "total de la factura y luego aplica el impuesto."
    assert trim_overlap(prev, cont) == " aplica el impuesto." or \
        trim_overlap(prev, cont) == "aplica el impuesto."


def test_trim_overlap_keeps_text_without_overlap():
    assert trim_overlap("Primera parte.", " Segunda parte.") == " Segunda parte."


def test_trim_overlap_ignores_tiny_coincidences():
    # Un solapamiento de pocos caracteres puede ser casual: no se recorta.
    assert trim_overlap("hola a", "a todos") == "a todos"


# ── continuación automática en streaming ─────────────────────────────────

def test_autocontinue_joins_parts_and_emits_single_done():
    part1 = "Paso 1: preparar el entorno. Paso 2: instalar las dependencias necesarias y "
    part2 = "instalar las dependencias necesarias y configurar la base de datos. Fin."
    fake = _FakeStream([
        [_sse({"delta": part1}), _sse({"type": "finish", "reason": "length"}), "data: [DONE]\n\n"],
        [_sse({"delta": part2}), _sse({"type": "finish", "reason": "stop"}), "data: [DONE]\n\n"],
    ])
    msgs = [{"role": "user", "content": "explica los pasos"}]
    chunks = _collect(stream_with_autocontinue(fake, "u", "m", msgs, max_tokens=0))

    assert _text(chunks) == part1 + "configurar la base de datos. Fin."
    assert sum(1 for c in chunks if c.startswith("data: [DONE]")) == 1
    assert chunks[-1].startswith("data: [DONE]")
    assert any('"continued"' in c for c in chunks)
    assert not any('"finish"' in c for c in chunks)  # evento interno, no se reenvía
    # La segunda llamada lleva la parte escrita y la instrucción de continuar.
    second = fake.calls[1]["messages"]
    assert second[:1] == msgs
    assert second[-2] == {"role": "assistant", "content": part1}
    assert second[-1] == {"role": "user", "content": CONTINUE_PROMPT}


def test_autocontinue_respects_limit(monkeypatch):
    monkeypatch.setattr(autocontinue, "AUTO_CONTINUE_MAX", 2)
    cut = [_sse({"delta": "x" * 50}), _sse({"type": "finish", "reason": "length"})]
    fake = _FakeStream([list(cut), list(cut), list(cut), list(cut)])
    _collect(stream_with_autocontinue(fake, "u", "m", [], max_tokens=0))
    assert len(fake.calls) == 3  # original + 2 continuaciones


def test_no_continuation_for_small_explicit_limit():
    fake = _FakeStream([[_sse({"delta": "Título corto"}), _sse({"type": "finish", "reason": "length"})]])
    chunks = _collect(stream_with_autocontinue(fake, "u", "m", [], max_tokens=20))
    assert len(fake.calls) == 1
    assert _text(chunks) == "Título corto"


def test_no_continuation_when_tool_call_was_truncated():
    fake = _FakeStream([[
        _sse({"delta": "Voy a ejecutar"}),
        _sse({"type": "tool_calls", "calls": [{"id": "1", "name": "bash", "arguments": "{\"cmd"}]}),
        _sse({"type": "finish", "reason": "length"}),
    ]])
    chunks = _collect(stream_with_autocontinue(fake, "u", "m", [], max_tokens=0))
    assert len(fake.calls) == 1
    assert any('"tool_calls"' in c for c in chunks)


def test_no_continuation_after_error():
    fake = _FakeStream([[
        _sse({"delta": "parcial"}),
        'event: error\ndata: {"error": "Read timeout", "status": 504}\n\n',
        _sse({"type": "finish", "reason": "length"}),
    ]])
    chunks = _collect(stream_with_autocontinue(fake, "u", "m", [], max_tokens=0))
    assert len(fake.calls) == 1
    assert any(c.startswith("event: error") for c in chunks)


def test_thinking_deltas_pass_through_and_are_not_joined():
    fake = _FakeStream([[
        _sse({"delta": "pensando…", "thinking": True}),
        _sse({"delta": "Respuesta final."}),
        _sse({"type": "finish", "reason": "stop"}),
    ]])
    chunks = _collect(stream_with_autocontinue(fake, "u", "m", [], max_tokens=0))
    assert _text(chunks) == "Respuesta final."
    assert any('"thinking": true' in c for c in chunks)


# ── stream_llm informa del motivo de fin ─────────────────────────────────

class _FakeResp:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b""


class _FakeStreamCtx:
    def __init__(self, lines, sink):
        self._lines, self._sink = lines, sink

    async def __aenter__(self):
        return _FakeResp(self._lines)

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, lines):
        self._lines = lines
        self.payloads = []

    def stream(self, method, url, **kw):
        self.payloads.append(kw.get("json"))
        return _FakeStreamCtx(self._lines, self.payloads)


def _patch_client(monkeypatch, lines):
    client = _FakeClient(lines)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_mark_host_dead", lambda *a, **k: False, raising=False)
    return client


def test_stream_llm_emits_finish_length_for_openai_compat(monkeypatch):
    _patch_client(monkeypatch, [
        'data: {"choices":[{"delta":{"content":"hola"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
        "data: [DONE]",
    ])
    chunks = _collect(llm_core.stream_llm(
        "https://api.example.com/v1/chat/completions", "m",
        [{"role": "user", "content": "hi"}]))
    finishes = [json.loads(c[6:]) for c in chunks if '"finish"' in c]
    assert finishes == [{"type": "finish", "reason": "length"}]


def test_stream_llm_emits_finish_for_anthropic(monkeypatch):
    _patch_client(monkeypatch, [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":3}}}',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hola"}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},"usage":{"output_tokens":5}}',
        'data: {"type":"message_stop"}',
    ])
    chunks = _collect(llm_core.stream_llm(
        "https://api.anthropic.com/v1/messages", "claude-x",
        [{"role": "user", "content": "hi"}]))
    assert any(json.loads(c[6:]).get("reason") == "max_tokens" for c in chunks if '"finish"' in c)


def test_fallback_wrapper_autocontinues_end_to_end(monkeypatch):
    """stream_llm_with_fallback (chat y agente) completa la respuesta cortada."""
    scripts = [
        ['data: {"choices":[{"delta":{"content":"Primera mitad de la respuesta, "}}]}',
         'data: {"choices":[{"delta":{},"finish_reason":"length"}]}', "data: [DONE]"],
        ['data: {"choices":[{"delta":{"content":"segunda mitad."}}]}',
         'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}', "data: [DONE]"],
    ]

    class _SeqClient(_FakeClient):
        def stream(self, method, url, **kw):
            self.payloads.append(kw.get("json"))
            return _FakeStreamCtx(scripts.pop(0), self.payloads)

    client = _SeqClient([])
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)

    chunks = _collect(llm_core.stream_llm_with_fallback(
        [("https://api.example.com/v1/chat/completions", "m", {})],
        [{"role": "user", "content": "hola"}], max_tokens=0))
    assert _text(chunks) == "Primera mitad de la respuesta, segunda mitad."
    assert len(client.payloads) == 2
    assert client.payloads[1]["messages"][-1]["content"] == CONTINUE_PROMPT


# ── continuación sin streaming ───────────────────────────────────────────

def test_llm_call_async_continues_truncated_response(monkeypatch):
    responses = [
        {"choices": [{"message": {"content": "Informe: sección uno completa y sección"}, "finish_reason": "length"}]},
        {"choices": [{"message": {"content": " dos completa."}, "finish_reason": "stop"}]},
    ]
    sent = []

    class _Resp:
        def __init__(self, data):
            self._data, self.status_code, self.is_success, self.text = data, 200, True, ""

        def json(self):
            return self._data

    class _Client:
        async def post(self, url, headers=None, json=None, timeout=None):
            sent.append(json)
            return _Resp(responses.pop(0))

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _Client())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_get_cached_response", lambda k: None)

    out = asyncio.run(llm_core.llm_call_async(
        "https://api.example.com/v1/chat/completions", "m",
        [{"role": "user", "content": "escribe el informe"}], max_tokens=4096))
    assert out == "Informe: sección uno completa y sección dos completa."
    assert len(sent) == 2


# ── opciones de OpenRouter ───────────────────────────────────────────────

def test_openrouter_options_usage_and_catalog_max_tokens(monkeypatch):
    async def fake_info(model):
        return {"max_completion_tokens": 65536, "context_length": 163840}
    import src.openrouter_catalog as cat
    monkeypatch.setattr(cat, "get_model_info_async", fake_info)
    payload = {"model": "deepseek/deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]}
    asyncio.run(llm_core._apply_openrouter_options(payload, payload["model"], 0))
    assert payload["usage"] == {"include": True}
    from src.ruffs_config import DEFAULT_OUTPUT_TOKENS_CAP
    # El máximo del modelo (65536), acotado por RUFFS_MAX_OUTPUT_TOKENS (32768 por defecto).
    assert payload["max_tokens"] == min(65536, DEFAULT_OUTPUT_TOKENS_CAP)


def test_openrouter_options_keep_explicit_max_tokens(monkeypatch):
    payload = {"model": "x/y", "messages": [], "max_tokens": 500}
    asyncio.run(llm_core._apply_openrouter_options(payload, "x/y", 500))
    assert payload["max_tokens"] == 500


def test_openrouter_prompt_cache_for_anthropic(monkeypatch):
    async def no_info(model):
        return None
    import src.openrouter_catalog as cat
    monkeypatch.setattr(cat, "get_model_info_async", no_info)
    long_sys = "regla " * 2000
    payload = {"model": "anthropic/claude-sonnet-4.5", "messages": [
        {"role": "system", "content": long_sys}, {"role": "user", "content": "hi"}]}
    asyncio.run(llm_core._apply_openrouter_options(payload, payload["model"], 0))
    sys_content = payload["messages"][0]["content"]
    assert isinstance(sys_content, list)
    assert sys_content[0]["cache_control"] == {"type": "ephemeral"}
    assert sys_content[0]["text"] == long_sys


def test_openrouter_no_cache_for_other_models(monkeypatch):
    async def no_info(model):
        return None
    import src.openrouter_catalog as cat
    monkeypatch.setattr(cat, "get_model_info_async", no_info)
    payload = {"model": "deepseek/deepseek-v4-flash", "messages": [
        {"role": "system", "content": "x" * 9000}]}
    asyncio.run(llm_core._apply_openrouter_options(payload, payload["model"], 0))
    assert isinstance(payload["messages"][0]["content"], str)


def test_openrouter_stream_payload_includes_usage(monkeypatch):
    async def no_info(model):
        return None
    import src.openrouter_catalog as cat
    monkeypatch.setattr(cat, "get_model_info_async", no_info)
    client = _patch_client(monkeypatch, ['data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}', "data: [DONE]"])
    _collect(llm_core.stream_llm("https://openrouter.ai/api/v1/chat/completions",
                                 "deepseek/deepseek-v4-flash", [{"role": "user", "content": "hi"}]))
    assert client.payloads[0]["usage"] == {"include": True}


def test_openrouter_usage_cost_is_reported(monkeypatch):
    async def no_info(model):
        return None
    import src.openrouter_catalog as cat
    monkeypatch.setattr(cat, "get_model_info_async", no_info)
    _patch_client(monkeypatch, [
        'data: {"choices":[{"delta":{"content":"ok"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2,"cost":0.00042,"prompt_tokens_details":{"cached_tokens":8}}}',
        "data: [DONE]",
    ])
    chunks = _collect(llm_core.stream_llm("https://openrouter.ai/api/v1/chat/completions",
                                          "deepseek/deepseek-v4-flash", [{"role": "user", "content": "hi"}]))
    usage = [json.loads(c[6:])["data"] for c in chunks if '"usage"' in c]
    assert usage and usage[0]["cost"] == 0.00042 and usage[0]["cached_tokens"] == 8


# ── memoria de herramientas ──────────────────────────────────────────────

def _assistant(text, events):
    return {"role": "assistant", "content": text, "metadata": {"tool_events": events}}


def test_tool_log_is_attached_to_assistant_messages():
    msgs = [
        {"role": "user", "content": "lista los archivos"},
        _assistant("Listo, hay 2 archivos.", [
            {"tool": "bash", "command": "ls", "output": "a.txt\nb.txt", "exit_code": 0}]),
        {"role": "user", "content": "continúa"},
    ]
    out = tool_memory.attach_tool_logs(msgs)
    assert out is not msgs and msgs[1]["content"] == "Listo, hay 2 archivos."  # no muta
    assert "[RUFFS tool log" in out[1]["content"]
    assert "1. bash: ls" in out[1]["content"] and "a.txt" in out[1]["content"]
    assert out[0] == msgs[0] and out[2] == msgs[2]


def test_tool_log_only_for_recent_turns():
    msgs = []
    for i in range(7):
        msgs.append({"role": "user", "content": f"paso {i}"})
        msgs.append(_assistant(f"hecho {i}", [{"tool": "bash", "command": f"echo {i}", "output": str(i)}]))
    out = tool_memory.attach_tool_logs(msgs)
    with_log = [m for m in out if "[RUFFS tool log" in (m.get("content") or "")]
    assert len(with_log) == tool_memory.RECENT_TURNS_WITH_LOG
    assert "hecho 6" in with_log[-1]["content"]


def test_tool_log_clips_large_outputs():
    big = "línea\n" * 5000
    log = tool_memory.summarize_tool_events([{"tool": "read_file", "command": "big.log", "output": big}])
    assert len(log) < 2000
    assert "caracteres omitidos" in log


def test_tool_log_marks_failures():
    log = tool_memory.summarize_tool_events([{"tool": "bash", "command": "make", "output": "error", "exit_code": 2}])
    assert "(exit 2)" in log


def test_session_context_messages_include_tool_log():
    from core.models import ChatMessage, Session
    s = Session(id="s1", name="t", endpoint_url="u", model="m")
    s.history.append(ChatMessage("user", "crea el archivo"))
    s.history.append(ChatMessage("assistant", "Creado.", metadata={
        "tool_events": [{"tool": "write_file", "command": "notas.md", "output": "ok"}]}))
    msgs = s.get_context_messages()
    assert "[RUFFS tool log" in msgs[1]["content"]
    # El historial que ve la interfaz no cambia.
    assert s.history[1].content == "Creado."
