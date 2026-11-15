#!/usr/bin/env python3
"""Exporta una copia limpia de RUFFS para los instaladores (Windows / macOS).

Toma SOLO lo que está confirmado en git (``git archive``): nada de ``data/``,
``.env``, bases de datos, registros, caché ni historial de commits. Después
comprueba que la copia no contiene datos personales y falla si encuentra:

  * archivos de datos/secretos (data/, .env, *.db, *.sqlite*, *.log, logs/,
    uploads/, .venv/, copias *.bak*, claves *.pem/*.key…);
  * claves de API con formato conocido (OpenRouter, Anthropic, OpenAI,
    GitHub, AWS, claves privadas PEM);
  * el correo de git de quien exporta o rutas de su carpeta personal
    (C:\\Users\\<usuario>, /Users/<usuario>, /home/<usuario>).

Uso:
    python scripts/exportar_limpio.py --out build/ruffs [--ref HEAD] [--zip]
"""
from __future__ import annotations

import argparse
import fnmatch
import getpass
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Lo que no hace falta para usar RUFFS (desarrollo, CI, Docker).
EXCLUDE_DIRS = {"tests", ".github", "docker", ".claude", "dist", "build", "installer"}
EXCLUDE_FILES = {"Dockerfile", ".dockerignore", "start-macos.sh", "build-macos-app.sh"}
# Las guías .docx van en la Release y en el repositorio, no dentro del instalador.
EXCLUDE_PREFIXES = ("docs/guias/",)

FORBIDDEN_PATTERNS = [
    "data/*", ".env", ".env.*", "*.db", "*.sqlite", "*.sqlite3", "*.log",
    "logs/*", "uploads/*", ".venv/*", "venv/*", "*.bak", "*.bak-*", "*.pem",
    "*.key", "license.lic", ".app_key", "datos_chroma/*", "services/cache/content/*",
    "auto_titles.json", "*.pyc",
]
ALLOWED = {".env.example"}

SECRET_RES = [
    re.compile(r"sk-or-v1-[A-Za-z0-9]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
TEXT_EXT = {".py", ".js", ".mjs", ".css", ".html", ".md", ".txt", ".json", ".yml",
            ".yaml", ".toml", ".ini", ".cfg", ".sh", ".ps1", ".bat", ".command",
            ".iss", ".example", ".plist", ".svg", ""}


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _personal_markers() -> list[str]:
    """Strings that would identify the person running the export."""
    marks = []
    try:
        email = _git("config", "user.email")
        if email:
            marks.append(email)
    except Exception:
        pass
    users = {getpass.getuser(), os.environ.get("USERNAME", ""), os.environ.get("USER", "")}
    for u in filter(None, users):
        marks += [f"Users\\{u}\\", f"Users/{u}/", f"/home/{u}/"]
    return [m for m in marks if len(m) > 3]


def export(out: Path, ref: str) -> Path:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    data = subprocess.check_output(["git", "archive", "--format=tar", ref], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members = []
        for m in tar.getmembers():
            top = m.name.split("/", 1)[0]
            if top in EXCLUDE_DIRS or m.name in EXCLUDE_FILES or m.name.startswith(EXCLUDE_PREFIXES):
                continue
            members.append(m)
        tar.extractall(out, members=members)
    sha = _git("rev-parse", "--short", ref)
    (out / "RUFFS_BUILD").write_text(f"{sha} {time.strftime('%Y-%m-%d %H:%M')}\n", encoding="utf-8")
    return out


def verify(out: Path) -> list[str]:
    problems = []
    markers = _personal_markers()
    for path in out.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(out).as_posix()
        name = path.name
        if name not in ALLOWED and any(
            fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat) for pat in FORBIDDEN_PATTERNS
        ):
            problems.append(f"archivo no permitido: {rel}")
            continue
        if path.suffix.lower() not in TEXT_EXT or path.stat().st_size > 5_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rx in SECRET_RES:
            if rx.search(text):
                problems.append(f"posible clave secreta en {rel} ({rx.pattern[:24]}…)")
        for mark in markers:
            if mark.lower() in text.lower():
                problems.append(f"dato personal del usuario que exporta en {rel}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="carpeta de destino (se vacía)")
    ap.add_argument("--ref", default="HEAD", help="commit/rama a exportar (por defecto HEAD)")
    ap.add_argument("--zip", action="store_true", help="crear también <out>.zip")
    args = ap.parse_args()

    out = export(Path(args.out).resolve(), args.ref)
    problems = verify(out)
    if problems:
        print("ERROR: la copia contiene datos que no deben distribuirse:", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        shutil.rmtree(out, ignore_errors=True)
        return 1
    n = sum(1 for p in out.rglob("*") if p.is_file())
    print(f"Copia limpia de RUFFS: {out} ({n} archivos, sin datos de usuario)")
    if args.zip:
        z = shutil.make_archive(str(out), "zip", root_dir=out)
        print("Zip:", z)
    return 0


if __name__ == "__main__":
    sys.exit(main())
