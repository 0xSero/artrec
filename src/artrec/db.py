from __future__ import annotations

import json
import sqlite3
import time

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS artworks (
  art_id TEXT PRIMARY KEY, work_key TEXT, source TEXT NOT NULL, source_id TEXT NOT NULL,
  title TEXT, artist TEXT, year INTEGER, medium TEXT, classification TEXT, tags TEXT,
  image_url TEXT, thumb_url TEXT, page_url TEXT, width INTEGER, height INTEGER,
  license TEXT, harvested_at REAL
);
CREATE INDEX IF NOT EXISTS artworks_work_key ON artworks(work_key);
CREATE TABLE IF NOT EXISTS visual (
  art_id TEXT PRIMARY KEY, dhash TEXT, emb BLOB, failed INTEGER DEFAULT 0, at REAL
);
CREATE TABLE IF NOT EXISTS batches (
  batch_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, folder TEXT
);
CREATE TABLE IF NOT EXISTS recommendations (
  art_id TEXT PRIMARY KEY, batch_id INTEGER, work_key TEXT, dhash TEXT, file_sha256 TEXT,
  path TEXT, score REAL, breakdown TEXT, recommended_at REAL
);
CREATE TABLE IF NOT EXISTS seen (            -- anything that must never be recommended
  art_id TEXT PRIMARY KEY, reason TEXT, at REAL
);
CREATE TABLE IF NOT EXISTS feedback (
  art_id TEXT PRIMARY KEY, rating INTEGER, at REAL
);
CREATE TABLE IF NOT EXISTS seeds (           -- liked reference images (local files or anchors)
  seed_id TEXT PRIMARY KEY, label TEXT, emb BLOB, dhash TEXT, at REAL
);
CREATE TABLE IF NOT EXISTS learned (         -- feedback-derived weight adjustments
  kind TEXT, key TEXT, delta REAL, PRIMARY KEY(kind, key)
);
CREATE TABLE IF NOT EXISTS http_cache (key TEXT PRIMARY KEY, url TEXT, body TEXT, fetched_at REAL);
CREATE TABLE IF NOT EXISTS host_cooldown (host TEXT PRIMARY KEY, until REAL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(recommendations)")}
    if "rank" not in cols:
        db.execute("ALTER TABLE recommendations ADD COLUMN rank INTEGER")
    vcols = {r[1] for r in db.execute("PRAGMA table_info(visual)")}
    if "color" not in vcols:
        db.execute("ALTER TABLE visual ADD COLUMN color REAL")
    if "aspect" not in vcols:
        db.execute("ALTER TABLE visual ADD COLUMN aspect REAL")  # width / height of the thumbnail
    # rate-limit failures are transient; retry other failures after 30 days
    db.execute("DELETE FROM visual WHERE failed=1 AND at < ?", (time.time() - 30 * 86400,))
    db.commit()
    return db


def get_meta(db, k, default=None):
    row = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(db, k, v):
    db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?,?)", (k, json.dumps(v)))
    db.commit()


def emb_to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, np.float32).tobytes()


def blob_to_emb(b: bytes | None) -> np.ndarray | None:
    return None if b is None else np.frombuffer(b, np.float32)


def now() -> float:
    return time.time()
