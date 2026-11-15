"""Comprueba de verdad los MCP del catálogo Ghidra, REA, Agent Desktop y HyperFrames.

Se ejecuta con el Python de una instalación de RUFFS, desde su carpeta:

    cd <carpeta de la app> && .venv/bin/python <ruta>/ci_mcp_extra.py [--install-ghidra] [--binary RUTA]

Usa las mismas entradas que siembra src/mcp_catalog.py (con los marcadores
{ruffs_python}/{ruffs_dir} resueltos como lo hace McpManager), así que prueba
exactamente lo que el usuario activa en Ajustes → MCP.

--install-ghidra: instala Ghidra + JDK con la herramienta ghidra_install (en
una carpeta temporal; solo la copia de RUFFS, sin usar un Ghidra del sistema)
y luego analiza y decompila --binary.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.getcwd())

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from src.mcp_catalog import catalog  # noqa: E402
from src.mcp_manager import _expand_placeholders  # noqa: E402

FAILURES: list[str] = []


def entry(key: str) -> dict:
    return next(e for e in catalog() if e["key"] == key)


def params(key: str, extra_env: dict | None = None) -> StdioServerParameters:
    e = entry(key)
    command, args = _expand_placeholders(e["command"], e["args"])
    command = shutil.which(command) or command
    env = {**os.environ, **(e.get("env") or {}), **(extra_env or {})}
    return StdioServerParameters(command=command, args=args, env=env)


async def list_tools(key: str, timeout: float = 300) -> list[str]:
    async def run():
        async with stdio_client(params(key)) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return [t.name for t in (await s.list_tools()).tools]
    return await asyncio.wait_for(run(), timeout)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK' if ok else 'FALLO'}] {name} {detail}".rstrip(), flush=True)
    if not ok:
        FAILURES.append(name)


async def ghidra(install: bool, binary: str | None) -> None:
    extra = {}
    if install:
        extra = {"RUFFS_GHIDRA_DIR": tempfile.mkdtemp(prefix="ruffs-ghidra-"), "RUFFS_GHIDRA_ONLY_LOCAL": "1"}
    async with stdio_client(params("ghidra", extra)) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            check("ghidra: herramientas", {"analyze", "decompile", "ghidra_install"} <= set(tools), str(len(tools)))

            async def call(name: str, **a) -> dict:
                res = await s.call_tool(name, a)
                text = res.content[0].text
                try:
                    return json.loads(text)
                except ValueError:
                    return {"raw": text}

            if binary:
                info = await call("binary_info", filepath=binary)
                check("ghidra: binary_info sin Ghidra", "format" in info, info.get("format", ""))
            if not install:
                st = await call("ghidra_status")
                print("   estado:", {k: st.get(k) for k in ("ready", "ghidra_version", "java_version")})
                return
            st = await call("ghidra_install")
            print("   ghidra_install:", st.get("state"), st.get("detail", ""), flush=True)
            deadline = time.time() + 1800
            last = ""
            while time.time() < deadline:
                st = await call("ghidra_status")
                inst = st.get("install") or {}
                if inst.get("detail") != last:
                    last = inst.get("detail", "")
                    print("   …", last, flush=True)
                if inst.get("state") in ("done", "error"):
                    break
                await asyncio.sleep(10)
            check("ghidra: instalación automática (Ghidra + JDK)", bool(st.get("ready")),
                  f"Ghidra {st.get('ghidra_version')} / Java {st.get('java_version')} (pide {st.get('java_needed')})")
            check("ghidra: decompilador nativo para esta plataforma", bool(st.get("decompiler")),
                  str((st.get("install") or {}).get("detail", ""))[:300])
            if not (st.get("ready") and binary):
                return
            t = time.time()
            summary = await call("analyze", filepath=binary)
            check("ghidra: analyze", "function_count" in summary,
                  f"{summary.get('format')} {summary.get('language')} funciones={summary.get('function_count')} ({time.time() - t:.0f}s)")
            if "function_count" not in summary:
                print(json.dumps(summary, ensure_ascii=False)[:3000])
                return
            fl = await call("list_functions", filepath=binary, limit=500)
            funcs = [f for f in fl.get("functions", []) if not f.get("thunk")]
            names = [f["name"] for f in funcs]
            print("   funciones:", names[:12])
            target = next((n for n in ("main", "_main", "suma", "_suma", "entry") if n in names),
                          names[0] if names else "entry")
            dec = await call("decompile", filepath=binary, function=target)
            check("ghidra: decompile", bool(dec.get("c")), f"{target}: {len((dec.get('c') or ''))} caracteres de C")
            if not dec.get("c"):
                print(json.dumps(dec, ensure_ascii=False)[:2000])
            imp = await call("list_imports", filepath=binary, limit=5)
            check("ghidra: list_imports", "imports" in imp, str(imp.get("total")))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install-ghidra", action="store_true")
    ap.add_argument("--binary")
    ap.add_argument("--skip", default="", help="claves a saltar, separadas por comas")
    a = ap.parse_args()
    skip = set(filter(None, a.skip.split(",")))

    if "ghidra" not in skip:
        try:
            await ghidra(a.install_ghidra, a.binary)
        except Exception as e:  # noqa: BLE001
            check("ghidra", False, repr(e))

    for key, expect in (("rea", 20), ("agent_desktop", 10)):
        if key in skip:
            continue
        try:
            t = time.time()
            tools = await list_tools(key)
            check(f"{key}: conecta y lista herramientas", len(tools) >= expect,
                  f"{len(tools)} herramientas ({time.time() - t:.0f}s)")
        except Exception as e:  # noqa: BLE001
            check(f"{key}: conecta y lista herramientas", False, repr(e))

    if "hyperframes" not in skip:
        # Necesita iniciar sesión en HeyGen (OAuth en el navegador): aquí solo se
        # comprueba que el proxy está y que el servidor remoto responde.
        e = entry("hyperframes")
        url = e["args"][-1]
        try:
            urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=20)
            code = 200
        except urllib.error.HTTPError as err:
            code = err.code
        except Exception as err:  # noqa: BLE001
            code = repr(err)
        check("hyperframes: servidor remoto de HeyGen responde", isinstance(code, int) and code < 500, f"HTTP {code}")
        npx = shutil.which("npx")
        ok = False
        if npx:
            proc = await asyncio.create_subprocess_exec(
                npx, "-y", "-p", e["args"][1], "node", "-e", "0",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            ok = (await asyncio.wait_for(proc.wait(), 300)) == 0
        check("hyperframes: proxy mcp-remote disponible", ok, e["args"][1])

    print("\nResultado:", "todo bien" if not FAILURES else f"fallos: {FAILURES}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
