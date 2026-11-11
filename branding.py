"""Persistent per-channel branding for Shorts Factory.

The built-in asset is sourced from the user's Bajo Mundo TV repository and
cut out from its white background, preserving white lettering within the logo.
"""
import json
import os
import re
import time
import uuid
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STUDIO_BASE = Path(os.getenv("DATA_DIR", "./data")).resolve()
BRAND_DIR = STUDIO_BASE / "channel-brands"
SOURCE = ROOT / "static" / "brands" / "bajo-mundo-original.webp"
DEFAULT_LOGO = BRAND_DIR / "bajo-mundo-transparent.png"
STUDIO_LOGO = BRAND_DIR / "bajo-mundo-studio-v1.png"
PROFILE_ID = re.compile(r"^[0-9a-f]{20}$")
MAX_BRANDS = 20


def _ensure_dir():
    BRAND_DIR.mkdir(parents=True, exist_ok=True)


def prepare_default_logo():
    """Remove only near-white edge-connected pixels to retain white text."""
    if DEFAULT_LOGO.is_file() and DEFAULT_LOGO.stat().st_size > 5000:
        return DEFAULT_LOGO
    from PIL import Image
    _ensure_dir()
    image = Image.open(SOURCE).convert("RGBA")
    w, h = image.size
    if w * h > 4_000_000:
        raise RuntimeError("Bajo Mundo logo has unexpected dimensions")
    pix = image.load()
    visited = bytearray(w * h)
    queue = deque()

    def push(x, y):
        i = y * w + x
        if visited[i]:
            return
        r, g, b, a = pix[x, y]
        if min(r, g, b) >= 226 and max(r, g, b) - min(r, g, b) <= 23:
            visited[i] = 1
            queue.append(i)

    for x in range(w):
        push(x, 0)
        push(x, h - 1)
    for y in range(h):
        push(0, y)
        push(w - 1, y)
    count = len(queue)
    while queue:
        i = queue.popleft()
        x, y = i % w, i // w
        if x:
            before = len(queue)
            push(x - 1, y)
            count += len(queue) - before
        if x + 1 < w:
            before = len(queue)
            push(x + 1, y)
            count += len(queue) - before
        if y:
            before = len(queue)
            push(x, y - 1)
            count += len(queue) - before
        if y + 1 < h:
            before = len(queue)
            push(x, y + 1)
            count += len(queue) - before
    if not w * h * .03 < count < w * h * .82:
        raise RuntimeError("Bajo Mundo logo matte removal failed quality check")
    for y in range(h):
        for x in range(w):
            if visited[y * w + x]:
                pix[x, y] = (0, 0, 0, 0)
    bbox = image.getbbox()
    if not bbox:
        raise RuntimeError("Empty brand image after processing")
    left, top, right, bottom = bbox
    image = image.crop((max(0, left - 12), max(0, top - 12),
                        min(w, right + 12), min(h, bottom + 12)))
    temp = DEFAULT_LOGO.with_suffix(".tmp.png")
    image.save(temp, format="PNG", optimize=True)
    temp.replace(DEFAULT_LOGO)
    return DEFAULT_LOGO



def prepare_studio_logo():
    """Draw a compact, readable Bajo Mundo TV video logo with Pillow.

    The wide wordmark retains visibility at phone-playback size.
    No user logo or existing render is overwritten.
    """
    if STUDIO_LOGO.is_file() and STUDIO_LOGO.stat().st_size > 7000:
        return STUDIO_LOGO
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    _ensure_dir()
    width, height = 1000, 340
    im = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle((27, 31, 973, 309), radius=82,
                         outline=(237, 46, 23, 125), width=14)
    im.alpha_composite(glow.filter(ImageFilter.GaussianBlur(12)))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((22, 24, 978, 315), radius=82,
                        fill=(9, 13, 21, 246), outline=(247, 73, 32, 253),
                        width=9)
    d.rounded_rectangle((32, 34, 968, 305), radius=75,
                        outline=(255, 202, 57, 210), width=4)
    d.line([(115, 77), (145, 48), (176, 78)], fill=(255, 208, 65, 255),
           width=10, joint="curve")
    d.ellipse((136, 39, 154, 57), fill=(255, 233, 108, 255))
    d.rounded_rectangle((63, 79, 236, 258), radius=43,
                        fill=(191, 23, 30, 255), outline=(255, 202, 61, 255),
                        width=9)
    d.rounded_rectangle((78, 96, 220, 240), radius=28,
                        fill=(23, 26, 37, 255), outline=(255, 243, 223, 230),
                        width=4)
    d.polygon([(124, 130), (124, 208), (181, 169)],
              fill=(255, 214, 72, 255))
    for yy, length in ((123, 23), (167, 32), (212, 23)):
        d.line([(252, yy), (252+length, yy)], fill=(245, 167, 48, 255),
               width=8)
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf"
    if not Path(font).is_file():
        font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    bold = ImageFont.truetype(font, 108)
    large = ImageFont.truetype(font, 111)
    small = ImageFont.truetype(font, 28)
    d.text((306, 49), "BAJO", font=bold, fill=(252, 252, 251, 255),
           stroke_width=4, stroke_fill=(19, 24, 32, 255))
    d.text((302, 152), "MUNDO", font=large, fill=(254, 203, 54, 255),
           stroke_width=5, stroke_fill=(34, 17, 8, 255))
    d.rounded_rectangle((760, 97, 941, 190), radius=27,
                        fill=(195, 29, 33, 255),
                        outline=(255, 218, 89, 255), width=5)
    tv_font = ImageFont.truetype(font, 72)
    box = d.textbbox((0, 0), "TV", font=tv_font)
    d.text((850 - (box[2]-box[0])//2, 98), "TV", font=tv_font,
           fill=(255, 255, 253, 255), stroke_width=2,
           stroke_fill=(75, 22, 25, 255))
    d.text((329, 273), "MÚSICA  ·  CULTURA  ·  CALLE", font=small,
           fill=(228, 222, 208, 240))
    temp = STUDIO_LOGO.with_suffix(".tmp.png")
    im.save(temp, format="PNG", optimize=True)
    temp.replace(STUDIO_LOGO)
    return STUDIO_LOGO


def brand_path(brand_id):
    if brand_id == "none":
        return None
    if brand_id == "bajo-mundo-studio":
        return prepare_studio_logo()
    if brand_id == "bajo-mundo":
        return prepare_default_logo()
    if not PROFILE_ID.fullmatch(brand_id or ""):
        raise ValueError("Unknown channel brand")
    folder = BRAND_DIR / brand_id
    path = folder / "logo.png"
    if not (folder / "profile.json").is_file() or not path.is_file():
        raise ValueError("Channel logo not found")
    return path


def list_brands():
    result = [{"id": "bajo-mundo", "name": "Bajo Mundo TV · Original logo",
               "built_in": True, "logo_url": "/api/brands/bajo-mundo/logo"},
              {"id": "bajo-mundo-studio", "name": "Bajo Mundo TV · Alternative watermark",
               "built_in": True, "logo_url": "/api/brands/bajo-mundo-studio/logo"},
              {"id": "none", "name": "No logo", "built_in": True, "logo_url": None}]
    if not BRAND_DIR.is_dir():
        return result
    for folder in sorted(BRAND_DIR.iterdir()):
        if not folder.is_dir() or not PROFILE_ID.fullmatch(folder.name):
            continue
        try:
            info = json.loads((folder / "profile.json").read_text(encoding="utf-8"))
            if (folder / "logo.png").is_file():
                result.append({"id": folder.name, "name": str(info["name"])[:55],
                               "built_in": False,
                               "logo_url": "/api/brands/" + folder.name + "/logo"})
        except (OSError, ValueError, KeyError):
            continue
    return result


def save_brand(name, file_path):
    from PIL import Image, UnidentifiedImageError
    name = " ".join((name or "").split())[:55]
    if len(name) < 2:
        raise ValueError("Enter a channel name")
    if len(list_brands()) - 3 >= MAX_BRANDS:
        raise ValueError("Channel profile limit reached")
    try:
        with Image.open(file_path) as source:
            if source.width * source.height > 16_000_000:
                raise ValueError("Logo dimensions are too large")
            image = source.convert("RGBA")
            if image.width > 1200 or image.height > 1200:
                image.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("Invalid image file") from exc
    _ensure_dir()
    profile = uuid.uuid4().hex[:20]
    folder = BRAND_DIR / profile
    folder.mkdir(mode=0o700)
    try:
        image.save(folder / "logo.png", format="PNG", optimize=True)
        (folder / "profile.json").write_text(
            json.dumps({"name": name, "created": int(time.time())}),
            encoding="utf-8")
    except Exception:
        for file in folder.iterdir():
            file.unlink()
        folder.rmdir()
        raise
    return {"id": profile, "name": name, "built_in": False,
            "logo_url": "/api/brands/" + profile + "/logo"}
