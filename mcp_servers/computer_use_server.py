"""
computer_use_server.py — "Computer Use" de RUFFS (Windows y macOS).

Deja que RUFFS vea y maneje el escritorio como lo haría una persona:

* screenshot / zoom      — la pantalla como imagen (los modelos con visión la
                           ven de verdad) y un recorte ampliado.
* mark_elements          — numera sobre la captura los botones, campos y
                           menús de la ventana activa (árbol de accesibilidad,
                           vía Agent Desktop) para hacer clic "en el 14"; la
                           lista en texto sirve también a modelos sin visión.
* click / type_text / press_keys / scroll / drag / move_mouse
                         — actúan y devuelven la pantalla ya estable, para
                           comprobar cada paso sin otra llamada.
* open_target / wait / list_screens / cursor_position
* Seguridad              — apps bloqueadas, confirmación antes de acciones
                           delicadas (cuadro de diálogo en el equipo), parada
                           de emergencia (Esc tres veces o ratón a la esquina
                           superior izquierda) y registro de acciones.

Coordenadas: siempre en píxeles de la ÚLTIMA captura devuelta (screenshot o
cualquier acción); el servidor las traduce a la pantalla real (varios
monitores, escalado de Windows, Retina en Mac).

Ajustes: data/computer_use/config.json (se crea con valores por defecto).
macOS: necesita los permisos de Grabación de pantalla y Accesibilidad
(herramienta request_permissions).
"""

from __future__ import annotations

import base64
import ctypes
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

if IS_WINDOWS:  # coordenadas físicas en todo (capturas, ratón, ventanas)
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001
            pass

from mcp.server.fastmcp import FastMCP, Image  # noqa: E402
from PIL import Image as PILImage, ImageChops, ImageDraw, ImageFont, ImageStat  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("RUFFS_COMPUTER_USE_DIR") or os.path.join(BASE_DIR, "data", "computer_use")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(DATA_DIR, "acciones.jsonl")
AGENT_DESKTOP_PKG = "agent-desktop@1.3.1"   # igual que src/mcp_catalog.py

DEFAULT_CONFIG: dict = {
    "_ayuda": ("confirmacion: 'dialogo' (cuadro en este equipo), 'chat' (el modelo te "
               "pregunta en RUFFS; útil desde el móvil, menos estricto) o 'desactivada'. "
               "apps_bloqueadas: nunca se tocan. apps_con_confirmacion: cada acción pide "
               "permiso. palabras_sensibles: botones que piden permiso antes de pulsarlos."),
    "confirmacion": "dialogo",
    "segundos_para_responder": 60,
    "apps_bloqueadas": [
        "1password", "bitwarden", "keepass", "keepassxc", "lastpass", "dashlane", "enpass",
        "nordpass", "proton pass", "keychain access", "acceso a llaveros", "passwords",
        "contraseñas", "credential manager", "administrador de credenciales", "regedit",
        "windows security", "seguridad de windows", "securityhealthhost", "gpedit", "secpol",
    ],
    "apps_con_confirmacion": [],
    "palabras_sensibles": [
        "enviar", "send", "eliminar", "delete", "borrar", "remove", "quitar", "comprar", "buy",
        "pagar", "pay", "checkout", "finalizar compra", "realizar pedido", "place order",
        "publicar", "publish", "post", "tweet", "confirmar", "confirm", "submit", "transferir",
        "transfer", "desinstalar", "uninstall", "formatear", "format", "suscribirse",
        "subscribe", "vaciar", "empty", "cerrar sesión", "sign out", "log out", "restablecer",
        "reset", "aceptar", "accept", "permitir", "allow", "instalar", "install",
    ],
    "parada_con_esc_triple": True,
    "revisar_elemento_al_clicar": True,
    "parada_en_esquina": True,
    "max_ancho_captura": 1280,
    "max_alto_captura": 800,
    "calidad_jpeg": 72,
}

mcp = FastMCP("computer-use")

# ─── Estado ──────────────────────────────────────────────────────────────────

_state: dict[str, Any] = {
    "paused": False, "pause_reason": "", "view": None, "marks": {}, "screen": None,
    "injecting": 0, "last_set_pos": None, "pending": {},
}
_esc_times: list[float] = []


def _cfg() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=1)
    except (OSError, ValueError):
        pass
    return cfg


def _log(tool: str, **data: Any) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": tool, **data}
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ─── Pantallas y capturas ────────────────────────────────────────────────────

def _mss():
    import mss
    return getattr(mss, "MSS", None)() if hasattr(mss, "MSS") else mss.mss()


def _monitors() -> list[dict]:
    with _mss() as s:
        return [dict(m) for m in s.monitors[1:]]


def _grab(mon: dict) -> PILImage.Image:
    with _mss() as s:
        shot = s.grab(mon)
        return PILImage.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def _mouse():
    from pynput.mouse import Controller
    return Controller()


def _kbd():
    from pynput.keyboard import Controller
    return Controller()


def _screen_for_point(x: float, y: float) -> int:
    for i, m in enumerate(_monitors(), 1):
        if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
            return i
    return 1


def _current_screen() -> int:
    if _state["screen"]:
        return _state["screen"]
    try:
        x, y = _mouse().position
        return _screen_for_point(x, y)
    except Exception:  # noqa: BLE001
        return 1


def _capture(screen: int | None = None, cursor: bool = True) -> tuple[PILImage.Image, dict]:
    """Captura el monitor indicado, reducida, y fija la correspondencia de coordenadas."""
    cfg = _cfg()
    mons = _monitors()
    idx = screen or _current_screen()
    if not 1 <= idx <= len(mons):
        idx = 1
    mon = mons[idx - 1]
    full = _grab(mon)
    max_w, max_h = int(cfg["max_ancho_captura"]), int(cfg["max_alto_captura"])
    ratio = min(max_w / full.width, max_h / full.height, 1.0)
    img = full.resize((max(1, round(full.width * ratio)), max(1, round(full.height * ratio))),
                      PILImage.LANCZOS) if ratio < 1 else full
    view = {"screen": idx, "left": mon["left"], "top": mon["top"], "width": mon["width"],
            "height": mon["height"], "img_w": img.width, "img_h": img.height,
            "full_w": full.width, "full_h": full.height}
    _state["view"] = view
    _state["screen"] = idx
    if cursor:
        try:
            mx, my = _mouse().position
            cx, cy = _to_img(mx, my, view)
            if 0 <= cx < img.width and 0 <= cy < img.height:
                d = ImageDraw.Draw(img)
                d.line([(cx - 9, cy), (cx + 9, cy)], fill=(255, 0, 80), width=2)
                d.line([(cx, cy - 9), (cx, cy + 9)], fill=(255, 0, 80), width=2)
        except Exception:  # noqa: BLE001
            pass
    return img, view


def _to_screen(x: float, y: float, view: dict | None = None) -> tuple[int, int]:
    v = view or _state["view"]
    if not v:
        raise RuntimeError("Primero haz una captura (screenshot) para tener coordenadas.")
    if not (0 <= x <= v["img_w"] and 0 <= y <= v["img_h"]):
        raise ValueError(f"({x}, {y}) está fuera de la captura ({v['img_w']}×{v['img_h']}).")
    return (round(v["left"] + x * v["width"] / v["img_w"]),
            round(v["top"] + y * v["height"] / v["img_h"]))


def _to_img(sx: float, sy: float, view: dict | None = None) -> tuple[int, int]:
    v = view or _state["view"]
    return (round((sx - v["left"]) * v["img_w"] / v["width"]),
            round((sy - v["top"]) * v["img_h"] / v["height"]))


def _jpeg(img: PILImage.Image) -> Image:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=int(_cfg()["calidad_jpeg"]), optimize=True)
    return Image(data=buf.getvalue(), format="jpeg")


def _thumb(screen: int) -> PILImage.Image:
    mon = _monitors()[screen - 1]
    return _grab(mon).convert("L").resize((200, max(1, round(200 * mon["height"] / mon["width"]))))


def _wait_stable(screen: int, timeout: float = 3.0, first_delay: float = 0.35) -> float:
    """Espera a que la pantalla deje de cambiar; devuelve los segundos esperados."""
    t0 = time.time()
    time.sleep(first_delay)
    prev = _thumb(screen)
    while time.time() - t0 < timeout:
        time.sleep(0.25)
        cur = _thumb(screen)
        diff = ImageStat.Stat(ImageChops.difference(prev, cur)).mean[0]
        if diff < 0.6:
            break
        prev = cur
    return time.time() - t0


# ─── Ventanas y apps (para los permisos por app) ─────────────────────────────

def _win_proc(hwnd: int) -> dict:
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    pid = ctypes.c_ulong()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    title = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(hwnd, title, 512)
    exe = ""
    h = k.OpenProcess(0x1000, False, pid.value)
    if h:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            exe = os.path.basename(buf.value)
        k.CloseHandle(h)
    return {"app": exe, "title": title.value, "pid": pid.value}


def _app_at(x: int, y: int) -> dict:
    try:
        if IS_WINDOWS:
            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
            u = ctypes.windll.user32
            u.WindowFromPoint.restype = ctypes.c_void_p
            u.GetAncestor.restype = ctypes.c_void_p
            hwnd = u.WindowFromPoint(POINT(int(x), int(y)))
            root = u.GetAncestor(ctypes.c_void_p(hwnd), 2) if hwnd else None
            return _win_proc(root or hwnd or 0)
        if IS_MAC:
            import Quartz
            wins = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID) or []
            for w in wins:
                b = w.get("kCGWindowBounds") or {}
                if w.get("kCGWindowLayer", 1) == 0 and b and \
                        b["X"] <= x < b["X"] + b["Width"] and b["Y"] <= y < b["Y"] + b["Height"]:
                    return {"app": str(w.get("kCGWindowOwnerName", "")),
                            "title": str(w.get("kCGWindowName", "") or ""),
                            "pid": int(w.get("kCGWindowOwnerPID", 0))}
    except Exception:  # noqa: BLE001
        pass
    return _front_app()


def _front_app() -> dict:
    try:
        if IS_WINDOWS:
            u = ctypes.windll.user32
            u.GetForegroundWindow.restype = ctypes.c_void_p
            return _win_proc(u.GetForegroundWindow() or 0)
        if IS_MAC:
            from AppKit import NSWorkspace
            a = NSWorkspace.sharedWorkspace().frontmostApplication()
            return {"app": str(a.localizedName()), "title": "", "pid": int(a.processIdentifier())}
    except Exception:  # noqa: BLE001
        pass
    return {"app": "", "title": "", "pid": 0}


def _matches(info: dict, names: list[str]) -> str | None:
    hay = f"{info.get('app', '')} {info.get('title', '')}".lower()
    app = (info.get("app") or "").lower().removesuffix(".exe")
    for n in names:
        n = (n or "").strip().lower()
        if n and (n == app or n in hay):
            return n
    return None


def _sensitive_word(label: str) -> str | None:
    text = (label or "").lower()
    for w in _cfg()["palabras_sensibles"]:
        if re.search(r"(?<![\wáéíóúñü])" + re.escape(w.lower()) + r"(?![\wáéíóúñü])", text):
            return w
    return None


# ─── Confirmación y parada de emergencia ─────────────────────────────────────

def _dialog(question: str) -> bool:
    secs = int(_cfg().get("segundos_para_responder", 60))
    text = question + f"\n\n(Si no respondes en {secs} s se cancela.)"
    if IS_WINDOWS:
        u = ctypes.windll.user32
        flags = 0x4 | 0x30 | 0x100 | 0x40000 | 0x10000 | 0x1000  # SÍ/NO, aviso, NO por defecto, encima
        try:
            fn = u.MessageBoxTimeoutW
            fn.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
                           ctypes.c_ushort, ctypes.c_ulong]
            return fn(None, text, "RUFFS · Control del equipo", flags, 0, secs * 1000) == 6
        except AttributeError:
            return u.MessageBoxW(None, text, "RUFFS · Control del equipo", flags) == 6
    if IS_MAC:
        q = text.replace("\\", "\\\\").replace('"', '\\"')
        script = (f'display dialog "{q}" with title "RUFFS · Control del equipo" '
                  f'buttons {{"Cancelar", "Permitir"}} default button "Cancelar" '
                  f'cancel button "Cancelar" with icon caution giving up after {secs}')
        try:
            out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                                 timeout=secs + 15).stdout
            return "button returned:Permitir" in out and "gave up:true" not in out
        except Exception:  # noqa: BLE001
            return False
    return False


def _confirm(what: str, token: str | None) -> str | None:
    """None si se puede seguir; si no, el motivo/instrucción para el modelo."""
    mode = str(_cfg().get("confirmacion", "dialogo")).lower()
    if mode == "desactivada":
        return None
    if mode == "chat":
        pend = _state["pending"]
        if token and pend.get(token, {}).get("what") == what and time.time() - pend[token]["t"] < 600:
            pend.pop(token, None)
            return None
        tok = secrets.token_hex(3)
        pend[tok] = {"what": what, "t": time.time()}
        return (f"NECESITA CONFIRMACIÓN: {what}. Pregunta al usuario en el chat si quiere que lo "
                f"hagas y espera su respuesta. Solo si dice que sí, repite exactamente la misma "
                f"llamada añadiendo confirm_token=\"{tok}\".")
    if _dialog(f"RUFFS quiere: {what}\n\n¿Lo permites?"):
        return None
    return f"El usuario NO permitió: {what}. No lo intentes de otra forma; pregúntale qué prefiere."


def _pause(reason: str) -> None:
    if not _state["paused"]:
        _state.update(paused=True, pause_reason=reason)
        _log("parada", motivo=reason)


def _guard(tool: str, point: tuple[int, int] | None = None, label: str = "",
           confirm_token: str | None = None, action_desc: str = "") -> tuple[dict, str | None]:
    """Comprobaciones antes de actuar. Devuelve (app destino, error o None)."""
    cfg = _cfg()
    if _state["paused"]:
        return {}, (f"Control del equipo EN PAUSA ({_state['pause_reason']}). El usuario lo paró; "
                    "no sigas. Si quiere continuar, llama a resume (le pedirá permiso).")
    if cfg.get("parada_en_esquina", True):
        try:
            mx, my = _mouse().position
            left = min(m["left"] for m in _monitors())
            top = min(m["top"] for m in _monitors())
            at_corner = mx <= left + 1 and my <= top + 1
            prev = _state.get("last_seen_corner")
            _state["last_seen_corner"] = at_corner
            # Solo si el ratón LLEGÓ a la esquina desde la última acción (no si ya estaba).
            if at_corner and prev is False and _state["last_set_pos"] != (round(mx), round(my)):
                _pause("ratón llevado a la esquina superior izquierda")
                return {}, "Parada de emergencia: el usuario llevó el ratón a la esquina. No sigas."
        except Exception:  # noqa: BLE001
            pass
    info = _app_at(*point) if point else _front_app()
    if info.get("pid") == os.getpid():
        return info, "Esa ventana es de RUFFS (su cuadro de confirmación): no se puede tocar."
    blocked = _matches(info, cfg["apps_bloqueadas"])
    if blocked:
        return info, (f"La app «{info.get('app') or info.get('title')}» está bloqueada para RUFFS "
                      f"(regla «{blocked}»). Pide al usuario que lo haga él.")
    reasons = []
    if _matches(info, cfg["apps_con_confirmacion"]):
        reasons.append(f"usar {info.get('app')}")
    word = _sensitive_word(label)
    if word:
        reasons.append(f"pulsar «{label[:80]}»")
    if reasons:
        what = action_desc or f"{tool}: " + ", ".join(reasons)
        err = _confirm(what + f" (en {info.get('app') or 'la app activa'})", confirm_token)
        if err:
            return info, err
    return info, None


def _start_esc_listener() -> None:
    if not _cfg().get("parada_con_esc_triple", True):
        return
    try:
        from pynput import keyboard

        def on_press(key, injected=False):
            if injected or _state["injecting"]:
                return
            if key == keyboard.Key.esc:
                now = time.time()
                _esc_times[:] = [t for t in _esc_times if now - t < 1.5] + [now]
                if len(_esc_times) >= 3:
                    _pause("Esc pulsado tres veces")

        listener = keyboard.Listener(on_press=on_press)
        listener.daemon = True
        listener.start()
    except Exception:  # noqa: BLE001  (sin permiso de supervisión de entrada en Mac)
        pass


# ─── Elementos numerados (Agent Desktop) ─────────────────────────────────────

_AD_PATH = os.path.join(DATA_DIR, "agent_desktop_path.txt")
_CLICK_ACTIONS = {"click", "toggle", "invoke", "press", "axpress", "setvalue", "select", "expand",
                  "collapse", "showmenu", "axshowmenu", "axconfirm", "axincrement", "axpick", "open"}
_INTERACTIVE_ROLES = {"button", "link", "checkbox", "radio", "radiobutton", "switch", "textfield",
                      "textbox", "edit", "combobox", "menuitem", "menu item", "tab", "tabitem",
                      "listitem", "treeitem", "slider", "spinbutton", "searchfield", "popupbutton",
                      "menubutton", "cell", "option", "hyperlink", "splitbutton", "text field",
                      "secure text field", "toggle", "dropdown", "menubaritem"}


def _agent_desktop_cmd() -> list[str] | None:
    try:
        with open(_AD_PATH, encoding="utf-8") as f:
            script = f.read().strip()
        if os.path.isfile(script):
            return [shutil.which("node") or "node", script]
    except OSError:
        pass
    npx = shutil.which("npx")
    if not npx:
        return None
    try:
        out = subprocess.run([npx, "-y", "-p", AGENT_DESKTOP_PKG, "node", "-e",
                              "console.log(process.env.PATH)"], capture_output=True, text=True,
                             timeout=300, stdin=subprocess.DEVNULL).stdout
    except Exception:  # noqa: BLE001
        return None
    for entry in out.strip().split(os.pathsep):
        if entry.rstrip("/\\").endswith(".bin"):
            script = os.path.join(os.path.dirname(entry.rstrip("/\\")), "agent-desktop", "bin", "agent-desktop")
            if os.path.isfile(script):
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(_AD_PATH, "w", encoding="utf-8") as f:
                    f.write(script)
                return [shutil.which("node") or "node", script]
    return None


def _snapshot(app: str | None, surface: str | None, timeout_ms: int = 6000) -> dict:
    cmd = _agent_desktop_cmd()
    if not cmd:
        raise RuntimeError("No se encontró Agent Desktop (hace falta Node.js; RUFFS lo instala).")
    args = cmd + ["snapshot", "--include-bounds", "-i", "--compact", "--timeout-ms", str(timeout_ms)]
    if app:
        args += ["--app", app]
    if surface:
        args += ["--surface", surface]
    kw: dict = {"creationflags": 0x08000000} if IS_WINDOWS else {}
    # Agent Desktop exige crear él mismo su carpeta privada (con sus permisos);
    # si una anterior quedó con permisos que no acepta, se rehace.
    home = os.path.join(DATA_DIR, "agent-desktop-home")
    for attempt in range(2):
        res = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=60, stdin=subprocess.DEVNULL,
                             env={**os.environ, "AGENT_DESKTOP_HOME": home}, **kw)
        try:
            data = json.loads(res.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise RuntimeError(f"Agent Desktop no respondió bien: {(res.stderr or res.stdout)[-400:]}")
        msg = (data.get("error") or {}).get("message", "")
        if not data.get("ok") and attempt == 0 and ("principal" in msg or "DACL" in msg or "owner" in msg):
            shutil.rmtree(home, ignore_errors=True)
            continue
        break
    if not data.get("ok"):
        raise RuntimeError(f"Agent Desktop: {msg or data}")
    return data["data"]


def _elements_at(pt: tuple[int, int]) -> str:
    """Etiqueta del elemento más pequeño bajo un punto de la pantalla (para avisar antes
    de pulsar algo delicado aunque se haga clic por coordenadas)."""
    try:
        data = _snapshot(None, None, timeout_ms=2500)
    except Exception:  # noqa: BLE001
        return ""
    elems: list = []
    _collect(data.get("tree") or {}, elems)
    best, area = "", None
    for e in elems:
        b = e["bounds"]
        if b["x"] <= pt[0] <= b["x"] + b["width"] and b["y"] <= pt[1] <= b["y"] + b["height"]:
            a = b["width"] * b["height"]
            if area is None or a < area:
                best, area = f"{e['role']} {e['name']}".strip(), a
    return best


def _collect(node: dict, out: list) -> None:
    b = node.get("bounds")
    acts = {a.lower() for a in node.get("available_actions") or []}
    role = (node.get("role") or "").lower()
    if b and node.get("ref_id") and b.get("width", 0) > 2 and b.get("height", 0) > 2 and \
            (acts & _CLICK_ACTIONS or role in _INTERACTIVE_ROLES):
        label = node.get("name") or node.get("description") or node.get("value") or ""
        out.append({"role": node.get("role_description") or node.get("role") or "",
                    "name": str(label)[:120], "bounds": b, "ref": node.get("ref_id")})
    for c in node.get("children") or []:
        _collect(c, out)


# ─── Herramientas ────────────────────────────────────────────────────────────

def _result(text: str, screen: int | None = None, show: str = "screenshot") -> list:
    if show == "none":
        return [text]
    if show == "elements":
        try:
            return _marks_result(None, None, screen, prefix=text + "\n\n")
        except Exception as e:  # noqa: BLE001
            text += f"\n(No se pudieron listar los elementos: {e})"
    img, v = _capture(screen)
    return [_jpeg(img), text + f"\nCaptura: pantalla {v['screen']}, {v['img_w']}×{v['img_h']} px "
                               "(usa estas coordenadas)."]


@mcp.tool()
def screenshot(screen: int = 0) -> list:
    """Captura la pantalla (por defecto la del ratón; ``screen`` = 1, 2… para elegir monitor).
    La imagen lleva una cruz rosa donde está el ratón. Todas las coordenadas que uses
    después (click, scroll, zoom…) son píxeles de ESTA captura."""
    return _result("Pantalla actual.", screen or None)


@mcp.tool()
def zoom(x0: int, y0: int, x1: int, y1: int) -> list:
    """Amplía una zona de la última captura (rectángulo x0,y0 → x1,y1 en sus píxeles) para
    leer texto pequeño o iconos. No cambia las coordenadas de referencia."""
    v = _state["view"]
    if not v:
        return ["Primero haz una captura (screenshot)."]
    full = _grab(_monitors()[v["screen"] - 1])
    sx, sy = full.width / v["img_w"], full.height / v["img_h"]
    box = (max(0, min(x0, x1)) * sx, max(0, min(y0, y1)) * sy, min(v["img_w"], max(x0, x1)) * sx,
           min(v["img_h"], max(y0, y1)) * sy)
    if box[2] - box[0] < 4 or box[3] - box[1] < 4:
        return ["Zona demasiado pequeña."]
    crop = full.crop(tuple(round(c) for c in box))
    cfg = _cfg()
    r = min(cfg["max_ancho_captura"] / crop.width, cfg["max_alto_captura"] / crop.height, 4.0)
    crop = crop.resize((max(1, round(crop.width * r)), max(1, round(crop.height * r))), PILImage.LANCZOS)
    return [_jpeg(crop), f"Zoom de ({x0},{y0})–({x1},{y1}) ×{r:.1f}. Las coordenadas siguen siendo las de la captura completa."]


def _marks_result(app: str | None, surface: str | None, screen: int | None, prefix: str = "") -> list:
    data = _snapshot(app, surface)
    elems: list = []
    _collect(data.get("tree") or {}, elems)
    img, v = _capture(screen, cursor=False)
    seen, marks, lines = set(), {}, []
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=13)
    except TypeError:
        font = ImageFont.load_default()
    palette = [(255, 0, 110), (0, 160, 255), (255, 140, 0), (0, 190, 120), (150, 70, 255)]
    for e in elems:
        b = e["bounds"]
        x0, y0 = _to_img(b["x"], b["y"], v)
        x1, y1 = _to_img(b["x"] + b["width"], b["y"] + b["height"], v)
        if x1 <= 0 or y1 <= 0 or x0 >= v["img_w"] or y0 >= v["img_h"]:
            continue
        key = (x0, y0, x1, y1)
        if key in seen:
            continue
        seen.add(key)
        n = len(marks) + 1
        if n > 200:
            break
        col = palette[n % len(palette)]
        d.rectangle([x0, y0, x1, y1], outline=col, width=2)
        tag = str(n)
        tw = d.textlength(tag, font=font) + 6
        ty = y0 - 16 if y0 >= 16 else y0
        d.rectangle([x0, ty, x0 + tw, ty + 15], fill=col)
        d.text((x0 + 3, ty + 1), tag, fill=(255, 255, 255), font=font)
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        marks[n] = {"x": cx, "y": cy, "name": e["name"], "role": e["role"]}
        lines.append(f"{n}. [{e['role']}] {e['name'] or '(sin nombre)'} — ({cx},{cy})")
    _state["marks"] = marks
    text = (prefix + f"App: {data.get('app', '?')}. {len(marks)} elementos numerados "
            "(usa click(element=N) o sus coordenadas):\n" + "\n".join(lines))
    return [_jpeg(img), text[:12000]]


@mcp.tool()
def mark_elements(app: str = "", surface: str = "", screen: int = 0) -> list:
    """Numera sobre la captura los elementos con los que se puede interactuar (botones,
    campos, menús, enlaces…) de la ventana activa, o de ``app``. ``surface`` puede ser
    taskbar, start-menu, dock, menubar, menu, alert… Devuelve la imagen y la lista en texto
    (sirve aunque el modelo no tenga visión). Luego: click(element=N)."""
    try:
        return _marks_result(app or None, surface or None, screen or None)
    except Exception as e:  # noqa: BLE001
        return [f"No se pudieron leer los elementos: {e}. Usa screenshot y coordenadas."]


def _target(x: int | None, y: int | None, element: int | None) -> tuple[tuple[int, int], str]:
    if element:
        m = _state["marks"].get(int(element))
        if not m:
            raise ValueError(f"No hay elemento {element}; vuelve a llamar a mark_elements.")
        return _to_screen(m["x"], m["y"]), f"{m['role']} {m['name']}".strip()
    if x is None or y is None:
        raise ValueError("Indica x e y (píxeles de la última captura) o element=N.")
    pt = _to_screen(x, y)
    label = ""
    for m in _state["marks"].values():   # ¿cae dentro de un elemento conocido?
        if abs(m["x"] - x) < 30 and abs(m["y"] - y) < 14:
            label = f"{m['role']} {m['name']}".strip()
            break
    if not label and _cfg().get("revisar_elemento_al_clicar", True):
        label = _elements_at(pt)
    return pt, label


def _inject(fn) -> None:
    _state["injecting"] += 1
    try:
        fn()
    finally:
        time.sleep(0.05)
        _state["injecting"] -= 1


def _key(name: str):
    from pynput.keyboard import Key, KeyCode
    n = name.strip().lower()
    alias = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt", "option": "alt",
             "opt": "alt", "altgr": "alt_gr", "cmd": "cmd", "command": "cmd", "win": "cmd",
             "windows": "cmd", "super": "cmd", "meta": "cmd", "enter": "enter", "return": "enter",
             "intro": "enter", "tab": "tab", "esc": "esc", "escape": "esc", "backspace": "backspace",
             "retroceso": "backspace", "delete": "delete", "del": "delete", "supr": "delete",
             "space": "space", "espacio": "space", "up": "up", "down": "down", "left": "left",
             "right": "right", "arriba": "up", "abajo": "down", "izquierda": "left",
             "derecha": "right", "home": "home", "inicio": "home", "end": "end", "fin": "end",
             "pageup": "page_up", "pgup": "page_up", "pagedown": "page_down", "pgdn": "page_down",
             "capslock": "caps_lock", "insert": "insert", "printscreen": "print_screen",
             "menu": "menu"}
    if n == "mod":
        return Key.cmd if IS_MAC else Key.ctrl
    if n in alias:
        return getattr(Key, alias[n])
    if re.fullmatch(r"f([1-9]|1[0-9]|20)", n):
        return getattr(Key, n)
    if len(name.strip()) == 1:
        return KeyCode.from_char(name.strip())
    raise ValueError(f"Tecla desconocida: {name}")


def _act(tool: str, desc: str, pt: tuple[int, int] | None, label: str, confirm_token: str | None,
         fn, show: str, settle: float = 3.0) -> list:
    info, err = _guard(tool, pt, label, confirm_token)
    if err:
        _log(tool, bloqueado=err[:200], app=info.get("app"))
        return [err]
    screen = _screen_for_point(*pt) if pt else _current_screen()
    try:
        _inject(fn)
    except Exception as e:  # noqa: BLE001
        return [f"No se pudo {desc}: {e}"]
    if pt:
        _state["last_set_pos"] = pt
    _log(tool, accion=desc, app=info.get("app"), ventana=(info.get("title") or "")[:80])
    waited = _wait_stable(screen, settle) if show != "none" else 0
    return _result(f"Hecho: {desc} (app: {info.get('app') or '?'}). Pantalla estable tras {waited:.1f} s.",
                   screen, show)


@mcp.tool()
def click(x: int | None = None, y: int | None = None, element: int | None = None,
          button: str = "left", clicks: int = 1, modifiers: str = "",
          show: str = "screenshot", confirm_token: str = "") -> list:
    """Clic en (x, y) de la última captura o en el elemento N de mark_elements.
    button: left | right | middle. clicks: 2 = doble clic. modifiers: p. ej. "ctrl" o
    "shift+cmd". show: screenshot (por defecto) | elements | none — qué devolver después.
    Si el botón parece delicado (Enviar, Borrar, Pagar…) se pedirá permiso al usuario."""
    from pynput.mouse import Button
    pt, label = _target(x, y, element)
    btn = {"left": Button.left, "right": Button.right, "middle": Button.middle}.get(button, Button.left)
    mods = [_key(k) for k in modifiers.split("+") if k.strip()]

    def do():
        from pynput.keyboard import Controller
        kb, mo = Controller(), _mouse()
        mo.position = pt
        time.sleep(0.08)
        for k in mods:
            kb.press(k)
        try:
            mo.click(btn, max(1, min(int(clicks), 3)))
        finally:
            for k in reversed(mods):
                kb.release(k)

    what = f"{'doble ' if clicks == 2 else ''}clic {button} en ({pt[0]},{pt[1]})" + (f" «{label}»" if label else "")
    return _act("click", what, pt, label, confirm_token or None, do, show)


@mcp.tool()
def type_text(text: str, enter: bool = False, show: str = "screenshot", confirm_token: str = "") -> list:
    """Escribe texto (con tildes y emojis) donde esté el foco. enter=true pulsa Intro al final
    (eso puede ENVIAR un mensaje o formulario: pide antes permiso al usuario con
    request_confirmation si no te lo ha pedido él)."""
    def do():
        kb = _kbd()
        kb.type(text)
        if enter:
            from pynput.keyboard import Key
            time.sleep(0.05)
            kb.press(Key.enter)
            kb.release(Key.enter)
    preview = text if len(text) <= 60 else text[:57] + "…"
    return _act("type_text", f"escribir «{preview}»" + (" + Intro" if enter else ""), None, "",
                confirm_token or None, do, show)


@mcp.tool()
def press_keys(keys: str, show: str = "screenshot", confirm_token: str = "") -> list:
    """Pulsa una combinación de teclas: "ctrl+s", "cmd+tab", "enter", "alt+f4", "mod+c"
    (mod = Ctrl en Windows, Cmd en Mac). Varias seguidas separadas por coma: "ctrl+a, delete"."""
    combos = [c.strip() for c in keys.split(",") if c.strip()]
    parsed = [[_key(k) for k in c.split("+") if k.strip()] for c in combos]

    def do():
        kb = _kbd()
        for combo in parsed:
            for k in combo:
                kb.press(k)
            time.sleep(0.04)
            for k in reversed(combo):
                kb.release(k)
            time.sleep(0.08)
    return _act("press_keys", f"pulsar {keys}", None, "", confirm_token or None, do, show)


@mcp.tool()
def scroll(x: int, y: int, direction: str = "down", amount: int = 5, show: str = "screenshot") -> list:
    """Desplaza (rueda del ratón) sobre (x, y): direction up | down | left | right, amount = muescas."""
    pt = _to_screen(x, y)
    n = max(1, min(int(amount), 30))
    dx, dy = {"up": (0, n), "down": (0, -n), "left": (-n, 0), "right": (n, 0)}.get(direction, (0, -n))

    def do():
        mo = _mouse()
        mo.position = pt
        time.sleep(0.05)
        mo.scroll(dx, dy)
    return _act("scroll", f"desplazar {direction} ×{n} en ({pt[0]},{pt[1]})", pt, "", None, do, show, 2.0)


@mcp.tool()
def drag(x0: int, y0: int, x1: int, y1: int, show: str = "screenshot") -> list:
    """Arrastra con el botón izquierdo desde (x0, y0) hasta (x1, y1)."""
    from pynput.mouse import Button
    a, b = _to_screen(x0, y0), _to_screen(x1, y1)

    def do():
        mo = _mouse()
        mo.position = a
        time.sleep(0.08)
        mo.press(Button.left)
        steps = 20
        for i in range(1, steps + 1):
            mo.position = (round(a[0] + (b[0] - a[0]) * i / steps), round(a[1] + (b[1] - a[1]) * i / steps))
            time.sleep(0.015)
        mo.release(Button.left)
    info, err = _guard("drag", b, "", None)  # también el destino
    if err:
        return [err]
    return _act("drag", f"arrastrar de ({a[0]},{a[1]}) a ({b[0]},{b[1]})", a, "", None, do, show)


@mcp.tool()
def move_mouse(x: int, y: int, show: str = "none") -> list:
    """Mueve el ratón a (x, y) sin hacer clic (p. ej. para ver un menú o una descripción emergente)."""
    pt = _to_screen(x, y)
    return _act("move_mouse", f"mover el ratón a ({pt[0]},{pt[1]})", pt, "", None,
                lambda: setattr(_mouse(), "position", pt), show, 1.5)


@mcp.tool()
def open_target(target: str, show: str = "screenshot") -> list:
    """Abre una app (por nombre: "Bloc de notas", "Safari", "notepad"), un archivo o una URL con
    su programa predeterminado."""
    cfg = _cfg()
    if _matches({"app": target, "title": target}, cfg["apps_bloqueadas"]):
        return [f"«{target}» está bloqueada para RUFFS."]
    _, err = _guard("open_target")
    if err and "EN PAUSA" in err:
        return [err]
    is_path = os.path.exists(os.path.expanduser(target))
    is_url = bool(re.match(r"^[a-z][a-z0-9+.-]*://", target, re.I))
    try:
        if IS_WINDOWS:
            if is_path or is_url:
                os.startfile(os.path.expanduser(target))  # noqa: S606
            else:
                subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=0x08000000)
        elif IS_MAC:
            cmd = ["open", os.path.expanduser(target)] if (is_path or is_url) else ["open", "-a", target]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return [f"No se pudo abrir «{target}»: {r.stderr.strip()[:300]}"]
        else:
            subprocess.Popen(["xdg-open", target])
    except Exception as e:  # noqa: BLE001
        return [f"No se pudo abrir «{target}»: {e}"]
    _log("open_target", destino=target)
    if show == "none":
        return [f"Abriendo «{target}»."]
    time.sleep(1.0)
    waited = _wait_stable(_current_screen(), 6.0)
    return _result(f"Abierto «{target}» (esperé {waited + 1:.1f} s).", None, show)


@mcp.tool()
def wait(seconds: float = 2.0, show: str = "screenshot") -> list:
    """Espera (máx. 30 s) y devuelve la pantalla: para cargas, descargas o animaciones."""
    time.sleep(max(0.0, min(float(seconds), 30.0)))
    return _result(f"Esperé {seconds} s.", None, show)


@mcp.tool()
def list_screens() -> str:
    """Monitores conectados (número, posición, tamaño) y cuál es el actual."""
    mons = _monitors()
    cur = _current_screen()
    return json.dumps([{"screen": i, "left": m["left"], "top": m["top"], "width": m["width"],
                        "height": m["height"], "current": i == cur} for i, m in enumerate(mons, 1)],
                      ensure_ascii=False)


@mcp.tool()
def cursor_position() -> str:
    """Dónde está el ratón, en píxeles de la última captura, y qué app hay debajo."""
    mx, my = _mouse().position
    info = _app_at(int(mx), int(my))
    pos = _to_img(mx, my) if _state["view"] else None
    return json.dumps({"captura": pos, "pantalla_real": [round(mx), round(my)], "app": info.get("app"),
                       "ventana": info.get("title")}, ensure_ascii=False)


@mcp.tool()
def request_confirmation(description: str, confirm_token: str = "") -> str:
    """Pide permiso al usuario ANTES de algo irreversible o delicado (enviar un mensaje o
    correo, comprar, borrar, publicar, cambiar ajustes…). Describe exactamente qué harás.
    Devuelve si el usuario lo permitió. Úsalo aunque el botón no parezca peligroso."""
    err = _confirm(description.strip()[:300], confirm_token or None)
    _log("request_confirmation", que=description[:200], permitido=err is None)
    return "PERMITIDO: adelante." if err is None else err


@mcp.tool()
def status() -> str:
    """Estado del control del equipo: pausa, permisos (macOS), monitores, ajustes y app activa."""
    cfg = _cfg()
    perms: dict = {}
    if IS_MAC:
        try:
            import Quartz
            perms["grabacion_de_pantalla"] = bool(Quartz.CGPreflightScreenCaptureAccess())
        except Exception:  # noqa: BLE001
            perms["grabacion_de_pantalla"] = None
        try:
            from ApplicationServices import AXIsProcessTrusted
            perms["accesibilidad"] = bool(AXIsProcessTrusted())
        except Exception:  # noqa: BLE001
            perms["accesibilidad"] = None
    return json.dumps({
        "en_pausa": _state["paused"], "motivo_pausa": _state["pause_reason"],
        "confirmacion": cfg["confirmacion"], "permisos_macos": perms or None,
        "monitores": len(_monitors()), "app_activa": _front_app().get("app"),
        "ajustes": CONFIG_PATH, "registro": LOG_PATH,
        "parada": "Esc tres veces seguidas o llevar el ratón a la esquina superior izquierda",
    }, ensure_ascii=False, indent=1)


@mcp.tool()
def request_permissions() -> str:
    """macOS: pide los permisos de Grabación de pantalla y Accesibilidad (abre Ajustes del
    Sistema). Después hay que reiniciar RUFFS. En Windows no hace falta nada."""
    if not IS_MAC:
        return "En Windows no hacen falta permisos especiales."
    msgs = []
    try:
        import Quartz
        if not Quartz.CGPreflightScreenCaptureAccess():
            Quartz.CGRequestScreenCaptureAccess()
            subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"])
            msgs.append("Activa RUFFS en Privacidad → Grabación de pantalla.")
    except Exception as e:  # noqa: BLE001
        msgs.append(f"Grabación de pantalla: {e}")
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
        if not AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}):
            subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])
            msgs.append("Activa RUFFS en Privacidad → Accesibilidad.")
    except Exception as e:  # noqa: BLE001
        msgs.append(f"Accesibilidad: {e}")
    return " ".join(msgs) + " Luego cierra y vuelve a abrir RUFFS." if msgs else "Los permisos ya están concedidos."


@mcp.tool()
def resume(confirm_token: str = "") -> str:
    """Reanuda el control del equipo tras una parada de emergencia (el usuario debe permitirlo)."""
    if not _state["paused"]:
        return "No estaba en pausa."
    err = _confirm("reanudar el control del equipo tras la parada de emergencia", confirm_token or None)
    if err:
        return err
    _state.update(paused=False, pause_reason="")
    _esc_times.clear()
    _log("reanudar")
    return "Control reanudado."


@mcp.tool()
def action_log(last: int = 20) -> str:
    """Las últimas acciones que hizo RUFFS en el equipo (registro local)."""
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            lines = f.readlines()[-max(1, min(int(last), 200)):]
        return "".join(lines) or "Sin acciones registradas."
    except OSError:
        return "Sin acciones registradas."


if __name__ == "__main__":
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    _cfg()
    _start_esc_listener()
    mcp.run()
