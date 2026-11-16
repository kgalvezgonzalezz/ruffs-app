"""RUFFS — aviso e instalación de nuevas versiones (solo en copias instaladas).

GET  /api/ruffs/update           → ¿hay una versión más nueva en GitHub? (con notas)
POST /api/ruffs/update/install   → descarga el instalador de esa versión, comprueba su
                                   SHA-256, lo abre y cierra RUFFS para que se actualice.

De dónde sale cada dato:
  * Versión instalada: archivo VERSION de la instalación (no existe en una copia
    de desarrollo, y entonces no se avisa de nada).
  * Repositorio: RUFFS_UPDATE_REPO en .env, o el archivo RUFFS_UPDATE_REPO que
    escribe la compilación de los instaladores en GitHub (el repositorio público
    del que salieron). Sin repositorio, el aviso queda desactivado.
  * Desactivar: RUFFS_UPDATE_CHECK=false.

Al actualizar se conservan chats, memorias, documentos y ajustes (data/ y .env).
Instalar solo se permite al administrador y desde el propio equipo: el asistente
de instalación se abre en esa pantalla.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from core.middleware import require_admin
from src.update_mac import mac_install_script  # noqa: F401  (sin dependencias: lo usa la CI)

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_TTL = 6 * 3600
USER_AGENT = "RUFFS-update-check"
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

_install_lock = threading.Lock()
_install_state: Dict[str, Any] = {"state": "idle"}


def _read_file(name: str) -> str:
    try:
        with open(os.path.join(BASE_DIR, name), encoding="utf-8-sig") as f:
            return f.read().strip()
    except OSError:
        return ""


def installed_version() -> str:
    return os.environ.get("RUFFS_VERSION_OVERRIDE", "").strip() or _read_file("VERSION")


def update_repo() -> str:
    repo = os.environ.get("RUFFS_UPDATE_REPO", "").strip() or _read_file("RUFFS_UPDATE_REPO")
    return repo if re.fullmatch(r"[\w.-]+/[\w.-]+", repo or "") else ""


def enabled() -> bool:
    return os.environ.get("RUFFS_UPDATE_CHECK", "true").strip().lower() not in ("0", "false", "no", "off")


def parse_version(v: str) -> tuple:
    nums = re.findall(r"\d+", (v or "").lstrip("vV"))
    return tuple(int(n) for n in nums[:4]) if nums else ()


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def _cache_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "update_check.json")


def _fetch_latest(repo: str) -> Dict[str, Any]:
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        rel = json.loads(r.read().decode("utf-8"))
    assets = {}
    for a in rel.get("assets") or []:
        name = a.get("name", "")
        kind = "windows" if name.lower().endswith(".exe") else "mac" if name.lower().endswith(".dmg") else None
        if kind:
            digest = (a.get("digest") or "").split(":", 1)[-1] or None
            assets[kind] = {"name": name, "url": a.get("browser_download_url"), "size": a.get("size"),
                            "sha256": digest}
    return {"tag": rel.get("tag_name", ""), "name": rel.get("name", ""), "notes": rel.get("body") or "",
            "published_at": rel.get("published_at"), "html_url": rel.get("html_url"), "assets": assets,
            "checked_at": time.time()}


def latest_release(force: bool = False) -> Optional[Dict[str, Any]]:
    repo = update_repo()
    if not repo:
        return None
    cache = None
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = None
    if cache and cache.get("repo") == repo and not force and time.time() - cache.get("checked_at", 0) < CACHE_TTL:
        return cache
    try:
        rel = _fetch_latest(repo)
        rel["repo"] = repo
        os.makedirs(os.path.dirname(_cache_path()), exist_ok=True)
        with open(_cache_path(), "w", encoding="utf-8") as f:
            json.dump(rel, f, ensure_ascii=False)
        return rel
    except Exception as e:  # sin conexión o límite de GitHub: lo último que se supo
        logger.info(f"[update] no se pudo consultar GitHub: {e}")
        return cache if cache and cache.get("repo") == repo else None


def _platform_key() -> Optional[str]:
    return "windows" if IS_WINDOWS else "mac" if IS_MAC else None


def _is_loopback(request: Request) -> bool:
    import ipaddress
    host = (request.client.host if request.client else "") or ""
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return host in ("localhost", "testclient")


def _is_admin(request: Request) -> bool:
    try:
        require_admin(request)
        return True
    except HTTPException:
        return False


def update_info(request: Optional[Request] = None, force: bool = False) -> Dict[str, Any]:
    current = installed_version()
    info: Dict[str, Any] = {"enabled": enabled(), "installed": bool(current), "current": current or None,
                            "repo": update_repo() or None, "newer": False}
    if not (info["enabled"] and current and info["repo"]):
        return info
    rel = latest_release(force=force)
    if not rel:
        return info
    latest = rel.get("tag", "").lstrip("vV")
    asset = (rel.get("assets") or {}).get(_platform_key() or "")
    info.update({
        "latest": latest, "newer": is_newer(latest, current), "title": rel.get("name") or f"RUFFS {latest}",
        "notes": rel.get("notes", ""), "published_at": rel.get("published_at"), "release_url": rel.get("html_url"),
        "download_url": (asset or {}).get("url"), "asset": (asset or {}).get("name"),
    })
    local = bool(request is not None and _is_loopback(request))
    admin = bool(request is not None and _is_admin(request))
    info["can_install"] = bool(info["newer"] and asset and asset.get("url") and local and admin)
    if info["newer"] and not info["can_install"]:
        info["why_not"] = ("Solo el administrador puede instalarla." if not admin else
                           "Ábrelo en el equipo donde está instalado RUFFS para instalarla." if not local else
                           "No hay instalador para este sistema en esa versión." if not asset else None)
    info["install"] = dict(_install_state)
    return info


# ─── Instalación ─────────────────────────────────────────────────────────────

def _download(url: str, dest: str, sha256: Optional[str]) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            _install_state["detail"] = f"Descargando: {done >> 20} MB" + (f" de {total >> 20} MB" if total else "")
    if sha256 and h.hexdigest().lower() != sha256.lower():
        os.remove(dest)
        raise RuntimeError("El SHA-256 de la descarga no coincide con el publicado; se descartó.")


def mac_app_path() -> Optional[str]:
    """Dónde está RUFFS.app (para reemplazarla)."""
    candidates = []
    res = os.environ.get("RUFFS_BUNDLE_RES") or ""
    if not res:
        try:
            with open(os.path.expanduser("~/Library/Application Support/RUFFS/.bundle_resources"), encoding="utf-8") as f:
                res = f.read().strip()
        except OSError:
            res = ""
    if res:
        candidates.append(os.path.dirname(os.path.dirname(res.rstrip("/"))))
    candidates += ["/Applications/RUFFS.app", os.path.expanduser("~/Applications/RUFFS.app")]
    for c in candidates:
        if c.endswith(".app") and os.path.isdir(c):
            return c
    return None


def _exit_soon(delay: float = 2.5) -> None:
    def _bye():
        logger.info("[update] cerrando RUFFS para instalar la actualización")
        # La ventana va en su propio proceso (ruffs_desktop.py): cerrarla también.
        main = sys.modules.get("__main__")
        try:
            if main is not None and hasattr(main, "close_window"):
                main.close_window()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)
    threading.Timer(delay, _bye).start()


def _install_worker(info: Dict[str, Any]) -> None:
    st = _install_state
    try:
        rel = latest_release() or {}
        asset = (rel.get("assets") or {}).get(_platform_key() or "") or {}
        tmpdir = tempfile.mkdtemp(prefix="ruffs-update-")
        dest = os.path.join(tmpdir, asset.get("name") or ("RUFFS-Setup.exe" if IS_WINDOWS else "RUFFS.dmg"))
        st.update(state="downloading", detail="Descargando la nueva versión…")
        _download(asset["url"], dest, asset.get("sha256"))
        st.update(state="launching", detail="Abriendo el instalador; RUFFS se cerrará y se reabrirá al terminar.")
        if IS_WINDOWS:
            flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([dest], creationflags=flags, close_fds=True, cwd=tmpdir)
        else:
            script = os.path.join(tmpdir, "actualizar.sh")
            with open(script, "w", encoding="utf-8") as f:
                f.write(mac_install_script(dest, mac_app_path()))
            os.chmod(script, 0o755)
            subprocess.Popen(["/bin/bash", script], start_new_session=True, close_fds=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        st.update(state="closing")
        _exit_soon()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[update] la instalación falló: {e}")
        st.update(state="error", detail=f"No se pudo actualizar: {e}")


def setup_update_routes() -> APIRouter:
    router = APIRouter(tags=["update"])

    @router.get("/api/ruffs/update")
    async def check_update(request: Request, force: bool = False) -> Dict[str, Any]:
        import asyncio
        return await asyncio.to_thread(update_info, request, force)

    @router.post("/api/ruffs/update/install")
    async def install_update(request: Request, _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        import asyncio
        if not _is_loopback(request):
            raise HTTPException(403, "La actualización solo se puede instalar desde el propio equipo.")
        info = await asyncio.to_thread(update_info, request, False)
        if not info.get("newer"):
            raise HTTPException(409, "Ya tienes la última versión.")
        if not info.get("can_install"):
            raise HTTPException(409, info.get("why_not") or "No se puede instalar ahora.")
        with _install_lock:
            if _install_state.get("state") in ("downloading", "launching", "closing"):
                return {"started": True, "install": dict(_install_state)}
            _install_state.clear()
            _install_state.update(state="downloading", version=info.get("latest"), started=time.time())
            threading.Thread(target=_install_worker, args=(info,), name="ruffs-update", daemon=True).start()
        return {"started": True, "install": dict(_install_state)}

    @router.get("/api/ruffs/update/status")
    async def install_status() -> Dict[str, Any]:
        return dict(_install_state)

    return router
