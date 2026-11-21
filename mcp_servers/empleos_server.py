"""
empleos_server.py — RUFFS: búsqueda de empleo y postulaciones con CV ajustado.

Busca ofertas en Computrabajo, GetOnBoard, LinkedIn e Indeed (Chile por defecto,
o cualquier país), guarda tu CV base, prepara para cada oferta un CV y una carta
ajustados (Word y PDF, SOLO con información verdadera de tu CV) y te ayuda a
postular:

* Computrabajo: envía la postulación con tu sesión (variable CT_COOKIES), solo
  cuando el usuario lo confirma (``confirmar=true``).
* LinkedIn, Indeed, GetOnBoard y el resto: postulación asistida — abre la oferta
  en tu navegador con los documentos listos. Sus condiciones prohíben los bots
  que postulan solos (y restringen la cuenta), y piden CAPTCHA: el clic final es
  tuyo.

Todo queda en un registro (``mis_postulaciones``). Los datos viven en
``data/empleos`` de la carpeta de RUFFS.

Basado en ideas y código abierto (MIT) de:
* speedyapply/JobSpy — búsqueda en LinkedIn/Indeed (se usa la librería).
* georgegiosue/mcp-computrabajo — búsqueda y postulación en Computrabajo
  (portado a Python).
* AIHawk (feder-cr/Jobs_Applier_AI_Agent) — flujo «buscar → puntuar → ajustar CV
  → postular con confirmación».

Uso: python empleos_server.py   (MCP por stdio)
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sqlite3
import sys
import unicodedata
import webbrowser
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
from mcp.server.fastmcp import FastMCP  # noqa: E402

import logging as _logging
for _n in ("httpx", "httpcore", "JobSpy", "urllib3"):
    _logging.getLogger(_n).setLevel(_logging.WARNING)

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("RUFFS_EMPLEOS_DIR") or (ROOT / "data" / "empleos"))
DOCS = DATA / "documentos"
DB_PATH = DATA / "empleos.db"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0 Safari/537.36")
CT_COUNTRIES = {"chile": "cl", "peru": "pe", "perú": "pe", "colombia": "co", "mexico": "mx", "méxico": "mx",
                "argentina": "ar", "ecuador": "ec"}
ALL_SITES = ["computrabajo", "chiletrabajos", "trabajando", "bne", "getonbrd", "linkedin", "indeed"]
# Laborum protege su API contra lectura automática: se ofrece su búsqueda como enlace.
ASSISTED_SITES = ["laborum"]
CHILE_ONLY = {"chiletrabajos", "trabajando", "bne"}

mcp = FastMCP("ruffs-empleos", instructions=(
    "Búsqueda de empleo y postulaciones con CV ajustado. Las ofertas vienen de la más reciente a la "
    "más antigua: prioriza las recién publicadas. Flujo: buscar_ofertas → ver_oferta → "
    "preparar_postulacion (te da la oferta, el CV base y las reglas) → escribe el CV y la carta "
    "ajustados (solo datos verdaderos del CV base) → crear_documentos → postular (primero sin "
    "confirmar, enseña el resumen al usuario y vuelve a llamar con confirmar=true solo si dice que sí). "
    "Si falta el CV base, pide al usuario la ruta de su CV y usa guardar_cv_base."))


# ─── Base de datos ───────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS ofertas (
            id TEXT PRIMARY KEY, fuente TEXT, titulo TEXT, empresa TEXT, ubicacion TEXT,
            sueldo TEXT, modalidad TEXT, fecha TEXT, url TEXT, descripcion TEXT, extra TEXT, visto TEXT);
        CREATE TABLE IF NOT EXISTS postulaciones (
            oferta_id TEXT PRIMARY KEY, estado TEXT, cv_docx TEXT, cv_pdf TEXT, carta_docx TEXT,
            carta_pdf TEXT, nota TEXT, creado TEXT, actualizado TEXT);
        CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT);
    """)
    return con


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _get_setting(key: str, default: Any = None) -> Any:
    with _db() as con:
        row = con.execute("SELECT valor FROM ajustes WHERE clave=?", (key,)).fetchone()
    return json.loads(row["valor"]) if row else default


def _set_setting(key: str, value: Any) -> None:
    with _db() as con:
        con.execute("INSERT OR REPLACE INTO ajustes (clave, valor) VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))


def _save_offer(o: dict) -> None:
    with _db() as con:
        prev = con.execute("SELECT descripcion, extra FROM ofertas WHERE id=?", (o["id"],)).fetchone()
        desc = o.get("descripcion") or ""
        extra = dict(o.get("extra") or {})
        if prev:   # una búsqueda nueva no pisa el detalle completo ya leído
            if len(prev["descripcion"] or "") > len(desc):
                desc = prev["descripcion"]
            extra = {**json.loads(prev["extra"] or "{}"), **extra}
        con.execute(
            "INSERT OR REPLACE INTO ofertas (id, fuente, titulo, empresa, ubicacion, sueldo, modalidad, fecha, url, "
            "descripcion, extra, visto) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (o["id"], o.get("fuente"), o.get("titulo"), o.get("empresa"), o.get("ubicacion"), o.get("sueldo"),
             o.get("modalidad"), o.get("fecha"), o.get("url"), desc or "", json.dumps(extra), _now()))


def _offer(oferta_id: str) -> dict | None:
    with _db() as con:
        row = con.execute("SELECT * FROM ofertas WHERE id=?", (oferta_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["extra"] = json.loads(d.get("extra") or "{}")
    return d


# ─── Utilidades ──────────────────────────────────────────────────────────────

def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _fold(s)).strip("-")


def _http():
    import httpx
    return httpx.Client(timeout=30, follow_redirects=True,
                        headers={"User-Agent": UA, "Accept-Language": "es-CL,es;q=0.9,en;q=0.8"})


def _clean(text: str, limit: int = 12000) -> str:
    t = re.sub(r"[ \t]+", " ", text or "")
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:limit] + ("…" if len(t) > limit else "")


_MONTHS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
           "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12, "ene": 1, "feb": 2,
           "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12}


def _to_date(text: Any) -> _dt.date | None:
    """«2026-11-20 16:55», «Hace 3 horas», «Ayer», «Hace 2 días», «19 de Noviembre de 2026», epoch… → fecha."""
    if text is None or text == "":
        return None
    if isinstance(text, (int, float)) and text > 10**8:
        return _dt.datetime.fromtimestamp(text / (1000 if text > 10**11 else 1)).date()
    t = _fold(str(text)).strip()
    today = _dt.date.today()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)
    if m:
        try:
            return _dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    if re.search(r"\b(hoy|today|minuto|minutos|hora|horas|hour|hours|min|recien|just)\b", t):
        return today
    if re.search(r"\b(ayer|yesterday)\b", t):
        return today - _dt.timedelta(days=1)
    m = re.search(r"(\d+)\s*(dia|dias|day|days|d\b)", t)
    if m:
        return today - _dt.timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\s*(semana|semanas|week|weeks)", t)
    if m:
        return today - _dt.timedelta(weeks=int(m.group(1)))
    m = re.search(r"(\d+)\s*(mes|meses|month|months)", t)
    if m:
        return today - _dt.timedelta(days=30 * int(m.group(1)))
    m = re.search(r"(\d{1,2})\s*(?:de\s+)?([a-z]{3,10})\.?(?:\s*(?:de\s+)?(\d{4}))?", t)
    if m and m.group(2) in _MONTHS:
        y = int(m.group(3)) if m.group(3) else today.year
        try:
            d = _dt.date(y, _MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            return None
        return d.replace(year=y - 1) if d > today + _dt.timedelta(days=1) and not m.group(3) else d
    return None


def _ago(d: _dt.date | None) -> str:
    if not d:
        return ""
    n = (_dt.date.today() - d).days
    return "hoy" if n <= 0 else "ayer" if n == 1 else f"hace {n} días"


def _short(o: dict) -> dict:
    out = {k: o.get(k) for k in ("id", "fuente", "titulo", "empresa", "ubicacion", "sueldo", "modalidad", "url")
           if o.get(k)}
    d = _to_date(o.get("fecha"))
    if d:
        out["publicada"] = d.isoformat()
        out["antiguedad"] = _ago(d)
    elif o.get("fecha"):
        out["fecha"] = o.get("fecha")
    return out


# ─── Fuentes ─────────────────────────────────────────────────────────────────

def _computrabajo_search(query: str, location: str, country: str, n: int) -> list[dict]:
    from bs4 import BeautifulSoup
    cc = CT_COUNTRIES.get(_fold(country), country if country in CT_COUNTRIES.values() else "cl")
    url = f"https://{cc}.computrabajo.com/trabajo-de-{_slug(query)}"
    loc = (location or "").split(",")[0].strip()
    if loc and _fold(loc) not in ("chile", "remoto"):
        url += "-en-" + _slug("rmetropolitana" if _fold(loc) in ("santiago", "region metropolitana", "rm") else loc)
    out: list[dict] = []
    with _http() as h:
        for page in range(1, 4):
            r = h.get(url + (f"?p={page}" if page > 1 else ""))
            if r.status_code != 200:
                break
            arts = BeautifulSoup(r.text, "html.parser").select("article.box_offer")
            if not arts:
                break
            for a in arts:
                link = a.select_one("h2 a.js-o-link")
                if not link:
                    continue
                comp = a.select_one("[offer-grid-article-company-url]") or a.select_one("p.dFlex.vm_fx")
                place = a.select_one("p.fs16.fc_base.mt5:not(.dFlex) span.mr10")
                sal = a.select_one(".icon.i_salary")
                mod = a.select_one(".icon.i_home_office, .icon.i_home")
                date = a.select_one("p.fs13.fc_aux")
                out.append({
                    "id": f"computrabajo:{a.get('data-id')}", "fuente": "computrabajo",
                    "titulo": link.get_text(strip=True), "empresa": comp.get_text(" ", strip=True) if comp else "",
                    "ubicacion": place.get_text(" ", strip=True) if place else "",
                    "sueldo": sal.parent.get_text(" ", strip=True) if sal else "",
                    "modalidad": mod.parent.get_text(" ", strip=True) if mod else "",
                    "fecha": date.get_text(" ", strip=True) if date else "",
                    "url": f"https://{cc}.computrabajo.com{link.get('href', '').split('#')[0]}",
                    "extra": {"country": cc, "offer_id": a.get("data-id")},
                })
                if len(out) >= n:
                    return out
    return out


def _computrabajo_detail(o: dict) -> str:
    from bs4 import BeautifulSoup
    with _http() as h:
        r = h.get(o["url"])
    s = BeautifulSoup(r.text, "html.parser")
    box = s.select_one("div[div-link='oferta']")
    if not box:
        m = s.find(string=re.compile("Descripción de la oferta", re.I))
        box = m.find_parent(["div", "section"]) if m else None
    return _clean(box.get_text("\n", strip=True)) if box else ""


def _getonbrd_search(query: str, location: str, country: str, n: int) -> list[dict]:
    with _http() as h:
        r = h.get("https://www.getonbrd.com/api/v0/search/jobs",
                  params={"query": query, "per_page": min(max(n, 1), 50), "expand": '["company"]'},
                  headers={"Accept": "application/json"})
    out = []
    want = _fold(country or "")
    for it in (r.json().get("data") or []) if r.status_code == 200 else []:
        a = it.get("attributes") or {}
        countries = a.get("countries") or []
        if want and want not in ("mundo", "todos", "any") and countries and not any(want in _fold(c) for c in countries) \
                and a.get("remote_modality") != "fully_remote":
            continue
        lo, hi = a.get("min_salary"), a.get("max_salary")
        out.append({
            "id": f"getonbrd:{it.get('id')}", "fuente": "getonbrd", "titulo": a.get("title") or "",
            "empresa": (((a.get("company") or {}).get("data") or {}).get("attributes") or {}).get("name", ""),
            "ubicacion": ", ".join(countries), "sueldo": f"USD {lo}–{hi}" if lo or hi else "",
            "modalidad": a.get("remote_modality") or "",
            "fecha": _dt.datetime.fromtimestamp(a["published_at"]).strftime("%Y-%m-%d") if a.get("published_at") else "",
            "url": f"https://www.getonbrd.com/jobs/{it.get('id')}",
            "descripcion": _clean(_html_text((a.get("description") or "") + "\n" + (a.get("functions") or "")
                                             + "\n" + (a.get("desirable") or "") + "\n" + (a.get("benefits") or ""))),
        })
        if len(out) >= n:
            break
    return out


def _chiletrabajos_search(query: str, location: str, country: str, n: int) -> list[dict]:
    from bs4 import BeautifulSoup
    out: list[dict] = []
    with _http() as h:
        for page in range(0, 3):
            url = "https://www.chiletrabajos.cl/encuentra-un-empleo" + (f"/{page * 30}" if page else "")
            r = h.get(url, params={"2": query})
            if r.status_code != 200:
                break
            items = BeautifulSoup(r.text, "html.parser").select("div.job-item")
            if not items:
                break
            for it in items:
                a = it.select_one("a[href*='/trabajo/']")
                if not a:
                    continue
                href = a["href"] if a["href"].startswith("http") else "https://www.chiletrabajos.cl" + a["href"]
                jid = (re.search(r"-(\d+)/?$", href) or [None, href[-40:]])[1]
                title = a.get_text(strip=True)
                lines = [x.strip() for x in it.get_text("\n", strip=True).split("\n") if x.strip()]
                if title in lines:   # antes del título puede venir la inicial del logo
                    lines = lines[lines.index(title):]
                date = next((x for x in lines if _to_date(x)), "")
                company = lines[1].rstrip(",") if len(lines) > 1 and lines[1] != date else ""
                place = lines[2] if len(lines) > 2 and lines[2] != date else ""
                out.append({"id": f"chiletrabajos:{jid}", "fuente": "chiletrabajos", "titulo": title,
                            "empresa": company, "ubicacion": place, "fecha": date, "url": href})
                if len(out) >= n:
                    return out
    return out


def _chiletrabajos_detail(o: dict) -> str:
    from bs4 import BeautifulSoup
    with _http() as h:
        r = h.get(o["url"])
    s = BeautifulSoup(r.text, "html.parser")
    for sc in s.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or "{}")
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            parts = [_html_text(data.get("description") or "")]
            sal = (data.get("baseSalary") or {}).get("value") or {}
            if isinstance(sal, dict) and (sal.get("value") or sal.get("minValue")):
                parts.append(f"Sueldo: {sal.get('value') or sal.get('minValue')} {(data.get('baseSalary') or {}).get('currency', '')}")
            return _clean("\n".join(p for p in parts if p))
    box = s.select_one("div.job-item") or s.select_one("main")
    return _clean(box.get_text("\n", strip=True)) if box else ""


def _trabajando_search(query: str, location: str, country: str, n: int) -> list[dict]:
    out: list[dict] = []
    with _http() as h:
        for page in range(1, 4):
            r = h.get("https://www.trabajando.cl/api/searchjob", headers={"Accept": "application/json"},
                      params={"palabraClave": query, "pagina": page, "orden": "RANKING", "tipoOrden": "DESC"})
            if r.status_code != 200:
                break
            ofs = r.json().get("ofertas") or []
            if not ofs:
                break
            for o in ofs:
                jid = o.get("idOferta")
                out.append({"id": f"trabajando:{jid}", "fuente": "trabajando", "titulo": o.get("nombreCargo") or "",
                            "empresa": o.get("nombreEmpresa") or "", "ubicacion": o.get("ubicacion") or "",
                            "modalidad": o.get("nombreJornada") or "", "fecha": o.get("fechaPublicacion") or "",
                            "url": f"https://www.trabajando.cl/trabajo/{jid}",
                            "descripcion": _clean(_html_text(o.get("descripcionOferta") or ""))})
                if len(out) >= n:
                    return out
    return out


def _trabajando_detail(o: dict) -> str:
    jid = o["id"].split(":", 1)[1]
    with _http() as h:
        d = h.get(f"https://www.trabajando.cl/api/ofertas/{jid}", headers={"Accept": "application/json"}).json()
    parts = [_html_text(d.get("descripcionOferta") or "")]
    if d.get("requisitosMinimos"):
        parts.append("Requisitos mínimos:\n" + _html_text(d["requisitosMinimos"]))
    if d.get("habilidades"):
        parts.append("Habilidades: " + ", ".join(str(x.get("nombre") if isinstance(x, dict) else x) for x in d["habilidades"]))
    if d.get("aniosExperiencia"):
        parts.append(f"Experiencia: {d.get('nombreOperadorExperiencia') or ''} {d['aniosExperiencia']} años".strip())
    if d.get("mostrarSueldo") and d.get("sueldo"):
        parts.append(f"Sueldo: {d.get('sueldo')} {d.get('nombreMoneda') or ''}")
    if d.get("linkPostulacionExterno"):
        o.setdefault("extra", {})["postular_en"] = d["linkPostulacionExterno"]
    return _clean("\n".join(p for p in parts if p))


def _bne_search(query: str, location: str, country: str, n: int) -> list[dict]:
    with _http() as h:
        r = h.post("https://www.bne.gob.cl/api/ofertas/busqueda", params={"page": 0, "size": min(max(n, 1), 50)},
                   json={"texto": query}, headers={"Accept": "application/json"})
    out = []
    for o in (r.json().get("content") or []) if r.status_code == 200 else []:
        emp = o.get("empresario") or {}
        place = ", ".join(x for x in (_bne_name(o.get("comuna")), _bne_name(o.get("region"))) if x)
        out.append({"id": f"bne:{o.get('id')}", "fuente": "bne", "titulo": o.get("nombrePuesto") or "",
                    "empresa": emp.get("nombreComercial") or "", "ubicacion": place,
                    "sueldo": _bne_name(o.get("rangoSalarial")), "modalidad": _bne_name(o.get("jornadaLaboral")),
                    "fecha": o.get("fechaPublicacion") or "", "url": f"https://www.bne.gob.cl/ofertas/{o.get('id')}",
                    "descripcion": _clean(_html_text(o.get("descripcion") or ""))})
    return out


def _bne_name(v: Any) -> str:
    if isinstance(v, dict):
        return str(v.get("nombre") or v.get("descripcion") or v.get("glosa") or "")
    return str(v or "")


def _bne_detail(o: dict) -> str:
    jid = o["id"].split(":", 1)[1]
    with _http() as h:
        d = h.get(f"https://www.bne.gob.cl/api/ofertas/publica/{jid}", headers={"Accept": "application/json"}).json()
    keys = ("descripcion", "descripcionOferta", "funciones", "requisitos", "competencias", "beneficios")
    parts = [f"{k.capitalize()}:\n{_html_text(str(d[k]))}" for k in keys if d.get(k)]
    return _clean("\n\n".join(parts)) or _clean(json.dumps({k: v for k, v in d.items() if isinstance(v, (str, int))},
                                                           ensure_ascii=False))


def _laborum_link(query: str) -> str:
    return f"https://www.laborum.cl/empleos-busqueda-{_slug(query)}.html"


def _html_text(html: str) -> str:
    from bs4 import BeautifulSoup
    return BeautifulSoup(html or "", "html.parser").get_text("\n", strip=True)


def _jobspy_search(site: str, query: str, location: str, country: str, n: int, days: int) -> list[dict]:
    from jobspy import scrape_jobs
    df = scrape_jobs(site_name=[site], search_term=query, location=location or None, results_wanted=n,
                     country_indeed=(country or "chile").lower(), hours_old=max(days, 1) * 24,
                     linkedin_fetch_description=False, verbose=0)
    out = []
    for _, r in df.iterrows():
        def v(k):
            x = r.get(k)
            return "" if x is None or (isinstance(x, float) and x != x) else str(x)
        jid = v("id") or v("job_url")
        lo, hi, cur = v("min_amount"), v("max_amount"), v("currency")
        out.append({
            "id": f"{site}:{jid}", "fuente": site, "titulo": v("title"), "empresa": v("company"),
            "ubicacion": v("location"), "sueldo": f"{cur} {lo}–{hi}".strip() if lo or hi else "",
            "modalidad": "remoto" if v("is_remote") == "True" else "", "fecha": v("date_posted"),
            "url": v("job_url"), "descripcion": _clean(v("description")),
            "extra": {"job_url_direct": v("job_url_direct")},
        })
    return out


def _linkedin_detail(o: dict) -> str:
    m = re.search(r"/jobs/view/(?:[^/]*?-)?(\d+)", o.get("url") or "")
    if not m:
        return ""
    with _http() as h:
        r = h.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}")
    if r.status_code != 200:
        return ""
    from bs4 import BeautifulSoup
    box = BeautifulSoup(r.text, "html.parser").select_one(".show-more-less-html__markup, .description__text")
    return _clean(box.get_text("\n", strip=True)) if box else ""


# ─── Herramientas: búsqueda ──────────────────────────────────────────────────

@mcp.tool()
def buscar_ofertas(consulta: str, ubicacion: str = "Santiago, Chile", pais: str = "chile",
                   sitios: list[str] | None = None, cantidad: int = 10, dias: int = 7) -> dict:
    """Busca ofertas de empleo en varios sitios a la vez.

    consulta: cargo o palabras clave (ej. "desarrollador python", "analista contable").
    ubicacion: ciudad/región (ej. "Santiago, Chile", "Valparaíso", "Remoto").
    pais: país para Indeed/Computrabajo (chile, peru, colombia, mexico, argentina, usa, spain…).
    sitios: subconjunto de computrabajo, chiletrabajos, trabajando, bne, getonbrd, linkedin, indeed
    (por defecto, todos; chiletrabajos/trabajando/bne solo en Chile). laborum: se devuelve el enlace
    de su búsqueda (su sitio no permite la lectura automática).
    cantidad: ofertas por sitio (máx. 30). dias: antigüedad máxima (por defecto 7: lo más nuevo).
    Devuelve las ofertas de la MÁS RECIENTE a la más antigua, con id (úsalo en las demás
    herramientas), título, empresa, sueldo, fecha de publicación y url."""
    wanted = [x.lower().strip() for x in (sitios or ALL_SITES + ASSISTED_SITES)]
    sites = [x for x in wanted if x in ALL_SITES] or ([] if any(x in ASSISTED_SITES for x in wanted) else ALL_SITES)
    if _fold(pais or "chile") != "chile":
        sites = [x for x in sites if x not in CHILE_ONLY]
    n = max(1, min(int(cantidad or 10), 30))
    results, errors = [], {}
    for site in sites:
        try:
            if site == "computrabajo":
                found = _computrabajo_search(consulta, ubicacion, pais, n)
            elif site == "chiletrabajos":
                found = _chiletrabajos_search(consulta, ubicacion, pais, n)
            elif site == "trabajando":
                found = _trabajando_search(consulta, ubicacion, pais, n)
            elif site == "bne":
                found = _bne_search(consulta, ubicacion, pais, n)
            elif site == "getonbrd":
                found = _getonbrd_search(consulta, ubicacion, pais, n)
            else:
                found = _jobspy_search(site, consulta, ubicacion, pais, n, dias)
            for o in found:
                _save_offer(o)
            results.extend(found)
        except Exception as e:  # un sitio caído no tumba la búsqueda
            errors[site] = str(e)[:200]
    # Lo más nuevo primero; las que superan «dias» se descartan (las sin fecha van al final).
    limit = _dt.date.today() - _dt.timedelta(days=max(int(dias or 7), 1))
    results.sort(key=lambda o: _to_date(o.get("fecha")) or _dt.date.min, reverse=True)
    seen, uniq, older = set(), [], 0
    for o in results:   # la misma oferta publicada en varios sitios
        d = _to_date(o.get("fecha"))
        if d and d < limit:
            older += 1
            continue
        key = (_fold(o.get("titulo"))[:60], _fold(o.get("empresa"))[:40])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(_short(o))
    res = {"total": len(uniq), "ofertas": uniq, "errores": errors,
           "orden": "de la más reciente a la más antigua",
           "siguiente": "Usa ver_oferta(id) para leer una oferta y preparar_postulacion(id) para ajustar el CV."}
    if older:
        res["descartadas_por_antiguedad"] = older
    if "laborum" in wanted or sitios is None:
        res["enlaces_asistidos"] = {"laborum": _laborum_link(consulta)}
    return res


@mcp.tool()
def ver_oferta(oferta_id: str) -> dict:
    """Detalle completo de una oferta (descripción, requisitos, sueldo, url). Acepta el id
    de buscar_ofertas o la URL de una oferta de Computrabajo/LinkedIn/GetOnBoard/Indeed."""
    o = _offer(oferta_id) or _offer_from_url(oferta_id)
    if not o:
        return {"error": "No conozco esa oferta. Búscala con buscar_ofertas o pásame su URL."}
    # Trabajando y BNE traen un resumen en la búsqueda; el detalle tiene los requisitos.
    if not o.get("descripcion") or (o["fuente"] in ("trabajando", "bne") and not (o.get("extra") or {}).get("detalle")):
        try:
            if o["fuente"] == "computrabajo":
                o["descripcion"] = _computrabajo_detail(o)
            elif o["fuente"] == "linkedin":
                o["descripcion"] = _linkedin_detail(o)
            elif o["fuente"] == "chiletrabajos":
                o["descripcion"] = _chiletrabajos_detail(o)
            elif o["fuente"] == "trabajando":
                o["descripcion"] = _trabajando_detail(o) or o.get("descripcion") or ""
            elif o["fuente"] == "bne":
                o["descripcion"] = _bne_detail(o) or o.get("descripcion") or ""
            if o["fuente"] in ("trabajando", "bne"):
                o.setdefault("extra", {})["detalle"] = True
            elif o["fuente"] == "getonbrd":
                slug = o["id"].split(":", 1)[1]
                with _http() as h:
                    a = (h.get(f"https://www.getonbrd.com/api/v0/jobs/{slug}").json().get("data") or {}).get("attributes") or {}
                o["descripcion"] = _clean(_html_text("\n".join(a.get(k) or "" for k in ("description", "functions", "desirable", "benefits"))))
        except Exception as e:
            o["descripcion"] = ""
            o["aviso"] = f"No se pudo leer el detalle ({str(e)[:120]}). Ábrela: {o.get('url')}"
        if o.get("descripcion"):
            _save_offer(o)
    res = _short(o)
    res["descripcion"] = o.get("descripcion") or "(sin descripción: abre la URL)"
    if o.get("aviso"):
        res["aviso"] = o["aviso"]
    return res


def _offer_from_url(url: str) -> dict | None:
    u = (url or "").strip()
    if not u.startswith("http"):
        return None
    if "computrabajo.com" in u:
        m = re.search(r"-([0-9A-F]{32})", u)
        cc = (re.search(r"https?://(\w\w)\.computrabajo", u) or [None, "cl"])[1]
        o = {"id": f"computrabajo:{m.group(1) if m else _slug(u)[-40:]}", "fuente": "computrabajo", "url": u,
             "extra": {"country": cc, "offer_id": m.group(1) if m else ""}}
    elif "linkedin.com" in u:
        m = re.search(r"(\d{6,})", u)
        o = {"id": f"linkedin:{m.group(1) if m else _slug(u)[-40:]}", "fuente": "linkedin", "url": u}
    elif "getonbrd.com" in u:
        o = {"id": f"getonbrd:{u.rstrip('/').split('/')[-1]}", "fuente": "getonbrd", "url": u}
    elif "chiletrabajos.cl" in u:
        m = re.search(r"-(\d+)/?$", u)
        o = {"id": f"chiletrabajos:{m.group(1) if m else _slug(u)[-40:]}", "fuente": "chiletrabajos", "url": u}
    elif "trabajando.c" in u:
        m = re.search(r"/(\d{5,})", u)
        o = {"id": f"trabajando:{m.group(1) if m else _slug(u)[-40:]}", "fuente": "trabajando", "url": u}
    elif "bne.gob.cl" in u or "bne.cl" in u:
        m = re.search(r"/ofertas/(\d+)", u)
        o = {"id": f"bne:{m.group(1) if m else _slug(u)[-40:]}", "fuente": "bne", "url": u}
    else:
        o = {"id": f"web:{_slug(u)[-60:]}", "fuente": "web", "url": u}
    _save_offer(o)
    return _offer(o["id"])


# ─── Herramientas: CV ────────────────────────────────────────────────────────

def _read_document(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"No existe el archivo: {p}")
    ext = p.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages)
    if ext == ".docx":
        import docx
        d = docx.Document(str(p))
        parts = [para.text for para in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts)
    return p.read_text(encoding="utf-8", errors="replace")


@mcp.tool()
def guardar_cv_base(ruta: str = "", texto: str = "", nombre: str = "", email: str = "", telefono: str = "",
                    ciudad: str = "", linkedin: str = "") -> dict:
    """Guarda tu CV base (el completo, del que salen las versiones ajustadas).

    ruta: archivo .docx, .pdf, .md o .txt de tu CV (o usa `texto`). Los datos de
    contacto (nombre, email, teléfono, ciudad, linkedin) van en la cabecera de los CV."""
    try:
        content = _read_document(ruta) if ruta else (texto or "")
    except Exception as e:
        return {"error": str(e)}
    contact = {k: v for k, v in {"nombre": nombre, "email": email, "telefono": telefono, "ciudad": ciudad,
                                 "linkedin": linkedin}.items() if v}
    if content.strip():
        _set_setting("cv_base", _clean(content, 40000))
        if ruta:
            _set_setting("cv_base_ruta", str(Path(ruta).expanduser()))
    if contact:
        _set_setting("contacto", {**(_get_setting("contacto", {}) or {}), **contact})
    if not content.strip() and not contact:
        return {"error": "Dame la ruta de tu CV (Word/PDF/texto) o su texto."}
    cv = _get_setting("cv_base", "")
    return {"ok": True, "caracteres": len(cv), "contacto": _get_setting("contacto", {}),
            "vista_previa": cv[:600], "aviso": None if _get_setting("contacto") else
            "Faltan tus datos de contacto (nombre, email, teléfono): pásalos para la cabecera del CV."}


@mcp.tool()
def ver_cv_base() -> dict:
    """Muestra el CV base guardado y tus datos de contacto."""
    cv = _get_setting("cv_base", "")
    if not cv:
        return {"error": "Aún no hay CV base. Pide al usuario la ruta de su CV y usa guardar_cv_base."}
    return {"contacto": _get_setting("contacto", {}), "ruta_original": _get_setting("cv_base_ruta", ""), "cv": cv}


_STOP = set(_fold(w) for w in """a al algo con como de del el en entre es esta este for la las lo los mas para por que se sin su sus
the to un una uno y o of and in on with you your our we will be are is as at an or e u ser tener trabajo empresa equipo
experiencia años ano anos conocimiento conocimientos manejo nivel buscamos requisitos deseable funciones cargo nos
oferta ofertas trabajando trabajar proyecto proyectos equipos bajo realizar descripción laboral mensual contrato trato
jornada horas semanales semana presencial remoto híbrido hibrido somos persona personas buscando búsqueda integrarse
conjunto junto parte será sera serás estar día dias días lunes viernes empresa empresas cliente clientes nuestro
nuestra nuestros nuestras sobre desde hasta cada todo todos todas otros otras muy más también tambien según
puesto rol área area responsabilidades beneficios postular postulación requisito plus valorará valorara indefinido
sueldo renta líquido liquido bruto pesos clp usd chile santiago región region metropolitana comuna
job work team role you're we're about who what this that from have has must years year strong ability""".split())


def _keywords(text: str, top: int = 40) -> list[str]:
    words = re.findall(r"[a-záéíóúñü0-9#+.]{2,}", (text or "").lower())
    freq: dict[str, int] = {}
    for w in words:
        w = w.strip(".")
        if len(w) < 2 or _fold(w) in _STOP or re.fullmatch(r"[\d.,$]+", w):
            continue
        freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:top]]


@mcp.tool()
def preparar_postulacion(oferta_id: str) -> dict:
    """Junta la oferta y tu CV base, compara palabras clave (qué pide la oferta, qué
    tienes y qué falta) y entrega las reglas para escribir el CV y la carta ajustados.
    Después, escribe ambos en Markdown y llama a crear_documentos."""
    o = ver_oferta(oferta_id)
    if o.get("error"):
        return o
    cv = _get_setting("cv_base", "")
    if not cv:
        return {"error": "Falta el CV base. Pide al usuario la ruta de su CV y usa guardar_cv_base."}
    kw = _keywords(o.get("descripcion", "") + " " + (o.get("titulo") or ""))
    cvf = _fold(cv)
    have = [k for k in kw if _fold(k) in cvf]
    missing = [k for k in kw if _fold(k) not in cvf]
    score = round(100 * len(have) / max(len(kw), 1))
    return {
        "oferta": o, "cv_base": cv, "contacto": _get_setting("contacto", {}),
        "coincidencia": {"porcentaje_palabras_clave": score, "tiene": have[:25], "faltan": missing[:25]},
        "idioma_cv": _lang(o.get("descripcion", "") + " " + (o.get("titulo") or "")),
        "plantilla_ats": ATS_TEMPLATE[_lang(o.get("descripcion", "") + " " + (o.get("titulo") or ""))],
        "reglas": [
            "VERACIDAD: usa SOLO información verdadera del CV base. Reordena, resume y destaca lo que pide la "
            "oferta, pero NUNCA inventes experiencia, títulos, fechas, empresas, cifras ni habilidades.",
            "FORMATO ATS OBLIGATORIO (para pasar los filtros automáticos e IA de los reclutadores): sigue "
            "exactamente 'plantilla_ats' — una sola columna, títulos de sección estándar tal cual, orden "
            "cronológico inverso, sin tablas, columnas, imágenes, íconos, emojis ni símbolos decorativos.",
            "Encabezado: '# Nombre Apellido' y en la línea siguiente el título del cargo de la oferta (solo si "
            "la persona realmente tiene ese perfil). Los datos de contacto los pone el documento.",
            "PALABRAS CLAVE: usa los términos EXACTOS de la oferta (mismo nombre de herramientas, cargos y "
            "habilidades, p. ej. 'Excel avanzado', 'SAP FI', 'Power BI') cuando describan algo que la persona SÍ "
            "tiene. Ponlos en el Perfil, en las viñetas de experiencia y en Habilidades. Si una sigla es común, "
            "escribe ambas formas una vez: 'Inteligencia Artificial (IA)'.",
            "Experiencia: por cada trabajo '### Cargo — Empresa' y debajo 'Ciudad | MM/AAAA – MM/AAAA' (o "
            "'Actual'); 3-5 viñetas que empiecen con un verbo de acción y, si el CV base los tiene, resultados "
            "medibles.",
            "Lo que está en 'faltan' NO se agrega al CV; si es importante, menciónalo con honestidad en la carta.",
            "Largo: 1 página si hay menos de 5 años de experiencia, máximo 2. Idioma: 'idioma_cv'.",
            "Carta en Markdown: 3-4 párrafos breves, dirigida a la empresa y al cargo, con 2-3 logros concretos "
            "y las palabras clave principales.",
            "Luego llama crear_documentos(oferta_id, cv_markdown, carta_markdown): revisa el informe 'ats' que "
            "devuelve y, si marca problemas, corrige el Markdown y vuelve a llamarla.",
        ],
    }


# ─── ATS ─────────────────────────────────────────────────────────────────────

ATS_TEMPLATE = {
    "es": ("# Nombre Apellido\nTítulo del cargo al que postula\n\n## Perfil profesional\n3-4 líneas con años de "
           "experiencia, especialidad y las palabras clave principales de la oferta.\n\n## Experiencia laboral\n"
           "### Cargo — Empresa\nCiudad | MM/AAAA – MM/AAAA\n- Verbo de acción + qué hiciste + resultado medible.\n\n"
           "## Educación\n### Título — Institución\nCiudad | AAAA\n\n## Habilidades\n- Técnicas: …\n- Herramientas: …\n\n"
           "## Certificaciones\n- Nombre — Institución (AAAA)\n\n## Idiomas\n- Español: nativo\n- Inglés: nivel"),
    "en": ("# First Last\nTarget job title\n\n## Summary\n3-4 lines with years of experience, specialty and the "
           "posting's main keywords.\n\n## Work Experience\n### Job Title — Company\nCity | MM/YYYY – MM/YYYY\n"
           "- Action verb + what you did + measurable result.\n\n## Education\n### Degree — Institution\nCity | YYYY\n\n"
           "## Skills\n- Technical: …\n- Tools: …\n\n## Certifications\n- Name — Issuer (YYYY)\n\n## Languages\n"
           "- English: level\n- Spanish: native"),
}
ATS_SECTIONS = {
    "es": {"perfil": ["perfil profesional", "perfil", "resumen profesional", "resumen"],
           "experiencia": ["experiencia laboral", "experiencia profesional", "experiencia"],
           "educacion": ["educacion", "formacion academica", "formacion"],
           "habilidades": ["habilidades", "competencias", "conocimientos"]},
    "en": {"perfil": ["summary", "professional summary", "profile"],
           "experiencia": ["work experience", "experience", "professional experience"],
           "educacion": ["education"], "habilidades": ["skills", "technical skills"]},
}
_ATS_CANON = {"perfil": ("Perfil profesional", "Summary"), "experiencia": ("Experiencia laboral", "Work Experience"),
              "educacion": ("Educación", "Education"), "habilidades": ("Habilidades", "Skills")}
_DECOR = re.compile(r"[\u2600-\u27BF\u2B00-\u2BFF\U0001F000-\U0001FAFF\uFE0F\u200D\u2190-\u21FF\u25A0-\u25FF]")  # emojis, íconos, flechas y figuras


def _lang(text: str) -> str:
    t = " " + _fold(text) + " "
    en = sum(t.count(f" {w} ") for w in ("the", "and", "you", "with", "experience", "skills", "we", "our"))
    es = sum(t.count(f" {w} ") for w in ("el", "la", "de", "con", "experiencia", "y", "para", "los"))
    return "en" if en > es else "es"


def _ats_normalize(md: str, lang: str) -> str:
    """Títulos de sección → nombres estándar que reconocen los ATS."""
    out = []
    for line in (md or "").splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            name = _fold(_DECOR.sub("", m.group(1))).strip(" :️")
            for key, aliases in {**ATS_SECTIONS["es"], **{k: v + ATS_SECTIONS["en"][k] for k, v in ATS_SECTIONS["es"].items()}}.items():
                if name in aliases:
                    line = "## " + _ATS_CANON[key][0 if lang == "es" else 1]
                    break
        out.append(_DECOR.sub("", line).rstrip())
    return "\n".join(out)


def _ats_check(md: str, offer: dict) -> dict:
    """Revisa el CV como lo haría un filtro ATS: secciones, formato y palabras clave."""
    lang = _lang((offer.get("descripcion") or "") + " " + (offer.get("titulo") or ""))
    problems, warnings = [], []
    heads = [_fold(h).strip(" :") for h in re.findall(r"^##\s+(.+)$", md or "", re.M)]
    for key, aliases in ATS_SECTIONS[lang].items():
        if not any(h in aliases for h in heads):
            problems.append(f"Falta la sección estándar «{_ATS_CANON[key][0 if lang == 'es' else 1]}».")
    if not re.search(r"^#\s+\S", md or "", re.M):
        problems.append("Falta el nombre como título principal (# Nombre Apellido).")
    if re.search(r"^\s*\|.*\|\s*$", md or "", re.M):
        problems.append("Tiene tablas: los ATS las leen mal. Usa viñetas.")
    if re.search(r"!\[[^\]]*\]\(", md or ""):
        problems.append("Tiene imágenes: quítalas.")
    if _DECOR.search(md or ""):
        warnings.append("Tenía íconos o emojis: se quitaron.")
    words = len(re.findall(r"\w+", md or ""))
    if words < 180:
        warnings.append(f"Es muy corto ({words} palabras): agrega logros reales del CV base.")
    elif words > 1100:
        warnings.append(f"Es largo ({words} palabras): resume para que quepa en 1-2 páginas.")
    kw = _keywords((offer.get("descripcion") or "") + " " + (offer.get("titulo") or ""), 30)
    cv_base = _fold(_get_setting("cv_base", "") or "")
    relevant = [k for k in kw if _fold(k) in cv_base]          # solo lo que la persona sí tiene
    used = [k for k in relevant if _fold(k) in _fold(md or "")]
    missing = [k for k in relevant if k not in used]
    cover = round(100 * len(used) / max(len(relevant), 1))
    if missing:
        warnings.append("Palabras clave de la oferta que SÍ están en tu CV base pero no en este CV: "
                        + ", ".join(missing[:12]))
    score = max(0, 100 - 25 * len(problems) - 5 * len(warnings) - (100 - cover) // 4)
    return {"ok": not problems, "puntaje": score, "idioma": lang, "cobertura_palabras_clave": f"{cover}%",
            "problemas": problems, "avisos": warnings}


# ─── Documentos (DOCX + PDF) ─────────────────────────────────────────────────

def _md_blocks(md: str):
    for raw in (md or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            yield ("blank", "")
        elif line.startswith("# "):
            yield ("h1", line[2:].strip())
        elif line.startswith("## "):
            yield ("h2", line[3:].strip())
        elif line.startswith("### "):
            yield ("h3", line[4:].strip())
        elif re.match(r"^\s*[-*•]\s+", line):
            yield ("li", re.sub(r"^\s*[-*•]\s+", "", line))
        else:
            yield ("p", line.strip())


def _runs(text: str):
    """**negrita** → [(texto, negrita)]"""
    parts = re.split(r"(\*\*[^*]+\*\*)", text)
    return [(p[2:-2], True) if p.startswith("**") and p.endswith("**") else (p.replace("*", ""), False) for p in parts if p]


def _write_docx(md: str, path: Path, contact: dict) -> None:
    import docx
    from docx.shared import Pt, RGBColor
    d = docx.Document()
    st = d.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(10.5)
    for sec in d.sections:
        sec.left_margin = sec.right_margin = sec.top_margin = sec.bottom_margin = Pt(50)
    first_h1 = True
    for kind, text in _md_blocks(md):
        if kind == "blank":
            continue
        if kind == "h1":
            p = d.add_paragraph()
            r = p.add_run(text)
            r.bold, r.font.size = True, Pt(20)
            if first_h1 and contact:
                line = " · ".join(v for v in (contact.get("email"), contact.get("telefono"), contact.get("ciudad"),
                                               contact.get("linkedin")) if v)
                if line:
                    c = d.add_paragraph().add_run(line)
                    c.font.size, c.font.color.rgb = Pt(9.5), RGBColor(0x55, 0x55, 0x55)
            first_h1 = False
            continue
        if kind in ("h2", "h3"):
            # Estilos de título reales de Word: los ATS los usan para reconocer las secciones.
            p = d.add_heading(level=1 if kind == "h2" else 2)
            r = p.add_run(text.upper() if kind == "h2" else text)
            r.bold, r.font.size, r.font.name = True, Pt(12 if kind == "h2" else 11), "Calibri"
            r.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F) if kind == "h2" else RGBColor(0, 0, 0)
            continue
        p = d.add_paragraph(style="List Bullet" if kind == "li" else None)
        for t, b in _runs(text):
            r = p.add_run(t)
            r.bold = b
    d.core_properties.title = "Curriculum Vitae" if contact else "Carta de presentación"
    if contact.get("nombre"):
        d.core_properties.author = contact["nombre"]
    d.save(str(path))


def _latin1(s: str) -> str:
    rep = {"•": "-", "–": "-", "—": "-", "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...", "€": "EUR", "·": "-"}
    return "".join(rep.get(c, c) for c in s).encode("latin-1", "replace").decode("latin-1")


def _write_pdf(md: str, path: Path, contact: dict) -> None:
    from fpdf import FPDF
    pdf = FPDF(format="A4")
    pdf.set_margins(18, 16, 18)
    pdf.set_auto_page_break(True, 16)
    pdf.add_page()
    w = pdf.w - pdf.l_margin - pdf.r_margin
    first_h1 = True
    for kind, text in _md_blocks(md):
        t = _latin1(text)
        if kind == "blank":
            pdf.ln(1.5)
        elif kind == "h1":
            pdf.set_font("Helvetica", "B", 19)
            pdf.multi_cell(w, 9, t)
            if first_h1 and contact:
                line = " - ".join(v for v in (contact.get("email"), contact.get("telefono"), contact.get("ciudad"),
                                               contact.get("linkedin")) if v)
                if line:
                    pdf.set_font("Helvetica", "", 9)
                    pdf.set_text_color(85, 85, 85)
                    pdf.multi_cell(w, 5, _latin1(line))
                    pdf.set_text_color(0, 0, 0)
            first_h1 = False
            pdf.ln(2)
        elif kind in ("h2", "h3"):
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 11.5 if kind == "h2" else 10.5)
            pdf.set_text_color(31, 58, 95) if kind == "h2" else None
            pdf.multi_cell(w, 6, t.upper() if kind == "h2" else t)
            pdf.set_text_color(0, 0, 0)
            if kind == "h2":
                pdf.set_draw_color(180, 190, 205)
                pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + w, pdf.get_y())
                pdf.ln(1.5)
        else:
            pdf.set_font("Helvetica", "", 10)
            txt = re.sub(r"\*\*([^*]+)\*\*", r"\1", t).replace("*", "")
            if kind == "li":
                pdf.set_x(pdf.l_margin + 3)
                pdf.multi_cell(w - 3, 5, "- " + txt)
            else:
                pdf.multi_cell(w, 5, txt)
    pdf.output(str(path))


@mcp.tool()
def crear_documentos(oferta_id: str, cv_markdown: str, carta_markdown: str = "") -> dict:
    """Crea el CV ajustado (y la carta, si la das) en Word (.docx) y PDF para esa oferta,
    y la registra como «preparada». El Markdown debe seguir las reglas de preparar_postulacion."""
    o = _offer(oferta_id)
    if not o:
        return {"error": "Oferta desconocida: búscala primero con buscar_ofertas o ver_oferta."}
    if not (cv_markdown or "").strip():
        return {"error": "Falta el CV en Markdown."}
    contact = _get_setting("contacto", {}) or {}
    lang = _lang((o.get("descripcion") or "") + " " + (o.get("titulo") or ""))
    cv_markdown = _ats_normalize(cv_markdown, lang)
    ats = _ats_check(cv_markdown, o)
    if any("tablas" in p or "imágenes" in p for p in ats["problemas"]):
        return {"error": "El CV no cumple el formato ATS; corrígelo y vuelve a llamar.", "ats": ats}
    folder = DOCS / f"{_dt.date.today():%Y-%m-%d}_{_slug(o.get('empresa') or 'empresa')[:30]}_{_slug(o.get('titulo') or 'cargo')[:40]}"
    folder.mkdir(parents=True, exist_ok=True)
    who = "_".join(w.capitalize() for w in _slug(contact.get("nombre") or "cv").split("-") if w) or "CV"
    role = "_".join(w.capitalize() for w in _slug(o.get("titulo") or "").split("-")[:4] if w)
    files = {}
    # Nombre de archivo que entienden reclutadores y ATS: Nombre_Apellido_CV_Cargo
    files["cv_docx"] = folder / f"{who}_CV_{role}.docx".replace("_CV_.", "_CV.")
    files["cv_pdf"] = folder / f"{who}_CV_{role}.pdf".replace("_CV_.", "_CV.")
    _write_docx(cv_markdown, files["cv_docx"], contact)
    _write_pdf(cv_markdown, files["cv_pdf"], contact)
    if (carta_markdown or "").strip():
        files["carta_docx"] = folder / f"{who}_Carta.docx"
        files["carta_pdf"] = folder / f"{who}_Carta.pdf"
        _write_docx(carta_markdown, files["carta_docx"], {})
        _write_pdf(carta_markdown, files["carta_pdf"], {})
    (folder / "cv.md").write_text(cv_markdown, encoding="utf-8")
    if carta_markdown:
        (folder / "carta.md").write_text(carta_markdown, encoding="utf-8")
    with _db() as con:
        con.execute(
            "INSERT OR REPLACE INTO postulaciones (oferta_id, estado, cv_docx, cv_pdf, carta_docx, carta_pdf, nota, creado, actualizado) "
            "VALUES (?, 'preparada', ?, ?, ?, ?, '', COALESCE((SELECT creado FROM postulaciones WHERE oferta_id=?), ?), ?)",
            (oferta_id, str(files["cv_docx"]), str(files["cv_pdf"]), str(files.get("carta_docx") or ""),
             str(files.get("carta_pdf") or ""), oferta_id, _now(), _now()))
    return {"ok": True, "carpeta": str(folder), **{k: str(v) for k, v in files.items()}, "ats": ats,
            "siguiente": ("Corrige lo que marca 'ats' y vuelve a llamar crear_documentos." if not ats["ok"] else
                          "Enséñale al usuario los archivos y el puntaje ATS; para postular usa postular(oferta_id) "
                          "(primero sin confirmar).")}


# ─── Postular ────────────────────────────────────────────────────────────────

def _computrabajo_apply(o: dict) -> dict:
    import httpx
    cookies = os.environ.get("CT_COOKIES", "").strip()
    cc = (o.get("extra") or {}).get("country") or "cl"
    oid = (o.get("extra") or {}).get("offer_id") or o["id"].split(":", 1)[1]
    url = f"https://candidato.{cc}.computrabajo.com/candidate/apply/?oi={oid}&p=280&idb=1&d=32&lc=ListOffers&d=33"
    body = {"url": url, "urlLogin": f"https://candidato.{cc}.computrabajo.com/acceso/", "ismobile": "false",
            "oi": oid, "p": "280", "idb": "1", "d": "33", "lc": "ListOffers"}
    r = httpx.post(url, data=body, timeout=30, headers={
        "User-Agent": UA, "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "Cookie": cookies,
        "Origin": f"https://{cc}.computrabajo.com", "Referer": f"https://{cc}.computrabajo.com/"})
    try:
        data = r.json()
    except Exception:
        return {"ok": False, "mensaje": "Computrabajo respondió algo ilegible; revisa la oferta en el sitio."}
    code = str(data.get("result") or "").lower()
    if code == "offerappliedok":
        return {"ok": True, "mensaje": data.get("message") or "Postulación enviada"}
    msg = {"offernotvalid": "La oferta ya no recibe postulaciones.",
           "notloggeduser": "Computrabajo no reconoció la sesión: actualiza CT_COOKIES."}.get(code)
    return {"ok": False, "mensaje": data.get("message") or msg or f"Computrabajo rechazó la postulación ({code or 'sin motivo'})."}


@mcp.tool()
def postular(oferta_id: str, confirmar: bool = False, abrir_navegador: bool = True) -> dict:
    """Postula a una oferta.

    Sin confirmar: devuelve el resumen (oferta, documentos, cómo se enviará) para que
    el usuario lo apruebe. Con confirmar=true:
    * Computrabajo (si CT_COOKIES está configurado): envía la postulación con tu
      cuenta (usa el CV de tu perfil de Computrabajo; el CV ajustado queda listo
      para subirlo como adjunto si quieres).
    * LinkedIn, Indeed, GetOnBoard y otros: abre la oferta en tu navegador con los
      documentos listos para subir; el clic final lo das tú (sus reglas prohíben
      los bots y piden CAPTCHA). Luego usa marcar_postulacion."""
    o = _offer(oferta_id)
    if not o:
        return {"error": "Oferta desconocida."}
    with _db() as con:
        p = con.execute("SELECT * FROM postulaciones WHERE oferta_id=?", (oferta_id,)).fetchone()
    p = dict(p) if p else {}
    docs = {k: p.get(k) for k in ("cv_pdf", "cv_docx", "carta_pdf", "carta_docx") if p.get(k)}
    direct = o["fuente"] == "computrabajo" and bool(os.environ.get("CT_COOKIES", "").strip())
    if p.get("estado") == "enviada":
        return {"aviso": "Ya postulaste a esta oferta.", "oferta": _short(o), "postulacion": p}
    if not confirmar:
        return {
            "requiere_confirmacion": True, "oferta": _short(o), "documentos": docs or "(sin CV ajustado: usa preparar_postulacion)",
            "como": ("Se enviará directamente con tu cuenta de Computrabajo." if direct else
                     "Postulación asistida: se abrirá la oferta en tu navegador para que subas el CV ajustado y "
                     "des el clic final."),
            "instruccion": "Muestra esto al usuario y llama postular(oferta_id, confirmar=true) SOLO si dice que sí.",
        }
    if direct:
        res = _computrabajo_apply(o)
        _mark(oferta_id, "enviada" if res["ok"] else "error", res["mensaje"])
        return {"enviada": res["ok"], "mensaje": res["mensaje"], "oferta": _short(o)}
    opened = False
    if abrir_navegador and o.get("url"):
        try:
            opened = webbrowser.open(o["url"])
        except Exception:
            opened = False
    _mark(oferta_id, "abierta", "Postulación asistida abierta en el navegador")
    return {
        "asistida": True, "navegador_abierto": opened, "url": o.get("url"), "documentos": docs,
        "pasos": ["Inicia sesión en el sitio si te lo pide.", "Pulsa «Postular / Easy Apply / Aplicar».",
                  f"Sube el CV ajustado: {docs.get('cv_pdf') or docs.get('cv_docx') or '(prepáralo primero)'}",
                  "Revisa las preguntas del formulario y envía.",
                  "Avísale a RUFFS para marcarla como enviada (marcar_postulacion)."],
    }


def _mark(oferta_id: str, estado: str, nota: str = "") -> None:
    with _db() as con:
        con.execute(
            "INSERT INTO postulaciones (oferta_id, estado, nota, creado, actualizado) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(oferta_id) DO UPDATE SET estado=excluded.estado, nota=excluded.nota, actualizado=excluded.actualizado",
            (oferta_id, estado, nota, _now(), _now()))


@mcp.tool()
def marcar_postulacion(oferta_id: str, estado: str, nota: str = "") -> dict:
    """Actualiza el estado de una postulación: preparada, abierta, enviada, entrevista,
    rechazada, oferta, descartada (con una nota opcional)."""
    estados = {"preparada", "abierta", "enviada", "entrevista", "rechazada", "oferta", "descartada", "error"}
    e = (estado or "").strip().lower()
    if e not in estados:
        return {"error": f"Estado no válido. Usa: {', '.join(sorted(estados))}"}
    _mark(oferta_id, e, nota)
    return {"ok": True, "oferta_id": oferta_id, "estado": e}


@mcp.tool()
def mis_postulaciones(estado: str = "") -> dict:
    """Lista tus postulaciones (todas o de un estado) con la oferta y los documentos."""
    q = ("SELECT p.*, o.titulo, o.empresa, o.fuente, o.url FROM postulaciones p LEFT JOIN ofertas o ON o.id = p.oferta_id"
         + (" WHERE p.estado=?" if estado else "") + " ORDER BY p.actualizado DESC LIMIT 200")
    with _db() as con:
        rows = [dict(r) for r in con.execute(q, (estado.lower(),) if estado else ())]
    resumen: dict[str, int] = {}
    for r in rows:
        resumen[r["estado"]] = resumen.get(r["estado"], 0) + 1
    return {"total": len(rows), "por_estado": resumen, "postulaciones": rows}


if __name__ == "__main__":
    mcp.run()
