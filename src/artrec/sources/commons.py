"""Wikimedia Commons curated categories (Doré, Blake series etc.). Heavily throttled."""
from __future__ import annotations

import html
import re
from urllib.parse import quote

from ..net import RateLimited
from . import Artwork

API = "https://commons.wikimedia.org/w/api.php"
OK_MIME = {"image/jpeg", "image/png", "image/tiff"}


def _strip(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def _members(http, cat: str, depth: int):
    params = {"action": "query", "format": "json", "generator": "categorymembers", "gcmtitle": cat,
              "gcmtype": "file|subcat", "gcmlimit": 50, "prop": "imageinfo",
              "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 330,
              "iiextmetadatafilter": "Artist|DateTimeOriginal|ObjectName|ImageDescription|LicenseShortName"}
    cont = {}
    while True:
        data = http.get_json(API, params={**params, **cont}, ttl_days=30)
        for p in (data.get("query", {}).get("pages", {}) or {}).values():
            if p.get("ns") == 14 and depth > 0:
                yield from _members(http, p["title"], depth - 1)
            elif p.get("ns") == 6 and p.get("imageinfo"):
                yield p
        if "continue" not in data:
            break
        cont = {k: v for k, v in data["continue"].items()}


def _year(s: str) -> int | None:
    m = re.search(r"\b(1[0-9]{3})\b", s or "")
    return int(m.group(1)) if m else None


def harvest(profile, matcher, http, log) -> list[Artwork]:
    out = {}
    for cat in profile.sources.get("commons_categories", []):
        n = 0
        try:
            for p in _members(http, cat, depth=1):
                ii = p["imageinfo"][0]
                if ii.get("mime") not in OK_MIME:
                    continue
                em = {k: _strip(v.get("value", "")) for k, v in (ii.get("extmetadata") or {}).items()}
                name = p["title"].removeprefix("File:")
                title = em.get("ObjectName") or re.sub(r"\.(jpe?g|png|tiff?)$", "", name, flags=re.I).replace("_", " ")
                w, h = ii.get("width"), ii.get("height")
                big = (w or 0) > 4000 or ii.get("mime") == "image/tiff"
                full = (ii["thumburl"].replace("/330px-", "/3840px-") if big and ii.get("thumburl") else ii["url"])
                if ii.get("mime") == "image/tiff" and full.endswith((".tif", ".tiff")):
                    full += ".jpg"
                art = Artwork(
                    source="commons", source_id=name, title=title[:300], artist=em.get("Artist", "")[:200],
                    year=_year(em.get("DateTimeOriginal", "")), image_url=full,
                    thumb_url=ii.get("thumburl") or ii["url"],
                    page_url=f"https://commons.wikimedia.org/wiki/File:{quote(name)}",
                    width=w, height=h, classification="print" if "engrav" in name.lower() else "",
                    tags=[cat.removeprefix("Category:"), em.get("ImageDescription", "")[:500]],
                    license=em.get("LicenseShortName", "public domain"),
                )
                out[art.source_id] = art
                n += 1
        except RateLimited:
            log("commons: rate-limited, using what was fetched so far")
            break
        if n == 0:
            log(f"commons: nothing in {cat}")
    return list(out.values())
