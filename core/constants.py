# src/constants.py
"""Application-wide constants and configuration values."""
import os

APP_VERSION = "0.9.1"

# Base paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/"
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")

# Data file paths
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")
MEMORY_DOC = os.path.join(DATA_DIR, "memory_doc.md")
PERSONAL_DIR = os.path.join(DATA_DIR, "personal_docs")
RUNBOOK_DIR = os.path.join(PERSONAL_DIR, "runbook")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
FEATURES_FILE = os.path.join(DATA_DIR, "features.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

# API Configuration
MAX_CONTEXT_MESSAGES = 90
REQUEST_TIMEOUT = 20
OPENAI_COMPAT_PATH = "/v1/chat/completions"

# Environment variables with defaults
DEFAULT_HOST = os.getenv("LLM_HOST", "localhost")
LLM_HOSTS = [h.strip() for h in os.getenv("LLM_HOSTS", "").split(",") if h.strip()]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SEARXNG_INSTANCE = os.getenv('SEARXNG_INSTANCE', 'http://localhost:8080')


# Cleanup configuration
CLEANUP_ENABLED = os.getenv("CLEANUP_ENABLED", "True").lower() == "true"
CLEANUP_INTERVAL_HOURS = int(os.getenv("CLEANUP_INTERVAL_HOURS", "24"))

# Default parameters
DEFAULT_TEMPERATURE = 1.0
DEFAULT_MAX_TOKENS = 0


# ─── RUFFS: puerto real del servidor (una sola fuente para el loopback) ─────
# Las herramientas del agente se llaman a sí mismas por HTTP (app_api, Deep
# Research, galería, contactos…). Antes apuntaban a un puerto fijo heredado de
# Odysseus (7000) y fallaban con «All connection attempts failed» porque RUFFS
# escucha en 7100 (o en el --port con que se arranque).
DEFAULT_PORT = 7100


def server_port() -> int:
    """Puerto en el que escucha ESTE servidor.

    Orden: RUFFS_SERVER_PORT (lo fija ruffs_desktop.py con su --port) → --port
    de la línea de órdenes (uvicorn app:app --port N) → APP_PORT de .env → 7100.
    """
    import sys as _sys
    env = os.environ.get("RUFFS_SERVER_PORT", "").strip()
    if env.isdigit():
        return int(env)
    argv = list(_sys.argv or [])
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv) and argv[i + 1].isdigit():
            return int(argv[i + 1])
        if a.startswith("--port=") and a[7:].isdigit():
            return int(a[7:])
    app_port = os.environ.get("APP_PORT", "").strip()
    return int(app_port) if app_port.isdigit() else DEFAULT_PORT


def loopback_base() -> str:
    """URL base para llamar al propio servidor (IPv4 directo: sin intento IPv6)."""
    return f"http://127.0.0.1:{server_port()}"
