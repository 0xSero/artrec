"""Polite HTTP: per-host throttling, Retry-After aware retries, response cache, host cooldown."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
import time
from urllib.parse import urlparse

import httpx

from . import __version__

UA = f"artrec/{__version__} (personal art recommender; python-httpx)"

# minimum seconds between requests per host
INTERVAL = {
    "query.wikidata.org": 5.0,
    "commons.wikimedia.org": 3.0,
    "upload.wikimedia.org": 4.0,
    "api.artic.edu": 1.0,
    "www.artic.edu": 0.15,
    "openaccess-api.clevelandart.org": 0.3,
    "openaccess-cdn.clevelandart.org": 0.1,
    "api.nga.gov": 0.1,
}
DEFAULT_INTERVAL = 0.3


class RateLimited(Exception):
    pass


class Http:
    def __init__(self, db: sqlite3.Connection, verbose: bool = False):
        self.db = db
        self.verbose = verbose
        self.client = httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=60)
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._glock = threading.Lock()
        self._dblock = threading.Lock()

    def log(self, *a):
        if self.verbose:
            print("  [http]", *a, file=sys.stderr)

    # -- cooldown: a host that rate-limited us is skipped for a while --------
    def cooling(self, host: str) -> bool:
        with self._dblock:
            row = self.db.execute("SELECT until FROM host_cooldown WHERE host=?", (host,)).fetchone()
        return bool(row and row[0] > time.time())

    def _cool(self, host: str, seconds: float):
        with self._dblock:
            self.db.execute("INSERT OR REPLACE INTO host_cooldown(host, until) VALUES(?,?)", (host, time.time() + seconds))
            self.db.commit()

    def _throttle(self, host: str):
        with self._glock:
            lock = self._locks.setdefault(host, threading.Lock())
        with lock:
            gap = INTERVAL.get(host, DEFAULT_INTERVAL)
            wait = self._last.get(host, 0) + gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    def get(self, url: str, params: dict | None = None, headers: dict | None = None, tries: int = 4) -> httpx.Response:
        host = urlparse(url).hostname or ""
        if self.cooling(host):
            raise RateLimited(host)
        for attempt in range(tries):
            self._throttle(host)
            try:
                r = self.client.get(url, params=params, headers=headers)
            except httpx.TransportError as e:
                self.log(f"{host}: {e!r}, retry {attempt + 1}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 503) or (r.status_code == 403 and "too many" in r.text[:500].lower()):
                ra = r.headers.get("retry-after", "")
                wait = float(ra) if ra.replace(".", "", 1).isdigit() else 5 * 2 ** attempt
                if wait > 60 or attempt == tries - 1:
                    self._cool(host, max(wait, 900))
                    raise RateLimited(host)
                self.log(f"{host}: {r.status_code}, waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            if r.status_code >= 500 and attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            return r
        raise httpx.HTTPError(f"giving up on {url}")

    def get_json(self, url: str, params: dict | None = None, ttl_days: float = 14, headers: dict | None = None):
        key = hashlib.sha1(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
        with self._dblock:
            row = self.db.execute("SELECT body, fetched_at FROM http_cache WHERE key=?", (key,)).fetchone()
        if row and time.time() - row[1] < ttl_days * 86400:
            return json.loads(row[0])
        try:
            r = self.get(url, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
        except (RateLimited, httpx.HTTPError, ValueError):
            if row:  # stale cache beats nothing
                return json.loads(row[0])
            raise
        with self._dblock:
            self.db.execute("INSERT OR REPLACE INTO http_cache(key, url, body, fetched_at) VALUES(?,?,?,?)",
                            (key, url, json.dumps(data), time.time()))
            self.db.commit()
        return data

    def download(self, url: str, dest, min_bytes: int = 1024) -> bool:
        r = self.get(url)
        if r.status_code != 200 or len(r.content) < min_bytes or not r.headers.get("content-type", "image").startswith("image"):
            self.log(f"download failed {r.status_code} {url}")
            return False
        tmp = f"{dest}.part"
        with open(tmp, "wb") as f:
            f.write(r.content)
        import os
        os.replace(tmp, dest)
        return True
