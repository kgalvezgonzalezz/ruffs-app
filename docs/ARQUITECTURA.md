# Arquitectura de RUFFS

Este documento explica cómo está construido RUFFS, módulo por módulo, para
que puedas entenderlo y modificarlo. RUFFS es un fork de **Odysseus**
(commit `6cf43ba` del 4 de junio de 2026, publicado bajo licencia MIT) con el
núcleo de IA reescrito para trabajar con proveedores de API, sobre todo
OpenRouter.

---

## 1. Visión general

RUFFS es una **aplicación web autoalojada**: un servidor en Python
(FastAPI + Uvicorn) que sirve una interfaz web (HTML + JavaScript sin
framework) y habla con proveedores de modelos de lenguaje por HTTP.

```
 Navegador (static/)                    Servidor (app.py)                         Fuera
 ───────────────────                    ─────────────────                         ─────
 index.html + js/*.js  ── HTTP/SSE ──►  core/middleware  (auth, CSP, límites)
                                         routes/*_routes.py  (≈45 routers)
                                              │
                                              ▼
                                         src/  (lógica)                    ──►  OpenRouter / OpenAI /
                                           llm_core.py   (proveedores)           Anthropic (HTTPS)
                                           agent_loop.py (agente)          ──►  Servidores MCP
                                           tool_*.py     (herramientas)          (procesos locales o HTTP)
                                         services/ (memoria, búsqueda,     ──►  Buscadores web, IMAP/SMTP,
                                                    research, voz…)              CalDAV, YouTube…
                                              │
                                              ▼
                                         data/  (SQLite, ChromaDB, JSON, subidas)
```

Todo corre en un solo proceso. Las tareas en segundo plano (recordatorios,
tareas programadas, sondeo de email, auditoría de habilidades, conexión de
servidores MCP) son corrutinas `asyncio` que arranca `app.py` al iniciar.

---

## 2. Mapa de carpetas

| Carpeta | Qué contiene |
|---|---|
| `app.py` | Punto de entrada. Crea la app FastAPI, registra los ~45 routers, el middleware, y las tareas de arranque/parada (`_startup_event`). |
| `core/` | Infraestructura: base de datos (`database.py`, SQLAlchemy), autenticación (`auth.py`), middleware HTTP, sesiones de chat (`session_manager.py`, `models.py`), escritura atómica de archivos. |
| `routes/` | La API HTTP, un archivo por dominio: `chat_routes`, `session_routes`, `document_routes`, `email_routes`, `calendar_routes`, `mcp_routes`, `model_routes`, `research_routes`, `task_routes`, etc. Solo validan, comprueban permisos y delegan en `src/`. |
| `src/` | La lógica de la aplicación (ver §3). |
| `services/` | Subsistemas grandes y autocontenidos: `memory/` (memoria y habilidades), `research/` (Deep Research), `search/` (buscadores web), `tts/` y `stt/` (voz), `docs/`, `youtube/`, `shell/`, `hwfit/` (Cookbook, desactivado). |
| `mcp_servers/` | Servidores MCP **integrados** que trae el proyecto: email, memoria, RAG y generación de imágenes. |
| `static/` | Interfaz web: `index.html`, `app.js` (arranque), `style.css`, y `js/` con ~67 módulos (chat, documentos, ajustes, email, calendario, editor de imágenes…). Librerías de terceros en `static/lib/`. |
| `companion/` | API para emparejar un móvil u otro cliente de la red local. |
| `integrations/` | Habilidades para usar RUFFS desde Claude Code y Codex. |
| `scripts/` | Utilidades de línea de comandos (copias de seguridad, sesiones, notas…) y `importar_mcp_odysseus.py`. |
| `tests/` | ~400 archivos de tests con pytest. |
| `data/` | **Tus datos** (no se sube a git): `app.db`, `auth.json`, `settings.json`, `chromadb/`, subidas, documentos personales. |

---

## 3. Módulos clave de `src/`

### 3.1 Conexión con los modelos — `llm_core.py`
- `_detect_provider(url)` decide el dialecto: `openrouter`, `anthropic`,
  `ollama`, `copilot`, `groq` u `openai` (compatible OpenAI por defecto).
- `stream_llm()` hace la petición en streaming y la traduce a un protocolo
  SSE interno común: `{"delta": …}` (texto), `{"delta": …, "thinking": true}`
  (razonamiento), `{"type": "tool_calls"}`, `{"type": "usage"}`,
  `{"type": "finish"}` (nuevo en RUFFS) y `[DONE]`.
- `stream_llm_with_fallback()` prueba una lista de modelos en orden si el
  principal falla **antes** de producir texto, y aplica la continuación
  automática (§5.1).
- `llm_call_async()` es la versión sin streaming, usada por Deep Research,
  email, resúmenes, títulos, etc.

### 3.2 Contexto de cada modelo — `model_context.py`, `openrouter_catalog.py`, `context_budget.py`
- `get_context_length()` averigua la ventana de contexto del modelo. Para
  OpenRouter se usa su catálogo público (`openrouter_catalog.py`).
- `context_budget.py` calcula cuántos tokens de historial caben.
- `context_compactor.py` recorta o resume el historial cuando no cabe.

### 3.3 El agente — `agent_loop.py` y compañía
- `stream_agent_loop()` es el bucle del agente: envía el historial y las
  herramientas al modelo, ejecuta las llamadas a herramientas que devuelve,
  añade los resultados y repite, hasta que el modelo responde sin pedir más
  herramientas o se alcanza el límite de rondas.
- `tool_index.py` elige qué herramientas ofrecer en cada turno con búsqueda
  semántica (RAG de herramientas): así no se envían las ~120 herramientas a la
  vez.
- `tool_schemas.py` define las herramientas (formato de función de OpenAI);
  `tool_implementations.py` y `builtin_actions.py` las implementan;
  `tool_execution.py` las despacha y **confina las rutas de archivo** (§6).
- `tool_parsing.py` extrae llamadas a herramientas escritas como bloques de
  código, para modelos sin llamadas a funciones nativas.
- `teacher_escalation.py`: si un modelo pequeño falla, uno más capaz puede
  retomar la tarea (desactivado por defecto).
- `tool_memory.py` (nuevo en RUFFS): memoria de herramientas entre turnos (§5.2).

### 3.4 Chat — `chat_handler.py`, `chat_processor.py`, `routes/chat_helpers.py`
Preparan el turno: preset (temperatura, `max_tokens`, prompt de sistema),
memoria del usuario, documentos adjuntos, búsqueda web, RAG… y deciden si el
turno va en modo **chat** (sin herramientas) o **agente**.

### 3.5 Memoria y RAG — `memory*.py`, `rag_*.py`, `embeddings.py`, `chroma_client.py`
- Memoria persistente de hechos sobre el usuario (`services/memory`), con
  búsqueda vectorial en ChromaDB y embeddings locales (FastEmbed, ONNX).
- RAG de documentos personales y subidos.
- `chroma_client.py`: en RUFFS usa ChromaDB **embebido** en
  `data/chromadb` salvo que configures un servidor aparte.

### 3.6 Otros subsistemas
| Módulo | Función |
|---|---|
| `deep_research.py`, `research_handler.py`, `services/research/` | Investigación en varios pasos (adaptado de Tongyi DeepResearch, Apache-2.0). |
| `task_scheduler.py` | Tareas programadas tipo cron que ejecuta el agente. |
| `mcp_manager.py`, `builtin_mcp.py` | Conexión a servidores MCP (§4). |
| `email_thread_parser.py`, `routes/email_*` | Bandeja IMAP/SMTP con clasificación por IA. |
| `caldav_sync.py`, `caldav_writeback.py` | Calendario con sincronización CalDAV. |
| `document_processor.py`, `upload_handler.py`, `pdf_*` | Documentos, subidas, PDF. |
| `visual_report.py` | Informe visual de Deep Research. |
| `webhook_manager.py` | Webhooks salientes. |
| `settings.py` | Ajustes en `data/settings.json` (lectura con caché de 2 s). |
| `ruffs_config.py` | Todos los valores que RUFFS cambia, ajustables por variable de entorno. |

---

## 4. Servidores MCP

Hay dos tipos:

1. **Integrados** (`src/builtin_mcp.py`): los que trae el proyecto en
   `mcp_servers/` (email, memoria, RAG, imágenes) y el navegador
   (`@playwright/mcp`, solo si ya está en la caché de npx). Arrancan solos.
2. **Del usuario**: los que añades en Ajustes → MCP. Se guardan en la tabla
   `mcp_servers` de `data/app.db` y se conectan al arrancar.

Transportes: `stdio` (un programa local), `http` (servidor remoto, nuevo en
RUFFS) y `sse` (remoto antiguo).

Cada servidor tiene un límite de tiempo de conexión (30 s; 120 s si se lanza
con npx/uvx) y se conectan en paralelo. Sus herramientas se ofrecen al agente
con el prefijo `mcp__<id>__<herramienta>` y pasan por el mismo RAG de
herramientas que las integradas.

Para traer los servidores que tenías en Odysseus:
`.venv\Scripts\python.exe scripts\importar_mcp_odysseus.py --aplicar`.

---

## 5. Lo que RUFFS cambia en el núcleo de IA

### 5.1 Respuestas cortadas → continuación automática (`src/autocontinue.py`)
**Problema en Odysseus:** cuando el modelo agotaba sus tokens de salida, el
proveedor devolvía `finish_reason: "length"`, pero `stream_llm` no lo leía y
daba la respuesta por terminada. Además no enviaba límite de salida
(`max_tokens = 0`), así que cada proveedor aplicaba el suyo.

**En RUFFS:** `stream_llm` emite el motivo de fin. Si es un corte por
longitud, `stream_with_autocontinue` vuelve a llamar al modelo con lo ya
escrito y la instrucción de seguir exactamente desde el último carácter, sin
repetir ni reverificar, y quita el texto duplicado en la unión. Hasta 4
continuaciones (`RUFFS_AUTO_CONTINUE_MAX`). No se continúa cuando el llamante
pidió un límite pequeño a propósito (títulos, etiquetas: `max_tokens < 1024`)
ni cuando el corte cae dentro de una llamada a herramienta.

### 5.2 "Al decirle que continúe lo revisa todo" → memoria de herramientas (`src/tool_memory.py`)
**Problema en Odysseus:** las acciones del agente (comandos, salidas, archivos
leídos) se guardaban en los metadatos del mensaje, pero
`_sanitize_llm_messages` descartaba los metadatos antes de enviar el
historial. En el turno siguiente el modelo solo veía lo que había *dicho*, no
lo que había *hecho*, y al pedirle que siguiera volvía a leer y comprobar todo.

**En RUFFS:** `Session.get_context_messages()` adjunta a las 4 últimas
respuestas del agente un registro compacto `[RUFFS tool log]` con cada
herramienta, su comando y un extracto de la salida (acotado). Una regla nueva
en el prompt del agente le indica que lo trate como su propia memoria y no
rehaga pasos completados.

### 5.3 El agente se detenía a mitad de tarea → rondas configurables y aviso
Odysseus cortaba el bucle a las 20 rondas **sin avisar**. RUFFS usa 60
(`RUFFS_AGENT_MAX_ROUNDS`) y, si se agotan, lo dice en el chat; al escribir
«continúa», la memoria de herramientas permite seguir sin repetir trabajo.

### 5.4 Optimización para OpenRouter
- `usage.include`: OpenRouter devuelve el **coste real** de cada respuesta;
  el agente lo acumula y lo registra (`cost_usd` en las métricas).
- `max_tokens` según la ficha del modelo (acotado a `RUFFS_MAX_OUTPUT_TOKENS`;
  ojo: OpenRouter reserva crédito según ese valor).
- Ventana de contexto real del modelo desde el catálogo.
- **Caché de prompts** para modelos `anthropic/*`: el prompt de sistema del
  agente (grande y estable) se cobra como lectura de caché en las rondas
  siguientes. DeepSeek, OpenAI y Gemini cachean automáticamente.
- Precarga del endpoint desde `OPENROUTER_API_KEY` (`src/ruffs_bootstrap.py`).

---

## 6. Seguridad

- **Autenticación** obligatoria (`AUTH_ENABLED=true`); cookie `ruffs_session`.
- **Confinamiento de rutas** de `read_file`/`write_file` (`tool_execution.py`):
  el agente solo puede tocar `data/`, las carpetas temporales y las que
  añadas en `tool_path_extra_roots`. Nunca `.ssh`, `.gnupg`, `.env`, claves
  SSH, etc. *(Tu copia de Odysseus añadía todas las unidades —C:\, E:\— de
  forma forzada, lo que anulaba esta protección.)*
- El lanzador **no** pide permisos de administrador y escucha solo en
  `127.0.0.1`. *(El de Odysseus se elevaba a administrador y escuchaba en
  toda la red.)*
- Las claves de API se guardan cifradas en `app.db` (`EncryptedText`,
  clave en `data/.app_key`).

---

## 7. Datos (`data/`)

| Archivo | Contenido |
|---|---|
| `app.db` | SQLite: sesiones, mensajes, documentos, endpoints de modelos (claves cifradas), servidores MCP, tareas, calendario… |
| `auth.json` | Usuarios y hashes de contraseña (bcrypt), 2FA. |
| `settings.json` | Ajustes de la app (modelo por defecto, búsqueda, voz…). |
| `chromadb/` | Vectores de memoria y RAG. |
| `cache/openrouter_models.json` | Catálogo de modelos de OpenRouter (se refresca cada 6 h). |
| `uploads/`, `personal_docs/` | Archivos subidos y documentos personales. |

Haz copias de esta carpeta: es lo único que no se puede regenerar.

---

## 8. Configuración

1. `.env` (ver `.env.example`): opciones de despliegue y de RUFFS.
2. **Ajustes** en la interfaz: modelos, proveedores, búsqueda, email, MCP…
   (se guardan en `data/settings.json` y `data/app.db`).
3. `src/ruffs_config.py`: valores por defecto de RUFFS, todos sobrescribibles
   por variable de entorno.

---

## 9. Estado conocido

- La batería de tests de Odysseus tiene fallos **previos** en Windows que no
  afectan al uso normal: tests de JavaScript cuyo arnés pasa a Node rutas
  `C:\` en lugar de URLs `file://`, tests del Cookbook (desactivado) y tests
  que asumen rutas de Linux (`/etc/…`) o permisos para crear enlaces
  simbólicos. Los tests propios de RUFFS están en `tests/test_ruffs_*.py`.
- El Cookbook (modelos locales) sigue en el código, desactivado
  (`RUFFS_ENABLE_LOCAL_MODELS=false`).
