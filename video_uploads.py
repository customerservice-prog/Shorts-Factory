"""Temporary, sequential video uploads for connections with request time limits."""
import asyncio
import errno
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException, Request

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
CHUNK_SIZE = 8 * 1024 * 1024
UPLOAD_IDLE_SECONDS = 60 * 60
WORKSPACE = tempfile.TemporaryDirectory(prefix="shorts-factory-inputs-")
DIRECTORY = Path(WORKSPACE.name)


@dataclass
class PendingUpload:
    name: str
    size: int
    path: Path
    offset: int = 0
    last_activity: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


pending: dict[str, PendingUpload] = {}


def video_suffix(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        raise HTTPException(400, "Please upload MP4, MOV, MKV, WEBM or M4V.")
    return suffix


def temporary_space_error() -> HTTPException:
    return HTTPException(507, "Temporary upload space is full. Try again after processing finishes.")


def begin(name: str, size: int, limit: int) -> str:
    suffix = video_suffix(name)
    if not 0 < size <= limit:
        raise HTTPException(413, f"Choose a video up to {limit // (1024 * 1024):,} MB.")
    reserved = sum(upload.size - upload.offset for upload in pending.values())
    if shutil.disk_usage(DIRECTORY).free < size + reserved:
        raise temporary_space_error()
    upload_id = uuid.uuid4().hex
    path = DIRECTORY / (upload_id + suffix)
    try:
        path.touch(mode=0o600, exist_ok=False)
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise temporary_space_error() from exc
        raise
    pending[upload_id] = PendingUpload(name, size, path)
    return upload_id


def get(upload_id: str) -> PendingUpload:
    upload = pending.get(upload_id)
    if upload is None:
        raise HTTPException(404, "This upload expired. Select the video and upload it again.")
    return upload


async def write(upload_id: str, offset: int, request: Request) -> int:
    upload = get(upload_id)
    async with upload.lock:
        get(upload_id)
        if not 0 <= offset <= upload.offset or (offset != upload.size and offset % CHUNK_SIZE):
            raise HTTPException(409, "This upload part arrived out of order.")
        upload.last_activity = time.monotonic()
        if offset < upload.offset or offset == upload.size:
            return upload.offset  # Retrying a part whose response was lost is safe.

        expected = min(CHUNK_SIZE, upload.size - offset)
        received = 0
        try:
            with upload.path.open("r+b") as output:
                output.seek(offset)
                try:
                    async for part in request.stream():
                        received += len(part)
                        if received > expected:
                            raise HTTPException(413, "Upload part is too large.")
                        output.write(part)
                    if received != expected:
                        raise HTTPException(400, "Upload part arrived incomplete.")
                    output.flush()
                except (Exception, asyncio.CancelledError):
                    output.truncate(offset)
                    raise
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                raise temporary_space_error() from exc
            raise

        upload.offset += received
        upload.last_activity = time.monotonic()
        return upload.offset


async def take(upload_id: str) -> tuple[str, Path]:
    upload = get(upload_id)
    async with upload.lock:
        get(upload_id)
        if upload.offset != upload.size:
            raise HTTPException(409, "Finish uploading the video before starting processing.")
        del pending[upload_id]
        return upload.name, upload.path


async def discard(upload_id: str):
    upload = pending.get(upload_id)
    if upload is None:
        return
    async with upload.lock:
        if upload_id in pending:
            upload.path.unlink(missing_ok=True)
            del pending[upload_id]


async def expire_idle_uploads():
    while True:
        await asyncio.sleep(60)
        cutoff = time.monotonic() - UPLOAD_IDLE_SECONDS
        for upload_id, upload in tuple(pending.items()):
            if not upload.lock.locked() and upload.last_activity < cutoff:
                try:
                    await discard(upload_id)
                except OSError:
                    upload.last_activity = time.monotonic()
