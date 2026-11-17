"""Wikidata SPARQL: works with an image, by profile artists. One query per chunk, cached 30 days."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import quote, unquote

from ..net import RateLimited
from . import Artwork

WDQS = "https://query.wikidata.org/sparql"

Q = """
SELECT ?w ?wLabel ?img ?date ?name (GROUP_CONCAT(DISTINCT ?tag; separator="|") AS ?tags) WHERE {
  VALUES ?name { %s }
  ?artist rdfs:label ?name; wdt:P31 wd:Q5.
  ?w wdt:P170 ?artist; wdt:P18 ?img.
  OPTIONAL { ?w wdt:P571 ?date }
  OPTIONAL { { ?w wdt:P180 ?t } UNION { ?w wdt:P136 ?t } UNION { ?w wdt:P135 ?t }
             ?t rdfs:label ?tag FILTER(lang(?tag) = "en") }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
} GROUP BY ?w ?wLabel ?img ?date ?name LIMIT 6000
"""


def upload_thumb(fname: str, width: int = 330) -> str:
    """Direct thumb.wikimedia.org thumbnail URL (skips the rate-limited Special:FilePath redirect and upload.wikimedia.org, which throttles thumbnail bursts hard)."""
    name = fname.replace(" ", "_")
    h = hashlib.md5(name.encode()).hexdigest()
    suffix = ".jpg" if name.lower().endswith((".tif", ".tiff", ".svg", ".pdf", ".djvu")) else ""
    if name.lower().endswith(".svg"):
        suffix = ".png"
    return f"https://thumb.wikimedia.org/wikipedia/commons/thumb/{h[0]}/{h[:2]}/{quote(name)}/{width}px-{quote(name)}{suffix}"


def harvest(profile, matcher, http, log) -> list[Artwork]:
    out = {}
    names = [a.name for a in profile.artists]
    for i in range(0, len(names), 12):
        chunk = names[i:i + 12]
        q = Q % " ".join(json.dumps(n) + "@en" for n in chunk)
        try:
            data = http.get_json(WDQS, params={"query": q, "format": "json"}, ttl_days=30,
                                 headers={"Accept": "application/sparql-results+json"})
        except RateLimited:
            log("wikidata: rate-limited, skipping for now (will retry on a later harvest)")
            break
        for b in data["results"]["bindings"]:
            qid = b["w"]["value"].rsplit("/", 1)[1]
            fname = unquote(b["img"]["value"].rsplit("/", 1)[1])
            fp = f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(fname)}"
            date = b.get("date", {}).get("value", "")
            try:
                year = int(date.lstrip("+")[:4]) if date else None
            except ValueError:
                year = None
            out[qid] = Artwork(
                source="wikidata", source_id=qid, title=b.get("wLabel", {}).get("value", ""),
                artist=b["name"]["value"], year=year, image_url=f"{fp}?width=3840",
                thumb_url=upload_thumb(fname), page_url=f"https://www.wikidata.org/wiki/{qid}",
                tags=b.get("tags", {}).get("value", "").split("|"),
            )
    return list(out.values())
