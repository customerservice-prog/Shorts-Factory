"""Fast full-length channel branding. Never trims time or slices videos into highlights."""
import json
import os
import subprocess
from pathlib import Path

POSITIONS = ("top_right", "top_left", "bottom_right", "bottom_left")


def probe_media(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=35)
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("The source does not contain a video track")
    duration = float(data.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise ValueError("Could not determine source duration")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {"width": int(video["width"]), "height": int(video["height"]),
            "duration": duration, "video_codec": video.get("codec_name"),
            "audio_codec": audio.get("codec_name") if audio else None,
            "has_audio": audio is not None, "frame_rate": video.get("avg_frame_rate")}


def brand_full_video(source, destination, logo, *, position="top_right",
                     crop_top=0, crop_bottom=0):
    """Preserve the source's complete timeline, audio and native frame geometry.

    Optional edge crops remove portions of the picture, not the duration.
    No Whisper, scoring, content sampling, automatic watermark recognition,
    platform eligibility decisions or downloads from third-party services.
    """
    source, destination, logo = Path(source), Path(destination), Path(logo)
    if not source.is_file() or not logo.is_file():
        raise ValueError("Source video or selected channel logo is unavailable")
    if position not in POSITIONS:
        raise ValueError("Unsupported channel logo placement")
    if (not all(type(v) is int and 0 <= v <= 25 for v in (crop_top, crop_bottom))
            or crop_top + crop_bottom > 40):
        raise ValueError("Edge crop may remove at most 25% per side and 40% total")
    if source.resolve() == destination.resolve():
        raise ValueError("Never overwrite source footage")
    before = probe_media(source)
    if before["width"] < 96 or before["height"] < 96:
        raise ValueError("Source video dimensions are too small")
    destination.parent.mkdir(parents=True, exist_ok=True)
    logo_width = max(58, min(360, int(before["width"] * 0.22)))
    margin = max(9, int(before["width"] * 0.026))
    x = str(margin) if position.endswith("left") else f"W-w-{margin}"
    y = str(margin) if position.startswith("top") else f"H-h-{margin}"
    crop_filter = ""
    if crop_top or crop_bottom:
        # Crop after decoding; keep the video and audio timelines untouched.
        crop_filter = (f"crop=iw:trunc(ih*{(100-crop_top-crop_bottom)/200:.6f})*2:"
                       f"0:trunc(ih*{crop_top/200:.6f})*2,")
    vf = (
        f"[0:v]{crop_filter}scale=trunc(iw/2)*2:trunc(ih/2)*2,"
        "setsar=1[base];"
        f"[1:v]format=rgba,scale={logo_width}:-2:flags=lanczos[brand];"
        f"[base][brand]overlay={x}:{y}:eof_action=repeat:format=auto,"
        "format=yuv420p[outv]"
    )
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(source), "-i", str(logo),
        "-filter_complex", vf, "-map", "[outv]", "-map", "0:a:0?",
        "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
        "-c:v", "libx264", "-preset", os.getenv("BRAND_FFMPEG_PRESET", "veryfast"),
        "-crf", os.getenv("BRAND_FFMPEG_CRF", "20"),
        "-pix_fmt", "yuv420p", "-threads", "2",
        "-c:a", "copy" if before["audio_codec"] == "aac" else "aac",
        "-movflags", "+faststart", str(destination)
    ]
    try:
        subprocess.run(command, check=True, timeout=1800, capture_output=True)
        after = probe_media(destination)
        if after["video_codec"] != "h264" or not destination.is_file() or destination.stat().st_size < 2048:
            raise ValueError("Export codec or media file verification failed")
        if before["has_audio"] and not after["has_audio"]:
            raise ValueError("Audio was unexpectedly lost in the export")
        if abs(after["duration"] - before["duration"]) > max(0.5, before["duration"] * 0.02):
            raise ValueError("Output duration did not preserve the source")
        return {
            "verified": True, "width": after["width"], "height": after["height"],
            "codec": "h264", "audio": after["has_audio"],
            "duration": round(after["duration"], 3),
            "original_duration": round(before["duration"], 3),
            "original_width": before["width"], "original_height": before["height"],
            "full_length": True, "captions_added": False,
            "size_bytes": destination.stat().st_size
        }
    except Exception:
        destination.unlink(missing_ok=True)
        raise
