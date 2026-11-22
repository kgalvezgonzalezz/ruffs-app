"""RUFFS: generar y editar imágenes con la API key que ya tiene la persona.

Proveedores:
  * OpenRouter (la misma key del chat): los modelos de imagen van por
    /chat/completions con ``modalities: ["image", "text"]`` (Gemini «Nano Banana»,
    GPT-5 Image…). Para editar se manda la imagen en el mensaje. OpenRouter
    devuelve el costo real de cada llamada.
  * OpenAI y servicios compatibles (Together, xAI, servidores locales…):
    /images/generations para crear y /images/edits para editar.

Lo usan: la herramienta del agente (generate_image / edit_image), el modo
«chat de imágenes» (elegir un modelo de imagen como modelo del chat) y el MCP
integrado image_gen.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

IMG_DIR = Path("data/generated_images")

# Modelos que se prueban, en orden, si no hay uno elegido en Ajustes.
OPENAI_MODELS = ("gpt-image-1.5", "gpt-image-1", "gpt-image-1-mini")
OPENROUTER_MODELS = ("google/gemini-2.5-flash-image", "google/gemini-3.1-flash-image", "openai/gpt-5-image-mini")
EDIT_INCAPABLE = ("dall-e-2", "dall-e-3")

_IMAGE_MODEL_RE = re.compile(r"(gpt-image|dall-e|chatgpt-image|imagen|flux|seedream|-image\b|-image-|nano-banana)", re.I)


def is_image_model(model: str) -> bool:
    """¿El nombre es de un modelo que genera imágenes? (no de uno que solo las ve)."""
    m = (model or "").lower()
    if "vision" in m or m.endswith("-vl") or "image-preview-vl" in m:
        return False
    return bool(_IMAGE_MODEL_RE.search(m))


def is_openrouter(url: str) -> bool:
    return "openrouter.ai" in (url or "")


# ─── Elegir modelo ───────────────────────────────────────────────────────────

def pick_model(model_spec: str = "", owner: Optional[str] = None, editing: bool = False):
    """→ (url, model_id, headers). Respeta el modelo pedido o el de Ajustes; si
    no hay, prueba los conocidos de OpenAI y de OpenRouter con las keys
    configuradas."""
    from src.ai_interaction import _resolve_model
    try:
        from src.settings import load_settings
        settings = load_settings() or {}
    except Exception:
        settings = {}
    asked = (model_spec or "").strip()
    wanted = asked or (settings.get("image_model") or "").strip()
    if wanted:
        try:
            url, mid, headers = _resolve_model(wanted, owner=owner)
        except ValueError:
            if asked:   # lo pidió la persona: decirle que no existe
                raise
            # El de Ajustes ya no existe (endpoint borrado, modelo retirado): automático.
            logger.warning("Modelo de imagen de Ajustes no encontrado (%s); uso el automático", wanted)
        else:
            if editing and any(x in mid.lower() for x in EDIT_INCAPABLE):
                raise ValueError(f"{mid} no puede editar imágenes; usa gpt-image-1 o google/gemini-2.5-flash-image.")
            return url, mid, headers
    for cand in OPENAI_MODELS + OPENROUTER_MODELS + (() if editing else ("dall-e-3",)):
        try:
            url, mid, headers = _resolve_model(cand, owner=owner)
        except ValueError:
            continue
        if is_image_model(mid):
            return url, mid, headers
    # Un endpoint marcado como «imagen» (p. ej. un servidor local de difusión).
    try:
        import httpx
        from core.database import SessionLocal, ModelEndpoint
        from src.auth_helpers import owner_filter
        from src.ai_interaction import build_headers
        db = SessionLocal()
        try:
            q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True, ModelEndpoint.model_type == "image")  # noqa: E712
            if owner:
                q = owner_filter(q, ModelEndpoint, owner)
            eps = q.all()
        finally:
            db.close()
        for ep in eps:
            base = ep.base_url.rstrip("/")
            base = base if base.endswith("/v1") else base + "/v1"
            r = httpx.get(base + "/models", headers=build_headers(ep.api_key, base), timeout=3)
            mids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")] if r.status_code == 200 else []
            if mids:
                return base + "/chat/completions", mids[0], build_headers(ep.api_key, base)
    except Exception:
        pass
    raise ValueError("No encontré un modelo de imágenes en tus endpoints. Con una key de OpenRouter funciona "
                     "google/gemini-2.5-flash-image; con una de OpenAI, gpt-image-1. Elígelo en "
                     "Ajustes → AI Defaults → Image Generation.")


_MODELS_CACHE: dict = {}


def available_models(owner: Optional[str] = None) -> list[dict]:
    """Modelos que GENERAN imágenes en los endpoints de la persona (para el
    selector de Ajustes). OpenRouter marca la salida de cada modelo
    (architecture.output_modalities); en los demás se reconoce por el nombre.
    Se guarda 10 min para no consultar a cada rato."""
    import time
    import httpx
    from core.database import SessionLocal, ModelEndpoint
    from src.auth_helpers import owner_filter
    from src.endpoint_resolver import normalize_base, build_headers, build_models_url
    key = owner or ""
    hit = _MODELS_CACHE.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        if owner:
            q = owner_filter(q, ModelEndpoint, owner)
        eps = [(e.name, e.base_url, e.api_key) for e in q.all()]
    finally:
        db.close()
    out, seen = [], set()
    for name, base_url, api_key in eps:
        base = normalize_base(base_url)
        try:
            r = httpx.get(build_models_url(base), headers=build_headers(api_key, base), timeout=8)
            data = (r.json().get("data") or []) if r.status_code == 200 else []
        except Exception:
            continue
        for m in data:
            mid = (m or {}).get("id")
            if not mid or mid in seen or mid.startswith("openrouter/"):
                continue
            outs = ((m.get("architecture") or {}).get("output_modalities"))
            if (("image" in outs) if isinstance(outs, list) else is_image_model(mid)):
                seen.add(mid)
                out.append({"id": mid, "endpoint": name})
    _MODELS_CACHE[key] = (time.time(), out)
    return out


# ─── Tamaños ─────────────────────────────────────────────────────────────────

_RATIOS = {"1024x1024": "1:1", "1024x1536": "2:3", "1536x1024": "3:2", "1024x1792": "9:16", "1792x1024": "16:9",
           "square": "1:1", "cuadrada": "1:1", "vertical": "9:16", "horizontal": "16:9"}
_GPT_SIZES = {"1:1": "1024x1024", "2:3": "1024x1536", "3:4": "1024x1536", "9:16": "1024x1536",
              "3:2": "1536x1024", "4:3": "1536x1024", "16:9": "1536x1024"}


def aspect_ratio(size: str) -> str:
    s = (size or "").strip().lower()
    if re.fullmatch(r"\d+:\d+", s):
        return s
    if s in _RATIOS:
        return _RATIOS[s]
    m = re.fullmatch(r"(\d+)x(\d+)", s)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        best = min(_GPT_SIZES, key=lambda r: abs(int(r.split(":")[0]) / int(r.split(":")[1]) - w / h))
        return best
    return "1:1"


def openai_size(size: str, model_id: str) -> str:
    s = (size or "").strip().lower()
    if "dall-e-3" in model_id.lower():
        return s if s in ("1024x1024", "1024x1792", "1792x1024") else "1024x1024"
    if s in ("1024x1024", "1024x1536", "1536x1024", "auto"):
        return s
    return _GPT_SIZES.get(aspect_ratio(s), "1024x1024") if s else "1024x1024"


# ─── Llamadas ────────────────────────────────────────────────────────────────

def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _err(resp) -> str:
    text = resp.text[:500]
    try:
        j = resp.json()
        e = j.get("error")
        text = (e.get("message") if isinstance(e, dict) else e) or text
    except Exception:
        pass
    return f"El proveedor respondió {resp.status_code}: {text}"


async def _openrouter(client, url, model_id, headers, prompt, images, size):
    content = [{"type": "text", "text": prompt}]
    for data, mime in images:
        content.append({"type": "image_url", "image_url": {"url": _data_url(data, mime)}})
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": content}],
        "modalities": ["image", "text"],
        "usage": {"include": True},
    }
    if size and "gemini" in model_id.lower():
        payload["image_config"] = {"aspect_ratio": aspect_ratio(size)}
    resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(_err(resp))
    data = resp.json()
    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    out = []
    for im in msg.get("images") or []:
        u = (im.get("image_url") or {}).get("url") or im.get("url")
        if u:
            out.append(u)
    note = msg.get("content") if isinstance(msg.get("content"), str) else ""
    cost = (data.get("usage") or {}).get("cost")
    if not out:
        raise RuntimeError("El modelo no devolvió ninguna imagen" + (f": {note[:300]}" if note else
                           ". ¿Es un modelo que genera imágenes?"))
    return out, cost, note


async def _openai(client, url, model_id, headers, prompt, images, size, quality):
    base = url.replace("/chat/completions", "").replace("/v1/messages", "").rstrip("/")
    sz = openai_size(size, model_id)
    q = quality if quality in ("low", "medium", "high", "auto") else "medium"
    is_gpt = "gpt-image" in model_id.lower()
    if images:
        files = [("image[]", (f"imagen_{i}{mimetypes.guess_extension(m) or '.png'}", d, m)) for i, (d, m) in enumerate(images)]
        form = {"model": model_id, "prompt": prompt, "n": "1", "size": sz}
        if is_gpt:
            form["quality"] = q
        h = {k: v for k, v in headers.items() if k.lower() != "content-type"}
        resp = await client.post(base + "/images/edits", data=form, files=files, headers=h)
    else:
        payload = {"model": model_id, "prompt": prompt, "n": 1, "size": sz}
        if is_gpt or "dall-e" not in model_id.lower():
            payload["quality"] = q
        resp = await client.post(base + "/images/generations", json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(_err(resp))
    out = []
    for im in resp.json().get("data") or []:
        if im.get("b64_json"):
            out.append(_data_url(base64.b64decode(im["b64_json"]), "image/png"))
        elif im.get("url"):
            out.append(im["url"])
    if not out:
        raise RuntimeError("El proveedor no devolvió ninguna imagen")
    return out, None, sz


# ─── Guardar ─────────────────────────────────────────────────────────────────

async def _save(client, ref: str, *, prompt, model_id, size, quality, session_id, owner) -> tuple[str, str]:
    """Imagen (data: URL o http) → data/generated_images + galería. → (url, id)."""
    if ref.startswith("data:"):
        head, b64 = ref.split(",", 1)
        mime = head[5:].split(";")[0] or "image/png"
        data = base64.b64decode(b64)
    else:
        r = await client.get(ref)
        r.raise_for_status()
        data, mime = r.content, r.headers.get("content-type", "image/png").split(";")[0]
    ext = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, ".png")
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex[:12]}{ext}"
    (IMG_DIR / filename).write_bytes(data)
    image_id = ""
    try:
        from core.database import SessionLocal, GalleryImage
        image_id = str(uuid.uuid4())
        db = SessionLocal()
        try:
            db.add(GalleryImage(id=image_id, filename=filename, prompt=prompt[:2000], model=model_id,
                                size=size, quality=quality, session_id=session_id, owner=owner))
            db.commit()
        finally:
            db.close()
    except Exception as e:   # la imagen ya está guardada; la galería es un extra
        logger.warning("No se pudo registrar en la galería: %s", e)
        image_id = ""
    return f"/api/generated-image/{filename}", image_id


# ─── Entrada pública ─────────────────────────────────────────────────────────

async def run(prompt: str, *, images: Iterable[tuple[bytes, str]] = (), model_spec: str = "", size: str = "",
              quality: str = "", owner: Optional[str] = None, session_id: Optional[str] = None) -> dict:
    """Genera (sin `images`) o edita (con `images`) y devuelve el dict que
    espera el chat (image_url, image_id, image_prompt…) o {"error": …}."""
    import httpx
    images = list(images)
    prompt = (prompt or "").strip()
    if not prompt:
        return {"error": "Falta la descripción de la imagen."}
    try:
        from src.settings import load_settings
        settings = load_settings() or {}
    except Exception:
        settings = {}
    quality = quality or settings.get("image_quality") or "medium"
    try:
        url, model_id, headers = pick_model(model_spec, owner=owner, editing=bool(images))
    except ValueError as e:
        return {"error": str(e)}
    logger.info("Imagen: %s model=%s size=%s imgs=%d prompt=%s", "editar" if images else "generar",
                model_id, size, len(images), prompt[:80])
    timeout = httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=30.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if is_openrouter(url):
                refs, cost, note = await _openrouter(client, url, model_id, headers, prompt, images, size)
                shown_size = aspect_ratio(size) if size else "auto"
            else:
                refs, cost, shown_size = await _openai(client, url, model_id, headers, prompt, images, size, quality)
                note = ""
            image_url, image_id = await _save(client, refs[0], prompt=prompt, model_id=model_id, size=shown_size,
                                              quality=quality, session_id=session_id, owner=owner)
    except httpx.TimeoutException:
        return {"error": "El proveedor tardó demasiado (5 min). Prueba de nuevo o con calidad «low»."}
    except Exception as e:
        return {"error": f"No se pudo {'editar' if images else 'generar'} la imagen: {e}"}
    verb = "Edité la imagen" if images else "Generé la imagen"
    text = f"{verb} con {model_id}.\nimage_url: {image_url}"
    if cost:
        text += f"\nCosto: US$ {float(cost):.4f}"
    if note and note.strip():
        text += f"\nNota del modelo: {note.strip()[:300]}"
    out = {"results": text, "image_url": image_url, "image_id": image_id, "image_prompt": prompt,
           "image_model": model_id, "image_size": shown_size, "image_quality": quality}
    if cost:
        out["image_cost_usd"] = round(float(cost), 6)
    return out


# ─── Imágenes de entrada (para editar) ───────────────────────────────────────

def _read_generated(name: str, owner: Optional[str]) -> Optional[tuple[bytes, str]]:
    fname = Path(name.split("?")[0]).name
    p = IMG_DIR / fname
    if not re.fullmatch(r"[\w.-]+", fname) or not p.is_file():
        return None
    try:   # solo las propias (o las antiguas sin dueño)
        from core.database import SessionLocal, GalleryImage
        db = SessionLocal()
        try:
            row = db.query(GalleryImage).filter(GalleryImage.filename == fname).first()
        finally:
            db.close()
        if row is not None and owner and getattr(row, "owner", None) not in (None, owner):
            return None
    except Exception:
        if owner:   # sin poder comprobar de quién es, no se usa
            return None
    return p.read_bytes(), mimetypes.guess_type(fname)[0] or "image/png"


def _read_upload(upload_id: str, owner: Optional[str]) -> Optional[tuple[bytes, str]]:
    from src.constants import UPLOAD_DIR
    from src.upload_handler import UploadHandler
    import os
    h = UploadHandler(os.path.dirname(os.path.abspath(UPLOAD_DIR)), UPLOAD_DIR)
    info = h.resolve_upload(upload_id, owner=owner)
    if not info or not info.get("path"):
        return None
    mime = info.get("mime") or mimetypes.guess_type(info["path"])[0] or ""
    if not mime.startswith("image/"):
        return None
    return Path(info["path"]).read_bytes(), mime


def last_images(session_id: Optional[str], owner: Optional[str]) -> list[str]:
    """Referencias de la imagen más reciente del chat: la última que la persona
    adjuntó o la última generada, lo que sea más nuevo."""
    if not session_id:
        return []
    try:
        from src.ai_interaction import get_session_manager
        sess = get_session_manager().get_session(session_id)
    except Exception:
        return []
    for msg in reversed(getattr(sess, "history", None) or getattr(sess, "messages", None) or []):
        meta = getattr(msg, "metadata", None) or {}
        for ev in reversed(meta.get("tool_events") or []):
            if ev.get("image_url"):
                return [ev["image_url"]]
        atts = [a for a in (meta.get("attachments") or []) if str(a.get("mime", "")).startswith("image/")]
        if atts:
            return [a["id"] for a in atts]
    return []


def load_images(refs: Iterable[str], owner: Optional[str], session_id: Optional[str] = None) -> tuple[list, list]:
    """«last» / id de adjunto / /api/generated-image/… → [(bytes, mime)], [no encontradas]."""
    out, missing = [], []
    refs = [r.strip() for r in refs if r and r.strip()]
    if not refs or any(r.lower() in ("last", "ultima", "última", "latest") for r in refs):
        refs = [r for r in refs if r.lower() not in ("last", "ultima", "última", "latest")] + last_images(session_id, owner)
    for r in refs[:4]:
        got = None
        if "generated-image/" in r or (IMG_DIR / Path(r).name).is_file():
            got = _read_generated(r.split("generated-image/")[-1], owner)
        else:
            try:
                got = _read_upload(r, owner)
            except Exception:
                got = None
        (out.append(got) if got else missing.append(r))
    return out, missing


def parse_refs(text: str) -> list[str]:
    return [x for x in re.split(r"[\s,;]+", text or "") if x]


def mcp_call_to_native(tool: str, content: str) -> tuple[str, str]:
    """Llamada al MCP integrado (mcp__image_gen__…) → herramienta nativa, que
    corre dentro de RUFFS sabiendo quién la pide y en qué chat."""
    try:
        a = json.loads(content) if content.strip().startswith("{") else {}
    except ValueError:
        a = {}
    if tool.endswith("edit_image"):
        imgs = a.get("images") or a.get("image") or "last"
        if isinstance(imgs, list):
            imgs = " ".join(map(str, imgs))
        return "edit_image", "\n".join([a.get("prompt", ""), str(imgs), a.get("model", ""), a.get("size", "")])
    return "generate_image", "\n".join([a.get("prompt", ""), a.get("model", ""), a.get("size", ""), a.get("quality", "")])
