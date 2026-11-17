"""RUFFS: las herramientas se llaman a sí mismas en el puerto REAL del servidor
(antes fijo en 7000, de Odysseus: app_api, Deep Research, galería y contactos
fallaban con «All connection attempts failed»)."""
import sys

from core import constants as c


def test_port_resolution_order(monkeypatch):
    monkeypatch.delenv("RUFFS_SERVER_PORT", raising=False)
    monkeypatch.delenv("APP_PORT", raising=False)
    monkeypatch.setattr(sys, "argv", ["uvicorn", "app:app"])
    assert c.server_port() == 7100                         # por defecto
    monkeypatch.setenv("APP_PORT", "7300")
    assert c.server_port() == 7300                         # .env
    monkeypatch.setattr(sys, "argv", ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7400"])
    assert c.server_port() == 7400                         # uvicorn --port manda sobre .env
    monkeypatch.setattr(sys, "argv", ["uvicorn", "app:app", "--port=7450"])
    assert c.server_port() == 7450
    monkeypatch.setenv("RUFFS_SERVER_PORT", "7500")
    assert c.server_port() == 7500                         # ruffs_desktop.py --port manda sobre todo
    assert c.loopback_base() == "http://127.0.0.1:7500"   # IPv4 directo


def test_no_hardcoded_7000_left():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("src/tool_implementations.py", "routes/mcp_routes.py", "companion/pairing.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "localhost:7000" not in text and '"7000"' not in text, rel
