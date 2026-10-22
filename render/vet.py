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
import threading
import time
import urllib.error
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


_THROTTLE = threading.Lock()
_LAST = {"t": 0.0}


_COOL = {"until": 0.0}


class gate_release:
    """Context manager that releases an already-acquired semaphore on exit (also on errors)."""

    def __init__(self, sem):
        self.sem = sem

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.sem.release()
        return False


WIKI_DL = threading.Semaphore(1)  # one Wikimedia transfer at a time; parallel pulls triggered 429 bursts


def polite(url: str) -> None:
    """Wikimedia rate-limits bursts (HTTP 429): keep 0.8 s between requests to its hosts and honour a
    shared cooldown after any 429, so one rejection does not cascade into every following request."""
    if "wikimedia.org" not in url:
        return
    with _THROTTLE:
        wait = max(0.8 - (time.time() - _LAST["t"]), _COOL["until"] - time.time())
        if wait > 0:
            time.sleep(wait)
        _LAST["t"] = time.time()


def cooldown(url: str, retry_after: str | None, attempt: int) -> None:
    """Called on HTTP 429/5xx: every thread waits before its next Wikimedia request."""
    secs = min(float(retry_after or 0) or 15.0 * (attempt + 1), 60.0)
    if "wikimedia.org" in url:
        with _THROTTLE:
            _COOL["until"] = max(_COOL["until"], time.time() + secs)
    else:
        time.sleep(secs)


def download(url: str, dest: Path, limit: int = 150 * 1024 * 1024, budget: float = 0.0,
             attempts: int = 4) -> tuple[Path, bool]:
    """Fetch to a local file. With `budget` (seconds) or when `limit` bytes are reached the download stops
    early and returns (path, True): a truncated video is still usable from the start after a remux.
    Without a budget, oversize raises (images must be complete)."""
    if not url.startswith("https://"):
        shutil.copyfile(url, dest)
        return dest, False
    gate = WIKI_DL if "wikimedia.org" in url else threading.Semaphore(1)
    for attempt in range(attempts):
        polite(url)
        gate.acquire()
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        t0 = time.monotonic()
        try:
            with gate_release(gate), urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
                total = 0
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    total += len(chunk)
                    over = total >= limit or (budget and time.monotonic() - t0 > budget)
                    if over and budget:
                        return dest, True
                    if total > limit:
                        raise VetError("file too large")
            return dest, False
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                cooldown(url, e.headers.get("Retry-After"), attempt)
                continue
            if e.code == 429:  # give up on this file; a short shared pause protects the next request
                cooldown(url, "8", 0)
            raise VetError(f"download HTTP {e.code}")
    raise VetError("download failed")


def remux_partial(src: Path, work: Path, tag: str) -> Path:
    """Copy the decodable start of a truncated download into a clean container so probe() reports
    the real usable length instead of the header's full duration."""
    out = work / f"{tag}_part.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:v:0", "-c", "copy", str(out)],
                   capture_output=True, timeout=120)
    if not out.exists() or out.stat().st_size < 100_000:
        raise VetError("partial download not decodable")
    return out


COVER = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"


def video_segment(src: str, length: float, work: Path, tag: str, src_duration: float = 0.0,
                  intro: float | None = None) -> Path:
    """Cut `length` seconds after skipping the intro; slow down slightly short sources, loop very short ones."""
    if src_duration and src_duration < (1.8 if intro is not None else 3.0):
        raise VetError("source too short")
    off = window_start(src_duration, length, intro)
    piece = work / f"{tag}_piece.mp4"
    take = length * (2.0 if intro is not None else MAX_SLOW) + 1.0
    run(["ffmpeg", "-y", "-v", "error", *ua(src), "-ss", f"{off:.2f}", "-i", src, "-t", f"{take:.2f}",
         "-an", "-vf", f"{COVER},fps={FPS},format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
         str(piece)], timeout=420)
    have = probe(str(piece))["duration"]
    if have < 1.5:
        raise VetError("usable part shorter than 1.5 s")
    out = work / f"{tag}.mp4"
    slow = 2.0 if intro is not None else MAX_SLOW  # a generated shot reads better in slow motion than looped
    if have >= length:
        vf, pre = f"fps={FPS}", []
    elif have * slow >= length:
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


def saturation(img: Path) -> float:
    """Hasler-Suesstrunk colourfulness: ~0-20 for black-and-white or sepia archive, 30+ for natural colour."""
    im = Image.open(img).convert("RGB").resize((135, 240))
    r, g, b = [list(ch.getdata()) for ch in im.split()]
    rg = [x - y for x, y in zip(r, g)]
    yb = [0.5 * (x + y) - z for x, y, z in zip(r, g, b)]

    def ms(a):
        m = sum(a) / len(a)
        return m, max(0.0, sum(v * v for v in a) / len(a) - m * m) ** 0.5

    (mrg, srg), (myb, syb) = ms(rg), ms(yb)
    return (srg ** 2 + syb ** 2) ** 0.5 + 0.3 * (mrg ** 2 + myb ** 2) ** 0.5


def black_band(img: Path) -> bool:
    """True when a full-width black stripe crosses the picture (missing satellite data, letterbox inside crop)."""
    im = Image.open(img).convert("L").resize((90, 160))
    px = im.load()
    rows = 0
    for y in range(16, 144):
        dark = sum(1 for x in range(90) if px[x, y] < 12)
        rows += dark >= 86
    return rows >= 3


def check(frames: list[Path]) -> dict:
    lum = [luminance(f) for f in frames]
    words = []
    for f in frames:
        words += latin_text(f)
    std = [ImageStat.Stat(Image.open(f).convert("L")).stddev[0] for f in frames]
    dark = sum(1 for v, d in zip(lum, std) if v < 10 or (v < 22 and d < 10)) / len(lum)
    marks = [w for w in words if WATERMARKS.match(w)]
    sat = [saturation(f) for f in frames]
    return {"lum": [round(v, 1) for v in lum], "dark_ratio": round(dark, 2), "latin_words": words[:12],
            "saturation": round(sum(sat) / len(sat), 1), "black_band": any(black_band(f) for f in frames),
            "watermarks": marks, "motion": round(motion(frames), 2) if len(frames) > 1 else 0.0}


def window_start(src_duration: float, length: float, intro: float | None = None) -> float:
    """Where to start reading a source. Archive footage skips its intro (titles, black); clips generated
    for the scene (intro=0) start at their first frame, which is the composed start image."""
    if intro is not None:
        return 0.0
    if not src_duration:
        return INTRO_SKIP
    off = min(max(INTRO_SKIP, 0.12 * src_duration), max(0.0, src_duration - min(length, src_duration) - 0.3))
    return max(0.0, min(off, src_duration - 2.5))


def precheck(url: str, length: float, src_duration: float, work: Path, tag: str, intro: float | None = None) -> dict:
    """Read 3 frames straight from the source (fast seek) and reject dark / text-heavy footage before encoding."""
    off = window_start(src_duration, length, intro)
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


def reject_style(rep: dict) -> None:
    if rep.get("saturation", 99) < 20 and max(rep.get("lum") or [0]) > 30:
        raise VetError(f"black-and-white / sepia archival look (colourfulness {rep['saturation']})")
    if rep.get("black_band"):
        raise VetError("black stripe across the picture")


def relevant(c: dict, frames: list, rep: dict) -> None:
    """CLIP check against the scene's own descriptions (see relevance.py); skipped if CLIP is absent."""
    import relevance
    r = relevance.judge(frames, c.get("scene_queries") or [c.get("query", "")], c.get("scene_must") or [])
    rep["relevance"] = r
    if not r["ok"]:
        raise VetError(f"not what the scene shows (relevance {r['relevance']}, looks like «{r['best_label']}»)")


def build_scene(c: dict, length: float, work: Path, tag: str, move: int) -> tuple[Path, dict]:
    """Returns (segment, report). Raises VetError when the candidate must not be used."""
    url = media_url(c)
    if c["kind"] == "image":
        img, _ = download(url, work / f"{tag}_src.img", limit=40 * 1024 * 1024)
        with Image.open(img) as im:
            if min(im.size) < 600:
                raise VetError(f"image too small {im.size}")
            im.convert("RGB").save(work / f"{tag}_src.jpg", quality=93)
        rep = check([work / f"{tag}_src.jpg"])
        if rep["dark_ratio"] >= 0.5:
            raise VetError("dark image")
        if len(rep["latin_words"]) >= 3 or rep["watermarks"]:
            raise VetError(f"text/logo in image: {rep['latin_words'][:5]}")
        reject_style(rep)
        relevant(c, [work / f"{tag}_src.jpg"], rep)
        seg = ken_burns(work / f"{tag}_src.jpg", length, work, tag, move)
        if detect(seg, "freezedetect=n=0.002:d=2.0", r"freeze_duration: ([\d.]+)"):
            raise VetError("featureless image (looks frozen even with camera move)")
        rep["treatment"] = "ken-burns photo"
        return seg, rep
    if url.startswith("https://"):  # work on a local copy: seeking over HTTP triggers rate limits
        try:
            local, partial = download(url, work / f"{tag}_src.vid", limit=45 * 1024 * 1024, budget=60, attempts=1)
        except VetError as e:
            if "HTTP 429" not in str(e) or not c.get("alt_url"):
                raise
            # uncached transcodes are throttled; the original file is served from cache (partial start is enough)
            local, partial = download(c["alt_url"], work / f"{tag}_src.vid", limit=45 * 1024 * 1024, budget=60, attempts=1)
        url = str(remux_partial(local, work, tag) if partial else local)
    info = probe(url)
    if info["height"] and min(info["width"], info["height"]) < 360:
        raise VetError("video resolution too low")
    intro = 0.0 if c.get("generated") else None
    pre = precheck(url, length, info["duration"], work, tag, intro)
    if pre["dark_ratio"] >= 0.5:
        raise VetError(f"black/dark footage {pre['lum']}")
    if len(pre["latin_words"]) >= 3 or pre["watermarks"]:
        raise VetError(f"text/logo in footage: {(pre['watermarks'] or pre['latin_words'])[:5]}")
    reject_style(pre)
    relevant(c, sorted(work.glob(f"{tag}_pre*.png")), pre)
    seg = video_segment(url, length, work, tag, info["duration"], intro)
    frames = sample_frames(seg, work, tag)
    rep = check(frames)
    rep["relevance"] = pre.get("relevance")
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
