from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "artrec"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "artrec"
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / "artrec"
OUT_DIR = Path(os.environ.get("ARTREC_OUT") or Path.home() / "Pictures" / "art-recs")
PROFILE_PATH = CONFIG_DIR / "profile.toml"
DB_PATH = DATA_DIR / "artrec.db"
THUMB_DIR = CACHE_DIR / "thumbs"
BULK_DIR = CACHE_DIR / "bulk"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, CACHE_DIR, THUMB_DIR, BULK_DIR, OUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if not PROFILE_PATH.exists():
        with resources.as_file(resources.files("artrec") / "default_profile.toml") as src:
            shutil.copy(src, PROFILE_PATH)


@dataclass
class ArtistSpec:
    name: str
    weight: float
    aliases: list[str] = field(default_factory=list)


@dataclass
class Profile:
    era: tuple[int, int]
    weights: dict[str, float]
    batch: dict
    artists: list[ArtistSpec]
    keywords: dict[str, float]
    classifications: dict[str, float]
    pos_prompts: list[str]
    neg_prompts: list[str]
    anchors: list[dict]
    sources: dict
    visual: dict = field(default_factory=dict)

    def source_on(self, name: str) -> bool:
        return bool(self.sources.get(name, True))


def load_profile(path: Path = PROFILE_PATH) -> Profile:
    raw = tomllib.loads(path.read_text())
    artists = []
    for name, spec in raw.get("artists", {}).items():
        if isinstance(spec, (int, float)):
            spec = {"w": spec}
        artists.append(ArtistSpec(name, float(spec.get("w", 0.7)), list(spec.get("aliases", []))))
    era = raw.get("era", {})
    w = {"meta": 0.4, "visual": 0.45, "quality": 0.1, "novelty": 0.05} | raw.get("weights", {})
    tot = sum(w.values()) or 1.0
    batch = {"size": 6, "max_per_artist": 1, "artist_cooldown_batches": 2, "min_long_side": 1600} | raw.get("batch", {})
    prompts = raw.get("prompts", {})
    return Profile(
        era=(int(era.get("start", 1100)), int(era.get("end", 1930))),
        weights={k: v / tot for k, v in w.items()},
        batch=batch,
        artists=artists,
        keywords={k.lower(): float(v) for k, v in raw.get("keywords", {}).items()},
        classifications={k.lower(): float(v) for k, v in raw.get("classifications", {}).items()},
        pos_prompts=list(prompts.get("positive", [])),
        neg_prompts=list(prompts.get("negative", [])),
        anchors=list(raw.get("anchors", [])),
        sources=raw.get("sources", {}),
        visual={"like_share": 0.6} | raw.get("visual", {}),
    )
