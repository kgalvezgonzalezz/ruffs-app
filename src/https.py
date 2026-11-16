"""RUFFS: conexiones HTTPS fiables desde Python.

Python valida los certificados con OpenSSL y el almacén del sistema. En algunos
equipos eso no basta (p. ej. Python de uv/python.org en macOS sin certificados
instalados, o Windows sin un certificado intermedio que el navegador sí obtiene
solo) y GitHub falla con «CERTIFICATE_VERIFY_FAILED: unable to get issuer
certificate» aunque el navegador funcione.

Aquí se usan a la vez los certificados del sistema y los de ``certifi`` (que
RUFFS ya instala), así funciona en los dos casos sin desactivar la verificación.
"""
from __future__ import annotations

import ssl
import urllib.request
from functools import lru_cache


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()          # almacén del sistema
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())   # + Mozilla (certifi)
    except Exception:  # noqa: BLE001  (sin certifi: solo el sistema)
        pass
    return ctx


def urlopen(req, timeout: float = 30):
    """urllib.request.urlopen con certificados del sistema + certifi."""
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())
