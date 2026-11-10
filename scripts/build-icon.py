"""Draw unraid/icon.png (512x512) to match unraid/icon.svg.

    uv run --with pillow python scripts/build-icon.py

Drawn at 4x and scaled down for smooth edges. The grid is the dashboard's
Overview tab icon: four rounded squares on a 24-unit grid, scaled 16.5x.
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "unraid" / "icon.png"
SIZE, SS = 512, 4                      # final size, supersampling factor
BLUE, WHITE = (0, 113, 227, 255), (255, 255, 255, 255)
SCALE, OFFSET = 16.5, 58               # same transform as icon.svg
STROKE, RADIUS = 1.8, 1.6              # in 24-unit grid units


def px(v: float) -> int:
    return round(v * SS)


img = Image.new("RGBA", (SIZE * SS, SIZE * SS), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, SIZE * SS - 1, SIZE * SS - 1], radius=px(115), fill=BLUE)

w = STROKE * SCALE
for gx, gy in [(3.5, 3.5), (13.5, 3.5), (3.5, 13.5), (13.5, 13.5)]:
    # An SVG stroke is centered on the path, so grow the outer edge by half the stroke.
    x0, y0 = OFFSET + gx * SCALE - w / 2, OFFSET + gy * SCALE - w / 2
    x1, y1 = OFFSET + (gx + 7) * SCALE + w / 2, OFFSET + (gy + 7) * SCALE + w / 2
    d.rounded_rectangle([px(x0), px(y0), px(x1), px(y1)], radius=px(RADIUS * SCALE + w / 2),
                        outline=WHITE, width=px(w))

img.resize((SIZE, SIZE), Image.LANCZOS).save(OUT, optimize=True)
print(f"{OUT.relative_to(OUT.parent.parent)}  {OUT.stat().st_size / 1024:.1f} KB")
