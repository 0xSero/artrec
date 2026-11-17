# artrec

A personal recommender for public-domain classical art: medieval, Renaissance, Northern engraving, visionary
(Blake, Doré, Bosch, Dürer, Piranesi, Escher-adjacent geometry), Symbolist. It never recommends the same work twice.

```
artrec recommend          # next batch (default 6), downloaded to ~/Pictures/art-recs/<date>-batchNNN/
artrec like 2 5           # by rank in the latest batch, or by art id prefix
artrec dislike 3
artrec seed ~/Pictures/some-painting.jpg   # add a liked style reference
artrec why <id>           # score breakdown
artrec history | stats | open | wallpaper <id> | profile --edit
```

## How it works

1. **Harvest** (weekly, cached). Pulls candidates from sources that need no account or key:
   Art Institute of Chicago API, Cleveland Museum of Art Open Access, National Gallery of Art open data (bulk CSV),
   curated Wikimedia Commons categories, and Wikidata. Wikimedia endpoints are throttled; if they rate-limit,
   the host cools down and the run continues with the other sources.
2. **Metadata score**: artist weight from `profile.toml` (discounted for "workshop of", "after", …), era fit,
   subject keywords (apocalypse, Dante, angels, polyhedra, …) and object type. Mundane objects are pushed down.
3. **Visual score**: CLIP ViT-B/32 (ONNX, CPU) embeddings of thumbnails, compared with
   your liked works and seeds (top-3 nearest), plus the style prompts in the profile. Dislikes push similar images down.
4. **Pick**: a weighted blend, then a greedy diverse selection: at most one work per artist per batch,
   a penalty for near-identical images, and a cooldown for artists shown recently.
   Ordering is deterministic (score, then hash), so the same state gives the same batch.
5. **Never twice**. Anything recommended, rated or marked `seen` is blocked by four hashes:
   - `art_id`, from `sha1(source:id)`;
   - `work_key`, from `sha1(artist surname|normalised title)`, which catches the same work listed by two museums;
   - a 64-bit `dHash` of the image (Hamming distance ≤ 8);
   - CLIP cosine ≥ 0.95, which catches another impression of the same print.

   The downloaded file's sha256 is stored as well.

Feedback nudges artist and keyword weights; these adjustments are stored in the database, so `profile.toml` stays hand-edited.

## Shaping the picks

Everything lives in `~/.config/artrec/profile.toml` (`artrec profile --edit`):

- `[artists]`, `[keywords]`, `[classifications]`: metadata weights. The candidate pool is harvested from the listed artists, so
  add an artist to bring their work in.
- `[prompts] positive / negative`: CLIP text anchors for the visual score.
- `[batch] min_color_share`: share of each batch that must be colour work. `min_aspect`: width / height floor, e.g. `1.5`
  for wide banner / wallpaper shapes (`0` = any).
- `[visual] like_share`: how much of the visual score comes from likeness to liked works and seeds versus the prompts.
  `subject` / `subject_against` / `subject_min`: an optional zero-shot gate, e.g. "only paintings of a single woman".

## Use it from Claude Code

`skill/SKILL.md` is a [Claude Code skill](https://docs.claude.com/en/docs/claude-code/skills) that drives the CLI:
it runs batches, shows the contact sheet, records likes and dislikes from plain-language reactions, and turns described
styles into profile edits. Install it with:

```
mkdir -p ~/.claude/skills/artrec && cp skill/SKILL.md ~/.claude/skills/artrec/
```

## Files

| what | where |
|---|---|
| taste profile | `~/.config/artrec/profile.toml` |
| database (pool, hashes, history, feedback) | `~/.local/share/artrec/artrec.db` |
| thumbnails + bulk CSVs | `~/.cache/artrec/` |
| recommended images | `~/Pictures/art-recs/` |

## Install

```
git clone https://github.com/0xSero/artrec && uv tool install --python 3.13 -e ./artrec
```

`artrec wallpaper` uses [Omarchy](https://omarchy.org)'s `omarchy theme bg set`; on other desktops it prints the file path.
The images are public domain or open access; check each source page for the exact licence.

Escher's work is still under copyright and no open-access source publishes it, so he steers the taste through
prompts and seeds; the actual picks come from public-domain precursors (Jamnitzer, Stöer, Piranesi, Vredeman de Vries).
