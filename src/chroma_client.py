"""
chroma_client.py

Singleton ChromaDB client.
Connects to a ChromaDB instance running as a standalone service first.
If the HTTP server is not reachable, falls back to embedded PersistentClient
(local SQLite, no external service needed).
"""

import os
import socket
import logging

logger = logging.getLogger(__name__)

_client = None

# A short connect probe so an unreachable ChromaDB fails fast instead of
# blocking on the OS connection timeout (~30-60s, WinError 10060 on Windows),
# which otherwise stalls app startup. Tunable via CHROMADB_CONNECT_TIMEOUT.
_CONNECT_TIMEOUT = float(os.getenv("CHROMADB_CONNECT_TIMEOUT", "2.0"))


def _port_open(host: str, port: int, timeout: float = None) -> bool:
    """Return True if a TCP connection to host:port succeeds within timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout or _CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


def get_chroma_client():
    """Get or create the singleton ChromaDB client.

    First tries to connect to a remote ChromaDB HTTP server (host:port from
    CHROMADB_HOST / CHROMADB_PORT env vars, default localhost:8100).
    If the remote server is not reachable, falls back to an embedded local
    PersistentClient (SQLite-based, no external service required).

    Raises RuntimeError with a clear install hint if the `chromadb` package
    is not installed -- it's an optional dependency (RAG + memory vectors).
    """
    global _client
    if _client is not None:
        return _client

    try:
        import chromadb
    except ImportError as e:
        raise RuntimeError(
            "ChromaDB integration is not installed. Install the optional "
            "dependency with: pip install chromadb"
        ) from e

    # RUFFS: por defecto, ChromaDB embebido en data/chromadb (sin proceso aparte).
    # Solo se usa un servidor HTTP si se configura explícitamente. Así RUFFS
    # nunca se conecta por accidente a la ChromaDB de Odysseus (puerto 8100).
    explicit_server = bool(os.getenv("CHROMADB_HOST") or os.getenv("CHROMADB_PORT"))
    host = os.getenv("CHROMADB_HOST", "localhost")
    port = int(os.getenv("CHROMADB_PORT", "8100"))

    if explicit_server:
        # Servidor configurado a propósito: si no responde, es un error (no se
        # cae al modo embebido, que guardaría la memoria en otro sitio). No se
        # cachea el fallo para que una llamada posterior pueda conectar.
        if not _port_open(host, port):
            raise RuntimeError(f"ChromaDB server {host}:{port} is not reachable")
        try:
            client = chromadb.HttpClient(host=host, port=port)
            # Health check before caching -- if the port is open but the service isn't
            # healthy yet (e.g. still starting), don't poison the singleton with a dead
            # client; leave _client unset so the next call retries.
            client.heartbeat()
            _client = client
            logger.info(f"ChromaDB connected: {host}:{port} (HTTP)")
            return _client
        except Exception as e:
            raise RuntimeError(f"ChromaDB server {host}:{port} is not healthy: {e}") from e

    # Fallback: embedded PersistentClient (SQLite local, no server needed)
    db_path = os.getenv("CHROMADB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "chromadb"))
    db_path = os.path.abspath(db_path)
    os.makedirs(db_path, exist_ok=True)
    client = chromadb.PersistentClient(path=db_path)
    _client = client
    logger.info(f"ChromaDB connected: embedded PersistentClient at {db_path}")
    return _client


def reset_client():
    """Reset the singleton (e.g. after config change)."""
    global _client
    _client = None
