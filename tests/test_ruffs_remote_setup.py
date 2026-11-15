"""RUFFS: with RUFFS on 0.0.0.0 (Tailscale/LAN) the first admin account can
only be created from the computer itself."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from routes.auth_routes import SetupRequest, setup_auth_routes


def _setup_endpoint(configured=False):
    auth = MagicMock()
    auth.is_configured = configured
    auth.setup.return_value = True
    router = setup_auth_routes(auth)
    for route in router.routes:
        if getattr(route, "path", "") == "/api/auth/setup":
            return auth, route.endpoint
    raise AssertionError("setup route not registered")


def _req(host):
    return SimpleNamespace(client=SimpleNamespace(host=host), cookies={})


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_first_admin_from_this_computer(host, monkeypatch):
    monkeypatch.delenv("RUFFS_ALLOW_REMOTE_SETUP", raising=False)
    auth, ep = _setup_endpoint()
    out = asyncio.run(ep(body=SetupRequest(username="admin", password="secreto123"), request=_req(host)))
    assert out["ok"] is True
    auth.setup.assert_called_once()


@pytest.mark.parametrize("host", ["100.101.102.103", "192.168.1.20"])  # Tailscale, LAN
def test_first_admin_from_network_is_refused(host, monkeypatch):
    monkeypatch.delenv("RUFFS_ALLOW_REMOTE_SETUP", raising=False)
    auth, ep = _setup_endpoint()
    with pytest.raises(HTTPException) as e:
        asyncio.run(ep(body=SetupRequest(username="intruso", password="secreto123"), request=_req(host)))
    assert e.value.status_code == 403
    auth.setup.assert_not_called()


def test_remote_setup_escape_hatch(monkeypatch):
    monkeypatch.setenv("RUFFS_ALLOW_REMOTE_SETUP", "true")
    auth, ep = _setup_endpoint()
    out = asyncio.run(ep(body=SetupRequest(username="admin", password="secreto123"), request=_req("100.101.102.103")))
    assert out["ok"] is True
