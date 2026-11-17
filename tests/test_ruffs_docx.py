"""RUFFS: documentos Word (.docx) en el editor — importar, ver, editar y descargar
conservando el formato."""
import io

import pytest

docx = pytest.importorskip("docx")
from docx.enum.text import WD_COLOR_INDEX  # noqa: E402
from docx.shared import Pt, RGBColor  # noqa: E402

import src.docx_doc as dd  # noqa: E402


def _png() -> bytes:
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (40, 20), (200, 30, 30)).save(b, "PNG")
    return b.getvalue()


def make_docx() -> bytes:
    d = docx.Document()
    d.sections[0].header.paragraphs[0].text = "Encabezado de empresa"
    d.add_heading("Informe trimestral", level=1)
    p = d.add_paragraph()
    r = p.add_run("Texto con fuente especial")
    r.font.name = "Georgia"; r.font.size = Pt(15); r.font.color.rgb = RGBColor(0x12, 0x34, 0x56)
    p2 = d.add_paragraph("Normal y ")
    p2.add_run("negrita").bold = True
    p2.add_run(" y ")
    p2.add_run("cursiva").italic = True
    d.add_paragraph("Primer punto", style="List Bullet")
    d.add_paragraph("Segundo punto", style="List Bullet")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "Mes"; t.cell(0, 1).text = "Ventas"
    t.cell(1, 0).text = "Enero"; t.cell(1, 1).text = "100"
    d.add_picture(io.BytesIO(_png()))
    d.add_paragraph("Párrafo final")
    b = io.BytesIO(); d.save(b)
    return b.getvalue()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import src.constants as c
    monkeypatch.setattr(c, "DATA_DIR", str(tmp_path))
    sid = dd.store_original(make_docx())
    return sid, dd.source_path(sid)


def test_import_text(store):
    sid, path = store
    text = dd.to_editor_text(path, sid)
    assert dd.find_source_id(text) == sid
    for expected in ("# Informe trimestral", "Texto con fuente especial", "Normal y **negrita** y *cursiva*",
                     "- Primer punto", "| Mes | Ventas |", "| Enero | 100 |", dd.IMAGE_TOKEN, "Párrafo final"):
        assert expected in text, expected


def test_unchanged_export_is_identical(store):
    sid, path = store
    text = dd.to_editor_text(path, sid)
    data, warns = dd.export_bytes(path, text)
    assert data == open(path, "rb").read() and not warns


def test_edits_keep_formatting(store):
    sid, path = store
    text = dd.to_editor_text(path, sid)
    text = (text.replace("Texto con fuente especial", "Texto CAMBIADO con fuente especial")
                .replace("| Enero | 100 |", "| Enero | 250 |")
                .replace("Párrafo final", "Párrafo final\nLínea nueva añadida")
                .replace("- Segundo punto\n", ""))
    data, warns = dd.export_bytes(path, text)
    assert not warns
    d = docx.Document(io.BytesIO(data))
    texts = [p.text for p in d.paragraphs]
    assert "Texto CAMBIADO con fuente especial" in texts and "Segundo punto" not in texts
    assert texts[texts.index("Párrafo final") + 1] == "Línea nueva añadida"
    edited = next(p for p in d.paragraphs if p.text.startswith("Texto CAMBIADO"))
    r = edited.runs[0]
    assert r.font.name == "Georgia" and r.font.size == Pt(15) and r.font.color.rgb == RGBColor(0x12, 0x34, 0x56)
    assert d.tables[0].cell(1, 1).text == "250" and d.tables[0].cell(0, 0).text == "Mes"
    assert d.sections[0].header.paragraphs[0].text == "Encabezado de empresa"      # encabezado intacto
    assert len(d.inline_shapes) == 1                                               # la imagen sigue
    assert next(p for p in d.paragraphs if p.text == "Informe trimestral").style.name == "Heading 1"
    assert next(p for p in d.paragraphs if p.text == "Primer punto").style.name == "List Bullet"


def test_bold_markers_become_bold(store):
    sid, path = store
    text = dd.to_editor_text(path, sid).replace("Normal y **negrita** y *cursiva*", "Ahora **todo esto** es *nuevo*")
    d = docx.Document(io.BytesIO(dd.export_bytes(path, text)[0]))
    p = next(p for p in d.paragraphs if p.text == "Ahora todo esto es nuevo")
    assert [(r.text, bool(r.bold), bool(r.italic)) for r in p.runs] == [
        ("Ahora ", False, False), ("todo esto", True, False), (" es ", False, False), ("nuevo", False, True)]


def test_table_shape_change_is_reported(store):
    sid, path = store
    text = dd.to_editor_text(path, sid).replace("| Enero | 100 |", "| Enero | 100 |\n| Febrero | 90 |")
    data, warns = dd.export_bytes(path, text)
    assert warns and docx.Document(io.BytesIO(data)).tables[0].cell(1, 0).text == "Enero"


def test_rejects_non_docx(tmp_path, monkeypatch):
    import src.constants as c
    monkeypatch.setattr(c, "DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        dd.store_original(b"esto no es un docx")


def test_real_guide_roundtrip(tmp_path, monkeypatch):
    """La guía de instalación real (14 imágenes, tablas, listas): sin cambios sale
    idéntica; con una edición conserva las imágenes."""
    import os
    import src.constants as c
    monkeypatch.setattr(c, "DATA_DIR", str(tmp_path))
    guide = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "docs", "guias", "Guia_instalacion_RUFFS_Windows.docx")
    if not os.path.isfile(guide):
        pytest.skip("sin la guía")
    data = open(guide, "rb").read()
    sid = dd.store_original(data)
    text = dd.to_editor_text(dd.source_path(sid), sid)
    assert dd.export_bytes(dd.source_path(sid), text)[0] == data
    edited = text.replace("# Antes de empezar", "# Antes de empezar (revisado)")
    d = docx.Document(io.BytesIO(dd.export_bytes(dd.source_path(sid), edited)[0]))
    assert len(d.inline_shapes) == 14
    assert any(p.text == "Antes de empezar (revisado)" for p in d.paragraphs)


@pytest.fixture()
def tool_env(tmp_path, monkeypatch):
    import os
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as database
    import src.constants as c
    import src.database as sdb
    import src.tool_execution as te
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    database.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", Session)
    monkeypatch.setattr(sdb, "SessionLocal", Session)
    monkeypatch.setattr(c, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(te, "_resolve_tool_path", lambda p: os.path.realpath(p))
    return tmp_path, Session


def test_agent_opens_docx_in_editor(tool_env):
    """«abre este .docx en el editor» → manage_documents import lo importa y lo abre."""
    import asyncio
    import json
    from src.tool_implementations import do_manage_documents
    tmp, Session = tool_env
    f = tmp / "Contrato cliente.docx"
    f.write_bytes(make_docx())
    res = asyncio.run(do_manage_documents(json.dumps({"action": "import", "path": str(f)}), owner="admin"))
    assert res.get("open_in_editor") and res["doc_id"] and res["title"] == "Contrato cliente"
    assert dd.find_source_id(res["content"]) and "Informe trimestral" in res["content"]
    from core.database import Document
    s = Session()
    d = s.query(Document).filter(Document.id == res["doc_id"]).one()
    assert d.owner == "admin" and dd.find_source_id(d.current_content)
    s.close()
    # texto plano también; formatos no admitidos, error claro
    t = tmp / "notas.md"
    t.write_text("# Notas\nhola", encoding="utf-8")
    r2 = asyncio.run(do_manage_documents(json.dumps({"action": "import", "path": str(t)}), owner="admin"))
    assert r2["open_in_editor"] and r2["language"] == "markdown" and "hola" in r2["content"]
    r3 = asyncio.run(do_manage_documents(json.dumps({"action": "import", "path": str(tmp / "x.pdf")}), owner="admin"))
    assert "error" in r3


def test_agent_tool_description_mentions_import():
    import src.agent_loop as al
    assert '"action": "import"' in al.TOOL_SECTIONS["manage_documents"]
