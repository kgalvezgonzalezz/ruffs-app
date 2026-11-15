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

# ¿Ya está en marcha? Entonces solo trae su ventana al frente.
if /usr/bin/curl -s -o /dev/null --max-time 2 "http://127.0.0.1:${RUFFS_PORT:-7100}/login"; then
  /usr/bin/osascript -e 'tell application "System Events" to set frontmost of (first process whose name contains "Python") to true' >/dev/null 2>&1 || true
  exit 0
fi
/usr/bin/open -a Terminal "$SUPPORT/RUFFS.command"
