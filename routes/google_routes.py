"""RUFFS: «Conectar con Google» — Gmail + Google Calendar con una sola
autorización OAuth (ver src/google_oauth.py)."""

import asyncio
import html
import logging
import urllib.parse
import uuid

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from src.auth_helpers import require_user

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/api/google/oauth/callback"


def _is_admin_or_single_user(request: Request) -> None:
    from src.auth_helpers import _auth_disabled
    if _auth_disabled():
        return
    from core.middleware import require_admin
    require_admin(request)


def _redirect_uri(request: Request) -> tuple[str, bool]:
    """(redirect_uri, mismo equipo). Google acepta 127.0.0.1/localhost con
    cualquier puerto para clientes de escritorio; se usa el mismo host desde
    el que se abrió RUFFS para que la cookie de sesión siga valiendo."""
    from core.constants import server_port
    host = (request.headers.get("host") or "").strip()
    hostname = host.split(":")[0].strip("[]").lower()
    if hostname in ("127.0.0.1", "localhost"):
        return f"http://{host}{CALLBACK_PATH}", True
    return f"http://127.0.0.1:{server_port()}{CALLBACK_PATH}", False


def _page(title: str, body: str, ok: bool) -> HTMLResponse:
    color = "#22c55e" if ok else "#ef4444"
    return HTMLResponse(f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;background:#0b0d17;color:#e5e7eb;display:flex;min-height:100vh;
align-items:center;justify-content:center;margin:0;padding:16px}}.c{{max-width:520px;background:#141827;
border:1px solid #2a3150;border-radius:14px;padding:28px}}h1{{font-size:20px;margin:0 0 12px;color:{color}}}
p{{line-height:1.5;font-size:14px}}a.b{{display:inline-block;margin-top:14px;padding:9px 16px;border-radius:9px;
background:#7c3aed;color:#fff;text-decoration:none}}input{{width:100%;box-sizing:border-box;padding:8px;
border-radius:8px;border:1px solid #2a3150;background:#0b0d17;color:#e5e7eb}}button{{margin-top:10px;padding:8px 14px;
border-radius:8px;border:0;background:#7c3aed;color:#fff}}</style></head><body><div class="c">
<h1>{html.escape(title)}</h1>{body}</div></body></html>""", status_code=200 if ok else 400)


def apply_connection(owner: str, email: str) -> dict:
    """Deja configurados Gmail (cuenta de correo con OAuth) y Google Calendar."""
    from core.database import EmailAccount, SessionLocal
    from routes.prefs_routes import _load_for_user, _save_for_user
    from src import google_oauth
    from src.secret_storage import encrypt

    marker = encrypt(google_oauth.GOOGLE_MARKER)
    db = SessionLocal()
    try:
        q = db.query(EmailAccount)
        if owner:
            q = q.filter(EmailAccount.owner == owner)
        row = next((r for r in q.all() if (r.imap_user or "").strip().lower() == email
                    or (r.from_address or "").strip().lower() == email), None)
        created = row is None
        if created:
            row = EmailAccount(id=uuid.uuid4().hex, name="Gmail", owner=owner, enabled=True,
                               is_default=(q.count() == 0))
            db.add(row)
        row.imap_host, row.imap_port = google_oauth.GMAIL_IMAP
        row.imap_starttls = False
        row.imap_user = email
        row.imap_password = marker
        row.smtp_host, row.smtp_port = google_oauth.GMAIL_SMTP
        row.smtp_security = "ssl"
        row.smtp_user = email
        row.smtp_password = marker
        row.from_address = row.from_address or email
        row.enabled = True
        db.commit()
        account_id = row.id
    finally:
        db.close()
    prefs = _load_for_user(owner) or {}
    prefs["google_calendar"] = {"enabled": True, "email": email}
    _save_for_user(owner, prefs)
    return {"email_account_id": account_id, "email_account_created": created}


def remove_connection(owner: str) -> None:
    """Quita la cuenta de Gmail con OAuth y la sincronización de Google Calendar.
    Los eventos ya copiados en RUFFS se quedan (solo dejan de sincronizarse)."""
    from core.database import EmailAccount, SessionLocal
    from routes.prefs_routes import _load_for_user, _save_for_user
    from src import google_oauth
    from src.secret_storage import decrypt

    db = SessionLocal()
    try:
        q = db.query(EmailAccount)
        if owner:
            q = q.filter(EmailAccount.owner == owner)
        for r in q.all():
            try:
                pw = decrypt(r.imap_password or "")
            except Exception:
                pw = ""
            if pw == google_oauth.GOOGLE_MARKER:
                was_default = r.is_default
                db.delete(r)
                db.flush()
                if was_default:
                    nxt = q.first()
                    if nxt:
                        nxt.is_default = True
        db.commit()
    finally:
        db.close()
    prefs = _load_for_user(owner) or {}
    if prefs.pop("google_calendar", None) is not None:
        _save_for_user(owner, prefs)


def setup_google_routes() -> APIRouter:
    router = APIRouter(prefix="/api/google", tags=["google"])

    @router.get("/status")
    async def google_status(request: Request):
        owner = require_user(request)
        from src import google_oauth
        st = google_oauth.status(owner)
        st["redirect_uri"], st["same_device"] = _redirect_uri(request)
        return st

    @router.post("/client")
    async def upload_client(request: Request, file: UploadFile = File(None), json_text: str = Form(None)):
        """Guarda el cliente OAuth (JSON descargado de Google Cloud)."""
        _is_admin_or_single_user(request)
        from src import google_oauth
        raw = (await file.read()) if file is not None else (json_text or "")
        try:
            return {"ok": True, **google_oauth.save_client(raw)}
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e))
        except Exception:
            raise HTTPException(400, "No se pudo leer el JSON del cliente de Google")

    @router.delete("/client")
    async def delete_client(request: Request):
        _is_admin_or_single_user(request)
        from src import google_oauth
        google_oauth.delete_client()
        return {"ok": True}

    @router.get("/connect")
    async def connect(request: Request):
        """Lleva a la página de permisos de Google; vuelve a /oauth/callback."""
        owner = require_user(request)
        from src import google_oauth
        redirect_uri, same_device = _redirect_uri(request)
        try:
            url = google_oauth.begin(owner, redirect_uri, request.query_params.get("hint", ""))
        except google_oauth.GoogleAuthError as e:
            return _page("Falta configurar Google", f"<p>{html.escape(str(e))}</p><a class='b' href='/'>Volver a RUFFS</a>", False)
        if same_device:
            return RedirectResponse(url)
        # Otro dispositivo (móvil, otra PC): Google solo vuelve a 127.0.0.1,
        # así que se copia la dirección final y se pega aquí.
        return _page("Conectar con Google", f"""
<p>1. Abre este enlace, elige tu cuenta y acepta los permisos:</p>
<p><a class="b" href="{html.escape(url)}" target="_blank" rel="noopener">Abrir Google</a></p>
<p>2. Al terminar, el navegador irá a una página que no carga (empieza por
<code>http://127.0.0.1</code>). Copia esa dirección completa y pégala aquí:</p>
<form method="post" action="/api/google/exchange"><input name="callback_url" placeholder="http://127.0.0.1:…/api/google/oauth/callback?state=…&code=…" required>
<button type="submit">Terminar conexión</button></form>""", True)

    @router.post("/open")
    async def open_in_browser(request: Request):
        """Abre la página de permisos de Google en el navegador del equipo.
        (Google puede bloquear el inicio de sesión dentro de la ventana de
        RUFFS por ser un navegador integrado; la vuelta no necesita la sesión
        de la ventana, así que funciona igual y la ventana detecta el cambio.)"""
        owner = require_user(request)
        from src import google_oauth
        redirect_uri, same_device = _redirect_uri(request)
        client_ip = (request.client.host if request.client else "") or ""
        if not same_device or client_ip not in ("127.0.0.1", "::1", "localhost"):
            return {"ok": True, "opened": False, "connect_url": "/api/google/connect"}
        try:
            url = google_oauth.begin(owner, redirect_uri)
        except google_oauth.GoogleAuthError as e:
            raise HTTPException(400, str(e))
        import webbrowser
        opened = False
        try:
            opened = bool(await asyncio.to_thread(webbrowser.open, url))
        except Exception:
            opened = False
        return {"ok": True, "opened": opened, "auth_url": url}

    async def _finish(state: str, code: str) -> HTMLResponse:
        from src import google_oauth
        entry = google_oauth.pop_state(state)
        if not entry:
            return _page("Enlace caducado", "<p>Esta autorización ya se usó o caducó. Vuelve a pulsar «Conectar con Google» en RUFFS.</p><a class='b' href='/'>Volver a RUFFS</a>", False)
        try:
            data = await asyncio.to_thread(google_oauth.exchange_code, entry, code)
        except google_oauth.GoogleAuthError as e:
            return _page("No se pudo conectar", f"<p>{html.escape(str(e))}</p><a class='b' href='/'>Volver a RUFFS</a>", False)
        email = data.get("email", "")
        if not email:
            return _page("No se pudo conectar", "<p>Google no indicó el correo de la cuenta.</p><a class='b' href='/'>Volver a RUFFS</a>", False)
        granted = set((data.get("scope") or "").split())
        missing = []
        if google_oauth.SCOPE_MAIL not in granted:
            missing.append("Gmail")
        if google_oauth.SCOPE_CALENDAR not in granted:
            missing.append("Calendar")
        owner = entry["owner"]
        await asyncio.to_thread(apply_connection, owner, email)
        try:
            from src.caldav_sync import sync_caldav
            asyncio.get_running_loop().create_task(sync_caldav(owner))
        except Exception:
            pass
        warn = (f"<p style='color:#fbbf24'>Ojo: no marcaste el permiso de {', '.join(missing)}. "
                "Vuelve a conectar y marca todas las casillas.</p>") if missing else ""
        return _page("Google conectado ✓", f"""
<p>RUFFS ya usa <b>{html.escape(email)}</b> para el correo (Gmail) y el calendario (Google Calendar).
Lo que cambies en RUFFS o en Google se sincroniza en ambos sentidos.</p>{warn}
<p>Ya puedes cerrar esta pestaña y volver a RUFFS.</p>
<a class="b" href="/">Abrir RUFFS aquí</a>""", True)

    @router.get("/oauth/callback")
    async def oauth_callback(request: Request):
        # Sin sesión obligatoria (exenta en app.py): el «state» de un solo uso,
        # aleatorio y con caducidad identifica al usuario que inició la conexión.
        p = request.query_params
        if p.get("error"):
            msg = "Cancelaste la conexión con Google." if p.get("error") == "access_denied" else f"Google respondió: {p.get('error')}"
            return _page("Conexión cancelada", f"<p>{html.escape(msg)}</p><a class='b' href='/'>Volver a RUFFS</a>", False)
        return await _finish(p.get("state", ""), p.get("code", ""))

    @router.post("/exchange")
    async def oauth_exchange(request: Request, callback_url: str = Form(...)):
        require_user(request)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url.strip()).query)
        if q.get("error"):
            return _page("Conexión cancelada", "<p>Google no dio permiso.</p><a class='b' href='/'>Volver a RUFFS</a>", False)
        return await _finish((q.get("state") or [""])[0], (q.get("code") or [""])[0])

    @router.post("/disconnect")
    async def disconnect(request: Request):
        owner = require_user(request)
        from src import google_oauth
        await asyncio.to_thread(google_oauth.disconnect, owner)
        await asyncio.to_thread(remove_connection, owner)
        return {"ok": True}

    @router.post("/sync")
    async def sync_now(request: Request):
        owner = require_user(request)
        from src.caldav_sync import sync_caldav
        return await sync_caldav(owner)

    return router
