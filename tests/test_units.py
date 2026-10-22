"""Fast offline checks for the scene engine (python3 tests/test_units.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "render"))
import align  # noqa: E402
import engine  # noqa: E402
import sources  # noqa: E402


def alignment_for(text, step=0.1):
    chars, st, en, t = [], [], [], 0.0
    for ch in text:
        chars.append(ch); st.append(t); en.append(t + step); t += step
    return {"characters": chars, "character_start_times_seconds": st, "character_end_times_seconds": en}


def test_words_from_alignment():
    w = align.words_from_alignment(alignment_for("بتعرف «الماجما»؟ صخر ذايب."))
    assert [x["word"] for x in w] == ["بتعرف", "الماجما", "صخر", "ذايب"], w
    assert w[0]["start"] == 0.0 and w[1]["start"] > w[0]["end"]


def test_word_timings_fallback():
    words, mode = align.word_timings("كلمة ثانية ثالثة", None, 3.0)
    assert mode == "proportional" and len(words) == 3 and words[-1]["end"] <= 3.0


def test_caption_lines_break_on_punct():
    words = align.words_proportional("واحد اثنين، ثلاثة أربعة خمسة ستة سبعة.", 5.0)
    lines = align.caption_lines(words)
    assert lines[0] == [0, 1] and all(len(l) <= 4 for l in lines), lines


def test_licence_filter():
    ok = ["CC0", "Public domain", "CC BY 4.0", "CC BY 2.0", "cc-by-3.0"]
    bad = ["CC BY-SA 4.0", "CC BY-NC 2.0", "CC BY-ND 4.0", "", "All rights reserved"]
    assert all(sources.licence_ok(x) for x in ok), [x for x in ok if not sources.licence_ok(x)]
    assert not any(sources.licence_ok(x) for x in bad), [x for x in bad if sources.licence_ok(x)]


def test_score_prefers_relevant_and_penalises_avoid():
    a = {"kind": "video", "title": "Kilauea lava fountain eruption", "desc": "", "width": 1280, "height": 720, "duration": 20}
    b = {"kind": "video", "title": "Atmospheric layers diagram", "desc": "", "width": 1280, "height": 720, "duration": 20}
    must, q, avoid = ["lava", "eruption"], ["volcano lava eruption"], ["atmospher", "diagram"]
    assert sources.score(a, must, q, avoid) > 0 > sources.score(b, must, q, avoid)


def test_time_scenes_follow_words():
    script = "جملة أولى قصيرة هون. جملة ثانية أطول شوي من الأولى. وجملة ثالثة بتختم الكلام كله."
    words, _ = align.word_timings(script, alignment_for(script, 0.08), 7.0)
    sents = engine.split_sentences(script)
    scenes = engine.time_scenes([{"text": s} for s in sents], words, 7.0)
    assert scenes[0]["start"] == 0.0 and abs(scenes[-1]["end"] - 7.0) < 1e-6
    assert all(s["end"] > s["start"] for s in scenes)


def test_style_checks(tmp=Path("/tmp/3loom_style_test")):
    import vet
    from PIL import Image, ImageDraw
    tmp.mkdir(exist_ok=True)
    sep = Image.effect_noise((540, 960), 50).convert("L").point(lambda v: v // 2 + 90)
    Image.merge("RGB", [sep.point(lambda v: min(255, v + 25)), sep.point(lambda v: v + 8), sep]).save(tmp / "sepia.png")
    col = Image.new("RGB", (540, 960))
    d = ImageDraw.Draw(col)
    for i in range(0, 960, 40):
        d.rectangle((0, i, 540, i + 40), fill=((i * 3) % 255, (i * 7) % 255, (255 - i) % 255))
    col.save(tmp / "colour.png")
    band = col.copy()
    ImageDraw.Draw(band).rectangle((0, 400, 540, 470), fill=(0, 0, 0))
    band.save(tmp / "band.png")
    assert vet.saturation(tmp / "sepia.png") < 20 < vet.saturation(tmp / "colour.png")
    assert vet.black_band(tmp / "band.png") and not vet.black_band(tmp / "colour.png")


def test_relevance_decision():
    import relevance
    labels = relevance.prompts_for(["soda fizzing"], ["soda", "bubbles"]) + relevance.DISTRACTORS
    n = 3
    good = [0.3, 0.2, 0.15] + [0.35 / len(relevance.DISTRACTORS)] * len(relevance.DISTRACTORS)
    bad = [0.05, 0.1, 0.05, 0.7] + [0.1 / (len(relevance.DISTRACTORS) - 1)] * (len(relevance.DISTRACTORS) - 1)
    assert relevance.decide(good, n, labels)["ok"]
    r = relevance.decide(bad, n, labels)
    assert not r["ok"] and "forest" in r["best_label"], r
    assert relevance.prompts_for(["a", "A", ""], ["b"]) == ["a photo of a", "a photo of b"]


def test_generated_clip_starts_at_zero():
    import vet
    assert vet.window_start(5.0, 6.0, intro=0.0) == 0.0
    assert vet.window_start(60.0, 6.0) >= vet.INTRO_SKIP


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); n += 1; print("PASS", name)
    print(f"{n} tests passed")
