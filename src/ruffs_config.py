"""Configuración central de RUFFS.

RUFFS es un fork de Odysseus v1.0 orientado a proveedores de API en la nube
(OpenRouter primero). Este módulo reúne los valores que RUFFS cambia respecto
a Odysseus, para que estén en un solo sitio y se puedan ajustar por variable de
entorno sin tocar el código.
"""

import os

APP_NAME = "RUFFS"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "si", "sí")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# ── Modelos locales (Cookbook, vLLM, llama.cpp, descargas de HuggingFace) ──
# RUFFS está pensado para APIs en la nube. El Cookbook sigue en el código pero
# queda desactivado: no se registran sus rutas, no aparece en la interfaz y sus
# herramientas no se ofrecen al agente (así no consumen contexto).
LOCAL_MODELS_ENABLED = _env_bool("RUFFS_ENABLE_LOCAL_MODELS", False)

LOCAL_MODEL_TOOLS = frozenset({
    "download_model", "serve_model", "list_served_models", "stop_served_model",
    "list_downloads", "cancel_download", "search_hf_models", "list_cached_models",
    "list_serve_presets", "serve_preset", "adopt_served_model",
    "list_cookbook_servers",
})

# ── Salida del modelo y continuación automática ──
# Cuando un proveedor corta la respuesta por límite de tokens
# (finish_reason="length" / stop_reason="max_tokens"), RUFFS pide al modelo
# que siga exactamente donde quedó y une las partes en una sola respuesta.
AUTO_CONTINUE_ENABLED = _env_bool("RUFFS_AUTO_CONTINUE", True)
AUTO_CONTINUE_MAX = _env_int("RUFFS_AUTO_CONTINUE_MAX", 4)

# Límite de tokens de salida cuando el llamante no fija ninguno (max_tokens=0).
# Odysseus no enviaba límite y cada proveedor aplicaba el suyo (a veces muy
# bajo). RUFFS usa el máximo que publica el modelo, acotado por este tope.
# No conviene subirlo mucho: OpenRouter reserva crédito según max_tokens y con
# modelos caros un tope alto puede dar 402 con poco saldo. Las respuestas más
# largas se completan con la continuación automática.
DEFAULT_OUTPUT_TOKENS_CAP = _env_int("RUFFS_MAX_OUTPUT_TOKENS", 32768, minimum=256)

# Caché de prompts en OpenRouter para modelos Anthropic: marca el prompt de
# sistema (estable entre rondas del agente) para que se cobre como lectura de
# caché. Solo a partir de este tamaño, por debajo no compensa.
PROMPT_CACHE_ENABLED = _env_bool("RUFFS_PROMPT_CACHE", True)
PROMPT_CACHE_MIN_CHARS = _env_int("RUFFS_PROMPT_CACHE_MIN_CHARS", 4000, minimum=0)

# ── Agente ──
# Odysseus cortaba el agente en silencio a las 20 rondas. RUFFS: 200 (tareas
# largas como importar cientos de elementos); la protección real es el tope de
# gasto de abajo y el detector de bucles del agente.
AGENT_MAX_ROUNDS = _env_int("RUFFS_AGENT_MAX_ROUNDS", 200, minimum=1)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# Tope de gasto por mensaje (USD, coste real que informa OpenRouter). Al
# alcanzarlo el agente se pausa y dice cómo seguir ("continúa"). 0 = sin tope.
AGENT_MAX_COST_USD = _env_float("RUFFS_AGENT_MAX_COST_USD", 1.0)

# Resumen de herramientas de turnos anteriores que se reinyecta al modelo para
# que, al pedirle "continúa", no tenga que volver a leer ni verificar todo.
TOOL_MEMORY_ENABLED = _env_bool("RUFFS_TOOL_MEMORY", True)
TOOL_MEMORY_OUTPUT_CHARS = _env_int("RUFFS_TOOL_MEMORY_OUTPUT_CHARS", 700, minimum=0)
TOOL_MEMORY_MAX_CHARS = _env_int("RUFFS_TOOL_MEMORY_MAX_CHARS", 6000, minimum=500)
