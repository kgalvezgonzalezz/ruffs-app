"""RUFFS: MCP de empleos — buscar, CV base, CV ajustado (Word/PDF), postular con
confirmación y registro. Sin red: las búsquedas se simulan."""

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def E(tmp_path, monkeypatch):
    monkeypatch.setenv("RUFFS_EMPLEOS_DIR", str(tmp_path / "empleos"))
    monkeypatch.syspath_prepend(str(ROOT / "mcp_servers"))
    sys.modules.pop("empleos_server", None)
    mod = importlib.import_module("empleos_server")
    fake = [{"id": "computrabajo:ABC", "fuente": "computrabajo", "titulo": "Desarrollador Backend Python",
             "empresa": "Acme", "ubicacion": "Santiago", "url": "https://cl.computrabajo.com/x-ABC",
             "extra": {"country": "cl", "offer_id": "ABC"}},
            {"id": "linkedin:123", "fuente": "linkedin", "titulo": "Desarrollador backend python", "empresa": "ACME",
             "url": "https://www.linkedin.com/jobs/view/123", "descripcion": "Buscamos Python, FastAPI, Docker y AWS."}]
    monkeypatch.setattr(mod, "_computrabajo_search", lambda *a: [fake[0]])
    monkeypatch.setattr(mod, "_getonbrd_search", lambda *a: [])
    for name in ("_chiletrabajos_search", "_trabajando_search", "_bne_search"):
        monkeypatch.setattr(mod, name, lambda *a: [])
    monkeypatch.setattr(mod, "_jobspy_search", lambda site, *a: [fake[1]] if site == "linkedin" else [])
    monkeypatch.setattr(mod, "_computrabajo_detail", lambda o: "Requisitos: Python, Django, PostgreSQL y liderazgo técnico.")
    return mod


def test_search_dedupes_and_details(E):
    r = E.buscar_ofertas("desarrollador python")
    assert r["total"] == 1 and r["errores"] == {}          # misma oferta en 2 sitios → 1
    d = E.ver_oferta("computrabajo:ABC")
    assert "PostgreSQL" in d["descripcion"]
    assert E.ver_oferta("no-existe").get("error")


def test_cv_documents_and_apply_flow(E, tmp_path, monkeypatch):
    E.buscar_ofertas("desarrollador python")
    assert E.preparar_postulacion("computrabajo:ABC")["error"].startswith("Falta el CV base")
    cv = tmp_path / "cv.md"
    cv.write_text("Ana Prueba. Desarrolladora con Python, Django y Docker.", encoding="utf-8")
    g = E.guardar_cv_base(ruta=str(cv), nombre="Ana Prueba", email="ana@example.com")
    assert g["ok"] and g["contacto"]["nombre"] == "Ana Prueba"
    p = E.preparar_postulacion("computrabajo:ABC")
    assert "python" in p["coincidencia"]["tiene"] and "postgresql" in p["coincidencia"]["faltan"]
    assert any("NUNCA inventes" in r for r in p["reglas"])
    c = E.crear_documentos("computrabajo:ABC", "# Ana Prueba\n\n## Perfil\nBackend **Python**.\n\n## Experiencia\n- APIs • Django",
                           "# Carta\n\nEstimados: me interesa el cargo.")
    for k in ("cv_docx", "cv_pdf", "carta_docx", "carta_pdf"):
        assert Path(c[k]).is_file() and Path(c[k]).stat().st_size > 500
    from pypdf import PdfReader
    assert "Ana Prueba" in PdfReader(c["cv_pdf"]).pages[0].extract_text()
    # postular: primero pide confirmación; sin CT_COOKIES es asistida
    monkeypatch.delenv("CT_COOKIES", raising=False)
    s = E.postular("computrabajo:ABC")
    assert s["requiere_confirmacion"] and "asistida" in s["como"].lower()
    opened = []
    monkeypatch.setattr(E.webbrowser, "open", lambda u: opened.append(u) or True)
    a = E.postular("computrabajo:ABC", confirmar=True)
    assert a["asistida"] and opened == ["https://cl.computrabajo.com/x-ABC"]
    # con sesión de Computrabajo se envía directo (simulado)
    monkeypatch.setenv("CT_COOKIES", "x=1")
    monkeypatch.setattr(E, "_computrabajo_apply", lambda o: {"ok": True, "mensaje": "Postulación enviada"})
    assert E.postular("computrabajo:ABC")["como"].startswith("Se enviará directamente")
    assert E.postular("computrabajo:ABC", confirmar=True)["enviada"]
    assert E.postular("computrabajo:ABC", confirmar=True).get("aviso") == "Ya postulaste a esta oferta."
    m = E.mis_postulaciones()
    assert m["por_estado"] == {"enviada": 1}
    assert E.marcar_postulacion("computrabajo:ABC", "entrevista")["ok"]
    assert E.marcar_postulacion("computrabajo:ABC", "x").get("error")


def test_integrated_in_ruffs():
    catalog = pytest.importorskip("src.mcp_catalog").catalog   # (el RUFFS personal no lo tiene)
    e = next(x for x in catalog() if x["key"] == "empleos")
    assert e["args"] == ["{ruffs_dir}/mcp_servers/empleos_server.py"] and e["command"] == "{ruffs_python}"
    from src.tool_index import _SERVER_ALIASES
    assert "postular" in _SERVER_ALIASES["empleos"] and "computrabajo" in _SERVER_ALIASES["empleos"]
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "python-jobspy>=1.1" in req and "fpdf2>=2.7" in req


def test_chilean_sites_newest_first_and_laborum_link(E, monkeypatch):
    import datetime as dt
    today = dt.date.today()
    MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
             "noviembre", "diciembre"]
    monkeypatch.setattr(E, "_chiletrabajos_search", lambda *a: [
        {"id": "chiletrabajos:1", "fuente": "chiletrabajos", "titulo": "Analista contable", "empresa": "Uno",
         "fecha": f"{(today - dt.timedelta(days=3)).day} de {MESES[(today - dt.timedelta(days=3)).month - 1]} de {(today - dt.timedelta(days=3)).year}",
         "url": "https://www.chiletrabajos.cl/trabajo/analista-1"}])
    monkeypatch.setattr(E, "_trabajando_search", lambda *a: [
        {"id": "trabajando:2", "fuente": "trabajando", "titulo": "Contador", "empresa": "Dos",
         "fecha": f"{today:%Y-%m-%d} 09:30", "url": "https://www.trabajando.cl/trabajo/2"},
        {"id": "trabajando:3", "fuente": "trabajando", "titulo": "Contador antiguo", "empresa": "Tres",
         "fecha": f"{today - dt.timedelta(days=40):%Y-%m-%d} 09:30", "url": "https://www.trabajando.cl/trabajo/3"}])
    monkeypatch.setattr(E, "_bne_search", lambda *a: [
        {"id": "bne:4", "fuente": "bne", "titulo": "Asistente contable", "empresa": "Cuatro",
         "fecha": f"{today - dt.timedelta(days=1):%Y-%m-%d}", "url": "https://www.bne.gob.cl/ofertas/4"}])
    r = E.buscar_ofertas("contable", sitios=["chiletrabajos", "trabajando", "bne", "laborum"])
    assert [o["id"] for o in r["ofertas"]] == ["trabajando:2", "bne:4", "chiletrabajos:1"]   # lo más nuevo primero
    assert r["ofertas"][0]["antiguedad"] == "hoy" and r["ofertas"][1]["antiguedad"] == "ayer"
    assert r["descartadas_por_antiguedad"] == 1                                            # más de 7 días
    assert r["enlaces_asistidos"]["laborum"] == "https://www.laborum.cl/empleos-busqueda-contable.html"
    # fuera de Chile no se consultan los sitios chilenos
    assert E.buscar_ofertas("contable", pais="peru", sitios=["bne"])["total"] == 0
    assert E._to_date("Hace 3 horas") == today and E._to_date("Publicada ayer") == today - dt.timedelta(days=1)
    assert E._to_date("20/11/2026") == dt.date(2026, 11, 20)


def test_cv_always_ats(E):
    E.buscar_ofertas("desarrollador python")
    E.guardar_cv_base(texto="Ana Prueba. Desarrolladora con Python, Django, PostgreSQL y Docker.", nombre="Ana Prueba",
                      email="usuario@example.com")
    p = E.preparar_postulacion("computrabajo:ABC")
    assert p["idioma_cv"] == "es" and "## Experiencia laboral" in p["plantilla_ats"]
    assert any("FORMATO ATS" in r for r in p["reglas"]) and any("EXACTOS" in r for r in p["reglas"])
    cv = ("# Ana Prueba\nDesarrolladora Backend\n\n## Resumen ⭐\nBackend con Python y Django.\n\n"
          "## Experiencia\n### Desarrolladora — Acme\nSantiago | 01/2020 – Actual\n- Construí APIs.\n\n"
          "## Formación académica\n### Ingeniería — U\n\n## Competencias\n- Python, Django")
    c = E.crear_documentos("computrabajo:ABC", cv, "")
    ats = c["ats"]
    assert ats["ok"] and ats["problemas"] == [] and 0 < ats["puntaje"] <= 100, ats
    assert any("PostgreSQL".lower() in a.lower() for a in ats["avisos"])   # está en el CV base, falta en el CV
    assert Path(c["cv_docx"]).name == "Ana_Prueba_CV_Desarrollador_Backend_Python.docx"
    import docx
    heads = [(x.style.name, x.text) for x in docx.Document(c["cv_docx"]).paragraphs if x.style.name.startswith("Heading")]
    assert ("Heading 1", "PERFIL PROFESIONAL") in heads and ("Heading 1", "HABILIDADES") in heads   # títulos estándar
    assert ("Heading 2", "Desarrolladora — Acme") in heads
    assert not any("⭐" in t for _, t in heads)
    bad = E.crear_documentos("computrabajo:ABC", cv + "\n\n| a | b |\n|---|---|\n| 1 | 2 |", "")
    assert bad.get("error") and "tablas" in bad["ats"]["problemas"][0]
    assert not E._ats_check("solo texto", {"descripcion": "Python"})["ok"]


def test_thinking_hidden_by_default():
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")
    assert "'text-emojis', 'show-thinking']);" in app
    assert "state['show-thinking'] !== true" in app
    assert '<input type="checkbox" data-ui-key="show-thinking">' in (ROOT / "static/index.html").read_text(encoding="utf-8")
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")
    assert "body.hide-thinking .thinking-section:not(.ruffs-collapsible):not(.live-think-active)" in css
    chat = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    assert chat.count("classList.remove('live-think-active')") == 3
    assert 'class="thinking-section ruffs-collapsible"' in (ROOT / "static/js/markdown.js").read_text(encoding="utf-8")
