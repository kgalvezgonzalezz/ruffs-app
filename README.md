# RUFFS

```
───────────────────────────────────────────────
 ⊹ ࣪ ˖ ૮( ˶ᵔ ᵕ ᵔ˶ )っ  RUFFS vers. 1.1
───────────────────────────────────────────────
```

Espacio de trabajo de IA autoalojado —chat, agente con herramientas, Deep
Research, documentos, memoria, email, calendario, notas y tareas— optimizado
para trabajar con **claves de API** (OpenRouter primero).

RUFFS es un fork de [Odysseus](https://github.com/odysseus-dev/odysseus)
(versión del 4 de junio de 2026, licencia MIT) con el núcleo de IA
rehecho para resolver sus problemas con APIs en la nube:

| Problema en Odysseus | En RUFFS |
|---|---|
| Las respuestas se cortaban a la mitad sin avisar | Detecta el corte por límite de tokens y **continúa sola**, empalmando las partes |
| Al pedirle "continúa", volvía a revisar todo lo hecho | El modelo recibe un **registro de las herramientas** que ya ejecutó y no las repite |
| El agente se detenía en silencio a las 20 rondas | 60 rondas (configurable) y **aviso** si se agotan |
| Sin datos de coste ni de contexto real en OpenRouter | **Coste real** por respuesta, ventana y salida máxima de cada modelo, **caché de prompts** |
| Registro de MCP roto (duplicados, paquetes inexistentes, conexiones colgadas) | Límite de tiempo por servidor, conexión en paralelo, transporte **HTTP** y migración desde Odysseus |
| Memoria vectorial degradada (solo cliente ChromaDB) | ChromaDB **embebido**, sin proceso aparte |
| El agente podía leer/escribir en todo el disco; lanzador como administrador en toda la red | Rutas confinadas; arranca sin admin y solo en `127.0.0.1` |

Detalles de cada cambio y de cómo funciona todo el programa:
[docs/ARQUITECTURA.md](docs/ARQUITECTURA.md).

## Instalación sencilla (recomendada)

Descarga el instalador de tu sistema. No hace falta instalar Python, Node.js
ni nada más: el instalador lo trae o lo descarga solo.

| Sistema | Descarga | Guía paso a paso |
|---|---|---|
| **Windows 10/11** (64 bits) | [⬇ RUFFS-Setup.exe](../../releases/latest/download/RUFFS-Setup.exe) | [Guía para Windows (.docx)](docs/guias/Guia_instalacion_RUFFS_Windows.docx) |
| **macOS 12 o posterior** (Apple Silicon e Intel) | [⬇ RUFFS.dmg](../../releases/latest/download/RUFFS.dmg) | [Guía para Mac (.docx)](docs/guias/Guia_instalacion_RUFFS_Mac.docx) |

Todas las versiones: [Releases](../../releases).

**Windows**

1. Abre `RUFFS-Setup.exe`. Si Windows SmartScreen avisa, pulsa
   **Más información → Ejecutar de todas formas** (el instalador no está firmado).
2. Sigue el asistente (no pide permisos de administrador). Al terminar prepara
   Python y los componentes; tarda unos minutos la primera vez.
3. Abre **RUFFS** desde el escritorio o el menú Inicio: se abre en su propia
   ventana. Crea tu usuario administrador.

**macOS**

1. Abre `RUFFS.dmg` y arrastra **RUFFS** a **Aplicaciones**.
2. La primera vez, haz **clic derecho → Abrir** sobre RUFFS en Aplicaciones
   (la app no está firmada por Apple) y confirma **Abrir**.
3. Espera a que termine la preparación inicial (unos minutos) y crea tu
   usuario administrador.

**Después, en los dos sistemas**

- Pon tu clave de OpenRouter en **Ajustes → Servicios → API** y elige tus
  modelos (las guías lo explican con capturas).
- Para usarlo desde el móvil u otro equipo: instala Tailscale en ambos y entra
  en `http://<IP de Tailscale>:7100` (guías, pasos 10–14).
- En **Ajustes → MCP** tienes, listos para activar: navegador, Filesystem,
  Fetch, Git, **Computer Use** (RUFFS ve y maneja tu escritorio), **Ghidra**,
  **REA**, **Agent Desktop**, **HyperFrames** y más. En **Prompt → Persona**
  está **Computer Ruffs**, la persona preparada para Computer Use.

## Instalación manual desde el código

### Puesta en marcha (Windows)

1. Necesitas **Python 3.11** (o [uv](https://docs.astral.sh/uv/), que lo instala solo).
2. Copia `.env.example` como `.env` y pon tu clave en `OPENROUTER_API_KEY`
   (o añádela después en Ajustes → Modelos).
3. Haz doble clic en el acceso directo **RUFFS** del escritorio (o en
   `Iniciar_RUFFS.bat`).

La primera vez crea el entorno de Python e instala dependencias (tarda unos
minutos) y abre `http://127.0.0.1:7100`, donde creas tu usuario
administrador. Las siguientes veces arranca directamente. La ventana de
consola queda minimizada en la barra de tareas: si la cierras, RUFFS se detiene.

### Arranque manual (sin el lanzador)

```powershell
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 7100
```

Usa `0.0.0.0` solo si quieres acceder desde otros equipos de tu red, y
mantén `AUTH_ENABLED=true`.

### Traer tus servidores MCP de Odysseus

```powershell
.venv\Scripts\python.exe scripts\importar_mcp_odysseus.py            # muestra qué haría
.venv\Scripts\python.exe scripts\importar_mcp_odysseus.py --aplicar  # los registra
```

Omite los desactivados, duplicados, paquetes inexistentes o archivados y
comandos que ya no existen.

## Configuración

Las opciones de RUFFS se ajustan en `.env` (todas documentadas en
`.env.example`):

| Variable | Por defecto | Qué hace |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Crea el endpoint de OpenRouter al arrancar |
| `RUFFS_DEFAULT_MODEL` | `deepseek/deepseek-v4-flash` | Modelo por defecto si no hay otro elegido |
| `RUFFS_AUTO_CONTINUE` / `_MAX` | `true` / `4` | Continuación automática de respuestas cortadas |
| `RUFFS_MAX_OUTPUT_TOKENS` | `16384` | Tope de tokens de salida por respuesta |
| `RUFFS_AGENT_MAX_ROUNDS` | `60` | Rondas del agente antes de pausar |
| `RUFFS_TOOL_MEMORY` | `true` | Memoria de herramientas entre turnos |
| `RUFFS_PROMPT_CACHE` | `true` | Caché de prompts para `anthropic/*` en OpenRouter |
| `RUFFS_ENABLE_LOCAL_MODELS` | `false` | Reactiva el Cookbook (modelos locales) |
| `APP_BIND` / `APP_PORT` | `127.0.0.1` / `7100` | Dónde escucha el servidor |

Si quieres que el agente pueda leer o escribir archivos fuera de `data/`,
añade esas carpetas en Ajustes (`tool_path_extra_roots`). Las rutas sensibles
(`.ssh`, `.gnupg`, `.env`, claves SSH…) siguen bloqueadas siempre.

## Tests

```powershell
$env:PYTHONUTF8 = "1"
.venv\Scripts\python.exe -m pytest tests -q
```

Los tests propios de RUFFS están en `tests/test_ruffs_*.py`.

## Licencia

MIT — ver [LICENSE](LICENSE) (copyright de los autores de Odysseus) y
[ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) para el resto del código de terceros.
