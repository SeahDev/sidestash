"""Draw the Sidestash icon in every size it's used at, from the same shapes as unraid/icon.svg.

    uv run --with pillow python scripts/build-icon.py

Writes:
  unraid/icon.png                      512px, rounded tile (Unraid)
  app/static/favicon/favicon.ico       16, 32 and 48px, rounded tile (browser tabs)
  app/static/favicon/apple-touch-icon.png  180px, square: iOS rounds the corners itself
  app/static/favicon/favicon.svg       copy of unraid/icon.svg (sharp at any size)

The grid is the dashboard's Overview tab icon: four rounded squares on a 24-unit
grid. Each size is drawn at 4x and scaled down for smooth edges; tab sizes get a
heavier stroke so the squares stay apart at 16px.
"""
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
FAV = ROOT / "app" / "static" / "favicon"
BLUE, WHITE = (0, 113, 227, 255), (255, 255, 255, 255)
SS = 4


def draw(size: int, rounded: bool = True, stroke: float = 1.8, grid: bool = True) -> Image.Image:
    """The icon at `size` px. `stroke` is in 24-unit grid units (1.8 matches the tab icon)."""
    big = size * SS
    scale = big / 512 * 16.5            # icon.svg scales the 24-unit grid 16.5x on a 512 tile
    offset = big / 512 * 58
    px = lambda v: round(v)
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=px(big * 115 / 512) if rounded else 0, fill=BLUE)
    w = stroke * scale
    for gx, gy in [(3.5, 3.5), (13.5, 3.5), (3.5, 13.5), (13.5, 13.5)] if grid else []:
        # An SVG stroke is centered on the path, so grow the outer edge by half the stroke.
        x0, y0 = offset + gx * scale - w / 2, offset + gy * scale - w / 2
        x1, y1 = offset + (gx + 7) * scale + w / 2, offset + (gy + 7) * scale + w / 2
        d.rounded_rectangle([px(x0), px(y0), px(x1), px(y1)], radius=px(1.6 * scale + w / 2),
                            outline=WHITE, width=max(1, px(w)))
    return img.resize((size, size), Image.LANCZOS)


def draw16() -> Image.Image:
    """16px by hand: scaled down, the squares blur together. Four 5x5 outlines,
    2px apart, corner pixels left out so they read as rounded."""
    img = draw(16, grid=False)             # just the tile, with smooth corners
    for x0, y0 in [(2, 2), (9, 2), (2, 9), (9, 9)]:
        for i in range(1, 4):
            for x, y in [(x0 + i, y0), (x0 + i, y0 + 4), (x0, y0 + i), (x0 + 4, y0 + i)]:
                img.putpixel((x, y), WHITE)
    return img


def save(img: Image.Image, path: Path, **kw):
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, **kw)
    print(f"{str(path.relative_to(ROOT)):42} {path.stat().st_size / 1024:6.1f} KB")


save(draw(512), ROOT / "unraid" / "icon.png", optimize=True)
tab = [draw16(), draw(32, stroke=2.1), draw(48, stroke=1.9)]
save(tab[2], FAV / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)], append_images=tab[:2])
save(draw(180, rounded=False).convert("RGB"), FAV / "apple-touch-icon.png", optimize=True)
shutil.copyfile(ROOT / "unraid" / "icon.svg", FAV / "favicon.svg")
print(f"{'app/static/favicon/favicon.svg':42} {(FAV / 'favicon.svg').stat().st_size / 1024:6.1f} KB")
