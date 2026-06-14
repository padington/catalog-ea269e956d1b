# Reels Catalogue

A hands-off pipeline that scrapes the Instagram reels, figures out what each one
is about, and publishes a browsable, category-filtered catalogue to GitHub Pages
— with click-to-play Instagram embeds.

## Architecture

```
Instagram private API              Local machine (queue-driven)                Public web
─────────────────────       ─────────────────────────────────────       ───────────────────

  direct_v2/inbox/  ─┐
  direct_v2/threads/ ├─► scrape.py ──► reels.db ◄── queue table (per-stage state machine)
  media/{pk}/info/  ─┘                 (SQLite)        │
                                          ▲            │  one generic worker (pipeline.drain)
                                          │            ▼  drives every stage off the queue:
                                          │
        enrich ─► download ─┬─ transcribe ─► categorize ─┐
        (IG)      (IG)       │  (whisper)    (ollama)      ├─► build_site.py ─► docs/index.html
                            │               tags ────────┤     (Jinja2 HTML)        │
                            └─ vision ──────────────────┘                       git push
                               (ollama VLM)                                         ▼
                                                                 GitHub Pages (padington.github.io)
```

Everything runs locally. The only thing that leaves your machine is the generated
`docs/index.html` (captions, thumbnails, shortcodes) — pushed to a **public** repo
with a random, unguessable name. Credentials, the database, and downloaded media
never leave.

### The queue

Work is no longer discovered implicitly (`WHERE output_col IS NULL`). Each reel's
progress through the pipeline is tracked explicitly in a `queue(pk, stage, status,
attempts, error, updated_at)` table, where `status ∈ {pending, running, done,
failed, skipped}`. A single generic worker (`pipeline.drain`) drives every stage:
it enqueues reels whose upstream dependency is `done`, claims a batch under a
lease, runs the stage's pure `process()` function, and writes the result back.

This makes the pipeline:
- **Modular & testable** — each stage is a pure `process(item, ctx) -> result`
  plus a `write(conn, pk, result)` callback, unit-tested with fixtures (no IG /
  ollama / whisper needed). See `tests/`.
- **Resumable & rate-limit-safe** — IG-paced stages (enrich, download) jitter
  their sleeps and abort cleanly on throttle (`ThrottleError`), releasing claimed
  work so a later run resumes. Local stages (transcribe, categorize, tags) run at
  full speed.
- **Decoupled** — downloading is IG-paced (~3/min) but transcription is local
  (~600/hr), so the slow IG step never blocks the fast local one.

## Components

| File | Role |
|------|------|
| `run.py` | argparse orchestrator — entry point for every stage (`scrape`, `thread`, `enrich`, `download`, `transcribe`, `vision`, `categorize`, `tags`, `status`, `stats`, `migrate`, `build`, `all`) |
| `pipeline.py` | Stage registry (the DAG) + the generic queue-driven worker (`drain`) shared by every stage; throttle detection |
| `login.py` | One-time Instagram auth → writes `ig_session.json` (session-cookie based, avoids password challenges) |
| `scrape.py` | Reads DM threads via **raw** private endpoints and extracts shared reels |
| `enrich.py` | Backfills real captions/thumbnails for reels that arrived caption-less |
| `transcribe.py` | Downloads the reel's video (IG-paced) and transcribes the audio locally with whisper |
| `vision.py` | Describes sampled scene-change keyframes of the downloaded mp4 with a local ollama VLM (`qwen2.5vl`), filling the `visual` column |
| `categorize.py` | Picks 1–2 categories + free-form tags from caption+transcript+visual using a local ollama model |
| `build_site.py` | Renders `reels.db` into a self-contained static HTML page (Jinja2) |
| `db.py` | Thin SQLite layer (stdlib only) — `reels` + `queue` schema, upsert/update helpers, queue claim/mark/backfill |
| `tests/` | Module-by-module unit tests for the pipeline, wiring, self-tests, and backfill |
| `reels.db` | SQLite store (local only, git-ignored) |
| `media/` | Downloaded `{pk}.mp4` files (local only, git-ignored) |
| `docs/index.html` | The published catalogue (served by GitHub Pages) |

### Why raw JSON for scraping

`instagrapi`'s typed models crash on Instagram's current **XMA** share format
(shared reels carry an `instagram://` URL that fails the library's URL validation).
So `scrape.py` bypasses the typed layer and calls `cl.private_request(...)` directly,
parsing the raw JSON. Shared reels show up as DM items of type `xma_clip` /
`xma_reel_share` / `xma_media_share`, with the payload as a single-element list.

## Data flow / pipeline stages

`scrape` seeds the table; the rest are **queue stages** driven off the `queue`
table, each gated on its upstream dependency being `done`/`skipped`:

```
scrape ──► enrich ──► download ─┬─ transcribe ──► categorize ──► build
                                │                  tags ────────┤
                                └─ vision ────────────────────┘
```

1. **scrape** — Walk the DM inbox (or one thread with cursor pagination), pull every
   shared-reel item, and `INSERT OR IGNORE` it into `reels.db`. Resumable: re-running
   never duplicates. Many DM shares have no caption, so they land as `Reel by @handle`.
2. **enrich** *(IG-paced)* — For rows with a missing/`Reel by @` placeholder caption,
   call `media/{pk}/info/` to fetch the real caption, canonical shortcode/URL, and a
   fresh thumbnail.
3. **download** *(IG-paced)* — Fetch the reel's `.mp4` into `media/{pk}.mp4`. Reels
   with no video are marked `skipped` (terminal, never retried). This is the slow,
   rate-limited step (~3/min); it's split out so transcription can run independently.
4. **transcribe** *(local)* — Extract 16 kHz mono PCM with ffmpeg and run **whisper**
   (`whisper-cli`, `ggml-large-v3-q5_0`) on it. ~600 reels/hr, ~9× real-time — never
   the bottleneck. An empty transcript (silent/music-only) is a valid `done`.
5. **vision** *(local)* — A twin of transcribe (`depends_on=download`, runs in
   parallel with it). Samples scene-change keyframes from the mp4 with ffmpeg and
   describes each with a local **ollama** VLM (`qwen2.5vl`), de-duplicates
   near-identical scenes, and stores the blob in `visual`. An empty `visual` (no
   usable frames) is a valid `done`.
6. **categorize** *(local)* — Ask a local **ollama** model (`llama3.2`) to pick 1–2
   labels *strictly* from a 26-item taxonomy (architecture, cooking, fitness, travel,
   …) from caption **+ transcript + visual**. Output is post-filtered to drop off-list
   labels. Falls back to a stub backend if ollama is unreachable.
7. **tags** *(local)* — Same caption+transcript+visual input, generates free-form
   descriptive tags via ollama for drill-down search.
8. **build** — Group reels by category and render one static `index.html`: sticky
   category filter chips, a card grid with lazy thumbnails, click-to-play Instagram
   embeds, and a per-reel tag list in the lightbox modal.
9. **publish** — Copy the page into `docs/` and `git push`; GitHub Pages serves it.

### Migrating an existing database

`run.py migrate` is a **lossless, idempotent** one-shot that backfills the `queue`
table from an existing `reels.db` — it reads each reel's populated columns
(`caption`, `transcript`, `categories`, `tags`) and existing `media/*.mp4` files,
and seeds the matching terminal queue markers (`done`/`skipped`). It only ever does
`INSERT OR IGNORE` into `queue`; it **never mutates the `reels` table**, so no
content is lost and no work is reprocessed. Running it twice inserts nothing the
second time.

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
| `transcript` | local-ASR audio transcript (plain text; `''` = silent/music-only, `NULL` = not yet done) |
| `visual` | local-VLM scene-description blob (plain text; `''` = no usable frames, `NULL` = not yet done) |
| `thumbnail_url` | Instagram CDN image (expires after a few weeks) |
| `taken_at` | original post time |
| `categories` | JSON array, `NULL` until categorized |
| `tags` | JSON array of free-form tags, `NULL` until generated |
| `created_at` | row insert time |

A separate `queue(pk, stage, status, attempts, error, updated_at)` table tracks
each reel's progress through every pipeline stage (see [The queue](#the-queue)).

## Usage

```bash
# one-time: create the virtualenv and install deps
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# one-time: authenticate (writes ig_session.json)
.venv/bin/python login.py

# full pipeline: scrape → enrich → download → transcribe → vision → categorize → tags → build
.venv/bin/python run.py all

# one-time, on an existing DB: seed the queue from already-populated columns
.venv/bin/python run.py migrate

# or run a single stage (each drains its slice of the queue)
.venv/bin/python run.py scrape
.venv/bin/python run.py thread --thread-id <id> --max 300
.venv/bin/python run.py enrich
.venv/bin/python run.py download
.venv/bin/python run.py transcribe
.venv/bin/python run.py vision
.venv/bin/python run.py categorize
.venv/bin/python run.py tags
.venv/bin/python run.py status        # per-stage queue counts
.venv/bin/python run.py stats         # per-stage benchmarking (processed/done/items-per-min)
.venv/bin/python run.py build

# run the tests (no IG / ollama / whisper needed)
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
```

Database path defaults to `reels.db`; override with `REELS_DB=/path/to.db`.
Categorizer is configurable via `REELS_CATEGORIZER` (`ollama`/`stub`), `OLLAMA_HOST`,
`OLLAMA_MODEL`. Whisper is configurable via `REELS_WHISPER_MODEL` /
`REELS_WHISPER_CLI`. The vision VLM is configurable via `REELS_VLM_MODEL`
(default `qwen2.5vl`, served by the same ollama at `OLLAMA_HOST`).

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
`ig_session.json`, `sessionid.txt`, `reels.db`, `*.db`, `media/`, `*.mp4`, `*.wav`,
`site/`, `.venv/`.

The published repo is public, so only non-sensitive output (`docs/index.html` +
source code) is ever pushed. The URL is randomized rather than secret-protected —
GitHub Pages free tier can't gate access, so obscurity is the privacy layer.

## Model roadmap (local enrichment)

The understanding of each reel is built up from several **local** models, each
adding a signal the next stage can use. Everything runs on-device — no cloud
inference, no content leaves the machine.

| Layer | Model | What it adds | Status |
|-------|-------|--------------|--------|
| **ASR** (audio → text) | whisper `large-v3-q5` via `whisper-cli` | spoken words, on-audio context | ✅ shipped (transcribe stage) |
| **LLM** (text → labels) | `llama3.2` via ollama | categories + free-form tags from caption+transcript | ✅ shipped (categorize/tags) |
| **VLM** (frames → text) | `qwen2.5vl` via ollama | *visual* understanding — setting, people, actions, objects via scene-change keyframe sampling; augments caption+transcript for categorize/tags | ✅ built (vision stage); staging e2e not yet run |
| **OCR** (frames → text) | (folded into the VLM prompt for now) | burned-in captions / overlay text | 🔜 planned |

### Why a VLM, and how it slots in without disturbing anything

ASR only hears the audio; a huge fraction of reels are silent or music-only, and
much of the meaning lives *on screen* (recipe text, place names, product shots).
A vision-language model reads sampled keyframes and emits a short description,
giving `categorize`/`tags` a third input alongside caption and transcript.

It's built as a purely **additive** stage — a twin of `transcribe`:

- a `vision` stage (`vision.py`), `depends_on="download"` (reuses the
  already-downloaded mp4), `ig_paced=False` (fully local), filling a nullable
  `visual` column;
- frames are chosen by ffmpeg **scene-change detection** (one per visual cut,
  capped at 8) with a proportional 25/50/75% fallback; each frame is described by
  `qwen2.5vl`, near-identical scenes are de-duplicated, and failed-extraction /
  failed-description frames are dropped (their error text never enters the blob);
- `categorize`/`tags` consume `caption + transcript + visual` **NULL-safely**, so
  reels without a visual blob behave exactly as today;
- no existing stage, column, or queue row changes — old reels keep working, and
  the new stage backfills lazily.

The model was validated on real sample videos (a standalone prototype re-ran the
*unchanged* categorize/tags with and without the visual signal); the staging
end-to-end drain has not run yet. The `vision` model is configurable via
`REELS_VLM_MODEL` (default `qwen2.5vl`).

## Known limitations

- Private or deleted reels won't load in the inline embed player.
- CDN thumbnails expire after ~weeks — re-enrich to refresh.
- ~23% of reels land in `other` (emoji-only / non-descriptive captions). The VLM /
  on-screen-text layer above is the planned fix for this bucket.
