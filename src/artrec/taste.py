"""Deterministic scoring: metadata taste (profile + learned feedback) and visual taste (CLIP)."""
from __future__ import annotations

import json
import math
import re

import numpy as np

from .config import Profile
from .db import blob_to_emb
from .hashing import norm
from .sources import ArtistMatcher

ARTIST_STEP, TAG_STEP = 0.08, 0.04
UNKNOWN_ARTIST = 0.25


class Taste:
    def __init__(self, profile: Profile, db):
        self.p = profile
        self.db = db
        self.matcher = ArtistMatcher(profile.artists)
        self.learned = {(k, key): d for k, key, d in db.execute("SELECT kind, key, delta FROM learned")}
        self._kw = [(re.compile(rf"\b{re.escape(norm(k))}\b"), k, v) for k, v in profile.keywords.items()]
        self._cls = [(norm(k), v) for k, v in profile.classifications.items()]

    # ---- metadata ----------------------------------------------------------
    def artist_key(self, attribution: str) -> tuple[str, float, float]:
        """(canonical artist key, base weight, attribution factor)"""
        spec, factor = self.matcher.match(attribution)
        if spec:
            return spec.name, spec.weight, factor
        n = norm(attribution)
        return (n or "unknown"), UNKNOWN_ARTIST, 1.0

    def text(self, r: dict) -> str:
        tags = r["tags"] if isinstance(r["tags"], list) else json.loads(r["tags"] or "[]")
        return norm(" ".join([r["title"] or "", r["medium"] or "", r["classification"] or "", *tags]))

    def keywords_hit(self, r: dict) -> list[str]:
        t = self.text(r)
        return [k for pat, k, _ in self._kw if pat.search(t)]

    def meta(self, r: dict) -> tuple[float, dict]:
        akey, aw, factor = self.artist_key(r["artist"] or "")
        a = np.clip(aw * factor + self.learned.get(("artist", akey), 0.0), 0, 1.2)

        y, (start, end) = r["year"], self.p.era
        era = 0.6 if y is None else (1.0 if start <= y <= end else math.exp(-min(abs(y - start), abs(y - end)) / 80))

        hits = self.keywords_hit(r)
        ks = sum(self.p.keywords[k] + self.learned.get(("tag", k), 0.0) for k in hits)
        kw = (np.clip(ks, -1, 1) + 1) / 2

        c = norm(f"{r['classification'] or ''} {r['medium'] or ''}")
        cv = [v for k, v in self._cls if k and k in c]
        cls = min(cv) if any(v < 0 for v in cv) else (max(cv) if cv else 0.0)
        clss = (cls + 1) / 2

        m = 0.55 * a + 0.15 * era + 0.20 * kw + 0.10 * clss
        if cls <= -0.8:
            m *= 0.3
        return float(m), {"artist": akey, "artist_w": round(float(a), 3), "era": round(era, 3),
                          "keywords": hits, "class": cls}

    def quality(self, r: dict) -> float:
        L = max(r["width"] or 0, r["height"] or 0)
        if not L:
            return 0.5
        lo = self.p.batch["min_long_side"]
        return float(0.3 + 0.7 * np.clip((L - lo) / max(4000 - lo, 1), 0, 1))

    # ---- visual ------------------------------------------------------------
    def reference_sets(self) -> tuple[np.ndarray, np.ndarray]:
        like = [blob_to_emb(e) for (e,) in self.db.execute("SELECT emb FROM seeds WHERE emb IS NOT NULL")]
        like += [blob_to_emb(e) for (e,) in self.db.execute(
            "SELECT v.emb FROM feedback f JOIN visual v USING(art_id) WHERE f.rating > 0 AND v.emb IS NOT NULL")]
        dis = [blob_to_emb(e) for (e,) in self.db.execute(
            "SELECT v.emb FROM feedback f JOIN visual v USING(art_id) WHERE f.rating < 0 AND v.emb IS NOT NULL")]
        z = np.zeros((0, 512), np.float32)
        return (np.stack(like) if like else z), (np.stack(dis) if dis else z)

    def visual(self, embs: np.ndarray) -> tuple[np.ndarray, dict]:
        from .visual import embed_texts
        like, dis = self.reference_sets()
        n = len(embs)
        if like.shape[0]:
            s = embs @ like.T
            s = np.where(s >= 0.97, -1.0, s)  # a work is not evidence for itself (anchors are in the pool)
            k = min(3, s.shape[1])
            like_sim = np.sort(s, axis=1)[:, -k:].mean(axis=1)
        else:
            like_sim = np.zeros(n)
        pos, neg = embed_texts(self.p.pos_prompts), embed_texts(self.p.neg_prompts)
        prompt = (embs @ pos.T).max(axis=1) if len(pos) else np.zeros(n)
        if len(neg):
            prompt = prompt - (embs @ neg.T).max(axis=1)
        ls = float(np.clip(self.p.visual.get("like_share", 0.6), 0, 1))
        vis = ls * _pct(like_sim) + (1 - ls) * _pct(prompt) if like.shape[0] else _pct(prompt)
        if dis.shape[0]:
            close = (embs @ dis.T).max(axis=1)
            vis = np.where(close > 0.88, vis * 0.5, vis)
        return vis, {"like_sim": like_sim, "prompt": prompt}

    def subject_prob(self, embs: np.ndarray) -> np.ndarray | None:
        """Zero-shot CLIP probability that each image shows `[visual] subject` rather than any `subject_against`."""
        from .visual import embed_texts
        subj, against = self.p.visual.get("subject"), list(self.p.visual.get("subject_against", []))
        if not subj or not against:
            return None
        logits = 100.0 * embs @ embed_texts([subj, *against]).T
        e = np.exp(logits - logits.max(axis=1, keepdims=True))
        return e[:, 0] / e.sum(axis=1)

    # ---- learning ----------------------------------------------------------
    def learn(self, r: dict, rating: int) -> None:
        akey, _, _ = self.artist_key(r["artist"] or "")
        updates = [("artist", akey, ARTIST_STEP * rating)]
        updates += [("tag", k, TAG_STEP * rating) for k in self.keywords_hit(r)]
        for kind, key, d in updates:
            cur = self.learned.get((kind, key), 0.0)
            lo, hi = (-0.6, 0.6) if kind == "artist" else (-0.5, 0.5)
            new = float(np.clip(cur + d, lo, hi))
            self.learned[(kind, key)] = new
            self.db.execute("INSERT OR REPLACE INTO learned(kind, key, delta) VALUES(?,?,?)", (kind, key, new))


def _pct(x: np.ndarray) -> np.ndarray:
    """Percentile rank in 0..1 (ties broken stably) — scale-free across CLIP similarity ranges."""
    x = np.asarray(x, np.float64)
    if len(x) <= 1:
        return np.full(len(x), 0.5)
    order = np.argsort(x, kind="stable")
    ranks = np.empty(len(x))
    ranks[order] = np.arange(len(x))
    return ranks / (len(x) - 1)
