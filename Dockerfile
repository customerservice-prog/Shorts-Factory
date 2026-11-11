FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg fonts-dejavu-core libgomp1 ca-certificates gosu tesseract-ocr tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py pipeline.py branding.py review.py brand_only.py show_style.py overlay_cleanup.py cleanup_preview.py /app/
COPY static /app/static
COPY entrypoint.sh /app/entrypoint.sh
RUN groupadd --gid 10001 studio && useradd --uid 10001 --gid studio --create-home studio \
    && mkdir -p /data && chown -R studio:studio /data /app \
    && chmod 755 /app/entrypoint.sh
ENV DATA_DIR=/data HF_HOME=/data/hf-cache WHISPER_MODEL=base WHISPER_THREADS=2
EXPOSE 8080
# Root is used only for chown of the Railway-mounted /data volume at startup.
# The entrypoint immediately drops privileges to studio before running Uvicorn.
USER root
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers"]
