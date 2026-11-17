"""RUFFS — escritorio.

Arranca el servidor de RUFFS y lo muestra en una ventana propia (pywebview:
WebView2 en Windows, WKWebView en macOS), sin abrir pestañas del navegador.
Al cerrar la ventana RUFFS sigue encendido (para el móvil); Enter en la consola
o volver a abrir el acceso directo reabre la ventana, y Ctrl+C o cerrar la
consola lo apaga (con --stop-on-close se apaga al cerrar la ventana).

    python ruffs_desktop.py                 # servidor + ventana
    python ruffs_desktop.py --no-window     # solo servidor (como uvicorn)
    python ruffs_desktop.py --attach        # solo la ventana (RUFFS ya en marcha)
    python ruffs_desktop.py --smoke 8       # abre la ventana 8 s y sale (pruebas)

Puerto y dirección: --port/--host, o APP_PORT/APP_BIND en .env (7100 /
127.0.0.1 por defecto). Si pywebview no está instalado, deja el servidor en
marcha y muestra la dirección para abrirla en el navegador.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def _env_defaults() -> tuple[str, int]:
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(HERE, ".env"), encoding="utf-8-sig")
    except Exception:
        pass
    host = os.environ.get("APP_BIND") or "127.0.0.1"
    try:
        port = int(os.environ.get("APP_PORT") or 7100)
    except ValueError:
        port = 7100
    return host, port


def _wait_ready(url: str, server_thread: threading.Thread, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not server_thread.is_alive():
            return False
        try:
            with urllib.request.urlopen(url + "/login", timeout=2) as r:
                if r.status < 500:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _webview_storage() -> str:
    """Where the window keeps cookies (so the login survives restarts)."""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/RUFFS")
    elif os.name == "nt":
        base = os.path.join(os.environ.get("LOCALAPPDATA") or HERE, "RUFFS")
    else:
        base = os.path.expanduser("~/.local/share/ruffs")
    path = os.path.join(base, "webview")
    os.makedirs(path, exist_ok=True)
    return path


def _open_window(url: str, smoke: float = 0) -> None:
    """Show RUFFS in a native window; returns when the window is closed."""
    import webview  # pywebview

    # Window icon: Windows needs an .ico; macOS takes the app bundle's icon.
    icon = os.path.join(HERE, "ruffs.ico") if os.name == "nt" else ""
    window = webview.create_window(
        "RUFFS", url, width=1320, height=860, min_size=(900, 600),
        background_color="#06071a", text_select=True,
    )
    if smoke:
        threading.Timer(smoke, window.destroy).start()
    start_kwargs = dict(private_mode=False, storage_path=_webview_storage())
    if icon and os.path.exists(icon):
        start_kwargs["icon"] = icon
    try:
        webview.start(**start_kwargs)
    except TypeError:  # older pywebview without `icon`
        start_kwargs.pop("icon", None)
        webview.start(**start_kwargs)


def _window_process(url_port: int, smoke: float = 0) -> int:
    """Run the window in its own process (the GUI loop can only start once
    per process, so each reopen is a fresh one)."""
    import subprocess
    cmd = [sys.executable, os.path.abspath(__file__), "--attach", "--port", str(url_port)]
    if smoke:
        cmd += ["--smoke", str(smoke)]
    global WINDOW_PROC
    WINDOW_PROC = subprocess.Popen(cmd, cwd=HERE)
    try:
        return WINDOW_PROC.wait()
    finally:
        WINDOW_PROC = None


# Proceso de la ventana abierta (lo cierra, p. ej., la instalación de una
# actualización antes de cerrar RUFFS).
WINDOW_PROC = None


def close_window() -> None:
    proc = WINDOW_PROC
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass


def main() -> int:
    host_d, port_d = _env_defaults()
    ap = argparse.ArgumentParser(description="RUFFS en una ventana de escritorio")
    ap.add_argument("--host", default=host_d)
    ap.add_argument("--port", type=int, default=port_d)
    ap.add_argument("--no-window", action="store_true", help="solo el servidor")
    ap.add_argument("--attach", action="store_true",
                    help="solo la ventana, para un RUFFS que ya está en marcha")
    ap.add_argument("--stop-on-close", action="store_true",
                    help="apagar RUFFS al cerrar la ventana")
    ap.add_argument("--smoke", type=float, default=0, help="cerrar la ventana tras N segundos (pruebas)")
    args = ap.parse_args()

    os.chdir(HERE)
    if HERE not in sys.path:
        sys.path.insert(0, HERE)

    # Puerto real para las llamadas internas de las herramientas (core.constants).
    os.environ["RUFFS_SERVER_PORT"] = str(args.port)
    view_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{view_host}:{args.port}"

    if args.attach:
        try:
            _open_window(url, args.smoke)
        except ImportError:
            print(f"[RUFFS] pywebview no está instalado: abre {url} en tu navegador.", flush=True)
            return 1
        return 0

    import uvicorn

    config = uvicorn.Config("app:app", host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)

    if args.no_window:
        server.run()
        return 0

    try:
        import webview  # noqa: F401  (pywebview)
    except ImportError:
        print(f"[RUFFS] pywebview no está instalado: abre {url} en tu navegador.", flush=True)
        server.run()
        return 0

    t = threading.Thread(target=server.run, name="ruffs-server", daemon=True)
    t.start()
    print(f"[RUFFS] Iniciando en {url} …", flush=True)
    # First start can take a while (embedding model download, DB setup).
    if not _wait_ready(url, t, timeout=600):
        print("[RUFFS] El servidor no arrancó. Revisa los mensajes de arriba.", flush=True)
        server.should_exit = True
        return 1

    stop_on_close = args.stop_on_close or bool(args.smoke)
    try:
        while True:
            print("[RUFFS] Listo. Abriendo la ventana de RUFFS…", flush=True)
            _window_process(args.port, args.smoke)
            if stop_on_close or not t.is_alive():
                break
            print(
                "",
                "[RUFFS] Ventana cerrada; RUFFS sigue encendido (también desde el móvil por Tailscale).",
                "        · Pulsa Enter aquí, o vuelve a abrir el acceso directo RUFFS, para reabrir la ventana.",
                f"        · O entra en {url} desde el navegador.",
                "        · Cierra esta consola o pulsa Ctrl+C para apagar RUFFS.",
                "",
                sep="\n", flush=True,
            )
            try:
                input()
            except EOFError:  # no console to read from: just keep serving
                while t.is_alive():
                    t.join(1)
                break
            if not t.is_alive():
                break
    except KeyboardInterrupt:
        pass

    print("[RUFFS] Deteniendo el servidor…", flush=True)
    server.should_exit = True
    t.join(timeout=15)
    return 0


if __name__ == "__main__":
    sys.exit(main())
