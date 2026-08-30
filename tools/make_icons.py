"""Resize the source artwork into the sizes Home Assistant asks for.

The artwork itself lives in ``assets/`` at full resolution. Home Assistant does
not read an integration's logo from the integration - the frontend fetches it
from brands.home-assistant.io by domain - so what this writes is what the
home-assistant/brands repository wants, ready to be submitted there. See
``brands/README.md``.

Run from the repository root:

    python tools/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
BRANDS_DIR = ROOT / "brands" / "custom_integrations" / "ezviz_doorbell"

ICON_SIZE = 256  # and twice that for the @2x
LOGO_WIDTH = 512  # at most 256 high, which 3:1 artwork clears easily


def make_icons() -> None:
    """Write the square icon, at both sizes."""
    source = Image.open(ASSETS / "icon-source.png").convert("RGBA")
    for name, size in (("icon.png", ICON_SIZE), ("icon@2x.png", ICON_SIZE * 2)):
        source.resize((size, size), Image.LANCZOS).save(
            BRANDS_DIR / name, "PNG", optimize=True
        )
        print(f"  {name}  {size}x{size}")


def make_logos() -> None:
    """Write the wide logo, at both sizes, keeping its aspect ratio."""
    source = Image.open(ASSETS / "logo-source.png").convert("RGBA")
    for name, width in (("logo.png", LOGO_WIDTH), ("logo@2x.png", LOGO_WIDTH * 2)):
        height = round(width * source.height / source.width)
        source.resize((width, height), Image.LANCZOS).save(
            BRANDS_DIR / name, "PNG", optimize=True
        )
        print(f"  {name}  {width}x{height}")


def main() -> None:
    """Generate every image this repository ships."""
    BRANDS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Writing artwork to {BRANDS_DIR}")
    make_icons()
    make_logos()


if __name__ == "__main__":
    main()
