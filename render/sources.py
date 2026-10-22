"""Find freely licensed footage for one scene: Wikimedia Commons (videos and photos) and the NASA
Image and Video Library. No API keys. Licence filter: public domain, CC0 and CC BY only
(no SA/NC/ND). Every candidate keeps its author, licence and page for the YouTube credits."""
from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request

UA = "ulum-blahjetna-render/2.0 (educational YouTube Shorts; github.com/mshehadeh102-dotcom/3loom-render)"
COMMONS = "https://commons.wikimedia.org/w/api.php"
NASA = "https://images-api.nasa.gov/search"
# words in a file title that usually mean text-heavy or non-footage material
BAD_TITLE = re.compile(r"\b(diagram|map|chart|graph|logo|infographic|poster|schematic|label(?:l?ed)?|annotated|"
                       r"text|title|caption|slide|lecture|interview|presentation|cover|stamp|banknote|coin|"
                       r"flag|portrait|signature|drawing|sketch|illustration of)\b", re.I)


def _get(url: str, params: dict, timeout: int = 30) -> dict:
    q = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(q, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _strip(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html.unescape(str(s or "")))).strip()


def licence_ok(lic: str) -> bool:
    l = lic.lower().strip()
    if not l:
        return False
    if re.search(r"\b(sa|nc|nd)\b|share.?alike|noncommercial|no.?deriv", l):
        return False
    return bool(re.match(r"^(cc0|public domain|pd|cc[ -]by(?:[ -]\d(\.\d)?)?$|cc[ -]by[ -]\d)", l))


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", s.lower()))


def score(cand: dict, must: list[str], queries: list[str], avoid: list[str]) -> float:
    text = (cand.get("title", "") + " " + cand.get("desc", "")).lower()
    title = cand.get("title", "").lower()
    s = 0.0
    for m in must:
        m = m.lower().strip()
        if m and m in title:
            s += 3
        elif m and m in text:
            s += 1.5
    qtok = set().union(*[_tokens(q) for q in queries]) if queries else set()
    s += 0.5 * len(qtok & _tokens(title))
    for a in avoid:
        a = a.lower().strip()
        if a and a in text:
            s -= 6
    if BAD_TITLE.search(cand.get("title", "")):
        s -= 4
    if cand["kind"] == "video":
        s += 1.0  # real motion beats a still when equally relevant
        if cand.get("duration", 0) and cand["duration"] < 4:
            s -= 3
    if min(cand.get("width", 0), cand.get("height", 0)) < 480:
        s -= 3
    return s


def commons_videos(query: str, limit: int = 12) -> list[dict]:
    d = _get(COMMONS, {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
                       "gsrsearch": f"filetype:video {query}", "gsrlimit": limit, "prop": "videoinfo",
                       "viprop": "url|size|mime|derivatives|extmetadata",
                       "viextmetadatafilter": "LicenseShortName|Artist|ImageDescription"})
    out = []
    for p in (d.get("query", {}).get("pages", {}) or {}).values():
        vi = (p.get("videoinfo") or [{}])[0]
        meta = vi.get("extmetadata") or {}
        lic = _strip((meta.get("LicenseShortName") or {}).get("value"))
        if not licence_ok(lic) or not vi.get("url"):
            continue
        url = vi["url"]
        best = None
        for der in vi.get("derivatives") or []:  # prefer a ~720p transcode: smaller, still sharp
            h = int(der.get("height") or 0)
            if 600 <= h <= 1100 and (best is None or abs(h - 720) < abs(int(best.get("height") or 0) - 720)):
                best = der
        if best and best.get("src"):
            url = best["src"]
        out.append({"kind": "video", "source": "Wikimedia Commons", "url": url, "title": p.get("title", "")[5:],
                    "desc": _strip((meta.get("ImageDescription") or {}).get("value"))[:300],
                    "license": lic, "author": _strip((meta.get("Artist") or {}).get("value"))[:120] or "unknown",
                    "page": vi.get("descriptionurl", ""), "width": int(vi.get("width") or 0),
                    "height": int(vi.get("height") or 0), "duration": float(vi.get("duration") or 0)})
    return out


def commons_images(query: str, limit: int = 12) -> list[dict]:
    d = _get(COMMONS, {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
                       "gsrsearch": f"filetype:bitmap {query}", "gsrlimit": limit, "prop": "imageinfo",
                       "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1600,
                       "iiextmetadatafilter": "LicenseShortName|Artist|ImageDescription"})
    out = []
    for p in (d.get("query", {}).get("pages", {}) or {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata") or {}
        lic = _strip((meta.get("LicenseShortName") or {}).get("value"))
        if not licence_ok(lic) or not (ii.get("thumburl") or ii.get("url")):
            continue
        if ii.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
            continue
        out.append({"kind": "image", "source": "Wikimedia Commons", "url": ii.get("thumburl") or ii["url"],
                    "title": p.get("title", "")[5:], "license": lic,
                    "desc": _strip((meta.get("ImageDescription") or {}).get("value"))[:300],
                    "author": _strip((meta.get("Artist") or {}).get("value"))[:120] or "unknown",
                    "page": ii.get("descriptionurl", ""), "width": int(ii.get("width") or 0),
                    "height": int(ii.get("height") or 0)})
    return out


def nasa(query: str, media: str, limit: int = 8) -> list[dict]:
    d = _get(NASA, {"q": query, "media_type": media, "page_size": limit})
    out = []
    for it in (d.get("collection", {}).get("items") or [])[:limit]:
        data = (it.get("data") or [{}])[0]
        nid = data.get("nasa_id")
        if not nid or not it.get("href"):
            continue
        out.append({"kind": "video" if media == "video" else "image", "source": "NASA",
                    "url": it["href"].replace("http://", "https://", 1),  # collection.json, resolved later
                    "manifest": True, "title": data.get("title", ""), "desc": _strip(data.get("description"))[:300],
                    "license": "NASA media (public domain; no NASA endorsement implied)",
                    "author": data.get("center") or "NASA", "page": f"https://images.nasa.gov/details/{urllib.parse.quote(nid)}",
                    "width": 1280, "height": 720})
    return out


def find_candidates(scene: dict, log=print) -> list[dict]:
    """All candidates for a scene, best first. Network errors on one source never stop the others."""
    queries = [q for q in (scene.get("queries") or []) if q][:3] or [scene.get("text", "")[:60]]
    must, avoid = scene.get("must") or [], scene.get("avoid") or []
    seen, cands = set(), []
    for q in queries:
        for fn in (lambda: commons_videos(q), lambda: nasa(q, "video"), lambda: commons_images(q), lambda: nasa(q, "image")):
            try:
                for c in fn():
                    key = c.get("page") or c["url"]
                    if key in seen:
                        continue
                    seen.add(key)
                    c["score"] = round(score(c, must, queries, avoid), 2)
                    c["query"] = q
                    cands.append(c)
            except Exception as e:  # noqa: BLE001 - keep searching other sources
                log(f"search error ({q}): {type(e).__name__}: {e}")
    cands = [c for c in cands if c["score"] > 0]
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands
