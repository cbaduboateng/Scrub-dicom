"""Draw the Scrub-DICOM mark and write icon.png, icon.ico, icon.icns and the in-app mark next to this file.

    python packaging/icons/make_icons.py            # the icon set + scrubdicom/app/assets/mark.png
    python packaging/icons/make_icons.py --sheet    # also a review sheet at every size

The mark: a hand-drawn flat paintbrush sweeping across the CT gantry ring on a navy tile. Solid brown handle with
a faint grain and a wobbly ink outline, a silver ferrule flecked with paint, ink bristles that bend together into a
tapered edge, and a loose yellow smear trailing from the edge. Drawn procedurally with a fixed random seed, so every
build produces the same pixels. Rules and palette: docs/brand.md.

Needs Pillow at build time only; the app itself does not use it.
"""
from __future__ import annotations

import math
import random
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parents[1] / "scrubdicom" / "app" / "assets"
SS = 4
NAVY = (14, 58, 92)        # #0E3A5C  tile
INK_BLUE = (30, 76, 112)   # inner disc
OFF = (236, 242, 247)      # #ECF2F7  ring
WOOD = (139, 90, 43)       # handle
YELLOWS = [(250, 225, 40), (255, 210, 30), (245, 195, 25), (255, 235, 90), (232, 160, 30)]
SEED = 11


def _bez(p0, p1, p2, t):
    return ((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
            (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1])


def _clipped_scribble(rng, poly, colours, count, width, along, jitter, n):
    layer = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    L = math.hypot(x1 - x0, y1 - y0) * 0.5
    for _ in range(count):
        px, py = rng.uniform(x0, x1), rng.uniform(y0, y1)
        ang = math.atan2(along[1], along[0]) + rng.uniform(-jitter, jitter)
        ln = rng.uniform(L * 0.2, L * 0.7)
        ld.line((px, py, px + math.cos(ang) * ln, py + math.sin(ang) * ln),
                fill=(*rng.choice(colours), rng.randint(80, 170)), width=max(1, int(width * rng.uniform(0.6, 1.3))))
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).polygon(poly, fill=255)
    return layer, mask


def _outline(rng, d, pts, colour, width, passes, jit):
    for _ in range(passes):
        q = [(x + rng.uniform(-jit, jit), y + rng.uniform(-jit, jit)) for x, y in pts]
        d.line(q + [q[0]], fill=colour, width=width, joint="curve")


def _brush(rng, im, tip, angle_deg, length, width, stroke_to, sweep):
    n = im.size[0]
    a = math.radians(angle_deg)
    dx, dy = math.cos(a), math.sin(a)
    nx, ny = -dy, dx
    P = lambda t, s: (tip[0] + dx * length * t + nx * width * s, tip[1] + dy * length * t + ny * width * s)
    jit = n * 0.004
    bx, by = stroke_to[0] - tip[0], stroke_to[1] - tip[1]
    bl = math.hypot(bx, by)
    bx, by = bx / bl, by / bl
    # paint smear from the bristle edge
    sm = Image.new("RGBA", im.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(sm)
    for _ in range(80):
        s = rng.uniform(-0.28, 0.28)
        start = (tip[0] + nx * width * s + bx * width * 0.05, tip[1] + ny * width * s + by * width * 0.05)
        far = rng.uniform(0.3, 1.0)
        end = (start[0] + bx * bl * far + rng.uniform(-sweep, sweep), start[1] + by * bl * far + rng.uniform(-sweep, sweep) * 0.6)
        mid = ((start[0] + end[0]) / 2 + rng.uniform(-sweep, sweep), (start[1] + end[1]) / 2 + rng.uniform(-sweep, sweep) * 0.6)
        pts = [_bez(start, mid, end, t / 12) for t in range(13)]
        sd.line(pts, fill=(*rng.choice(YELLOWS), rng.randint(110, 225)), width=max(2, int(width * rng.uniform(0.02, 0.08))), joint="curve")
    im.alpha_composite(sm.filter(ImageFilter.GaussianBlur(n * 0.003)))
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    # handle
    handle = [P(0.5, -0.24), P(0.62, -0.19), P(0.97, -0.21), P(1.0, -0.12), P(1.0, 0.12), P(0.97, 0.21), P(0.62, 0.19), P(0.5, 0.24)]
    d.polygon(handle, fill=WOOD)
    g, gm = _clipped_scribble(rng, handle, [(160, 108, 56), (118, 74, 34), (176, 124, 66)], 40, n * 0.005, (dx, dy), 0.08, n)
    layer.paste(g, (0, 0), Image.composite(g.split()[3], Image.new("L", (n, n), 0), gm))
    d.polygon([P(0.52, -0.19), P(0.62, -0.15), P(0.96, -0.17), P(0.96, -0.09), P(0.62, -0.06), P(0.52, -0.1)], fill=(178, 124, 68, 150))
    _outline(rng, d, handle, (50, 30, 14, 235), max(2, int(n * 0.006)), 3, jit)
    # ferrule
    fer = [P(0.3, -0.46), P(0.52, -0.42), P(0.52, 0.42), P(0.3, 0.46)]
    d.polygon(fer, fill=(190, 195, 205))
    g, gm = _clipped_scribble(rng, fer, [(230, 235, 240), (150, 155, 165), (120, 200, 120), (90, 160, 230), (230, 90, 70), (255, 220, 60)], 110, n * 0.006, (nx, ny), 0.3, n)
    layer.paste(g, (0, 0), Image.composite(g.split()[3], Image.new("L", (n, n), 0), gm))
    _outline(rng, d, fer, (40, 40, 50, 230), max(2, int(n * 0.006)), 3, jit)
    for s in (-0.22, 0.22):
        r = P(0.41, s)
        rv = width * 0.04
        d.ellipse((r[0] - rv, r[1] - rv, r[0] + rv, r[1] + rv), fill=(70, 74, 84))
    # bristles: one bundle bending to a tapered edge
    for _ in range(70):
        s = rng.uniform(-0.45, 0.45)
        base = P(0.31, s * 0.95)
        s_tip = s * 0.62
        tip_pt = (tip[0] + nx * width * s_tip + bx * width * 0.3, tip[1] + ny * width * s_tip + by * width * 0.3)
        ctrl = P(0.08, s * 0.9)
        pts = [_bez(base, ctrl, tip_pt, t / 12) for t in range(13)]
        shade = rng.randint(16, 52)
        d.line(pts, fill=(shade, shade, shade + 6, rng.randint(180, 255)), width=max(2, int(width * rng.uniform(0.015, 0.045))), joint="curve")
        if rng.random() < 0.35:
            d.line(pts[9:], fill=(*rng.choice(YELLOWS), 210), width=max(2, int(width * 0.022)))
    edge = [P(0.31, -0.43), P(0.1, -0.41), (tip[0] + nx * width * -0.28 + bx * width * 0.3, tip[1] + ny * width * -0.28 + by * width * 0.3),
            (tip[0] + nx * width * 0.28 + bx * width * 0.3, tip[1] + ny * width * 0.28 + by * width * 0.3), P(0.1, 0.41), P(0.31, 0.43)]
    _outline(rng, d, edge, (12, 12, 16, 120), max(2, int(n * 0.004)), 2, jit)
    im.alpha_composite(layer)


def mark(size: int, tile: bool = True) -> Image.Image:
    """The mark at `size` px. tile=False leaves the background transparent (for the in-app header)."""
    rng = random.Random(SEED)
    n = size * SS
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if tile:
        d.rounded_rectangle((0, 0, n - 1, n - 1), radius=n * 0.22, fill=NAVY)
    cx = cy = n / 2
    R, w = n * 0.34, n * 0.065
    d.ellipse((cx - R, cy - R, cx + R, cy + R), outline=OFF, width=int(w))
    d.ellipse((cx - R * 0.62, cy - R * 0.62, cx + R * 0.62, cy + R * 0.62), fill=INK_BLUE if tile else (30, 76, 112, 255))
    _brush(rng, im, tip=(n * 0.46, n * 0.62), angle_deg=-58, length=n * 0.45, width=n * 0.19, stroke_to=(n * 0.14, n * 0.74), sweep=n * 0.025)
    return im.resize((size, size), Image.LANCZOS)


def sheet(path: Path) -> None:
    try:
        f_small = ImageFont.truetype("/System/Library/Fonts/HelveticaNeue.ttc", 18, index=0)
        f_word = ImageFont.truetype("/System/Library/Fonts/HelveticaNeue.ttc", 64, index=1)
    except Exception:
        f_small = f_word = ImageFont.load_default()
    im = Image.new("RGB", (1500, 640), (248, 248, 250))
    d = ImageDraw.Draw(im)
    d.text((40, 18), "Scrub-DICOM mark at 512, 256, 128, 64, 32, 16 px, and the wordmark", fill=(60, 60, 70), font=f_small)
    x, y = 40, 60
    for sz in (512, 256, 128, 64, 32, 16):
        m = mark(sz)
        im.paste(m, (x, y + 512 - sz), m)
        x += sz + 24
    lock = mark(72)
    im.paste(lock, (x + 10, y + 200), lock)
    d.text((x + 100, y + 206), "Scrub", fill=(12, 38, 66), font=f_word)
    d.text((x + 290, y + 206), "DICOM", fill=(0, 186, 168), font=f_word)
    im.save(path)


def main() -> int:
    mark(1024).save(HERE / "icon.png")
    mark(256).save(HERE / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    ASSETS.mkdir(parents=True, exist_ok=True)
    for px in (24, 32, 48, 64, 96, 128, 192):       # exact sizes for the app; resampled here, never shrunk by Tk
        mark(px).save(ASSETS / f"mark_{px}.png")
    (ASSETS / "mark.png").unlink(missing_ok=True)
    if sys.platform == "darwin" and shutil.which("iconutil"):
        iconset = HERE / "icon.iconset"
        iconset.mkdir(exist_ok=True)
        for base in (16, 32, 128, 256, 512):
            mark(base).save(iconset / f"icon_{base}x{base}.png")
            mark(base * 2).save(iconset / f"icon_{base}x{base}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "icon.icns")], check=True)
        shutil.rmtree(iconset)
    if "--sheet" in sys.argv:
        sheet(HERE / "brand_sheet.png")
    print("icons written to", HERE, "and", ASSETS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
