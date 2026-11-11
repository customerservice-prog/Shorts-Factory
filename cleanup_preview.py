"""Small, disposable before/after preview of selected source graphics.

Uses the same FFmpeg delogo interpolation as the full-length editor. A still
frame is an illustrative sample, not proof that overlays are removed for the
entire video, or that previously obscured pixels were genuinely reconstructed.
"""
from io import BytesIO
from pathlib import Path
import subprocess
import tempfile

from PIL import Image, ImageOps, UnidentifiedImageError

from overlay_cleanup import delogo_filters, normalize_masks
from brand_only import POSITIONS


def preview_frame(frame_bytes, masks, logo=None, *, position="top_right",
                  logo_size=24, logo_opacity=100,
                  crop_top=0, crop_bottom=0):
    masks = normalize_masks(masks)
    if not isinstance(frame_bytes, bytes) or not 0 < len(frame_bytes) <= 5_000_000:
        raise ValueError("Preview frame must be a JPEG or PNG smaller than 5 MB")
    if position not in POSITIONS:
        raise ValueError("Unsupported logo position")
    if type(logo_size) is not int or not 15 <= logo_size <= 40:
        raise ValueError("Logo size must be between 15 and 40 percent")
    if type(logo_opacity) is not int or not 60 <= logo_opacity <= 100:
        raise ValueError("Logo visibility must be between 60 and 100 percent")
    if not all(type(v) is int and 0 <= v <= 25 for v in (crop_top,crop_bottom)):
        raise ValueError("Invalid crop percentage")
    if crop_top+crop_bottom > 40:
        raise ValueError("Maximum combined crop is 40 percent")
    if logo is not None and (not Path(logo).is_file() or Path(logo).is_symlink()):
        raise ValueError("Selected channel logo is unavailable")
    try:
        with Image.open(BytesIO(frame_bytes)) as opened:
            if opened.format not in ("JPEG", "PNG"):
                raise ValueError("Upload a JPEG or PNG preview frame")
            if not (96 <= opened.width <= 2300 and 96 <= opened.height <= 2300):
                raise ValueError("Preview frame must be between 96 and 2300 pixels per side")
            if opened.width*opened.height > 3_300_000:
                raise ValueError("Preview frame is too large")
            frame=ImageOps.exif_transpose(opened).convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("The preview frame is unreadable") from exc

    width,height=frame.size
    filters=delogo_filters(masks,width,height)
    if crop_top or crop_bottom:
        filters.append(
            f"crop=iw:trunc(ih*{(100-crop_top-crop_bottom)/200:.6f})*2:"
            f"0:trunc(ih*{crop_top/200:.6f})*2"
        )
    if filters:
        with tempfile.TemporaryDirectory() as directory:
            before=Path(directory)/"before.png"
            after=Path(directory)/"processed.png"
            frame.save(before,format="PNG")
            cmd=["ffmpeg","-hide_banner","-nostdin","-loglevel","error","-y",
                 "-i",str(before),"-vf",",".join(filters),
                 "-frames:v","1","-update","1",str(after)]
            try:
                subprocess.run(cmd,check=True,capture_output=True,timeout=30)
                with Image.open(after) as edited:
                    frame=edited.convert("RGB")
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                raise ValueError("FFmpeg could not render the selected cleanup areas") from exc

    if logo is not None:
        with Image.open(logo) as original:
            overlay=original.convert("RGBA")
        logo_width=max(58,min(1200,int(width*logo_size/100)))
        logo_height=max(2,round(overlay.height*logo_width/overlay.width))
        overlay=overlay.resize((logo_width,logo_height),Image.Resampling.LANCZOS)
        if logo_opacity != 100:
            alpha=overlay.getchannel("A")
            overlay.putalpha(alpha.point(lambda a:round(a*logo_opacity/100)))
        margin=max(12,int(width*.035))
        x=margin if position.endswith("left") else frame.width-logo_width-margin
        y=margin if position.startswith("top") else frame.height-logo_height-margin
        composite=frame.convert("RGBA")
        composite.alpha_composite(overlay,(max(0,x),max(0,y)))
        frame=composite.convert("RGB")
    buf=BytesIO()
    frame.save(buf,format="JPEG",quality=88,subsampling=0,optimize=True)
    return buf.getvalue()
