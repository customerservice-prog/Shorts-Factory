"""Best-effort removal of embedded graphics from authorized source footage.

We never claim to perfectly reconstruct missing image pixels. FFmpeg's delogo
interpolates inside a selected rectangle from its neighbors, and may introduce
visible smearing in complex moving scenes. Automatic OCR only SUGGESTS areas;
users must preview/confirm selected masks.
"""
import csv
import io
import json
import math
import subprocess
import tempfile
from pathlib import Path

MAX_MASKS = 6


def normalize_masks(raw):
    if raw in (None, "", []):
        return []
    if isinstance(raw, str):
        if len(raw) > 3000:
            raise ValueError("Cleanup selection is too long")
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ValueError("Cleanup regions are not valid JSON") from exc
    if not isinstance(raw, list) or len(raw) > MAX_MASKS:
        raise ValueError("Select no more than six cleanup areas")
    result = []
    total_area = 0
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Cleanup area is invalid")
        try:
            x, y, w, h = (float(item[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("Cleanup area requires x, y, width, height") from exc
        if not all(math.isfinite(v) for v in (x,y,w,h)):
            raise ValueError("Cleanup positions must be finite")
        if not (0 <= x <= 100 and 0 <= y <= 100 and
                2 <= w <= 65 and 2 <= h <= 40 and
                x + w <= 100.0001 and y + h <= 100.0001):
            raise ValueError("Cleanup area is outside allowed frame bounds")
        total_area += w*h
        result.append({"x":round(x,2), "y":round(y,2),
                       "w":round(w,2), "h":round(h,2)})
    if total_area > 3000:
        raise ValueError("Cleanup boxes cover too much of the video; use a crop or source re-edit")
    return result


def delogo_filters(masks, width, height):
    """Build bounded FFmpeg filters in ORIGINAL source pixel coordinates."""
    approved = normalize_masks(masks)
    filters = []
    for mask in approved:
        x = max(1, int(width * mask["x"] / 100))
        y = max(1, int(height * mask["y"] / 100))
        w = max(5, int(width * mask["w"] / 100))
        h = max(5, int(height * mask["h"] / 100))
        if x + w >= width:
            w = width - x - 1
        if y + h >= height:
            h = height - y - 1
        if w < 5 or h < 5:
            raise ValueError("Cleanup area too close to the edge")
        filters.append(f"delogo=x={x}:y={y}:w={w}:h={h}:show=0")
    return filters


def _sample_frame(source, seconds, out):
    subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{seconds:.3f}", "-i", str(source),
        "-vf", "scale=640:-2", "-frames:v", "1", str(out)
    ], capture_output=True, check=True, timeout=30)


def suggest_text_masks(source, duration):
    """OCR-based suggestions for likely embedded caption/banner text.

    No suggestion is an automatic removal: environmental text and signs can be
    mistaken for overlays. Only the selected areas are later modified.
    """
    source = Path(source)
    if not source.is_file():
        raise ValueError("Video is missing")
    if not isinstance(duration, (int,float)) or duration <= 0:
        raise ValueError("Video duration is invalid")
    stamps = sorted({round(min(max(0.1, duration*p), max(0.1,duration-0.3)),2)
                     for p in (0.12, 0.48, 0.84)})
    observations = []
    recognized_words = []
    frame_paths = []
    with tempfile.TemporaryDirectory() as td:
        for index, stamp in enumerate(stamps):
            frame = Path(td) / f"frame_{index}.png"
            try:
                _sample_frame(source, stamp, frame)
                if not frame.is_file():
                    continue
                frame_paths.append(frame)
                from PIL import Image
                with Image.open(frame) as im:
                    width, height = im.size
                completed = subprocess.run([
                    "tesseract", str(frame), "stdout",
                    "-l", "eng+spa", "--psm", "11", "tsv"
                ], capture_output=True, text=True, timeout=25, check=True)
                words = csv.DictReader(io.StringIO(completed.stdout), delimiter="\t")
                lines = {}
                for word in words:
                    text = (word.get("text") or "").strip()
                    try:
                        confidence = float(word.get("conf", -1))
                        x, y, w, h = (int(word[k]) for k in
                                      ("left","top","width","height"))
                        if confidence < 58 or len(text) < 2 or w < 5:
                            continue
                        recognized_words.append(text.lower())
                        key = (word["block_num"],word["par_num"],word["line_num"])
                        lines.setdefault(key,[]).append((x,y,w,h,text,confidence))
                    except (ValueError,TypeError,KeyError):
                        continue
                for entries in lines.values():
                    if not entries:
                        continue
                    l = max(0,min(e[0] for e in entries)-8)
                    t = max(0,min(e[1] for e in entries)-8)
                    r = min(width,max(e[0]+e[2] for e in entries)+8)
                    b = min(height,max(e[1]+e[3] for e in entries)+8)
                    x,y,w,h = (l/width*100,t/height*100,
                               (r-l)/width*100,(b-t)/height*100)
                    if w < 2 or h < 2 or w > 65 or h > 24:
                        continue
                    observations.append((index,{"x":round(x,2),"y":round(y,2),
                                         "w":round(w,2),"h":round(h,2)},
                                         round(sum(e[5] for e in entries)/len(entries))))
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
        static_candidates = possible_static_graphic_zones(frame_paths)
    suggestions = []
    # One suggestion per repeated approximate text position, not the words
    # themselves (captions may change during the video).
    for frame_id, box, confidence in sorted(observations,key=lambda x:-x[2]):
        cx, cy = box["x"]+box["w"]/2, box["y"]+box["h"]/2
        found = next((s for s in suggestions
                      if abs((s["x"]+s["w"]/2)-cx)<9 and
                         abs((s["y"]+s["h"]/2)-cy)<6), None)
        if found:
            found["frames"].add(frame_id)
            found["confidence"] = max(found["confidence"],confidence)
        else:
            suggestions.append({**box,"frames":{frame_id},
                                "confidence":confidence})
    # Weak detections are left for manual selection, not guessed.
    suggestions=[s for s in suggestions if len(s["frames"]) >= 2 or
                 (s["confidence"] >= 87 and (s["y"] < 22 or s["y"]>70))]
    suggestions.sort(key=lambda s:(-len(s["frames"]),-s["confidence"]))
    result=[]
    for s in suggestions:
        bbox={k:s[k] for k in ("x","y","w","h")}
        try:
            normalize_masks(result+[bbox])
        except ValueError:
            continue
        result.append(bbox)
        if len(result) >= MAX_MASKS:
            break
    labels = " ".join(recognized_words)
    possible_brand = any(key in labels for key in (
        "buho", "búho", "show tv", "suscrib", "subscribe",
        "youtube", "canal", "follow", "síguenos"))
    possible_own_brand = ("bajo" in labels and "mundo" in labels)
    area = sum(item["w"] * item["h"] for item in result) / 100.0
    heavy = area >= 12 or len(result) >= 5
    return {
        "regions": result,
        "review_candidates": static_candidates,
        "suspected_prior_branding": bool(possible_brand or result or static_candidates),
        "possible_existing_bajo_logo": possible_own_brand,
        "cleanup_difficulty": "heavy" if heavy else
                              ("review" if result or static_candidates else "unknown"),
        "message": "Text regions and possible fixed corner graphics were detected. Some may be part of the scene. Review and approve boxes before removal; missed watermarks can remain.",
        "requires_confirmation": True, "method": "frame-sampled OCR and static-area review"
    }


def possible_static_graphic_zones(frames):
    """Conservative review-only markers for stationary corner/lower-third art.

    No pixel is erased on this evidence alone. A static studio or paused camera
    can look identical to an overlay, so the user MUST inspect every marker.
    """
    from PIL import Image, ImageChops, ImageFilter, ImageStat
    if len(frames) < 3:
        return []
    images = []
    for path in frames:
        try:
            with Image.open(path) as image:
                images.append(image.convert("L").resize((160, 280)))
        except OSError:
            continue
    if len(images) < 3:
        return []
    motion = Image.new("L", images[0].size, color=0)
    for image in images[1:]:
        motion = ImageChops.lighter(motion, ImageChops.difference(images[0], image))
    edge = images[0].filter(ImageFilter.FIND_EDGES)
    # Small, plausible locations rather than auto-masking entire halves.
    zones = [
        (2, 2, 33, 16), (65, 2, 33, 16),
        (2, 75, 35, 18), (63, 75, 35, 18),
        (28, 75, 45, 18), (30, 42, 40, 17),
    ]
    result = []
    for x, y, w, h in zones:
        region = tuple(round(v) for v in (
            160*x/100, 280*y/100, 160*(x+w)/100, 280*(y+h)/100
        ))
        if ImageStat.Stat(motion.crop(region)).mean[0] > 28:
            continue
        if ImageStat.Stat(edge.crop(region)).mean[0] < 24:
            continue
        result.append({"x": float(x), "y": float(y),
                       "w": float(w), "h": float(h),
                       "type": "static_graphic_possible"})
    return result[:4]
