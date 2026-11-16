#!/bin/bash
# RUFFS para macOS — lanzador (se ejecuta en una ventana de Terminal, que
# hace de consola de RUFFS; la interfaz se abre en su propia ventana).
#
# Primera vez: copia RUFFS a ~/Library/Application Support/RUFFS/app,
# descarga Python 3.11 (con el uv incluido; no hace falta Homebrew) y las
# dependencias. Siguientes veces arranca directamente. Al actualizar la app
# solo se reemplaza el programa: data/ y .env (tus datos) se conservan.
#
# Variables (pruebas/CI):
#   RUFFS_SUPPORT_DIR   carpeta de datos (por defecto ~/Library/Application Support/RUFFS)
#   RUFFS_BUNDLE_RES    carpeta Resources de RUFFS.app (si no, la guardada al abrir la app)
#   RUFFS_HEADLESS=1    solo servidor, sin ventana
#   RUFFS_SETUP_ONLY=1  instalar y salir
set -uo pipefail

SUPPORT="${RUFFS_SUPPORT_DIR:-$HOME/Library/Application Support/RUFFS}"
RES="${RUFFS_BUNDLE_RES:-$(cat "$SUPPORT/.bundle_resources" 2>/dev/null || true)}"
APPDIR="$SUPPORT/app"
UV="$SUPPORT/bin/uv"
mkdir -p "$SUPPORT/bin" "$SUPPORT/logs"

printf '\033]0;RUFFS\007'
step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
fail() {
  printf '\n\033[31mERROR: %s\033[0m\n' "$1"
  if [ "${RUFFS_SETUP_ONLY:-0}" = "1" ] || [ "${RUFFS_HEADLESS:-0}" = "1" ]; then exit 1; fi
  read -r -p "Pulsa Enter para cerrar…" _ ; exit 1
}
retry() { local n=1; until "$@"; do [ $n -ge 3 ] && return 1; n=$((n+1)); echo "Reintentando ($n/3)…"; sleep 3; done; }

[ -n "$RES" ] && [ -d "$RES/ruffs" ] || fail "No encuentro los archivos de RUFFS.app. Abre RUFFS desde la carpeta Aplicaciones."

# 1. Programa: copiar/actualizar desde la app (conserva datos y entorno)
BUILD_NEW="$(cat "$RES/ruffs/RUFFS_BUILD" 2>/dev/null || echo nuevo)"
BUILD_OLD="$(cat "$APPDIR/RUFFS_BUILD" 2>/dev/null || echo)"
if [ "$BUILD_NEW" != "$BUILD_OLD" ]; then
  step "Preparando RUFFS ($BUILD_NEW)"
  mkdir -p "$APPDIR"
  rsync -a --delete \
    --exclude 'data/' --exclude '.env' --exclude '.venv/' --exclude 'logs/' --exclude 'uploads/' \
    "$RES/ruffs/" "$APPDIR/" || fail "No se pudo copiar RUFFS a $APPDIR"
fi
if ! cmp -s "$RES/bin/uv" "$UV" 2>/dev/null; then
  cp -X "$RES/bin/uv" "$UV" && chmod +x "$UV" || fail "No se pudo preparar uv"
fi
if [ -f "$RES/bin/uvx" ] && ! cmp -s "$RES/bin/uvx" "$SUPPORT/bin/uvx" 2>/dev/null; then
  cp -X "$RES/bin/uvx" "$SUPPORT/bin/uvx" && chmod +x "$SUPPORT/bin/uvx"
fi

# 2. Python 3.11 + dependencias (solo si cambian)
export UV_PYTHON_INSTALL_DIR="$SUPPORT/python"
export UV_CACHE_DIR="$SUPPORT/cache/uv"
export UV_HTTP_TIMEOUT=180
cd "$APPDIR" || fail "No existe $APPDIR"
if [ ! -x .venv/bin/python ]; then
  step "Instalando Python 3.11 (solo la primera vez)"
  rm -rf .venv
  retry "$UV" venv --python 3.11 .venv || fail "No se pudo crear el entorno de Python"
fi
REQ_HASH="$(cat requirements.txt requirements-desktop.txt 2>/dev/null | shasum -a 256 | cut -d' ' -f1)"
if [ "$REQ_HASH" != "$(cat .venv/.ruffs_requirements 2>/dev/null)" ]; then
  step "Instalando dependencias (la primera vez tarda unos minutos)"
  retry "$UV" pip install --python .venv/bin/python -r requirements.txt -r requirements-desktop.txt \
    || fail "Falló la instalación de dependencias. Revisa el error de arriba."
  echo "$REQ_HASH" > .venv/.ruffs_requirements
fi

# 3. Configuración inicial (la cuenta de administrador se crea en la propia app)
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  # Escuchar en todas las interfaces (0.0.0.0) para entrar desde otros
  # dispositivos con Tailscale. La primera cuenta de administrador solo se
  # puede crear desde este Mac.
  sed -i '' 's/^APP_BIND=.*/APP_BIND=0.0.0.0/' .env
  echo "Creado .env (puedes poner ahí tu OPENROUTER_API_KEY o configurarla desde la app)."
fi
# Actualizaciones: ajustes nuevos en un .env ya existente (se conserva al
# actualizar). 1.1.3: rondas del agente de 60 a 200 y tope de gasto por mensaje.
if [ -f .env ]; then
  if grep -q '^RUFFS_AGENT_MAX_ROUNDS=60[[:space:]]*$' .env; then
    sed -i '' 's/^RUFFS_AGENT_MAX_ROUNDS=60[[:space:]]*$/RUFFS_AGENT_MAX_ROUNDS=200/' .env
  fi
  if ! grep -q '^RUFFS_AGENT_MAX_COST_USD=' .env; then
    printf '\n# Tope de gasto por mensaje en dólares (coste real de OpenRouter); 0 = sin tope.\nRUFFS_AGENT_MAX_COST_USD=1.0\n' >> .env
  fi
fi
mkdir -p data
export PYTHONUTF8=1 NUMBA_DISABLE_JIT=1

# 3b. Node.js portátil + navegador para los MCP (MCP de navegador y los del
#     catálogo que van con npx). Queda en la carpeta de datos de RUFFS, sin
#     tocar el sistema. Si falla, RUFFS funciona igual sin esos MCP.
NODE_VERSION="v26.13.1"
PLAYWRIGHT_MCP="@playwright/mcp@0.0.85"   # igual que src/builtin_mcp.py
# MCP del catálogo que van con npx (igual que src/mcp_catalog.py).
CATALOG_MCPS="rea-agents@6.9.0 agent-desktop@1.3.1 mcp-remote@0.16.0"
NODE_DIR="$SUPPORT/node"
if [ ! -x "$NODE_DIR/bin/node" ]; then
  step "Instalando Node.js $NODE_VERSION para los MCP (solo la primera vez)"
  case "$(uname -m)" in arm64) NARCH=arm64 ;; *) NARCH=x64 ;; esac
  NNAME="node-$NODE_VERSION-darwin-$NARCH"
  NTMP="$SUPPORT/.node-download"; rm -rf "$NTMP"; mkdir -p "$NTMP"
  if curl -fsSL -o "$NTMP/$NNAME.tar.gz" "https://nodejs.org/dist/$NODE_VERSION/$NNAME.tar.gz" \
     && curl -fsSL -o "$NTMP/SHASUMS256.txt" "https://nodejs.org/dist/$NODE_VERSION/SHASUMS256.txt" \
     && [ "$(grep "  $NNAME.tar.gz\$" "$NTMP/SHASUMS256.txt" | awk '{print $1}')" = "$(shasum -a 256 "$NTMP/$NNAME.tar.gz" | awk '{print $1}')" ] \
     && tar -xzf "$NTMP/$NNAME.tar.gz" -C "$NTMP"; then
    rm -rf "$NODE_DIR"; mv "$NTMP/$NNAME" "$NODE_DIR"; echo "Node.js $NODE_VERSION listo."
  else
    echo "No se pudo instalar Node.js ahora; los MCP con npx no estarán disponibles hasta el próximo arranque."
  fi
  rm -rf "$NTMP"
fi
# Node y uv/uvx de la instalación primero en el PATH (RUFFS y sus MCP).
export PATH="$NODE_DIR/bin:$SUPPORT/bin:$PATH"
export PLAYWRIGHT_BROWSERS_PATH="$SUPPORT/ms-playwright"
if [ -x "$NODE_DIR/bin/npx" ] && [ ! -f "$NODE_DIR/.ruffs-playwright-mcp" ]; then
  step "Preparando el MCP de navegador (solo la primera vez)"
  # Mismo Playwright que usa el MCP → instala su Chromium en PLAYWRIGHT_BROWSERS_PATH.
  if "$NODE_DIR/bin/npx" -y "$PLAYWRIGHT_MCP" --version \
     && "$NODE_DIR/bin/npx" -y -p "$PLAYWRIGHT_MCP" playwright install chromium; then
    echo "$PLAYWRIGHT_MCP" > "$NODE_DIR/.ruffs-playwright-mcp"
  else
    echo "No se pudo preparar ahora; se reintentará al arrancar."
  fi
fi
if [ -x "$NODE_DIR/bin/npx" ] && [ "$(cat "$NODE_DIR/.ruffs-catalog-mcp" 2>/dev/null)" != "$CATALOG_MCPS" ]; then
  step "Preparando los MCP REA, Agent Desktop y HyperFrames (solo la primera vez)"
  all_ok=1
  for pkg in $CATALOG_MCPS; do
    # Misma caché que "npx -y <paquete> ..." (la que usará RUFFS al activarlo).
    "$NODE_DIR/bin/npx" -y -p "$pkg" node -e 0 \
      || { all_ok=0; echo "  $pkg no se pudo preparar ahora; se descargará al activarlo."; }
  done
  [ "$all_ok" = 1 ] && echo "$CATALOG_MCPS" > "$NODE_DIR/.ruffs-catalog-mcp"
fi

if [ ! -d data/fastembed_cache ] || [ -z "$(ls -A data/fastembed_cache 2>/dev/null)" ]; then
  step "Descargando el modelo de memoria (una sola vez)"
  .venv/bin/python -c "import os; from fastembed import TextEmbedding; d=os.path.join('data','fastembed_cache'); os.makedirs(d, exist_ok=True); TextEmbedding(model_name='sentence-transformers/all-MiniLM-L6-v2', cache_dir=d); print('Modelo listo')" \
    || echo "No se pudo descargar ahora; se hará al arrancar."
fi

if [ "${RUFFS_SETUP_ONLY:-0}" = "1" ]; then
  step "RUFFS está instalado y listo"
  exit 0
fi

# 4. Arranque
step "Iniciando RUFFS"
echo "Esta ventana es la consola de RUFFS. Cerrar la ventana de RUFFS no lo apaga (vuelve a abrir RUFFS o pulsa Enter aquí para recuperarla); para apagarlo cierra esta ventana o pulsa Ctrl+C."
if grep -q '^APP_BIND=0.0.0.0' .env 2>/dev/null; then
  PORT="$(grep '^APP_PORT=' .env | cut -d= -f2)"; PORT="${PORT:-7100}"
  echo "RUFFS acepta conexiones desde otros dispositivos (Tailscale / tu red), con inicio de sesión."
  TS="$(command -v tailscale || true)"
  [ -z "$TS" ] && [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ] && TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
  if [ -n "$TS" ]; then
    TSIP="$("$TS" ip -4 2>/dev/null | head -1)"
    [ -n "$TSIP" ] && printf '\033[32mDesde tus otros dispositivos con Tailscale: http://%s:%s\033[0m\n' "$TSIP" "$PORT"
  fi
fi
if [ "${RUFFS_HEADLESS:-0}" = "1" ]; then
  exec .venv/bin/python ruffs_desktop.py --no-window
fi
.venv/bin/python ruffs_desktop.py
echo ""
echo "RUFFS se ha detenido. Ya puedes cerrar esta ventana."
