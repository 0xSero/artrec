---
name: artrec
description: Recommend public-domain classical art (medieval, Renaissance, visionary, Symbolist) to the user with the local `artrec` CLI, and record their likes and dislikes. Use when the user asks for art recommendations, new art, paintings or engravings to look at, a new art wallpaper, or reacts to a previous recommendation ("love #2", "not that one").
---

# artrec

The user's taste lives in `~/.config/artrec/profile.toml` (artists, keywords, style prompts, shape and colour rules,
optional subject gate). Read it before changing anything. Source and docs: https://github.com/0xSero/artrec

## Recommend
1. `artrec recommend` (add `-n N` for a different batch size). The first run of the week re-harvests the sources, which takes a few minutes.
   `artrec recommend --dry-run` previews a batch without recording it; use it to check profile changes.
2. Show the result: the contact sheet `_sheet.jpg` from the batch folder (Read it), plus the numbered list with titles and artists.
   Offer `artrec open` for the HTML gallery.
3. Never hand-pick art outside the tool. The tool's hash store is what guarantees nothing is recommended twice.
   If the user wants a specific work, they can `artrec seed <file>` it.

## Feedback
- "I like 2 and 5" → `artrec like 2 5` (ranks refer to the latest batch; art id prefixes work too).
- "not into 3" → `artrec dislike 3`. "I already know that one" → `artrec seen 3`.
- "make it my wallpaper" → `artrec wallpaper <n|id>`.

## Shaping the picks (profile.toml)
- A style described in words → `[prompts] positive` / `negative`.
- More or less of an artist or subject → `[artists]` / `[keywords]` weights. The pool is harvested from `[artists]`,
  so a new artist needs `artrec harvest` before their work can appear.
- "wide / banner / wallpaper shape" → `[batch] min_aspect` (e.g. 1.5). "colourful" → `[batch] min_color_share` (up to 1.0).
- "only pictures of X" (e.g. a single woman) → `[visual] subject`, `subject_against`, `subject_min`.
  The gate only sees candidates that have thumbnails, so after adding artists it may pass few until more are fetched (`artrec warm`).
- Picks keep looking like old liked works or seeds when the user wants something new → lower `[visual] like_share`.
- Back the profile up before a big change.

## Debug
- `artrec stats` shows the pool size per source, host cooldowns and learned weights.
- `artrec -v harvest` re-harvests verbosely. `artrec why <id>` shows the score breakdown.
