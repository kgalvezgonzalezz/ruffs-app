"""CalDAV → local SQLite sync.

The Settings UI lets users save CalDAV credentials, but the original
sync path was removed when calendar storage was migrated to SQLite.
This module re-wires that gap as a one-way pull (remote → local),
called on calendar open and from a periodic scheduler loop.

Design notes:
- We use the `caldav` lib so PROPFIND discovery + REPORT XML work
  across Radicale / Nextcloud / Apple / Fastmail without us
  reinventing the protocol. It's pure Python.
- The lib is synchronous; we run it in a threadpool via
  `asyncio.to_thread` so the FastAPI event loop stays free.
- Each remote calendar maps to one local `CalendarCal` row with
  `source="caldav"` and `id` = a stable hash of the remote URL so
  re-syncs idempotently target the same row.
- Events upsert by VEVENT UID (kept as the local `uid`). Local
  CalDAV-sourced events not seen in the latest pull are deleted so
  remote deletions propagate.
- Datetimes are converted to UTC and the row is flagged `is_utc=True`
  so the serializer adds the Z suffix and the frontend renders in the
  user's local TZ correctly.
"""

import asyncio
import hashlib
import ipaddress
import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

# Pull window: 90 days back, 1 year forward. Keeps the REPORT cheap and
# matches what the calendar UI typically renders. Far-future recurring
# events still come through via RRULE expansion on the frontend.
_LOOKBACK_DAYS = 90
_LOOKAHEAD_DAYS = 365
_BLOCKED_HOSTS = {
    "localhost",
    "localhost.",
    "ip6-localhost",
    "metadata.google.internal",
}


def _private_caldav_allowed() -> bool:
    return os.environ.get("ODYSSEUS_ALLOW_PRIVATE_CALDAV", "0").lower() in {"1", "true", "yes"}


def _validate_caldav_ip(host: str) -> None:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        raise ValueError("CalDAV URL host is not allowed")
    if ip.is_private and not _private_caldav_allowed():
        raise ValueError("Private CalDAV IPs require ODYSSEUS_ALLOW_PRIVATE_CALDAV=1")


def validate_caldav_url(raw_url: str) -> str:
    """Validate and normalize a user-provided CalDAV URL before server-side use."""
    url = (raw_url if isinstance(raw_url, str) else "").strip()
    if not url:
        raise ValueError("CalDAV URL is required")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("CalDAV URL must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError("CalDAV URL must include a host")
    if parsed.username or parsed.password:
        raise ValueError("Put CalDAV credentials in the username/password fields, not the URL")
    if parsed.fragment:
        raise ValueError("CalDAV URL fragments are not allowed")
    try:
        parsed.port
    except ValueError:
        raise ValueError("CalDAV URL has an invalid port")
    host = (parsed.hostname or "").lower()
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        raise ValueError("CalDAV URL host is not allowed")
    _validate_caldav_ip(host)
    return urlunparse(parsed._replace(fragment="")).rstrip("/")


def _stable_cal_id(remote_url: str) -> str:
    """Deterministic local id for a remote CalDAV calendar — same URL
    always maps to the same local row across restarts and re-syncs."""
    h = hashlib.sha256(remote_url.encode("utf-8")).hexdigest()[:24]
    return f"caldav-{h}"


def _to_utc_naive(dt):
    """CalDAV datetimes can be tz-aware (with a TZID) or naive. The DB
    column is naive but we set is_utc=True so the serializer adds Z.
    All-day events stay as date and get widened to datetime here."""
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None), False
        return dt, False  # naive → treat as local
    # date-only (all-day)
    return datetime(dt.year, dt.month, dt.day), True


def _find_existing_event(db, pending, uid_val, calendar_id):
    """Find the event to update for THIS calendar.

    CalendarEvent.uid is the global primary key, so an unscoped lookup by uid
    returns whatever row holds that VEVENT uid — including another owner's.
    The old code then reassigned that row's calendar_id, moving (stealing)
    another user's event into the syncing calendar whenever the two share a
    uid (shared/subscribed/public calendars, or two accounts on one server).
    Scope the lookup to the calendar being synced; a genuine cross-user uid
    collision then fails the PK insert inside the per-calendar try/except
    instead of hijacking the row. (import_ics was already fixed this way.)
    """
    from core.database import CalendarEvent
    return pending.get(uid_val) or db.query(CalendarEvent).filter(
        CalendarEvent.uid == uid_val,
        CalendarEvent.calendar_id == calendar_id,
    ).first()


UID_SEP = "::"


def remote_uid(local_uid: str) -> str:
    """UID del servidor a partir del UID local («UID::caldav-…» → «UID»)."""
    u = local_uid or ""
    i = u.find(UID_SEP + "caldav-")
    return u[:i] if i > 0 else u


def _local_uid(db, pending, uid_val, calendar_id):
    """UID local para ``uid_val`` en ``calendar_id``.

    calendar_events.uid es clave primaria GLOBAL, pero Google repite el mismo
    UID en varios calendarios (p. ej. «Festivos en Chile» y otro calendario de
    festivos): al insertar la segunda copia fallaba con UNIQUE constraint y ese
    calendario dejaba de sincronizarse entero. Si el UID ya es de otro
    calendario, esta copia se guarda como «UID::<calendario>»."""
    from core.database import CalendarEvent
    ns = f"{uid_val}{UID_SEP}{calendar_id}"
    if ns in pending:
        return ns
    if uid_val in pending:
        return uid_val
    rows = db.query(CalendarEvent.uid, CalendarEvent.calendar_id).filter(
        CalendarEvent.uid.in_([uid_val, ns])).all()
    for u, c in rows:
        if c == calendar_id:
            return u
    return ns if any(u == uid_val for u, _c in rows) else uid_val


def _sync_blocking(owner: str, url: str, username: str, password: str) -> dict:
    """The actual sync — synchronous, intended to run in a threadpool.
    Returns counts: {calendars, events, deleted, errors}."""
    # Lazy imports so a missing `caldav` dep doesn't break app startup —
    # the integrations form still works, sync just no-ops with an error.
    import caldav
    from caldav.lib.error import AuthorizationError

    result = {"calendars": 0, "events": 0, "deleted": 0, "errors": []}

    client = caldav.DAVClient(url=url, username=username, password=password)

    # Discovery: try principal → calendars first; if the server doesn't
    # support discovery (or the URL points directly at a calendar), fall
    # back to treating the URL as a single calendar.
    # RUFFS: un 404 en el descubrimiento (URL que apunta directo a un
    # calendario) ya no aborta: se prueba la URL como calendario. Solo un
    # error de autenticación corta aquí, con un mensaje claro.
    calendars = []
    try:
        principal = client.principal()
        calendars = principal.calendars()
    except AuthorizationError as e:
        result["errors"].append(f"Usuario o contraseña rechazados por el servidor CalDAV ({e})")
        return result
    except Exception as e:
        logger.info(f"CalDAV principal discovery failed, trying URL as calendar: {e}")
        try:
            calendars = [client.calendar(url=url)]
        except Exception as e2:
            result["errors"].append(f"Could not open URL as calendar: {e2}")
            return result

    if not calendars:
        try:
            calendars = [client.calendar(url=url)]
        except Exception as e:
            result["errors"].append(f"No calendars and URL fallback failed: {e}")
            return result

    _pull_into_db(owner, [(c, None) for c in calendars], result)
    return result


def _sync_google_blocking(owner: str) -> dict:
    """RUFFS: Google Calendar con «Conectar con Google» (OAuth + CalDAV)."""
    from src import google_oauth
    result = {"calendars": 0, "events": 0, "deleted": 0, "errors": []}
    try:
        _client, cals = google_oauth.caldav_calendars(owner)
    except google_oauth.GoogleAuthError as e:
        result["errors"].append(f"Google: {e}")
        return result
    _pull_into_db(owner, cals, result, label="Google")
    if any("403" in e for e in result["errors"]):
        result["errors"].append("Google: activa «CalDAV API» en tu proyecto de Google Cloud "
                                "(https://console.cloud.google.com/apis/library/caldav.googleapis.com)")
    return result


def _pull_into_db(owner: str, calendars, result: dict, label: str = "CalDAV") -> None:
    """Trae los eventos de cada calendario remoto a la base local.

    ``calendars``: lista de (calendario caldav, info|None); info puede traer
    ``name`` y ``color`` (Google) para el calendario local."""
    from core.database import CalendarCal, CalendarEvent, SessionLocal

    start = datetime.utcnow() - timedelta(days=_LOOKBACK_DAYS)
    end = datetime.utcnow() + timedelta(days=_LOOKAHEAD_DAYS)

    db = SessionLocal()
    try:
        for remote_cal, info in calendars:
            try:
                remote_url = str(remote_cal.url)
                cal_id = _stable_cal_id(remote_url)
                display_name = ((info or {}).get("name") or remote_cal.name or "").strip() or label

                local_cal = db.query(CalendarCal).filter(
                    CalendarCal.id == cal_id,
                    CalendarCal.owner == owner,
                ).first()
                if not local_cal:
                    local_cal = CalendarCal(
                        id=cal_id,
                        owner=owner,
                        name=display_name,
                        color=(info or {}).get("color") or "#5b8abf",
                        source="caldav",
                    )
                    db.add(local_cal)
                    db.commit()
                else:
                    # Refresh the display name if the user renamed it
                    # remotely; preserve any local color override.
                    if local_cal.name != display_name:
                        local_cal.name = display_name
                        db.commit()
                result["calendars"] += 1

                # Fetch events in window. `date_search` returns CalendarObject
                # resources; each may contain one VEVENT (most servers) or
                # several (rare).
                from icalendar import Calendar as iCal

                seen_uids = set()
                # Track events added to the session but not yet committed so
                # duplicate UIDs within the same batch are updated, not re-inserted
                # (which would violate the UNIQUE constraint on commit).
                pending: dict = {}
                try:
                    objs = remote_cal.date_search(start=start, end=end, expand=False)
                except Exception as e:
                    result["errors"].append(f"{display_name}: date_search failed ({e})")
                    continue

                for obj in objs:
                    try:
                        ical = iCal.from_ical(obj.data)
                    except Exception as e:
                        result["errors"].append(f"{display_name}: parse failed ({e})")
                        continue

                    for comp in ical.walk():
                        if comp.name != "VEVENT":
                            continue
                        uid_val = str(comp.get("uid", "")) or str(uuid.uuid4())
                        uid_val = _local_uid(db, pending, uid_val, local_cal.id)
                        seen_uids.add(uid_val)

                        dtstart_p = comp.get("dtstart")
                        if not dtstart_p:
                            continue
                        start_dt, all_day = _to_utc_naive(dtstart_p.dt)

                        dtend_p = comp.get("dtend")
                        if dtend_p:
                            end_dt, _ = _to_utc_naive(dtend_p.dt)
                        elif all_day:
                            end_dt = start_dt + timedelta(days=1)
                        else:
                            end_dt = start_dt + timedelta(hours=1)

                        # is_utc reflects whether the source carried a TZ
                        # we converted from. All-day = no TZ semantics.
                        row_is_utc = (
                            not all_day
                            and isinstance(dtstart_p.dt, datetime)
                            and dtstart_p.dt.tzinfo is not None
                        )

                        summary = str(comp.get("summary", ""))
                        description = str(comp.get("description", ""))
                        location = str(comp.get("location", ""))
                        rrule = (
                            comp.get("rrule").to_ical().decode()
                            if comp.get("rrule")
                            else ""
                        )

                        existing = _find_existing_event(db, pending, uid_val, local_cal.id)
                        if existing:
                            existing.calendar_id = local_cal.id
                            existing.summary = summary
                            existing.description = description
                            existing.location = location
                            existing.dtstart = start_dt
                            existing.dtend = end_dt
                            existing.all_day = all_day
                            existing.is_utc = row_is_utc
                            existing.rrule = rrule
                        else:
                            new_ev = CalendarEvent(
                                uid=uid_val,
                                calendar_id=local_cal.id,
                                summary=summary,
                                description=description,
                                location=location,
                                dtstart=start_dt,
                                dtend=end_dt,
                                all_day=all_day,
                                is_utc=row_is_utc,
                                rrule=rrule,
                            )
                            db.add(new_ev)
                            pending[uid_val] = new_ev
                        result["events"] += 1
                db.commit()

                # Prune locally-cached CalDAV events that vanished
                # upstream (only within our sync window — events outside
                # the window aren't in `objs`, so we'd false-delete them).
                stale = db.query(CalendarEvent).filter(
                    CalendarEvent.calendar_id == local_cal.id,
                    CalendarEvent.dtstart >= start,
                    CalendarEvent.dtstart <= end,
                    ~CalendarEvent.uid.in_(seen_uids) if seen_uids else CalendarEvent.uid.isnot(None),
                ).all()
                for ev in stale:
                    db.delete(ev)
                result["deleted"] += len(stale)
                db.commit()
            except Exception as e:
                logger.exception("CalDAV sync failed for one calendar")
                result["errors"].append(str(e)[:200])
                db.rollback()
    finally:
        db.close()


def google_enabled(prefs: dict) -> bool:
    return bool(((prefs or {}).get("google_calendar") or {}).get("enabled"))


def _merge(a: dict, b: dict) -> dict:
    return {k: (a.get(k, 0) + b.get(k, 0)) if k != "errors" else a.get("errors", []) + b.get("errors", [])
            for k in ("calendars", "events", "deleted", "errors")}


async def sync_caldav(owner: str) -> dict:
    """Pull CalDAV state into local DB for `owner`. Returns counts +
    errors. Loads credentials from the user's prefs; no-ops with a
    clear error if CalDAV isn't configured.

    RUFFS: también trae Google Calendar si está conectado con Google
    (independiente de un CalDAV propio: pueden convivir)."""
    from routes.prefs_routes import _load_for_user

    prefs = _load_for_user(owner) or {}
    cfg = prefs.get("caldav", {}) or {}
    url = (cfg.get("url") or "").strip()
    user = (cfg.get("username") or "").strip()
    pw = cfg.get("password") or ""
    try:
        from src.secret_storage import decrypt
        pw = decrypt(pw)
    except Exception:
        pass
    result = {"calendars": 0, "events": 0, "deleted": 0, "errors": []}
    has_caldav = bool(url and user and pw)
    has_google = google_enabled(prefs)
    if not (has_caldav or has_google):
        return {
            "calendars": 0, "events": 0, "deleted": 0,
            "errors": ["CalDAV is not configured"],
        }
    if has_caldav:
        try:
            url = validate_caldav_url(url)
            result = _merge(result, await asyncio.to_thread(_sync_blocking, owner, url, user, pw))
        except ValueError as e:
            result["errors"].append(str(e))
        except Exception as e:
            logger.exception("CalDAV sync raised")
            result["errors"].append(str(e)[:200])
    if has_google:
        try:
            result = _merge(result, await asyncio.to_thread(_sync_google_blocking, owner))
        except Exception as e:
            logger.exception("Google Calendar sync raised")
            result["errors"].append(f"Google: {str(e)[:200]}")
    return result


_SYNC_INTERVAL_SECONDS = 10 * 60


async def caldav_background_loop() -> None:
    """RUFFS: sincroniza cada 10 minutos los calendarios de todos los usuarios
    con CalDAV o Google conectado (antes solo se traían al abrir el
    calendario, así que lo creado en el móvil no aparecía hasta entonces)."""
    await asyncio.sleep(90)
    while True:
        try:
            from routes.prefs_routes import _load
            users = (_load() or {}).get("_users") or {}
            for owner, prefs in list(users.items()):
                caldav_cfg = (prefs or {}).get("caldav") or {}
                if caldav_cfg.get("url") or google_enabled(prefs):
                    res = await sync_caldav(owner)
                    if res.get("errors"):
                        logger.info("Sincronización de calendario (%s): %s", owner, "; ".join(res["errors"])[:300])
        except Exception as e:  # noqa: BLE001
            logger.debug(f"caldav_background_loop: {e}")
        await asyncio.sleep(_SYNC_INTERVAL_SECONDS)
