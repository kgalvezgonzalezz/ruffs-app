"""Prueba real del MCP Computer Use con una ventana de prueba propia (no toca otras apps).

    cd <carpeta de la app> && <python> <ruta>/ci_computer_use.py [--no-elements]

Abre una ventanita Tk con un campo de texto y un botón «Enviar», y comprueba vía
MCP: captura, zoom, elementos numerados, clic, escritura con tildes, teclas,
la confirmación antes de pulsar «Enviar» (modo chat, con token) y la parada de
emergencia (ratón a la esquina).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.getcwd())

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from src.mcp_catalog import catalog  # noqa: E402
from src.mcp_manager import _expand_placeholders  # noqa: E402

FAIL: list[str] = []
APP_STATE = {"sent": 0, "text": ""}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK' if ok else 'FALLO'}] {name} {detail}".rstrip(), flush=True)
    if not ok:
        FAIL.append(name)


def run_window(ready: threading.Event, geom: dict) -> None:
    import tkinter as tk
    root = tk.Tk()
    root.title("Ventana de prueba RUFFS")
    root.geometry("520x220+160+160")
    root.attributes("-topmost", True)
    var = tk.StringVar()
    tk.Label(root, text="Prueba de control del equipo", font=("Arial", 16)).pack(pady=8)
    entry = tk.Entry(root, textvariable=var, font=("Arial", 16), width=28)
    entry.pack(pady=6)

    def send():
        APP_STATE["sent"] += 1
    btn = tk.Button(root, text="Enviar", font=("Arial", 16), command=send)
    btn.pack(pady=6)

    def poll():
        APP_STATE["text"] = var.get()
        root.after(100, poll)

    def report():
        root.update_idletasks()
        geom["entry"] = (entry.winfo_rootx() + entry.winfo_width() // 2, entry.winfo_rooty() + entry.winfo_height() // 2)
        geom["button"] = (btn.winfo_rootx() + btn.winfo_width() // 2, btn.winfo_rooty() + btn.winfo_height() // 2)
        root.lift()
        root.focus_force()
        ready.set()
    root.after(800, report)
    root.after(100, poll)
    geom["root"] = root
    root.mainloop()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-elements", action="store_true", help="no probar mark_elements (sin Agent Desktop)")
    a = ap.parse_args()

    data = tempfile.mkdtemp(prefix="ruffs-cu-")
    with open(os.path.join(data, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"confirmacion": "chat", "parada_con_esc_triple": False}, f)

    ready, geom = threading.Event(), {}
    threading.Thread(target=run_window, args=(ready, geom), daemon=True).start()
    ready.wait(15)
    from pynput.mouse import Controller as _M
    _M().position = geom["entry"]   # lejos de la esquina de parada

    e = next(x for x in catalog() if x["key"] == "computer_use")
    command, args = _expand_placeholders(e["command"], e["args"])
    params = StdioServerParameters(command=shutil.which(command) or command, args=args,
                                   env={**os.environ, **(e.get("env") or {}), "RUFFS_COMPUTER_USE_DIR": data})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            check("herramientas", {"screenshot", "click", "type_text", "mark_elements", "zoom"} <= set(tools), str(len(tools)))

            async def call(name, **kw):
                res = await s.call_tool(name, kw)
                imgs = [c for c in res.content if getattr(c, "type", "") == "image"]
                text = " ".join(getattr(c, "text", "") for c in res.content if getattr(c, "type", "") == "text")
                return imgs, text

            st = json.loads((await call("status"))[1])
            print("   estado:", {k: st.get(k) for k in ("monitores", "app_activa", "permisos_macos")})
            imgs, text = await call("screenshot")
            check("screenshot devuelve imagen", len(imgs) == 1 and imgs[0].mimeType == "image/jpeg", text.splitlines()[-1] if text else "")
            mons = json.loads((await call("list_screens"))[1])
            cur = next(m for m in mons if m["current"])
            # Coordenadas de captura de un punto real de la pantalla
            import re
            m = re.search(r"(\d+)×(\d+) px", text)
            iw, ih = int(m.group(1)), int(m.group(2))

            def to_img(pt):
                return (round((pt[0] - cur["left"]) * iw / cur["width"]), round((pt[1] - cur["top"]) * ih / cur["height"]))
            if os.name != "nt" and sys.platform == "darwin":
                # mss en Mac da puntos; Tk también: coinciden.
                pass
            ex, ey = to_img(geom["entry"])
            bx, by = to_img(geom["button"])

            imgs, text = await call("zoom", x0=max(0, ex - 150), y0=max(0, ey - 60), x1=ex + 150, y1=by + 40)
            check("zoom devuelve imagen", len(imgs) == 1, text[:60])

            if not a.no_elements:
                # Tk apenas expone nombres de accesibilidad: basta con que lea la ventana
                # (en apps reales — navegadores, Office, Finder… — salen todos los nombres).
                imgs, text = await call("mark_elements")
                n = re.search(r"(\d+) elementos numerados", text)
                ok = len(imgs) == 1 and n and int(n.group(1)) >= 1
                check("mark_elements lee la ventana activa", bool(ok), text.splitlines()[0][:120] if text else "")
                print("   " + text[:900].replace(chr(10), chr(10) + "   "))

            imgs, text = await call("click", x=ex, y=ey)
            check("click en el campo", "Hecho" in text and len(imgs) == 1, text.splitlines()[0][:100])
            await call("type_text", text="Hola, ñandú: acción ✓", show="none")
            time.sleep(0.5)
            check("type_text con tildes", APP_STATE["text"] == "Hola, ñandú: acción ✓", repr(APP_STATE["text"]))
            await call("press_keys", keys="mod+a, backspace", show="none")
            time.sleep(0.4)
            check("press_keys (seleccionar todo y borrar)", APP_STATE["text"] == "", repr(APP_STATE["text"]))

            # Confirmación: regla "esta app pide permiso" sobre la ventana de prueba.
            cfg_path = os.path.join(data, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"confirmacion": "chat", "parada_con_esc_triple": False,
                           "apps_con_confirmacion": ["Ventana de prueba RUFFS"]}, f)
            imgs, text = await call("click", x=bx, y=by, show="none")
            m = re.search(r'confirm_token="([0-9a-f]+)"', text)
            time.sleep(0.3)
            check("pide confirmación antes de actuar", bool(m) and APP_STATE["sent"] == 0, text[:110])
            _, bad = await call("click", x=bx, y=by, show="none", confirm_token="000000")
            check("un token inventado no sirve", "NECESITA CONFIRMACIÓN" in bad and APP_STATE["sent"] == 0)
            if m:
                imgs, text = await call("click", x=bx, y=by, show="none", confirm_token=m.group(1))
                time.sleep(0.4)
                check("con el token del usuario sí se pulsa", APP_STATE["sent"] == 1, text[:80])
            # Bloqueo: la app bloqueada no se toca en absoluto.
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"confirmacion": "chat", "parada_con_esc_triple": False,
                           "apps_bloqueadas": ["Ventana de prueba RUFFS"]}, f)
            _, text = await call("click", x=bx, y=by, show="none")
            time.sleep(0.3)
            check("app bloqueada", "bloqueada" in text and APP_STATE["sent"] == 1, text[:90])
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"confirmacion": "chat", "parada_con_esc_triple": False}, f)

            # Parada de emergencia: el usuario lleva el ratón a la esquina superior izquierda.
            from pynput.mouse import Controller
            Controller().position = (ex + 40, ey + 40)
            await call("cursor_position")
            await call("wait", seconds=0.1, show="none")
            Controller().position = (min(x["left"] for x in mons), min(x["top"] for x in mons))
            time.sleep(0.3)
            _, text = await call("click", x=ex, y=ey, show="none")
            check("parada de emergencia (esquina)", "emergencia" in text.lower() or "pausa" in text.lower(), text[:80])
            _, text = await call("click", x=ex, y=ey, show="none")
            check("sigue en pausa", "PAUSA" in text, text[:60])
            log = (await call("action_log", last=10))[1]
            check("registro de acciones", "click" in log and "parada" in log)
    try:
        geom["root"].after(0, geom["root"].destroy)
    except Exception:  # noqa: BLE001
        pass
    print("\nResultado:", "todo bien" if not FAIL else f"fallos: {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
