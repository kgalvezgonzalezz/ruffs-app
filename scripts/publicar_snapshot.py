"""Copia pública de RUFFS para el repositorio público (la usa .github/workflows/publicar.yml).

    python scripts/publicar_snapshot.py --out CARPETA [--ref HEAD]

Toma SOLO lo confirmado en git (``git archive``), sin el flujo de publicación
(que es del repositorio privado), y se NIEGA a continuar si encuentra algo que
no debería ser público:

  * claves de API o privadas (OpenRouter, OpenAI, GitHub, AWS, Google, Slack,
    bloques PRIVATE KEY) y archivos de firma/claves (.pem, .p12, .jks, .env…);
  * rutas de carpetas personales (C:\\Users\\<nombre>, /Users/<nombre>/) que no
    sean genéricas;
  * correos de proveedores reales (gmail, outlook, icloud…) que no sean ejemplos;
  * direcciones IP de Tailscale (100.64.0.0/10) que no estén en la lista de ejemplos;
  * autor o «modificado por» en los metadatos de documentos de Office.

Sale con código 1 y la lista de problemas si algo falla.
"""
from __future__ import annotations

import argparse
import fnmatch
import io
import ipaddress
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EXCLUDE = {".github/workflows/publicar.yml"}
FORBIDDEN_FILES = ["*.pem", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.key", ".env", "id_rsa*",
                   "id_ed25519*", "*.db", "*.sqlite", "*.sqlite3"]
KEY_PATTERNS = [
    r"sk-or-v1-[A-Za-z0-9]{20,}", r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}", r"ghp_[A-Za-z0-9]{30,}",
    r"github_pat_[A-Za-z0-9_]{30,}", r"AKIA[0-9A-Z]{16}", r"AIza[0-9A-Za-z_-]{35}",
    r"xox[bpas]-[0-9A-Za-z-]{10,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
]
GENERIC_USERS = {"runner", "you", "your", "user", "username", "usuario", "tu", "tunombre", "name",
                 "nombre", "me", "admin", "public", "default", "shared", "example", "test", "foo",
                 "x", "a", "b", "alice", "bob", "jane", "john", "<user>", "%username%", "$user",
                 "models", "ubuntu", "app", "ruffs", "odysseus", "linuxbrew"}
REAL_PROVIDERS = r"gmail|googlemail|outlook|hotmail|live|icloud|me|mac|yahoo|proton|protonmail|aol|gmx"
PLACEHOLDER_LOCALS = {"you", "your", "user", "me", "name", "nombre", "usuario", "example", "test",
                      "someone", "alguien", "tu", "foo"}
# Ejemplos de la documentación y de los tests heredados de Odysseus (proyecto público).
EXAMPLE_IPS = {"100.64.0.0", "100.64.0.1", "100.127.255.254", "100.101.102.103", "100.88.77.66",
               "100.100.100.100", "100.64.0.10", "100.127.255.255", "100.117.136.97",
               "100.100.50.20", "100.64.0.5"}
TEXT_EXT = {".py", ".js", ".mjs", ".ts", ".json", ".md", ".txt", ".html", ".css", ".yml", ".yaml",
            ".toml", ".cfg", ".ini", ".ps1", ".bat", ".sh", ".command", ".iss", ".xml", ".plist",
            ".kt", ".kts", ".java", ".swift", ".gradle", ".properties", ".svg", ".env.example", ""}


def export(ref: str, out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    data = subprocess.check_output(["git", "archive", "--format=tar", ref], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(out, members=[m for m in tar.getmembers() if m.name not in EXCLUDE])


def _scan_text(rel: str, text: str, problems: list[str]) -> None:
    for pat in KEY_PATTERNS:
        if re.search(pat, text):
            problems.append(f"{rel}: posible clave ({pat[:20]}…)")
    for m in re.finditer(r"(?i)(?:[A-Z]:\\{1,2}Users\\{1,2}|/Users/|/home/)([A-Za-z0-9._ -]{2,40}?)(?=[\\/\"'\s]|$)", text):
        user = m.group(1).strip().lower()
        if user not in GENERIC_USERS and not user.startswith(("<", "%", "$", "{")):
            problems.append(f"{rel}: ruta de carpeta personal «{m.group(0)[:60]}»")
    for m in re.finditer(r"([A-Za-z0-9._%+-]+)@(" + REAL_PROVIDERS + r")\.[a-z.]{2,6}\b", text, re.I):
        if m.group(1).lower() not in PLACEHOLDER_LOCALS:
            problems.append(f"{rel}: correo real «{m.group(0)}»")
    for m in re.finditer(r"\b100\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b(?!/)", text):
        ip = m.group(0)
        try:
            if ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10") and ip not in EXAMPLE_IPS:
                problems.append(f"{rel}: IP de Tailscale «{ip}» (usa una de ejemplo)")
        except ValueError:
            pass


def _scan_office(rel: str, path: Path, problems: list[str]) -> None:
    try:
        with zipfile.ZipFile(path) as z:
            core = z.read("docProps/core.xml").decode("utf-8", "ignore") if "docProps/core.xml" in z.namelist() else ""
    except zipfile.BadZipFile:
        return
    for tag in ("dc:creator", "cp:lastModifiedBy"):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", core)
        if m and m.group(1).strip() not in ("", "RUFFS"):
            problems.append(f"{rel}: metadato {tag} = «{m.group(1)}» (déjalo en RUFFS)")


def scan(out: Path) -> list[str]:
    problems: list[str] = []
    for p in out.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(out).as_posix()
        if rel.endswith(".env.example"):
            pass
        elif any(fnmatch.fnmatch(p.name, pat) for pat in FORBIDDEN_FILES):
            problems.append(f"{rel}: tipo de archivo que no debe publicarse")
            continue
        if p.suffix.lower() in (".docx", ".xlsx", ".pptx"):
            _scan_office(rel, p, problems)
            continue
        if p.suffix.lower() not in TEXT_EXT or p.name == "package-lock.json" or p.stat().st_size > 3_000_000:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        _scan_text(rel, text, problems)
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--ref", default="HEAD")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    export(a.ref, out)
    problems = scan(out)
    if problems:
        print("NO se publica: hay datos que no deben ser públicos:")
        for p in problems[:200]:
            print("  -", p)
        return 1
    n = sum(1 for p in out.rglob("*") if p.is_file())
    print(f"Copia pública lista en {out} ({n} archivos, revisada)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
