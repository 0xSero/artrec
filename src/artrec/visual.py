"""CLIP (ViT-B/32 via fastembed/ONNX, CPU) embeddings + perceptual hashes for thumbnails."""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import numpy as np
from PIL import Image

from .config import THUMB_DIR
from .db import blob_to_emb, emb_to_blob, now
from .hashing import dhash
from .net import RateLimited

IMG_MODEL = "Qdrant/clip-ViT-B-32-vision"
TXT_MODEL = "Qdrant/clip-ViT-B-32-text"


@lru_cache(1)
def _img_model():
    from fastembed import ImageEmbedding
    return ImageEmbedding(IMG_MODEL)


@lru_cache(1)
def _txt_model():
    from fastembed import TextEmbedding
    return TextEmbedding(TXT_MODEL)


def _unit(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, np.float32)
    return m / (np.linalg.norm(m, axis=-1, keepdims=True) + 1e-8)


def embed_images(paths: list[str]) -> np.ndarray:
    return _unit(np.stack(list(_img_model().embed(paths, batch_size=32))))


@lru_cache(8)
def _embed_texts_cached(texts: tuple[str, ...]) -> np.ndarray:
    return _unit(np.stack(list(_txt_model().embed(list(texts)))))


def embed_texts(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.zeros((0, 512), np.float32)
    return _embed_texts_cached(tuple(texts))


def colorfulness(im: Image.Image) -> float:
    """Hasler & Süsstrunk colourfulness (~0 greyscale, >30 clearly colourful)."""
    a = np.asarray(im.convert("RGB").resize((128, 128)), np.float32)
    rg = a[..., 0] - a[..., 1]
    yb = 0.5 * (a[..., 0] + a[..., 1]) - a[..., 2]
    return float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean()))


def ensure_visual(db, http, rows: list[dict], log, workers: int = 8) -> None:
    """Download thumbnails for rows lacking a visual record, then hash + embed them."""
    have = {r[0] for r in db.execute("SELECT art_id FROM visual")}
    todo = [r for r in rows if r["art_id"] not in have]
    if not todo:
        return
    log(f"fetching {len(todo)} thumbnails")

    def fetch(r):
        p = THUMB_DIR / f"{r['art_id']}.jpg"
        if p.exists():
            return r, p
        try:
            return (r, p) if http.download(r["thumb_url"], p) else (r, None)
        except RateLimited:
            return r, "retry"
        except Exception:
            return r, None

    ok = []
    with ThreadPoolExecutor(workers) as ex:
        for i, (r, p) in enumerate(ex.map(fetch, todo), 1):
            if p == "retry":
                continue
            if p is None:
                db.execute("INSERT OR REPLACE INTO visual(art_id, failed, at) VALUES(?,1,?)", (r["art_id"], now()))
                continue
            try:
                with Image.open(p) as im:
                    im = im.convert("RGB")
                    if max(im.size) > 512:  # keep the cache small
                        im.thumbnail((400, 400))
                        im.save(p, quality=88)
                    ok.append((r["art_id"], p, dhash(im), colorfulness(im), im.width / im.height))
            except Exception:
                p.unlink(missing_ok=True)
                db.execute("INSERT OR REPLACE INTO visual(art_id, failed, at) VALUES(?,1,?)", (r["art_id"], now()))
            if i % 50 == 0:
                print(f"  thumbnails {i}/{len(todo)}", file=sys.stderr)
    db.commit()
    if not ok:
        return
    log(f"embedding {len(ok)} images")
    for i in range(0, len(ok), 64):
        chunk = ok[i:i + 64]
        embs = embed_images([str(p) for _, p, _, _, _ in chunk])
        db.executemany("INSERT OR REPLACE INTO visual(art_id, dhash, emb, failed, at, color, aspect) VALUES(?,?,?,0,?,?,?)",
                       [(a, f"{h:016x}", emb_to_blob(e), now(), c, asp) for (a, _, h, c, asp), e in zip(chunk, embs)])
        db.commit()


def backfill_color(db) -> None:
    rows = db.execute("SELECT art_id FROM visual WHERE failed=0 AND color IS NULL").fetchall()
    for (a,) in rows:
        p = THUMB_DIR / f"{a}.jpg"
        if p.exists():
            with Image.open(p) as im:
                db.execute("UPDATE visual SET color=? WHERE art_id=?", (colorfulness(im), a))
    db.commit()


def backfill_aspect(db) -> None:
    rows = db.execute("SELECT art_id FROM visual WHERE failed=0 AND aspect IS NULL").fetchall()
    for (a,) in rows:
        p = THUMB_DIR / f"{a}.jpg"
        if p.exists():
            with Image.open(p) as im:
                db.execute("UPDATE visual SET aspect=? WHERE art_id=?", (im.width / im.height, a))
    db.commit()


def load_aspects(db, art_ids: list[str] | None = None) -> dict[str, float]:
    if art_ids is None:
        return dict(db.execute("SELECT art_id, aspect FROM visual WHERE aspect IS NOT NULL"))
    out = {}
    for i in range(0, len(art_ids), 900):
        chunk = art_ids[i:i + 900]
        q = f"SELECT art_id, aspect FROM visual WHERE aspect IS NOT NULL AND art_id IN ({','.join('?' * len(chunk))})"
        out.update(dict(db.execute(q, chunk)))
    return out


def load_colors(db, art_ids: list[str]) -> dict[str, float]:
    out = {}
    for i in range(0, len(art_ids), 900):
        chunk = art_ids[i:i + 900]
        q = f"SELECT art_id, color FROM visual WHERE color IS NOT NULL AND art_id IN ({','.join('?' * len(chunk))})"
        out.update(dict(db.execute(q, chunk)))
    return out


def load_visual(db, art_ids: list[str]) -> dict[str, tuple[int, np.ndarray]]:
    out = {}
    for i in range(0, len(art_ids), 900):
        chunk = art_ids[i:i + 900]
        q = f"SELECT art_id, dhash, emb FROM visual WHERE failed=0 AND art_id IN ({','.join('?' * len(chunk))})"
        for a, h, e in db.execute(q, chunk):
            out[a] = (int(h, 16), blob_to_emb(e))
    return out


def embed_file(path: str) -> tuple[int, np.ndarray]:
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((400, 400))
        h = dhash(im)
        tmp = THUMB_DIR / "_seed_tmp.jpg"
        im.save(tmp, quality=90)
    return h, embed_images([str(tmp)])[0]
