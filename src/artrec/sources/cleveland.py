"""Cleveland Museum of Art Open Access API (no key, CC0). https://openaccess-api.clevelandart.org/"""
from __future__ import annotations

from . import Artwork, strong_keywords

API = "https://openaccess-api.clevelandart.org/api/artworks/"


def _search(http, params: dict, pages: int):
    for p in range(pages):
        data = http.get_json(API, params={**params, "has_image": 1, "cc0": 1, "limit": 100, "skip": p * 100})
        rows = data.get("data", [])
        yield from rows
        if len(rows) < 100:
            break


def _to_art(r) -> Artwork | None:
    imgs = r.get("images") or {}
    web, prt = imgs.get("web") or {}, imgs.get("print") or {}
    best = prt if prt.get("url") else web
    if not best.get("url"):
        return None
    creators = r.get("creators") or []
    artist = creators[0].get("description", "") if creators else ""
    artist = artist.split("(")[0].strip()
    def _int(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return None
    return Artwork(
        source="cleveland", source_id=str(r["id"]), title=r.get("title") or "", artist=artist,
        year=r.get("creation_date_latest") or r.get("creation_date_earliest"),
        image_url=best["url"], thumb_url=web.get("url") or best["url"], page_url=r.get("url") or "",
        width=_int(best.get("width")), height=_int(best.get("height")),
        medium=r.get("technique") or "", classification=r.get("type") or "",
        tags=[r.get("description") or "", r.get("digital_description") or "", r.get("culture", [""])[0] if r.get("culture") else ""],
        license="CC0",
    )


def harvest(profile, matcher, http, log) -> list[Artwork]:
    out = {}
    for a in profile.artists:
        for r in _search(http, {"q": a.name}, pages=2):
            creators = r.get("creators") or []
            if creators and matcher.match(creators[0].get("description", ""))[0] is a and (art := _to_art(r)):
                out[art.source_id] = art
    for kw in strong_keywords(profile):
        for r in _search(http, {"q": kw, "created_before": profile.era[1]}, pages=1):
            if (art := _to_art(r)):
                out.setdefault(art.source_id, art)
    return list(out.values())
