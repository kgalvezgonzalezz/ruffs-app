"""RUFFS — catálogo de servidores MCP que funcionan en cualquier equipo.

Se añaden a Ajustes → MCP **desactivados**: aparecen listos para activarlos
con un clic, pero no arrancan (ni descargan nada) hasta que el usuario los
activa. Usan solo lo que instala RUFFS:

  * ``npx``  → Node.js portátil de la instalación (tools/node).
  * ``uvx``  → el uv incluido (tools/uvx), que trae su propio Python.
  * ``{ruffs_python}`` / ``{ruffs_dir}`` → el Python y la carpeta de esta
    instalación; McpManager los sustituye al conectar, así que siguen
    valiendo aunque RUFFS se instale (o se mueva) a cualquier carpeta.

Se siembra una sola vez por instalación (marca en data/). Nunca toca un
servidor que el usuario ya tenga (mismo nombre o mismo paquete), y las rutas
(p. ej. la carpeta personal para Filesystem) se calculan en el equipo de
destino, así que el instalador no lleva datos de nadie.

Desactivar: RUFFS_MCP_CATALOG=false en .env.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

_MARKER = os.path.join("data", ".mcp_catalog_v1")
# Claves que sembraba la primera versión del catálogo (marca en texto plano).
_V1_KEYS = ["filesystem", "fetch", "time", "git", "sequential_thinking", "notion"]


def _home() -> str:
    return os.path.expanduser("~")


def catalog() -> list[dict]:
    """Entradas del catálogo. ``package`` identifica el servidor para no
    duplicar uno que el usuario ya haya añadido por su cuenta."""
    return [
        {
            "key": "filesystem",
            "name": "Filesystem (archivos de tu carpeta personal)",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", _home()],
            "package": "@modelcontextprotocol/server-filesystem",
        },
        {
            "key": "fetch",
            "name": "Fetch (leer páginas web como texto)",
            "command": "uvx",
            "args": ["mcp-server-fetch"],
            "package": "mcp-server-fetch",
        },
        {
            "key": "time",
            "name": "Time (fecha, hora y zonas horarias)",
            "command": "uvx",
            "args": ["mcp-server-time"],
            "package": "mcp-server-time",
        },
        {
            "key": "git",
            "name": "Git (historial y cambios de repositorios)",
            "command": "uvx",
            "args": ["mcp-server-git"],
            "package": "mcp-server-git",
        },
        {
            "key": "sequential_thinking",
            "name": "Sequential Thinking (razonamiento paso a paso)",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
            "package": "@modelcontextprotocol/server-sequential-thinking",
        },
        {
            "key": "notion",
            "name": "Notion (necesita tu token de integración)",
            "command": "npx",
            "args": ["-y", "@notionhq/notion-mcp-server"],
            "env": {"NOTION_TOKEN": ""},
            "package": "@notionhq/notion-mcp-server",
        },
        {
            "key": "ghidra",
            "name": "Ghidra (ingeniería inversa: decompilar y analizar binarios)",
            "command": "{ruffs_python}",
            "args": ["{ruffs_dir}/mcp_servers/ghidra_server.py"],
            # Con env propio el servidor hereda todo el entorno (GHIDRA_HOME,
            # JAVA_HOME, ProgramFiles…) y encuentra un Ghidra ya instalado.
            "env": {"PYTHONIOENCODING": "utf-8"},
            "package": "ghidra_server.py",
        },
        {
            "key": "computer_use",
            "name": "Computer Use (RUFFS ve y maneja tu escritorio)",
            "command": "{ruffs_python}",
            "args": ["{ruffs_dir}/mcp_servers/computer_use_server.py"],
            "env": {"PYTHONIOENCODING": "utf-8"},
            "package": "computer_use_server.py",
        },
        {
            # RUFFS: buscar empleo y postular con CV ajustado (Computrabajo,
            # GetOnBoard, LinkedIn, Indeed). Para postular directo en
            # Computrabajo, añade CT_COOKIES (tu sesión) en el env del servidor.
            "key": "empleos",
            "name": "Empleos (buscar ofertas y postular con CV ajustado)",
            "command": "{ruffs_python}",
            "args": ["{ruffs_dir}/mcp_servers/empleos_server.py"],
            "env": {"PYTHONIOENCODING": "utf-8"},
            "package": "empleos_server.py",
        },
        {
            "key": "rea",
            "name": "REA (ingeniería inversa de apps, binarios y webs)",
            "command": "npx",
            "args": ["-y", "rea-agents@6.9.0", "mcp"],
            "package": "rea-agents",
        },
        {
            "key": "agent_desktop",
            "name": "Agent Desktop (manejar apps del escritorio)",
            "command": "npx",
            "args": ["-y", "agent-desktop@1.3.1", "mcp"],
            "package": "agent-desktop",
        },
        {
            "key": "hyperframes",
            "name": "HyperFrames (vídeos con HeyGen; pide iniciar sesión)",
            "command": "npx",
            "args": ["-y", "mcp-remote@0.16.0", "https://mcp.heygen.com/mcp/hyperframes/"],
            "package": "mcp.heygen.com/mcp/hyperframes",
        },
    ]


def _enabled() -> bool:
    return (os.environ.get("RUFFS_MCP_CATALOG", "true").strip().lower()
            not in ("0", "false", "no", "off"))


def _seeded_keys() -> set[str] | None:
    """Claves ya sembradas en esta instalación (None si nunca se sembró)."""
    if not os.path.exists(_MARKER):
        return None
    try:
        with open(_MARKER, encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seeded") or [])
    except (OSError, ValueError, AttributeError):
        return set(_V1_KEYS)  # marca antigua (texto): solo las seis primeras


def seed_mcp_catalog(force: bool = False) -> int:
    """Añade, desactivadas, las entradas que esta instalación aún no recibió.

    Cada entrada se siembra una sola vez: si el usuario la borra, no vuelve.
    Las entradas nuevas de una actualización sí llegan a instalaciones viejas.
    Devuelve cuántas añadió."""
    if not _enabled():
        return 0
    seeded = None if force else _seeded_keys()
    pending = [e for e in catalog() if seeded is None or e["key"] not in seeded]
    if not pending:
        return 0
    from core.database import McpServer, SessionLocal

    db = SessionLocal()
    added = 0
    try:
        existing = db.query(McpServer).all()
        names = {(s.name or "").strip().lower() for s in existing}
        ids = {s.id for s in existing}
        blob = " ".join(f"{s.command or ''} {s.args or ''} {s.url or ''}" for s in existing).lower()
        for entry in pending:
            sid = f"catalog_{entry['key']}"
            if (sid in ids or entry["name"].lower() in names
                    or entry["package"].lower() in blob):
                continue
            db.add(McpServer(
                id=sid,
                name=entry["name"],
                transport="stdio",
                command=entry["command"],
                args=json.dumps(entry["args"]),
                env=json.dumps(entry.get("env") or {}),
                is_enabled=False,
            ))
            added += 1
        db.commit()
    finally:
        db.close()
    try:
        os.makedirs(os.path.dirname(_MARKER), exist_ok=True)
        done = sorted((seeded or set()) | {e["key"] for e in pending})
        with open(_MARKER, "w", encoding="utf-8") as f:
            json.dump({"seeded": done,
                       "nota": "catálogo de MCP sembrado; bórralo para volver a sembrarlo todo"},
                      f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    if added:
        logger.info(f"RUFFS: {added} servidores MCP del catálogo añadidos (desactivados)")
    return added
