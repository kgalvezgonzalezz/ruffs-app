"""RUFFS: HTTPS a GitHub aunque el almacén del sistema no tenga la cadena de
certificados (error real: CERTIFICATE_VERIFY_FAILED «unable to get issuer
certificate» al buscar actualizaciones)."""
import ssl
import urllib.error
import urllib.request

import pytest

URL = "https://api.github.com/zen"


def _online():
    try:
        urllib.request.urlopen(URL, timeout=10)
        return True
    except Exception:
        return False


@pytest.fixture()
def empty_system_store(monkeypatch):
    """Simula un equipo cuyo almacén de certificados no sirve para GitHub."""
    real = ssl.create_default_context

    def no_cas(*a, **k):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)   # verifica, pero sin ninguna CA
        return ctx
    monkeypatch.setattr(ssl, "create_default_context", no_cas)
    import src.https as h
    h.ssl_context.cache_clear()
    yield
    monkeypatch.setattr(ssl, "create_default_context", real)
    h.ssl_context.cache_clear()


@pytest.mark.skipif(not _online(), reason="sin conexión")
def test_github_works_without_system_certificates(empty_system_store):
    # Sin el arreglo: el mismo error que vio el usuario.
    with pytest.raises(urllib.error.URLError) as e:
        urllib.request.urlopen(URL, timeout=15, context=ssl.create_default_context())
    assert "CERTIFICATE_VERIFY_FAILED" in str(e.value)
    # Con el arreglo (sistema + certifi): funciona.
    from src.https import urlopen
    try:
        with urlopen(URL, timeout=15) as r:
            assert r.status == 200
    except urllib.error.HTTPError as err:
        # GitHub limita las consultas sin sesión desde los equipos compartidos de CI
        # (403 «rate limit exceeded»). Una respuesta HTTP ya prueba que el
        # certificado se verificó: lo que comprueba este test.
        assert err.code in (403, 429), err


def test_context_still_verifies():
    from src.https import ssl_context
    ctx = ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
