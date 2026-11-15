"""RUFFS: script de actualización en macOS (sin dependencias: lo importan la app
y la prueba de la CI con el Python del sistema)."""
from __future__ import annotations

from typing import Optional


def mac_install_script(dmg: str, app_path: Optional[str]) -> str:
    """Script que, con RUFFS ya cerrado, sustituye RUFFS.app por la del DMG y la abre.
    Si algo falla, abre el DMG para que el usuario arrastre la app a mano."""
    def q(s: str) -> str:
        return "'" + s.replace("'", "'\\''") + "'"
    dest = q(app_path) if app_path else "''"
    return f"""#!/bin/bash
sleep 3
DMG={q(dmg)}
DEST={dest}
if [ -z "$DEST" ]; then open "$DMG"; exit 0; fi
MNT="$(mktemp -d /tmp/ruffs-update.XXXXXX)"
if ! hdiutil attach -nobrowse -quiet -mountpoint "$MNT" "$DMG"; then open "$DMG"; exit 0; fi
SRC="$(ls -d "$MNT"/*.app 2>/dev/null | head -1)"
if [ -n "$SRC" ] && ditto "$SRC" "$DEST.new" && rm -rf "$DEST" && mv "$DEST.new" "$DEST"; then
  hdiutil detach -quiet "$MNT"
  [ -n "$RUFFS_UPDATE_NO_OPEN" ] || open "$DEST"
else
  rm -rf "$DEST.new"
  hdiutil detach -quiet "$MNT"
  open "$DMG"
fi
"""
