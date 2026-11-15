"""Memoria de herramientas entre turnos.

Problema que resuelve (heredado de Odysseus): el agente guarda en los
metadatos de cada respuesta lo que ejecutó (``tool_events``: comando, salida,
código de salida...), pero ``_sanitize_llm_messages`` descarta los metadatos
antes de enviar el historial al proveedor. En el turno siguiente el modelo solo
veía lo que *dijo*, no lo que *hizo* ni lo que obtuvo. Al pedirle "continúa",
volvía a leer archivos y a ejecutar comprobaciones para reconstruir su estado,
gastando tokens.

RUFFS adjunta al contenido de esas respuestas un registro compacto de las
herramientas, solo en los últimos turnos y con tamaño acotado.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from src.ruffs_config import (
    TOOL_MEMORY_ENABLED, TOOL_MEMORY_MAX_CHARS, TOOL_MEMORY_OUTPUT_CHARS,
)

# Solo las respuestas más recientes llevan el registro: las antiguas ya no
# suelen hacer falta y el recorte de contexto las sacrificaría igualmente.
RECENT_TURNS_WITH_LOG = 4

LOG_HEADER = (
    "[RUFFS tool log — actions you ALREADY executed in this turn and their "
    "results. Treat it as your own memory: do not redo or re-verify these "
    "steps unless a result shows an error, and do not repeat this log to the user.]"
)


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.25):]
    return f"{head}\n…[{len(text) - len(head) - len(tail)} caracteres omitidos]…\n{tail}"


def summarize_tool_events(events: Optional[Iterable[dict]]) -> str:
    """Convierte ``tool_events`` en un registro de texto compacto."""
    lines: List[str] = []
    for i, ev in enumerate(events or [], 1):
        if not isinstance(ev, dict):
            continue
        tool = ev.get("tool") or "?"
        cmd = (ev.get("command") or "").strip()
        rc = ev.get("exit_code")
        head = f"{i}. {tool}"
        if cmd:
            head += f": {_clip(cmd, 300)}"
        if rc not in (None, 0):
            head += f"  (exit {rc})"
        if ev.get("doc_id"):
            head += f"  [documento: {ev.get('doc_title') or ''} id={ev.get('doc_id')}]"
        lines.append(head)
        out = _clip(ev.get("output") or "", TOOL_MEMORY_OUTPUT_CHARS)
        if out:
            lines.append("   → " + out.replace("\n", "\n     "))
    if not lines:
        return ""
    body = "\n".join(lines)
    if len(body) > TOOL_MEMORY_MAX_CHARS:
        body = _clip(body, TOOL_MEMORY_MAX_CHARS)
    return f"{LOG_HEADER}\n{body}"


def attach_tool_logs(messages: List[dict]) -> List[dict]:
    """Devuelve una copia del historial con el registro en las últimas respuestas.

    ``messages`` son dicts con ``role``, ``content`` y opcionalmente
    ``metadata``. No modifica la lista original.
    """
    if not TOOL_MEMORY_ENABLED:
        return messages
    targets = [
        i for i, m in enumerate(messages)
        if m.get("role") == "assistant" and (m.get("metadata") or {}).get("tool_events")
    ][-RECENT_TURNS_WITH_LOG:]
    if not targets:
        return messages
    out = list(messages)
    for i in targets:
        msg = out[i]
        log = summarize_tool_events(msg["metadata"]["tool_events"])
        if not log:
            continue
        content = msg.get("content") or ""
        if isinstance(content, str):
            out[i] = dict(msg, content=(content.rstrip() + "\n\n" + log).strip())
    return out
