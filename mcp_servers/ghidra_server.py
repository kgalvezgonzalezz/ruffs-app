"""
ghidra_server.py — servidor MCP de Ghidra para RUFFS (Windows, macOS y Linux).

Analiza binarios con Ghidra en modo headless (sin abrir la interfaz): resumen,
funciones, decompilación a C, desensamblado, importaciones, exportaciones,
cadenas y referencias cruzadas. Cada binario se analiza una sola vez y el
proyecto queda en caché (data/ghidra/cache), así que las siguientes consultas
no repiten el análisis.

Ghidra y Java se buscan solos (GHIDRA_HOME / JAVA_HOME si existen, la copia de
RUFFS en data/tools y las rutas de instalación habituales). Si no están, la
herramienta ``ghidra_install`` descarga Ghidra (GitHub oficial) y el JDK que pida
(Eclipse Temurin) dentro de RUFFS, comprobando su SHA-256; no necesita
permisos de administrador.

Las herramientas binary_info y extract_strings funcionan sin Ghidra.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile

from mcp.server.fastmcp import FastMCP

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ghidra_scripts")
DATA_DIR = os.environ.get("RUFFS_GHIDRA_DIR") or os.path.join(BASE_DIR, "data", "ghidra")
TOOLS_DIR = os.path.join(DATA_DIR, "tools")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
# Solo la copia de RUFFS (data/ghidra/tools), sin buscar en el sistema (pruebas).
ONLY_LOCAL = os.environ.get("RUFFS_GHIDRA_ONLY_LOCAL", "").strip().lower() in ("1", "true", "yes")
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
MAX_CHARS = 60_000
USER_AGENT = "RUFFS-ghidra-mcp"

mcp = FastMCP("ghidra")
_lock = threading.Lock()          # un análisis de Ghidra a la vez
_install_state: dict = {"state": "idle"}
_install_thread: threading.Thread | None = None


# ─── Localizar Ghidra y Java ──────────────────────────────────────────────────

def _headless(home: str) -> str:
    return os.path.join(home, "support", "analyzeHeadless.bat" if IS_WINDOWS else "analyzeHeadless")


def _ghidra_version(home: str) -> str:
    try:
        with open(os.path.join(home, "Ghidra", "application.properties"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("application.version="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "?"


def _version_key(path: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", os.path.basename(path.rstrip("/\\")))[:4])


def _ghidra_candidates() -> list[str]:
    pats = [os.path.join(TOOLS_DIR, "ghidra_*")]
    home = os.path.expanduser("~")
    if ONLY_LOCAL:
        pass
    elif IS_WINDOWS:
        for root in filter(None, {os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)"),
                                  os.environ.get("LOCALAPPDATA"), "C:\\"}):
            pats += [os.path.join(root, "ghidra*"), os.path.join(root, "Ghidra", "ghidra*")]
        pats += [os.path.join(home, "Tools", "ghidra*"), os.path.join(home, "ghidra*"),
                 os.path.join(home, "Downloads", "ghidra*", "ghidra*")]
    else:
        pats += ["/Applications/ghidra*", "/Applications/Ghidra*/ghidra*",
                 "/opt/homebrew/Caskroom/ghidra/*/ghidra_*", "/usr/local/Caskroom/ghidra/*/ghidra_*",
                 "/opt/ghidra*", "/usr/share/ghidra", os.path.join(home, "ghidra*"),
                 os.path.join(home, "Applications", "ghidra*"), os.path.join(home, "Downloads", "ghidra*")]
    found = []
    for pat in pats:
        for p in glob.glob(pat):
            if os.path.isfile(_headless(p)):
                found.append(p)
    return sorted(set(found), key=_version_key, reverse=True)


def find_ghidra() -> str | None:
    env = "" if ONLY_LOCAL else os.environ.get("GHIDRA_HOME", "").strip()
    if env and os.path.isfile(_headless(env)):
        return env
    c = _ghidra_candidates()
    return c[0] if c else None


def _java_major(java_home: str) -> int:
    try:
        with open(os.path.join(java_home, "release"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("JAVA_VERSION="):
                    v = line.split("=", 1)[1].strip().strip('"')
                    major = v.split(".")[0]
                    return int(v.split(".")[1]) if major == "1" else int(major)
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _is_jdk(home: str) -> bool:
    exe = "javac.exe" if IS_WINDOWS else "javac"
    return os.path.isfile(os.path.join(home, "bin", exe))


def _java_needed(home: str | None = None) -> int:
    """Java mínimo que pide el Ghidra encontrado (application.java.min; 12.2 → 25)."""
    home = home or find_ghidra()
    if home:
        try:
            with open(os.path.join(home, "Ghidra", "application.properties"), encoding="utf-8") as f:
                for line in f:
                    if line.startswith("application.java.min="):
                        return int(line.split("=", 1)[1].strip() or 21)
        except (OSError, ValueError):
            pass
    return 21


def find_java() -> str | None:
    """JDK con la versión mínima que pide Ghidra (21 si aún no hay Ghidra)."""
    need = _java_needed()
    cands = []
    env = "" if ONLY_LOCAL else os.environ.get("JAVA_HOME", "").strip()
    if env:
        cands.append(env)
    cands += glob.glob(os.path.join(TOOLS_DIR, "jdk*"))
    cands += glob.glob(os.path.join(TOOLS_DIR, "jdk*", "Contents", "Home"))
    if ONLY_LOCAL:
        pass
    elif IS_WINDOWS:
        for root in filter(None, {os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramW6432")}):
            for vendor in ("Java", "Eclipse Adoptium", "Microsoft", "Zulu", "Amazon Corretto", "BellSoft", "OpenJDK"):
                cands += glob.glob(os.path.join(root, vendor, "*jdk*"))
    else:
        cands += glob.glob("/Library/Java/JavaVirtualMachines/*/Contents/Home")
        cands += glob.glob(os.path.expanduser("~/Library/Java/JavaVirtualMachines/*/Contents/Home"))
        cands += glob.glob("/opt/homebrew/opt/openjdk*/libexec/openjdk.jdk/Contents/Home")
        cands += glob.glob("/usr/local/opt/openjdk*/libexec/openjdk.jdk/Contents/Home")
        cands += glob.glob("/usr/lib/jvm/*")
    javac = None if ONLY_LOCAL else shutil.which("javac")
    if javac:
        cands.append(os.path.dirname(os.path.dirname(os.path.realpath(javac))))
    good = [c for c in dict.fromkeys(cands) if os.path.isdir(c) and _is_jdk(c) and _java_major(c) >= need]
    good.sort(key=_java_major, reverse=True)
    return good[0] if good else None


# ─── Instalación (Ghidra + JDK dentro de RUFFS) ──────────────────────────────

def _ssl_context():
    """Certificados del sistema + certifi (ver src/https.py): sin esto, en algunos
    equipos GitHub falla con CERTIFICATE_VERIFY_FAILED."""
    import ssl
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        pass
    return ctx


def _urlopen(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


def _get_json(url: str) -> object:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with _urlopen(req, 60) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_text(url: str) -> tuple[str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _urlopen(req, 60) as r:
        return r.read().decode("utf-8", "replace"), r.geturl()


_GHIDRA_REPO = "NationalSecurityAgency/ghidra"


def _latest_ghidra() -> tuple[str, str, str | None, str]:
    """(nombre, url, sha256, etiqueta) del zip de la última versión de Ghidra.

    Primero la API de GitHub; si falla (p. ej. límite de peticiones por IP), las
    páginas públicas de la versión, que también listan el zip y su SHA-256."""
    try:
        rel = _get_json(f"https://api.github.com/repos/{_GHIDRA_REPO}/releases/latest")
        asset = next(a for a in rel["assets"]
                     if a["name"].startswith("ghidra_") and a["name"].endswith(".zip"))
        digest = (asset.get("digest") or "").split(":", 1)[-1] or None
        if not digest:  # versiones antiguas de la API: el SHA-256 va en las notas
            m = re.search(r"\b([0-9a-fA-F]{64})\b", rel.get("body") or "")
            digest = m.group(1) if m else None
        return asset["name"], asset["browser_download_url"], digest, rel.get("tag_name", "")
    except Exception as api_error:  # noqa: BLE001
        _, final = _get_text(f"https://github.com/{_GHIDRA_REPO}/releases/latest")
        tag = final.rstrip("/").rsplit("/", 1)[-1]
        if not tag or tag == "latest":
            raise RuntimeError(f"No se pudo consultar GitHub: {api_error}")
        html, _ = _get_text(f"https://github.com/{_GHIDRA_REPO}/releases/expanded_assets/{tag}")
        m = re.search(r'href="(/' + re.escape(_GHIDRA_REPO) + r'/releases/download/[^"]+/(ghidra_[^"/]+\.zip))"', html)
        if not m:
            raise RuntimeError(f"No se encontró el zip de Ghidra {tag} ({api_error})")
        name = m.group(2)
        digest = None
        for page in (html, _get_text(f"https://github.com/{_GHIDRA_REPO}/releases/tag/{tag}")[0]):
            near = page[page.find(name):][:4000] if name in page else page
            d = re.search(r"\b([0-9a-f]{64})\b", near)
            if d:
                digest = d.group(1)
                break
        return name, "https://github.com" + m.group(1), digest, tag


def _download(url: str, dest: str, sha256: str | None, label: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    with _urlopen(req, 120) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            _install_state["detail"] = (f"Descargando {label}: {done >> 20} MB"
                                        + (f" de {total >> 20} MB" if total else ""))
    if sha256 and h.hexdigest().lower() != sha256.lower():
        os.remove(dest)
        raise RuntimeError(f"El SHA-256 de {label} no coincide; descarga descartada.")


def _safe_target(dest: str, name: str) -> str:
    target = os.path.realpath(os.path.join(dest, name))
    root = os.path.realpath(dest)
    if not (target == root or target.startswith(root + os.sep)):
        raise RuntimeError(f"Ruta no permitida dentro del archivo: {name}")
    return target


def _extract(archive: str, dest: str) -> None:
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                target = _safe_target(dest, info.filename)
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                mode = (info.external_attr >> 16) & 0o777
                if mode and not IS_WINDOWS:
                    os.chmod(target, mode)
    else:
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                _safe_target(dest, m.name)
            try:
                t.extractall(dest, filter="tar")
            except TypeError:  # Python sin filtros de tarfile
                t.extractall(dest)


def _native_platform() -> str:
    arm = platform.machine().lower() in ("arm64", "aarch64")
    if IS_WINDOWS:
        return "win_x86_64"          # Windows ARM usa la emulación x86-64
    if IS_MAC:
        return "mac_arm_64" if arm else "mac_x86_64"
    return "linux_arm_64" if arm else "linux_x86_64"


def _natives_ok(home: str | None) -> bool:
    """¿Hay decompilador nativo para esta plataforma (incluido o compilado)?"""
    if not home:
        return False
    plat, exe = _native_platform(), ("decompile.exe" if IS_WINDOWS else "decompile")
    base = os.path.join(home, "Ghidra", "Features", "Decompiler")
    return any(os.path.isfile(os.path.join(base, sub, plat, exe)) for sub in (os.path.join("build", "os"), "os"))


def _build_natives(home: str, java: str) -> None:
    if IS_MAC and subprocess.run(["xcrun", "--find", "clang"], capture_output=True).returncode != 0:
        try:
            subprocess.Popen(["xcode-select", "--install"])  # abre el instalador de Apple
        except OSError:
            pass
        raise RuntimeError("Para decompilar en Mac faltan las herramientas de línea de comandos de Xcode. "
                           "Se abrió el instalador de Apple: acéptalo y, cuando termine, vuelve a "
                           "llamar a ghidra_install (el resto ya funciona).")
    if not IS_MAC and not IS_WINDOWS and not (shutil.which("make") and (shutil.which("gcc") or shutil.which("clang"))):
        raise RuntimeError("Para compilar el decompilador faltan make y gcc/clang (p. ej. sudo apt install build-essential).")
    gdir = os.path.join(home, "support", "gradle")
    gradlew = os.path.join(gdir, "gradlew.bat" if IS_WINDOWS else "gradlew")
    if not IS_WINDOWS:
        os.chmod(gradlew, 0o755)
    env = {**os.environ, "JAVA_HOME": java,
           "PATH": os.path.join(java, "bin") + os.pathsep + os.environ.get("PATH", ""),
           "GRADLE_USER_HOME": os.path.join(DATA_DIR, "gradle")}
    res = subprocess.run([gradlew, "buildNatives", "--no-daemon", "-q"], cwd=gdir, env=env,
                         stdin=subprocess.DEVNULL, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=3600)
    if res.returncode != 0 or not _natives_ok(home):
        raise RuntimeError("No se pudo compilar el decompilador: " + ((res.stderr or "") + (res.stdout or ""))[-1500:])


def _install_worker() -> None:
    st = _install_state
    try:
        os.makedirs(TOOLS_DIR, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix="dl-", dir=TOOLS_DIR)
        try:
            # Primero Ghidra: cada versión pide un Java mínimo (12.2 → JDK 25).
            if not find_ghidra():
                st.update(state="running", detail="Buscando la última versión de Ghidra…")
                name, url, digest, tag = _latest_ghidra()
                arc = os.path.join(tmp, name)
                _download(url, arc, digest, f"Ghidra ({tag})")
                st["detail"] = "Descomprimiendo Ghidra (puede tardar un par de minutos)…"
                _extract(arc, TOOLS_DIR)
                os.remove(arc)
            need = _java_needed()
            if not find_java():
                arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
                osname = "windows" if IS_WINDOWS else ("mac" if IS_MAC else "linux")
                st.update(state="running", detail=f"Buscando el JDK {need} (Eclipse Temurin)…")
                info = _get_json(f"https://api.adoptium.net/v3/assets/latest/{need}/hotspot"
                                 f"?architecture={arch}&image_type=jdk&os={osname}&vendor=eclipse")
                pkg = info[0]["binary"]["package"]
                arc = os.path.join(tmp, pkg["name"])
                _download(pkg["link"], arc, pkg.get("checksum"), f"Java (JDK {need})")
                st["detail"] = "Descomprimiendo Java…"
                _extract(arc, TOOLS_DIR)
                os.remove(arc)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        g, j = find_ghidra(), find_java()
        if g and j and not _natives_ok(g):
            # Ghidra oficial no trae el decompilador para macOS (ni Linux ARM):
            # se compila aquí con su propio Gradle y las herramientas de Xcode.
            st.update(state="running", detail="Compilando el decompilador de Ghidra para este equipo (unos minutos)…")
            _build_natives(g, j)
        if g and j:
            st.update(state="done", detail=f"Listo: Ghidra {_ghidra_version(g)} en {g}; Java {_java_major(j)} en {j}"
                      + ("" if _natives_ok(g) else " (sin decompilador nativo: decompile no funcionará)"))
        else:
            st.update(state="error", detail="La instalación terminó, pero no se encuentra Ghidra o Java.")
    except Exception as e:  # noqa: BLE001
        st.update(state="error", detail=f"No se pudo instalar: {e}")
    finally:
        st["finished"] = time.time()


# ─── Ejecutar Ghidra headless ────────────────────────────────────────────────

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _not_ready() -> str | None:
    if not find_ghidra():
        return ("Ghidra no está instalado (ni GHIDRA_HOME apunta a él). Usa la herramienta "
                "ghidra_install para descargarlo dentro de RUFFS (~500 MB, unos minutos), "
                "o instala Ghidra tú y pon GHIDRA_HOME en el entorno de este servidor MCP.")
    if not find_java():
        return (f"Falta Java {_java_needed()} o posterior (JDK). Usa ghidra_install para descargarlo dentro de "
                "RUFFS, o instala el JDK que pide Ghidra (25 para Ghidra 12.2) y pon JAVA_HOME en el entorno de este servidor MCP.")
    return None


def _run(filepath: str, request: dict, timeout: int = 600) -> dict:
    problem = _not_ready()
    if problem:
        return {"error": problem}
    filepath = os.path.abspath(os.path.expanduser(filepath or ""))
    if not os.path.isfile(filepath):
        return {"error": f"No existe el archivo: {filepath}"}
    ghidra, java = find_ghidra(), find_java()
    digest = _sha256_file(filepath)
    proj = os.path.join(CACHE_DIR, digest[:20])
    os.makedirs(proj, exist_ok=True)
    marker = os.path.join(proj, "ready.json")
    # Copia con un nombre seguro: así ni la ruta ni el nombre del binario pasan
    # por el intérprete de analyzeHeadless.bat.
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filepath))[:80] or "binario"
    with _lock:
        fd, out_json = tempfile.mkstemp(suffix=".json", dir=proj)
        os.close(fd)
        req_file = out_json[:-5] + ".properties"
        with open(req_file, "w", encoding="utf-8") as f:
            for k, v in request.items():
                v = str(v).replace("\\", "\\\\").replace("\n", " ").replace("\r", " ")
                f.write(f"{k}={v}\n")
        cmd = [_headless(ghidra), proj, "proyecto"]
        if os.path.exists(marker):
            cmd += ["-process", json.load(open(marker, encoding="utf-8"))["program"], "-noanalysis", "-readOnly"]
        else:
            src_dir = os.path.join(proj, "entrada")
            os.makedirs(src_dir, exist_ok=True)
            src = os.path.join(src_dir, safe)
            shutil.copyfile(filepath, src)
            cmd += ["-import", src, "-overwrite"]
        cmd += ["-scriptPath", SCRIPT_DIR, "-postScript", "RuffsExport.java", out_json, req_file,
                "-log", os.path.join(proj, "ghidra.log"), "-scriptlog", os.path.join(proj, "script.log")]
        env = {**os.environ, "JAVA_HOME": java, "GHIDRA_HOME": ghidra,
               # Ajustes y caché propios: no tocan (ni chocan con) el Ghidra
               # que el usuario tenga abierto con su propia configuración.
               "XDG_CONFIG_HOME": os.path.join(DATA_DIR, "home", "config"),
               "XDG_CACHE_HOME": os.path.join(DATA_DIR, "home", "cache")}
        env["PATH"] = os.path.join(java, "bin") + os.pathsep + env.get("PATH", "")
        kw: dict = {}
        if IS_WINDOWS:
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        try:
            res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=timeout, env=env, **kw)
        except subprocess.TimeoutExpired:
            return {"error": f"Ghidra no terminó en {timeout} s (binario grande). Prueba con un timeout mayor."}
        finally:
            try:
                os.remove(req_file)
            except OSError:
                pass
        data = None
        try:
            if os.path.getsize(out_json) > 0:
                with open(out_json, encoding="utf-8") as f:
                    data = json.load(f)
        except (OSError, ValueError):
            data = None
        finally:
            try:
                os.remove(out_json)
            except OSError:
                pass
        if data is None:
            tail = ((res.stdout or "") + "\n" + (res.stderr or ""))[-3000:]
            return {"error": "Ghidra no devolvió resultado.", "return_code": res.returncode, "log": tail}
        if not os.path.exists(marker):
            with open(marker, "w", encoding="utf-8") as f:
                json.dump({"program": safe, "source": filepath, "analyzed": time.time()}, f)
            shutil.rmtree(os.path.join(proj, "entrada"), ignore_errors=True)
        return data


def _out(obj: object) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=1)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n… (recortado, {len(text)} caracteres en total; usa filter/offset/limit)"
    return text


# ─── Herramientas MCP ────────────────────────────────────────────────────────

@mcp.tool()
def ghidra_status() -> str:
    """Estado de Ghidra y Java para este servidor: dónde están, versiones, caché y
    progreso de ghidra_install si se está instalando."""
    g, j = find_ghidra(), find_java()
    return _out({
        "ghidra_home": g, "ghidra_version": _ghidra_version(g) if g else None,
        "java_home": j, "java_version": _java_major(j) if j else None,
        "java_needed": _java_needed(g),
        "ready": bool(g and j),
        "decompiler": _natives_ok(g),
        "install": _install_state,
        "cache_dir": CACHE_DIR,
        "platform": f"{platform.system()} {platform.machine()}",
        "hint": None if (g and j) else _not_ready(),
    })


@mcp.tool()
def ghidra_install() -> str:
    """Descarga e instala Ghidra (última versión oficial de GitHub) y, si falta, un
    JDK que pida esa versión (Eclipse Temurin) dentro de RUFFS, sin permisos de administrador.
    Comprueba el SHA-256 de cada descarga. Corre en segundo plano (~500 MB):
    consulta el progreso con ghidra_status."""
    global _install_thread
    if find_ghidra() and find_java() and _natives_ok(find_ghidra()):
        return _out({"state": "done", "detail": "Ghidra, Java y el decompilador ya están disponibles.",
                     "ghidra_home": find_ghidra(), "java_home": find_java()})
    if _install_thread and _install_thread.is_alive():
        return _out(_install_state)
    _install_state.clear()
    _install_state.update(state="running", detail="Empezando…", started=time.time())
    _install_thread = threading.Thread(target=_install_worker, name="ghidra-install", daemon=True)
    _install_thread.start()
    return _out({"state": "running", "detail": "Instalación iniciada en segundo plano. "
                 "Consulta ghidra_status cada minuto o dos hasta que diga ready: true."})


@mcp.tool()
def binary_info(filepath: str) -> str:
    """Datos básicos de un archivo binario sin Ghidra: formato (PE/ELF/Mach-O…),
    arquitectura, tamaño y hashes MD5/SHA-256."""
    path = os.path.abspath(os.path.expanduser(filepath))
    if not os.path.isfile(path):
        return _out({"error": f"No existe el archivo: {path}"})
    with open(path, "rb") as f:
        head = f.read(4096)
    info: dict = {"file": path, "size_bytes": os.path.getsize(path), "magic": head[:4].hex()}
    if head[:2] == b"MZ":
        info["format"] = "PE (Windows)"
        try:
            pe = int.from_bytes(head[0x3C:0x40], "little")
            if head[pe:pe + 4] == b"PE\0\0":
                machine = int.from_bytes(head[pe + 4:pe + 6], "little")
                info["arch"] = {0x14C: "x86", 0x8664: "x86-64", 0xAA64: "ARM64", 0x1C0: "ARM"}.get(machine, hex(machine))
                chars = int.from_bytes(head[pe + 22:pe + 24], "little")
                info["is_dll"] = bool(chars & 0x2000)
        except Exception:  # noqa: BLE001
            pass
    elif head[:4] == b"\x7fELF":
        info["format"] = "ELF (Linux/Unix)"
        info["bits"] = 64 if head[4] == 2 else 32
        machine = int.from_bytes(head[18:20], "little" if head[5] == 1 else "big")
        info["arch"] = {3: "x86", 62: "x86-64", 40: "ARM", 183: "ARM64", 243: "RISC-V", 8: "MIPS"}.get(machine, str(machine))
    elif head[:4] in (b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe"):
        info["format"] = "Mach-O (macOS/iOS)"
    elif head[:4] in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
        info["format"] = "Mach-O universal (fat) o clase Java"
    elif head[:4] == b"dex\n":
        info["format"] = "Dalvik DEX (Android)"
    elif head[:4] == b"\0asm":
        info["format"] = "WebAssembly"
    elif head[:2] == b"PK":
        info["format"] = "ZIP (APK/JAR/IPA u otro)"
    else:
        info["format"] = "desconocido"
    md5, sha = hashlib.md5(), hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            md5.update(chunk)
            sha.update(chunk)
    info["md5"], info["sha256"] = md5.hexdigest(), sha.hexdigest()
    return _out(info)


@mcp.tool()
def extract_strings(filepath: str, min_length: int = 5, filter: str = "", limit: int = 300) -> str:
    """Cadenas ASCII y UTF-16 de un binario sin Ghidra (rápido). Destaca URLs, IPs
    y rutas. ``filter`` busca texto (sin distinguir mayúsculas)."""
    path = os.path.abspath(os.path.expanduser(filepath))
    if not os.path.isfile(path):
        return _out({"error": f"No existe el archivo: {path}"})
    with open(path, "rb") as f:
        data = f.read(200 * 1024 * 1024)
    n = max(3, int(min_length))
    found = [m.decode("ascii") for m in re.findall(rb"[\x20-\x7e]{%d,}" % n, data)]
    found += [m.decode("utf-16le") for m in re.findall(rb"(?:[\x20-\x7e]\x00){%d,}" % n, data)]
    found = list(dict.fromkeys(found))
    flt = (filter or "").lower()
    if flt:
        found = [s for s in found if flt in s.lower()]
    return _out({
        "total": len(found),
        "urls": [s for s in found if re.search(r"https?://|ftp://", s)][:100],
        "ips": [s for s in found if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}(:\d+)?", s)][:100],
        "paths": [s for s in found if re.search(r"[A-Za-z]:\\|^/(usr|etc|var|tmp|home|Users|Library|System)/", s)][:100],
        "strings": found[:max(1, int(limit))],
    })


@mcp.tool()
def analyze(filepath: str, timeout: int = 900) -> str:
    """Analiza un binario con Ghidra (la primera vez tarda: de segundos a varios
    minutos según el tamaño; luego queda en caché) y devuelve el resumen: formato,
    procesador, compilador, base, nº de funciones, secciones y puntos de entrada."""
    return _out(_run(filepath, {"mode": "summary"}, timeout))


@mcp.tool()
def list_functions(filepath: str, filter: str = "", offset: int = 0, limit: int = 200) -> str:
    """Funciones que encontró Ghidra (nombre, dirección, tamaño, firma). Paginado
    con offset/limit; ``filter`` busca en el nombre."""
    return _out(_run(filepath, {"mode": "functions", "filter": filter, "offset": offset, "limit": limit}))


@mcp.tool()
def decompile(filepath: str, function: str) -> str:
    """Decompila a pseudocódigo C una función, indicada por nombre (p. ej. "main",
    "FUN_00401000") o por una dirección que esté dentro de ella (p. ej. "0x401000")."""
    res = _run(filepath, {"mode": "decompile", "target": function})
    if isinstance(res, dict) and res.get("error") and not _natives_ok(find_ghidra()):
        res["hint"] = ("Este Ghidra no tiene decompilador para " + _native_platform()
                       + ": llama a ghidra_install para compilarlo (en Mac necesita las herramientas de Xcode).")
    return _out(res)


@mcp.tool()
def disassemble(filepath: str, function: str, limit: int = 400) -> str:
    """Desensamblado de una función (nombre o dirección), instrucción a instrucción."""
    return _out(_run(filepath, {"mode": "disassemble", "target": function, "limit": limit}))


@mcp.tool()
def list_imports(filepath: str, filter: str = "", limit: int = 500) -> str:
    """Funciones importadas (símbolos externos) con su biblioteca."""
    return _out(_run(filepath, {"mode": "imports", "filter": filter, "limit": limit}))


@mcp.tool()
def list_exports(filepath: str, filter: str = "", limit: int = 500) -> str:
    """Símbolos exportados y puntos de entrada."""
    return _out(_run(filepath, {"mode": "exports", "filter": filter, "limit": limit}))


@mcp.tool()
def list_strings(filepath: str, filter: str = "", limit: int = 300) -> str:
    """Cadenas que Ghidra identificó como datos, con su dirección (para luego
    buscar quién las usa con xrefs)."""
    return _out(_run(filepath, {"mode": "strings", "filter": filter, "limit": limit}))


@mcp.tool()
def xrefs(filepath: str, target: str, limit: int = 200) -> str:
    """Referencias cruzadas hacia una dirección o función: desde dónde se llama o
    se usa, con el tipo de referencia y la función que la contiene."""
    return _out(_run(filepath, {"mode": "xrefs", "target": target, "limit": limit}))


@mcp.tool()
def clear_cache(filepath: str = "") -> str:
    """Borra los análisis guardados: el de un binario (filepath) o todos."""
    if filepath:
        path = os.path.abspath(os.path.expanduser(filepath))
        if not os.path.isfile(path):
            return _out({"error": f"No existe el archivo: {path}"})
        target = os.path.join(CACHE_DIR, _sha256_file(path)[:20])
    else:
        target = CACHE_DIR
    shutil.rmtree(target, ignore_errors=True)
    return _out({"cleared": target})


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    mcp.run()
