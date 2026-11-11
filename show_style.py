"""Original-video show graphics for Bajo Mundo TV, with optional speech subtitles.

The input timeline is untouched. Generated speech captions are only suggestions:
automatic transcription can be inaccurate, especially with music or slang.
"""
from pathlib import Path
import re
from PIL import Image, ImageDraw, ImageFont


def _font(size):
    paths = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    )
    for path in paths:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def create_follow_banner(path):
    """Create transparent, broadcast-style banner using text, not a fake link."""
    path = Path(path)
    image = Image.new("RGBA", (950, 145), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 5, 946, 135), radius=35,
                           fill=(11, 15, 24, 238),
                           outline=(255, 188, 54, 255), width=6)
    draw.rounded_rectangle((27, 26, 182, 115), radius=25,
                           fill=(188, 30, 30, 255))
    draw.text((48, 44), "TV", font=_font(50), fill=(255, 255, 255, 255))
    phrase = "SÍGUENOS PARA MÁS"
    textfont = _font(56)
    while draw.textbbox((0, 0), phrase, font=textfont)[2] > 720:
        textfont = _font(textfont.size - 2)
    draw.text((206, 37), phrase, font=textfont, fill=(255, 255, 255, 255),
              stroke_width=1, stroke_fill=(0, 0, 0, 255))
    draw.line((211, 115, 893, 115), fill=(238, 166, 41, 220), width=4)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)
    return path


def _ass_time(seconds):
    centis = max(0, int(round(seconds * 100)))
    h, centis = divmod(centis, 360000)
    m, centis = divmod(centis, 6000)
    s, cs = divmod(centis, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def _ass_safe(text):
    value = str(text or "").replace("\\", "").replace("{", "").replace("}", "")
    value = re.sub(r"[\r\n\t]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def create_subtitle_file(path, words, width, height, *, style="show"):
    """Write time-synced lower-third ASS subtitles with mobile-safe placement.

    These are transcription-derived subtitles, not proof of what was said.
    Returns the number of subtitle cue groups generated.
    """
    if style not in ("show", "clean"):
        raise ValueError("Unsupported subtitle style")
    if width < 100 or height < 100:
        raise ValueError("Video resolution too small for captions")
    size = max(22, min(88, int(width * (0.074 if style == "show" else 0.064))))
    margin_lr = max(18, round(width * 0.07))
    margin_v = max(35, round(height * 0.19))
    # Opaque rectangular text plate creates readability over busy footage.
    primary = "&H0000D8FF" if style == "show" else "&H00FFFFFF"
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Caption,DejaVu Sans,{size},{primary},&H00FFFFFF,"
        f"&H00000000,&H64000000,-1,0,0,0,100,100,0,0,3,2,0,2,"
        f"{margin_lr},{margin_lr},{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, "
        "MarginR, MarginV, Effect, Text\n"
    )
    clean = []
    for word in words:
        try:
            start = float(word["start"])
            end = float(word["end"])
            text = _ass_safe(word["text"])
        except (ValueError, TypeError, KeyError):
            continue
        if text and 0 <= start < end:
            clean.append({"start": start, "end": end, "text": text})
    groups, chunk = [], []
    for word in clean:
        if chunk and (word["start"] - chunk[-1]["end"] > 0.75 or
                      len(chunk) >= 5 or
                      sum(len(x["text"]) for x in chunk) + len(word["text"]) > 33):
            groups.append(chunk)
            chunk = []
        chunk.append(word)
        if re.search(r"[.!?…]$", word["text"]):
            groups.append(chunk)
            chunk = []
    if chunk:
        groups.append(chunk)
    lines = [header]
    for chunk in groups:
        start = chunk[0]["start"]
        end = max(start + 0.15, chunk[-1]["end"] + 0.12)
        phrase = " ".join(word["text"] for word in chunk)
        if len(phrase) > 90:
            phrase = phrase[:87].rstrip() + "…"
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,{phrase}\n")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    return len(groups)
