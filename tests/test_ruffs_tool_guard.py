"""RUFFS: el agente no repite consultas ni fallos inútiles (caso real: status ×4,
mark_elements ×3, la misma llamada fallida ×7 escribiendo «voy a hacerlo»)."""
import asyncio
import json
import os
import sys

import pytest

from src.tool_guard import TurnToolGuard, is_read_only


def test_read_only_classification():
    assert is_read_only("mcp__computer_use__status")
    assert is_read_only("mcp__computer_use__mark_elements")
    assert is_read_only("mcp__63ff5b71__inspect_macho")
    assert not is_read_only("mcp__computer_use__click")
    assert not is_read_only("mcp__computer_use__wait")
    assert not is_read_only("bash")


def test_repeated_query_without_action_is_blocked():
    g = TurnToolGuard()
    g.start_round()
    assert g.check("mcp__computer_use__status", "{}") is None
    g.record("mcp__computer_use__status", "{}", {"output": "en_pausa: false"})
    assert g.check("mcp__computer_use__list_screens", "{}") is None          # otra consulta: sí
    g.record("mcp__computer_use__list_screens", "{}", {"output": "1 pantalla"})
    blocked = g.check("mcp__computer_use__status", "{ }")                     # misma, sin acción
    assert blocked and "SIN CAMBIOS" in blocked[1]["output"] and "en_pausa" in blocked[1]["output"]
    # Tras una acción, volver a mirar sí tiene sentido.
    assert g.check("mcp__computer_use__click", '{"x": 1, "y": 2}') is None
    g.record("mcp__computer_use__click", '{"x": 1, "y": 2}', {"output": "Hecho"})
    assert g.check("mcp__computer_use__status", "{}") is None


def test_repeated_failure_is_blocked_until_something_changes():
    g = TurnToolGuard()
    g.start_round()
    err = {"output": '{"error":{"code":"target_unavailable","message":"No app is open."}}'}
    assert g.check("mcp__rea__inspect_macho", "{}") is None
    g.record("mcp__rea__inspect_macho", "{}", err)
    b = g.check("mcp__rea__inspect_macho", "{}")
    assert b and "NO EJECUTADA" in b[1]["error"] and "No app is open" in b[1]["error"]
    assert g.check("mcp__rea__inspect_macho", '{"path": "C:/x.exe"}') is None  # otros argumentos: sí
    # Un fallo de una acción tampoco se repite igual
    g.record("mcp__computer_use__click", '{"element": 9}', {"error": "No hay elemento 9"})
    assert g.check("mcp__computer_use__click", '{"element": 9}') is not None


def test_round_all_blocked():
    g = TurnToolGuard()
    g.start_round()
    g.check("mcp__x__status", "{}")
    g.record("mcp__x__status", "{}", {"output": "ok"})
    g.start_round()
    g.check("mcp__x__status", "{}")
    assert g.round_all_blocked()


def test_agent_stops_repeating_quickly():
    """Modelo simulado que repite la misma llamada fallida con una frase de narración
    distinta cada vez (como en el chat real). Antes: hasta 8+ rondas; ahora la llamada
    se ejecuta una sola vez y el detector corta en pocas rondas."""
    r = _sim("repeat_guard")
    assert r["executed"] == 1, r
    assert r["calls"] <= 7, r
    assert r["blocked_note"], r


class _FakeMgr:
    def __init__(self):
        self._tools = {
            "computer_use": [{"name": n} for n in ("screenshot", "click", "type_text", "open_target",
                                                     "status", "mark_elements", "wait", "zoom")],
            "63ff5b71": [{"name": f"tool_{i}"} for i in range(255)],
            "email": [{"name": "list_emails"}],
        }
        self._connections = {"computer_use": {"name": "Computer Use (RUFFS ve y maneja tu escritorio)"},
                             "63ff5b71": {"name": "rea"}, "email": {"name": "Built-in: Email"}}

    def is_builtin(self, sid):
        return sid in {"email", "memory", "rag", "image_gen"} or sid.startswith("builtin_")


class _FakeIdx:
    def retrieve(self, query, k=8):
        return [f"mcp__63ff5b71__tool_{i}" for i in range(0, 200, 3)]


def test_expand_selection_includes_whole_small_server():
    from src.tool_index import expand_mcp_selection
    got = expand_mcp_selection({"bash", "mcp__computer_use__status"}, "abre el programa", _FakeMgr())
    assert {"mcp__computer_use__click", "mcp__computer_use__open_target"} <= got
    assert not any(n.startswith("mcp__63ff5b71__") for n in got)
    assert "mcp__email__list_emails" not in got


def test_expand_selection_by_mention_and_persona():
    from src.tool_index import expand_mcp_selection
    got = expand_mcp_selection({"bash"}, "usa el mcp de computer ruffs y abre craft", _FakeMgr())
    assert "mcp__computer_use__click" in got
    got = expand_mcp_selection({"bash"}, "abre craft", _FakeMgr(), extra_text="Eres Computer Ruffs, el agente")
    assert "mcp__computer_use__click" in got
    got = expand_mcp_selection({"bash"}, "analiza este binario con rea", _FakeMgr(), _FakeIdx())
    big = [n for n in got if n.startswith("mcp__63ff5b71__")]
    assert 0 < len(big) <= 25


def test_dsml_calls_wrapper_is_parsed_and_stripped():
    r = _sim("dsml")
    assert r["tools"] == ["mcp__x__click"] and not r["leftover"], r


def test_windows_shell_note(monkeypatch):
    import src.agent_loop as al
    monkeypatch.setattr(al.os, "name", "nt")
    p = al._assemble_prompt({"bash", "web_search"})
    assert "cmd.exe" in p
    p = al._assemble_prompt({"web_search"})
    assert "cmd.exe" not in p


def _sim(name):
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, os.path.join(root, "tests", "_agent_sim.py"), name],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=root, timeout=300, env={**os.environ, "PYTHONUTF8": "1"})
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, (r.stdout[-1500:], r.stderr[-1500:])
    return json.loads(line[7:])
