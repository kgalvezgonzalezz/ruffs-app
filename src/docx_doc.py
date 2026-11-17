"""RUFFS: documentos de Word (.docx) en el editor de documentos.

Cómo funciona
-------------
* Al importar, el .docx original se guarda tal cual (data/docx_sources/<id>.docx)
  y el editor recibe su texto: una línea por párrafo (``#`` para títulos, ``-``
  para listas, ``**negrita**`` / ``*cursiva*``), las tablas como tablas Markdown y
  ``![imagen]`` donde hay imágenes. La primera línea es la marca
  ``<!-- docx_source id="…" -->`` que enlaza el texto con su original.
* La vista «Word» del editor muestra el .docx con su formato real (página,
  fuentes, colores, tablas, imágenes, encabezados…).
* Al descargar en Word:
    - si el texto no se ha tocado, se entrega el archivo ORIGINAL, idéntico;
    - si se ha editado, se aplican los cambios SOBRE el original: los párrafos
      sin cambios quedan intactos; los cambiados conservan su estilo y el
      formato de su primer fragmento (fuente, tamaño, color); los nuevos copian
      el formato del párrafo anterior; los borrados desaparecen. Encabezados,
      pies, márgenes, imágenes y estilos del documento no se tocan. En las
      tablas se actualizan las celdas si la tabla mantiene filas y columnas.
"""
from __future__ import annotations

import copy
import difflib
import io
import os
import re
import uuid
from typing import List, Optional, Tuple

MARKER_RE = re.compile(r'<!--\s*docx_source\s+id="([0-9a-f]{32})"\s*-->')
MAX_DOCX_BYTES = 50 * 1024 * 1024
IMAGE_TOKEN = "![imagen]"


# ─── Almacén de originales ───────────────────────────────────────────────────

def _store_dir() -> str:
    from src.constants import DATA_DIR
    d = os.path.join(DATA_DIR, "docx_sources")
    os.makedirs(d, exist_ok=True)
    return d


def source_path(source_id: str) -> Optional[str]:
    if not re.fullmatch(r"[0-9a-f]{32}", source_id or ""):
        return None
    p = os.path.join(_store_dir(), source_id + ".docx")
    return p if os.path.isfile(p) else None


def store_original(data: bytes) -> str:
    if len(data) > MAX_DOCX_BYTES:
        raise ValueError("El archivo .docx supera los 50 MB.")
    if not data.startswith(b"PK"):
        raise ValueError("No parece un archivo .docx válido (no es un ZIP de Office).")
    sid = uuid.uuid4().hex
    with open(os.path.join(_store_dir(), sid + ".docx"), "wb") as f:
        f.write(data)
    return sid


def find_source_id(content: str) -> Optional[str]:
    m = MARKER_RE.search(content or "")
    return m.group(1) if m else None


# ─── .docx → texto del editor ────────────────────────────────────────────────

def _open(data_or_path):
    from docx import Document
    if isinstance(data_or_path, (bytes, bytearray)):
        return Document(io.BytesIO(data_or_path))
    return Document(data_or_path)


def _body_blocks(doc) -> List[Tuple[str, object]]:
    """[('p', Paragraph) | ('tbl', Table)] en el orden del cuerpo."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    out = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            out.append(("p", Paragraph(child, doc)))
        elif tag == "tbl":
            out.append(("tbl", Table(child, doc)))
    return out


def _heading_level(par) -> int:
    try:
        name = (par.style.name or "").lower()
        sid = (par.style.style_id or "").lower()
    except Exception:  # noqa: BLE001
        return 0
    if name == "title" or sid == "title" or name == "título":
        return 1
    m = re.search(r"(?:heading|t[ií]tulo|ttulo)\s*(\d)", name) or re.search(r"(?:heading|ttulo|titulo)(\d)", sid)
    return min(int(m.group(1)), 6) if m else 0


def _is_list(par) -> bool:
    ppr = par._p.pPr
    if ppr is not None and ppr.numPr is not None:
        return True
    try:
        return "list" in (par.style.name or "").lower()
    except Exception:  # noqa: BLE001
        return False


def _has_image(el) -> bool:
    return bool(el.xpath(".//*[local-name()='drawing' or local-name()='pict']"))


def _runs_markdown(par) -> str:
    parts = []
    for run in par.runs:
        t = run.text
        if not t:
            continue
        lead = t[: len(t) - len(t.lstrip())]
        trail = t[len(t.rstrip()):]
        core = t.strip()
        if core and run.bold and run.italic:
            core = f"***{core}***"
        elif core and run.bold:
            core = f"**{core}**"
        elif core and run.italic:
            core = f"*{core}*"
        parts.append(lead + core + trail)
    text = "".join(parts)
    return text.replace("****", "").replace("\n", " ")


def paragraph_line(par) -> str:
    text = _runs_markdown(par)
    if _has_image(par._p):
        text = (text + " " + IMAGE_TOKEN).strip()
    level = _heading_level(par)
    if level and text:
        return "#" * level + " " + re.sub(r"^\*\*(.*)\*\*$", r"\1", text)
    if _is_list(par) and text:
        return "- " + text
    if text.startswith(("|", "#", "- ", "<!--")):
        text = "\\" + text
    return text


def _cell_text(cell) -> str:
    return " ".join(p.text.strip() for p in cell.paragraphs if p.text.strip()).replace("|", "\\|")


def table_lines(tbl) -> List[str]:
    rows = []
    for r in tbl.rows:
        try:
            rows.append([_cell_text(c) for c in r.cells])
        except Exception:  # noqa: BLE001
            rows.append([])
    if not rows:
        return ["| |"]
    ncol = max(len(r) for r in rows) or 1
    lines = ["| " + " | ".join(r + [""] * (ncol - len(r))) + " |" for r in rows]
    return [lines[0], "|" + "---|" * ncol] + lines[1:]


def to_editor_text(data_or_path, source_id: str) -> str:
    doc = _open(data_or_path)
    lines = [f'<!-- docx_source id="{source_id}" -->', ""]
    for kind, obj in _body_blocks(doc):
        if kind == "p":
            lines.append(paragraph_line(obj))
        else:
            lines.extend(table_lines(obj))
    return "\n".join(lines).rstrip() + "\n"


# ─── Texto del editor → bloques ──────────────────────────────────────────────

def _split_cells(line: str) -> List[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|") and not inner.endswith("\\|"):
        inner = inner[:-1]
    cells = re.split(r"(?<!\\)\|", inner)
    return [c.strip().replace("\\|", "|") for c in cells]


def parse_editor_text(content: str) -> List[Tuple[str, object]]:
    """[('p', línea) | ('tbl', [[celdas]...])] a partir del texto del editor."""
    lines = (content or "").replace("\r\n", "\n").split("\n")
    # quitar la marca y la línea en blanco que la sigue
    if lines and MARKER_RE.search(lines[0]):
        lines = lines[1:]
        if lines and not lines[0].strip():
            lines = lines[1:]
    while lines and not lines[-1].strip():
        lines.pop()
    blocks: List[Tuple[str, object]] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                if not re.fullmatch(r"\s*\|?(\s*:?-{2,}:?\s*\|)+\s*", lines[i]):
                    rows.append(_split_cells(lines[i]))
                i += 1
            blocks.append(("tbl", rows))
            continue
        blocks.append(("p", ln))
        i += 1
    return blocks


def _strip_inline(text: str) -> str:
    t = re.sub(r"(\*\*\*|\*\*|\*)(.+?)\1", r"\2", text)
    return t.replace(IMAGE_TOKEN, "").strip()


def _line_parts(line: str) -> Tuple[int, bool, str]:
    """(nivel de título, es lista, texto sin prefijo)."""
    m = re.match(r"^(#{1,6})\s+(.*)$", line)
    if m:
        return len(m.group(1)), False, m.group(2)
    if line.startswith("- "):
        return 0, True, line[2:]
    if line.startswith("\\"):
        return 0, False, line[1:]
    return 0, False, line


def _key(block) -> str:
    kind, val = block
    if kind == "tbl":
        return "T:" + "\x1f".join("\x1e".join(r) for r in val)
    lvl, is_list, text = _line_parts(val)
    return f"P{lvl}{int(is_list)}:" + re.sub(r"\s+", " ", _strip_inline(text))


# ─── Aplicar cambios sobre el original ───────────────────────────────────────

def _segments(text: str) -> List[Tuple[str, bool, bool]]:
    out = []
    pos = 0
    for m in re.finditer(r"(\*\*\*|\*\*|\*)(.+?)\1", text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False, False))
        mark = m.group(1)
        out.append((m.group(2), len(mark) >= 2, len(mark) in (1, 3)))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False, False))
    return [s for s in out if s[0]]


def _set_paragraph(par, line: str, doc) -> None:
    from docx.oxml.ns import qn
    lvl, _is_list_line, text = _line_parts(line)
    keep_images = IMAGE_TOKEN in text
    text = text.replace(IMAGE_TOKEN, "").rstrip()
    runs = list(par._p.iterchildren(qn("w:r")))
    base = next((r for r in runs if r.find(qn("w:t")) is not None), runs[0] if runs else None)
    base_rpr = copy.deepcopy(base.find(qn("w:rPr"))) if base is not None and base.find(qn("w:rPr")) is not None else None
    for r in runs:
        if keep_images and _has_image(r):
            continue
        par._p.remove(r)
    for hl in list(par._p.iterchildren(qn("w:hyperlink"))):
        par._p.remove(hl)
    base_bold = base_rpr is not None and base_rpr.find(qn("w:b")) is not None
    base_ital = base_rpr is not None and base_rpr.find(qn("w:i")) is not None
    for seg, bold, ital in _segments(text):
        run = par.add_run(seg)
        if base_rpr is not None:
            run._r.insert(0, copy.deepcopy(base_rpr))
        run.bold = True if bold else (False if base_bold else None)
        run.italic = True if ital else (False if base_ital else None)
    if lvl and _heading_level(par) != lvl:
        for name in (f"Heading {lvl}", f"Título {lvl}"):
            try:
                par.style = doc.styles[name]
                break
            except KeyError:
                continue


def _set_table(tbl, rows) -> bool:
    try:
        cells = [[c for c in r.cells] for r in tbl.rows]
    except Exception:  # noqa: BLE001
        return False
    if len(cells) != len(rows) or any(len(a) != len(b) for a, b in zip(cells, rows)):
        return False
    for orig_row, new_row in zip(cells, rows):
        for cell, text in zip(orig_row, new_row):
            if _cell_text(cell).replace("\\|", "|") == text:
                continue
            pars = cell.paragraphs
            first = pars[0]
            for extra in pars[1:]:
                extra._p.getparent().remove(extra._p)
            first_runs = first.runs
            if first_runs:
                first_runs[0].text = text
                for r in first_runs[1:]:
                    r._r.getparent().remove(r._r)
            else:
                first.add_run(text)
    return True


def _new_paragraph_after(anchor_el, template_par, line: str, doc):
    from docx.text.paragraph import Paragraph
    if template_par is not None:
        new_p = copy.deepcopy(template_par._p)
    else:
        from docx.oxml import OxmlElement
        new_p = OxmlElement("w:p")
    if anchor_el is None:
        body = doc.element.body
        body.insert(0, new_p)
    else:
        anchor_el.addnext(new_p)
    par = Paragraph(new_p, doc)
    if template_par is not None:
        # sin imágenes ni campos heredados del modelo
        for r in list(new_p):
            if r.tag.rsplit("}", 1)[-1] not in ("pPr",):
                new_p.remove(r)
        tr = template_par.runs[0]._r if template_par.runs else None
        if tr is not None:
            from docx.oxml.ns import qn
            rpr = tr.find(qn("w:rPr"))
            r = copy.deepcopy(tr)
            for ch in list(r):
                if ch is not rpr and ch.tag.rsplit("}", 1)[-1] != "rPr":
                    r.remove(ch)
            new_p.append(r)
    _set_paragraph(par, line, doc)
    return par


def _table_as_text(rows) -> str:
    return " | ".join(" · ".join(r) for r in rows)


def export_bytes(source_file: str, content: str) -> Tuple[bytes, List[str]]:
    """El .docx con los cambios del editor aplicados sobre el original.

    Devuelve (bytes, avisos). Sin cambios → el archivo original, idéntico."""
    with open(source_file, "rb") as f:
        original = f.read()
    doc = _open(original)
    orig_blocks = _body_blocks(doc)
    orig_keys = []
    for kind, obj in orig_blocks:
        if kind == "p":
            orig_keys.append(_key(("p", paragraph_line(obj))))
        else:
            orig_keys.append(_key(("tbl", parse_editor_text("\n".join(table_lines(obj)))[0][1])))
    # Los párrafos vacíos del final no aparecen en el editor (se recortan): se
    # dejan fuera de la comparación y se conservan en el archivo.
    while orig_keys and orig_keys[-1] == "P00:":
        orig_keys.pop()
        orig_blocks.pop()
    new_blocks = parse_editor_text(content)
    new_keys = [_key(b) for b in new_blocks]
    if orig_keys == new_keys:
        return original, []

    warnings: List[str] = []
    sm = difflib.SequenceMatcher(None, orig_keys, new_keys, autojunk=False)
    last_par = None   # último párrafo «vivo», para copiar su formato
    anchor = None     # último elemento «vivo» del cuerpo (None = principio)

    def pair_by_kind(olds, news):
        """Empareja dentro de un tramo cambiado: párrafo con párrafo, tabla con
        tabla, en orden; lo que sobra se borra (viejo) o se inserta (nuevo)."""
        pairs, oi = [], 0
        for nb in news:
            k = next((x for x in range(oi, len(olds)) if olds[x][0] == nb[0]), None)
            if k is None:
                pairs.append((None, nb))
                continue
            for x in range(oi, k):
                pairs.append((olds[x], None))
            pairs.append((olds[k], nb))
            oi = k + 1
        for x in range(oi, len(olds)):
            pairs.append((olds[x], None))
        return pairs

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i1, i2):
                if orig_blocks[k][0] == "p":
                    last_par = orig_blocks[k][1]
            anchor = orig_blocks[i2 - 1][1]._element
            continue
        for old, new in pair_by_kind(orig_blocks[i1:i2], new_blocks[j1:j2]):
            if old is not None and new is not None:
                okind, oobj = old
                if okind == "p":
                    _set_paragraph(oobj, new[1], doc)
                    last_par = oobj
                elif not _set_table(oobj, new[1]):
                    warnings.append("Una tabla cambió de filas o columnas: se mantuvo la original.")
                anchor = oobj._element
            elif old is not None:          # borrado
                el = old[1]._element
                el.getparent().remove(el)
            else:                          # nuevo
                nkind, nval = new
                if nkind == "tbl":
                    warnings.append("Una tabla nueva se guardó como texto.")
                line = nval if nkind == "p" else _table_as_text(nval)
                par = _new_paragraph_after(anchor, last_par, line, doc)
                last_par = par
                anchor = par._element
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue(), sorted(set(warnings))


# ─── Crear el documento del editor ───────────────────────────────────────────

def create_document(data: bytes, title: str, owner: Optional[str], session_id: Optional[str] = None) -> str:
    """Guarda el original, crea el Document del editor (activo) y devuelve su id."""
    from core.database import Document, DocumentVersion, SessionLocal
    source_id = store_original(data)
    content = to_editor_text(source_path(source_id), source_id)
    db = SessionLocal()
    try:
        doc_id = str(uuid.uuid4())
        doc = Document(id=doc_id, session_id=session_id, title=title or "Documento", language="markdown",
                       current_content=content, version_count=1, is_active=True, owner=owner)
        ver = DocumentVersion(id=str(uuid.uuid4()), document_id=doc_id, version_number=1, content=content,
                              summary="Importado de Word (.docx)", source="upload")
        db.add(doc)
        db.add(ver)
        db.commit()
        return doc_id
    finally:
        db.close()
