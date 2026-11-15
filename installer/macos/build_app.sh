#!/bin/bash
# Construye RUFFS para macOS: dist/RUFFS.app y dist/RUFFS.dmg
#
# Se ejecuta en un Mac (o en GitHub Actions, macos-latest). La app:
#   * lleva una copia limpia de RUFFS (scripts/exportar_limpio.py: sin datos
#     de usuario, sin .env, sin historial de git);
#   * lleva uv (universal: Apple Silicon + Intel) para instalar Python 3.11 y
#     las dependencias en el primer arranque, sin Homebrew;
#   * guarda los datos del usuario en ~/Library/Application Support/RUFFS.
#
# Uso:  bash installer/macos/build_app.sh [ref]   (ref por defecto: HEAD)
set -euo pipefail

REF="${1:-HEAD}"
UV_VERSION="${UV_VERSION:-0.13.4}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT/installer/macos"
BUILD="$ROOT/build/macos"
DIST="$ROOT/dist"
APP="$DIST/RUFFS.app"
RES="$APP/Contents/Resources"

[ "$(uname)" = "Darwin" ] || { echo "Este script se ejecuta en macOS."; exit 1; }
rm -rf "$APP" "$BUILD"
mkdir -p "$BUILD" "$DIST" "$APP/Contents/MacOS" "$RES/bin"

echo "==> Copia limpia de RUFFS ($REF)"
python3 "$ROOT/scripts/exportar_limpio.py" --out "$RES/ruffs" --ref "$REF"
BUILD_ID="$(cut -d' ' -f1 "$RES/ruffs/RUFFS_BUILD")"
VERSION="$(tr -d '[:space:]' < "$RES/ruffs/VERSION" 2>/dev/null || true)"
VERSION="${VERSION:-1.0}"

echo "==> uv $UV_VERSION (universal)"
for arch in aarch64 x86_64; do
  asset="uv-$arch-apple-darwin.tar.gz"
  url="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$asset"
  curl -fsSL -o "$BUILD/$asset" "$url"
  curl -fsSL -o "$BUILD/$asset.sha256" "$url.sha256"
  expected="$(awk '{print $1}' "$BUILD/$asset.sha256")"
  actual="$(shasum -a 256 "$BUILD/$asset" | awk '{print $1}')"
  [ "$expected" = "$actual" ] || { echo "SHA-256 de $asset no coincide"; exit 1; }
  mkdir -p "$BUILD/uv-$arch"
  tar -xzf "$BUILD/$asset" -C "$BUILD/uv-$arch"
done
for tool in uv uvx; do   # uvx: MCP del catálogo (fetch, time, git)
  lipo -create -output "$RES/bin/$tool" \
    "$(find "$BUILD/uv-aarch64" -name "$tool" -type f | head -1)" \
    "$(find "$BUILD/uv-x86_64" -name "$tool" -type f | head -1)"
  chmod +x "$RES/bin/$tool"
done

echo "==> Icono"
ICONSET="$BUILD/RUFFS.iconset"
mkdir -p "$ICONSET"
SRC_PNG="$RES/ruffs/static/icons/ruffs-logo.png"
# Lienzo cuadrado con margen (el logo no es cuadrado) y todos los tamaños.
sips --padToHeightWidth 600 600 --padColor FFFFFF "$SRC_PNG" --out "$BUILD/square.png" >/dev/null 2>&1 \
  || cp "$SRC_PNG" "$BUILD/square.png"
python3 - "$SRC_PNG" "$BUILD/square.png" <<'PY' || true
# Pad onto a transparent square canvas (sips pads with an opaque colour).
import sys
try:
    from PIL import Image
except ImportError:
    sys.exit(0)
im = Image.open(sys.argv[1]).convert("RGBA")
side = int(max(im.size) * 1.12)
canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2), im)
canvas.save(sys.argv[2])
PY
for s in 16 32 128 256 512; do
  sips -z $s $s "$BUILD/square.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$BUILD/square.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$RES/RUFFS.icns"

echo "==> Bundle"
cp "$HERE/RUFFS.command" "$RES/RUFFS.command"
cp "$HERE/RUFFS-main.sh" "$APP/Contents/MacOS/RUFFS"
chmod +x "$RES/RUFFS.command" "$APP/Contents/MacOS/RUFFS"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>               <string>RUFFS</string>
  <key>CFBundleDisplayName</key>        <string>RUFFS</string>
  <key>CFBundleIdentifier</key>         <string>app.ruffs.desktop</string>
  <key>CFBundleVersion</key>            <string>$VERSION</string>
  <key>CFBundleShortVersionString</key> <string>$VERSION</string>
  <key>CFBundlePackageType</key>        <string>APPL</string>
  <key>CFBundleExecutable</key>         <string>RUFFS</string>
  <key>CFBundleIconFile</key>           <string>RUFFS</string>
  <key>LSMinimumSystemVersion</key>     <string>12.0</string>
  <key>NSHighResolutionCapable</key>    <true/>
  <key>NSAppleEventsUsageDescription</key><string>RUFFS abre su consola en Terminal.</string>
</dict>
</plist>
PLIST
# Firma ad-hoc (sin cuenta de desarrollador): necesaria en Apple Silicon.
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || echo "  (firma ad-hoc omitida)"

echo "==> RUFFS.dmg"
rm -f "$DIST/RUFFS.dmg"
if command -v create-dmg >/dev/null 2>&1; then
  create-dmg --volname "RUFFS" --volicon "$RES/RUFFS.icns" \
    --window-size 540 360 --icon-size 110 \
    --icon "RUFFS.app" 140 170 --app-drop-link 400 170 \
    --no-internet-enable "$DIST/RUFFS.dmg" "$APP" >/dev/null
else
  STAGE="$BUILD/dmg"; mkdir -p "$STAGE"
  cp -R "$APP" "$STAGE/"; ln -s /Applications "$STAGE/Applications"
  hdiutil create -volname "RUFFS" -srcfolder "$STAGE" -ov -format UDZO "$DIST/RUFFS.dmg" >/dev/null
fi

echo ""
echo "Listo:"
echo "  $APP"
echo "  $DIST/RUFFS.dmg  ($(du -h "$DIST/RUFFS.dmg" | cut -f1), SHA-256 $(shasum -a 256 "$DIST/RUFFS.dmg" | cut -c1-16)…)"
