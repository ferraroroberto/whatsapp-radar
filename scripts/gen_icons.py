"""Generate PWA/tray/Stream-Deck icons from the shared fleet icon-brand generator.

Thin caller onto ``project-scaffolding``'s ``brand_gen.render_set()`` — the
master art is whatsapp-radar's vendored Lucide ``radar.svg``, not a bespoke
Pillow-drawn silhouette (app-launcher#65: a coherent icon family across the
fleet). Drops the previous WhatsApp-green blip tint in favor of the fleet's
monochrome white-on-`#0A0A0A` look.

Writes into ``app/webapp/static/``: ``icon-512.png``, ``icon-512-maskable.png``,
``icon-180.png``, ``icon-192.png``, ``favicon.ico``. Into ``assets/tray/``:
``whatsapp-radar.ico``. Into ``assets/stream-deck/``: ``whatsapp-radar-144.png``.

Usage:
    python scripts/gen_icons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

SCAFFOLDING_SCRIPTS = Path(r"E:\automation\project-scaffolding\scripts")
sys.path.insert(0, str(SCAFFOLDING_SCRIPTS))

from brand_gen import render_set  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "app" / "webapp" / "static"


def main() -> None:
    render_set(
        master=Path(r"E:\automation\project-scaffolding\brand\radar.svg"),
        out_dir=STATIC_DIR,
        tray_out_dir=PROJECT_ROOT / "assets" / "tray",
        stream_deck_out_dir=PROJECT_ROOT / "assets" / "stream-deck",
        project_slug="whatsapp-radar",
    )
    print(f"wrote icons to {STATIC_DIR}")


if __name__ == "__main__":
    main()
