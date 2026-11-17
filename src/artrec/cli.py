from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

from . import config
from .config import load_profile
from .db import connect, emb_to_blob, now


def _open():
    config.ensure_dirs()
    db = connect(config.DB_PATH)
    return db


def _http(db, verbose):
    from .net import Http
    return Http(db, verbose=verbose)


def resolve_ids(db, refs: list[str]) -> list[str]:
    """Accept full/prefix art_ids, a rank number in the latest batch, or a recommended file path."""
    out = []
    last = db.execute("SELECT MAX(batch_id) FROM batches").fetchone()[0]
    for ref in refs:
        base = os.path.basename(ref)
        if ref.isdigit() and int(ref) < 100 and last:
            m = db.execute("SELECT art_id FROM recommendations WHERE batch_id=? AND rank=?", (last, int(ref))).fetchone()
            if m:
                out.append(m[0]); continue
        if base[:2].isdigit() and base[2:3] == "-":  # 01-<art_id>-...
            ref = base.split("-")[1]
        m = [r[0] for r in db.execute("SELECT art_id FROM artworks WHERE art_id LIKE ?", (ref + "%",))]
        if len(m) != 1:
            sys.exit(f"'{ref}': {'no match' if not m else 'ambiguous'}")
        out.append(m[0])
    return out


def _row(db, aid):
    from .recommend import COLS
    r = db.execute(f"SELECT {','.join(COLS)} FROM artworks WHERE art_id=?", (aid,)).fetchone()
    return dict(zip(COLS, r))


def _print_items(items):
    for it in items:
        w = it["why"]
        size = f"{it['width']}×{it['height']}" if it.get("width") else "?"
        print(f"{it['rank']:>2}. [{it['art_id']}] {it['title'] or 'Untitled'}")
        print(f"    {it['artist'] or 'unknown'}{', ' + str(it['year']) if it['year'] else ''} · {it['source']} · {size}"
              f" · score {w['final']:.3f} (meta {w['meta']:.2f}, visual {w['visual']:.2f})")
        if it.get("path"):
            print(f"    {it['path']}")
        print(f"    {it['page_url']}")


def cmd_recommend(a):
    from .recommend import recommend
    db = _open()
    items = recommend(db, _http(db, a.verbose), load_profile(), n=a.n, refresh=a.refresh,
                      download=not a.no_download, dry_run=a.dry_run)
    if a.json:
        print(json.dumps(items, indent=2, default=str)); return
    _print_items(items)
    if items and items[0].get("path"):
        folder = os.path.dirname(items[0]["path"])
        print(f"\ngallery: {folder}/index.html\nsheet:   {folder}/_sheet.jpg")
        print("feedback: artrec like <n|id> … / artrec dislike <n|id> …")


def cmd_warm(a):
    from .recommend import warm
    db = _open()
    warm(db, _http(db, a.verbose), load_profile(), limit=a.limit)


def cmd_harvest(a):
    from .recommend import harvest
    db = _open()
    n = harvest(db, _http(db, a.verbose), load_profile(), force=True)
    print(f"harvested {n} records")


def _feedback(a, rating):
    from .taste import Taste
    from .visual import ensure_visual
    db = _open()
    taste = Taste(load_profile(), db)
    ids = resolve_ids(db, a.ids)
    rows = [_row(db, i) for i in ids]
    ensure_visual(db, _http(db, False), rows, lambda *x: None)
    for aid, r in zip(ids, rows):
        prev = db.execute("SELECT rating FROM feedback WHERE art_id=?", (aid,)).fetchone()
        if prev and prev[0] == rating:
            print(f"{aid}: already {'liked' if rating > 0 else 'disliked'}"); continue
        taste.learn(r, rating if not prev else 2 * rating)  # flipping a vote undoes the old one
        db.execute("INSERT OR REPLACE INTO feedback(art_id, rating, at) VALUES(?,?,?)", (aid, rating, now()))
        db.execute("INSERT OR IGNORE INTO seen(art_id, reason, at) VALUES(?,?,?)", (aid, "feedback", now()))
        print(f"{'♥' if rating > 0 else '✗'} {r['title']} — {r['artist']}")
    db.commit()


def cmd_seen(a):
    db = _open()
    for aid in resolve_ids(db, a.ids):
        db.execute("INSERT OR IGNORE INTO seen(art_id, reason, at) VALUES(?,?,?)", (aid, "manual", now()))
    db.commit()


def cmd_seed(a):
    from .hashing import sha256_file
    from .visual import embed_file
    db = _open()
    for p in a.paths:
        h, e = embed_file(p)
        sid = "file:" + sha256_file(p)[:16]
        db.execute("INSERT OR REPLACE INTO seeds(seed_id, label, emb, dhash, at) VALUES(?,?,?,?,?)",
                   (sid, a.label or os.path.basename(p), emb_to_blob(e), f"{h:016x}", now()))
        print(f"seeded {sid} ({a.label or os.path.basename(p)})")
    db.commit()


def cmd_history(a):
    db = _open()
    q = ("SELECT r.batch_id, r.art_id, a.title, a.artist, a.year, f.rating, r.recommended_at FROM recommendations r "
         "JOIN artworks a USING(art_id) LEFT JOIN feedback f USING(art_id) ORDER BY r.recommended_at DESC LIMIT ?")
    for b, aid, t, ar, y, rt, at in db.execute(q, (a.limit,)):
        mark = {1: "♥", -1: "✗"}.get(rt, " ")
        print(f"{mark} b{b:03d} {aid} {datetime.fromtimestamp(at):%Y-%m-%d}  {t[:60]} — {ar[:40]}{', ' + str(y) if y else ''}")


def cmd_why(a):
    db = _open()
    for aid in resolve_ids(db, a.ids):
        r = _row(db, aid)
        rec = db.execute("SELECT breakdown, score FROM recommendations WHERE art_id=?", (aid,)).fetchone()
        print(f"{aid}  {r['title']} — {r['artist']} ({r['year']})  [{r['source']}]")
        if rec:
            print(json.dumps(json.loads(rec[0]), indent=2))
        else:
            from .taste import Taste
            m, why = Taste(load_profile(), db).meta(r)
            print(json.dumps({"meta": round(m, 3), **why}, indent=2))


def cmd_stats(a):
    db = _open()
    one = lambda q: db.execute(q).fetchone()[0]
    print(f"pool:            {one('SELECT COUNT(*) FROM artworks')}")
    for s, c in db.execute("SELECT source, COUNT(*) FROM artworks GROUP BY source ORDER BY 2 DESC"):
        print(f"  {s:12s}   {c}")
    print(f"embedded:        {one('SELECT COUNT(*) FROM visual WHERE failed=0')}")
    print(f"recommended:     {one('SELECT COUNT(*) FROM recommendations')} in {one('SELECT COUNT(*) FROM batches')} batches")
    print(f"blocked (seen):  {one('SELECT COUNT(*) FROM seen')}")
    print(f"feedback:        {one('SELECT COUNT(*) FROM feedback WHERE rating>0')} liked, {one('SELECT COUNT(*) FROM feedback WHERE rating<0')} disliked")
    print(f"seeds:           {one('SELECT COUNT(*) FROM seeds')}")
    for h, u in db.execute("SELECT host, until FROM host_cooldown WHERE until > ?", (time.time(),)):
        print(f"cooling down:    {h} until {datetime.fromtimestamp(u):%H:%M}")
    top = db.execute("SELECT key, delta FROM learned WHERE kind='artist' ORDER BY delta DESC LIMIT 5").fetchall()
    if top:
        print("learned artists: " + ", ".join(f"{k} {d:+.2f}" for k, d in top))
    print(f"db:              {config.DB_PATH}\nprofile:         {config.PROFILE_PATH}\noutput:          {config.OUT_DIR}")


def cmd_profile(a):
    config.ensure_dirs()
    if a.edit:
        subprocess.run([os.environ.get("EDITOR", "nano"), str(config.PROFILE_PATH)])
    else:
        print(config.PROFILE_PATH)


def cmd_wallpaper(a):
    db = _open()
    aid = resolve_ids(db, [a.id])[0]
    p = db.execute("SELECT path FROM recommendations WHERE art_id=?", (aid,)).fetchone()
    if not p or not p[0] or not os.path.exists(p[0]):
        sys.exit("that artwork has no downloaded file")
    if not shutil.which("omarchy"):
        sys.exit(f"omarchy not found; file is at {p[0]}")
    subprocess.run(["omarchy", "theme", "bg", "set", p[0]], check=True)
    print(f"background set: {p[0]}")


def cmd_open(a):
    db = _open()
    row = db.execute("SELECT folder FROM batches ORDER BY batch_id DESC LIMIT 1").fetchone()
    if not row:
        sys.exit("no batches yet")
    subprocess.Popen(["xdg-open", os.path.join(row[0], "index.html")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="artrec", description="Recommend public-domain classical art you have not seen before.")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("recommend", aliases=["rec"], help="new batch of recommendations")
    s.add_argument("-n", type=int, help="batch size (default from profile)")
    s.add_argument("--refresh", action="store_true", help="re-harvest sources first")
    s.add_argument("--no-download", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="show picks without recording them")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_recommend)
    sub.add_parser("harvest", help="refresh the candidate pool").set_defaults(fn=cmd_harvest)
    s = sub.add_parser("warm", help="slowly pre-fetch thumbnails for top candidates (for a timer)")
    s.add_argument("--limit", type=int, default=40); s.set_defaults(fn=cmd_warm)
    for name, fn, h in [("like", lambda a: _feedback(a, 1), "teach: more like this"),
                        ("dislike", lambda a: _feedback(a, -1), "teach: less like this"),
                        ("seen", cmd_seen, "never recommend these"), ("why", cmd_why, "score breakdown")]:
        s = sub.add_parser(name, help=h)
        s.add_argument("ids", nargs="+", help="art id (prefix), rank in latest batch, or file path")
        s.set_defaults(fn=fn)
    s = sub.add_parser("seed", help="add local images as liked style references")
    s.add_argument("paths", nargs="+"); s.add_argument("--label")
    s.set_defaults(fn=cmd_seed)
    s = sub.add_parser("history"); s.add_argument("--limit", type=int, default=30); s.set_defaults(fn=cmd_history)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    s = sub.add_parser("profile", help="print path / --edit the taste profile"); s.add_argument("--edit", action="store_true")
    s.set_defaults(fn=cmd_profile)
    s = sub.add_parser("wallpaper", help="set a recommended work as the desktop background"); s.add_argument("id")
    s.set_defaults(fn=cmd_wallpaper)
    sub.add_parser("open", help="open the latest batch gallery").set_defaults(fn=cmd_open)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
