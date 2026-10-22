"""GitHub Actions entry point for the «علوم بلهجتنا» render job.

n8n (workflow «3. إنتاج الفيديو») writes jobs/<job_id>.json in the public repo 3loom-render:
  {"job_id", "episode_id", "script", "clips": [{"url": ...}], "audio_b64",
   "loudness_lufs", "callback_url", "nonce"}
then dispatches .github/workflows/render.yml with job_id.

  python run_job.py mask     JOB.json            -> prints ::add-mask:: for the callback url
  python run_job.py render   JOB.json OUT_DIR    -> OUT_DIR/<job_id>.mp4 + OUT_DIR/result.json
  python run_job.py callback JOB.json OUT_DIR    -> POSTs result.json (+ asset id) to the n8n Wait node

No publishing, no paid services: inputs are the narration n8n already produced and
free-licensed NASA / Wikimedia clip URLs.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def load(path: str) -> dict:
    job = json.loads(Path(path).read_text(encoding="utf-8"))
    if not JOB_ID.match(str(job.get("job_id", ""))):
        raise SystemExit("bad job_id")
    return job


def cmd_mask(job: dict) -> None:
    for key in ("callback_url", "nonce"):
        if job.get(key):
            print(f"::add-mask::{job[key]}")


def cmd_render(job: dict, out_dir: Path) -> int:
    """Scene engine v2 (engine.py). Old jobs without a scene plan still work: scenes are derived
    from the script sentences and the supplied clips / visual keywords."""
    import engine

    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"ok": False}
    try:
        lufs = float(job.get("loudness_lufs", -14))
        if not -20 <= lufs <= -9:
            raise engine.RenderError("unsupported loudness")
        info = engine.render_job(job, out_dir)
        video = out_dir / f"{job['job_id']}.mp4"
        credits = [f"{c['source']}: {c.get('title') or ''} — {c.get('author') or ''} — {c.get('license') or ''} — {c.get('page') or ''}"
                   for c in info["scenes"] if c.get("source") not in ("generated",)]
        q = info["qc"]
        result = {"ok": True, "file": video.name, "size": video.stat().st_size, "duration": info["duration"],
                  "clips": len(info["scenes"]), "sync": info["sync"], "words": info["words"],
                  "scene_kinds": [c["kind"] for c in info["scenes"]], "credits": "\n".join(credits),
                  "scene_titles": [str(c.get("title") or "")[:60] for c in info["scenes"]],
                  "rejected": sum(1 for r in info["search_report"] for t in r.get("tried", []) if not t.get("ok")),
                  "qc": {k: q[k] for k in ("passed", "problems", "loudness_lufs", "true_peak_dbfs", "black_intervals",
                                           "freezes_over_2_5s", "width", "height", "fps")},
                  "render_seconds": info["render_seconds"], "runner": info["runner"]}
    except Exception as e:  # reported back to n8n instead of hanging the Wait node
        rep = out_dir / "render_report.json"
        extra = ""
        if rep.exists():
            try:
                extra = " | qc=" + json.dumps(json.loads(rep.read_text(encoding="utf-8")).get("qc", {}), ensure_ascii=False)[:600]
            except Exception:  # noqa: BLE001
                pass
        result = {"ok": False, "error": (f"{type(e).__name__}: {e}" + extra)[:1500]}
    (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


def cmd_callback(job: dict, out_dir: Path) -> int:
    rf = out_dir / "result.json"
    result = json.loads(rf.read_text(encoding="utf-8")) if rf.exists() else {
        "ok": False, "error": "render step did not finish (see GitHub Actions log)"}
    asset = os.environ.get("ASSET_ID", "").strip()
    if result.get("ok") and not asset:
        result = {"ok": False, "error": "video rendered but upload to release failed"}
    payload = {"job_id": job["job_id"], "episode_id": job.get("episode_id"), "nonce": job.get("nonce", ""),
               "asset_id": asset, "sheet_asset_id": os.environ.get("SHEET_ASSET_ID", "").strip(),
               "run_url": os.environ.get("RUN_URL", ""), **result}
    url = job.get("callback_url", "")
    if not url.startswith("https://"):
        print("no https callback_url; nothing sent")
        return 1
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "3loom-render-actions/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        print("callback HTTP", r.status)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in {"mask", "render", "callback"}:
        print(__doc__)
        return 2
    job = load(argv[1])
    if argv[0] == "mask":
        cmd_mask(job)
        return 0
    out_dir = Path(argv[2])
    return cmd_render(job, out_dir) if argv[0] == "render" else cmd_callback(job, out_dir)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
