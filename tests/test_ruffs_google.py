"""RUFFS: «Conectar con Google» (Gmail por XOAUTH2 + Google Calendar por CalDAV
con OAuth), contraseñas de aplicación de Gmail y descubrimiento CalDAV."""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta

import pytest

from src import google_oauth as go


CLIENT = {"installed": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-secreto",
                        "redirect_uris": ["http://localhost"]}}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as database
    import src.constants as c
    import routes.prefs_routes as prefs
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    database.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", Session)
    monkeypatch.setattr(c, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(prefs, "PREFS_FILE", str(tmp_path / "user_prefs.json"))
    go._pending.clear()
    return tmp_path, Session


class _Resp:
    def __init__(self, code, data):
        self.status_code, self._d = code, data
        self.text = json.dumps(data)

    def json(self):
        return self._d


class FakeHttp:
    """Sustituye a httpx.Client: responde token / userinfo / calendarList."""
    calls = []
    token_response = None
    calendars = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, data=None, **k):
        FakeHttp.calls.append(("POST", url, dict(data or {})))
        if url == go.TOKEN_URL:
            return FakeHttp.token_response or _Resp(200, {
                "access_token": "ya29.nuevo", "expires_in": 3599, "refresh_token": "1//refresh",
                "scope": " ".join(go.SCOPES)})
        return _Resp(200, {})

    def get(self, url, params=None, headers=None, **k):
        FakeHttp.calls.append(("GET", url, headers))
        if url == go.USERINFO_URL:
            return _Resp(200, {"email": "Usuario@Gmail.com"})
        if url == go.CALENDAR_LIST_URL:
            return _Resp(200, {"items": FakeHttp.calendars})
        return _Resp(404, {})


@pytest.fixture()
def fake_http(monkeypatch):
    FakeHttp.calls, FakeHttp.token_response = [], None
    FakeHttp.calendars = [
        {"id": "usuario@gmail.com", "summary": "Kevin", "backgroundColor": "#039be5", "primary": True, "accessRole": "owner"},
        {"id": "es.chile#holiday@group.v.calendar.google.com", "summary": "Feriados", "accessRole": "reader"},
    ]
    monkeypatch.setattr(go, "_http", lambda: FakeHttp())
    return FakeHttp


def _connect(owner="kevin"):
    go.save_client(json.dumps(CLIENT))
    url = go.begin(owner, "http://127.0.0.1:7100/api/google/oauth/callback")
    state = url.split("state=")[1].split("&")[0]
    entry = go.pop_state(state)
    return go.exchange_code(entry, "4/codigo"), url


# ── Contraseñas de aplicación y mensajes ─────────────────────────────────────

def test_gmail_app_password_spaces_and_messages():
    assert go.normalize_password("imap.gmail.com", "abcd efgh ijkl mnop") == "abcdefghijklmnop"
    assert go.normalize_password("smtp.gmail.com", "abcd efgh ijkl mnop") == "abcdefghijklmnop"
    assert go.normalize_password("imap.migadu.com", "abcd efgh ijkl mnop") == "abcd efgh ijkl mnop"
    assert go.normalize_password("imap.gmail.com", "mi clave con espacios 1") == "mi clave con espacios 1"
    assert go.normalize_password("imap.gmail.com", go.GOOGLE_MARKER) == go.GOOGLE_MARKER
    m = go.friendly_login_error("imap.gmail.com", Exception("b'[ALERT] Application-specific password required'"))
    assert "contraseña de aplicación" in m and "apppasswords" in m
    m = go.friendly_login_error("smtp.gmail.com", Exception("(535, b'5.7.8 Username and Password not accepted. BadCredentials')"))
    assert "Conectar con Google" in m
    assert go.friendly_login_error("imap.example.com", Exception("boom")) == "boom"
    assert go.xoauth2_string("user@gmail.com", "T") == "user=user@gmail.com\x01auth=Bearer T\x01\x01"


def test_client_json_formats(env):
    assert go.parse_client_json(json.dumps(CLIENT))["client_id"].endswith(".apps.googleusercontent.com")
    assert go.parse_client_json({"web": CLIENT["installed"]})["client_secret"] == "GOCSPX-secreto"
    with pytest.raises(ValueError):
        go.parse_client_json({"installed": {"client_id": "x", "client_secret": ""}})
    assert go.get_client() is None
    go.save_client(json.dumps(CLIENT))
    raw = open(os.path.join(env[0], "google_oauth", "client.json"), encoding="utf-8").read()
    assert "GOCSPX-secreto" not in raw          # el secreto se guarda cifrado
    assert go.get_client()["client_secret"] == "GOCSPX-secreto"
    go.delete_client()
    # Reutiliza el cliente del MCP de Gmail si ya existe
    os.makedirs(os.path.join(env[0], "mcp_oauth", "gmail"), exist_ok=True)
    with open(os.path.join(env[0], "mcp_oauth", "gmail", "gcp-oauth.keys.json"), "w") as f:
        json.dump(CLIENT, f)
    assert go.get_client()["source"] == "mcp"


# ── Flujo OAuth ──────────────────────────────────────────────────────────────

def test_oauth_flow_pkce_state_and_tokens(env, fake_http):
    data, url = _connect()
    assert "code_challenge_method=S256" in url and "access_type=offline" in url
    assert "mail.google.com" in url and "auth%2Fcalendar" in url
    assert data["email"] == "usuario@gmail.com"
    tok_call = [c for c in fake_http.calls if c[1] == go.TOKEN_URL][0][2]
    assert tok_call["code_verifier"] and tok_call["grant_type"] == "authorization_code"
    # state de un solo uso
    state = url.split("state=")[1].split("&")[0]
    assert go.pop_state(state) is None
    # tokens cifrados en disco, y solo para su dueño
    raw = open(go._token_path("kevin"), encoding="utf-8").read()
    assert "1//refresh" not in raw and "ya29" not in raw
    st = go.status("kevin")
    assert st["connected"] and st["email"] == "usuario@gmail.com" and st["has_client"]
    assert not go.status("otro")["connected"]


def test_token_refresh_and_revoked(env, fake_http):
    _connect()
    assert go.get_access_token("kevin") == "ya29.nuevo"           # vigente: sin llamar a Google
    t = go.load_tokens("kevin"); t["expires_at"] = time.time() - 10; go._save_tokens("kevin", t)
    fake_http.token_response = _Resp(200, {"access_token": "ya29.renovado", "expires_in": 3600})
    assert go.get_access_token("kevin") == "ya29.renovado"
    t = go.load_tokens("kevin"); t["expires_at"] = 0; go._save_tokens("kevin", t)
    fake_http.token_response = _Resp(400, {"error": "invalid_grant"})
    with pytest.raises(go.GoogleAuthError):
        go.get_access_token("kevin")
    st = go.status("kevin")
    assert st["needs_reconnect"] and not st["connected"] and "7 días" in st["error"]


# ── Gmail: IMAP / SMTP ───────────────────────────────────────────────────────

class FakeImap:
    def __init__(self):
        self.auth = None
        self.login_args = None

    def authenticate(self, mech, cb):
        self.auth = (mech, cb(b""))

    def login(self, u, p):
        self.login_args = (u, p)


class FakeSmtp:
    def __init__(self):
        self.auth_args = None
        self.login_args = None

    def ehlo_or_helo_if_needed(self):
        pass

    def auth(self, mech, cb, initial_response_ok=True):
        self.auth_args = (mech, cb())

    def login(self, u, p):
        self.login_args = (u, p)


def test_imap_smtp_login_oauth_and_app_password(env, fake_http):
    from routes.email_helpers import _imap_login, _smtp_login
    _connect()
    c = FakeImap()
    _imap_login(c, "imap.gmail.com", "usuario@gmail.com", go.GOOGLE_MARKER, "kevin")
    assert c.auth == ("XOAUTH2", b"user=usuario@gmail.com\x01auth=Bearer ya29.nuevo\x01\x01") and c.login_args is None
    s = FakeSmtp()
    _smtp_login(s, "smtp.gmail.com", "usuario@gmail.com", go.GOOGLE_MARKER, "kevin")
    assert s.auth_args[0] == "XOAUTH2" and "Bearer ya29.nuevo" in s.auth_args[1]
    # Otra cuenta u otro usuario no pueden usar el token
    with pytest.raises(go.GoogleAuthError):
        _imap_login(FakeImap(), "imap.gmail.com", "alguien@gmail.com", go.GOOGLE_MARKER, "kevin")
    with pytest.raises(go.GoogleAuthError):
        _imap_login(FakeImap(), "imap.gmail.com", "usuario@gmail.com", go.GOOGLE_MARKER, "intruso")
    # Contraseña de aplicación pegada con espacios
    c2 = FakeImap()
    _imap_login(c2, "imap.gmail.com", "usuario@gmail.com", "abcd efgh ijkl mnop", "kevin")
    assert c2.login_args == ("usuario@gmail.com", "abcdefghijklmnop")


def test_apply_and_remove_connection(env):
    from core.database import EmailAccount
    from routes.google_routes import apply_connection, remove_connection
    from routes.prefs_routes import _load_for_user
    from routes.email_helpers import _get_email_config
    from src.secret_storage import decrypt
    tmp, Session = env
    r1 = apply_connection("kevin", "usuario@gmail.com")
    r2 = apply_connection("kevin", "usuario@gmail.com")          # idempotente
    assert r1["email_account_created"] and not r2["email_account_created"]
    s = Session()
    rows = s.query(EmailAccount).filter(EmailAccount.owner == "kevin").all()
    assert len(rows) == 1 and rows[0].is_default and rows[0].imap_host == "imap.gmail.com"
    assert rows[0].smtp_host == "smtp.gmail.com" and rows[0].smtp_security == "ssl"
    assert decrypt(rows[0].imap_password) == go.GOOGLE_MARKER and rows[0].imap_password != go.GOOGLE_MARKER
    s.close()
    cfg = _get_email_config(r1["email_account_id"], owner="kevin")
    assert cfg["owner"] == "kevin" and cfg["imap_password"] == go.GOOGLE_MARKER
    assert _load_for_user("kevin")["google_calendar"]["enabled"]
    remove_connection("kevin")
    s = Session()
    assert s.query(EmailAccount).count() == 0
    s.close()
    assert "google_calendar" not in _load_for_user("kevin")


# ── Google Calendar: pull y push por CalDAV ──────────────────────────────────

ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:evento-1@google.com
SUMMARY:Reunión con Ana
DTSTART:{start}
DTEND:{end}
END:VEVENT
END:VCALENDAR
"""


class FakeObj:
    def __init__(self, data):
        self.data = data
        self.saved = False
        self.deleted = False

    def save(self):
        self.saved = True

    def delete(self):
        self.deleted = True


class FakeCal:
    def __init__(self, url, name, events=()):
        self.url, self.name = url, name
        self.events = {uid: FakeObj(d) for uid, d in events}
        self.created = []

    def date_search(self, start, end, expand=False):
        return list(self.events.values())

    def event_by_uid(self, uid):
        if uid not in self.events:
            raise KeyError(uid)
        return self.events[uid]

    def save_event(self, ical):
        self.created.append(ical)


def _google_cals():
    now = datetime.utcnow() + timedelta(days=2)
    ics = ICS.format(start=now.strftime("%Y%m%dT%H%M%SZ"), end=(now + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ"))
    primary = FakeCal(go.caldav_url("usuario@gmail.com"), None, [("evento-1@google.com", ics)])
    holidays = FakeCal(go.caldav_url("es.chile#holiday@group.v.calendar.google.com"), None)
    return None, [(primary, {"name": "Kevin", "color": "#039be5", "writable": True}),
                  (holidays, {"name": "Feriados", "color": "", "writable": False})]


def test_google_calendar_sync_and_writeback(env, monkeypatch):
    from core.database import CalendarCal, CalendarEvent
    from routes.prefs_routes import _save_for_user
    from src import caldav_sync, caldav_writeback
    tmp, Session = env
    cals = _google_cals()
    monkeypatch.setattr(go, "caldav_calendars", lambda owner: cals)
    _save_for_user("kevin", {"google_calendar": {"enabled": True, "email": "usuario@gmail.com"}})
    res = asyncio.run(caldav_sync.sync_caldav("kevin"))
    assert res["errors"] == [] and res["calendars"] == 2 and res["events"] == 1
    s = Session()
    local = {c.name: c for c in s.query(CalendarCal).filter(CalendarCal.owner == "kevin")}
    assert set(local) == {"Kevin", "Feriados"} and local["Kevin"].color == "#039be5"
    ev = s.query(CalendarEvent).one()
    assert ev.summary == "Reunión con Ana" and ev.calendar_id == local["Kevin"].id and ev.is_utc
    kevin_id, feriados_id = local["Kevin"].id, local["Feriados"].id
    s.close()
    # Push: nuevo evento creado en RUFFS → Google; editar uno existente; borrar
    start = datetime.utcnow() + timedelta(days=3)
    new_ev = {"uid": "ruffs-1", "summary": "Dentista", "dtstart": start, "dtend": start + timedelta(hours=1),
              "all_day": False, "is_utc": True}
    r = asyncio.run(caldav_writeback.writeback_event("kevin", "caldav", kevin_id, new_ev))
    assert r == {"ok": True, "created": True} and "Dentista" in cals[1][0][0].created[0]
    r = asyncio.run(caldav_writeback.writeback_event("kevin", "caldav", kevin_id, dict(new_ev, uid="evento-1@google.com", summary="Reunión movida")))
    assert r.get("updated") and "Reunión movida" in cals[1][0][0].events["evento-1@google.com"].data
    r = asyncio.run(caldav_writeback.writeback_event("kevin", "caldav", kevin_id, {"uid": "evento-1@google.com"}, delete=True))
    assert r["ok"] and cals[1][0][0].events["evento-1@google.com"].deleted
    # Calendario de solo lectura: no se escribe y se explica
    r = asyncio.run(caldav_writeback.writeback_event("kevin", "caldav", feriados_id, new_ev))
    assert not r["ok"] and "solo lectura" in r["error"]
    # Borrado en Google → desaparece en RUFFS en la siguiente sincronización
    cals[1][0][0].events.clear()
    res = asyncio.run(caldav_sync.sync_caldav("kevin"))
    assert res["deleted"] == 1
    s = Session()
    assert s.query(CalendarEvent).count() == 0
    s.close()


def test_google_sync_reports_reconnect(env, monkeypatch):
    from routes.prefs_routes import _save_for_user
    from src import caldav_sync

    def boom(owner):
        raise go.GoogleAuthError("Vuelve a conectar Google")
    monkeypatch.setattr(go, "caldav_calendars", boom)
    _save_for_user("kevin", {"google_calendar": {"enabled": True}})
    res = asyncio.run(caldav_sync.sync_caldav("kevin"))
    assert res["errors"] == ["Google: Vuelve a conectar Google"]


def test_list_calendars_builds_caldav_urls(env, fake_http):
    _connect()
    cals = go.list_calendars("kevin")
    assert [c["writable"] for c in cals] == [True, False] and cals[0]["color"] == "#039be5"
    assert go.caldav_url(cals[1]["id"]) == "https://apidata.googleusercontent.com/caldav/v2/es.chile%23holiday@group.v.calendar.google.com/events/"


def test_caldav_discovery_404_falls_back_to_url(env, monkeypatch):
    import caldav
    from caldav.lib.error import NotFoundError
    from src import caldav_sync
    used = {}

    class FakeClient:
        def __init__(self, url, username, password):
            self.url = url

        def principal(self):
            raise NotFoundError("404 en el principal")

        def calendar(self, url):
            used["url"] = url
            return FakeCal(url, "Mi calendario")

    monkeypatch.setattr(caldav, "DAVClient", FakeClient)
    res = caldav_sync._sync_blocking("kevin", "https://dav.example.com/cal/kevin/personal", "kevin", "x")
    assert used["url"].endswith("/personal") and res["calendars"] == 1 and res["errors"] == []


def test_callback_is_auth_exempt_and_routes_registered():
    src = open(os.path.join(os.path.dirname(__file__), "..", "app.py"), encoding="utf-8").read()
    assert '"/api/google/oauth/callback",' in src and "setup_google_routes()" in src
    assert "caldav_background_loop()" in src
    from routes.google_routes import setup_google_routes
    paths = {r.path for r in setup_google_routes().routes}
    assert {"/api/google/status", "/api/google/connect", "/api/google/oauth/callback", "/api/google/open",
            "/api/google/disconnect", "/api/google/client", "/api/google/exchange", "/api/google/sync"} <= paths


def test_same_uid_in_two_google_calendars(env, monkeypatch):
    """Google repite el UID de un festivo en dos calendarios: antes fallaba con
    UNIQUE constraint (calendar_events.uid) y ese calendario no se sincronizaba."""
    from core.database import CalendarEvent
    from routes.prefs_routes import _save_for_user
    from src import caldav_sync, caldav_writeback
    tmp, Session = env
    day = (datetime.utcnow() + timedelta(days=5)).strftime("%Y%m%d")
    hol = ("BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:20270527_corpus@google.com\nSUMMARY:Corpus Christi\n"
           f"DTSTART;VALUE=DATE:{day}\nEND:VEVENT\nEND:VCALENDAR\n")
    a = FakeCal(go.caldav_url("es.chile#holiday@group.v.calendar.google.com"), None, [("20270527_corpus@google.com", hol)])
    b = FakeCal(go.caldav_url("usuario@gmail.com"), None, [("20270527_corpus@google.com", hol)])
    cals = (None, [(a, {"name": "Festivos en Chile", "writable": False}), (b, {"name": "Usuario", "writable": True})])
    monkeypatch.setattr(go, "caldav_calendars", lambda owner: cals)
    _save_for_user("kevin", {"google_calendar": {"enabled": True}})
    r1 = asyncio.run(caldav_sync.sync_caldav("kevin"))
    assert r1["errors"] == [] and r1["events"] == 2
    r2 = asyncio.run(caldav_sync.sync_caldav("kevin"))       # idempotente
    assert r2["errors"] == [] and r2["deleted"] == 0
    s = Session()
    rows = s.query(CalendarEvent).all()
    uids = sorted(e.uid for e in rows)
    assert len(rows) == 2 and uids[0] == "20270527_corpus@google.com" and uids[1].startswith("20270527_corpus@google.com::caldav-")
    copy = next(e for e in rows if "::" in e.uid)
    cal_id, local_uid = copy.calendar_id, copy.uid
    s.close()
    assert caldav_sync.remote_uid(local_uid) == "20270527_corpus@google.com"
    # editar la copia en RUFFS actualiza el evento de Google con su UID original
    target = a if caldav_writeback._stable_cal_id(str(a.url)) == cal_id else b
    start = datetime.utcnow() + timedelta(days=5)
    r = asyncio.run(caldav_writeback.writeback_event("kevin", "caldav", cal_id, {
        "uid": local_uid, "summary": "Corpus Christi (movido)", "dtstart": start, "dtend": start + timedelta(hours=1),
        "all_day": False, "is_utc": True}))
    if target is b:
        assert r.get("updated") and "movido" in b.events["20270527_corpus@google.com"].data
    else:
        assert not r["ok"] and "solo lectura" in r["error"]
