"""RUFFS: lo que RUFFS dice en voz alta al leer una respuesta (auto-play).

La respuesta escrita del chat no cambia. Cuando trae tablas, código, listas
largas o es muy extensa, leerla tal cual rompe la conversación, así que aquí se
prepara una versión para escuchar: 2–4 frases naturales en el mismo idioma.
Ese texto solo se usa para el audio: no se muestra ni se guarda en el chat.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

MAX_SPEAKABLE_CHARS = 600      # más largo que esto → resumen
MAX_INPUT_CHARS = 12000

_THINK_RE = re.compile(r"<think(?:ing)?[^>]*>[\s\S]*?(?:</think(?:ing)?>|$)", re.I)
_FENCE_RE = re.compile(r"```[\s\S]*?(?:```|$)")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.M)
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$", re.M)
_LIST_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.M)
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+", re.M)
_URL_RE = re.compile(r"https?://\S+|www\.\S+")


def strip_think(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


def needs_summary(text: str) -> bool:
    """¿Leerla tal cual sonaría mal? (tablas, código, listas largas, títulos, enlaces o muy larga)."""
    t = strip_think(text)
    if _FENCE_RE.search(t) or _TABLE_SEP_RE.search(t) or len(_TABLE_ROW_RE.findall(t)) >= 3:
        return True
    if len(_LIST_RE.findall(t)) >= 4 or len(_HEADING_RE.findall(t)) >= 2 or len(_URL_RE.findall(t)) >= 2:
        return True
    return len(plain_text(t)) > MAX_SPEAKABLE_CHARS


def plain_text(text: str) -> str:
    """Markdown → texto para leer (sin código, tablas, enlaces ni símbolos)."""
    t = strip_think(text)
    t = _FENCE_RE.sub(" ", t)
    t = _TABLE_ROW_RE.sub(" ", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)            # imágenes
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)         # [texto](url) → texto
    t = _URL_RE.sub(" ", t)
    t = _HEADING_RE.sub("", t)
    t = _LIST_RE.sub("", t)
    t = re.sub(r"[*_~>#|]+", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n\n", t)
    return t.strip()


def _closing(lang_sample: str) -> str:
    english = bool(re.search(r"\b(the|and|you|is|are|with)\b", lang_sample.lower())) and not re.search(
        r"\b(el|la|los|las|que|de|y|es|con)\b", lang_sample.lower())
    return "The details are on screen." if english else "Te dejé el detalle en pantalla."


_DANGLING = re.compile(r"\s+(?:en|a|de|del|desde|aquí|acá|aqui|at|in|on|here|from)\s*[.:]?$", re.I)


def _tidy_sentence(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    # «Puedes ver más en <enlace>» sin el enlace → «Puedes ver más.»
    s = _DANGLING.sub("", s).strip()
    if s.endswith(":") or s.endswith(","):
        s = s[:-1].rstrip() + "."
    if s and s[-1] not in ".!?…":
        s += "."
    return s


def local_spoken(text: str, limit: int = 380) -> str:
    """Sin modelo: el comienzo de la respuesta en frases completas + aviso si
    se dejó fuera algo (tablas, código, enlaces) o se cortó."""
    t = plain_text(text)
    dropped = needs_summary(text)
    out = ""
    truncated = False
    for p in [p.strip() for p in t.split("\n\n") if p.strip()]:
        for s in re.split(r"(?<=[.!?…:])\s+", p.replace("\n", " ")):
            s = _tidy_sentence(s)
            if len(s) < 2:
                continue
            if out and len(out) + len(s) > limit:
                truncated = True
                break
            out = (out + " " + s).strip()
        if truncated:
            break
    if not out:
        return _closing(text)
    return (out + " " + _closing(t)).strip() if (dropped or truncated) else out


_PROMPT = (
    "Eres la voz de RUFFS. Te paso la respuesta que RUFFS acaba de escribir en el chat. "
    "Escribe SOLO lo que RUFFS diría en voz alta a la persona, como en una conversación: "
    "2 a 4 frases naturales y concisas, en el MISMO idioma de la respuesta. "
    "Nada de tablas, listas, viñetas, markdown, emojis, código, símbolos ni direcciones web; "
    "redondea o resume las cifras si hay muchas. No inventes nada que no esté en la respuesta. "
    "Si la respuesta tiene detalles que no caben (tablas, código, listas largas, enlaces), "
    "termina con una frase breve diciendo que el detalle está en pantalla. "
    "Devuelve únicamente el texto a leer, sin comillas ni explicaciones."
)


def _candidates(owner):
    from src.endpoint_resolver import resolve_endpoint
    out = []
    for kind in ("utility", "default"):
        try:
            url, model, headers = resolve_endpoint(kind, owner=owner)
            if url and model:
                out.append((url, model, headers))
        except Exception:
            pass
    return out


async def spoken_version(text: str, mode: str = "smart", owner=None) -> dict:
    """{speech, summarized, source}. mode: smart | summary | full."""
    clean = strip_think(text)[:MAX_INPUT_CHARS]
    if not clean.strip():
        return {"speech": "", "summarized": False, "source": "empty"}
    if mode == "full" or (mode != "summary" and not needs_summary(clean)):
        return {"speech": plain_text(clean), "summarized": False, "source": "original"}
    cands = _candidates(owner)
    if cands:
        try:
            from src.llm_core import llm_call_async_with_fallback
            raw = await llm_call_async_with_fallback(
                cands,
                [{"role": "system", "content": _PROMPT}, {"role": "user", "content": clean}],
                temperature=0.3, max_tokens=1500, timeout=30,
            )
            speech = plain_text(strip_think(raw or "")).strip().strip('"«»').strip()
            if speech:
                return {"speech": speech, "summarized": True, "source": "llm"}
        except Exception as e:  # noqa: BLE001
            logger.info("Resumen hablado: el modelo falló (%s); se usa el resumen local", str(e)[:160])
    return {"speech": local_spoken(clean), "summarized": True, "source": "local"}
