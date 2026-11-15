"""RUFFS: freno de llamadas inútiles dentro de un mismo turno del agente.

Problema que resuelve (visto en uso real): el modelo repetía la MISMA consulta
(status, mark_elements, list_screens, screenshot…) una y otra vez sin hacer nada
entre medias, o reintentaba sin cambiar nada una llamada que ya había fallado
(p. ej. la misma herramienta con el mismo error 7 veces). Escribía «voy a
hacerlo» entre llamadas, así que el detector de bucles lo tomaba por progreso.

Reglas, por turno:
  * Consulta repetida sin cambios: una herramienta de solo lectura llamada otra
    vez con los mismos argumentos, sin que ninguna acción haya ocurrido desde
    la anterior, no se ejecuta. El modelo recibe «SIN CAMBIOS» con el resultado
    anterior resumido y la instrucción de actuar o responder.
  * Fallo repetido: una llamada idéntica a otra que ya falló, sin ninguna
    acción entre medias, no se ejecuta: hay que cambiar algo.
  * «Acción» es cualquier herramienta que no sea de solo lectura y termine bien
    (clic, escribir, abrir, ejecutar un comando, esperar…): puede cambiar lo
    que hay, así que después se permite volver a consultar.

Los bloqueos cuentan como rondas sin progreso para el detector de bucles.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple

# Nombres (sin el prefijo mcp__servidor__) de herramientas que solo consultan.
_READ_ONLY_EXACT = {
    "screenshot", "zoom", "mark_elements", "status", "list_screens", "cursor_position",
    "action_log", "ghidra_status", "binary_info", "extract_strings", "web_search", "web_fetch",
    "read_file", "grep", "glob", "ls", "list_models", "list_sessions", "search_chats",
    "snapshot", "find", "get", "is", "version", "permissions",
}
_READ_ONLY_PREFIXES = (
    "list_", "get_", "read_", "inspect_", "view", "search", "find_", "query_", "describe_",
    "show_", "check_", "fetch_", "lookup_", "resolve_", "count_", "status", "desktop_list_",
    "desktop_get_", "desktop_find", "desktop_snapshot", "desktop_screenshot", "desktop_is_",
)
# Siempre se consideran acción (el estado puede cambiar aunque «solo» esperen).
_ACTION_EXACT = {"wait", "sleep", "bash", "python", "write_file", "edit_file"}

_SUMMARY_CHARS = 600


def _base_name(tool: str) -> str:
    return tool.split("__", 2)[-1] if tool.startswith("mcp__") else tool


def is_read_only(tool: str) -> bool:
    name = _base_name(tool).lower()
    if name in _ACTION_EXACT:
        return False
    return name in _READ_ONLY_EXACT or name.startswith(_READ_ONLY_PREFIXES)


def _normalize_args(content: str) -> str:
    text = (content or "").strip()
    try:
        return json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False)
    except (ValueError, TypeError):
        return re.sub(r"\s+", " ", text)


def _result_text(result: Dict[str, Any]) -> str:
    for key in ("error", "output", "results", "stdout", "result"):
        val = result.get(key)
        if val:
            return val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
    return ""


def _is_error(result: Dict[str, Any]) -> bool:
    if result.get("error"):
        return True
    code = result.get("exit_code")
    if isinstance(code, int) and code not in (0, None):
        return True
    text = _result_text(result).lstrip()
    # Errores estructurados que algunos MCP devuelven como texto (p. ej. REA).
    return text.startswith('{"error"') or text.startswith("Error:") or '"error":{"code"' in text[:200]


class TurnToolGuard:
    """Estado de un turno: qué se consultó, qué falló y cuántas acciones hubo."""

    def __init__(self) -> None:
        self._epoch = 0  # sube con cada acción que termina bien
        self._seen: Dict[str, Tuple[int, bool, str]] = {}  # firma → (epoch, error, resumen)
        self._round_calls = 0
        self._round_blocked = 0
        self.total_blocked = 0

    @staticmethod
    def signature(tool: str, content: str) -> str:
        return f"{tool}\x00{_normalize_args(content)}"

    def start_round(self) -> None:
        self._round_calls = 0
        self._round_blocked = 0

    def round_all_blocked(self) -> bool:
        return self._round_calls > 0 and self._round_blocked == self._round_calls

    def check(self, tool: str, content: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        """None si la llamada puede ejecutarse; si no, (desc, resultado sintético)."""
        self._round_calls += 1
        prev = self._seen.get(self.signature(tool, content))
        if not prev or prev[0] != self._epoch:
            return None
        _epoch, was_error, summary = prev
        if was_error:
            msg = (f"NO EJECUTADA: ya hiciste exactamente esta llamada a {tool} en este turno y falló, "
                   f"y desde entonces no ha cambiado nada. Error anterior: {summary}\n"
                   "No la repitas igual: cambia los argumentos, usa otra herramienta o explica al "
                   "usuario qué falta o qué necesitas de él.")
            desc, result = f"{tool}: REPETIDA (ya falló)", {"error": msg, "exit_code": 1}
        elif is_read_only(tool):
            msg = (f"SIN CAMBIOS: ya consultaste {tool} con estos mismos argumentos en este turno y "
                   "no has hecho ninguna acción desde entonces, así que el resultado sería el mismo. "
                   f"Resultado anterior (resumido): {summary}\n"
                   "No vuelvas a consultar: da el siguiente paso real (clic, escribir, abrir, ejecutar…) "
                   "o responde al usuario con lo que ya sabes.")
            desc, result = f"{tool}: REPETIDA (sin cambios)", {"output": msg, "exit_code": 0}
        else:
            return None
        self._round_blocked += 1
        self.total_blocked += 1
        return desc, result

    def record(self, tool: str, content: str, result: Dict[str, Any]) -> None:
        error = _is_error(result)
        summary = _result_text(result).strip().replace("\n", " ")[:_SUMMARY_CHARS]
        if not error and not is_read_only(tool):
            self._epoch += 1  # una acción: lo consultado antes puede haber cambiado
        self._seen[self.signature(tool, content)] = (self._epoch, error, summary)
