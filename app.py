import asyncio
import errno
import hmac
import json
import logging
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
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from pipeline import make_shorts, generate_thumbnail, probe_video, render_clip
from branding import BRAND_DIR, PROFILE_ID, brand_path, list_brands, save_brand
from review import PLATFORMS, AUDIO_RIGHTS, ORIGINALITY, inspect_export, risk_review
from brand_only import brand_full_video, POSITIONS, probe_media
from show_style import create_subtitle_file, create_follow_banner
from overlay_cleanup import normalize_masks, suggest_text_masks
from cleanup_preview import preview_frame
from pipeline import transcribe

BASE = Path(os.getenv("DATA_DIR", "./data")).resolve()
BASE.mkdir(parents=True, exist_ok=True)
UPLOAD_WORKSPACE = tempfile.TemporaryDirectory(prefix="shorts-factory-inputs-")
UPLOAD_DIR = Path(UPLOAD_WORKSPACE.name)
STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_MB", "750")) * 1024 * 1024
PASSWORD = os.getenv("APP_PASSWORD", "")
HOSTED = bool(os.getenv("RAILWAY_PROJECT_ID") or os.getenv("RENDER") or os.getenv("HOSTED_MODE"))
EXECUTOR = ThreadPoolExecutor(max_workers=max(1, min(2, int(os.getenv("WORKERS", "1")))))
LOCK = threading.RLock()
THUMB_LOCK = threading.Lock()
ALLOWED_VIDEOS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
ALLOWED_LOGOS = {".png", ".webp", ".jpg", ".jpeg"}
ACTIVE_JOB_STATUSES = ("queued", "analyzing", "rendering")
LOGGER = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_app):
    await asyncio.to_thread(cleanup_stored_sources)
    yield


app = FastAPI(title="Shorts Factory", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


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
    public = {key: value for key, value in job.items() if key not in ("source", "logo")}
    source = job.get("source")
    public["source_available"] = bool(
        job.get("status") == "completed" and source and Path(source).is_file()
    )
    return public


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
            if (job.get("status") in ACTIVE_JOB_STATUSES or
                    job.get("reframe", {}).get("status") in ACTIVE_JOB_STATUSES):
                continue
            if time.time() - folder.stat().st_mtime > age_limit:
                shutil.rmtree(folder)
        except (OSError, ValueError):
            continue


def remove_source(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        LOGGER.warning("Could not remove a Shorts Factory source upload", exc_info=True)
        return False


def cleanup_stored_sources():
    """Release original uploads written to the volume by previous versions."""
    removed = 0
    for folder in iter_job_folders():
        try:
            job = read_job(folder.name)
        except HTTPException:
            continue
        updates = {}
        source = Path(job.get("source") or "")
        if source.suffix in ALLOWED_VIDEOS and source == folder / ("source" + source.suffix):
            if remove_source(source):
                removed += 1
                updates["source"] = None
        # Processing runs in this process. Jobs left active by a previous
        # container can no longer make progress.
        if job.get("status") in ACTIVE_JOB_STATUSES:
            updates.update(status="interrupted", note="Processing interrupted by a restart. Upload again.")
        reframe = job.get("reframe", {})
        if reframe.get("status") in ACTIVE_JOB_STATUSES:
            updates["reframe"] = {**reframe, "status": "failed",
                                  "error": "Processing interrupted by a restart. Try again."}
        if updates:
            try:
                update_job(folder.name, **updates)
            except OSError:
                LOGGER.warning("Could not update an interrupted Shorts Factory job", exc_info=True)
    if removed:
        LOGGER.info("Removed %d previously stored original uploads", removed)


def process(job_id: str):
    job = read_job(job_id)
    source = Path(job["source"])
    try:
        def progress(percent: int, status: str, note: str):
            update_job(job_id, progress=max(0, min(100, int(percent))), status=status, note=note)
        result = make_shorts(job, job_dir(job_id), progress)
        remove_source(source)
        update_job(job_id, source=None, progress=100, status="completed",
                   note="Your shorts are ready.", clips=result["clips"],
                   warnings=result["warnings"], transcript=result["transcript"],
                   zip_file=result["zip_file"], review=result["review"])
    except Exception as exc:
        remove_source(source)
        shutil.rmtree(job_dir(job_id) / "outputs", ignore_errors=True)
        update_job(job_id, source=None, status="failed", note=str(exc)[:500], progress=0)


def process_brand_only(job_id: str):
    """Video-only render: bypass transcription and highlight selection."""
    job = read_job(job_id)
    source = Path(job["source"])
    try:
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
        remove_source(source)
        update_job(job_id, source=None, status="completed", progress=100,
                   note="Full-length branded video ready.",
                   clips=[clip], review=review, zip_file=None,
                   cleanup_status=technical["source_cleanliness"],
                   transcript=" ".join(w["text"] for w in words)[:60000],
                   clip_length=round(before["duration"], 3),
                   warnings=processing_warnings + [
                       "Filming, adding subtitles, or applying a logo does not establish music rights or Facebook monetization eligibility."
                   ])
    except Exception as exc:
        remove_source(source)
        shutil.rmtree(job_dir(job_id) / "outputs", ignore_errors=True)
        update_job(job_id, source=None, status="failed", progress=0,
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
    keep_existing_text: bool = Form(False),
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
    if keep_existing_text:
        subtitle_mode = "none"
        follow_banner = auto_cleanup = False
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
    try:
        logo_path = brand_path(brand_id)
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(400, "Selected logo profile is unavailable.") from exc
    if logo_path is None:
        raise HTTPException(400, "Select a channel profile that has a logo.")
    # Check persistent space for the export. Uploaded originals live in /tmp.
    if shutil.disk_usage(BASE).free < 250 * 1024 * 1024:
        raise HTTPException(507, "Server storage is low. Download and delete exports you no longer need.")
    job_id = uuid.uuid4().hex[:20]
    folder = job_dir(job_id)
    folder.mkdir(mode=0o700, parents=True)
    source = None
    try:
        source = await save_video_upload(video)
        needed_space = max(180 * 1024 * 1024, source.stat().st_size * 2)
        if shutil.disk_usage(BASE).free < needed_space:
            raise HTTPException(507, "Not enough space to render this video. Download and delete exports you no longer need.")
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
            "keep_existing_text": keep_existing_text,
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
    except (Exception, asyncio.CancelledError):
        if source is not None:
            remove_source(source)
        shutil.rmtree(folder, ignore_errors=True)
        raise



@app.post("/api/jobs/{job_id}/clean-rebrand", status_code=202)
async def clean_rebrand_saved_source(
    job_id: str,
    video: UploadFile | None = File(None),
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
    fresh_id = uuid.uuid4().hex[:20]
    folder = job_dir(fresh_id)
    source = await source_for_edit(original, video)
    try:
        if shutil.disk_usage(BASE).free < max(180 * 1024 * 1024, source.stat().st_size):
            raise HTTPException(507, "Insufficient space to render the new copy.")
        folder.mkdir(parents=True, mode=0o700)
        logo = folder / "brand.png"
        shutil.copyfile(brand_path("bajo-mundo"), logo)
        now = int(time.time())
        record = {
            "id": fresh_id, "filename": original.get("filename", "video"),
            "created_at": now, "updated_at": now,
            "status": "queued", "progress": 1,
            "note": "Analyzing old graphics before creating separate Bajo Mundo version",
            "mode": "brand_only", "source_job_id": job_id,
            "source": str(source), "logo": str(logo),
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
        remove_source(source)
        shutil.rmtree(folder, ignore_errors=True)
        raise


@app.post("/api/cleanup/preview")
async def processed_cleanup_frame(
    frame: UploadFile = File(...),
    cleanup_regions: str = Form("[]"),
    cleanup_authorized: bool = Form(False),
    brand_id: str = Form("bajo-mundo"),
    logo_position: str = Form("top_right"),
    logo_size: int = Form(24),
    logo_opacity: int = Form(100),
    logo_already_embedded: bool = Form(False),
    crop_top: int = Form(0),
    crop_bottom: int = Form(0),
    x_access_key: str | None = Header(default=None),
):
    """Generate a real after-cleanup still from the selected browser video frame.

    Not an endorsement of source rights, quality or perfect reconstruction.
    Images are discarded after rendering; no uploaded video is stored.
    """
    protect(x_access_key)
    try:
        masks=normalize_masks(cleanup_regions)
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
    if masks and not cleanup_authorized:
        raise HTTPException(400,"Confirm you have permission to modify the selected graphics.")
    try:
        logo=None if logo_already_embedded else brand_path(brand_id)
        if logo is None and not logo_already_embedded:
            raise ValueError("No logo selected")
        data=await frame.read(5_000_001)
        result=await asyncio.to_thread(
            preview_frame,data,masks,logo,position=logo_position,
            logo_size=logo_size,logo_opacity=logo_opacity,
            crop_top=crop_top,crop_bottom=crop_bottom
        )
    except (ValueError,OSError,RuntimeError) as exc:
        raise HTTPException(422,str(exc)[:240]) from exc
    return Response(
        result,media_type="image/jpeg",
        headers={"Cache-Control":"no-store","X-Cleanup-Applied":str(len(masks)),
                 "X-Preview-Notice":"Single frame, not a full-video quality certification"}
    )


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
    source = await save_video_upload(video)
    try:
        info = await asyncio.to_thread(probe_media, source)
        return await asyncio.to_thread(suggest_text_masks, source, info["duration"])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise HTTPException(422, "Analysis unavailable; mark unwanted graphics manually.") from exc
    finally:
        remove_source(source)


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
            jobs.append(public_job(read_job(folder.name)))
        except HTTPException:
            pass
        if len(jobs) >= 100:
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
    except (Exception, asyncio.CancelledError):
        path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    if total == 0:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "The uploaded file was empty.")


async def save_video_upload(video: UploadFile) -> Path:
    suffix = Path(video.filename or "").suffix.lower()
    if suffix not in ALLOWED_VIDEOS:
        raise HTTPException(400, "Please upload MP4, MOV, MKV, WEBM or M4V.")
    if video.size is not None and video.size > MAX_UPLOAD:
        raise HTTPException(413, "File is larger than the allowed upload size.")
    if video.size is not None and shutil.disk_usage(UPLOAD_DIR).free < video.size:
        raise HTTPException(507, "Temporary upload space is full. Try again after processing finishes.")
    source = UPLOAD_DIR / (uuid.uuid4().hex + suffix)
    try:
        await save_upload(video, source, MAX_UPLOAD)
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise HTTPException(507, "Temporary upload space is full. Try again after processing finishes.") from exc
        raise
    return source


async def source_for_edit(job: dict, video: UploadFile | None) -> Path:
    if video is not None:
        return await save_video_upload(video)
    original = Path(job.get("source") or "")
    if original.suffix not in ALLOWED_VIDEOS or not original.is_file() or original.is_symlink():
        raise HTTPException(409, "Select the original video from your device and try again.")
    source = UPLOAD_DIR / (uuid.uuid4().hex + original.suffix)
    try:
        await asyncio.to_thread(shutil.copyfile, original, source)
    except (OSError, asyncio.CancelledError):
        remove_source(source)
        raise
    return source


@app.post("/api/jobs", status_code=202)
async def create_job(
    video: UploadFile = File(...),
    logo: UploadFile | None = File(default=None),
    clip_count: int = Form(default=5),
    clip_length: int = Form(default=45),
    layout: str = Form(default="fill"),
    captions: str = Form(default="pop"),
    keep_existing_text: bool = Form(False),
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
    logo_suffix = Path(logo.filename or "").suffix.lower() if logo and logo.filename else ""
    if logo_suffix and logo_suffix not in ALLOWED_LOGOS:
        raise HTTPException(400, "The logo must be PNG, WEBP, JPG, or JPEG.")
    clip_name = (video.filename or "Video")[:140]
    job_id = uuid.uuid4().hex[:20]
    directory = job_dir(job_id)
    directory.mkdir(parents=True)
    source_path = None
    try:
        source_path = await save_video_upload(video)
        logo_path = selected_logo
        if logo and logo_suffix:
            logo_path = directory / ("brand" + logo_suffix)
            await save_upload(logo, logo_path, 8 * 1024 * 1024)
        now = int(time.time())
        job = {
            "id": job_id, "filename": clip_name, "created_at": now, "updated_at": now,
            "status": "queued", "progress": 2, "note": "Waiting for video processor...",
            "clip_count": clip_count, "clip_length": clip_length, "layout": layout,
            "captions": "none" if keep_existing_text else captions,
            "brand_name": "" if keep_existing_text else brand_name.strip()[:55],
            "keep_existing_text": keep_existing_text,
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
    except (Exception, asyncio.CancelledError):
        if source_path is not None:
            remove_source(source_path)
        shutil.rmtree(directory, ignore_errors=True)
        raise



def _reframe_worker(job_id: str, source: Path, source_filename: str, focus: str, output_name: str):
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
        duration, has_audio = probe_video(source)
        if duration + .05 < float(original["end"]):
            raise RuntimeError("Select the original upload; this video ends before the selected clip.")
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
            # Keep only the MP4s. The archive is built outside /data
            # when the user actually requests a ZIP download.
            zip_name = latest.get("zip_file") or "shorts_factory_export.zip"
            latest.update({"clips": new_clips, "review": review, "zip_file": zip_name,
                           "reframe": {"status": "ready", "file": output_name, "focus": focus,
                                       "source_clip": source_filename},
                           "updated_at": int(time.time())})
            # ZIP can be regenerated from the saved clips on demand.
            (out_dir / zip_name).unlink(missing_ok=True)
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
    finally:
        remove_source(source)


@app.post("/api/jobs/{job_id}/reframe", status_code=202)
async def reframe_saved_clip(
    job_id: str,
    video: UploadFile | None = File(None),
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
    job = read_job(job_id)
    if job.get("status") != "completed":
        raise HTTPException(409, "Clip is not ready")
    source = await source_for_edit(job, video)
    try:
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
            # Pick a new filename while the job is locked.
            for version in range(1, 100):
                candidate = f"{clip_file[:-4]}_focus_{focus}_{version:02}.mp4"
                if not (job_dir(job_id) / "outputs" / candidate).exists():
                    break
            else:
                raise HTTPException(409, "Too many focused versions.")
            update_job(job_id, reframe={
                "status": "queued", "file": candidate,
                "source_clip": clip_file, "focus": focus
            })
            try:
                EXECUTOR.submit(_reframe_worker, job_id, source, clip_file, focus, candidate)
            except RuntimeError:
                update_job(job_id, reframe={"status": "failed", "error": "Try again after the server restarts."})
                raise HTTPException(503, "The video processor is restarting. Try again shortly.")
    except Exception:
        remove_source(source)
        raise
    return {"status": "queued", "file": candidate,
            "detail": "A new focused copy is being rendered; original clip is unchanged."}


def archive_on_demand(job: dict, output_dir: Path, filename: str):
    """Build a downloadable ZIP outside /data, without permanently duplicating MP4s."""
    fd, temporary = tempfile.mkstemp(prefix="shorts-download-", suffix=".zip")
    os.close(fd)
    path = Path(temporary)
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
            for clip in job.get("clips", []):
                name = clip.get("file", "")
                if not re.fullmatch(r"clip_[0-9]{2}(?:_focus_(?:left|right|fill)_[0-9]{2})?\.mp4", name):
                    raise HTTPException(409, "An export has an invalid filename.")
                video = output_dir / name
                if not video.is_file() or video.is_symlink():
                    raise HTTPException(409, "An exported clip is missing. Archive unavailable.")
                bundle.write(video, arcname=name)
            bundle.writestr("clips.json", json.dumps({
                "source": job.get("filename"),
                "rights_selected": job.get("rights"),
                "channel": job.get("channel_name"),
                "brand_id": job.get("brand_id"),
                "previous_brand_cover": job.get("cover_old_brand", "none"),
                "clips": job.get("clips", []),
                "warnings": job.get("warnings", []),
                "review": job.get("review"),
                "note": "Export does not establish copyright clearance or monetization eligibility.",
            }, ensure_ascii=False, indent=2))
        return FileResponse(path, filename=filename, media_type="application/zip",
                            background=BackgroundTask(lambda: path.unlink(missing_ok=True)))
    except Exception:
        path.unlink(missing_ok=True)
        raise


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
    if filename.endswith(".zip") and not file_path.is_file():
        return archive_on_demand(job, file_path.parent, filename)
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
    with LOCK:
        job = read_job(job_id)
        if job["status"] in ACTIVE_JOB_STATUSES or job.get("reframe", {}).get("status") in ACTIVE_JOB_STATUSES:
            raise HTTPException(409, "Cannot delete an active processing job")
        shutil.rmtree(job_dir(job_id))
    return {"deleted": True}


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
