"""Dependency-free preflight checks for a scientific short render job.

Fail closed: never dispatch expensive rendering or publishing with malformed assets.
"""
from __future__ import annotations

import json
import math
import re
from urllib.parse import urlsplit

JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
ALLOWED_MEDIA_HOSTS = (
    "images-assets.nasa.gov", "images.nasa.gov", "upload.wikimedia.org",
    "commons.wikimedia.org", "images.pexels.com", "videos.pexels.com",
    "cdn.pixabay.com", "player.vimeo.com",
)


def valid_media_url(url: str) -> bool:
    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return (parts.scheme == "https" and not parts.username and not parts.password
                and parts.port in (None, 443) and host in ALLOWED_MEDIA_HOSTS)
    except ValueError:
        return False


def check_job(job: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(job, dict):
        return ["job must be a JSON object"]
    if not JOB_ID.fullmatch(str(job.get("job_id", ""))):
        errors.append("invalid job_id")
    script = job.get("script", "")
    if not isinstance(script, str) or not (20 <= len(script.strip()) <= 5000):
        errors.append("script must be 20..5000 characters")
    clips = job.get("clips")
    if not isinstance(clips, list) or not (1 <= len(clips) <= 8):
        errors.append("provide 1..8 clips")
    else:
        for i, clip in enumerate(clips):
            if not isinstance(clip, dict) or not valid_media_url(clip.get("url", "")):
                errors.append(f"clip {i + 1}: unapproved or invalid media URL")
    audio = job.get("audio_b64", "")
    if not isinstance(audio, str) or not (100 <= len(audio) <= 20_000_000):
        errors.append("audio_b64 missing or exceeds 20MB encoded limit")
    try:
        lufs = float(job.get("loudness_lufs", -14))
        if not math.isfinite(lufs) or not -20 <= lufs <= -9:
            errors.append("invalid loudness_lufs")
    except (TypeError, ValueError):
        errors.append("invalid loudness_lufs")
    callback = job.get("callback_url", "")
    if not isinstance(callback, str) or not callback.startswith("https://"):
        errors.append("callback must use HTTPS")
    if job.get("publish") or job.get("upload"):
        errors.append("publishing is disabled: human approval required")
    return errors


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("job")
    args = parser.parse_args()
    with open(args.job, encoding="utf-8") as f:
        job = json.load(f)
    errors = check_job(job)
    print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
