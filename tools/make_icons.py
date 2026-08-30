"""Resize the source artwork into the add-on's icon.png and logo.png.

The artwork itself lives in ``assets/`` at full resolution; the Supervisor only
ever needs a small square icon and a wide logo, so this script is the one place
that decides those output sizes.

Run from the repository root:

    python tools/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
OUT_DIR = ROOT / "ezviz_doorbell_push"

ICON_SIZE = 512  # square; the Supervisor scales it down for the add-on card
LOGO_WIDTH = 900  # height follows the source aspect ratio

# Home Assistant does not read an integration's logo from the integration. It
# fetches it from brands.home-assistant.io by domain, so these are built to
# that repository's sizes, ready to be submitted there. See the README.
BRANDS_DIR = ROOT / "brands" / "custom_integrations" / "ezviz_doorbell"
BRANDS_ICON = 256  # and twice that for the @2x
BRANDS_LOGO_WIDTH = 512  # at most 256 high, which 3:1 artwork clears easily


def make_icon() -> None:
    """Write the square add-on icon."""
    source = Image.open(ASSETS / "icon-source.png").convert("RGBA")
    icon = source.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
    icon.save(OUT_DIR / "icon.png", "PNG", optimize=True)
    print(f"  icon.png  {icon.width}x{icon.height}")


def make_logo() -> None:
    """Write the wide brand logo, keeping its transparent background."""
    source = Image.open(ASSETS / "logo-source.png").convert("RGBA")
    height = round(LOGO_WIDTH * source.height / source.width)
    logo = source.resize((LOGO_WIDTH, height), Image.LANCZOS)
    logo.save(OUT_DIR / "logo.png", "PNG", optimize=True)
    print(f"  logo.png  {logo.width}x{logo.height}")


def make_brand_images() -> None:
    """Write the four images the brands repository asks for."""
    BRANDS_DIR.mkdir(parents=True, exist_ok=True)

    icon = Image.open(ASSETS / "icon-source.png").convert("RGBA")
    for name, size in (("icon.png", BRANDS_ICON), ("icon@2x.png", BRANDS_ICON * 2)):
        icon.resize((size, size), Image.LANCZOS).save(
            BRANDS_DIR / name, "PNG", optimize=True
        )
        print(f"  brands/{name}  {size}x{size}")

    logo = Image.open(ASSETS / "logo-source.png").convert("RGBA")
    for name, width in (
        ("logo.png", BRANDS_LOGO_WIDTH),
        ("logo@2x.png", BRANDS_LOGO_WIDTH * 2),
    ):
        height = round(width * logo.height / logo.width)
        logo.resize((width, height), Image.LANCZOS).save(
            BRANDS_DIR / name, "PNG", optimize=True
        )
        print(f"  brands/{name}  {width}x{height}")


def main() -> None:
    """Generate every image this repository ships."""
    print(f"Writing artwork to {OUT_DIR}")
    make_icon()
    make_logo()
    make_brand_images()


if __name__ == "__main__":
    main()
