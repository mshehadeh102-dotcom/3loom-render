"""Arabic on-screen graphics for «علوم بلهجتنا»: word-highlight captions, hook title, fact cards,
end card and generated backgrounds. Pillow with libraqm shapes the Arabic; every word is shaped
on its own and colour only changes at word boundaries, so letter joining is never broken."""
from __future__ import annotations

import math
import random
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
SAFE_X = 70
CAPTION_CENTER_Y = 1330
HOOK_Y = 300
WHITE = (255, 255, 255, 255)
ACCENT = (255, 213, 74, 255)      # yellow highlight
TEAL = (25, 195, 180, 255)
NAVY = (11, 30, 58)
SHADOW = (0, 0, 0, 255)

_FONT_FILE: str | None = None


def set_font_file(path: str) -> None:
    global _FONT_FILE
    _FONT_FILE = path
    _font.cache_clear()


@lru_cache(maxsize=64)
def _font(size: int) -> ImageFont.FreeTypeFont:
    if not _FONT_FILE:
        raise RuntimeError("font not configured")
    f = ImageFont.truetype(_FONT_FILE, size)
    try:  # variable fonts (Noto Kufi Arabic[wght]) -> bold
        f.set_variation_by_name("Bold")
    except Exception:  # noqa: BLE001 - static fonts have no variations
        pass
    return f


SYMBOLS = {"=": ":", "→": "،", "←": "،", "≈": "تقريبًا", "°": "°"}


def _text_img(text: str, size: int, fill=WHITE, stroke=6, stroke_fill=SHADOW) -> Image.Image:
    for a, b in SYMBOLS.items():
        text = text.replace(a, b)
    f = _font(size)
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    box = probe.textbbox((0, 0), text, font=f, direction="rtl", language="ar", stroke_width=stroke)
    w, h = box[2] - box[0] + 4, box[3] - box[1] + 4
    img = Image.new("RGBA", (max(w, 1), max(h, 1)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text((2 - box[0], 2 - box[1]), text, font=f, fill=fill, direction="rtl", language="ar",
           stroke_width=stroke, stroke_fill=stroke_fill)
    return img


def _line_metrics(size: int) -> tuple[int, int]:
    f = _font(size)
    asc, desc = f.getmetrics()
    return asc, desc


def caption_line(words: list[str], active: int | None, size: int = 82, max_w: int = W - 2 * SAFE_X) -> Image.Image:
    """One caption line, words laid out right-to-left; `active` word is yellow and slightly larger."""
    while True:
        space = int(size * 0.32)
        imgs = []
        for i, w in enumerate(words):
            if i == active:
                imgs.append(_text_img(w, int(size * 1.1), fill=ACCENT, stroke=7))
            else:
                imgs.append(_text_img(w, size, fill=WHITE, stroke=6))
        total = sum(im.width for im in imgs) + space * (len(imgs) - 1)
        if total <= max_w or size <= 56:
            break
        size -= 6
    hgt = max(im.height for im in imgs)
    pad_x, pad_y = 34, 22
    canvas = Image.new("RGBA", (total + 2 * pad_x, hgt + 2 * pad_y), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((0, 0, canvas.width - 1, canvas.height - 1), radius=30, fill=(8, 12, 24, 150))
    x = canvas.width - pad_x
    for im in imgs:  # first word on the right
        x -= im.width
        canvas.alpha_composite(im, (x, pad_y + (hgt - im.height) // 2))
        x -= space
    return canvas


def wrap_words(text: str, size: int, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], []
    for w in words:
        trial = " ".join(cur + [w])
        if cur and _text_img(trial, size, stroke=0).width > max_w:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    return lines


def text_block(text: str, size: int, fill=WHITE, max_w: int = W - 2 * SAFE_X, stroke: int = 7,
               gap: float = 0.18) -> Image.Image:
    while True:
        lines = wrap_words(text, size, max_w)
        if len(lines) <= 3 or size <= 52:
            break
        size -= 6
    imgs = [_text_img(l, size, fill=fill, stroke=stroke) for l in lines]
    g = int(size * gap)
    out = Image.new("RGBA", (max(i.width for i in imgs), sum(i.height for i in imgs) + g * (len(imgs) - 1)), (0, 0, 0, 0))
    y = 0
    for im in imgs:
        out.alpha_composite(im, ((out.width - im.width) // 2, y))
        y += im.height + g
    return out


def hook_overlay(text: str, progress: float) -> Image.Image:
    """Big hook title near the top; `progress` 0..1 drives a pop-in scale."""
    base = text_block(text, 92, fill=WHITE, stroke=8)
    scale = 0.82 + 0.18 * min(1.0, progress) ** 0.5
    im = base.resize((max(1, int(base.width * scale)), max(1, int(base.height * scale))), Image.LANCZOS)
    band = Image.new("RGBA", (W, im.height + 70), (0, 0, 0, 0))
    d = ImageDraw.Draw(band)
    d.rounded_rectangle((40, 0, W - 40, band.height - 1), radius=36, fill=(11, 30, 58, int(185 * min(1.0, progress * 1.5))))
    d.rectangle((W // 2 - 90, band.height - 10, W // 2 + 90, band.height - 4), fill=ACCENT)
    band.alpha_composite(im, ((W - im.width) // 2, 30))
    return band


def fact_card(title: str, sub: str = "") -> Image.Image:
    """Centred card used on graphic scenes (e.g. «الماجما = صخر ذايب»)."""
    t = text_block(title, 104, fill=ACCENT, stroke=8)
    s = text_block(sub, 64, fill=WHITE, stroke=6) if sub else None
    w = min(W - 80, max(t.width, s.width if s else 0) + 120)
    h = t.height + (s.height + 40 if s else 0) + 120
    card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=44, fill=(11, 30, 58, 215), outline=TEAL, width=6)
    card.alpha_composite(t, ((w - t.width) // 2, 60))
    if s:
        card.alpha_composite(s, ((w - s.width) // 2, 60 + t.height + 40))
    return card


def end_overlay(channel: str = "علوم بلهجتنا") -> Image.Image:
    """Channel badge only: the spoken outro is already on screen as captions, so no repeated sentence."""
    b = text_block(channel, 96, fill=ACCENT, stroke=8)
    w, h = b.width + 120, b.height + 90
    card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=40, fill=(11, 30, 58, 205), outline=ACCENT, width=5)
    card.alpha_composite(b, ((w - b.width) // 2, 45))
    return card


def compose(caption: Image.Image | None = None, hook: Image.Image | None = None,
            card: Image.Image | None = None, end: Image.Image | None = None) -> Image.Image:
    frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if hook is not None:
        frame.alpha_composite(hook, (0, HOOK_Y))
    if card is not None:
        frame.alpha_composite(card, ((W - card.width) // 2, (H - card.height) // 2 - 160))
    if end is not None:
        frame.alpha_composite(end, ((W - end.width) // 2, 700))
    if caption is not None:
        frame.alpha_composite(caption, ((W - caption.width) // 2, CAPTION_CENTER_Y - caption.height // 2))
    return frame


def gradient_background(seed: int, path: Path) -> Path:
    """Generated motion-graphics background (dark navy→teal with soft glows); animated later by zoompan."""
    rnd = random.Random(seed)
    big_w, big_h = 1300, 2310
    img = Image.new("RGB", (big_w, big_h), NAVY)
    d = ImageDraw.Draw(img)
    top, bottom = (8, 22, 48), (14, 92, 104) if seed % 2 else (60, 24, 70)
    for y in range(big_h):
        k = y / big_h
        d.line([(0, y), (big_w, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * k) for i in range(3)))
    glow = Image.new("RGBA", (big_w, big_h), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    for _ in range(14):
        r = rnd.randint(60, 260)
        x, y = rnd.randint(0, big_w), rnd.randint(0, big_h)
        g.ellipse((x - r, y - r, x + r, y + r), fill=(80, 220, 210, rnd.randint(25, 60)))
    for _ in range(160):
        x, y = rnd.randint(0, big_w), rnd.randint(0, big_h)
        s = rnd.choice([2, 2, 3, 4])
        g.ellipse((x, y, x + s, y + s), fill=(255, 255, 255, rnd.randint(90, 200)))
    glow = glow.filter(ImageFilter.GaussianBlur(6))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    img.save(path, quality=92)
    return path


def contact_sheet(frames: list[Path], out: Path, labels: list[str] | None = None) -> Path:
    thumbs = [Image.open(p).convert("RGB").resize((270, 480)) for p in frames]
    cols = min(len(thumbs), 6)
    rows = math.ceil(len(thumbs) / cols)
    sheet = Image.new("RGB", (cols * 270 + (cols + 1) * 8, rows * 480 + (rows + 1) * 8), (20, 20, 20))
    for i, t in enumerate(thumbs):
        sheet.paste(t, (8 + (i % cols) * 278, 8 + (i // cols) * 488))
    sheet.save(out, quality=88)
    return out
