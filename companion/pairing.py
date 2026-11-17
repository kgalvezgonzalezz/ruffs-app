"""Shared pairing helpers for the companion bridge.

Token minting + LAN discovery + QR rendering, kept here as small, importable
units so the route layer stays thin and the logic is directly testable.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import shutil
import socket
import subprocess
import uuid

import bcrypt

PAIRING_VERSION = 1
COMPANION_SCOPE = "chat"


def default_port() -> int:
    """Best guess at the port the server is reachable on. Callers that know the
    real request port should pass it explicitly."""
    try:
        from core.constants import server_port
        return server_port()
    except Exception:  # noqa: BLE001
        return 7100


_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def is_tailscale_ip(ip: str) -> bool:
    """True for a Tailscale (CGNAT 100.64.0.0/10) address."""
    try:
        return ipaddress.ip_address(ip) in _TAILSCALE_NET
    except ValueError:
        return False


def tailscale_ipv4() -> str | None:
    """This machine's Tailscale IPv4 via `tailscale ip -4`, or None.

    getaddrinfo() usually lists it too, but not on every OS/config, and it is
    the address that matters most for a phone: it works on and off the home
    Wi-Fi. Best-effort, short timeout, never raises.
    """
    exe = shutil.which("tailscale")
    if not exe and os.name == "nt":
        default = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Tailscale", "tailscale.exe")
        exe = default if os.path.exists(default) else None
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "ip", "-4"], capture_output=True, text=True, timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        ip = line.strip()
        if is_tailscale_ip(ip):
            return ip
    return None


def lan_ip_candidates() -> list[str]:
    """IPv4 addresses a phone could use to reach this host, best first.

    Tailscale first (works at home and away), then the LAN egress address
    found with the UDP-connect trick (the interface the OS would use to reach
    the default gateway -- what a phone on the same Wi-Fi should target; no
    packets are actually sent), then any other local address. Loopback and
    link-local addresses are dropped.
    """
    candidates: list[str] = []

    def _add(ip):
        if ip and ip not in candidates and not ip.startswith(("127.", "169.254.")):
            candidates.append(ip)

    _add(tailscale_ipv4())

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        _add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(info[4][0])
    except OSError:
        pass

    # Stable sort: Tailscale addresses ahead of everything else.
    return sorted(candidates, key=lambda ip: not is_tailscale_ip(ip))


def find_admin_user() -> str | None:
    """Resolve an admin username from data/auth.json (schema uses is_admin),
    falling back to the first user."""
    auth_path = os.path.join("data", "auth.json")
    try:
        with open(auth_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    users = data.get("users") or {}
    if not isinstance(users, dict):
        return None
    for uname, udata in users.items():
        if isinstance(udata, dict) and udata.get("is_admin") is True:
            return uname
    return next(iter(users), None)


def mint_token(owner: str, name: str = "companion") -> tuple[str, str]:
    """Create a chat-scoped API token row and return (token_id, raw_token).

    The raw token is returned ONCE -- only its bcrypt hash + an 8-char prefix
    are persisted. Mirrors routes/api_token_routes.py so cookie- and
    companion-minted tokens are indistinguishable to the auth middleware.
    """
    from core.database import get_db_session, ApiToken

    raw_token = "ody_" + secrets.token_urlsafe(32)
    token_hash = bcrypt.hashpw(raw_token.encode(), bcrypt.gensalt()).decode()
    token_id = str(uuid.uuid4())[:8]

    with get_db_session() as db:
        db.add(ApiToken(
            id=token_id,
            owner=owner,
            name=name,
            token_hash=token_hash,
            token_prefix=raw_token[:8],
            scopes=COMPANION_SCOPE,
            is_active=True,
        ))
    return token_id, raw_token


def pairing_payload(host: str, port: int, token: str, hosts: list[str] | None = None) -> dict:
    """The exact JSON a client scans / accepts. Keep keys stable.

    `host` is the preferred address (older clients read only this one).
    `hosts` -- every candidate, best first -- is added only when there is more
    than one, so a client can fall back from Tailscale to the LAN or vice versa.
    """
    payload = {"v": PAIRING_VERSION, "host": host, "port": port, "token": token}
    extra = [h for h in (hosts or []) if h]
    if len(extra) > 1:
        payload["hosts"] = extra
    return payload


def pairing_qr_png_data_uri(payload: dict) -> str | None:
    """Render the pairing payload as a QR `data:` URI for an <img>. Returns None
    if the optional qrcode dep is unavailable."""
    try:
        import base64
        import io

        import qrcode

        img = qrcode.make(json.dumps(payload, separators=(",", ":")))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None
