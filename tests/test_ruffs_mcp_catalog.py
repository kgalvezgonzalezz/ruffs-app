"""RUFFS: catálogo de MCP portátiles (src/mcp_catalog.py) y MCP de navegador."""
import json
import os

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as database
    import src.mcp_catalog as cat

    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    database.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", Session)
    monkeypatch.setattr(cat, "_MARKER", str(tmp_path / "data" / ".mcp_catalog_v1"))
    monkeypatch.delenv("RUFFS_MCP_CATALOG", raising=False)
    return Session


def test_seeds_disabled_entries_once(db):
    from core.database import McpServer
    from src.mcp_catalog import catalog, seed_mcp_catalog

    assert seed_mcp_catalog() == len(catalog())
    s = db()
    rows = s.query(McpServer).all()
    assert len(rows) == len(catalog())
    assert all(r.is_enabled is False for r in rows)          # nada arranca solo
    fs = next(r for r in rows if r.id == "catalog_filesystem")
    assert json.loads(fs.args)[-1] == os.path.expanduser("~")  # ruta del equipo de destino
    s.close()
    assert seed_mcp_catalog() == 0                             # solo una vez


def test_does_not_duplicate_user_servers(db):
    from core.database import McpServer
    from src.mcp_catalog import catalog, seed_mcp_catalog

    s = db()
    s.add(McpServer(id="mio", name="filesystem", transport="stdio", command="npx",
                    args=json.dumps(["-y", "@modelcontextprotocol/server-filesystem", "C:/x"]),
                    is_enabled=True))
    s.commit(); s.close()
    assert seed_mcp_catalog() == len(catalog()) - 1
    s = db()
    assert s.query(McpServer).filter(McpServer.id == "catalog_filesystem").first() is None
    assert s.query(McpServer).filter(McpServer.id == "mio").one().is_enabled is True
    s.close()


def test_catalog_can_be_disabled(db, monkeypatch):
    from src.mcp_catalog import seed_mcp_catalog
    monkeypatch.setenv("RUFFS_MCP_CATALOG", "false")
    assert seed_mcp_catalog() == 0


def test_browser_mcp_is_pinned_and_uses_a_present_browser(monkeypatch):
    import importlib
    import src.builtin_mcp as b

    monkeypatch.delenv("RUFFS_MCP_BROWSER", raising=False)
    monkeypatch.delenv("RUFFS_PLAYWRIGHT_MCP", raising=False)
    importlib.reload(b)
    args = b._browser_mcp_args()
    assert "@playwright/mcp@0.0.85" in args and "@playwright/mcp@latest" not in args
    if b.IS_WINDOWS:
        assert args[-2:] == ["--browser", "msedge"]
    monkeypatch.setenv("RUFFS_MCP_BROWSER", "firefox")
    assert b._browser_mcp_args()[-2:] == ["--browser", "firefox"]


def test_old_installs_receive_only_the_new_entries(db, tmp_path):
    """Una instalación con la marca antigua (v1, texto) recibe solo las nuevas."""
    import src.mcp_catalog as cat
    from core.database import McpServer

    os.makedirs(os.path.dirname(cat._MARKER), exist_ok=True)
    with open(cat._MARKER, "w", encoding="utf-8") as f:
        f.write("catálogo de MCP sembrado; bórralo para volver a sembrar\n")
    added = cat.seed_mcp_catalog()
    new_keys = [e["key"] for e in cat.catalog() if e["key"] not in cat._V1_KEYS]
    assert added == len(new_keys) and {"ghidra", "rea", "agent_desktop", "hyperframes"} <= set(new_keys)
    s = db()
    ids = {r.id for r in s.query(McpServer).all()}
    assert ids == {f"catalog_{k}" for k in new_keys}
    s.close()


def test_deleted_entry_does_not_come_back(db):
    from core.database import McpServer
    from src.mcp_catalog import seed_mcp_catalog

    seed_mcp_catalog()
    s = db()
    s.query(McpServer).filter(McpServer.id == "catalog_rea").delete()
    s.commit(); s.close()
    assert seed_mcp_catalog() == 0
    s = db()
    assert s.query(McpServer).filter(McpServer.id == "catalog_rea").first() is None
    s.close()


def test_ghidra_entry_resolves_to_this_install():
    """{ruffs_python}/{ruffs_dir} → el Python y la carpeta de esta instalación."""
    import sys
    from src.mcp_catalog import catalog
    from src.mcp_manager import _expand_placeholders

    e = next(x for x in catalog() if x["key"] == "ghidra")
    command, args = _expand_placeholders(e["command"], e["args"])
    assert command == sys.executable
    assert os.path.isfile(args[0]) and args[0].endswith("ghidra_server.py")
    assert os.path.isfile(os.path.join(os.path.dirname(args[0]), "ghidra_scripts", "RuffsExport.java"))
    assert _expand_placeholders("npx", ["-y", "x"]) == ("npx", ["-y", "x"])


def test_npx_catalog_entries_are_pinned():
    from src.mcp_catalog import catalog

    for e in catalog():
        if e["key"] in ("rea", "agent_desktop", "hyperframes"):
            pkg = e["args"][1]
            assert e["command"] == "npx" and "@" in pkg[1:], pkg   # versión fija
