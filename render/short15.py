"""15-second / four-image Ken Burns adapter for the existing 3loom renderer.

Usage: python render/short15.py path/to/job.json output_dir

The job JSON must contain audio_b64, script, and exactly four images, each
with a verified HTTPS URL and clear licensing information. This module never
calls paid image or video APIs, and never publishes to YouTube.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import engine

SCENE_TEXT = [
    "بتعرف إن اللي بيطلع من البركان مش نار؟",
    "هاي صخور ذايبة من شدة الحرارة!",
    "تحت الأرض اسمها صهارة، ولما تطلع للسطح بنسميها حمم.",
    "يعني اللي شايفه مش نار… بتعرف ليش؟",
]


def prepare(job: dict) -> dict:
    images = job.get("images") or []
    if len(images) != 4:
        raise ValueError("Exactly four licensed images are required")
    if not job.get("audio_b64"):
        raise ValueError("Narration audio_b64 is required")
    if not job.get("job_id"):
        raise ValueError("job_id is required")
    scenes = []
    for i, im in enumerate(images):
        if not isinstance(im, dict) or not str(im.get("url", "")).startswith("https://"):
            raise ValueError(f"Image {i+1} must have an HTTPS URL")
        if not im.get("license"):
            raise ValueError(f"Image {i+1} needs a source license")
        scenes.append({
            "text": SCENE_TEXT[i],
            "type": "footage",
            "queries": ["volcano lava", "molten basalt", "volcano magma cross section", "volcano eruption"],
            "must": [], "avoid": [],
            "graphic": {"title": "", "sub": ""},
            "image": im["url"],
            "image_license": im["license"],
            "image_author": im.get("author", ""),
            "image_page": im.get("page", ""),
        })
    # Image URLs are fed directly as vetted, licensed photo candidates.
    # Keep the standard engine's QC, captions, voice alignment and MP4 export.
    job = dict(job)
    job["script"] = " ".join(SCENE_TEXT)
    job["hook"] = "مش نار؟!"
    job["scenes"] = scenes
    job["punch_cuts"] = False
    return job


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: short15.py JOB.json OUTPUT_DIR")
    job = prepare(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    result = engine.render_job(job, Path(sys.argv[2]))
    if not (14.5 <= result["duration"] <= 15.5):
        raise ValueError(f"Expected 15 seconds, got {result['duration']}")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
