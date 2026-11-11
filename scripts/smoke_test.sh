#!/usr/bin/env bash
set -euo pipefail

URL="http://127.0.0.1:18781"
TOKEN="smoke-test-only-not-for-production"
DIR="$(mktemp -d)"
NAME="shorts-factory-ci-smoke"
VOLUME="shorts-factory-ci-data"

cleanup() {
  docker logs "$NAME" --tail 100 2>/dev/null || true
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker volume rm "$VOLUME" >/dev/null 2>&1 || true
  rm -rf "$DIR"
}
trap cleanup EXIT

docker run -d --name "$NAME" -p 127.0.0.1:18781:8080 \
  -e APP_PASSWORD="$TOKEN" -e WHISPER_MODEL=tiny -e JOB_TTL_HOURS=48 \
  -v "$VOLUME":/data shorts-factory-test >/dev/null

ready=0
for _ in $(seq 1 45); do
  if curl -fsS --max-time 3 "$URL/healthz" > "$DIR/health.json" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 2
done
test "$ready" = "1"
# Confirm channel branding is private and built-in asset returns a real PNG.
test "$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/brands")" = "401"
curl -fsS -H "X-Access-Key: $TOKEN" "$URL/api/brands" > "$DIR/brands.json"
python - "$DIR/brands.json" <<'PY'
import json,sys
brands=json.load(open(sys.argv[1]))
assert brands["default"] == "bajo-mundo"
assert any(x["id"]=="bajo-mundo" for x in brands["brands"])
assert any(x["id"]=="bajo-mundo-studio" for x in brands["brands"])
PY
curl -fsS --max-time 60 -H "X-Access-Key: $TOKEN" "$URL/api/brands/bajo-mundo/logo" -o "$DIR/logo.png"
python - "$DIR/logo.png" <<'PY'
from PIL import Image
import sys
with Image.open(sys.argv[1]) as img:
    assert img.mode == "RGBA"
    assert img.size[0] > 100
    alpha = img.getchannel("A")
    assert alpha.getextrema()[0] == 0, "Logo must have transparent background"
PY

python - "$DIR/health.json" <<'PY'
import json, sys
health = json.load(open(sys.argv[1]))
assert health.get("status") == "ok"
assert health.get("ffmpeg") and health.get("ffprobe"), health
PY

curl -fsS --max-time 60 -H "X-Access-Key: $TOKEN" \
  "$URL/api/brands/bajo-mundo-studio/logo" -o "$DIR/studio-logo.png"
python - "$DIR/studio-logo.png" <<'PY'
from PIL import Image
import sys
with Image.open(sys.argv[1]) as picture:
    assert picture.mode == "RGBA"
    assert picture.size == (1000, 340)
    assert picture.getchannel("A").getextrema() == (0, 255)
PY

# A Railway persistent volume includes a protected lost+found directory.
# The jobs-list endpoint and cleanup must skip it, never return 500.
docker exec -u 0 "$NAME" sh -c 'mkdir -p /data/lost+found && chmod 000 /data/lost+found'
code="$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/jobs")"
test "$code" = "401"
code="$(curl -sS -H "X-Access-Key: $TOKEN" -o "$DIR/jobs-before.json" -w '%{http_code}' "$URL/api/jobs")"
test "$code" = "200"
python - "$DIR/jobs-before.json" <<'PY'
import json, sys
assert json.load(open(sys.argv[1])) == {"jobs": []}
PY

# Verify completed projects are kept by default even when housekeeping runs.
docker exec "$NAME" python -c '
import os, time, uuid
from pathlib import Path
import app
name = uuid.uuid4().hex[:20]
folder = app.BASE / name
folder.mkdir()
(folder / "job.json").write_text("{\"status\":\"completed\"}")
old = time.time() - 10 * 86400
os.utime(folder, (old, old))
app.cleanup_old_jobs()
assert folder.exists(), "Default retention must not remove saved exports"
(folder / "job.json").unlink()
folder.rmdir()
'

# Generate an entirely synthetic test clip; no copyrighted source material.
docker exec "$NAME" ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc2=size=320x180:rate=12:duration=4" \
  -c:v libx264 -pix_fmt yuv420p /tmp/smoke-source.mp4
docker cp "$NAME:/tmp/smoke-source.mp4" "$DIR/source.mp4"

curl -fsS --max-time 30 -H "X-Access-Key: $TOKEN" \
  -F "video=@$DIR/source.mp4" \
  -F "clip_count=1" -F "clip_length=20" \
  -F "layout=blur" -F "captions=none" \
  -F "brand_name=Test Studio" -F "rights=owned" \
  "$URL/api/jobs" > "$DIR/submission.json"

id="$(python - "$DIR/submission.json" <<'PY'
import json, sys
job = json.load(open(sys.argv[1]))
assert job["status"] == "queued", job
print(job["id"])
PY
)"

finished=0
for _ in $(seq 1 100); do
  curl -fsS --max-time 5 -H "X-Access-Key: $TOKEN" \
    "$URL/api/jobs/$id" > "$DIR/status.json"
  state="$(python - "$DIR/status.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))["status"])
PY
)"
  if [[ "$state" == "completed" ]]; then
    finished=1
    break
  elif [[ "$state" == "failed" || "$state" == "interrupted" ]]; then
    echo "Job failed:"
    cat "$DIR/status.json"
    exit 1
  fi
  sleep 3
done
test "$finished" = "1"

curl -fsS --max-time 40 -H "X-Access-Key: $TOKEN" \
  "$URL/api/jobs/$id/files/clip_01.mp4" -o "$DIR/clip_01.mp4"
test -s "$DIR/clip_01.mp4"

# Confirm private thumbnail generation works and returns real JPEG bytes.
code="$(curl -sS -o /dev/null -w '%{http_code}' \
  "$URL/api/jobs/$id/thumbnails/clip_01.mp4")"
test "$code" = "401"
curl -fsS --max-time 45 -H "X-Access-Key: $TOKEN" \
  "$URL/api/jobs/$id/thumbnails/clip_01.mp4" -o "$DIR/clip_01.jpg"
python - "$DIR/clip_01.jpg" <<'PY'
import sys
from pathlib import Path
payload = Path(sys.argv[1]).read_bytes()
assert len(payload) > 1000, len(payload)
assert payload[:3] == b"\xff\xd8\xff", "Thumbnail is not a JPEG"
print("A real rendered video thumbnail was generated.")
PY
curl -fsS --max-time 20 -H "X-Access-Key: $TOKEN" \
  "$URL/api/jobs/$id/thumbnails/clip_01.mp4" -o "$DIR/clip_01_cached.jpg"
cmp "$DIR/clip_01.jpg" "$DIR/clip_01_cached.jpg"
code="$(curl -sS -H "X-Access-Key: $TOKEN" -o /dev/null -w '%{http_code}' \
  "$URL/api/jobs/$id/thumbnails/clip_99.mp4")"
test "$code" = "404"
# Create a new focused version of a finished clip, without overwriting the old MP4.
cp "$DIR/clip_01.mp4" "$DIR/original-copy.mp4"
code="$(curl -sS -o /dev/null -w '%{http_code}' \
  -F "clip_file=clip_01.mp4" -F "focus=left" -F "rights_confirmed=true" \
  "$URL/api/jobs/$id/reframe")"
test "$code" = "401"
code="$(curl -sS -H "X-Access-Key: $TOKEN" -o /dev/null -w '%{http_code}' \
  -F "clip_file=clip_01.mp4" -F "focus=right" -F "rights_confirmed=false" \
  "$URL/api/jobs/$id/reframe")"
test "$code" = "400"
curl -fsS -H "X-Access-Key: $TOKEN" \
  -F "clip_file=clip_01.mp4" -F "focus=left" -F "rights_confirmed=true" \
  "$URL/api/jobs/$id/reframe" > "$DIR/reframe.json"
python - "$DIR/reframe.json" <<'PY'
import json,sys
response=json.load(open(sys.argv[1]))
assert response["status"] == "queued", response
assert response["file"] == "clip_01_focus_left_01.mp4", response
PY
focused=0
for _ in $(seq 1 90); do
  curl -fsS -H "X-Access-Key: $TOKEN" "$URL/api/jobs/$id" > "$DIR/status.json"
  state="$(python - "$DIR/status.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("reframe", {}).get("status", "pending"))
PY
)"
  if [[ "$state" == "ready" ]]; then
    focused=1
    break
  elif [[ "$state" == "failed" ]]; then
    cat "$DIR/status.json"
    exit 1
  fi
  sleep 2
done
test "$focused" = "1"
curl -fsS -H "X-Access-Key: $TOKEN" \
  "$URL/api/jobs/$id/files/clip_01.mp4" -o "$DIR/original-after.mp4"
cmp "$DIR/original-copy.mp4" "$DIR/original-after.mp4"
curl -fsS -H "X-Access-Key: $TOKEN" \
  "$URL/api/jobs/$id/files/clip_01_focus_left_01.mp4" -o "$DIR/clip_01_focus_left_01.mp4"
test -s "$DIR/clip_01_focus_left_01.mp4"
curl -fsS -H "X-Access-Key: $TOKEN" \
  "$URL/api/jobs/$id/thumbnails/clip_01_focus_left_01.mp4" \
  -o "$DIR/clip_focus_thumb.jpg"
test -s "$DIR/clip_focus_thumb.jpg"
python - "$DIR/status.json" <<'PY'
import json,sys
job=json.load(open(sys.argv[1]))
assert len(job["clips"]) == 2, job
assert job["clips"][0]["file"] == "clip_01.mp4"
assert job["clips"][1]["file"] == "clip_01_focus_left_01.mp4"
assert job["clips"][1]["technical"]["verified"] is True
PY

curl -fsS --max-time 40 -H "X-Access-Key: $TOKEN" \
  "$URL/api/jobs/$id/files/shorts_factory_export.zip" -o "$DIR/export.zip"
test -s "$DIR/export.zip"

python - "$DIR/export.zip" "$DIR/status.json" <<'PY'
import json, sys, zipfile
job = json.load(open(sys.argv[2]))
assert len(job["clips"]) == 2, job
assert job["review"]["monetization_guaranteed"] is False
assert job["clips"][0]["technical"]["verified"] is True
assert job["brand_id"] == "bajo-mundo"
with zipfile.ZipFile(sys.argv[1]) as zipped:
    assert "clip_01.mp4" in zipped.namelist()
    assert "clip_01_focus_left_01.mp4" in zipped.namelist()
    assert "clips.json" in zipped.namelist()
print("Authenticated upload, video processing, MP4 download and ZIP export passed.")
PY

docker exec "$NAME" ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height -of csv=p=0 \
  "/data/$id/outputs/clip_01.mp4" > "$DIR/dimensions.txt"
grep -Fxq "1080,1920" "$DIR/dimensions.txt"

# Full-length branding uses the SAME full synthetic source and does not cut scenes.
code="$(curl -sS -o /dev/null -w '%{http_code}' \
  -F "video=@$DIR/source.mp4" -F "rights_acknowledged=true" \
  "$URL/api/brand-only")"
test "$code" = "401"
code="$(curl -sS -H "X-Access-Key: $TOKEN" -o /dev/null -w '%{http_code}' \
  -F "video=@$DIR/source.mp4" \
  "$URL/api/brand-only")"
test "$code" = "400"
curl -fsS -H "X-Access-Key: $TOKEN" \
  -F "video=@$DIR/source.mp4" -F "brand_id=bajo-mundo" \
  -F "rights_acknowledged=true" -F "logo_position=top_right" \
  -F "logo_size=33" -F "logo_opacity=90" \
  -F "subtitle_mode=none" -F "subtitle_style=show" \
  -F "follow_banner=true" -F "performance_permission=unknown" \
  -F 'cleanup_regions=[{"x":5,"y":5,"w":20,"h":10}]' \
  -F "cleanup_authorized=true" \
  -F "rights=unknown" -F "audio_rights=unknown" \
  "$URL/api/brand-only" > "$DIR/brand-submit.json"
brand_id="$(python - "$DIR/brand-submit.json" <<'PY'
import json,sys
job=json.load(open(sys.argv[1]))
assert job["mode"] == "brand_only"
assert job["brand_id"] == "bajo-mundo"
assert job["logo_size"] == 33
assert job["logo_opacity"] == 90
print(job["id"])
PY
)"
full_done=0
for _ in $(seq 1 90); do
  curl -fsS -H "X-Access-Key: $TOKEN" "$URL/api/jobs/$brand_id" > "$DIR/brand-job.json"
  brand_state="$(python - "$DIR/brand-job.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))["status"])
PY
)"
  if [[ "$brand_state" == "completed" ]]; then
    full_done=1
    break
  elif [[ "$brand_state" == "failed" ]]; then
    cat "$DIR/brand-job.json"
    exit 1
  fi
  sleep 2
done
test "$full_done" = "1"
python - "$DIR/brand-job.json" <<'PY'
import json,sys
job=json.load(open(sys.argv[1]))
assert job["mode"] == "brand_only"
assert job["zip_file"] is None, "Do not duplicate MP4 storage for batch branding"
assert len(job["clips"]) == 1
assert job["clips"][0]["technical"]["verified"]
assert job["clips"][0]["technical"]["full_length"]
assert job["clips"][0]["technical"]["follow_banner"]
assert job["clips"][0]["technical"]["cleanup_region_count"] == 1
assert job["clips"][0]["technical"]["logo_added"]
assert job["clips"][0]["technical"]["logo_size"] == 33
assert job["clips"][0]["captions"] is False
assert job["review"]["monetization_guaranteed"] is False
assert job["clips"][0]["captions"] is False
assert abs(job["clips"][0]["length"] - 4) < 1.0
PY
curl -fsS -H "X-Access-Key: $TOKEN" \
  "$URL/api/jobs/$brand_id/files/clip_01.mp4" -o "$DIR/brand-full.mp4"
docker exec "$NAME" ffprobe -v error -show_entries \
  format=duration:stream=codec_type,width,height -of json \
  "/data/$brand_id/outputs/clip_01.mp4" > "$DIR/brand-probe.json"
python - "$DIR/brand-probe.json" <<'PY'
import json,sys
media=json.load(open(sys.argv[1]))
video=next(x for x in media["streams"] if x["codec_type"]=="video")
assert (video["width"],video["height"])==(320,180),video
assert abs(float(media["format"]["duration"])-4)<.6
PY

# OCR suggestions require authentication and never modify the source.
curl -fsS --max-time 90 -H "X-Access-Key: $TOKEN" \
  -F "video=@$DIR/source.mp4" "$URL/api/cleanup/suggest" > "$DIR/suggestions.json"
python - "$DIR/suggestions.json" <<'PY'
import json,sys
data=json.load(open(sys.argv[1]))
assert data["requires_confirmation"] is True
assert isinstance(data["regions"], list)
PY
echo "Shorts Factory container smoke test passed."
