"""Regression checks for Arabic subtitle timeline (stdlib only)."""
import importlib.util
from pathlib import Path
import sys
import types

# Pillow is installed by the render workflow; the timeline itself needs no images.
if "PIL" not in sys.modules:
    try:
        import PIL  # noqa: F401
    except ImportError:
        pil = types.ModuleType("PIL")
        for name in ("Image", "ImageDraw", "ImageFont"):
            setattr(pil, name, types.ModuleType("PIL." + name))
        sys.modules["PIL"] = pil

module_path = Path(__file__).resolve().parents[1] / "render" / "render.py"
spec = importlib.util.spec_from_file_location("video_render", module_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def check(parts, duration):
    result = mod.timeline(parts, duration)
    assert len(result) == len(parts)
    assert result[0][0] == 0.0
    assert abs(result[-1][1] - duration) < 0.002
    for i, (start, end, _) in enumerate(result):
        assert end >= start, (start, end)
        assert end <= duration + 0.002
        if i:
            assert abs(start - result[i - 1][1]) <= 0.002


def test_short_audio_many_phrases():
    check(["مرحبا"] * 12, 3.0)


def test_normal_audio():
    check(["كيف تتشكل البراكين", "ترتفع الصهارة", "ثم تخرج"], 12.0)


def test_empty():
    assert mod.timeline([], 10.0) == []
    assert mod.timeline(["مرحبا"], 0.0) == []


if __name__ == "__main__":
    test_short_audio_many_phrases()
    test_normal_audio()
    test_empty()
    print("subtitle timeline checks passed")
