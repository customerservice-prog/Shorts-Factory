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
  -e APP_PASSWORD="$TOKEN" -e WHISPER_MODEL=tiny \
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
python - "$DIR/health.json" <<'PY'
import json, sys
health = json.load(open(sys.argv[1]))
assert health.get("status") == "ok"
assert health.get("ffmpeg") and health.get("ffprobe"), health
PY

code="$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/jobs")"
test "$code" = "401"

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
curl -fsS --max-time 40 -H "X-Access-Key: $TOKEN" \
  "$URL/api/jobs/$id/files/shorts_factory_export.zip" -o "$DIR/export.zip"
test -s "$DIR/export.zip"

python - "$DIR/export.zip" "$DIR/status.json" <<'PY'
import json, sys, zipfile
job = json.load(open(sys.argv[2]))
assert len(job["clips"]) == 1, job
with zipfile.ZipFile(sys.argv[1]) as zipped:
    assert "clip_01.mp4" in zipped.namelist()
    assert "clips.json" in zipped.namelist()
print("Authenticated upload, video processing, MP4 download and ZIP export passed.")
PY

docker exec "$NAME" ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height -of csv=p=0 \
  "/data/$id/outputs/clip_01.mp4" > "$DIR/dimensions.txt"
grep -Fxq "1080,1920" "$DIR/dimensions.txt"

echo "Shorts Factory container smoke test passed."
