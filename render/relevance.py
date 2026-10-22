"""Visual relevance gate: does the picture actually show what the scene talks about?

Title matching alone accepted «Soda Springs» (a stone ring in a forest) for a carbonated-drink
scene. CLIP (open_clip ViT-B-32, OpenAI weights, MIT licence) runs on the runner CPU and
compares each frame with the scene's own descriptions and with a fixed set of distractors
(forest, wood, map, satellite view, text slide ...). The frame passes when the scene prompts
together win at least MIN_SHARE of the probability. If CLIP cannot be loaded the gate is
skipped and the report says so; the render never fails because of it.
"""
from __future__ import annotations

import os
import threading

MIN_SHARE = float(os.environ.get("RELEVANCE_MIN", "0.45"))
DISTRACTORS = [
    "a photo of a forest with trees", "a wooden surface", "a stone wall", "a city street", "people talking",
    "a slide with text", "a map or a diagram", "a satellite image of the earth from space",
    "an old black and white photo", "an empty room", "a logo", "a cartoon drawing",
]
_LOCK = threading.Lock()
_STATE: dict = {}


def _load():
    with _LOCK:
        if "model" in _STATE or "error" in _STATE:
            return _STATE.get("model")
        if os.environ.get("RELEVANCE_OFF"):
            _STATE["error"] = "disabled"
            return None
        try:
            import open_clip
            import torch
            torch.set_num_threads(max(1, (os.cpu_count() or 2) // 2))
            model, _, pre = open_clip.create_model_and_transforms("ViT-B-32", pretrained="openai")
            model.eval()
            _STATE["model"] = (model, pre, open_clip.get_tokenizer("ViT-B-32"), torch)
        except Exception as e:  # noqa: BLE001 - optional component
            _STATE["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        return _STATE.get("model")


def prompts_for(queries: list[str], must: list[str]) -> list[str]:
    seen, out = set(), []
    for t in list(queries)[:3] + list(must)[:3]:
        t = (t or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(f"a photo of {t}")
    return out


def decide(probs: list[float], n_scene: int, labels: list[str]) -> dict:
    """probs: mean softmax over [scene prompts..., distractors...]."""
    share = sum(probs[:n_scene])
    best = max(range(len(probs)), key=lambda i: probs[i])
    return {"relevance": round(share, 3), "best_label": labels[best], "ok": share >= MIN_SHARE}


def judge(frames, queries: list[str], must: list[str]) -> dict:
    scene = prompts_for(queries, must)
    if not scene or not frames:
        return {"relevance": None, "ok": True, "note": "no prompts"}
    m = _load()
    if not m:
        return {"relevance": None, "ok": True, "note": f"gate skipped ({_STATE.get('error')})"}
    model, pre, tok, torch = m
    from PIL import Image
    labels = scene + DISTRACTORS
    with _LOCK, torch.no_grad():
        imgs = torch.stack([pre(Image.open(f).convert("RGB")) for f in frames])
        ie = model.encode_image(imgs)
        te = model.encode_text(tok(labels))
        ie = ie / ie.norm(dim=-1, keepdim=True)
        te = te / te.norm(dim=-1, keepdim=True)
        probs = (100.0 * ie @ te.T).softmax(dim=-1).mean(dim=0).tolist()
    return decide(probs, len(scene), labels)


def status() -> str:
    _load()
    return "clip ViT-B-32" if _STATE.get("model") else f"off ({_STATE.get('error')})"
