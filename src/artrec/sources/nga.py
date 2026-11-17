"""National Gallery of Art open data (CC0 bulk CSVs on GitHub, IIIF images, no key)."""
from __future__ import annotations

import csv
import re
import sys
import time

from ..config import BULK_DIR
from ..hashing import norm
from . import Artwork

RAW = "https://raw.githubusercontent.com/NationalGalleryOfArt/opendata/main/data/{}.csv"
FILES = ["objects", "published_images", "objects_terms"]
REFRESH_DAYS = 30
KEEP_CLASSES = {"painting", "print", "drawing", "volume", "portfolio", "technical material"}

csv.field_size_limit(sys.maxsize)


def _ensure(http, log):
    for f in FILES:
        path = BULK_DIR / f"nga_{f}.csv"
        if path.exists() and time.time() - path.stat().st_mtime < REFRESH_DAYS * 86400:
            continue
        log(f"nga: downloading {f}.csv")
        with http.client.stream("GET", RAW.format(f), timeout=600) as r:
            r.raise_for_status()
            tmp = path.with_suffix(".part")
            with open(tmp, "wb") as out:
                for chunk in r.iter_bytes(1 << 20):
                    out.write(chunk)
            tmp.replace(path)


def harvest(profile, matcher, http, log) -> list[Artwork]:
    _ensure(http, log)
    # primary open-access image per object
    images = {}
    with open(BULK_DIR / "nga_published_images.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["viewtype"] == "primary" and r["openaccess"] == "1":
                images[r["depictstmsobjectid"]] = r
    kw = [(re.compile(rf"\b{re.escape(k)}\b"), k) for k, v in profile.keywords.items() if v >= 0.3]
    start, end = profile.era
    cands = {}
    with open(BULK_DIR / "nga_objects.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            img = images.get(r["objectid"])
            if not img:
                continue
            cls = (r["classification"] or "").lower()
            if cls not in KEEP_CLASSES:
                continue
            artist, _ = matcher.match(r["attribution"] or "")
            try:
                year = int(r["endyear"] or r["beginyear"])
            except ValueError:
                year = None
            text = norm(f"{r['title']} {img.get('assistivetext', '')}")
            if not artist:
                # discovery: unknown artist, but right era and strong subject match
                if year is None or not (start <= year <= end) or not any(p.search(text) for p, _ in kw):
                    continue
            cands[r["objectid"]] = (r, img)
    terms: dict[str, list[str]] = {}
    with open(BULK_DIR / "nga_objects_terms.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["objectid"] in cands and r["termtype"] in ("Keyword", "Theme", "Style", "School", "Technique"):
                terms.setdefault(r["objectid"], []).append(r["term"])
    out = []
    for oid, (r, img) in cands.items():
        base = img["iiifurl"]
        try:
            w, h = int(img["width"]), int(img["height"])
        except ValueError:
            w = h = None
        try:
            year = int(r["endyear"] or r["beginyear"])
        except ValueError:
            year = None
        attribution = r["attribution"] or ""
        out.append(Artwork(
            source="nga", source_id=oid, title=r["title"] or "", artist=attribution, year=year,
            image_url=f"{base}/full/!4000,4000/0/default.jpg",
            thumb_url=f"{base}/full/!400,400/0/default.jpg",
            page_url=f"https://www.nga.gov/collection/art-object-page.{oid}.html",
            width=w, height=h, medium=r["medium"] or "", classification=r["classification"] or "",
            tags=[*terms.get(oid, []), img.get("assistivetext") or ""], license="CC0",
        ))
    return out
