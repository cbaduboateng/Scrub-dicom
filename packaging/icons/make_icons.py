"""Draw the app icon and write icon.png, icon.ico and (on macOS) icon.icns next to this file.

    python packaging/icons/make_icons.py

Needs Pillow at build time only; the app itself does not use it. The icon is a stylised CT gantry ring with
a heart inside on a dark blue tile: no text, so it reads at 16 px.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
BG, RING, HEART = (14, 58, 92), (236, 242, 247), (214, 52, 66)


def draw(size: int) -> Image.Image:
    s = 4  # supersample for smooth edges
    n = size * s
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    r = n * 0.22
    d.rounded_rectangle((0, 0, n - 1, n - 1), radius=r, fill=BG)
    # gantry ring
    cx = cy = n / 2
    R, w = n * 0.36, n * 0.075
    d.ellipse((cx - R, cy - R, cx + R, cy + R), outline=RING, width=int(w))
    # heart: two circles and a triangle, slightly below centre
    hs = n * 0.19
    hy = cy + n * 0.02
    lobe = hs * 0.55
    d.ellipse((cx - hs, hy - hs * 0.9, cx - hs + 2 * lobe, hy - hs * 0.9 + 2 * lobe), fill=HEART)
    d.ellipse((cx + hs - 2 * lobe, hy - hs * 0.9, cx + hs, hy - hs * 0.9 + 2 * lobe), fill=HEART)
    top = hy - hs * 0.9 + lobe
    d.polygon([(cx - hs, top), (cx + hs, top), (cx, hy + hs * 0.95)], fill=HEART)
    # a small gap in the ring at 45 degrees: the "scrub" cut
    a = math.radians(45)
    gx, gy = cx + R * math.cos(a), cy - R * math.sin(a)
    g = w * 0.9
    d.ellipse((gx - g, gy - g, gx + g, gy + g), fill=BG)
    return im.resize((size, size), Image.LANCZOS)


def main() -> int:
    png = draw(1024)
    png.save(HERE / "icon.png")
    draw(256).save(HERE / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    if sys.platform == "darwin" and shutil.which("iconutil"):
        iconset = HERE / "icon.iconset"
        iconset.mkdir(exist_ok=True)
        for base in (16, 32, 128, 256, 512):
            draw(base).save(iconset / f"icon_{base}x{base}.png")
            draw(base * 2).save(iconset / f"icon_{base}x{base}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "icon.icns")], check=True)
        shutil.rmtree(iconset)
    print("icons written to", HERE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
