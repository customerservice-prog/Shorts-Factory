"""Honest technical QA and monetization-risk assessment; never legal clearance.

Platform eligibility depends on rights, originality, account standing and
platform decisions unavailable to this offline self-hosted application.
"""
import json
import re
import subprocess
import unicodedata
from pathlib import Path

AD_SENSITIVE = {
    "fuck", "fucking", "shit", "bitch", "motherfucker", "nigga", "nigger",
    "cabron", "cabrones", "coño", "mierda", "puta", "putas", "pendejo",
}
MUSIC_VIDEO = re.compile(r"\b(official (music )?video|video oficial|visualizer|lyric video)\b", re.I)
PLATFORMS = {"youtube_shorts", "facebook_reels", "tiktok_rewards", "other"}
RIGHTS = {"owned", "licensed", "commentary", "unknown"}
AUDIO_RIGHTS = {"owned", "licensed", "no_music", "unknown"}
ORIGINALITY = {"original", "substantial_commentary", "minimal", "unknown"}


def inspect_export(path, expected_length, source_has_audio):
    """Check actual encoded output; reject broken files before marking ready."""
    file = Path(path)
    if not file.is_file() or file.stat().st_size < 6000:
        raise RuntimeError("Export is empty or too small")
    run = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,size:stream=index,codec_type,codec_name,width,height",
        "-of", "json", str(file)
    ], capture_output=True, text=True, timeout=40, check=True)
    data = json.loads(run.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video or (video.get("width"), video.get("height")) != (1080, 1920):
        raise RuntimeError("Export failed 1080×1920 portrait verification")
    if video.get("codec_name") != "h264":
        raise RuntimeError("Export failed H.264 video verification")
    if source_has_audio and (not audio or audio.get("codec_name") != "aac"):
        raise RuntimeError("Export lost its expected AAC audio")
    duration = float(data.get("format", {}).get("duration") or 0)
    if duration <= 0 or abs(duration - expected_length) > max(2.5, expected_length * .12):
        raise RuntimeError("Export duration differs unexpectedly from requested cut")
    return {"width": 1080, "height": 1920, "codec": "h264",
            "audio": bool(audio), "duration": round(duration, 2),
            "size_bytes": file.stat().st_size, "verified": True}


def item(level, title, detail):
    return {"level": level, "title": title, "detail": detail}


def _clean(text):
    result = unicodedata.normalize("NFKD", str(text).lower())
    return "".join(c for c in result if not unicodedata.combining(c))


def risk_review(job, clips, words, language, source_duration, has_audio):
    """Per-clip and source-level evidence with explicit unverified checks."""
    platform = job.get("target_platform", "youtube_shorts")
    if platform not in PLATFORMS:
        platform = "youtube_shorts"
    findings = []
    rights = job.get("rights", "unknown")
    sound = job.get("audio_rights", "unknown")
    originality = job.get("originality", "unknown")
    evidence = str(job.get("rights_evidence", "")).strip()
    if rights == "unknown":
        findings.append(item("high", "Footage rights are unverified",
                             "Identify the footage owner and obtain rights to publish and monetize it."))
    elif rights == "commentary":
        findings.append(item("high", "Commentary exception is not automatically established",
                             "Fair use and platform originality require substantive new commentary and human review."))
    elif rights == "licensed" and not evidence:
        findings.append(item("high", "License evidence not provided",
                             "Keep a license or written permission that includes commercial edits and this platform."))
    else:
        findings.append(item("review", "Footage ownership/permission is self-reported",
                             "The studio cannot verify contracts, ownership or reuse restrictions."))
    if has_audio:
        if sound == "unknown":
            findings.append(item("high", "Music and audio rights unverified",
                                 "Check all background music, beats, vocals and third-party recordings separately."))
        elif sound == "licensed" and not evidence:
            findings.append(item("review", "Audio license not independently verified",
                                 "Check commercial synchronization/master recording rights and platform scope."))
    if originality in ("minimal", "unknown"):
        findings.append(item("high", "Original contribution unclear or minimal",
                             "A short excerpt, captions, crop and added channel logo alone do not resolve reused-content risk."))
    elif originality == "substantial_commentary":
        findings.append(item("review", "Original commentary must be reviewed",
                             "Verify that substantial new analysis, narration or original storytelling is audible and visible."))
    if MUSIC_VIDEO.search(job.get("filename", "")):
        findings.append(item("high", "Filename suggests an existing music video",
                             "Confirm rights from relevant visual, composition and master owners before monetizing clips."))
    if platform == "tiktok_rewards":
        findings.append(item("high", "TikTok Creator Rewards duration",
                             "This program requires videos longer than one minute; 20–60-second exports do not qualify."))
    if platform == "youtube_shorts":
        findings.append(item("review", "YouTube Shorts reused-content and channel review",
                             "Check originality, advertiser suitability and channel eligibility after upload."))
    elif platform == "facebook_reels":
        findings.append(item("review", "Facebook originality and account eligibility",
                             "Check content monetization status and originality rules in the Professional Dashboard."))
    findings.append(item("unverified", "No copyright-match database connected",
                         "The studio cannot detect every protected song, video clip, claim, restriction or content fingerprint."))
    per_clip = []
    for i, clip in enumerate(clips, 1):
        start, end = float(clip.get("start", 0)), float(clip.get("end", 0))
        segment_words = [w.get("text", "") for w in words
                         if start <= float(w.get("start", 0)) <= end]
        segment_text = _clean(" ".join(segment_words))
        tokens = set(re.findall(r"[a-z]+", segment_text))
        flagged = sorted(tokens & AD_SENSITIVE)
        checks = []
        if flagged:
            checks.append(item("review", "Potentially advertiser-sensitive speech",
                               "Review language and context: " + ", ".join(flagged[:5]) + "."))
        if not clip.get("captions") and job.get("captions") != "none":
            checks.append(item("review", "Requested captions unavailable",
                               "Check transcription accuracy, subtitle timing and readability."))
        if len(segment_words) < 7 and has_audio:
            checks.append(item("review", "Limited detected dialogue",
                               "This may be music or instrumental footage; highlight ranking may be less reliable."))
        if job.get("layout") == "blur":
            checks.append(item("review", "Blurred-edge framing",
                               "Inspect composition for distracting duplicate panels or unreadable text."))
        if clip.get("highlight_score", 0) < 40:
            checks.append(item("review", "Lower highlight heuristic",
                               "Watch for weak opening hooks; this score does not predict reach."))
        if platform == "tiktok_rewards" and clip.get("length", 0) <= 60:
            checks.append(item("high", "Too short for TikTok Creator Rewards",
                               "Program videos must be longer than one minute."))
        if not clip.get("technical", {}).get("verified"):
            checks.append(item("high", "Video technical inspection missing",
                               "Export codec, dimensions and duration must be verified."))
        per_clip.append({"clip": i, "file": clip.get("file"), "status": "manual_review_required",
                         "detected_language": language or "unknown",
                         "spoken_words_detected": len(segment_words),
                         "checks": checks,
                         "disclaimer": "Cannot verify monetization eligibility or copyright clearance."})
    return {
        "status": "high_risk" if any(f["level"] == "high" for f in findings) else "manual_review_required",
        "monetization_guaranteed": False,
        "platform": platform,
        "source_duration": round(source_duration, 1),
        "source_findings": findings,
        "clip_reviews": per_clip,
        "limitations": [
            "No platform account, ad-suitability or copyright fingerprint verification.",
            "Permissions and originality claims are user-provided, not independently authenticated.",
            "A final human review and the destination platform's decision are required.",
        ],
    }
