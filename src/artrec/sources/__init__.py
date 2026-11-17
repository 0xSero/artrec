from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from ..config import ArtistSpec, Profile
from ..hashing import art_id, norm, work_key


@dataclass
class Artwork:
    source: str
    source_id: str
    title: str
    artist: str
    year: int | None
    image_url: str          # best full-resolution URL we may download
    thumb_url: str          # ~300 px, used for embeddings and dedupe hashes
    page_url: str
    width: int | None = None
    height: int | None = None
    medium: str = ""
    classification: str = ""
    tags: list[str] = field(default_factory=list)
    license: str = "public domain"

    @property
    def art_id(self) -> str:
        return art_id(self.source, self.source_id)

    @property
    def work_key(self) -> str | None:
        return work_key(self.artist, self.title)

    def row(self) -> dict:
        d = asdict(self)
        d["art_id"], d["work_key"] = self.art_id, self.work_key
        return d


_QUALIFIERS = {
    "workshop of": 0.6, "circle of": 0.55, "follower of": 0.5, "school of": 0.5, "style of": 0.45,
    "after": 0.55, "copy after": 0.45, "attributed to": 0.85, "imitator of": 0.4, "manner of": 0.45,
    "possibly": 0.8, "probably": 0.85,
}


class ArtistMatcher:
    """Maps free-form attributions ("after Albrecht Dürer", "Dürer, Albrecht") to profile artists."""

    def __init__(self, artists: list[ArtistSpec]):
        self.artists = artists
        self._pats = []
        for a in artists:
            for name in [a.name, *a.aliases]:
                n = norm(name)
                parts = n.split()
                forms = {n}
                if len(parts) >= 2:  # "durer albrecht" (inverted catalog form)
                    forms.add(" ".join([parts[-1], *parts[:-1]]))
                for f in forms:
                    self._pats.append((re.compile(rf"\b{re.escape(f)}\b"), a))
        self._pats.sort(key=lambda p: -len(p[0].pattern))

    def match(self, attribution: str) -> tuple[ArtistSpec | None, float]:
        """Returns (artist, attribution factor in 0..1)."""
        n = norm(attribution)
        if not n:
            return None, 0.0
        for pat, a in self._pats:
            m = pat.search(n)
            if m:
                prefix = n[: m.start()].strip()
                factor = 1.0
                for q, f in _QUALIFIERS.items():
                    if prefix.endswith(q) or prefix == q:
                        factor = f
                        break
                else:
                    if prefix and len(prefix.split()) > 3:  # "workshop of the master of ..." etc.
                        factor = 0.7
                return a, factor
        return None, 0.0


def harvest_all(profile: Profile, http, log) -> list[Artwork]:
    from . import aic, cleveland, commons, nga, wikidata

    matcher = ArtistMatcher(profile.artists)
    out: list[Artwork] = []
    for name, mod in [("aic", aic), ("cleveland", cleveland), ("nga", nga), ("commons", commons), ("wikidata", wikidata)]:
        if not profile.source_on(name):
            continue
        try:
            got = mod.harvest(profile, matcher, http, log)
            log(f"{name}: {len(got)} candidates")
            out.extend(got)
        except Exception as e:  # one broken source never blocks a recommendation
            log(f"{name}: skipped ({type(e).__name__}: {e})")
    return out


def strong_keywords(profile: Profile, min_w: float = 0.4) -> list[str]:
    return sorted(k for k, v in profile.keywords.items() if v >= min_w)
