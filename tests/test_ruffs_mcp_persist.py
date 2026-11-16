"""RUFFS: un MCP que conectas («Reconectar») queda activado y se reconecta solo al
abrir RUFFS; los activados que fallan al arrancar se reintentan solos."""
import asyncio
import json

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as database
    import routes.mcp_routes as mr
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    database.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", Session)
    monkeypatch.setattr(mr, "SessionLocal", Session)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    return Session


class FakeMgr:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    async def disconnect_server(self, sid):
        pass

    async def connect_server(self, **kw):
        self.calls.append(kw["server_id"])
        return self.ok

    def get_server_status(self, sid):
        return {"status": "connected" if self.ok else "error", "tool_count": 3}


def _client(mgr):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import importlib
    import core.database as database
    import routes.mcp_routes as mr
    mr = importlib.reload(mr)                 # el router es global: uno limpio por test
    mr.SessionLocal = database.SessionLocal   # la BD temporal del fixture
    app = FastAPI()
    app.include_router(mr.setup_mcp_routes(mgr))
    return TestClient(app)


def _add(db, enabled=False):
    from core.database import McpServer
    s = db()
    s.add(McpServer(id="catalog_ghidra", name="Ghidra", transport="stdio", command="python",
                    args=json.dumps(["x.py"]), env="{}", is_enabled=enabled))
    s.commit()
    s.close()


def _enabled(db):
    from core.database import McpServer
    s = db()
    try:
        return s.query(McpServer).filter(McpServer.id == "catalog_ghidra").one().is_enabled
    finally:
        s.close()


def test_reconnect_success_enables_server(db):
    _add(db, enabled=False)
    r = _client(FakeMgr(ok=True)).post("/api/mcp/servers/catalog_ghidra/reconnect")
    assert r.status_code == 200 and r.json()["connected"] and r.json()["is_enabled"]
    assert _enabled(db) is True


def test_failed_reconnect_does_not_enable(db):
    _add(db, enabled=False)
    r = _client(FakeMgr(ok=False)).post("/api/mcp/servers/catalog_ghidra/reconnect")
    assert r.status_code == 200 and not r.json()["connected"]
    assert _enabled(db) is False


def test_startup_retries_failed_servers(monkeypatch):
    from src.mcp_manager import McpManager
    m = McpManager()
    attempts = {"n": 0}

    async def flaky(**kw):
        attempts["n"] += 1
        if attempts["n"] >= 2:
            m._connected_ids.add(kw["server_id"])
            return True
        return False
    monkeypatch.setattr(m, "connect_server", flaky)
    asyncio.run(m._retry_failed_on_startup([{"server_id": "s1", "name": "lento"}], delays=(0, 0)))
    assert attempts["n"] == 2 and "s1" in m._connected_ids   # falló una vez y conectó al reintentar
