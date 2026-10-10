#!/usr/bin/env python3
"""Create a small folder of sample pictures for trying Lumen."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "sample-photos"


def _canvas(size, color, label):
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((16, 16, size[0] - 16, size[1] - 16), outline="white", width=4)
    draw.text((32, 32), label, fill="white")
    return image


def make_samples(dest: Path = DEST) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "trips" / "2024").mkdir(parents=True, exist_ok=True)
    (dest / "formats").mkdir(parents=True, exist_ok=True)

    harbor = _canvas((960, 640), "#1d4e89", "Harbor morning")
    draw = ImageDraw.Draw(harbor)
    draw.ellipse((700, 60, 860, 220), fill="#f0b429")
    draw.rectangle((0, 460, 960, 640), fill="#16324f")
    harbor.save(dest / "harbor-morning.jpg", "JPEG", quality=90)

    kitchen = _canvas((800, 600), "#c46b3a", "Kitchen window")
    draw = ImageDraw.Draw(kitchen)
    draw.rectangle((180, 120, 620, 460), fill="#f3e2c4")
    draw.rectangle((220, 160, 400, 420), fill="#8fb8d6")
    kitchen.save(dest / "kitchen-window.jpg", "JPEG", quality=90)

    bike = _canvas((900, 600), "#b03a48", "Red bicycle")
    draw = ImageDraw.Draw(bike)
    draw.ellipse((160, 300, 340, 480), outline="white", width=8)
    draw.ellipse((520, 300, 700, 480), outline="white", width=8)
    draw.line((250, 390, 610, 390), fill="white", width=8)
    bike.save(dest / "red-bicycle.jpg", "JPEG", quality=90)

    market = _canvas((880, 560), "#5c3d8a", "Night market")
    draw = ImageDraw.Draw(market)
    for x in range(40, 840, 80):
        draw.rectangle((x, 200, x + 50, 420), fill="#f2c14e")
    market.save(dest / "night-market.jpg", "JPEG", quality=90)

    portrait = _canvas((640, 800), "#2f4858", "Portrait study")
    draw = ImageDraw.Draw(portrait)
    draw.ellipse((170, 140, 470, 460), fill="#e6c2a4")
    draw.polygon([(120, 760), (320, 500), (520, 760)], fill="#234e70")
    portrait.save(dest / "portrait-study.jpg", "JPEG", quality=90)

    trail = _canvas((1000, 640), "#2f6a4a", "Green trail")
    draw = ImageDraw.Draw(trail)
    draw.polygon([(0, 640), (220, 220), (420, 640)], fill="#1e4632")
    draw.polygon([(300, 640), (620, 140), (980, 640)], fill="#173828")
    trail.save(dest / "trips" / "2024" / "green-trail.jpg", "JPEG", quality=90)

    door = _canvas((720, 720), "#24577a", "Blue door")
    draw = ImageDraw.Draw(door)
    draw.rectangle((220, 80, 500, 640), fill="#16324f")
    draw.ellipse((430, 340, 460, 370), fill="#f0b429")
    door.save(dest / "blue-door.jpg", "JPEG", quality=90)

    rotated = _canvas((320, 180), "#884422", "EXIF orientation 6")
    draw = ImageDraw.Draw(rotated)
    draw.rectangle((0, 0, 36, 180), fill="#ffdd55")
    exif = Image.Exif()
    exif[0x0112] = 6
    rotated.save(dest / "rotated-exif.jpg", "JPEG", quality=90, exif=exif)

    upper = _canvas((400, 300), "#444444", "UPPER.JPG")
    upper.save(dest / "Upper.JPG", "JPEG", quality=85)

    png = _canvas((240, 160), "#0b6e4f", "PNG")
    png.save(dest / "formats" / "tiny.png", "PNG")
    webp = _canvas((240, 160), "#7a3e9d", "WEBP")
    webp.save(dest / "formats" / "tiny.webp", "WEBP", quality=80)
    gif = _canvas((240, 160), "#9d3e3e", "GIF")
    gif.save(dest / "formats" / "tiny.gif", "GIF")
    tiff = _canvas((240, 160), "#3e5f9d", "TIFF")
    tiff.save(dest / "formats" / "tiny.tiff", "TIFF")

    (dest / "notes.txt").write_text("not a photo\n", encoding="utf-8")
    (dest / "trips" / "readme.txt").write_text("skip me\n", encoding="utf-8")
    hidden = _canvas((80, 60), "#111111", "hidden")
    hidden.save(dest / ".secret.jpg", "JPEG", quality=70)
    return dest


if __name__ == "__main__":
    folder = make_samples()
    print(folder)
    sys.exit(0)
