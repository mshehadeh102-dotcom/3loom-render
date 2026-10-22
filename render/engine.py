"""Scene engine v2 for «علوم بلهجتنا».

job -> voice (loudnorm) -> word timings (ElevenLabs alignment) -> scenes timed by their sentences ->
per-scene footage search + vetting (Commons/NASA/Flow; black, text/logo, still checks) or a generated
motion-graphics scene -> crossfades -> overlay track (hook title, word-highlight captions, fact
cards, end card) + progress bar -> final MP4 -> QC gate -> contact sheet.
Everything runs on a CPU GitHub Actions runner with free sources; nothing is published.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import align
import sources
import textfx
import vet

FPS = 30
W, H = 1080, 1920
XF = 0.4                 # crossfade seconds
MIN_SCENE = 2.6          # seconds
MAX_TRIES = 8            # candidates tried per scene
TRANSITIONS = ["fade", "smoothleft", "fade", "smoothup", "fade", "circleopen"]


class RenderError(RuntimeError):
    pass


import threading  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

LOCK = threading.Lock()
WORKERS = int(os.environ.get("SCENE_WORKERS", "3"))


def log(msg: str) -> None:
    print(f"[engine {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sh(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RenderError(f"{cmd[0]} failed: {p.stderr[-1200:]}")
    return p


def duration(path: Path) -> float:
    return vet.probe(str(path))["duration"]


def font_path() -> str:
    env = os.environ.get("CAPTION_FONT")
    if env and Path(env).is_file():
        return env
    p = subprocess.run(["fc-list", ":lang=ar", "file", "family", "style"], capture_output=True, text=True)
    fonts = [l for l in p.stdout.splitlines() if l.strip()]
    for want in ("Noto Kufi Arabic", "Noto Sans Arabic", "Noto Naskh Arabic"):
        hits = [f for f in fonts if want in f]
        bold = [f for f in hits if "Bold" in f and "Semi" not in f and "Extra" not in f]
        for f in bold + hits:
            path = f.split(":", 1)[0]
            if Path(path).is_file():
                return path
    if fonts:
        return fonts[0].split(":", 1)[0]
    raise RenderError("no Arabic font found")


def normalize_voice(raw: Path, out: Path, lufs: float) -> None:
    """Two-pass EBU R128 loudnorm so the result really lands on the target (one pass undershoots speech)."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(raw), "-af", f"loudnorm=I={lufs}:TP=-1.5:LRA=11:print_format=json",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", p.stderr, re.S)
    af = f"loudnorm=I={lufs}:TP=-1.5:LRA=11"
    if m:
        d = json.loads(m.group(0))
        af += (f":measured_I={d['input_i']}:measured_TP={d['input_tp']}:measured_LRA={d['input_lra']}"
               f":measured_thresh={d['input_thresh']}:offset={d['target_offset']}:linear=true")
    sh(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-af", af + ",aresample=48000", "-c:a", "aac", "-b:a", "192k", str(out)])


# ---------------------------------------------------------------- scenes
def split_sentences(script: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!؟?…])\s+|(?<=\.\.)\s*", script) if p.strip()]
    return parts or [script]


def default_scenes(job: dict) -> list[dict]:
    """Old jobs (no scene plan): group sentences into ~5 scenes and reuse visual keywords / clips."""
    sents = split_sentences(job.get("script", ""))
    n = max(1, min(6, len(sents)))
    per = -(-len(sents) // n)
    groups = [" ".join(sents[i:i + per]) for i in range(0, len(sents), per)]
    kws = [k.strip() for k in str(job.get("visual_keywords", "")).split("|") if k.strip()]
    clips = [c.get("url") for c in job.get("clips", []) if isinstance(c, dict) and c.get("url")]
    scenes = []
    for i, g in enumerate(groups):
        q = kws[i % len(kws)] if kws else (job.get("title") or "science")
        scenes.append({"text": g, "queries": [q], "must": q.split()[:2], "avoid": [],
                       "clip": clips[i] if i < len(clips) else None})
    return scenes


def time_scenes(scenes: list[dict], words: list[dict], total: float) -> list[dict]:
    """Give each scene [start, end) from the words it contains (scene texts are consecutive script parts)."""
    idx = 0
    for sc in scenes:
        n = len([w for w in sc["text"].split() if align.clean_word(w)])
        first = min(idx, len(words) - 1)
        sc["w0"], sc["w1"] = first, min(len(words), idx + n) - 1
        idx += n
    for i, sc in enumerate(scenes):
        sc["start"] = 0.0 if i == 0 else words[sc["w0"]]["start"]
    for i, sc in enumerate(scenes):
        sc["end"] = scenes[i + 1]["start"] if i + 1 < len(scenes) else total
    # merge scenes that are too short into the previous one
    merged = []
    for sc in scenes:
        if merged and (sc["end"] - sc["start"] < MIN_SCENE or sc["start"] >= sc["end"]):
            merged[-1]["end"] = sc["end"]
            merged[-1]["text"] += " " + sc["text"]
            continue
        merged.append(sc)
    if len(merged) > 1 and merged[0]["end"] - merged[0]["start"] < MIN_SCENE:
        merged[1]["start"] = 0.0
        merged.pop(0)
    for sc in merged:
        sc["dur"] = round(sc["end"] - sc["start"], 3)
    return merged


# ---------------------------------------------------------------- footage
SEARCH_BUDGET = float(os.environ.get("SEARCH_BUDGET", "540"))  # seconds for all footage searching
T0 = {"t": time.monotonic()}


def build_scene_segment(sc: dict, i: int, length: float, work: Path, used: set, report: list) -> dict:
    tried = []
    if sc.get("type") != "graphic":
        cands = []
        for u in [sc.get("clip")] + list(sc.get("flow") or []):
            if u:
                cands.append({"kind": "video", "source": "Google Flow" if "3loom-inputs" in u else "supplied",
                              "url": u, "title": "supplied clip", "license": "supplied", "author": "", "page": u, "score": 99})
        try:
            cands += sources.find_candidates(sc, log)
        except Exception as e:  # noqa: BLE001
            log(f"scene {i + 1}: search failed: {e}")
        throttled = 0
        for c in cands:
            key = c.get("page") or c["url"]
            if len(tried) >= MAX_TRIES or time.monotonic() - T0["t"] > SEARCH_BUDGET:
                break
            if throttled >= 2 and c["kind"] == "video" and c.get("source") == "Wikimedia Commons":
                continue  # this scene's Commons videos keep returning 429: try photos / NASA instead
            with LOCK:
                if key in used:
                    continue
                used.add(key)  # reserve so a parallel scene does not take the same clip
            try:
                seg, rep = vet.build_scene(c, length, work, f"s{i}_{len(tried)}", move=i)
                tried.append({"title": c.get("title"), "ok": True, **rep})
                report.append({"scene": i + 1, "tried": tried})
                return {"segment": seg, "kind": rep.get("treatment"), "source": c.get("source"), "title": c.get("title"),
                        "license": c.get("license"), "author": c.get("author"), "page": c.get("page"), "score": c.get("score")}
            except Exception as e:  # noqa: BLE001
                tried.append({"title": c.get("title"), "ok": False, "reason": str(e)[:160]})
                throttled += "HTTP 429" in str(e)
                log(f"scene {i + 1}: rejected «{c.get('title')}»: {str(e)[:120]}")
    # generated motion-graphics scene
    bg = textfx.gradient_background(i + 7, work / f"g{i}.jpg")
    seg = vet.ken_burns(bg, length, work, f"g{i}", move=i)
    report.append({"scene": i + 1, "tried": tried, "fallback": "graphic"})
    return {"segment": seg, "kind": "graphic", "source": "generated", "title": "motion graphic",
            "license": "original", "author": "علوم بلهجتنا", "page": "", "graphic": True}


def crossfade(segs: list[Path], durs: list[float], out: Path) -> None:
    if len(segs) == 1:
        shutil.copyfile(segs[0], out)
        return
    inputs, parts, last, offset = [], [], "0:v", 0.0
    for p in segs:
        inputs += ["-i", str(p)]
    for k in range(1, len(segs)):
        offset += durs[k - 1]
        nxt = f"x{k}"
        parts.append(f"[{last}][{k}:v]xfade=transition={TRANSITIONS[k % len(TRANSITIONS)]}:duration={XF}:offset={offset:.3f}[{nxt}]")
        last = nxt
    sh(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(parts), "-map", f"[{last}]",
        "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(out)])


# ---------------------------------------------------------------- overlay track
def overlay_track(words: list[dict], scenes: list[dict], hook: str, total: float, work: Path) -> Path:
    lines = align.caption_lines(words)
    line_of = {wi: li for li, l in enumerate(lines) for wi in l}
    hook_end = min(3.2, max(2.0, scenes[0]["end"] - 0.2)) if hook else 0.0
    end_start = max(0.0, total - 2.6)
    cards = [(sc["start"] + 0.25, sc["end"] - 0.15, sc.get("card")) for sc in scenes if sc.get("card")]

    pts = {0.0, total, end_start, hook_end}
    for k in range(1, 5):
        pts.add(round(0.07 * k, 3))  # hook pop-in steps
    for w in words:
        pts.update({w["start"], w["end"]})
    for a, b, _ in cards:
        pts.update({max(0.0, a), max(0.0, b)})
    pts = sorted(p for p in pts if 0.0 <= p <= total)

    def caption_at(t: float):
        cur = None
        for wi, w in enumerate(words):
            if w["start"] <= t:
                cur = wi
            else:
                break
        if cur is None:
            return None
        nxt = words[cur + 1]["start"] if cur + 1 < len(words) else total
        if t >= words[cur]["end"] + 0.9 and t < nxt:  # long pause: hide
            return None
        li = line_of[cur]
        return (li, lines[li].index(cur))

    cache: dict = {}
    entries = []
    for a, b in zip(pts, pts[1:]):
        if b - a < 1e-3:
            continue
        t = a + 1e-4
        cap = caption_at(t)
        hook_p = None if not hook or t >= hook_end else min(1.0, (t + 0.07) / 0.28)
        card = next((c for (s, e, c) in cards if s <= t < e), None)
        end = t >= end_start
        key = (cap, None if hook_p is None else round(hook_p, 2), json.dumps(card, ensure_ascii=False) if card else None, end)
        if key not in cache:
            img = textfx.compose(
                caption=textfx.caption_line([words[i]["word"] for i in lines[cap[0]]], cap[1]) if cap else None,
                hook=textfx.hook_overlay(hook, hook_p) if hook_p is not None else None,
                card=textfx.fact_card(card.get("title", ""), card.get("sub", "")) if card else None,
                end=textfx.end_overlay() if end else None)
            f = work / f"ov{len(cache):04d}.png"
            img.save(f, compress_level=3)
            cache[key] = f
        entries.append((cache[key], b - a))
    lst = work / "overlay.txt"
    with open(lst, "w") as fh:
        fh.write("ffconcat version 1.0\n")
        for f, d in entries:
            fh.write(f"file '{f.name}'\nduration {d:.4f}\n")
        fh.write(f"file '{entries[-1][0].name}'\n")
    return lst


# ---------------------------------------------------------------- QC
def qc(final: Path, bg: Path, voice_dur: float, work: Path) -> dict:
    info = json.loads(sh(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height,r_frame_rate:format=duration",
                          "-of", "json", str(final)]).stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    dur = float(info["format"]["duration"])
    bd = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(bg), "-vf", "blackdetect=d=0.5:pix_th=0.10",
                         "-an", "-f", "null", "-"], capture_output=True, text=True)
    blacks = re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", bd.stderr)
    fz = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(bg), "-vf", "freezedetect=n=0.002:d=2.5",
                         "-an", "-f", "null", "-"], capture_output=True, text=True)
    freezes = re.findall(r"freeze_duration: ([\d.]+)", fz.stderr)
    lo = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(final), "-af", "ebur128=peak=true", "-vn", "-f", "null", "-"],
                        capture_output=True, text=True)
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", lo.stderr)
    tp = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", lo.stderr)
    res = {
        "width": v["width"], "height": v["height"], "fps": v["r_frame_rate"], "vcodec": v["codec_name"],
        "acodec": a["codec_name"] if a else None, "duration": round(dur, 2), "voice_duration": round(voice_dur, 2),
        "black_intervals": [[float(x), float(y)] for x, y in blacks], "freezes_over_2_5s": [float(x) for x in freezes],
        "loudness_lufs": float(m[-1]) if m else None, "true_peak_dbfs": float(tp[-1]) if tp else None,
    }
    problems = []
    if (v["width"], v["height"]) != (W, H):
        problems.append("resolution")
    if v["r_frame_rate"] not in ("30/1",):
        problems.append("fps")
    if not a:
        problems.append("no audio")
    if abs(dur - voice_dur) > 0.5:
        problems.append("duration mismatch")
    if res["black_intervals"]:
        problems.append("black frames")
    if res["freezes_over_2_5s"]:
        problems.append("frozen picture")
    if res["loudness_lufs"] is None or not (-16.5 <= res["loudness_lufs"] <= -12.0):
        problems.append("loudness")
    res["problems"] = problems
    res["passed"] = not problems
    return res


# ---------------------------------------------------------------- main
def render_job(job: dict, out_dir: Path) -> dict:
    t0 = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="eng-"))
    textfx.set_font_file(font_path())
    try:
        import base64
        raw = work / "voice_raw.mp3"
        raw.write_bytes(base64.b64decode(job["audio_b64"]))
        lufs = float(job.get("loudness_lufs", -14))
        voice = work / "voice.m4a"
        normalize_voice(raw, voice, lufs)
        total = duration(voice)
        script = job.get("script", "")
        words, sync = align.word_timings(script, job.get("alignment"), total)
        if not words:
            raise RenderError("script has no words")
        log(f"voice {total:.2f}s, {len(words)} words, sync={sync}")
        import relevance
        log(f"relevance gate: {relevance.status()}")
        T0["t"] = time.monotonic()

        scenes = [dict(s) for s in (job.get("scenes") or [])] or default_scenes(job)
        flow = [u for u in (job.get("flow_clips") or []) if u]
        if flow:
            for i, sc in enumerate(scenes):
                sc["flow"] = [flow[i % len(flow)]]
        scenes = time_scenes(scenes, words, total)
        log("scenes: " + ", ".join(f"{s['start']:.1f}-{s['end']:.1f}" for s in scenes))

        used, report, chosen, segs, durs = set(), [], [], [], []
        lengths = [sc["dur"] + (XF if i < len(scenes) - 1 else 0.0) for i, sc in enumerate(scenes)]
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            picks = list(pool.map(lambda i: build_scene_segment(scenes[i], i, lengths[i], work, used, report), range(len(scenes))))
        report.sort(key=lambda r: r["scene"])
        for i, sc in enumerate(scenes):
            pick = picks[i]
            if pick.get("graphic") and not sc.get("card"):
                g = sc.get("graphic") or {}
                sc["card"] = {"title": g.get("title") or " ".join(sc["text"].split()[:3]), "sub": g.get("sub", "")}
            elif sc.get("type") == "graphic" or sc.get("graphic", {}).get("show"):
                g = sc.get("graphic") or {}
                if g.get("title"):
                    sc["card"] = {"title": g["title"], "sub": g.get("sub", "")}
            chosen.append({k: v for k, v in pick.items() if k != "segment"} | {"start": sc["start"], "end": sc["end"]})
            segs.append(pick["segment"])
            durs.append(sc["dur"])
            log(f"scene {i + 1}: {pick['kind']} — {pick.get('title')}")

        bg = work / "bg.mp4"
        crossfade(segs, durs, bg)
        ov = overlay_track(words, scenes, job.get("hook") or "", total, work)
        final = out_dir / f"{job['job_id']}.mp4"
        flt = (f"[0:v][2:v]overlay=0:0:eof_action=repeat:format=auto[v1];"
               f"color=c=0xFFD54A:s={W}x8:r={FPS}:d={total:.3f}[bar];"
               f"[v1][bar]overlay=x='-w+w*t/{total:.3f}':y=0:shortest=1[v]")
        sh(["ffmpeg", "-y", "-v", "error", "-i", str(bg), "-i", str(voice), "-f", "concat", "-safe", "0", "-i", str(ov),
            "-filter_complex", flt, "-map", "[v]", "-map", "1:a", "-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-maxrate", "6M", "-bufsize", "12M",
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "copy", "-movflags", "+faststart", str(final)], timeout=1500)

        q = qc(final, bg, total, work)
        frames = []
        stamps = [min(1.2, total / 4)] + [(s["start"] + s["end"]) / 2 for s in chosen] + [max(0.0, total - 1.0)]
        for k, t in enumerate(stamps[:12]):
            f = work / f"sheet{k}.png"
            sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(final), "-frames:v", "1", str(f)])
            frames.append(f)
        sheet = textfx.contact_sheet(frames, out_dir / f"{job['job_id']}_sheet.jpg")
        info = {"duration": round(total, 2), "scenes": chosen, "sync": sync, "words": len(words), "qc": q,
                "search_report": report, "sheet": sheet.name, "render_seconds": round(time.time() - t0, 1),
                "runner": {"cpus": os.cpu_count(), "gpu": bool(shutil.which("nvidia-smi"))}}
        (out_dir / "render_report.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        if not q["passed"]:
            raise RenderError("QC_FAILED: " + ", ".join(q["problems"]))
        return info
    finally:
        if not os.environ.get("KEEP_WORK"):
            shutil.rmtree(work, ignore_errors=True)
        else:
            log(f"work kept at {work}")
