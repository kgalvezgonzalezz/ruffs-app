"""Importa a RUFFS los servidores MCP registrados en Odysseus.

En Odysseus los servidores se daban de alta con ``register_mcp_servers.py``,
que escribía directamente en SQLite (ids numéricos, rutas fijas a C:\\Odysseus)
y, además, ``builtin_mcp.py`` volvía a arrancar muchos de ellos como
"integrados". Resultado: duplicados, paquetes npm inexistentes que se
intentaban descargar en cada arranque y servidores que nunca conectaban.

Este script lee la tabla ``mcp_servers`` de Odysseus en modo solo lectura y
registra en RUFFS, una sola vez, los que siguen siendo válidos.

Uso (desde C:\\RUFFS):
    .venv\\Scripts\\python.exe scripts\\importar_mcp_odysseus.py            # solo muestra el plan
    .venv\\Scripts\\python.exe scripts\\importar_mcp_odysseus.py --aplicar  # registra en RUFFS

No imprime nunca los valores de las variables de entorno (pueden ser tokens).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# Paquetes npm que no existen o que sus autores archivaron.
NPM_INVALIDOS = {
    "@modelcontextprotocol/server-fetch": "no existe en npm (fetch es el paquete de Python mcp-server-fetch)",
    "@modelcontextprotocol/server-time": "no existe en npm (time es el paquete de Python mcp-server-time)",
    "@modelcontextprotocol/server-think": "no existe",
    "@modelcontextprotocol/server-prompt": "no existe",
    "@modelcontextprotocol/server-puppeteer": "archivado por Anthropic (usa el navegador integrado de RUFFS)",
    "@modelcontextprotocol/server-gdrive": "archivado por Anthropic",
    "@modelcontextprotocol/server-google-drive": "no existe",
    "@modelcontextprotocol/server-brave-search": "archivado por Anthropic",
    "@modelcontextprotocol/server-everything": "servidor de pruebas: solo añade herramientas de ejemplo al contexto",
}


def _comando_existe(cmd: str) -> bool:
    if not cmd:
        return False
    if os.path.dirname(cmd):
        return os.path.exists(cmd)
    return shutil.which(cmd) is not None


def _revisar(row: dict) -> tuple[str, str]:
    """Devuelve (decisión, motivo): 'importar' u 'omitir'."""
    if not row["is_enabled"]:
        return "omitir", "desactivado en Odysseus"
    transport = (row["transport"] or "stdio").lower()
    if transport in ("sse", "http", "streamable_http"):
        return ("importar", "remoto") if row["url"] else ("omitir", "sin URL")
    try:
        args = json.loads(row["args"] or "[]")
    except ValueError:
        return "omitir", "args no es JSON válido"
    for a in args:
        base = str(a).split("@latest")[0]
        if base in NPM_INVALIDOS:
            return "omitir", NPM_INVALIDOS[base]
    if not _comando_existe(row["command"] or ""):
        return "omitir", f"no se encuentra el comando {row['command']!r}"
    for a in args:
        a = str(a)
        if a.lower().endswith(".py") and os.path.isabs(a) and not os.path.exists(a):
            return "omitir", f"no existe el script {a}"
    if "odysseus\\python" in (row["command"] or "").lower():
        return "importar", "usa el Python de Odysseus (sus dependencias están allí)"
    return "importar", "ok"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--origen", default=r"C:\Odysseus\data\app.db")
    p.add_argument("--aplicar", action="store_true", help="registrar de verdad en RUFFS")
    a = p.parse_args()

    if not os.path.exists(a.origen):
        print(f"No se encuentra la base de datos de Odysseus: {a.origen}")
        return 1
    con = sqlite3.connect(f"file:{a.origen}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "select id, name, transport, command, args, env, url, is_enabled, disabled_tools from mcp_servers")]
    con.close()

    plan, vistos = [], set()
    for r in rows:
        decision, motivo = _revisar(r)
        firma = (r["transport"], r["command"], r["args"], r["url"])
        if decision == "importar" and firma in vistos:
            decision, motivo = "omitir", "duplicado"
        vistos.add(firma)
        plan.append((r, decision, motivo))

    print(f"{'Servidor':<22} {'Decisión':<9} Motivo")
    print("-" * 90)
    for r, decision, motivo in plan:
        env_keys = list(json.loads(r["env"] or "{}").keys()) if r["env"] else []
        extra = f"  [env: {', '.join(env_keys)}]" if env_keys else ""
        print(f"{r['name']:<22} {decision:<9} {motivo}{extra}")

    a_importar = [r for r, d, _ in plan if d == "importar"]
    print(f"\n{len(a_importar)} para importar, {len(plan) - len(a_importar)} omitidos.")
    if not a_importar:
        return 0
    if not a.aplicar:
        print("\nModo de prueba: no se ha cambiado nada. Repite con --aplicar para registrarlos en RUFFS.")
        return 0

    from core.database import Base, McpServer, SessionLocal, engine
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    creados = 0
    try:
        existentes = {s.name for s in db.query(McpServer).all()}
        for r in a_importar:
            if r["name"] in existentes:
                print(f"  ya existe en RUFFS, se deja como está: {r['name']}")
                continue
            transport = (r["transport"] or "stdio").lower()
            if transport == "streamable_http":
                transport = "http"
            db.add(McpServer(
                id=str(uuid.uuid4())[:8], name=r["name"], transport=transport,
                command=r["command"], args=r["args"] or "[]", env=r["env"] or "{}",
                url=r["url"], is_enabled=True, disabled_tools=r["disabled_tools"],
            ))
            creados += 1
        db.commit()
    finally:
        db.close()
    print(f"\nRegistrados en RUFFS: {creados}. Se conectarán la próxima vez que arranques RUFFS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
