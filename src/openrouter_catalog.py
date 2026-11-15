"""Catálogo de modelos de OpenRouter (contexto, salida máxima y precios).

OpenRouter publica en ``GET https://openrouter.ai/api/v1/models`` (sin
autenticación) la ficha de cada modelo: ``context_length``,
``top_provider.max_completion_tokens`` y ``pricing``. Odysseus no la usaba:
para endpoints de tipo API suponía 128K de contexto y no enviaba límite de
salida, así que cada proveedor aplicaba el suyo.

Este módulo descarga el catálogo una vez, lo guarda en memoria y en disco
(``data/cache/openrouter_models.json``) y lo refresca cada pocas horas.
Si la red falla, usa la copia en disco aunque esté caducada.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import httpx

logger = logging.getLogger(__name__)

CATALOG_URL = "https://openrouter.ai/api/v1/models"
_TTL_SECONDS = 6 * 3600
_FAILURE_BACKOFF_SECONDS = 300
_CACHE_FILE = Path(os.environ.get("RUFFS_DATA_DIR", "data")) / "cache" / "openrouter_models.json"

_lock = threading.Lock()
_models: Dict[str, dict] = {}
_loaded_at = 0.0
_last_failure = 0.0


def _index(payload: dict) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for m in (payload or {}).get("data") or []:
        mid = m.get("id")
        if not mid:
            continue
        top = m.get("top_provider") or {}
        pricing = m.get("pricing") or {}
        out[mid] = {
            "id": mid,
            "name": m.get("name") or mid,
            "context_length": int(m.get("context_length") or top.get("context_length") or 0),
            "max_completion_tokens": int(top.get("max_completion_tokens") or 0),
            "prompt_price": _to_float(pricing.get("prompt")),
            "completion_price": _to_float(pricing.get("completion")),
            "cache_read_price": _to_float(pricing.get("input_cache_read")),
            "supported_parameters": list(m.get("supported_parameters") or []),
        }
    return out


def _to_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_disk() -> bool:
    global _models, _loaded_at
    try:
        raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        models = _index(raw.get("payload") or {})
        if models:
            _models = models
            _loaded_at = float(raw.get("fetched_at") or 0)
            return True
    except FileNotFoundError:
        pass
    except Exception as e:  # copia corrupta: se ignora y se vuelve a descargar
        logger.debug(f"[openrouter] caché en disco ilegible: {e}")
    return False


def _fetch() -> bool:
    global _models, _loaded_at, _last_failure
    try:
        r = httpx.get(CATALOG_URL, timeout=15.0)
        r.raise_for_status()
        payload = r.json()
        models = _index(payload)
        if not models:
            raise ValueError("catálogo vacío")
        _models, _loaded_at = models, time.time()
        try:
            _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_FILE.write_text(
                json.dumps({"fetched_at": _loaded_at, "payload": payload}),
                encoding="utf-8",
            )
        except OSError as e:
            logger.debug(f"[openrouter] no se pudo guardar la caché: {e}")
        logger.info(f"[openrouter] catálogo cargado: {len(models)} modelos")
        return True
    except Exception as e:
        _last_failure = time.time()
        logger.warning(f"[openrouter] no se pudo descargar el catálogo: {e}")
        return False


def _ensure_loaded() -> None:
    now = time.time()
    if _models and now - _loaded_at < _TTL_SECONDS:
        return
    with _lock:
        if _models and time.time() - _loaded_at < _TTL_SECONDS:
            return
        if not _models:
            _load_disk()
            if _models and time.time() - _loaded_at < _TTL_SECONDS:
                return
        if time.time() - _last_failure < _FAILURE_BACKOFF_SECONDS:
            return  # evita martillear la red si está caída; se usa lo que haya
        _fetch()


def get_model_info(model: str) -> Optional[dict]:
    """Ficha del modelo (sin el sufijo ``:free``/``:nitro``…) o ``None``."""
    if not model:
        return None
    _ensure_loaded()
    if model in _models:
        return _models[model]
    base = model.split(":")[0]
    return _models.get(base)


async def get_model_info_async(model: str) -> Optional[dict]:
    """Versión que no bloquea el bucle de eventos si hay que descargar."""
    if _models and time.time() - _loaded_at < _TTL_SECONDS:
        return get_model_info(model)
    import asyncio
    return await asyncio.to_thread(get_model_info, model)


def context_length(model: str) -> int:
    info = get_model_info(model)
    return int(info["context_length"]) if info and info.get("context_length") else 0


def max_output_tokens(model: str) -> int:
    info = get_model_info(model)
    return int(info["max_completion_tokens"]) if info and info.get("max_completion_tokens") else 0


def is_openrouter_url(url: str) -> bool:
    return "openrouter.ai" in (url or "").lower()
