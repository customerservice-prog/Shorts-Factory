# Shorts Factory

**One long video → a batch of branded vertical Shorts.** A standalone, self-hosted editing studio, built for the GitHub repository customerservice-prog/Shorts-Factory.

## Features

- Drag-and-drop video upload: MP4, MOV, MKV, WEBM, M4V
- Brand full-length videos, including one-hour recordings up to 8 GB per file by default
- Local speech transcription with Faster Whisper (no paid LLM API key)
- Speech-aware highlight ranking and non-overlapping clip selection
- Choose 1–10 clips, targeting 20, 30, 45 or 60 seconds
- 1080 × 1920 vertical MP4/H.264 exports with AAC audio and loudness normalization
- Two framings: blurred/full-frame interview and center crop
- Animated word-highlight captions (ASS karaoke) or classic captions
- Optional custom logo upload or text watermark
- In-app progress, clip preview, individual downloads and a ZIP batch export
- Private access key, automatic cleanup, and persistent job history (with a volume)

**Not included:** automatic posting, social account connections, copyright detection/clearance, ad monetization approval or guaranteed viral highlights. Clip ranking uses local transcription and transparent heuristics, not proprietary engagement-prediction data.

## Run locally

Requires Python 3.11+, FFmpeg with libass and drawtext support, and enough CPU/RAM to render video.

1. Clone the repository and install the dependencies:
   
   - python -m venv .venv
   - source .venv/bin/activate (on Windows: .venv\Scripts\activate)
   - pip install -r requirements.txt
2. Set APP_PASSWORD to a long private password (recommended).
3. Start the app with: uvicorn app:app --host 127.0.0.1 --port 8080
4. Open http://127.0.0.1:8080

Or build and run the included Dockerfile:

- docker build -t shorts-factory .
- docker run --rm -p 8080:8080 -v shorts-factory-data:/data -e APP_PASSWORD=change-me shorts-factory

The first transcription may download a Whisper model. Processing runs locally, but downloading the pretrained model initially requires internet access. Videos are never submitted to a paid AI API by this app.

## Deploy on Railway

1. Create a Railway project, add a service from this GitHub repository (branch main). Railway uses Dockerfile via railway.json.
2. Set **APP_PASSWORD** to a strong unique password before allowing visitors to upload files. The app rejects uploads on Railway when this value is missing.
3. Attach a **persistent volume mounted at /data** for exports, job history, brand profiles, and the downloaded Whisper model.
4. For CPU transcription and HD video encoding, allocate enough CPU/RAM; aim for **at least 2 GB RAM and 2 vCPU**, depending on the model and source resolution. Jobs can take minutes on smaller services.
5. Set optional variables such as MAX_UPLOAD_MB=750, MAX_FULL_UPLOAD_MB=8192, WHISPER_MODEL=base, WHISPER_THREADS=2, WORKERS=1, JOB_TTL_HOURS=48, and FFMPEG_CRF=22.
6. Use Railway's generated HTTPS domain. /healthz reports the web app and FFmpeg presence.

IMPORTANT: Railway and storage can incur hosting costs. No paid AI API is needed, but the hosting itself is not necessarily free.

## Technical overview

- FastAPI backend serves the SPA, manages uploads/jobs, and requires X-Access-Key when APP_PASSWORD is configured.
- A single in-process worker runs Faster Whisper for speech transcription and scores sentence-bounded candidate windows using word density, strong openings, thematic words, punctuation and overlap reduction.
- FFmpeg creates vertical videos, branding, subtitles and normalized audio.
- Original video uploads stay in temporary space during processing. To reframe or rebrand a saved job, select the original from your device. Finished exports live under DATA_DIR (default ./data, production /data).
- Choose **Already has subtitles or text** to keep on-screen wording and skip added captions. In full-video mode this also skips the follow banner and automatic text cleanup. A batch can use the setting for individual files.
- Full-length uploads use separate requests for each part. Railway can close an individual upload request after five minutes.
- Opening the studio with the access key authorizes media playback and downloads. Playback and large MP4 downloads can use HTTP byte ranges.
- Processing jobs are local; active jobs interrupted by an app restart cannot be resumed.
- Files are processed from **direct uploads only**. The app has no URL downloader.

## Copyright and monetization

This tool edits videos; it **does not grant permission to republish a podcast, interview, music, or video**. Cropping, shortening, subtitling, logo overlays, or changing speed generally do not by themselves make reused material original or eligible for monetization. Fair use is fact-specific, and platform rules vary. The rights selector is a reminder, not a legal determination.

For the clearest commercial workflow, use your own content, content you have licensed for the relevant uses, or materials for which you have obtained creator permission. Review generated clips and each platform's current rules before posting.

## Testing

- python -m unittest discover -s tests -v
- python -m py_compile app.py pipeline.py
- node --check static/app.js

Tests cover clip selection fallback, title generation, highlight scoring, subtitle generation and ASS time formatting. Full FFmpeg/Whisper output requires an integration test with a real permitted video.

## Operational limitations

- Highlight ranking is a heuristic and can miss a better quote. Always review the clips.
- Full-frame layout preserves interview participants better than a center crop; neither currently performs automatic speaker/face tracking.
- Word timing depends on the accuracy of the Whisper model. Poor audio, overlapping speakers and music can degrade subtitles.
- No queue persistence across server restarts, GPU support, platform upload integration or automatic brand music licensing.
- Batch ZIPs are generated when requested and take temporary space on the server.
