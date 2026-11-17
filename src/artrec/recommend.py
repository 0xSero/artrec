"""Harvest -> filter already-seen (hashes) -> score -> diverse pick -> download -> record."""
from __future__ import annotations

import html
import json
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from .config import OUT_DIR, Profile
from .db import emb_to_blob, get_meta, now, set_meta
from .hashing import DHASH_MAX_DIST, DUP_COS, dhash, hamming, norm, sha256_file
from .net import RateLimited
from .sources import harvest_all
from .taste import Taste
from .visual import backfill_aspect, backfill_color, ensure_visual, load_aspects, load_colors, load_visual

HARVEST_EVERY_DAYS = 7
COLOR_MIN = 22.0  # colourfulness above which a work counts as "colour" for batch balance
SHORTLIST, EXPLORE, PER_ARTIST_SHORTLIST = 350, 60, 18
COLS = ["art_id", "work_key", "source", "source_id", "title", "artist", "year", "medium", "classification",
        "tags", "image_url", "thumb_url", "page_url", "width", "height", "license"]


def log(*a):
    print("·", *a, file=sys.stderr)


# ---------------------------------------------------------------- harvest
def harvest(db, http, profile: Profile, force: bool = False) -> int:
    last = get_meta(db, "last_harvest", 0)
    if not force and time.time() - last < HARVEST_EVERY_DAYS * 86400 and db.execute("SELECT 1 FROM artworks LIMIT 1").fetchone():
        return 0
    log("harvesting candidates (cached responses are reused)")
    arts = harvest_all(profile, http, log)
    rows = []
    for a in arts:
        d = a.row()
        d["tags"] = json.dumps([t for t in d["tags"] if t][:40])
        rows.append(tuple(d[c] for c in COLS) + (now(),))
    db.executemany(f"INSERT OR REPLACE INTO artworks({','.join(COLS)}, harvested_at) VALUES({','.join('?' * (len(COLS) + 1))})", rows)
    db.commit()
    set_meta(db, "last_harvest", time.time())
    log(f"pool: {db.execute('SELECT COUNT(*) FROM artworks').fetchone()[0]} artworks")
    return len(rows)


def pool_rows(db) -> list[dict]:
    cur = db.execute(f"SELECT {','.join(COLS)} FROM artworks")
    return [dict(zip(COLS, r)) for r in cur]


def resolve_anchors(db, http, taste: Taste, rows: list[dict]) -> None:
    done = set(get_meta(db, "anchors_done", []))
    want = [a for a in taste.p.anchors if f"{a['artist']}|{a['title']}" not in done]
    if not want:
        return
    picks = []
    for a in want:
        t = norm(a["title"])
        for r in rows:
            if taste.artist_key(r["artist"] or "")[0] == a["artist"] and t in norm(r["title"] or ""):
                picks.append((a, r))
                break
    ensure_visual(db, http, [r for _, r in picks], log)
    vis = load_visual(db, [r["art_id"] for _, r in picks])
    for a, r in picks:
        if r["art_id"] in vis:
            h, e = vis[r["art_id"]]
            db.execute("INSERT OR REPLACE INTO seeds(seed_id, label, emb, dhash, at) VALUES(?,?,?,?,?)",
                       (f"anchor:{r['art_id']}", f"{a['artist']} — {r['title']}", emb_to_blob(e), None, now()))
            done.add(f"{a['artist']}|{a['title']}")
    db.commit()
    set_meta(db, "anchors_done", sorted(done))


# ---------------------------------------------------------------- blocked set
class Blocked:
    def __init__(self, db):
        self.ids = {r[0] for r in db.execute("SELECT art_id FROM recommendations UNION SELECT art_id FROM seen")}
        self.keys = {r[0] for r in db.execute(
            "SELECT work_key FROM recommendations WHERE work_key IS NOT NULL UNION "
            "SELECT a.work_key FROM seen s JOIN artworks a USING(art_id) WHERE a.work_key IS NOT NULL")}
        hs, es = [], []
        for h, e in db.execute(
                "SELECT v.dhash, v.emb FROM visual v WHERE v.art_id IN (SELECT art_id FROM recommendations UNION SELECT art_id FROM seen)"):
            if h:
                hs.append(int(h, 16))
            if e is not None:
                es.append(np.frombuffer(e, np.float32))
        for h, e in db.execute("SELECT dhash, emb FROM seeds WHERE seed_id NOT LIKE 'anchor:%'"):
            if h:
                hs.append(int(h, 16))
            if e is not None:
                es.append(np.frombuffer(e, np.float32))
        self.hashes = hs
        self.embs = np.stack(es) if es else np.zeros((0, 512), np.float32)

    def by_identity(self, r) -> bool:
        return r["art_id"] in self.ids or (r["work_key"] is not None and r["work_key"] in self.keys)

    def by_image(self, h: int, e: np.ndarray) -> str | None:
        if any(hamming(h, x) <= DHASH_MAX_DIST for x in self.hashes):
            return "dhash"
        if len(self.embs) and float((self.embs @ e).max()) >= DUP_COS:
            return "clip"
        return None

    def add(self, r, h, e):
        self.ids.add(r["art_id"])
        if r["work_key"]:
            self.keys.add(r["work_key"])
        self.hashes.append(h)
        self.embs = np.vstack([self.embs, e[None]])


# ---------------------------------------------------------------- recommend
def recommend(db, http, profile: Profile, n: int | None = None, refresh: bool = False,
              download: bool = True, dry_run: bool = False) -> list[dict]:
    n = n or profile.batch["size"]
    harvest(db, http, profile, force=refresh)
    taste = Taste(profile, db)
    rows = pool_rows(db)
    if not rows:
        raise SystemExit("candidate pool is empty — every source failed; try `artrec harvest --force -v`")
    resolve_anchors(db, http, taste, rows)

    blocked = Blocked(db)
    min_side = profile.batch["min_long_side"]
    min_aspect = float(profile.batch.get("min_aspect", 0) or 0)
    backfill_aspect(db)
    aspects = load_aspects(db)

    def aspect_of(r) -> float | None:  # museum dimensions first, else the cached thumbnail's shape
        if r["width"] and r["height"]:
            return r["width"] / r["height"]
        return aspects.get(r["art_id"])

    def wide_enough(r) -> bool:  # unknown shape passes here and is checked once its thumbnail is fetched
        a = aspect_of(r)
        return not min_aspect or a is None or a >= min_aspect

    cands = [r for r in rows if not blocked.by_identity(r) and wide_enough(r)
             and (not (r["width"] or r["height"]) or max(r["width"] or 0, r["height"] or 0) >= min_side)]
    log(f"{len(rows)} in pool, {len(cands)} not yet recommended")

    scored = []
    for r in cands:
        m, why = taste.meta(r)
        scored.append((m, r["art_id"], r, why))
    scored.sort(key=lambda x: (-x[0], x[1]))  # deterministic: score, then hash
    batch_no = db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] + 1
    # shortlist by metadata, capped per artist so heavy-weight artists can't crowd out the rest
    short, per, rest = [], {}, []
    for s in scored:
        a = s[3]["artist"]
        if len(short) < SHORTLIST and per.get(a, 0) < PER_ARTIST_SHORTLIST:
            short.append(s)
            per[a] = per.get(a, 0) + 1
        else:
            rest.append(s)
    median = scored[len(scored) // 2][0]
    rest = [s for s in rest if s[0] >= median]
    short += random.Random(batch_no).sample(rest, min(EXPLORE, len(rest)))  # seeded exploration
    # anything already embedded costs nothing to score visually, so let it compete too
    embedded = {a for (a,) in db.execute("SELECT art_id FROM visual WHERE failed=0 AND emb IS NOT NULL")}
    in_short = {s[1] for s in short}
    short += [s for s in scored if s[1] in embedded and s[1] not in in_short]

    ensure_visual(db, http, [s[2] for s in short], log)
    backfill_color(db)
    vis = load_visual(db, [s[1] for s in short])
    short = [s for s in short if s[1] in vis]
    if min_aspect:
        aspects.update(load_aspects(db, [s[1] for s in short]))
        short = [s for s in short if (aspect_of(s[2]) or 0) >= min_aspect]
    colors = load_colors(db, [s[1] for s in short])
    if not short:
        raise SystemExit("no candidate thumbnails could be fetched; check network / `artrec stats`")
    embs = np.stack([vis[s[1]][1] for s in short])
    subj = taste.subject_prob(embs)
    if subj is not None:  # hard subject gate, e.g. "a single woman"
        keep = subj >= float(profile.visual.get("subject_min", 0.4))
        log(f"subject gate: {int(keep.sum())}/{len(short)} candidates match {profile.visual['subject']!r}")
        short = [s for s, k in zip(short, keep) if k]
        if not short:
            raise SystemExit("no candidate passes the subject gate; lower [visual] subject_min or fetch more thumbnails (artrec warm)")
        embs = np.stack([vis[s[1]][1] for s in short])
    vscore, vparts = taste.visual(embs)

    recent = recent_artists(db, profile.batch["artist_cooldown_batches"])
    W = profile.weights
    final = []
    for i, (m, aid, r, why) in enumerate(short):
        q = taste.quality(r)
        nov = 0.0 if why["artist"] in recent else 1.0
        f = W["meta"] * m + W["visual"] * float(vscore[i]) + W["quality"] * q + W["novelty"] * nov
        why |= {"color": round(colors.get(aid, 0.0), 1), "meta": round(m, 3), "visual": round(float(vscore[i]), 3), "like_sim": round(float(vparts["like_sim"][i]), 3),
                "prompt": round(float(vparts["prompt"][i]), 3), "quality": round(q, 3), "novelty": nov, "final": round(f, 4)}
        final.append((f, aid, r, why, i))
    final.sort(key=lambda x: (-x[0], x[1]))

    picks, per_artist = [], {}
    max_pa = profile.batch["max_per_artist"]
    need_color = round(n * float(profile.batch.get("min_color_share", 0.34)))
    is_color = lambda aid: colors.get(aid, 0.0) >= COLOR_MIN
    pool = final[:250]
    while len(picks) < n and pool:
        best, best_adj = None, -1e9
        have_color = sum(is_color(p[1]) for p in picks)
        force_color = (need_color - have_color) >= (n - len(picks)) and any(is_color(c[1]) for c in pool)
        for c in pool:
            f, aid, r, why, i = c
            if per_artist.get(why["artist"], 0) >= max_pa or (force_color and not is_color(aid)):
                continue
            div = max((float(embs[i] @ embs[p[4]]) for p in picks), default=0.0)
            adj = f - 0.15 * max(0.0, div - 0.6) / 0.4
            if adj > best_adj:
                best, best_adj = c, adj
        if best is None:
            break
        pool.remove(best)
        f, aid, r, why, i = best
        h, e = vis[aid]
        if (reason := blocked.by_image(h, e)):
            db.execute("INSERT OR IGNORE INTO seen(art_id, reason, at) VALUES(?,?,?)", (aid, f"dup:{reason}", now()))
            continue
        blocked.add(r, h, e)
        picks.append(best)
        per_artist[why["artist"]] = per_artist.get(why["artist"], 0) + 1
    db.commit()

    if dry_run:
        return [{"rank": k + 1, **p[2], "why": p[3]} for k, p in enumerate(picks)]
    return _commit_batch(db, http, picks, download)


def recent_artists(db, k: int) -> set[str]:
    rows = db.execute("SELECT breakdown FROM recommendations WHERE batch_id IN "
                      "(SELECT batch_id FROM batches ORDER BY batch_id DESC LIMIT ?)", (k,))
    return {json.loads(b).get("artist") for (b,) in rows}


def _slug(s: str, n: int = 50) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(s))[:n].strip("-") or "untitled"


def _commit_batch(db, http, picks, download: bool) -> list[dict]:
    ts = datetime.now()
    cur = db.execute("INSERT INTO batches(created_at, folder) VALUES(?, '')", (time.time(),))
    batch_id = cur.lastrowid
    folder = OUT_DIR / f"{ts:%Y-%m-%d}-batch{batch_id:03d}"
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for k, (f, aid, r, why, _) in enumerate(picks, 1):
        akey = why["artist"]
        path = folder / f"{k:02d}-{aid}-{_slug(akey, 24)}-{_slug(r['title'])}.jpg"
        sha = None
        if download:
            try:
                if http.download(r["image_url"], path, min_bytes=20_000):
                    with Image.open(path) as im:
                        r["width"], r["height"] = im.size
                        if im.mode not in ("RGB", "L") or im.format != "JPEG":
                            im.convert("RGB").save(path, "JPEG", quality=95)
                    sha = sha256_file(path)
            except (RateLimited, Exception) as e:
                log(f"download failed for {aid}: {e}")
        h = load_visual(db, [aid]).get(aid, (0, None))[0]
        db.execute("INSERT OR REPLACE INTO recommendations(art_id, batch_id, rank, work_key, dhash, file_sha256, path, score, breakdown, recommended_at)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (aid, batch_id, k, r["work_key"], f"{h:016x}", sha, str(path) if sha else None, f, json.dumps(why), time.time()))
        out.append({"rank": k, **r, "why": why, "path": str(path) if sha else None})
    db.execute("UPDATE batches SET folder=? WHERE batch_id=?", (str(folder), batch_id))
    db.commit()
    _write_gallery(folder, batch_id, out)
    return out


def _write_gallery(folder: Path, batch_id: int, items: list[dict]) -> None:
    (folder / "batch.json").write_text(json.dumps(items, indent=2, default=str))
    cards = []
    for it in items:
        img = Path(it["path"]).name if it.get("path") else it["thumb_url"]
        meta = " · ".join(str(x) for x in [it["artist"], it["year"], it["source"].upper()] if x)
        cards.append(f"""<figure><a href="{html.escape(img)}"><img src="{html.escape(img)}" loading="lazy"></a>
<figcaption><b>{it['rank']}. {html.escape(it['title'] or 'Untitled')}</b><span>{html.escape(meta)}</span>
<code>artrec like {it['art_id']}</code> <a href="{html.escape(it['page_url'])}">source ↗</a></figcaption></figure>""")
    (folder / "index.html").write_text(f"""<!doctype html><meta charset="utf-8"><title>Art batch {batch_id}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>:root{{--bg:#0d0c0b;--fg:#e9e2d4;--mute:#9a907f}} body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 Georgia,serif;padding:24px 16px}}
h1{{font-weight:400;letter-spacing:.04em;margin:0 0 20px}} main{{display:grid;gap:28px;grid-template-columns:repeat(auto-fill,minmax(min(100%,420px),1fr))}}
img{{width:100%;height:auto;display:block;border-radius:2px}} figure{{margin:0}} figcaption{{margin-top:8px;display:grid;gap:2px}}
span{{color:var(--mute)}} code{{color:var(--mute);font-size:12px}} a{{color:#c9a96e}}</style>
<h1>Batch {batch_id}</h1><main>{''.join(cards)}</main>""")
    paths = [it["path"] for it in items if it.get("path")]
    if paths:
        subprocess.run(["magick", "montage", *paths, "-thumbnail", "520x520>", "-geometry", "+8+8",
                        "-background", "#0d0c0b", "-tile", "3x", str(folder / "_sheet.jpg")],
                       check=False, capture_output=True)


def warm(db, http, profile: Profile, limit: int = 40) -> int:
    """Slowly embed the best not-yet-embedded candidates from rate-limited hosts (run from a timer)."""
    harvest(db, http, profile)
    taste = Taste(profile, db)
    have = {r[0] for r in db.execute("SELECT art_id FROM visual")}
    blocked = Blocked(db)
    rows = [r for r in pool_rows(db) if r["art_id"] not in have and not blocked.by_identity(r)]
    rows.sort(key=lambda r: (-taste.meta(r)[0], r["art_id"]))
    todo = rows[:limit]
    ensure_visual(db, http, todo, log, workers=1)
    got = db.execute(f"SELECT COUNT(*) FROM visual WHERE failed=0 AND art_id IN ({','.join('?' * len(todo))})",
                     [r["art_id"] for r in todo]).fetchone()[0] if todo else 0
    log(f"warmed {got}/{len(todo)}; {len(rows) - got} candidates still without thumbnails")
    return got
