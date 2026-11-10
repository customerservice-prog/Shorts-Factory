import hmac
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pipeline import make_shorts, generate_thumbnail
from branding import BRAND_DIR, PROFILE_ID, brand_path, list_brands, save_brand
from review import PLATFORMS, AUDIO_RIGHTS, ORIGINALITY

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
    age_limit = int(os.getenv("JOB_TTL_HOURS", "48")) * 3600
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
    return FileResponse(file_path, filename=filename, media_type="application/zip" if filename.endswith(".zip") else "video/mp4")


@app.get("/api/jobs/{job_id}/thumbnails/{clip_filename}")
def get_thumbnail(job_id: str, clip_filename: str, x_access_key: str | None = Header(default=None)):
    """Authenticated, cached posters for current AND previously finished jobs."""
    protect(x_access_key)
    job = read_job(job_id)
    if job.get("status") != "completed":
        raise HTTPException(409, "Video is not yet ready")
    if not re.fullmatch(r"clip_[0-9]{2}\.mp4", clip_filename):
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
    if job["status"] in ("queued", "analyzing", "rendering"):
        raise HTTPException(409, "Cannot delete an active processing job")
    shutil.rmtree(job_dir(job_id))
    return {"deleted": True}


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
