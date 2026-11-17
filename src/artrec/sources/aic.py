"""Art Institute of Chicago public API (no key). https://api.artic.edu/docs/"""
from __future__ import annotations

from . import Artwork, strong_keywords

API = "https://api.artic.edu/api/v1/artworks/search"
FIELDS = ("id,title,artist_title,artist_display,date_start,date_end,image_id,thumbnail,style_titles,"
          "classification_titles,subject_titles,term_titles,medium_display,is_public_domain")
IIIF = "https://www.artic.edu/iiif/2"


def _search(http, q: str, pages: int, era_end: int | None = None):
    for page in range(1, pages + 1):
        params = {"q": q, "query[term][is_public_domain]": "true", "fields": FIELDS, "limit": 100, "page": page}
        data = http.get_json(API, params=params, headers={"AIC-User-Agent": "artrec (personal recommender)"})
        rows = data.get("data", [])
        yield from rows
        if len(rows) < 100 or page >= data.get("pagination", {}).get("total_pages", 0):
            break


def _to_art(r, artist_name=None) -> Artwork | None:
    if not r.get("image_id") or not r.get("is_public_domain"):
        return None
    th = r.get("thumbnail") or {}
    w, h = th.get("width"), th.get("height")
    tags = [*(r.get("style_titles") or []), *(r.get("subject_titles") or []), *(r.get("term_titles") or [])]
    return Artwork(
        source="aic", source_id=str(r["id"]), title=r.get("title") or "", artist=artist_name or r.get("artist_title") or "",
        year=r.get("date_end") or r.get("date_start"),
        image_url=f"{IIIF}/{r['image_id']}/full/3000,/0/default.jpg",
        thumb_url=f"{IIIF}/{r['image_id']}/full/400,/0/default.jpg",
        page_url=f"https://www.artic.edu/artworks/{r['id']}",
        width=w, height=h, medium=r.get("medium_display") or "",
        classification=", ".join(r.get("classification_titles") or []),
        tags=[*tags, th.get("alt_text") or ""],
    )


def harvest(profile, matcher, http, log) -> list[Artwork]:
    out = {}
    for a in profile.artists:
        for r in _search(http, a.name, pages=3):
            m, _ = matcher.match(r.get("artist_title") or r.get("artist_display") or "")
            if m is a and (art := _to_art(r)):
                out[art.source_id] = art
    era_end = profile.era[1]
    for kw in strong_keywords(profile):
        for r in _search(http, kw, pages=1):
            if (r.get("date_end") or 9999) <= era_end and (art := _to_art(r)):
                out.setdefault(art.source_id, art)
    return list(out.values())
