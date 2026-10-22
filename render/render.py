"""Render a vertical Arabic Short: stock clips + narration + burned-in Arabic captions.

Pipeline (all local, no paid services):
1. Normalise narration loudness (EBU R128 loudnorm, default -14 LUFS, true peak -1.5 dBTP).
2. Download clips, cut each to an equal share of the narration, scale/crop to 1080x1920 @30fps.
3. Split the script into short phrases (max 5 words) and time them proportionally to
   their length across the narration. This is an APPROXIMATE sync; word-level sync needs ASR.
4. Draw each phrase with Pillow (raqm shaping, RTL) to a transparent PNG and overlay it.
5. Mux to H.264/AAC MP4 with faststart.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FPS = 30
MAX_WORDS = 5
MIN_PHRASE_SEC = 0.6
CAPTION_Y = 1240          # inside the safe area (150..1640)
CAPTION_MAX_W = 860       # safe area x 40..950 (centred)
DOWNLOAD_LIMIT = 200 * 1024 * 1024


class RenderError(RuntimeError):
    pass


def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RenderError(f"{cmd[0]} failed: {p.stderr[-1500:]}")
    return p.stdout + p.stderr


def probe_duration(path: Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        raise RenderError(f"cannot read duration of {path.name}")
    return float(p.stdout.strip())


def font_path() -> str:
    env = os.environ.get("CAPTION_FONT")
    if env and Path(env).is_file():
        return env
    p = subprocess.run(["fc-list", ":lang=ar", "file", "family", "style"], capture_output=True, text=True)
    fonts = [line for line in p.stdout.splitlines() if line.strip()]
    for want in ("Noto Kufi Arabic", "Noto Sans Arabic", "Noto Naskh Arabic", "DejaVu Sans"):
        hits = [f for f in fonts if want in f]
        bold = [f for f in hits if "Bold" in f and "Semi" not in f and "Extra" not in f]
        for f in bold + hits:
            path = f.split(":", 1)[0]
            if Path(path).is_file():
                return path
    if fonts:
        return fonts[0].split(":", 1)[0]
    raise RenderError("no Arabic font found (install fonts-noto-core)")


NASA_PREFERENCE = ("~medium.mp4", "~mobile.mp4", "~small.mp4", "~large.mp4", "~orig.mp4")


def safe_url(url: str) -> str:
    return urllib.parse.quote(url, safe=":/?=&%~+#,;@!$'()*")


def resolve_nasa(url: str) -> str:
    """NASA search returns a collection.json manifest; pick a reasonably sized MP4 from it."""
    req = urllib.request.Request(safe_url(url), headers={"User-Agent": "3loom-render/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        files = [u.replace("http://", "https://", 1) for u in json.loads(r.read().decode())]
    for suffix in NASA_PREFERENCE:
        for f in files:
            if f.endswith(suffix):
                return f
    mp4 = [f for f in files if f.lower().endswith(".mp4")]
    if not mp4:
        raise RenderError(f"no mp4 in NASA manifest {url[:80]}")
    return mp4[0]


def download(url: str, dest: Path) -> Path:
    if url.startswith("file://") and os.environ.get("ALLOW_FILE_URLS") == "1":
        shutil.copyfile(url[7:], dest)
        return dest
    url = url.replace("http://images-assets.nasa.gov", "https://images-assets.nasa.gov", 1)
    if url.startswith("https://images-assets.nasa.gov/") and url.endswith("collection.json"):
        url = resolve_nasa(url)
    if not url.startswith("https://"):
        raise RenderError(f"refusing non-https clip url: {url[:80]}")
    req = urllib.request.Request(safe_url(url), headers={"User-Agent": "ulum-blahjetna-render/1.0 (educational)"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        total = 0
        while chunk := r.read(1 << 20):
            total += len(chunk)
            if total > DOWNLOAD_LIMIT:
                raise RenderError("clip too large")
            f.write(chunk)
    return dest


def phrases(script: str) -> list[str]:
    sentences = [s.strip() for s in re.split(r"(?<=[.!؟?…])\s+|\.\.\s*|\n+", script) if s.strip()]
    out: list[str] = []
    for s in sentences:
        words = s.split()
        n = max(1, -(-len(words) // MAX_WORDS))
        size = -(-len(words) // n)
        for i in range(0, len(words), size):
            out.append(" ".join(words[i:i + size]))
    return out


def timeline(parts: list[str], total: float) -> list[tuple[float, float, str]]:
    weights = [max(len(p.replace(" ", "")), 4) for p in parts]
    scale = total / sum(weights)
    t, out = 0.0, []
    for p, w in zip(parts, weights):
        d = max(w * scale, MIN_PHRASE_SEC)
        out.append((round(t, 3), round(min(t + d, total), 3), p))
        t += d
    return out


def caption_png(text: str, font_file: str, dest: Path) -> Path:
    size = 78
    while True:
        font = ImageFont.truetype(font_file, size)
        probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        box = probe.textbbox((0, 0), text, font=font, direction="rtl", language="ar")
        if box[2] - box[0] <= CAPTION_MAX_W or size <= 48:
            break
        size -= 4
    w, h = box[2] - box[0] + 64, box[3] - box[1] + 48
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=26, fill=(0, 0, 0, 150))
    d.text((32 - box[0], 24 - box[1]), text, font=font, fill=(255, 255, 255, 255),
           direction="rtl", language="ar", stroke_width=3, stroke_fill=(0, 0, 0, 255))
    img.save(dest)
    return dest


def render(audio: Path, clips: list[dict], script: str, out: Path,
           width: int = 1080, height: int = 1920, lufs: float = -14.0) -> dict:
    if not clips:
        raise RenderError("no clips supplied (WAITING_FOR_VIDEO_ASSETS)")
    work = Path(tempfile.mkdtemp(prefix="render-"))
    try:
        voice = work / "voice.m4a"
        run(["ffmpeg", "-y", "-v", "error", "-i", str(audio), "-af",
             f"loudnorm=I={lufs}:TP=-1.5:LRA=11,aresample=48000", "-c:a", "aac", "-b:a", "192k", str(voice)])
        total = probe_duration(voice)

        seg = total / len(clips)
        parts = []
        for i, c in enumerate(clips):
            src = download(c["url"], work / f"src{i}.mp4")
            dst = work / f"seg{i}.mp4"
            run(["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", str(src), "-t", f"{seg:.3f}",
                 "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={FPS},setsar=1,format=yuv420p",
                 "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(dst)])
            parts.append(dst)
        lst = work / "list.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
        bg = work / "bg.mp4"
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(bg)])

        font_file = font_path()
        tl = timeline(phrases(script), total)
        inputs = ["-i", str(bg), "-i", str(voice)]
        chain, last = [], "0:v"
        for k, (a, b, text) in enumerate(tl):
            png = caption_png(text, font_file, work / f"cap{k}.png")
            inputs += ["-i", str(png)]
            nxt = f"v{k}"
            chain.append(f"[{last}][{k + 2}:v]overlay=x=(W-w)/2:y={CAPTION_Y}:enable='between(t,{a},{b})'[{nxt}]")
            last = nxt
        cmd = ["ffmpeg", "-y", "-v", "error", *inputs]
        if chain:
            cmd += ["-filter_complex", ";".join(chain), "-map", f"[{last}]"]
        else:
            cmd += ["-map", "0:v"]
        cmd += ["-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-maxrate", "5M", "-bufsize", "10M",
                "-pix_fmt", "yuv420p",
                "-r", str(FPS), "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(out)]
        run(cmd)
        return {"duration": round(total, 2), "clips": len(clips), "captions": len(tl),
                "timeline": [{"start": a, "end": b, "text": t} for a, b, t in tl],
                "sync": "approximate (proportional to text length, no ASR)"}
    finally:
        shutil.rmtree(work, ignore_errors=True)
