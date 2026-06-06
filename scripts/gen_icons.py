"""Generate PWA icons: a radar sweep silhouette on pure-black.

Solid-white-on-black, flat, no outlines — matches the fleet's icon style. A set
of concentric range rings, a swept wedge, and a single blip read clearly down to
favicon size.

Writes ``icon-512.png``, ``icon-512-maskable.png``, ``icon-180.png`` and a
multi-size ``favicon.ico`` into ``app/webapp/static/``.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BG = (10, 10, 10)
FG = (240, 240, 240)
BLIP = (37, 211, 102)  # WhatsApp green

OUT_DIR = Path(__file__).resolve().parent.parent / "app" / "webapp" / "static"


def draw_radar(size: int, inset: float) -> Image.Image:
    """Render a radar sweep centred on a black square.

    ``inset`` is the fraction of the canvas reserved as padding (used for the
    maskable variant's safe margins).
    """
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)

    pad = int(size * inset)
    content = size - 2 * pad
    cx = cy = size // 2
    r = content // 2
    ring_w = max(2, content // 64)

    # Concentric range rings.
    for frac in (1.0, 0.66, 0.33):
        rr = int(r * frac)
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=FG, width=ring_w)

    # Cross-hairs.
    d.line([cx - r, cy, cx + r, cy], fill=FG, width=ring_w)
    d.line([cx, cy - r, cx, cy + r], fill=FG, width=ring_w)

    # Swept wedge (a translucent-looking solid pie slice, top-right quadrant).
    d.pieslice([cx - r, cy - r, cx + r, cy + r], start=-60, end=0, fill=FG)

    # Blip near the leading edge of the sweep.
    blip_r = max(3, content // 22)
    bx = cx + int(r * 0.5)
    by = cy - int(r * 0.42)
    d.ellipse([bx - blip_r, by - blip_r, bx + blip_r, by + blip_r], fill=BLIP)

    return img


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    draw_radar(512, inset=0.08).save(OUT_DIR / "icon-512.png", "PNG")
    draw_radar(512, inset=0.20).save(OUT_DIR / "icon-512-maskable.png", "PNG")
    draw_radar(180, inset=0.08).save(OUT_DIR / "icon-180.png", "PNG")
    draw_radar(256, inset=0.08).save(
        OUT_DIR / "favicon.ico",
        "ICO",
        sizes=[(16, 16), (32, 32), (48, 48)],
    )

    print(f"wrote icons to {OUT_DIR}")


if __name__ == "__main__":
    main()
