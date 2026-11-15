"""Precarga de OpenRouter desde el archivo .env.

RUFFS está pensado para trabajar con claves de API. Si ``.env`` define
``OPENROUTER_API_KEY`` y todavía no hay ningún endpoint de OpenRouter, se crea
uno al arrancar (la clave se guarda cifrada, igual que desde Ajustes) y, si no
hay modelo por defecto, se usa ``RUFFS_DEFAULT_MODEL``.

Nunca sobrescribe un endpoint existente ni un modelo por defecto ya elegido.
"""

from __future__ import annotations

import logging
import os
import uuid

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"


def seed_openrouter_endpoint() -> str | None:
    """Crea el endpoint de OpenRouter si hace falta. Devuelve su id o None."""
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        return None
    from core.database import ModelEndpoint, SessionLocal

    db = SessionLocal()
    try:
        existing = db.query(ModelEndpoint).filter(ModelEndpoint.base_url.like("%openrouter.ai%")).first()
        if existing:
            return existing.id
        ep_id = str(uuid.uuid4())[:8]
        db.add(ModelEndpoint(
            id=ep_id,
            name="OpenRouter",
            base_url=OPENROUTER_BASE_URL,
            api_key=key,
            is_enabled=True,
            model_type="llm",
            endpoint_kind="api",
            supports_tools=True,
            owner=None,  # compartido: visible para todos los usuarios de esta instalación
        ))
        db.commit()
        logger.info("RUFFS: endpoint de OpenRouter creado desde OPENROUTER_API_KEY")
    finally:
        db.close()

    try:
        from src.settings import load_settings, save_settings
        settings = dict(load_settings())
        if not settings.get("default_model"):
            settings["default_endpoint_id"] = ep_id
            settings["default_model"] = (os.environ.get("RUFFS_DEFAULT_MODEL") or DEFAULT_MODEL).strip()
            save_settings(settings)
            logger.info(f"RUFFS: modelo por defecto → {settings['default_model']} (OpenRouter)")
    except Exception as e:  # el endpoint ya está creado; esto es solo comodidad
        logger.warning(f"RUFFS: no se pudo fijar el modelo por defecto: {e}")
    return ep_id
