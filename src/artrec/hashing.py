"""Identity and content hashes used to never recommend the same artwork twice.

Four layers, cheapest first:
  art_id    sha1(source:source_id)          exact record identity
  work_key  sha1(artist|title)              same work listed by two sources
  dhash     64-bit difference hash          same image, different scan/resize
  clip      cosine >= DUP_COS on embeddings same work, different photo/impression
plus sha256 of the downloaded file, stored for provenance.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

import numpy as np
from PIL import Image

DHASH_MAX_DIST = 8
DUP_COS = 0.95
_GENERIC = {"untitled", "plate", "study", "sketch", "figure", "fragment", "page", "leaf", "folio"}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def art_id(source: str, source_id: str) -> str:
    return hashlib.sha1(f"{source}:{source_id}".encode()).hexdigest()[:12]


def work_key(artist: str, title: str) -> str | None:
    """None for titles too generic to identify a work on their own."""
    t = norm(title)
    words = t.split()
    if len(t) < 12 or len(words) < 2 or (words[0] in _GENERIC and len(words) <= 3):
        return None
    a = norm(artist).split()
    a = a[-1] if a else ""  # surname is the most stable part across sources
    return hashlib.sha1(f"{a}|{t}".encode()).hexdigest()[:12]


def dhash(img: Image.Image, size: int = 8) -> int:
    g = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    a = np.asarray(g, dtype=np.int16)
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
