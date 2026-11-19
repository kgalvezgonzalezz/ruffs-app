"""RUFFS: «Conectar con Google» — una sola autorización OAuth 2.0 para Gmail y
Google Calendar.

Por qué: Google ya no acepta la contraseña normal por IMAP/SMTP (solo una
contraseña de aplicación u OAuth) y su CalDAV solo acepta OAuth (con usuario y
contraseña responde 401). Con esto RUFFS usa el mismo token para:

* **Gmail** por IMAP/SMTP con XOAUTH2 — el resto del código de correo
  (carpetas, sondeo, envío) no cambia. La cuenta guarda como contraseña el
  marcador ``GOOGLE_MARKER``; al iniciar sesión se cambia por el token.
* **Google Calendar** por CalDAV (``apidata.googleusercontent.com``) con
  ``Authorization: Bearer`` — reaprovecha la sincronización y la escritura
  de vuelta de ``src/caldav_sync.py`` / ``src/caldav_writeback.py``.

El cliente OAuth es de tipo «App de escritorio» (Google Cloud del usuario):
redirección por bucle local (127.0.0.1/localhost) con PKCE. Los tokens se
guardan cifrados (src/secret_storage) en ``data/google_oauth/``.
"""

from __future__ import annotations

import base64
import glob
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from typing import Optional
from urllib.parse import quote, urlencode

logger = logging.getLogger(__name__)

GOOGLE_MARKER = "__ruffs_google_oauth__"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
CALDAV_BASE = "https://apidata.googleusercontent.com/caldav/v2/"
SCOPE_MAIL = "https://mail.google.com/"
SCOPE_CALENDAR = "https://www.googleapis.com/auth/calendar"
SCOPES = ["openid", "email", SCOPE_MAIL, SCOPE_CALENDAR]
GMAIL_IMAP = ("imap.gmail.com", 993)
GMAIL_SMTP = ("smtp.gmail.com", 465)
_STATE_TTL = 15 * 60

_lock = threading.Lock()
_pending: dict = {}  # state -> {owner, verifier, redirect_uri, exp}


class GoogleAuthError(Exception):
    """Google rechazó o no hay autorización válida (hay que volver a conectar)."""


# ─── Rutas y cifrado ─────────────────────────────────────────────────────────

def _base_dir() -> str:
    from src.constants import DATA_DIR
    d = os.path.join(DATA_DIR, "google_oauth")
    os.makedirs(os.path.join(d, "tokens"), exist_ok=True)
    return d


def _enc(v: str) -> str:
    from src.secret_storage import encrypt
    return encrypt(v) if v else ""


def _dec(v: str) -> str:
    from src.secret_storage import decrypt
    try:
        return decrypt(v) if v else ""
    except Exception:
        return v or ""


def is_gmail_host(host: str) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    return h.endswith("gmail.com") or h.endswith("googlemail.com") or h.endswith(".google.com")


def normalize_password(host: str, password: str) -> str:
    """Las contraseñas de aplicación de Google se muestran como
    «abcd efgh ijkl mnop»: quien la pega con espacios no puede iniciar sesión."""
    if password and password != GOOGLE_MARKER and is_gmail_host(host):
        compact = password.replace(" ", "").replace(" ", "")
        if len(compact) == 16 and compact.isalpha():
            return compact
    return password


# ─── Cliente OAuth (credenciales de Google Cloud) ───────────────────────────

def parse_client_json(raw) -> dict:
    """Acepta el JSON descargado de Google Cloud ({"installed": {...}} o
    {"web": {...}}) o un dict con client_id/client_secret."""
    data = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw or {})
    inner = data.get("installed") or data.get("web") or data
    cid = (inner.get("client_id") or "").strip()
    sec = (inner.get("client_secret") or "").strip()
    if not cid.endswith(".apps.googleusercontent.com") or not sec:
        raise ValueError("No es un cliente OAuth de Google válido: falta client_id (…apps.googleusercontent.com) o client_secret")
    return {"client_id": cid, "client_secret": sec}


def save_client(raw) -> dict:
    c = parse_client_json(raw)
    path = os.path.join(_base_dir(), "client.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"client_id": c["client_id"], "client_secret": _enc(c["client_secret"])}, f)
    return {"client_id": c["client_id"]}


def delete_client() -> None:
    try:
        os.remove(os.path.join(_base_dir(), "client.json"))
    except FileNotFoundError:
        pass


def get_client() -> Optional[dict]:
    """El cliente guardado en RUFFS o, si no hay, el del MCP de Gmail
    (data/mcp_oauth/**/*.json) — quien ya lo creó no tiene que repetirlo."""
    path = os.path.join(_base_dir(), "client.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return {"client_id": d["client_id"], "client_secret": _dec(d["client_secret"]), "source": "ruffs"}
        except Exception as e:  # noqa: BLE001
            logger.warning("google_oauth: client.json ilegible: %s", e)
    try:
        from src.constants import DATA_DIR
        for p in sorted(glob.glob(os.path.join(DATA_DIR, "mcp_oauth", "**", "*.json"), recursive=True)):
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict) and d.get("installed"):
                    c = parse_client_json(d)
                    c["source"] = "mcp"
                    return c
            except Exception:
                continue
    except Exception:
        pass
    return None


# ─── Tokens por usuario ─────────────────────────────────────────────────────

def _token_path(owner: Optional[str]) -> str:
    h = hashlib.sha256((owner or "_").encode("utf-8")).hexdigest()[:20]
    return os.path.join(_base_dir(), "tokens", f"{h}.json")


def load_tokens(owner: Optional[str]) -> Optional[dict]:
    p = _token_path(owner)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if (d.get("owner") or "") != (owner or ""):
            return None
        d["refresh_token"] = _dec(d.get("refresh_token", ""))
        d["access_token"] = _dec(d.get("access_token", ""))
        return d
    except Exception as e:  # noqa: BLE001
        logger.warning("google_oauth: token ilegible: %s", e)
        return None


def _save_tokens(owner: Optional[str], d: dict) -> None:
    out = dict(d)
    out["owner"] = owner or ""
    out["refresh_token"] = _enc(d.get("refresh_token", ""))
    out["access_token"] = _enc(d.get("access_token", ""))
    p = _token_path(owner)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f)
    os.replace(tmp, p)


def status(owner: Optional[str]) -> dict:
    c = get_client()
    t = load_tokens(owner)
    return {
        "has_client": bool(c),
        "client_source": (c or {}).get("source", ""),
        "connected": bool(t and t.get("refresh_token") and not t.get("needs_reconnect")),
        "needs_reconnect": bool(t and t.get("needs_reconnect")),
        "email": (t or {}).get("email", ""),
        "scopes": (t or {}).get("scope", "").split(),
        "error": (t or {}).get("last_error", ""),
    }


# ─── Flujo de autorización ──────────────────────────────────────────────────

def begin(owner: Optional[str], redirect_uri: str, login_hint: str = "") -> str:
    c = get_client()
    if not c:
        raise GoogleAuthError("Falta el cliente OAuth de Google (sube el JSON de Google Cloud)")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        for k in [k for k, v in _pending.items() if v["exp"] < now]:
            _pending.pop(k, None)
        _pending[state] = {"owner": owner or "", "verifier": verifier, "redirect_uri": redirect_uri, "exp": now + _STATE_TTL}
    params = {
        "client_id": c["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return AUTH_URL + "?" + urlencode(params)


def pop_state(state: str) -> Optional[dict]:
    with _lock:
        entry = _pending.pop(state or "", None)
    if not entry or entry["exp"] < time.time():
        return None
    return entry


def _http():
    import httpx
    return httpx.Client(timeout=20.0)


def exchange_code(entry: dict, code: str) -> dict:
    """Canjea el código por tokens, averigua el correo de la cuenta y los guarda."""
    c = get_client()
    if not c:
        raise GoogleAuthError("Falta el cliente OAuth de Google")
    with _http() as h:
        r = h.post(TOKEN_URL, data={
            "code": code, "client_id": c["client_id"], "client_secret": c["client_secret"],
            "redirect_uri": entry["redirect_uri"], "grant_type": "authorization_code",
            "code_verifier": entry["verifier"],
        })
        if r.status_code != 200:
            raise GoogleAuthError(f"Google rechazó el código: {r.text[:300]}")
        tok = r.json()
        u = h.get(USERINFO_URL, headers={"Authorization": f"Bearer {tok['access_token']}"})
        email = (u.json().get("email") if u.status_code == 200 else "") or ""
    if not tok.get("refresh_token"):
        prev = load_tokens(entry["owner"]) or {}
        tok["refresh_token"] = prev.get("refresh_token", "")
    if not tok.get("refresh_token"):
        raise GoogleAuthError("Google no devolvió un token de renovación; vuelve a intentarlo")
    data = {
        "email": email.lower(),
        "refresh_token": tok["refresh_token"],
        "access_token": tok.get("access_token", ""),
        "expires_at": time.time() + int(tok.get("expires_in", 3600)),
        "scope": tok.get("scope", " ".join(SCOPES)),
        "connected_at": time.time(),
    }
    _save_tokens(entry["owner"], data)
    return data


def get_access_token(owner: Optional[str]) -> str:
    """Token de acceso vigente (lo renueva si le queda menos de 2 minutos)."""
    t = load_tokens(owner)
    if not t or not t.get("refresh_token"):
        raise GoogleAuthError("Google no está conectado")
    if t.get("access_token") and t.get("expires_at", 0) - time.time() > 120:
        return t["access_token"]
    c = get_client()
    if not c:
        raise GoogleAuthError("Falta el cliente OAuth de Google")
    with _lock:
        t = load_tokens(owner) or t
        if t.get("access_token") and t.get("expires_at", 0) - time.time() > 120:
            return t["access_token"]
        with _http() as h:
            r = h.post(TOKEN_URL, data={
                "client_id": c["client_id"], "client_secret": c["client_secret"],
                "refresh_token": t["refresh_token"], "grant_type": "refresh_token",
            })
        if r.status_code != 200:
            err = ""
            try:
                err = r.json().get("error", "")
            except Exception:
                pass
            if err in ("invalid_grant", "unauthorized_client", "invalid_client"):
                t["needs_reconnect"] = True
                t["last_error"] = ("Google retiró el permiso (o caducó: si tu app de Google Cloud está en "
                                   "modo «Prueba», el permiso dura 7 días). Vuelve a conectar Google.")
                _save_tokens(owner, t)
                raise GoogleAuthError(t["last_error"])
            raise GoogleAuthError(f"No se pudo renovar el acceso a Google: HTTP {r.status_code}")
        tok = r.json()
        t["access_token"] = tok["access_token"]
        t["expires_at"] = time.time() + int(tok.get("expires_in", 3600))
        if tok.get("refresh_token"):
            t["refresh_token"] = tok["refresh_token"]
        t.pop("needs_reconnect", None)
        t.pop("last_error", None)
        _save_tokens(owner, t)
        return t["access_token"]


def disconnect(owner: Optional[str]) -> None:
    t = load_tokens(owner)
    if t and t.get("refresh_token"):
        try:
            with _http() as h:
                h.post(REVOKE_URL, data={"token": t["refresh_token"]})
        except Exception:
            pass
    try:
        os.remove(_token_path(owner))
    except FileNotFoundError:
        pass


# ─── Gmail: IMAP / SMTP con XOAUTH2 ─────────────────────────────────────────

def xoauth2_string(user: str, access_token: str) -> str:
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"


def _check_account(owner: Optional[str], user: str) -> str:
    t = load_tokens(owner)
    if not t:
        raise GoogleAuthError("Esta cuenta usa «Conectar con Google», pero Google no está conectado")
    if t.get("email") and user and t["email"].lower() != user.strip().lower():
        raise GoogleAuthError(f"Google está conectado como {t['email']}, no como {user}")
    return get_access_token(owner)


def imap_login(conn, user: str, owner: Optional[str]) -> None:
    token = _check_account(owner, user)
    auth = xoauth2_string(user, token).encode()
    conn.authenticate("XOAUTH2", lambda _challenge: auth)


def smtp_login(smtp, user: str, owner: Optional[str]) -> None:
    token = _check_account(owner, user)
    auth = xoauth2_string(user, token)
    smtp.ehlo_or_helo_if_needed()
    smtp.auth("XOAUTH2", lambda challenge=None: auth, initial_response_ok=True)


def friendly_login_error(host: str, err: Exception | str) -> str:
    """Mensaje claro para los fallos típicos de Gmail."""
    msg = str(err)
    if not is_gmail_host(host):
        return msg[:300]
    low = msg.lower()
    if "application-specific password" in low or "534" in low or "5.7.9" in low:
        return ("Gmail no acepta tu contraseña normal. Usa «Conectar con Google» (recomendado) o crea una "
                "contraseña de aplicación en https://myaccount.google.com/apppasswords (requiere verificación en 2 pasos).")
    if "authenticationfailed" in low or "invalid credentials" in low or "535" in low or "badcredentials" in low:
        return ("Gmail rechazó el usuario o la contraseña. Con Gmail usa «Conectar con Google» o una contraseña "
                "de aplicación (https://myaccount.google.com/apppasswords), no tu contraseña normal.")
    return msg[:300]


# ─── Google Calendar por CalDAV ─────────────────────────────────────────────

def list_calendars(owner: Optional[str]) -> list[dict]:
    """Calendarios de la cuenta (Calendar API) — Google CalDAV no permite
    descubrirlos por PROPFIND, así que se construye la URL de cada uno."""
    token = get_access_token(owner)
    out, page, retried = [], None, False
    with _http() as h:
        while True:
            params = {"minAccessRole": "reader", "maxResults": 250}
            if page:
                params["pageToken"] = page
            r = h.get(CALENDAR_LIST_URL, params=params, headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 401 and not retried:
                # Token anulado antes de caducar: se fuerza la renovación una vez.
                retried = True
                t = load_tokens(owner) or {}
                t["expires_at"] = 0
                _save_tokens(owner, t)
                token = get_access_token(owner)
                continue
            if r.status_code == 401:
                raise GoogleAuthError("Google rechazó el acceso de RUFFS. Vuelve a conectar Google.")
            if r.status_code == 403 and "accessNotConfigured" in r.text:
                raise GoogleAuthError("Activa «Google Calendar API» y «CalDAV API» en tu proyecto de Google Cloud")
            if r.status_code != 200:
                raise GoogleAuthError(f"Google Calendar respondió HTTP {r.status_code}: {r.text[:200]}")
            d = r.json()
            for it in d.get("items", []):
                if it.get("deleted") or it.get("hidden"):
                    continue
                out.append({
                    "id": it["id"], "name": it.get("summaryOverride") or it.get("summary") or it["id"],
                    "color": it.get("backgroundColor") or "", "primary": bool(it.get("primary")),
                    "writable": it.get("accessRole") in ("owner", "writer"),
                })
            page = d.get("nextPageToken")
            if not page:
                break
    return out


def caldav_url(cal_id: str) -> str:
    return CALDAV_BASE + quote(cal_id, safe="@") + "/events/"


def caldav_calendars(owner: Optional[str]):
    """(client, [(caldav.Calendar, info)]) listos para el pull/push de CalDAV."""
    import caldav
    token = get_access_token(owner)
    client = caldav.DAVClient(url=CALDAV_BASE, headers={"Authorization": f"Bearer {token}"},
                              enable_rfc6764=False, timeout=30)
    cals = []
    for info in list_calendars(owner):
        cals.append((caldav.Calendar(client=client, url=caldav_url(info["id"]), name=info["name"]), info))
    return client, cals
