# Handoff para el agente de RUFFS: cambiar el micrófono con el que escucha

**Para quién:** el agente de IA de RUFFS (modo Agente).
**Qué aprende:** cuando el usuario pide (escribiendo o hablando) que RUFFS lo escuche por otro
micrófono, cambiarlo él mismo con una sola llamada a `ui_control`.

---

## 1. Cómo funciona (lo que el agente debe saber)

- La **lista de micrófonos vive en la ventana del usuario** (el navegador / la ventana de RUFFS),
  no en el servidor. El agente **no puede verla** ni conoce los identificadores de los dispositivos.
- Por eso el agente **no elige un ID**: manda el **nombre tal como lo dice el usuario** y la ventana
  busca el micrófono que coincide entre los que detecta el sistema.
- La elección se guarda **en ese dispositivo** (PC o móvil) y la usan el dictado (botón del
  micrófono) y la palabra de activación («RUFFS, …»). La palabra de activación se reinicia sola con
  el micrófono nuevo.
- Si el nombre no coincide con ningún micrófono (o coincide con varios), la ventana **no cambia nada**
  y abre el **menú de micrófonos** para que el usuario elija.
- Si el micrófono elegido se desconecta más tarde, RUFFS vuelve al predeterminado y avisa.

## 2. La herramienta

`ui_control` con la acción **`set_mic`**:

| Lo que manda el agente | Qué hace la ventana |
|---|---|
| `set_mic <parte del nombre>` | Usa el micrófono cuyo nombre contiene **todas** esas palabras (ej. `set_mic fifine`, `set_mic aula`, `set_mic cámara`) |
| `set_mic predeterminado` | Vuelve al micrófono predeterminado del sistema |
| `set_mic menu` | Abre la lista de micrófonos para que el usuario elija |
| `set_mic 2` | El 2.º micrófono de la lista (solo si el usuario lo dice así) |

En llamada nativa: `{"action": "set_mic", "name": "fifine"}`.

La ventana entiende equivalencias español/inglés: «cámara» encuentra «USB **camera** audio»,
«cascos/auriculares» encuentra «headset», «integrado/portátil» encuentra «internal/array», etc.

## 3. Cuándo usarla

| El usuario dice | El agente llama |
|---|---|
| «usa el micrófono Fifine para escucharme» | `set_mic fifine` |
| «cambia al micrófono de la cámara» | `set_mic cámara` |
| «escúchame por los cascos AULA» | `set_mic aula` |
| «vuelve al micrófono normal / predeterminado» | `set_mic predeterminado` |
| «¿qué micrófonos tengo?» / «cambia de micrófono» (sin decir cuál) | `set_mic menu` |

## 4. Reglas

1. **Una sola llamada** a `ui_control` con `set_mic`. No hace falta nada más.
2. **Nunca inventes** nombres ni identificadores de dispositivos, y no intentes listar micrófonos con
   `bash`, `python`, Computer Use ni `manage_settings`: el servidor no los ve.
3. Usa **la palabra que dijo el usuario** (la marca o lo distintivo: «fifine», «aula», «cámara»), no
   la frase entera.
4. Después, di en una frase **qué micrófono pediste** y que también puede elegirlo con la flechita
   **▾** junto al micrófono (o clic derecho sobre él; en el móvil, mantenerlo pulsado).
5. Si el usuario no nombra ninguno, abre el menú (`set_mic menu`) en vez de adivinar.

## 5. Ejemplo completo

> **Usuario (hablando):** «RUFFS, escúchame por el micrófono Fifine.»
>
> **Agente →** `ui_control` `{"action": "set_mic", "name": "fifine"}`
>
> **Ventana:** cambia a «FIFINE (Fifine Microphone)» y muestra «Micrófono: FIFINE (Fifine Microphone)».
>
> **Agente:** «Listo, ahora te escucho por el micrófono Fifine. Si quieres otro, también puedes
> elegirlo con la flechita ▾ junto al micrófono.»

## 6. Dónde está en el código (para mantenimiento)

- Acción del servidor: `src/ai_interaction.py` → `do_ui_control` (`set_mic`).
- Esquema de la herramienta y conversión de la llamada nativa: `src/tool_schemas.py`.
- Regla que aprende el agente: `src/agent_loop.py` (reglas «RUFFS — microphone» y descripción de `ui_control`).
- Selección de herramienta al hablar de micrófonos: `src/tool_index.py`.
- Ventana: `static/js/chatStream.js` (evento `set_mic`) y `static/app.js` (`window.ruffsSetMicByName`,
  menú ▾); micrófono elegido y lista: `static/js/voiceLocal.js`.
