#!/usr/bin/env python3
"""Rebuild BreadSched's icons and logo from the artwork in ``data/brand``.

``breadsched-icon.webp`` (the green tile, no text) becomes the application icon:
transparent outside its rounded tile, at every size GTK's hicolor theme looks
for, inside the package (so an uninstalled checkout finds it too), and in
``data/icons`` as the scalable icon the Linux, Flatpak, and Windows packages
install, and as the Windows icon (``data/icons/org.breadsched.BreadSched.ico``) that
the installer, its Start-menu shortcut, and the Apps list show.
``breadsched-logo.webp`` (with the BreadSched name) becomes the logo the
splash screen and About dialog show. The web pages get a favicon and a home-screen
icon.

Needs Pillow, which BreadSched itself does not: ``python -m pip install pillow``.
"""

from __future__ import annotations

import base64
import io
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "data" / "brand"
APP_ID = "org.breadsched.BreadSched"
PACKAGE = ROOT / "src" / "breadsched"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
#: The sizes in the Windows icon; Windows draws 256 px as its largest.
WINDOWS_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
#: How far a pixel may stray from white and still be the artwork's backdrop.
BACKDROP_TOLERANCE = 18


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"wrote {path.relative_to(ROOT)} ({len(data):,} bytes)")


def windows_icon(images: list[Image.Image]) -> bytes:
    """A Windows ``.ico`` holding each image as a 32-bit bitmap with alpha.

    Bitmaps rather than PNG entries, so every reader (NSIS, the shell, older
    Windows) takes each size. Each bitmap is stored bottom-up in BGRA order,
    twice its height to leave room for the 1-bit mask, which is clear since the
    alpha already says what shows.
    """
    entries: list[bytes] = []
    images = sorted(images, key=lambda image: image.width)
    for image in images:
        rgba = image.convert("RGBA")
        width, height = rgba.size
        rows = [rgba.crop((0, y, width, y + 1)).tobytes("raw", "BGRA") for y in range(height)]
        mask_row = bytes(((width + 31) // 32) * 4)
        header = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0, 0, 0, 0, 0, 0)
        entries.append(header + b"".join(reversed(rows)) + mask_row * height)
    directory = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for image, data in zip(images, entries, strict=True):
        side = image.width
        directory += struct.pack(
            "<BBBBHHII", side % 256, side % 256, 0, 0, 1, 32, len(data), offset
        )
        offset += len(data)
    return directory + b"".join(entries)


def transparent_tile(source: Image.Image) -> Image.Image:
    """The tile with the white backdrop around its rounded corners made clear."""
    rgb = source.convert("RGB")
    marker = (255, 0, 255)
    filled = rgb.copy()
    width, height = filled.size
    for corner in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        ImageDraw.floodfill(filled, corner, marker, thresh=BACKDROP_TOLERANCE)
    backdrop = Image.new("L", filled.size, 0)
    backdrop.putdata([255 if pixel == marker else 0 for pixel in filled.getdata()])
    # Soften the edge so the corners stay smooth once scaled down.
    alpha = backdrop.filter(ImageFilter.GaussianBlur(1.5)).point(lambda value: 255 - value)
    tile = rgb.convert("RGBA")
    tile.putalpha(alpha)
    return tile.crop(tile.getbbox())


def opaque_tile(tile: Image.Image) -> Image.Image:
    """The tile over its own green, for a home screen that rounds corners itself."""
    green = tile.getpixel((tile.width // 2, tile.height // 40))
    backing = Image.new("RGBA", tile.size, (green[0], green[1], green[2], 255))
    return Image.alpha_composite(backing, tile).convert("RGB")


def trimmed_logo(source: Image.Image) -> Image.Image:
    """The logo with most of its white margin trimmed, on a white card with rounded
    corners (its letters keep their white counters on any theme)."""
    rgb = source.convert("RGB")
    ink = Image.eval(rgb.convert("L"), lambda value: 255 if value < 255 - BACKDROP_TOLERANCE else 0)
    left, top, right, bottom = ink.getbbox()
    margin = round(0.04 * rgb.width)
    card = rgb.crop(
        (
            max(0, left - margin),
            max(0, top - margin),
            min(rgb.width, right + margin),
            min(rgb.height, bottom + margin),
        )
    ).convert("RGBA")
    # Drawn four times larger and scaled down, so the corners are smooth.
    corners = Image.new("L", (card.width * 4, card.height * 4), 0)
    radius = round(0.05 * card.width) * 4
    ImageDraw.Draw(corners).rounded_rectangle(
        (0, 0, corners.width - 1, corners.height - 1), radius=radius, fill=255
    )
    card.putalpha(corners.resize(card.size, Image.LANCZOS))
    return card


def main() -> None:
    tile = transparent_tile(Image.open(BRAND / "breadsched-icon.webp"))
    icons = PACKAGE / "gui" / "resources" / "icons" / "hicolor"
    for size in ICON_SIZES:
        scaled = tile.resize((size, size), Image.LANCZOS)
        _write(icons / f"{size}x{size}" / "apps" / f"{APP_ID}.png", _png(scaled))
    # The packages install one scalable icon: an SVG carrying the 512 px image.
    large = base64.b64encode(_png(tile.resize((512, 512), Image.LANCZOS))).decode("ascii")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="512" height="512" viewBox="0 0 512 512">\n'
        f'  <image width="512" height="512" xlink:href="data:image/png;base64,{large}"/>\n'
        "</svg>\n"
    )
    _write(ROOT / "data" / "icons" / f"{APP_ID}.svg", svg.encode("ascii"))
    # The Windows icon is made of the same images the hicolor tree carries.
    sized = [Image.open(icons / f"{s}x{s}" / "apps" / f"{APP_ID}.png") for s in WINDOWS_ICON_SIZES]
    _write(ROOT / "data" / "icons" / f"{APP_ID}.ico", windows_icon(sized))

    static = PACKAGE / "web" / "static"
    _write(static / "favicon.png", _png(tile.resize((64, 64), Image.LANCZOS)))
    home_screen = opaque_tile(tile).resize((180, 180), Image.LANCZOS)
    _write(static / "apple-touch-icon.png", _png(home_screen))

    logo = trimmed_logo(Image.open(BRAND / "breadsched-logo.webp"))
    width = 640
    height = round(logo.height * width / logo.width)
    _write(
        PACKAGE / "gui" / "resources" / "breadsched-logo.png",
        _png(logo.resize((width, height), Image.LANCZOS)),
    )


if __name__ == "__main__":
    main()
