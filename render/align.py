"""Word timings for Arabic captions.

Primary source: ElevenLabs `/with-timestamps` character alignment, which n8n now passes in the
job as `alignment` ({characters, character_start_times_seconds, character_end_times_seconds}).
The characters are exactly the text ElevenLabs spoke, so grouping them on whitespace gives
word timings. Fallback: proportional timing by word length (the old behaviour).
"""
from __future__ import annotations

import re

PUNCT = "«»\"'“”.,،؛;:!؟?…()[]-–—"
SENT_END = re.compile(r"[.!؟?…]+$|\.\.$")


def clean_word(w: str) -> str:
    return w.strip(PUNCT + " ")


def words_from_alignment(alignment: dict | None) -> list[dict]:
    """Group per-character timings into words. Returns [{word, raw, start, end}]."""
    if not alignment:
        return []
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    if not chars or len(chars) != len(starts) or len(chars) != len(ends):
        return []
    words, buf, b_start, b_end = [], [], None, None
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if buf:
                words.append(("".join(buf), b_start, b_end))
            buf, b_start, b_end = [], None, None
            continue
        if b_start is None:
            b_start = float(s)
        buf.append(ch)
        b_end = float(e)
    if buf:
        words.append(("".join(buf), b_start, b_end))
    out = []
    for raw, s, e in words:
        w = clean_word(raw)
        if not w:  # pure punctuation: extend previous word
            if out:
                out[-1]["raw"] += raw
                out[-1]["end"] = max(out[-1]["end"], e)
            continue
        out.append({"word": w, "raw": raw, "start": round(s, 3), "end": round(max(e, s + 0.05), 3)})
    return out


def words_proportional(script: str, total: float) -> list[dict]:
    raws = [r for r in script.split() if clean_word(r)]
    if not raws:
        return []
    weights = [max(len(clean_word(r)), 2) + (3 if SENT_END.search(r) else 0) for r in raws]
    scale = total / sum(weights)
    t, out = 0.0, []
    for r, w in zip(raws, weights):
        d = w * scale
        out.append({"word": clean_word(r), "raw": r, "start": round(t, 3), "end": round(t + d * 0.92, 3)})
        t += d
    return out


def word_timings(script: str, alignment: dict | None, total: float) -> tuple[list[dict], str]:
    words = words_from_alignment(alignment)
    if words and len(words) >= 0.8 * len([r for r in script.split() if clean_word(r)]):
        # clamp to the real audio length (loudnorm can shift a few ms)
        for w in words:
            w["start"] = min(w["start"], total)
            w["end"] = min(w["end"], total)
        return words, "elevenlabs-alignment"
    return words_proportional(script, total), "proportional"


def caption_lines(words: list[dict], max_words: int = 4, max_chars: int = 24) -> list[list[int]]:
    """Split word indices into short caption lines, breaking after sentence/comma punctuation."""
    lines, cur, chars = [], [], 0
    for i, w in enumerate(words):
        if cur and (len(cur) >= max_words or chars + len(w["word"]) > max_chars):
            lines.append(cur)
            cur, chars = [], 0
        cur.append(i)
        chars += len(w["word"]) + 1
        if re.search(r"[.!؟?…،,:]+[»\"']?$", w["raw"]):
            lines.append(cur)
            cur, chars = [], 0
    if cur:
        lines.append(cur)
    return lines
