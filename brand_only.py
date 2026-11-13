"""Fast full-length channel branding. Never trims time or slices videos into highlights."""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from overlay_cleanup import delogo_filters

POSITIONS = ("top_right", "top_left", "bottom_right", "bottom_left")
LONG_VIDEO_SECONDS = 15 * 60


def probe_media(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,duration,bit_rate",
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
            "audio_bitrate": int(audio["bit_rate"]) if audio and str(audio.get("bit_rate", "")).isdigit() else 0,
            "video_duration": float(video["duration"]) if video.get("duration") not in (None, "N/A") else None,
            "has_audio": audio is not None, "frame_rate": video.get("avg_frame_rate")}


def run_brand_encoder(command, duration, progress):
    timeout = max(1800, duration * 5)
    if progress is None:
        subprocess.run(command, check=True, timeout=timeout, capture_output=True)
        return

    with tempfile.TemporaryDirectory(prefix="shorts-render-") as scratch, tempfile.TemporaryFile() as errors:
        report = Path(scratch) / "progress"
        args = [*command[:1], "-progress", str(report), "-stats_period", "3", *command[1:]]
        with subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=errors) as encoder:
            deadline = time.monotonic() + timeout
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("The full-video render exceeded its processing time.")
                    try:
                        status = encoder.wait(timeout=min(6, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        if report.is_file():
                            with report.open("rb") as state:
                                state.seek(max(0, report.stat().st_size - 4096))
                                for line in reversed(state.read().splitlines()):
                                    if line.startswith(b"out_time_us="):
                                        timestamp = line.split(b"=", 1)[1]
                                        if timestamp.isdigit():
                                            progress(min(1, int(timestamp) / (duration * 1_000_000)))
                                            break
            finally:
                if encoder.poll() is None:
                    encoder.kill()
            if status:
                errors.seek(max(0, errors.tell() - 2000))
                message = errors.read().decode(errors="replace").strip()
                raise RuntimeError(message or "FFmpeg could not render the full video.")


def brand_full_video(source, destination, logo, *, position="top_right",
                     crop_top=0, crop_bottom=0, logo_size=30, logo_opacity=100,
                     subtitle_file=None, follow_banner=None, cleanup_regions=None,
                     max_output_bytes=None, progress=None):
    """Preserve the source's complete timeline, audio and native frame geometry.

    Optional edge crops remove portions of the picture, not the duration.
    No Whisper, scoring, content sampling, automatic watermark recognition,
    platform eligibility decisions or downloads from third-party services.
    """
    source, destination = Path(source), Path(destination)
    subtitle_file = Path(subtitle_file) if subtitle_file else None
    follow_banner = Path(follow_banner) if follow_banner else None
    logo = Path(logo) if logo is not None else None
    if (subtitle_file and not subtitle_file.is_file()) or (follow_banner and not follow_banner.is_file()):
        raise ValueError("Show-style graphic or subtitle file is missing")
    if not source.is_file() or (logo is not None and not logo.is_file()):
        raise ValueError("Source video or selected channel logo is unavailable")
    if type(logo_size) is not int or not 15 <= logo_size <= 40:
        raise ValueError("Choose a logo size from 15% to 40%")
    if type(logo_opacity) is not int or not 60 <= logo_opacity <= 100:
        raise ValueError("Choose watermark opacity from 60% to 100%")
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
    logo_width = max(58, min(1200, int(before["width"] * logo_size / 100)))
    margin = max(12, int(before["width"] * 0.035))
    x = str(margin) if position.endswith("left") else f"W-w-{margin}"
    y = str(margin) if position.startswith("top") else f"H-h-{margin}"
    crop_filter = ""
    if crop_top or crop_bottom:
        # Crop after decoding; keep the video and audio timelines untouched.
        crop_filter = (f"crop=iw:trunc(ih*{(100-crop_top-crop_bottom)/200:.6f})*2:"
                       f"0:trunc(ih*{crop_top/200:.6f})*2,")
    removal = delogo_filters(cleanup_regions or [], before["width"], before["height"])
    prefix = ",".join(removal + ([crop_filter.rstrip(",")] if crop_filter else []))
    if prefix:
        prefix += ","
    filters = [f"[0:v]{prefix}scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1[base]"]
    current = "base"
    inputs = ["-i", str(source)]
    next_input = 1
    if logo is not None:
        inputs += ["-i", str(logo)]
        filters += [f"[{next_input}:v]format=rgba,scale={logo_width}:-2:flags=lanczos,"
                    f"colorchannelmixer=aa={logo_opacity/100:.2f}[brand]",
                    f"[{current}][brand]overlay={x}:{y}:eof_action=repeat:format=auto[branded]"]
        current = "branded"
        next_input += 1
    if follow_banner is not None:
        inputs += ["-i", str(follow_banner)]
        banner_width = max(180, min(900, int(before["width"] * .52)))
        follow_x = "W-w-" + str(margin) if position.endswith("left") else str(margin)
        filters += [f"[{next_input}:v]format=rgba,scale={banner_width}:-2:flags=lanczos[cta]",
                    f"[{current}][cta]overlay={follow_x}:{margin}:"
                    "eof_action=repeat:enable='lt(t,3.0)'[with_cta]"]
        current = "with_cta"
    if subtitle_file is not None:
        # Files are generated under application-owned hex job directories.
        filters += [f"[{current}]ass=filename={subtitle_file}[captioned]"]
        current = "captioned"
    filters += [f"[{current}]format=yuv420p[outv]"]
    vf = ";".join(filters)
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        *inputs,
        "-filter_complex_threads", "2", "-filter_complex", vf, "-map", "[outv]", "-map", "0:a:0?",
        "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
        "-c:v", "libx264", "-preset", os.getenv("BRAND_FFMPEG_PRESET", "veryfast"),
        "-crf", os.getenv("BRAND_FFMPEG_CRF", "20"),
        "-pix_fmt", "yuv420p", "-threads", "2",
        "-c:a", "copy" if before["audio_codec"] == "aac" else "aac",
    ]
    if max_output_bytes is not None:
        audio_budget = max(before["audio_bitrate"], 256_000) if before["has_audio"] else 0
        video_budget = int((max_output_bytes * 7.6 / before["duration"] - audio_budget) / 1000)
        if video_budget < 500:
            raise ValueError("Download and remove finished exports to free space for a new MP4.")
        command += ["-maxrate", f"{video_budget}k", "-bufsize", f"{video_budget * 2}k",
                    "-fs", str(max_output_bytes)]
    command += ["-movflags", "+faststart", str(destination)]
    try:
        run_brand_encoder(command, before["duration"], progress)
        after = probe_media(destination)
        if after["video_codec"] != "h264" or not destination.is_file() or destination.stat().st_size < 2048:
            raise ValueError("Export codec or media file verification failed")
        if before["has_audio"] and not after["has_audio"]:
            raise ValueError("Audio was unexpectedly lost in the export")
        original_video_duration = before["video_duration"]
        video_duration_mismatch = (original_video_duration is not None and
                                   after["video_duration"] is not None and
                                   abs(after["video_duration"] - original_video_duration) > 2)
        if abs(after["duration"] - before["duration"]) > 2 or video_duration_mismatch:
            if max_output_bytes and destination.stat().st_size >= max_output_bytes * .98:
                raise ValueError("The full video needs more export space. Download and remove finished exports, then try again.")
            raise ValueError("Output duration did not preserve the source")
        return {
            "verified": True, "width": after["width"], "height": after["height"],
            "codec": "h264", "audio": after["has_audio"],
            "duration": round(after["duration"], 3),
            "original_duration": round(before["duration"], 3),
            "original_width": before["width"], "original_height": before["height"],
            "full_length": True, "captions_added": subtitle_file is not None,
            "follow_banner": follow_banner is not None,
            "cleanup_region_count": len(cleanup_regions or []),
            "cleanup_verified_visual": False,
            "logo_added": logo is not None, "logo_size": logo_size if logo else 0,
            "logo_opacity": logo_opacity if logo else 0,
            "size_bytes": destination.stat().st_size
        }
    except Exception:
        destination.unlink(missing_ok=True)
        raise
