import hmac
import json
import os
import zipfile
from copy import deepcopy
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pipeline import make_shorts, generate_thumbnail, probe_video, render_clip
from branding import BRAND_DIR, PROFILE_ID, brand_path, list_brands, save_brand
from review import PLATFORMS, AUDIO_RIGHTS, ORIGINALITY, inspect_export, risk_review
from brand_only import brand_full_video, POSITIONS, probe_media
from show_style import create_subtitle_file, create_follow_banner
from overlay_cleanup import normalize_masks, suggest_text_masks
from pipeline import transcribe

BASE = Path(os.getenv("DATA_DIR", "./data")).resolve()
BASE.mkdir(parents=True, exist_ok=True)
STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_MB", "750")) * 1024 * 1024
PASSWORD = os.getenv("APP_PASSWORD", "")
HOSTED = bool(os.getenv("RAILWAY_PROJECT_ID") or os.getenv("RENDER") or os.getenv("HOSTED_MODE"))
EXECUTOR = ThreadPoolExecutor(max_workers=max(1, min(2, int(os.getenv("WORKERS", "1")))))
LOCK = threading.RLock()
THUMB_LOCK = threading.Lock()
ALLOWED_VIDEOS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
ALLOWED_LOGOS = {".png", ".webp", ".jpg", ".jpeg"}
app = FastAPI(title="Shorts Factory", docs_url=None, redoc_url=None, openapi_url=None)


def protect(x_access_key: str | None):
    if HOSTED and not PASSWORD:
        raise HTTPException(503, "Administrator must set APP_PASSWORD before processing uploads.")
    if PASSWORD and not hmac.compare_digest(x_access_key or "", PASSWORD):
        raise HTTPException(401, "Incorrect access password.")


def job_dir(job_id: str) -> Path:
    if len(job_id) != 20 or not all(c in "0123456789abcdef" for c in job_id):
        raise HTTPException(404, "Job not found")
    return BASE / job_id


def record_path(job_id: str) -> Path:
    return job_dir(job_id) / "job.json"


def read_job(job_id: str) -> dict:
    try:
        return json.loads(record_path(job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(404, "Job not found")


def save_job(job: dict):
    with LOCK:
        target = record_path(job["id"])
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)


def update_job(job_id: str, **fields):
    with LOCK:
        job = read_job(job_id)
        job.update(fields)
        job["updated_at"] = int(time.time())
        save_job(job)


def public_job(job: dict) -> dict:
    return {key: value for key, value in job.items() if key not in ("source", "logo")}


def iter_job_folders(newest_first: bool = False) -> list[Path]:
    """Read only our own 20-character hex job folders.

    Railway persistent disks can contain system-owned folders such as
    lost+found, which are not readable by the unprivileged app process.
    Never probe inside them or let one unreadable folder break the API.
    """
    discovered = []
    for folder in BASE.iterdir():
        name = folder.name
        if len(name) != 20 or any(c not in "0123456789abcdef" for c in name):
            continue
        try:
            if folder.is_symlink() or not folder.is_dir():
                continue
            record = folder / "job.json"
            if not record.is_file():
                continue
            discovered.append((folder.stat().st_mtime, folder))
        except OSError:
            continue
    discovered.sort(key=lambda entry: entry[0], reverse=newest_first)
    return [folder for _, folder in discovered]


def cleanup_old_jobs():
    # Existing Railway services may still define JOB_TTL_HOURS=48.
    # NEVER delete finished projects on that setting alone.
    # Automatic cleanup requires a separate, explicit, opt-in switch.
    if os.getenv("AUTODELETE_FINISHED_JOBS", "false").lower() not in ("true", "1"):
        return
    ttl_hours = int(os.getenv("JOB_TTL_HOURS", "0"))
    if ttl_hours <= 0:
        return
    age_limit = ttl_hours * 3600
    for folder in iter_job_folders():
        try:
            job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
            if job.get("status") in ("queued", "analyzing", "rendering"):
                continue
            if time.time() - folder.stat().st_mtime > age_limit:
                shutil.rmtree(folder)
        except (OSError, ValueError):
            continue


def process(job_id: str):
    try:
        job = read_job(job_id)
        def progress(percent: int, status: str, note: str):
            update_job(job_id, progress=max(0, min(100, int(percent))), status=status, note=note)
        result = make_shorts(job, job_dir(job_id), progress)
        update_job(job_id, progress=100, status="completed",
                   note="Your shorts are ready.", clips=result["clips"],
                   warnings=result["warnings"], transcript=result["transcript"],
                   zip_file=result["zip_file"], review=result["review"])
    except Exception as exc:
        update_job(job_id, status="failed", note=str(exc)[:500], progress=0)


def process_brand_only(job_id: str):
    """Video-only render: bypass transcription and highlight selection."""
    try:
        job = read_job(job_id)
        update_job(job_id, status="rendering", progress=8,
                   note="Branding full video; keeping all original seconds and audio...")
        folder = job_dir(job_id)
        outputs = folder / "outputs"
        outputs.mkdir(exist_ok=True)
        target = outputs / "clip_01.mp4"
        before = probe_media(job["source"])
        auto_scan = None
        approved_masks = list(job.get("cleanup_regions", []))
        processing_warnings = []
        if job.get("auto_cleanup"):
            update_job(job_id, status="analyzing", progress=9,
                       note="Scanning original frames for old logos, text and branded graphics...")
            try:
                auto_scan = suggest_text_masks(job["source"], before["duration"])
                # Explicit consent enables conservative automatic text-region cleanup.
                # Static/PiP graphic hints remain review-only because backgrounds
                # can be mistaken for watermarks.
                if job.get("cleanup_authorized") and not approved_masks:
                    approved_masks = auto_scan.get("regions", [])[:6]
                if auto_scan.get("review_candidates"):
                    processing_warnings.append(
                        "Possible embedded graphics remain outside automatically detected text. Review the source and final video; a clean rebrand is NOT verified.")
                if auto_scan.get("possible_existing_bajo_logo"):
                    processing_warnings.append(
                        "The source may already have Bajo Mundo branding. Check for duplicate logos.")
                if auto_scan.get("cleanup_difficulty") == "heavy":
                    processing_warnings.append(
                        "This source has extensive on-screen text; automatic cleanup may visibly smear the picture.")
                if not approved_masks and auto_scan.get("suspected_prior_branding"):
                    processing_warnings.append(
                        "Old branding may still be visible. No confidently removable text area was found.")
                update_job(job_id, source_scan=auto_scan,
                           cleanup_regions_applied=approved_masks)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                processing_warnings.append(
                    "Automatic overlay analysis failed. Unwanted logos may remain; inspect before publishing.")
                update_job(job_id, source_scan={"error":"scan_unavailable"},
                           cleanup_regions_applied=approved_masks)
        captions_path = None
        follow_banner = None
        language = ""
        words = []
        if job.get("subtitle_mode") == "auto":
            if not before["has_audio"]:
                processing_warnings.append("Source has no audio; automatic subtitles were skipped.")
            else:
                update_job(job_id, status="analyzing", progress=13,
                           note="Detecting speech for synchronized captions (not changing footage)...")
                try:
                    words, language = transcribe(Path(job["source"]))
                    if words:
                        candidate = folder / "show_captions.ass"
                        count = create_subtitle_file(
                            candidate, words, before["width"], before["height"],
                            style=job.get("subtitle_style", "show")
                        )
                        if count:
                            captions_path = candidate
                        else:
                            processing_warnings.append("No usable speech timestamps; captions not added.")
                    else:
                        processing_warnings.append(
                            "No reliable spoken words detected; left original audio unchanged without invented captions."
                        )
                    if language and language != "es":
                        processing_warnings.append(
                            "Detected language: " + language +
                            ". Captions reflect detected speech; no Spanish translation was performed."
                        )
                except Exception:
                    processing_warnings.append(
                        "Automatic subtitles were unavailable. Video was still exported without invented captions."
                    )
        if job.get("follow_banner"):
            follow_banner = create_follow_banner(folder / "follow_banner.png")
        update_job(job_id, status="rendering", progress=46,
                   note="Applying original channel logo and optional show-style graphics...")
        technical = brand_full_video(
            job["source"], target, job["logo"],
            position=job["logo_position"],
            crop_top=job["crop_top"], crop_bottom=job["crop_bottom"],
            logo_size=job.get("logo_size", 30),
            logo_opacity=job.get("logo_opacity", 100),
            subtitle_file=captions_path, follow_banner=follow_banner,
            cleanup_regions=approved_masks
        )
        technical["source_cleanliness"] = (
            "needs_visual_review" if approved_masks or
            (auto_scan and auto_scan.get("suspected_prior_branding")) else
            "not_certified"
        )
        technical["auto_cleanup_requested"] = bool(job.get("auto_cleanup"))
        update_job(job_id, progress=93, note="Verifying full video duration and audio...")
        clip = {
            "file": "clip_01.mp4",
            "title": Path(job["filename"]).stem[:120],
            "start": 0, "end": round(before["duration"], 3),
            "length": round(before["duration"], 3),
            "highlight_score": 0, "captions": technical["captions_added"],
            "subtitle_language": language if technical["captions_added"] else None,
            "captions_need_review": technical["captions_added"],
            "layout": "original", "technical": technical,
            "full_length": True, "brand_id": job["brand_id"]
        }
        review = risk_review(job, [clip], words, language, before["duration"], before["has_audio"])
        clip["review"] = review["clip_reviews"][0]
        review["source_findings"].append({
            "level": "review", "title": "Own-camera footage is not automatically music-cleared",
            "detail": "Even when you filmed it, the performer, songwriters, publishers or labels may control music and performance rights. Written permission and platform review may still be required."
        })
        if job.get("performance_permission", "unknown") == "unknown":
            review["status"] = "high_risk"
            review["source_findings"].append({
                "level": "high", "title": "Artist/performance release not confirmed",
                "detail": "Get permission for commercial publishing and editing from the relevant performer/rightsholder; also verify recording and composition rights separately."
            })
        if auto_scan and auto_scan.get("suspected_prior_branding"):
            review["source_findings"].append({
                "level": "high", "title": "Possible previous branding detected",
                "detail": "Automatic detection cannot prove removal of all old logos, picture-in-picture boxes, or on-video text. Inspect the rendered result; if heavily covered, use unbranded camera footage."
            })
            review["status"] = "high_risk"
        if technical.get("cleanup_region_count", 0):
            review["source_findings"].append({
                "level": "review",
                "title": "Old overlays interpolated; check the full video",
                "detail": "Interpolated pixels can smear moving scenes or leave text remnants. Watch for distorted faces and rights/attribution obligations before publishing."
            })
        if technical["captions_added"]:
            review["source_findings"].append({
                "level": "review", "title": "Proofread auto captions before posting",
                "detail": "Speech recognition can mishear Dominican slang, names and music. Watch the whole video and correct mistakes before distribution."
            })
        update_job(job_id, status="completed", progress=100,
                   note="Full-length branded video ready. Originals preserved.",
                   clips=[clip], review=review, zip_file=None,
                   cleanup_status=technical["source_cleanliness"],
                   transcript=" ".join(w["text"] for w in words)[:60000],
                   clip_length=round(before["duration"], 3),
                   warnings=processing_warnings + [
                       "Filming, adding subtitles, or applying a logo does not establish music rights or Facebook monetization eligibility."
                   ])
    except Exception as exc:
        update_job(job_id, status="failed", progress=0,
                   note="Unable to brand the video: " + str(exc)[:350])


@app.post("/api/brand-only", status_code=202)
async def submit_brand_only(
    video: UploadFile = File(...),
    brand_id: str = Form("bajo-mundo"),
    logo_position: str = Form("top_right"),
    logo_size: int = Form(30),
    logo_opacity: int = Form(100),
    logo_already_embedded: bool = Form(False),
    subtitle_mode: str = Form("none"),
    subtitle_style: str = Form("show"),
    follow_banner: bool = Form(False),
    performance_permission: str = Form("unknown"),
    release_reference: str = Form(""),
    cleanup_regions: str = Form("[]"),
    cleanup_authorized: bool = Form(False),
    auto_cleanup: bool = Form(False),
    crop_top: int = Form(0),
    crop_bottom: int = Form(0),
    rights: str = Form("unknown"),
    audio_rights: str = Form("unknown"),
    originality: str = Form("unknown"),
    rights_acknowledged: bool = Form(False),
    x_access_key: str | None = Header(default=None),
):
    protect(x_access_key)
    if not rights_acknowledged:
        raise HTTPException(400, "Acknowledge that branding does not grant monetization or reposting rights.")
    if logo_position not in POSITIONS:
        raise HTTPException(400, "Choose a valid logo position.")
    if subtitle_mode not in ("none", "auto") or subtitle_style not in ("show", "clean"):
        raise HTTPException(400, "Unsupported show subtitle settings.")
    if performance_permission not in ("unknown", "written", "not_applicable"):
        raise HTTPException(400, "Invalid performance-rights selection.")
    if len(release_reference) > 300:
        raise HTTPException(400, "Release reference is too long.")
    try:
        masks = normalize_masks(cleanup_regions)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if (masks or auto_cleanup) and not cleanup_authorized:
        raise HTTPException(400, "Confirm your permission before automatic or manual graphic removal.")
    if not (15 <= logo_size <= 40 and 60 <= logo_opacity <= 100):
        raise HTTPException(400, "Watermark size or opacity is outside safe limits.")
    if not (0 <= crop_top <= 25 and 0 <= crop_bottom <= 25 and
            crop_top + crop_bottom <= 40):
        raise HTTPException(400, "Edge crop limit is 25% per side or 40% combined.")
    if rights not in ("unknown", "owned", "licensed", "commentary"):
        raise HTTPException(400, "Unsupported footage rights selection.")
    if audio_rights not in AUDIO_RIGHTS or originality not in ORIGINALITY:
        raise HTTPException(400, "Unsupported audio rights or originality selection.")
    suffix = Path(video.filename or "").suffix.lower()
    if suffix not in ALLOWED_VIDEOS:
        raise HTTPException(400, "Please upload MP4, MOV, MKV, WEBM or M4V.")
    try:
        logo_path = brand_path(brand_id)
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(400, "Selected logo profile is unavailable.") from exc
    if logo_path is None:
        raise HTTPException(400, "Select a channel profile that has a logo.")
    # Reserve storage conservatively for uncompressed source and rendered MP4.
    if shutil.disk_usage(BASE).free < 250 * 1024 * 1024:
        raise HTTPException(507, "Server storage is low. Download and archive exports first.")
    job_id = uuid.uuid4().hex[:20]
    folder = job_dir(job_id)
    folder.mkdir(mode=0o700, parents=True)
    try:
        source = folder / ("source" + suffix)
        await save_upload(video, source, MAX_UPLOAD)
        needed_space = max(180 * 1024 * 1024, source.stat().st_size * 2)
        if shutil.disk_usage(BASE).free < needed_space:
            raise HTTPException(507, "Not enough server space to safely render this video. Archive previous exports first.")
        logo_snapshot = folder / "brand.png" if not logo_already_embedded else None
        if logo_snapshot:
            shutil.copyfile(logo_path, logo_snapshot)
        now = int(time.time())
        channel = next((x["name"] for x in list_brands() if x["id"] == brand_id),
                       "Selected channel")
        record = {
            "id": job_id, "filename": (video.filename or "Video")[:140],
            "created_at": now, "updated_at": now, "status": "queued",
            "progress": 1, "note": "Queued for fast full-length branding",
            "mode": "brand_only", "clip_count": 1, "clip_length": 0,
            "layout": "original", "captions": "none", "brand_name": "",
            "rights": rights, "audio_rights": audio_rights,
            "originality": originality, "target_platform": "facebook_reels",
            "rights_evidence": "", "brand_id": brand_id, "channel_name": channel,
            "logo_position": logo_position,
            "logo_size": logo_size, "logo_opacity": logo_opacity,
            "logo_already_embedded": logo_already_embedded,
            "subtitle_mode": subtitle_mode, "subtitle_style": subtitle_style,
            "follow_banner": follow_banner,
            "performance_permission": performance_permission,
            "release_reference": release_reference.strip(),
            "cleanup_regions": masks,
            "cleanup_authorized": bool(cleanup_authorized),
            "auto_cleanup": bool(auto_cleanup),
            "crop_top": crop_top, "crop_bottom": crop_bottom,
            "source": str(source), "logo": str(logo_snapshot) if logo_snapshot else None,
            "review": None, "clips": [], "warnings": [],
            "zip_file": None, "transcript": ""
        }
        save_job(record)
        cleanup_old_jobs()
        EXECUTOR.submit(process_brand_only, job_id)
        return public_job(record)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise



@app.post("/api/jobs/{job_id}/clean-rebrand", status_code=202)
def clean_rebrand_saved_source(
    job_id: str,
    source_rights_confirmed: bool = Form(False),
    auto_cleanup: bool = Form(True),
    x_access_key: str | None = Header(default=None),
):
    """Create a NEW non-destructive Bajo Mundo TV export from an existing upload.

    Explicit editing rights confirmation required. A video with old baked-in
    graphics can still need manual cleanup; nothing is certified or published.
    """
    protect(x_access_key)
    if not source_rights_confirmed:
        raise HTTPException(400, "Confirm you have permission to edit and rebrand the source.")
    original = read_job(job_id)
    if original.get("status") != "completed":
        raise HTTPException(409, "The source project is not completed.")
    src = Path(original.get("source") or "")
    if not src.is_file() or src.is_symlink():
        raise HTTPException(409, "Saved original video unavailable. Re-upload the source.")
    if src.suffix.lower() not in ALLOWED_VIDEOS:
        raise HTTPException(400, "Unsupported original video format.")
    if shutil.disk_usage(BASE).free < max(180*1024*1024, src.stat().st_size):
        raise HTTPException(507, "Insufficient space to render the new copy.")
    fresh_id = uuid.uuid4().hex[:20]
    folder = job_dir(fresh_id)
    folder.mkdir(parents=True, mode=0o700)
    try:
        dst = folder / ("source" + src.suffix.lower())
        # Avoid duplicating the video on the same persistent volume.
        try:
            os.link(src, dst)
        except OSError:
            shutil.copyfile(src, dst)
        logo = folder / "brand.png"
        shutil.copyfile(brand_path("bajo-mundo"), logo)
        now = int(time.time())
        record = {
            "id": fresh_id, "filename": original.get("filename", "video"),
            "created_at": now, "updated_at": now,
            "status": "queued", "progress": 1,
            "note": "Analyzing old graphics before creating separate Bajo Mundo version",
            "mode": "brand_only", "source_job_id": job_id,
            "source": str(dst), "logo": str(logo),
            "logo_position": "top_right", "logo_size": 24,
            "logo_opacity": 100, "logo_already_embedded": False,
            "brand_id": "bajo-mundo", "channel_name": "Bajo Mundo TV",
            "subtitle_mode": "none", "subtitle_style": "show",
            "follow_banner": False, "performance_permission": "unknown",
            "rights": original.get("rights", "unknown"),
            "audio_rights": original.get("audio_rights", "unknown"),
            "originality": original.get("originality", "unknown"),
            "target_platform": "facebook_reels",
            "cleanup_regions": [], "cleanup_authorized": True,
            "auto_cleanup": bool(auto_cleanup),
            "crop_top": 0, "crop_bottom": 0,
            "clip_count": 1, "clips": [], "warnings": [],
            "review": None, "zip_file": None, "transcript": "",
            "brand_name": "", "layout": "original", "captions": "none"
        }
        save_job(record)
        EXECUTOR.submit(process_brand_only, fresh_id)
        return public_job(record)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


@app.post("/api/cleanup/suggest")
async def suggest_old_graphic_areas(
    video: UploadFile = File(...),
    x_access_key: str | None = Header(default=None),
):
    """Suggest likely text regions only; never remove without review."""
    protect(x_access_key)
    suffix = Path(video.filename or "").suffix.lower()
    if suffix not in ALLOWED_VIDEOS:
        raise HTTPException(400, "Choose MP4, MOV, MKV, WEBM or M4V.")
    with tempfile.TemporaryDirectory(dir=str(BASE)) as td:
        source = Path(td) / ("sample" + suffix)
        await save_upload(video, source, MAX_UPLOAD)
        try:
            info = probe_media(source)
            return suggest_text_masks(source, info["duration"])
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise HTTPException(422, "Analysis unavailable; mark unwanted graphics manually.") from exc


@app.get("/healthz")
def health():
    return {"status": "ok", "ffmpeg": shutil.which("ffmpeg") is not None,
            "ffprobe": shutil.which("ffprobe") is not None}


@app.get("/api/config")
def config():
    return {"protected": bool(PASSWORD) or HOSTED,
            "needs_setup": HOSTED and not bool(PASSWORD),
            "max_upload_mb": MAX_UPLOAD // 1024 // 1024,
            "engine": "Local Whisper + FFmpeg"}


@app.get("/api/brands")
def get_brands(x_access_key: str | None = Header(default=None)):
    protect(x_access_key)
    return {"brands": list_brands(), "default": "bajo-mundo"}


@app.get("/api/brands/{brand_id}/logo")
def brand_logo(brand_id: str, x_access_key: str | None = Header(default=None)):
    protect(x_access_key)
    try:
        path = brand_path(brand_id)
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(404, "Channel logo not available") from exc
    if not path:
        raise HTTPException(404, "No logo for this channel")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=1800",
                                 "X-Content-Type-Options": "nosniff"})


@app.post("/api/brands", status_code=201)
async def create_brand_profile(
    name: str = Form(...), logo: UploadFile = File(...),
    x_access_key: str | None = Header(default=None),
):
    protect(x_access_key)
    extension = Path(logo.filename or "").suffix.lower()
    if extension not in ALLOWED_LOGOS:
        raise HTTPException(400, "Upload a PNG, WEBP, JPG or JPEG channel logo.")
    with tempfile.TemporaryDirectory(dir=str(BASE)) as temporary:
        source = Path(temporary) / ("brand" + extension)
        await save_upload(logo, source, 8 * 1024 * 1024)
        try:
            return save_brand(name, source)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


@app.get("/api/jobs")
def list_jobs(x_access_key: str | None = Header(default=None)):
    protect(x_access_key)
    jobs = []
    for folder in iter_job_folders(newest_first=True):
        try:
            job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
            if job.get("status") in ("queued", "analyzing", "rendering") and job.get("updated_at", 0) < time.time() - 7200:
                job["status"] = "interrupted"
                job["note"] = "Processing interrupted by a restart. Upload again."
                save_job(job)
            jobs.append(public_job(job))
        except (OSError, ValueError):
            pass
        if len(jobs) >= 20:
            break
    return {"jobs": jobs}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, x_access_key: str | None = Header(default=None)):
    protect(x_access_key)
    return public_job(read_job(job_id))


async def save_upload(upload: UploadFile, path: Path, limit: int):
    total = 0
    try:
        with path.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise HTTPException(413, "File is larger than the allowed upload size.")
                output.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    if total == 0:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "The uploaded file was empty.")


@app.post("/api/jobs", status_code=202)
async def create_job(
    video: UploadFile = File(...),
    logo: UploadFile | None = File(default=None),
    clip_count: int = Form(default=5),
    clip_length: int = Form(default=45),
    layout: str = Form(default="fill"),
    captions: str = Form(default="pop"),
    brand_name: str = Form(default=""),
    brand_id: str = Form(default="bajo-mundo"),
    cover_old_brand: str = Form(default="none"),
    rights: str = Form(default="unknown"),
    audio_rights: str = Form(default="unknown"),
    originality: str = Form(default="unknown"),
    target_platform: str = Form(default="youtube_shorts"),
    rights_evidence: str = Form(default=""),
    x_access_key: str | None = Header(default=None),
):
    protect(x_access_key)
    if clip_count not in range(1, 11):
        raise HTTPException(400, "Choose 1 to 10 clips.")
    if clip_length not in (20, 30, 45, 60, 90):
        raise HTTPException(400, "Unsupported clip length.")
    if layout not in ("blur", "fill", "left", "right"):
        raise HTTPException(400, "Unsupported video layout.")
    if captions not in ("pop", "classic", "none"):
        raise HTTPException(400, "Unsupported caption style.")
    if rights not in ("owned", "licensed", "commentary", "unknown"):
        raise HTTPException(400, "Unsupported source-rights selection.")
    if audio_rights not in AUDIO_RIGHTS or originality not in ORIGINALITY or target_platform not in PLATFORMS:
        raise HTTPException(400, "Unsupported rights or platform review settings.")
    if cover_old_brand not in ("none", "top_left", "top_right", "bottom_left", "bottom_right"):
        raise HTTPException(400, "Unsupported previous logo location.")
    if cover_old_brand != "none" and rights not in ("owned", "licensed"):
        raise HTTPException(400, "Replacing previous branding requires rights to this footage.")
    if len(rights_evidence) > 500:
        raise HTTPException(400, "The rights reference is too long.")
    if target_platform == "tiktok_rewards" and clip_length < 90:
        clip_length = 90  # Automatic target to exceed the 60-second threshold.
    try:
        selected_logo = brand_path(brand_id)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(400, "Channel brand unavailable. Choose another logo.") from exc
    selected_channel = next((brand["name"] for brand in list_brands()
                             if brand["id"] == brand_id), "Custom channel")
    suffix = Path(video.filename or "").suffix.lower()
    if suffix not in ALLOWED_VIDEOS:
        raise HTTPException(400, "Please upload an MP4, MOV, MKV, WEBM or M4V video.")
    logo_suffix = Path(logo.filename or "").suffix.lower() if logo and logo.filename else ""
    if logo_suffix and logo_suffix not in ALLOWED_LOGOS:
        raise HTTPException(400, "The logo must be PNG, WEBP, JPG, or JPEG.")
    clip_name = (video.filename or "Video")[:140]
    job_id = uuid.uuid4().hex[:20]
    directory = job_dir(job_id)
    directory.mkdir(parents=True)
    try:
        source_path = directory / ("source" + suffix)
        await save_upload(video, source_path, MAX_UPLOAD)
        logo_path = selected_logo
        if logo and logo_suffix:
            logo_path = directory / ("brand" + logo_suffix)
            await save_upload(logo, logo_path, 8 * 1024 * 1024)
        now = int(time.time())
        job = {
            "id": job_id, "filename": clip_name, "created_at": now, "updated_at": now,
            "status": "queued", "progress": 2, "note": "Waiting for video processor...",
            "clip_count": clip_count, "clip_length": clip_length, "layout": layout,
            "captions": captions, "brand_name": brand_name.strip()[:55],
            "rights": rights, "source": str(source_path),
            "logo": str(logo_path) if logo_path else None,
            "brand_id": brand_id if not (logo and logo_suffix) else "custom-upload",
            "cover_old_brand": cover_old_brand,
            "channel_name": selected_channel if not (logo and logo_suffix) else "Uploaded channel logo",
            "audio_rights": audio_rights, "originality": originality,
            "target_platform": target_platform, "rights_evidence": rights_evidence.strip(),
            "review": None, "clips": [],
            "warnings": [], "zip_file": None, "transcript": "",
        }
        save_job(job)
        cleanup_old_jobs()
        EXECUTOR.submit(process, job_id)
        return public_job(job)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise



def _reframe_worker(job_id: str, source_filename: str, focus: str, output_name: str):
    """Non-destructive edit of an existing saved clip from its original footage."""
    folder = job_dir(job_id)
    out_dir = folder / "outputs"
    temporary = out_dir / ("_pending_" + output_name)
    destination = out_dir / output_name
    try:
        update_job(job_id, reframe={"status": "rendering", "file": output_name,
                                    "focus": focus, "source_clip": source_filename})
        job = read_job(job_id)
        original = next(c for c in job["clips"] if c["file"] == source_filename)
        source = Path(job["source"])
        if not source.is_file():
            raise RuntimeError("Original uploaded video is no longer available")
        duration, has_audio = probe_video(source)
        captions_name = "captions_" + source_filename[5:7] + ".ass"
        captions_path = folder / captions_name
        subtitle = captions_path if original.get("captions") and captions_path.is_file() else None
        logo = Path(job["logo"]) if job.get("logo") else None
        if logo is not None and not logo.is_file():
            raise RuntimeError("The original selected logo is no longer available")
        render_clip(source, temporary,
                    float(original["start"]), float(original["end"]), focus,
                    subtitle, job.get("brand_name", ""), logo, has_audio,
                    job.get("cover_old_brand", "none"))
        technical = inspect_export(temporary,
                                   float(original["end"]) - float(original["start"]),
                                   has_audio)
        temporary.replace(destination)
        with LOCK:
            latest = read_job(job_id)
            old_clip = next(c for c in latest["clips"] if c["file"] == source_filename)
            variant = {key: deepcopy(value) for key, value in old_clip.items()
                       if key not in ("file", "technical", "review")}
            variant.update({"file": output_name, "technical": technical,
                            "layout": focus, "source_clip": source_filename,
                            "title": old_clip.get("title", "Highlight") + " · " + focus + " focus"})
            if "review" not in latest or not latest["review"]:
                latest["review"] = risk_review(
                    latest, latest["clips"], [], "", duration, has_audio)
            # Keep original per-clip audit details, add a real manual composition check.
            report = deepcopy(old_clip.get("review") or
                              (latest["review"].get("clip_reviews") or [{}])[0])
            report["file"] = output_name
            report["clip"] = len(latest["clips"]) + 1
            report["status"] = "manual_review_required"
            report.setdefault("checks", []).append({
                "level": "review", "title": "Check the new focus and embedded overlays",
                "detail": "This crop changes the visible composition, not source rights. "
                          "Watch the complete result for cut-off faces, existing captions "
                          "and channel marks before publishing."
            })
            variant["review"] = report
            new_clips = latest["clips"] + [variant]
            review = latest["review"]
            review.setdefault("clip_reviews", []).append(report)
            # Rebuild the ZIP without modifying any original MP4.
            zip_name = latest.get("zip_file") or "shorts_factory_export.zip"
            zip_pending = out_dir / "_pending_export.zip"
            with zipfile.ZipFile(zip_pending, "w", zipfile.ZIP_STORED) as archive:
                for clip in new_clips:
                    archive.write(out_dir / clip["file"], arcname=clip["file"])
                archive.writestr("clips.json", json.dumps({
                    "source": latest.get("filename"),
                    "clips": new_clips, "review": review,
                    "note": "Exports require human rights and monetization review."
                }, ensure_ascii=False, indent=2))
            zip_pending.replace(out_dir / zip_name)
            latest.update({"clips": new_clips, "review": review, "zip_file": zip_name,
                           "reframe": {"status": "ready", "file": output_name, "focus": focus,
                                       "source_clip": source_filename},
                           "updated_at": int(time.time())})
            save_job(latest)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        with LOCK:
            try:
                update_job(job_id, reframe={
                    "status": "failed", "file": output_name, "focus": focus,
                    "source_clip": source_filename,
                    "error": "Could not create this focused version. " + str(exc)[:180]
                })
            except HTTPException:
                pass


@app.post("/api/jobs/{job_id}/reframe", status_code=202)
def reframe_saved_clip(
    job_id: str,
    clip_file: str = Form(...),
    focus: str = Form(...),
    rights_confirmed: bool = Form(False),
    x_access_key: str | None = Header(default=None),
):
    protect(x_access_key)
    if not rights_confirmed:
        raise HTTPException(400, "Confirm you own or are permitted to re-edit this footage.")
    if focus not in ("left", "right", "fill"):
        raise HTTPException(400, "Choose left, right or full-frame focus.")
    if not re.fullmatch(r"clip_[0-9]{2}\.mp4", clip_file):
        raise HTTPException(400, "Reframe one original clip at a time.")
    with LOCK:
        job = read_job(job_id)
        if job.get("status") != "completed":
            raise HTTPException(409, "Clip is not ready")
        if not any(c.get("file") == clip_file for c in job.get("clips", [])):
            raise HTTPException(404, "Original clip not found")
        if job.get("reframe", {}).get("status") in ("queued", "rendering"):
            raise HTTPException(409, "A focused version is already processing.")
        if len(job.get("clips", [])) >= 20:
            raise HTTPException(409, "Maximum of 20 versions in this project.")
        if not Path(job.get("source", "")).is_file():
            raise HTTPException(409, "The original source is unavailable; upload again.")
        # One immutable filename per operation; existing results are never overwritten.
        version = 1
        while True:
            candidate = f"{clip_file[:-4]}_focus_{focus}_{version:02}.mp4"
            if not (job_dir(job_id) / "outputs" / candidate).exists():
                break
            version += 1
            if version > 99:
                raise HTTPException(409, "Too many focused versions.")
        update_job(job_id, reframe={
            "status": "queued", "file": candidate,
            "source_clip": clip_file, "focus": focus
        })
    EXECUTOR.submit(_reframe_worker, job_id, clip_file, focus, candidate)
    return {"status": "queued", "file": candidate,
            "detail": "A new focused copy is being rendered; original clip is unchanged."}


@app.get("/api/jobs/{job_id}/files/{filename}")
def get_output(job_id: str, filename: str, x_access_key: str | None = Header(default=None)):
    protect(x_access_key)
    job = read_job(job_id)
    if job.get("status") != "completed":
        raise HTTPException(409, "Export is not ready")
    allowed = {c["file"] for c in job.get("clips", [])} | {job.get("zip_file")}
    if filename not in allowed or not filename:
        raise HTTPException(404, "File not found")
    file_path = job_dir(job_id) / "outputs" / filename
    if not file_path.is_file():
        raise HTTPException(404, "File not found")
    download_name = filename
    if job.get("mode") == "brand_only" and filename == "clip_01.mp4":
        base_name = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(job.get("filename", "video")).stem)
        channel_name = re.sub(r"[^A-Za-z0-9_-]+", "_", job.get("channel_name", "Bajo_Mundo_TV"))
        download_name = (base_name[:85].strip("_") or "video") + "_" + (channel_name[:35].strip("_") or "Branded") + ".mp4"
    return FileResponse(file_path, filename=download_name,
                        media_type="application/zip" if filename.endswith(".zip") else "video/mp4")


@app.get("/api/jobs/{job_id}/thumbnails/{clip_filename}")
def get_thumbnail(job_id: str, clip_filename: str, x_access_key: str | None = Header(default=None)):
    """Authenticated, cached posters for current AND previously finished jobs."""
    protect(x_access_key)
    job = read_job(job_id)
    if job.get("status") != "completed":
        raise HTTPException(409, "Video is not yet ready")
    if not re.fullmatch(r"clip_[0-9]{2}(?:_focus_(?:left|right|fill)_[0-9]{2})?\.mp4", clip_filename):
        raise HTTPException(404, "Clip not found")
    clip = next((c for c in job.get("clips", []) if c.get("file") == clip_filename), None)
    if clip is None:
        raise HTTPException(404, "Clip not found")
    output_dir = job_dir(job_id) / "outputs"
    video = output_dir / clip_filename
    cover = output_dir / clip_filename.replace(".mp4", ".jpg")
    if not video.is_file():
        raise HTTPException(404, "Clip file is missing")
    if not cover.is_file():
        with THUMB_LOCK:
            if not cover.is_file():
                temporary = output_dir / (cover.stem + "_pending.jpg")
                try:
                    generate_thumbnail(video, temporary, float(clip.get("length") or 20))
                    temporary.replace(cover)
                except (OSError, RuntimeError, ValueError) as exc:
                    temporary.unlink(missing_ok=True)
                    raise HTTPException(422, "Thumbnail preview could not be generated") from exc
    return FileResponse(cover, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600",
                                 "X-Content-Type-Options": "nosniff"})


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, x_access_key: str | None = Header(default=None)):
    protect(x_access_key)
    job = read_job(job_id)
    if job["status"] in ("queued", "analyzing", "rendering") or job.get("reframe", {}).get("status") in ("queued", "rendering"):
        raise HTTPException(409, "Cannot delete an active processing job")
    shutil.rmtree(job_dir(job_id))
    return {"deleted": True}


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
