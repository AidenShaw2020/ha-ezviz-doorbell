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


def main() -> None:
    """Generate both images."""
    print(f"Writing artwork to {OUT_DIR}")
    make_icon()
    make_logo()


if __name__ == "__main__":
    main()
