"""Tests del registro y conexión de servidores MCP en RUFFS.

Fallos de Odysseus que cubren:
- una conexión sin límite de tiempo podía colgarse para siempre;
- al arrancar se conectaban uno a uno con 20 s para todos (los últimos nunca
  llegaban a conectarse);
- no había transporte HTTP para servidores remotos.
"""
import asyncio
import shutil
import time

import pytest

from src import mcp_manager as mm


def test_resolve_command_uses_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda c: r"C:\Program Files\nodejs\npx.cmd" if c == "npx" else None)
    assert mm._resolve_command("npx") == r"C:\Program Files\nodejs\npx.cmd"
    # Rutas explícitas y comandos desconocidos se respetan.
    assert mm._resolve_command(r"C:\tools\srv.exe") == r"C:\tools\srv.exe"
    assert mm._resolve_command("noexiste") == "noexiste"


def test_launchers_get_longer_timeout():
    assert mm._connect_timeout("npx", []) == mm._CONNECT_TIMEOUT_LAUNCHER
    assert mm._connect_timeout(r"C:\x\uvx.exe", []) == mm._CONNECT_TIMEOUT_LAUNCHER
    assert mm._connect_timeout("python", []) == mm._CONNECT_TIMEOUT


def test_hanging_server_times_out_with_clear_error(monkeypatch):
    monkeypatch.setattr(mm, "_CONNECT_TIMEOUT", 0.2)

    async def hang(self, *a, **k):
        await asyncio.sleep(30)

    monkeypatch.setattr(mm.McpManager, "_connect_stdio", hang)
    mgr = mm.McpManager()
    t0 = time.monotonic()
    ok = asyncio.run(mgr.connect_server("s1", "lento", "stdio", command="python", args=["srv.py"]))
    assert ok is False
    assert time.monotonic() - t0 < 5
    st = mgr.get_server_status("s1")
    assert st["status"] == "error" and "no respondió" in st["error"]


def test_unknown_transport_is_reported():
    mgr = mm.McpManager()
    ok = asyncio.run(mgr.connect_server("s2", "raro", "websocket", url="ws://x"))
    assert ok is False
    assert "desconocido" in mgr.get_server_status("s2")["error"]


def test_http_transport_requires_url():
    mgr = mm.McpManager()
    ok = asyncio.run(mgr.connect_server("s3", "remoto", "http", url=None))
    assert ok is False


def test_connect_all_enabled_runs_in_parallel(monkeypatch):
    """Cinco servidores de 0,5 s deben conectar en bastante menos de 2,5 s."""
    from types import SimpleNamespace

    rows = [SimpleNamespace(id=f"id{i}", name=f"srv{i}", transport="stdio", command="python",
                            args=f'["s{i}.py"]', env="{}", url=None) for i in range(5)]
    rows.append(SimpleNamespace(id="bad", name="roto", transport="stdio", command="python",
                                args="[esto no es json", env="{}", url=None))

    class _Q:
        def filter(self, *a):
            return self

        def all(self):
            return rows

    class _DB:
        def query(self, *a):
            return _Q()

        def close(self):
            pass

    import sys

    class _FakeMcpServer:
        is_enabled = True

    monkeypatch.setitem(sys.modules, "src.database",
                        SimpleNamespace(McpServer=_FakeMcpServer, SessionLocal=lambda: _DB()))

    async def slow_ok(self, server_id, name, command, args, env):
        await asyncio.sleep(0.5)
        self._connections[server_id] = {"status": "connected", "name": name, "transport": "stdio",
                                        "command": command, "args": list(args)}
        self._connected_ids.add(server_id)
        return True

    monkeypatch.setattr(mm.McpManager, "_connect_stdio", slow_ok)
    mgr = mm.McpManager()
    t0 = time.monotonic()
    ok = asyncio.run(mgr.connect_all_enabled())
    elapsed = time.monotonic() - t0
    assert ok == 5
    assert elapsed < 2.0
    # La fila con JSON inválido queda marcada con un error claro, sin romper el resto.
    assert mgr.get_server_status("bad")["status"] == "error"
    assert "JSON" in mgr.get_server_status("bad")["error"]
