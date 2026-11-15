"""Continuación automática de respuestas cortadas por límite de tokens.

Problema que resuelve (heredado de Odysseus): cuando un proveedor termina una
respuesta por límite de longitud (``finish_reason == "length"`` en la API de
OpenAI/OpenRouter, ``stop_reason == "max_tokens"`` en Anthropic,
``done_reason == "length"`` en Ollama), Odysseus lo trataba como una respuesta
completa. El usuario veía el texto cortado a la mitad y tenía que escribir
"continúa"; el modelo, sin contexto de que era una continuación, volvía a
verificar todo lo anterior y gastaba tokens.

RUFFS detecta el corte, pide al mismo modelo que siga exactamente donde quedó
(sin repetir, sin resumir y sin volver a verificar) y empalma las partes en un
único flujo, eliminando el solapamiento cuando el modelo repite las últimas
palabras.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncGenerator, Callable, Dict, List, Optional

from src.ruffs_config import AUTO_CONTINUE_ENABLED, AUTO_CONTINUE_MAX

logger = logging.getLogger(__name__)

CONTINUE_PROMPT = (
    "Your previous message was cut off by the output length limit. Continue "
    "EXACTLY from the last character you wrote, as if no interruption happened. "
    "Output only the continuation: do not repeat anything already written, do "
    "not restart, do not summarize, do not apologize, and do not re-check or "
    "re-verify earlier work. If you were in the middle of a code block or a "
    "list, keep going inside it."
)

# Límites pequeños los fija el llamante a propósito (títulos, clasificaciones,
# etiquetas...). Ahí el corte es intencionado y no se continúa.
MIN_MAX_TOKENS_FOR_CONTINUE = 1024

# Cuántos caracteres del final de la parte anterior se comparan para detectar
# que el modelo repitió texto al reanudar.
_OVERLAP_WINDOW = 400
_MIN_OVERLAP = 12


def normalize_finish_reason(raw: Optional[str]) -> Optional[str]:
    """Unifica los motivos de fin de los distintos proveedores."""
    if not raw:
        return None
    r = str(raw).strip().lower()
    if r in ("length", "max_tokens", "max_output_tokens", "model_length"):
        return "length"
    if r in ("stop", "end_turn", "stop_sequence", "eos"):
        return "stop"
    if r in ("tool_calls", "tool_use", "function_call"):
        return "tool_calls"
    return r


def continuation_allowed(max_tokens: Optional[int]) -> bool:
    """¿Se debe continuar automáticamente una respuesta con este límite?"""
    if not AUTO_CONTINUE_ENABLED or AUTO_CONTINUE_MAX <= 0:
        return False
    if not max_tokens or max_tokens <= 0:
        return True
    return max_tokens >= MIN_MAX_TOKENS_FOR_CONTINUE


def build_continuation_messages(messages: List[Dict], partial_text: str) -> List[Dict]:
    """Historial para pedir la continuación: lo que había + la parte ya escrita."""
    return list(messages) + [
        {"role": "assistant", "content": partial_text},
        {"role": "user", "content": CONTINUE_PROMPT},
    ]


def trim_overlap(previous: str, continuation: str) -> str:
    """Quita del inicio de ``continuation`` lo que repite el final de ``previous``.

    Los modelos a veces reanudan repitiendo las últimas palabras o la última
    línea. Se busca el sufijo más largo de ``previous`` que sea prefijo de
    ``continuation`` (ignorando espacios iniciales de la continuación).
    """
    if not previous or not continuation:
        return continuation
    tail = previous[-_OVERLAP_WINDOW:]
    lead_ws = len(continuation) - len(continuation.lstrip())
    body = continuation[lead_ws:]
    best = 0
    max_len = min(len(tail), len(body))
    for size in range(max_len, _MIN_OVERLAP - 1, -1):
        if tail.endswith(body[:size]):
            best = size
            break
    if best:
        return body[best:]
    return continuation


def _parse_sse(chunk: str) -> Optional[dict]:
    if not chunk.startswith("data: ") or chunk.startswith("data: [DONE]"):
        return None
    try:
        data = json.loads(chunk[6:])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


async def stream_with_autocontinue(
    stream_fn: Callable[..., AsyncGenerator[str, None]],
    url: str,
    model: str,
    messages: List[Dict],
    *,
    headers: Optional[Dict] = None,
    emit_finish: bool = False,
    **kwargs,
) -> AsyncGenerator[str, None]:
    """Envuelve ``stream_fn`` (``stream_llm``) y continúa si se corta por longitud.

    Mantiene el protocolo SSE de ``stream_llm``: ``data: {"delta": ...}``,
    eventos ``type``, ``event: error`` y un único ``data: [DONE]`` final. Los
    eventos internos ``finish`` no se reenvían; en su lugar, si hubo
    continuaciones, se emite ``{"type": "continued", "count": N}`` como
    información para quien quiera mostrarlo.
    """
    max_tokens = kwargs.get("max_tokens")
    allowed = continuation_allowed(max_tokens)
    current_messages = messages
    full_text = ""
    continuations = 0
    finish = None

    while True:
        finish = None
        saw_tool_calls = False
        errored = False
        is_continuation = continuations > 0
        # En una continuación se retiene el comienzo para quitar el solapamiento.
        pending = "" if is_continuation else None
        prefix_for_overlap = full_text

        async for chunk in stream_fn(url, model, current_messages, headers=headers, **kwargs):
            if chunk.startswith("event: error"):
                errored = True
                if pending:
                    yield f'data: {json.dumps({"delta": pending})}\n\n'
                    full_text += pending
                    pending = None
                yield chunk
                continue
            if chunk.startswith("data: [DONE]"):
                continue  # el [DONE] final lo emite este envoltorio
            data = _parse_sse(chunk)
            if data is None:
                yield chunk
                continue
            if data.get("type") == "finish":
                finish = normalize_finish_reason(data.get("reason"))
                continue
            if data.get("type") == "tool_calls":
                saw_tool_calls = True
                yield chunk
                continue
            if "delta" in data and not data.get("thinking"):
                text = data.get("delta") or ""
                if pending is not None:
                    pending += text
                    if len(pending) < _OVERLAP_WINDOW:
                        continue
                    text = trim_overlap(prefix_for_overlap, pending)
                    pending = None
                    if not text:
                        continue
                    data = dict(data, delta=text)
                    chunk = f"data: {json.dumps(data)}\n\n"
                full_text += text
                yield chunk
                continue
            yield chunk

        if pending:
            text = trim_overlap(prefix_for_overlap, pending)
            if text:
                full_text += text
                yield f'data: {json.dumps({"delta": text})}\n\n'

        if (
            finish == "length"
            and allowed
            and not errored
            and not saw_tool_calls
            and full_text.strip()
            and continuations < AUTO_CONTINUE_MAX
        ):
            continuations += 1
            logger.info(
                "[autocontinue] %s cortado por longitud; continuación %d/%d (%d caracteres hasta ahora)",
                model, continuations, AUTO_CONTINUE_MAX, len(full_text),
            )
            current_messages = build_continuation_messages(messages, full_text)
            continue

        if finish == "length" and not errored:
            if saw_tool_calls:
                logger.warning("[autocontinue] %s cortado por longitud dentro de una llamada a herramienta", model)
            elif continuations >= AUTO_CONTINUE_MAX and allowed:
                logger.warning("[autocontinue] %s sigue cortándose tras %d continuaciones", model, continuations)
        break

    if continuations:
        yield f'data: {json.dumps({"type": "continued", "count": continuations})}\n\n'
    # RUFFS: si el envoltorio se rinde por longitud, reenvia el motivo para que
    # el consumidor no confunda una respuesta cortada con una respuesta completa.
    if emit_finish and finish == "length":
        yield f'data: {json.dumps({"type": "finish", "reason": "length"})}\n\n'
    yield "data: [DONE]\n\n"
