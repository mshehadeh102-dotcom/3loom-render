"""Turn a candidate into a 1080x1920 scene segment and check it before it is used.

Checks per candidate (frames sampled from the exact part that will be shown):
- black / nearly black picture        -> rejected
- English text, titles, agency logos   -> rejected (Tesseract OCR, Latin words with high confidence)
- frozen or still video                -> not rejected: re-rendered as a slow Ken Burns move so the
                                          opening is never a static frame
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

FPS = 30
W, H = 1080, 1920
UA = "ulum-blahjetna-render/2.0 (educational YouTube Shorts)"
INTRO_SKIP = 2.0          # seconds skipped at the start of any source video (title cards, logos)
MAX_SLOW = 1.6
WATERMARKS = re.compile(r"^(usgs|noaa|nasa|esa|jaxa|getty|shutterstock|istock|pond5|storyblocks|copyright|www)$", re.I)


class VetError(RuntimeError):
    pass


def ua(src: str) -> list[str]:
    return ["-user_agent", UA] if src.startswith("http") else []


def run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise VetError(f"{cmd[0]}: {p.stderr[-600:]}")
    return p


def probe(path_or_url: str) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", *ua(path_or_url), "-show_entries",
                        "format=duration:stream=width,height,codec_type", "-of", "json", path_or_url],
                       capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        raise VetError("cannot probe media")
    d = json.loads(p.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"duration": float((d.get("format") or {}).get("duration") or 0), "width": int(v.get("width") or 0),
            "height": int(v.get("height") or 0)}


def resolve_nasa(url: str, kind: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        files = [u.replace("http://", "https://", 1).replace(" ", "%20") for u in json.loads(r.read().decode())]
    prefs = ("~medium.mp4", "~mobile.mp4", "~small.mp4", "~large.mp4", "~orig.mp4") if kind == "video" \
        else ("~large.jpg", "~medium.jpg", "~orig.jpg", "~large.png", "~orig.png")
    for suf in prefs:
        for f in files:
            if f.endswith(suf):
                return f
    raise VetError("no usable file in NASA manifest")


def media_url(c: dict) -> str:
    url = c["url"]
    if c.get("manifest") or url.endswith("collection.json"):
        url = resolve_nasa(url, c["kind"])
    if url.startswith("file://"):
        return url[7:]
    if not url.startswith("https://"):
        raise VetError("refusing non-https media")
    return url


def download(url: str, dest: Path, limit: int = 60 * 1024 * 1024) -> Path:
    if not url.startswith("https://"):
        shutil.copyfile(url, dest)
        return dest
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r, open(dest, "wb") as f:
        total = 0
        while chunk := r.read(1 << 20):
            total += len(chunk)
            if total > limit:
                raise VetError("file too large")
            f.write(chunk)
    return dest


COVER = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"


def video_segment(src: str, length: float, work: Path, tag: str, src_duration: float = 0.0) -> Path:
    """Cut `length` seconds after skipping the intro; slow down slightly short sources, loop very short ones."""
    if src_duration and src_duration < 3.0:
        raise VetError("source shorter than 3 s")
    off = window_start(src_duration, length)
    piece = work / f"{tag}_piece.mp4"
    take = length * MAX_SLOW + 1.0
    run(["ffmpeg", "-y", "-v", "error", *ua(src), "-ss", f"{off:.2f}", "-i", src, "-t", f"{take:.2f}",
         "-an", "-vf", f"{COVER},fps={FPS},format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
         str(piece)], timeout=420)
    have = probe(str(piece))["duration"]
    if have < 1.5:
        raise VetError("usable part shorter than 1.5 s")
    out = work / f"{tag}.mp4"
    if have >= length:
        vf, pre = f"fps={FPS}", []
    elif have * MAX_SLOW >= length:
        vf, pre = f"setpts={length / have:.4f}*PTS,fps={FPS}", []
    else:
        vf, pre = f"fps={FPS}", ["-stream_loop", "-1"]  # loops the trimmed piece, never the intro
    run(["ffmpeg", "-y", "-v", "error", *pre, "-i", str(piece), "-t", f"{length:.3f}", "-vf", vf + ",format=yuv420p",
         "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", str(out)], timeout=420)
    return out


KB_MOVES = [
    ("min(zoom+{r},1.18)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),                    # slow push-in
    ("1.15", "(iw-iw/zoom)*on/{n}", "ih/2-(ih/zoom/2)"),                              # pan left->right
    ("max(1.18-{r}*on,1.0)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),                  # pull-out
    ("1.15", "(iw-iw/zoom)*(1-on/{n})", "ih/2-(ih/zoom/2)"),                          # pan right->left
]


def ken_burns(image: Path, length: float, work: Path, tag: str, move: int = 0) -> Path:
    n = max(2, int(round(length * FPS)))
    z, x, y = KB_MOVES[move % len(KB_MOVES)]
    r = 0.18 / n
    zp = f"zoompan=z='{z.format(r=r, n=n)}':x='{x.format(n=n)}':y='{y.format(n=n)}':d={n}:s={W}x{H}:fps={FPS}"
    out = work / f"{tag}.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", str(image), "-frames:v", str(n),
         "-vf", f"scale={int(W * 1.3)}:{int(H * 1.3)}:force_original_aspect_ratio=increase,crop={int(W * 1.3)}:{int(H * 1.3)},{zp},format=yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", str(out)], timeout=420)
    return out


def sample_frames(video: Path, work: Path, tag: str, count: int = 4) -> list[Path]:
    dur = probe(str(video))["duration"] or 1.0
    frames = []
    for k in range(count):
        t = dur * (k + 0.5) / count
        f = work / f"{tag}_f{k}.png"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
             "-vf", "scale=540:960", str(f)])
        frames.append(f)
    return frames


def detect(video: Path, vf: str, pattern: str) -> list[float]:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(video), "-vf", vf, "-an", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=300)
    return [float(x) for x in re.findall(pattern, p.stderr)]


def luminance(img: Path) -> float:
    return ImageStat.Stat(Image.open(img).convert("L")).mean[0]


def motion(frames: list[Path]) -> float:
    ims = [Image.open(f).convert("L").resize((135, 240)) for f in frames]
    diffs = [ImageStat.Stat(ImageChops.difference(a, b)).mean[0] for a, b in zip(ims, ims[1:])]
    return sum(diffs) / max(1, len(diffs))


_RAPID = None


def _rapid_words(img: Path) -> list[str]:
    global _RAPID
    try:
        if _RAPID is None:
            from rapidocr_onnxruntime import RapidOCR
            _RAPID = RapidOCR()
        res, _ = _RAPID(str(img))
    except Exception:  # noqa: BLE001 - OCR is a filter, never a crash
        return []
    words = []
    for _box, text, conf in res or []:
        if float(conf) >= 0.75:
            words += re.findall(r"[A-Za-z]{3,}", text)
    return words


def latin_text(img: Path) -> list[str]:
    """English words read with high confidence (titles, captions, agency logos): Tesseract or RapidOCR."""
    if not shutil.which("tesseract"):
        return _rapid_words(img)
    p = subprocess.run(["tesseract", str(img), "-", "-l", "eng", "--psm", "11", "tsv"],
                       capture_output=True, text=True, timeout=60)
    words = []
    for line in p.stdout.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 12:
            continue
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        w = cols[11].strip()
        if conf >= 80 and re.fullmatch(r"[A-Za-z]{3,}", w):
            words.append(w)
    return words


def check(frames: list[Path]) -> dict:
    lum = [luminance(f) for f in frames]
    words = []
    for f in frames:
        words += latin_text(f)
    std = [ImageStat.Stat(Image.open(f).convert("L")).stddev[0] for f in frames]
    dark = sum(1 for v, d in zip(lum, std) if v < 10 or (v < 22 and d < 10)) / len(lum)
    marks = [w for w in words if WATERMARKS.match(w)]
    return {"lum": [round(v, 1) for v in lum], "dark_ratio": round(dark, 2), "latin_words": words[:12],
            "watermarks": marks, "motion": round(motion(frames), 2) if len(frames) > 1 else 0.0}


def window_start(src_duration: float, length: float) -> float:
    if not src_duration:
        return INTRO_SKIP
    off = min(max(INTRO_SKIP, 0.12 * src_duration), max(0.0, src_duration - min(length, src_duration) - 0.3))
    return max(0.0, min(off, src_duration - 2.5))


def precheck(url: str, length: float, src_duration: float, work: Path, tag: str) -> dict:
    """Read 3 frames straight from the source (fast seek) and reject dark / text-heavy footage before encoding."""
    off = window_start(src_duration, length)
    span = min(length, max(1.0, (src_duration or off + length) - off - 0.2))
    frames = []
    for k in range(3):
        t = off + span * (k + 0.5) / 3
        f = work / f"{tag}_pre{k}.png"
        try:
            run(["ffmpeg", "-y", "-v", "error", *ua(url), "-ss", f"{t:.2f}", "-i", url, "-frames:v", "1",
                 "-vf", "scale=540:960:force_original_aspect_ratio=increase,crop=540:960", str(f)], timeout=120)
            frames.append(f)
        except VetError:
            continue
    if not frames:
        raise VetError("could not read frames from source")
    return check(frames)


def build_scene(c: dict, length: float, work: Path, tag: str, move: int) -> tuple[Path, dict]:
    """Returns (segment, report). Raises VetError when the candidate must not be used."""
    url = media_url(c)
    if c["kind"] == "image":
        img = download(url, work / f"{tag}_src.img")
        with Image.open(img) as im:
            if min(im.size) < 600:
                raise VetError(f"image too small {im.size}")
            im.convert("RGB").save(work / f"{tag}_src.jpg", quality=93)
        rep = check([work / f"{tag}_src.jpg"])
        if rep["dark_ratio"] >= 0.5:
            raise VetError("dark image")
        if len(rep["latin_words"]) >= 3 or rep["watermarks"]:
            raise VetError(f"text/logo in image: {rep['latin_words'][:5]}")
        seg = ken_burns(work / f"{tag}_src.jpg", length, work, tag, move)
        if detect(seg, "freezedetect=n=0.002:d=2.0", r"freeze_duration: ([\d.]+)"):
            raise VetError("featureless image (looks frozen even with camera move)")
        rep["treatment"] = "ken-burns photo"
        return seg, rep
    info = probe(url)
    if info["height"] and min(info["width"], info["height"]) < 360:
        raise VetError("video resolution too low")
    pre = precheck(url, length, info["duration"], work, tag)
    if pre["dark_ratio"] >= 0.5:
        raise VetError(f"black/dark footage {pre['lum']}")
    if len(pre["latin_words"]) >= 3 or pre["watermarks"]:
        raise VetError(f"text/logo in footage: {(pre['watermarks'] or pre['latin_words'])[:5]}")
    seg = video_segment(url, length, work, tag, info["duration"])
    frames = sample_frames(seg, work, tag)
    rep = check(frames)
    if rep["dark_ratio"] >= 0.5:
        raise VetError(f"black/dark footage {rep['lum']}")
    blacks = detect(seg, "blackdetect=d=0.5:pix_th=0.10", r"black_duration:([\d.]+)")
    if blacks:
        raise VetError(f"black interval {blacks[0]:.1f}s inside the shot")
    rep["freeze"] = detect(seg, "freezedetect=n=0.002:d=2.0", r"freeze_duration: ([\d.]+)")
    if len(rep["latin_words"]) >= 3 or rep["watermarks"]:
        raise VetError(f"text/logo in footage: {(rep['watermarks'] or rep['latin_words'])[:5]}")
    if rep["motion"] < 1.2 or rep["freeze"]:  # frozen or a still: give it camera movement
        k = max(range(len(frames)), key=lambda i: luminance(frames[i]))
        full = work / f"{tag}_still.png"
        t = (probe(str(seg))["duration"] or 1.0) * (k + 0.5) / len(frames)
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(seg), "-frames:v", "1", str(full)])
        seg = ken_burns(full, length, work, tag + "_kb", move)
        if detect(seg, "freezedetect=n=0.002:d=2.0", r"freeze_duration: ([\d.]+)"):
            raise VetError("featureless still (looks frozen even with camera move)")
        rep["treatment"] = "still video -> ken-burns"
    else:
        rep["treatment"] = "video"
    return seg, rep
