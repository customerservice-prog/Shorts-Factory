"""Local speech-aware clip selection and FFmpeg rendering.

No scraping or publishing is performed. Users supply their own video files.
The selection score is heuristic; it does not guarantee virality or monetization.
"""
import json
import os
import re
import subprocess
import threading
import zipfile
from pathlib import Path
from review import inspect_export, risk_review

_MODEL = None
_MODEL_LOCK = threading.Lock()
PUNCH_WORDS = {
    "secret", "truth", "never", "crazy", "unbelievable", "mistake", "reason",
    "actually", "really", "biggest", "first", "money", "million", "risk",
    "imagine", "changed", "happened", "story", "problem", "important",
    "insane", "learned", "behind", "why", "how", "what", "nobody", "everyone",
    "listen", "wait", "real", "exactly", "wow", "no", "yes", "pero", "nunca",
    "dinero", "verdad", "porque", "secreto", "entonces", "mira",
}
FILLERS = {"um", "uh", "erm", "umm", "like", "basically", "literally"}


def command(args, timeout=180):
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("FFmpeg/FFprobe is not installed in the server.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Video processing exceeded its time limit.") from exc
    if res.returncode:
        raise RuntimeError((res.stderr or res.stdout or "Video processor failed")[-1200:])
    return res.stdout


def probe_video(path: Path):
    output = command(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                      "-of", "json", str(path)], 40)
    data = json.loads(output)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("This file does not contain a readable video track.")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration < 3:
        raise RuntimeError("Video needs to be at least 3 seconds long.")
    if duration > 4 * 3600:
        raise RuntimeError("Videos over 4 hours aren't supported.")
    return duration, any(s.get("codec_type") == "audio" for s in streams)


def transcribe(path: Path):
    global _MODEL
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("Speech recognition is not installed.") from exc
    with _MODEL_LOCK:
        if _MODEL is None:
            _MODEL = WhisperModel(os.getenv("WHISPER_MODEL", "base"),
                                  device="cpu", compute_type="int8",
                                  cpu_threads=max(1, int(os.getenv("WHISPER_THREADS", "2"))))
    segments, info = _MODEL.transcribe(
        str(path), beam_size=2, vad_filter=True, word_timestamps=True,
        condition_on_previous_text=False,
    )
    words = []
    for segment in segments:
        if segment.words:
            for word in segment.words:
                if word.start is None or word.end is None:
                    continue
                content = (word.word or "").strip()
                if content:
                    words.append({"start": float(word.start), "end": float(word.end),
                                  "text": content})
        else:
            pieces = (segment.text or "").split()
            if pieces:
                step = (float(segment.end) - float(segment.start)) / len(pieces)
                for ix, piece in enumerate(pieces):
                    words.append({"start": float(segment.start) + ix * step,
                                  "end": float(segment.start) + (ix + 1) * step,
                                  "text": piece})
    return words, getattr(info, "language", "")


def sentence_groups(words):
    if not words:
        return []
    groups, current = [], []
    for i, word in enumerate(words):
        current.append(word)
        pause = words[i + 1]["start"] - word["end"] if i + 1 < len(words) else 999
        if re.search(r"[.!?…]$", word["text"]) or pause > 0.95 or len(current) >= 32:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def clip_title(words, index):
    sentence = " ".join(w["text"] for w in words[:14]).strip()
    sentence = re.sub(r"\s+", " ", sentence).strip(".,!? -")
    if not sentence:
        return "Highlight " + str(index).zfill(2)
    if len(sentence) > 70:
        sentence = sentence[:67].rsplit(" ", 1)[0] + "..."
    return sentence[:1].upper() + sentence[1:]


def score_window(selected, begin, finish, target):
    if not selected:
        return 0.0
    text = " ".join(w["text"] for w in selected).lower()
    tokens = re.findall(r"[\w']+", text, re.UNICODE)
    duration = max(1.0, finish - begin)
    density = min(1.0, (len(tokens) / duration) / 2.3)
    punches = sum(1 for token in tokens if token in PUNCH_WORDS)
    filler = sum(1 for token in tokens if token in FILLERS)
    hook_start = any(t in PUNCH_WORDS for t in tokens[:12])
    ending = bool(re.search(r"[.!?…]$", selected[-1]["text"]))
    length_quality = max(0, 1 - abs(duration - target) / max(target, 1))
    return round(
        39 + 19 * density + 15 * length_quality + 3 * min(punches, 5)
        + (9 if hook_start else 0) + (6 if ending else 0)
        + min(7, text.count("?") * 3 + text.count("!") * 2)
        - min(12, filler * 2), 1
    )


def select_clips(words, duration, count, target):
    # Align starts and endings with sentence boundaries where possible.
    groups = sentence_groups(words)
    candidates = []
    for i, group in enumerate(groups):
        start = max(0, group[0]["start"] - 0.35)
        if start > duration - 4:
            continue
        for j in range(i, len(groups)):
            end = min(duration, groups[j][-1]["end"] + 0.4)
            length = end - start
            if length < min(12, target * 0.60) and end < duration - 0.5:
                continue
            if length > target + 6:
                break
            selected = [w for part in groups[i:j + 1] for w in part]
            score = score_window(selected, start, end, target)
            candidates.append({"start": round(start, 2), "end": round(end, 2),
                               "score": score, "words": selected})
            if j > i + 18:
                break
    # When speech isn't available or isn't long enough, use evenly distributed
    # windows. The UI will explicitly say when captioning was unavailable.
    window = min(float(target), duration)
    step = max(6.0, window * 0.52)
    times = [0.0]
    x = step
    while x < duration - 4:
        times.append(x)
        x += step
    for start in times:
        start = min(start, max(0.0, duration - window))
        end = min(duration, start + window)
        selected = [w for w in words if start <= w["start"] < end]
        candidates.append({"start": round(start, 2), "end": round(end, 2),
                           "score": score_window(selected, start, end, target) - 11,
                           "words": selected})
    candidates.sort(key=lambda c: c["score"], reverse=True)
    chosen = []
    for candidate in candidates:
        if candidate["end"] - candidate["start"] < 3:
            continue
        overlapping = False
        for existing in chosen:
            common = max(0, min(candidate["end"], existing["end"])
                         - max(candidate["start"], existing["start"]))
            smaller = min(candidate["end"] - candidate["start"],
                          existing["end"] - existing["start"])
            if common / max(1, smaller) > 0.32:
                overlapping = True
                break
        if not overlapping:
            chosen.append(candidate)
        if len(chosen) == count:
            break
    # Show the highest scoring first. Scores are relative heuristics, not
    # calibrated probabilities of achieving views.
    return chosen


def ass_time(seconds):
    cents = max(0, round(seconds * 100))
    return f"{cents // 360000}:{(cents // 6000) % 60:02}:{(cents // 100) % 60:02}.{cents % 100:02}"


def ass_escape(text):
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def write_captions(path: Path, words, start: float, end: float, mode: str):
    if not words or mode == "none":
        return False
    selected = [w for w in words if w["end"] > start and w["start"] < end]
    if not selected:
        return False
    primary = "&H0000E8FF" if mode == "pop" else "&H00FFFFFF"
    size = 74 if mode == "pop" else 65
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n"
        "\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Caption,DejaVu Sans,{size},{primary},&H00FFFFFF,"
        "&H00000000,&H70000000,-1,0,0,0,100,100,0,0,1,5,2,2,70,70,400,1\n"
        "\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [header]
    # Group 3-5 words at a time. Whisper word timings drive progressive highlights.
    group = []
    groups = []
    for word in selected:
        group.append(word)
        letters = sum(len(x["text"]) for x in group)
        if len(group) >= 5 or letters >= 27 or re.search(r"[.!?]$", word["text"]):
            groups.append(group)
            group = []
    if group:
        groups.append(group)
    for group in groups:
        first = max(start, group[0]["start"])
        last = min(end, group[-1]["end"] + 0.13)
        if last <= first:
            continue
        if mode == "pop":
            chunks = []
            for i, word in enumerate(group):
                next_start = group[i + 1]["start"] if i + 1 < len(group) else last
                interval = max(7, round((next_start - max(first, word["start"])) * 100))
                chunks.append(r"{\k" + str(interval) + "}" + ass_escape(word["text"]).upper())
            text = " ".join(chunks)
        else:
            text = " ".join(ass_escape(w["text"]) for w in group)
        lines.append(f"Dialogue: 0,{ass_time(first - start)},{ass_time(last - start)},Caption,,0,0,0,,{text}\n")
    path.write_text("".join(lines), encoding="utf-8")
    return True


def render_clip(source, destination, start, end, layout, captions_file, brand, logo, audio, cover_old_brand="none", clean_top=0, clean_bottom=0):
    args = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source)]
    if logo:
        args.extend(["-i", str(logo)])
    filters = []
    if any(type(pct) not in (int, float) or pct < 0 or pct > 35 for pct in (clean_top, clean_bottom)) or clean_top + clean_bottom > 50:
        raise ValueError("Crop percentages are out of bounds")
    video_input = "[0:v]"
    if clean_top or clean_bottom:
        # Remove selected top/bottom bands before recomposing the 9:16 output.
        # This crops pixels; it does not magically reconstruct a covered scene.
        kept = (100 - clean_top - clean_bottom) / 100
        top = clean_top / 100
        filters.append(f"[0:v]crop=iw:trunc(ih*{kept:.5f}/2)*2:0:trunc(ih*{top:.5f}/2)*2[cleaned_input]")
        video_input = "[cleaned_input]"
    if layout == "fill":
        filters.append(f"{video_input}scale=1080:1920:force_original_aspect_ratio=increase,"
                       "crop=1080:1920,setsar=1[canvas]")
    elif layout in ("left", "right"):
        # First isolate the chosen half of the ORIGINAL frame, including vertical
        # side-by-side edits. Merely shifting a 1080px crop did nothing on
        # existing 9:16 split-screen footage.
        x_pos = "0" if layout == "left" else "iw/2"
        filters.append(f"{video_input}crop=iw/2:ih:{x_pos}:0,"
                       "scale=1080:1920:force_original_aspect_ratio=increase,"
                       "crop=1080:1920,setsar=1[canvas]")
    else:
        filters.append(f"{video_input}split=2[bg][fg]")
        filters.append("[bg]scale=540:960:force_original_aspect_ratio=increase,"
                       "crop=540:960,boxblur=18:5,scale=1080:1920[back]")
        filters.append("[fg]scale=1080:1400:force_original_aspect_ratio=decrease,"
                       "setsar=1[front]")
        filters.append("[back][front]overlay=(W-w)/2:(H-h)/2[canvas]")
    current = "canvas"
    if cover_old_brand != "none":
        # Blur a small selected corner for footage the user is authorized to rebrand.
        # No attempt at identifying third-party marks or bypassing rights controls.
        positions = {
            "top_left": ("0", "0"),
            "top_right": ("iw-320", "0"),
            "bottom_left": ("0", "ih-195"),
            "bottom_right": ("iw-320", "ih-195"),
        }
        if cover_old_brand not in positions:
            raise ValueError("Unsupported old brand area")
        px, py = positions[cover_old_brand]
        ox = px.replace("iw", "W")
        oy = py.replace("ih", "H")
        filters.append(f"[{current}]split=2[coverbase][coverpatch]")
        filters.append(f"[coverpatch]crop=320:195:{px}:{py},boxblur=24:1[masked]")
        filters.append(f"[coverbase][masked]overlay={ox}:{oy}[covered]")
        current = "covered"
    if logo:
        filters.append("[1:v]format=rgba,scale=240:-2[logomark]")
        filters.append(f"[{current}][logomark]overlay=W-w-38:45:eof_action=repeat[branded]")
        current = "branded"
    elif brand:
        brand_path = destination.parent / "brand_text.txt"
        brand_path.write_text(brand, encoding="utf-8")
        font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        filters.append(
            f"[{current}]drawtext=fontfile={font}:textfile={brand_path}:"
            "fontsize=35:fontcolor=white@0.9:borderw=2:bordercolor=black:"
            "x=w-text_w-65:y=85[branded]"
        )
        current = "branded"
    if captions_file:
        filters.append(f"[{current}]ass=filename={captions_file}[final]")
    else:
        filters.append(f"[{current}]format=yuv420p[final]")
    args.extend(["-filter_complex", ";".join(filters), "-map", "[final]", "-map", "0:a?",
                 "-c:v", "libx264", "-preset", os.getenv("FFMPEG_PRESET", "veryfast"),
                 "-crf", os.getenv("FFMPEG_CRF", "22"), "-r", "30",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k"])
    if audio:
        args.extend(["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"])
    args.extend(["-movflags", "+faststart", "-max_muxing_queue_size", "1024",
                 str(destination)])
    command(args, timeout=1200)



def generate_thumbnail(clip_path: Path, output_path: Path, duration_seconds: float):
    """Make a real 9:16 JPEG cover from an exported Short.

    Called lazily so jobs created before the thumbnail feature also work.
    Never use source URLs or external image services.
    """
    if not clip_path.is_file():
        raise RuntimeError("Finished clip is unavailable.")
    position = min(15.0, max(0.6, float(duration_seconds) * 0.36))
    command([
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-ss", f"{position:.2f}", "-i", str(clip_path),
        "-frames:v", "1", "-vf", "scale=360:640:flags=lanczos,format=yuvj420p",
        "-q:v", "3", "-f", "image2", "-update", "1", str(output_path),
    ], timeout=40)
    if not output_path.is_file() or output_path.stat().st_size < 1000:
        raise RuntimeError("Unable to extract a video thumbnail.")

def make_shorts(job: dict, directory: Path, progress):
    source = Path(job["source"])
    out_dir = directory / "outputs"
    out_dir.mkdir(exist_ok=True)
    progress(7, "analyzing", "Checking video and audio...")
    duration, has_audio = probe_video(source)
    words, lang, warnings = [], "", []
    if has_audio and job["captions"] != "none":
        progress(12, "analyzing", "Transcribing speech and finding highlights...")
        try:
            words, lang = transcribe(source)
        except Exception as exc:
            warnings.append("Speech recognition unavailable: " + str(exc)[:190]
                            + ". Clips will export without captions.")
    elif has_audio:
        # Transcription is still needed to choose the best excerpts.
        progress(12, "analyzing", "Analyzing dialogue...")
        try:
            words, lang = transcribe(source)
        except Exception as exc:
            warnings.append("Dialogue analysis unavailable: " + str(exc)[:190]
                            + ". Clips use evenly spaced video sections.")
    else:
        warnings.append("No audio track found: clips will not have captions.")
    if not words:
        warnings.append("Highlights are timeline-based, not speech-ranked.")
    progress(38, "analyzing", "Ranking the strongest moments...")
    candidates = select_clips(words, duration, job["clip_count"], job["clip_length"])
    if not candidates:
        raise RuntimeError("Could not identify any usable clips.")
    clips = []
    logo = Path(job["logo"]) if job.get("logo") else None
    for index, selected in enumerate(candidates, 1):
        output_name = f"clip_{index:02}.mp4"
        destination = out_dir / output_name
        caption_file = directory / f"captions_{index:02}.ass"
        has_captions = write_captions(caption_file, words, selected["start"],
                                      selected["end"], job["captions"])
        progress(40 + round((index - 1) / len(candidates) * 53),
                 "rendering", f"Rendering vertical clip {index} of {len(candidates)}...")
        render_clip(source, destination, selected["start"], selected["end"],
                    job["layout"], caption_file if has_captions else None,
                    job["brand_name"], logo, has_audio,
                    job.get("cover_old_brand", "none"))
        technical = inspect_export(destination, selected["end"] - selected["start"], has_audio)
        clips.append({
            "technical": technical,
            "file": output_name, "title": clip_title(selected["words"], index),
            "start": selected["start"], "end": selected["end"],
            "length": round(selected["end"] - selected["start"], 1),
            "highlight_score": max(0, min(100, round(selected["score"]))),
            "captions": has_captions, "layout": job["layout"],
        })
    progress(96, "rendering", "Reviewing export quality and publishing risks...")
    review = risk_review(job, clips, words, lang, duration, has_audio)
    for clip, report in zip(clips, review["clip_reviews"]):
        clip["review"] = report
    warnings.append("Monetization is NOT certified: review rights, music, originality and platform eligibility before publishing.")
    progress(97, "rendering", "Packaging verified technical exports...")
    zip_name = "shorts_factory_export.zip"
    with zipfile.ZipFile(out_dir / zip_name, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=1) as bundle:
        for clip in clips:
            bundle.write(out_dir / clip["file"], arcname=clip["file"])
        bundle.writestr("clips.json", json.dumps({
            "source": job["filename"], "rights_selected": job["rights"],
            "channel": job.get("channel_name"), "brand_id": job.get("brand_id"),
            "previous_brand_cover": job.get("cover_old_brand", "none"),
            "clips": clips, "warnings": warnings, "review": review,
            "note": "Export does not establish copyright clearance or monetization eligibility.",
        }, indent=2))
    transcript = " ".join(w["text"] for w in words)
    return {"clips": clips, "warnings": warnings, "review": review, "zip_file": zip_name,
            "transcript": transcript[:20000]}
