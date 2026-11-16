#!/bin/bash
# Ejecutable de RUFFS.app (Contents/MacOS/RUFFS).
#
# Abre la consola de RUFFS en Terminal (como la ventana de consola en
# Windows). El lanzador se copia sin atributos de cuarentena a la carpeta de
# datos para que Terminal pueda ejecutarlo, y se le indica dónde están los
# recursos de la app (que macOS puede mover al ejecutarla desde Descargas).
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
SUPPORT="$HOME/Library/Application Support/RUFFS"
mkdir -p "$SUPPORT"
printf '%s' "$RES" > "$SUPPORT/.bundle_resources"
cp -X "$RES/RUFFS.command" "$SUPPORT/RUFFS.command"
chmod +x "$SUPPORT/RUFFS.command"

# ¿Ya está en marcha (cerraste la ventana, pero sigue encendido)? Entonces solo
# vuelve a abrir su ventana, sin arrancar otro servidor.
PORT="${RUFFS_PORT:-7100}"
if /usr/bin/curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/login"; then
  APP="$SUPPORT/app"
  if [ -x "$APP/.venv/bin/python" ]; then
    (cd "$APP" && nohup .venv/bin/python ruffs_desktop.py --attach --port "$PORT" >/dev/null 2>&1 &)
  else
    /usr/bin/open "http://127.0.0.1:$PORT"
  fi
  exit 0
fi
/usr/bin/open -a Terminal "$SUPPORT/RUFFS.command"
