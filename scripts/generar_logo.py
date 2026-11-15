"""Genera los iconos y el logo de RUFFS a partir de una imagen cuadrada.

Uso (desde C:\\RUFFS):
    .venv\\Scripts\\python.exe scripts\\generar_logo.py ruta\\al\\logo.png

Produce:
    static/icons/ruffs-logo.png      logo con fondo transparente (bienvenida, login)
    static/icons/favicon-32.png      icono de pestaña
    static/icons/favicon.ico         icono de pestaña (16/32/48/64)
    static/icons/apple-touch-icon.png  180x180
    static/icon-192.png, icon-512.png  iconos de la app instalable (manifest.json)
    ruffs.ico                         icono para accesos directos de Windows (hasta 256)
    static/icons/ruffs-logo-source.png copia de la imagen original
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "static" / "icons"


def _bg_color(im: Image.Image) -> tuple[int, int, int]:
    w, h = im.size
    pts = [(4, 4), (w - 5, 4), (4, h - 5), (w - 5, h - 5), (w // 2, 4), (4, h // 2)]
    cs = [im.getpixel(p) for p in pts]
    return tuple(sorted(c[i] for c in cs)[len(cs) // 2] for i in range(3))


def _content_box(im: Image.Image, bg, thr: int = 40) -> tuple[int, int, int, int]:
    px = im.load()
    w, h = im.size
    xs, ys = [], []
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            r, g, b = px[x, y]
            if max(abs(r - bg[0]), abs(g - bg[1]), abs(b - bg[2])) > thr:
                xs.append(x)
                ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def _square_crop(im: Image.Image, box, pad_ratio: float) -> Image.Image:
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    side = max(x1 - x0, y1 - y0) * (1 + pad_ratio)
    half = side / 2
    w, h = im.size
    l, t = max(0, int(cx - half)), max(0, int(cy - half))
    r, b = min(w, int(cx + half)), min(h, int(cy + half))
    return im.crop((l, t, r, b))


def _transparent(im: Image.Image, bg, lo: int = 14, hi: int = 70) -> Image.Image:
    """Fondo → transparente, con borde suave y sin halo oscuro."""
    rgba = im.convert("RGBA")
    px = rgba.load()
    w, h = rgba.size
    for y in range(h):
        for x in range(w):
            r, g, b, _ = px[x, y]
            d = max(abs(r - bg[0]), abs(g - bg[1]), abs(b - bg[2]))
            a = 0.0 if d <= lo else 1.0 if d >= hi else (d - lo) / (hi - lo)
            if a <= 0:
                px[x, y] = (0, 0, 0, 0)
                continue
            # Quitar la mezcla con el fondo oscuro (des-premultiplicar).
            nr = min(255, max(0, round((r - bg[0] * (1 - a)) / a)))
            ng = min(255, max(0, round((g - bg[1] * (1 - a)) / a)))
            nb = min(255, max(0, round((b - bg[2] * (1 - a)) / a)))
            px[x, y] = (nr, ng, nb, round(a * 255))
    return rgba


def _with_outline(logo: Image.Image, color, px: int = 3) -> Image.Image:
    """Añade un contorno del color del fondo original alrededor de la silueta.

    Sobre temas oscuros casi no se ve; sobre temas claros hace que el perro
    (color crema) no se pierda contra el fondo.
    """
    from PIL import ImageFilter
    alpha = logo.getchannel("A")
    grown = alpha.filter(ImageFilter.MaxFilter(px * 2 + 1)).filter(ImageFilter.GaussianBlur(0.8))
    outline = Image.new("RGBA", logo.size, tuple(color) + (0,))
    outline.putalpha(grown)
    pad = px + 2
    canvas = Image.new("RGBA", (logo.width + pad * 2, logo.height + pad * 2), (0, 0, 0, 0))
    canvas.alpha_composite(outline, (pad, pad))
    canvas.alpha_composite(logo, (pad, pad))
    return canvas


def main(src: str) -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    im = Image.open(src).convert("RGB")
    bg = _bg_color(im)
    box = _content_box(im, bg)
    im.save(ICONS / "ruffs-logo-source.png", optimize=True)

    # Logo transparente, ajustado al contenido.
    tight = im.crop((max(0, box[0] - 12), max(0, box[1] - 12),
                     min(im.width, box[2] + 12), min(im.height, box[3] + 12)))
    logo = _transparent(tight, bg)
    logo.thumbnail((512, 512), Image.LANCZOS)
    logo = _with_outline(logo, bg, px=3)
    logo.save(ICONS / "ruffs-logo.png", optimize=True)

    # Iconos de la app: imagen completa (el perro queda dentro de la zona
    # segura de los iconos "maskable").
    full = im.resize((512, 512), Image.LANCZOS)
    full.save(ROOT / "static" / "icon-512.png", optimize=True)
    im.resize((192, 192), Image.LANCZOS).save(ROOT / "static" / "icon-192.png", optimize=True)

    # Pestaña y Windows: recorte más cerrado para que se distinga en pequeño.
    fav = _square_crop(im, box, 0.06)
    fav.resize((180, 180), Image.LANCZOS).save(ICONS / "apple-touch-icon.png", optimize=True)
    fav.resize((32, 32), Image.LANCZOS).save(ICONS / "favicon-32.png", optimize=True)
    fav.resize((256, 256), Image.LANCZOS).save(
        ICONS / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    fav.resize((256, 256), Image.LANCZOS).save(
        ROOT / "ruffs.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

    print(f"fondo={bg} contenido={box}")
    for p in sorted(ICONS.iterdir()) + [ROOT / "static" / "icon-192.png", ROOT / "static" / "icon-512.png", ROOT / "ruffs.ico"]:
        print(f"  {p.relative_to(ROOT)}  {p.stat().st_size // 1024} KB")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
