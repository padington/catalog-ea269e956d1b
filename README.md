# Reels Catalogue

A hands-off pipeline that scrapes the Instagram reels figures out what each one is about, and publishes a browsable, category-filtered
catalogue to GitHub Pages — with click-to-play Instagram embeds.

## Architecture

```
Instagram private API                Local machine                      Public web
─────────────────────         ──────────────────────────         ───────────────────
                                                                   
  direct_v2/inbox/  ─┐                                            
  direct_v2/threads/ ├─► scrape.py ──► reels.db ──► build_site.py ──► docs/index.html
  media/{pk}/info/  ─┘        ▲          (SQLite)         ▲                 │
                             │            │  ▲            │            git push
                        enrich.py ────────┘  │       (Jinja2 HTML)         │
                             │                │                            ▼
                        categorize.py ────────┘              GitHub Pages (padington.github.io)
                          (ollama llama3.2)
```

Everything runs locally. The only thing that leaves your machine is the generated
`docs/index.html` (captions, thumbnails, shortcodes) — pushed to a **public** repo
with a random, unguessable name. Credentials and the database never leave.

## Components

| File | Role |
|------|------|
| `run.py` | argparse orchestrator — entry point for every stage (`scrape`, `thread`, `enrich`, `categorize`, `build`, `all`) |
| `login.py` | One-time Instagram auth → writes `ig_session.json` (session-cookie based, avoids password challenges) |
| `scrape.py` | Reads DM threads via **raw** private endpoints and extracts shared reels |
| `enrich.py` | Backfills real captions/thumbnails for reels that arrived caption-less |
| `categorize.py` | Tags each reel with 1–2 categories from a fixed taxonomy using a local ollama model |
| `build_site.py` | Renders `reels.db` into a self-contained static HTML page (Jinja2) |
| `db.py` | Thin SQLite layer (stdlib only) — schema + upsert/update/iterate helpers |
| `reels.db` | SQLite store (local only, git-ignored) |
| `docs/index.html` | The published catalogue (served by GitHub Pages) |

### Why raw JSON for scraping

`instagrapi`'s typed models crash on Instagram's current **XMA** share format
(shared reels carry an `instagram://` URL that fails the library's URL validation).
So `scrape.py` bypasses the typed layer and calls `cl.private_request(...)` directly,
parsing the raw JSON. Shared reels show up as DM items of type `xma_clip` /
`xma_reel_share` / `xma_media_share`, with the payload as a single-element list.

## Data flow / pipeline stages

1. **scrape** — Walk the DM inbox (or one thread with cursor pagination), pull every
   shared-reel item, and `INSERT OR IGNORE` it into `reels.db`. Resumable: re-running
   never duplicates. Many DM shares have no caption, so they land as `Reel by @handle`.
2. **enrich** — For rows with a missing/placeholder caption, call `media/{pk}/info/`
   to fetch the real caption, canonical shortcode/URL, and a fresh thumbnail. Enriched
   rows get their `categories` reset to `NULL` so they’ll be re-categorized.
3. **categorize** — For each uncategorized row, ask a local **ollama** model
   (`llama3.2`) to pick 1–2 labels *strictly* from a 26-item taxonomy (architecture,
   cooking, fitness, travel, …). Output is post-filtered to drop anything off-list.
   Falls back to a stub backend if ollama is unreachable.
4. **build** — Group reels by category and render one static `index.html`: sticky
   category filter chips (client-side JS), a card grid with lazy thumbnails, and
   click-to-play Instagram embeds.
5. **publish** — Copy the page into `docs/` and `git push`; GitHub Pages serves it.

## Database schema

`reels` table, primary key `pk` (Instagram media id):

| column | notes |
|--------|-------|
| `pk` | media id, primary key |
| `shortcode` | reel code (used for URL + embed) |
| `url` | canonical `instagram.com/reel/<code>/` |
| `source` | where it came from (dm / thread) |
| `shared_by` | username who shared it |
| `caption` | text caption (after enrich) |
| `thumbnail_url` | Instagram CDN image (expires after a few weeks) |
| `taken_at` | original post time |
| `categories` | JSON array, `NULL` until categorized |
| `created_at` | row insert time |

## Usage

```bash
# one-time: create the virtualenv and install deps
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# one-time: authenticate (writes ig_session.json)
.venv/bin/python login.py

# full pipeline: scrape → enrich → categorize → build
.venv/bin/python run.py all

# or run a single stage
.venv/bin/python run.py scrape
.venv/bin/python run.py thread --thread-id <id> --max 300
.venv/bin/python run.py enrich
.venv/bin/python run.py categorize
.venv/bin/python run.py build
```

Database path defaults to `reels.db`; override with `REELS_DB=/path/to.db`.
Categorizer is configurable via `REELS_CATEGORIZER` (`ollama`/`stub`), `OLLAMA_HOST`,
`OLLAMA_MODEL`.

## Publishing updates

The live site is the `docs/` folder served by GitHub Pages. To refresh it:

```bash
.venv/bin/python run.py build          # regenerate site/index.html
cp site/index.html docs/index.html     # stage it for Pages
git add docs/index.html && git commit -m "refresh catalogue" && git push
```

Re-running `enrich` + `build` periodically also refreshes thumbnails, which expire.

## Secrets

These are git-ignored and must **never** be committed:
`ig_session.json`, `sessionid.txt`, `reels.db`, `*.db`, `.venv/`.

The published repo is public, so only non-sensitive output (`docs/index.html` +
source code) is ever pushed. The URL is randomized rather than secret-protected —
GitHub Pages free tier can't gate access, so obscurity is the privacy layer.

## Known limitations

- Private or deleted reels won't load in the inline embed player.
- CDN thumbnails expire after ~weeks — re-enrich to refresh.
- ~23% of reels land in `other` (emoji-only / non-descriptive captions). On-screen
  text OCR would shrink that bucket; not yet implemented.
