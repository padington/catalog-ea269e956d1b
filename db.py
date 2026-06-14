import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS reels (
    pk            TEXT PRIMARY KEY,
    shortcode     TEXT,
    url           TEXT,
    source        TEXT,
    shared_by     TEXT,
    caption       TEXT,
    thumbnail_url TEXT,
    taken_at      INTEGER,
    categories    TEXT,
    tags          TEXT,
    created_at    INTEGER
)
"""


def connect(path="reels.db"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    pk         TEXT NOT NULL,
    stage      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending|running|done|failed|skipped
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    updated_at INTEGER,
    PRIMARY KEY (pk, stage)
)
"""

QUEUE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_queue_stage_status "
    "ON queue(stage, status)"
)

# Per-stage benchmarking log: one row appended per drain() run.
STAGE_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS stage_runs (
    id         INTEGER PRIMARY KEY,
    stage      TEXT,
    started_at INTEGER,
    ended_at   INTEGER,
    processed  INTEGER,
    done       INTEGER,
    failed     INTEGER,
    skipped    INTEGER,
    seconds    REAL
)
"""


def init_db(conn):
    conn.execute(SCHEMA)
    # Migrate older DBs that predate the free-form `tags` column.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(reels)")}
    if "tags" not in cols:
        conn.execute("ALTER TABLE reels ADD COLUMN tags TEXT")
    # Migrate older DBs that predate the local-ASR `transcript` column.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(reels)")}
    if "transcript" not in cols:
        conn.execute("ALTER TABLE reels ADD COLUMN transcript TEXT")
    # Migrate older DBs that predate the local-VLM `visual` column.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(reels)")}
    if "visual" not in cols:
        conn.execute("ALTER TABLE reels ADD COLUMN visual TEXT")
    # Explicit per-stage work queue (replaces the implicit "output IS NULL"
    # discovery). Idempotent: created once, never reset on re-runs.
    conn.execute(QUEUE_SCHEMA)
    conn.execute(QUEUE_INDEX)
    conn.execute(STAGE_RUNS_SCHEMA)
    conn.commit()


def upsert_reel(conn, reel):
    # INSERT OR IGNORE on pk keeps re-runs resumable and never clobbers an
    # already-categorized row.
    conn.execute(
        """
        INSERT OR IGNORE INTO reels
            (pk, shortcode, url, source, shared_by, caption,
             thumbnail_url, taken_at, categories, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reel["pk"],
            reel.get("shortcode"),
            reel.get("url"),
            reel.get("source"),
            reel.get("shared_by"),
            reel.get("caption"),
            reel.get("thumbnail_url"),
            reel.get("taken_at"),
            None,
            int(time.time()),
        ),
    )
    conn.commit()


def iter_uncategorized(conn):
    cur = conn.execute("SELECT * FROM reels WHERE categories IS NULL")
    for row in cur:
        yield dict(row)


_UPDATABLE = ("shortcode", "url", "caption", "thumbnail_url", "taken_at",
              "categories", "transcript")


def update_reel(conn, pk, fields):
    # UPDATE only whitelisted columns for a pk; column names are validated
    # against _UPDATABLE (never interpolated from caller input) and values are
    # parameterized, so this is SQL-injection-safe.
    cols = [c for c in fields if c in _UPDATABLE]
    if not cols:
        return
    assignments = ", ".join(f"{c} = ?" for c in cols)
    values = [fields[c] for c in cols]
    conn.execute(
        f"UPDATE reels SET {assignments} WHERE pk = ?",
        (*values, pk),
    )
    conn.commit()


def set_categories(conn, pk, categories):
    conn.execute(
        "UPDATE reels SET categories = ? WHERE pk = ?",
        (json.dumps(categories), pk),
    )
    conn.commit()


def set_tags(conn, pk, tags):
    conn.execute(
        "UPDATE reels SET tags = ? WHERE pk = ?",
        (json.dumps(tags), pk),
    )
    conn.commit()


def set_transcript(conn, pk, text):
    # Store the raw local-ASR transcript (plain text, not JSON). An empty
    # string is a sentinel for "no video / nothing to transcribe" so the reel
    # isn't retried forever.
    conn.execute(
        "UPDATE reels SET transcript = ? WHERE pk = ?",
        (text, pk),
    )
    conn.commit()


def set_visual(conn, pk, text):
    # Store the local-VLM scene-description blob (plain text, not JSON). An
    # empty string is a valid terminal ("no usable frames") so the reel isn't
    # retried forever — mirrors set_transcript.
    conn.execute(
        "UPDATE reels SET visual = ? WHERE pk = ?",
        (text, pk),
    )
    conn.commit()


def iter_untranscribed(conn):
    cur = conn.execute("SELECT * FROM reels WHERE transcript IS NULL")
    for row in cur:
        yield dict(row)


def iter_untagged_with_caption(conn):
    # Reels that still need fine-grained tags AND have a real caption to work
    # from (the `Reel by @handle` placeholder yields nothing useful).
    cur = conn.execute(
        "SELECT * FROM reels WHERE tags IS NULL "
        "AND caption IS NOT NULL AND caption NOT LIKE 'Reel by @%'"
    )
    for row in cur:
        yield dict(row)


# --------------------------------------------------------------------------- #
# Explicit work queue.
#
# One row per (pk, stage). The generic driver in pipeline.py enqueues ready
# work, atomically claims a batch (flipping it to 'running'), then marks each
# item 'done'/'skipped'/'failed'. A 'failed' row is retried until attempts hits
# max_attempts. This replaces the old "WHERE <output> IS NULL" discovery.
# --------------------------------------------------------------------------- #


def enqueue(conn, pk, stage):
    # INSERT OR IGNORE: only ever creates a fresh pending row; never resets an
    # existing one (so done/failed/running rows survive re-runs).
    conn.execute(
        "INSERT OR IGNORE INTO queue (pk, stage, status, attempts, updated_at) "
        "VALUES (?, ?, 'pending', 0, ?)",
        (pk, stage, int(time.time())),
    )
    conn.commit()


def backfill_queue(conn):
    """Seed TERMINAL queue rows reflecting what each reel ALREADY has.

    One-time lossless migration for DBs populated under the OLD implicit
    pipeline (which had no queue rows). Under the new gated DAG, a downstream
    stage only runs once its upstream is 'done'/'skipped' in the queue, so
    existing reels would never flow. This inserts the terminal markers that the
    old content implies, letting downstream work resume WITHOUT reprocessing.

    Strictly INSERT OR IGNORE into the `queue` table only — it NEVER updates or
    deletes any column of `reels`, and never resets an existing queue row
    (idempotent + safe to run twice). Pending rows are NOT created here; they
    are derived later by pipeline.enqueue_ready.

    Per-reel rules (columns read from `reels`; the download mp4 check hits the
    on-disk media dir):
      enrich:     caption present and NOT a 'Reel by @' placeholder -> 'done'
      download:   transcript non-empty OR <pk>.mp4 on disk non-empty -> 'done';
                  transcript == '' (no-video sentinel)               -> 'skipped'
      transcribe: transcript non-empty -> 'done'; transcript == '' -> 'skipped'
      categorize: categories NOT NULL -> 'done'
      tags:       tags NOT NULL -> 'done'
      vision:     visual NOT NULL -> 'done'

    Returns a summary dict {(stage, status): count} of rows actually inserted.
    """
    import transcribe

    now = int(time.time())
    media_dir = transcribe.MEDIA_DIR

    rows = conn.execute(
        "SELECT pk, caption, transcript, visual, categories, tags FROM reels"
    ).fetchall()

    pending = []  # (pk, stage, status)
    for r in rows:
        pk = r["pk"]
        caption = r["caption"]
        transcript = r["transcript"]
        visual = r["visual"]
        categories = r["categories"]
        tags = r["tags"]

        # enrich: a real (non-placeholder) caption means enrich is done.
        if caption is not None and not caption.startswith("Reel by @"):
            pending.append((pk, "enrich", "done"))

        # download / transcribe share the transcript/media evidence.
        has_transcript = transcript is not None and transcript != ""
        mp4 = os.path.join(media_dir, f"{pk}.mp4")
        has_media = os.path.exists(mp4) and os.path.getsize(mp4) > 0

        if has_transcript or has_media:
            pending.append((pk, "download", "done"))
        elif transcript == "":
            pending.append((pk, "download", "skipped"))

        if has_transcript:
            pending.append((pk, "transcribe", "done"))
        elif transcript == "":
            pending.append((pk, "transcribe", "skipped"))

        # vision: a non-NULL visual blob ("" included) means vision is done.
        if visual is not None:
            pending.append((pk, "vision", "done"))

        # categorize / tags: presence of the column value is the marker.
        if categories is not None:
            pending.append((pk, "categorize", "done"))
        if tags is not None:
            pending.append((pk, "tags", "done"))

    # Count only rows we actually insert (INSERT OR IGNORE skips existing ones),
    # so re-runs report 0 inserted and never flip a 'done'. cursor.rowcount is
    # 1 on a real insert and 0 when the row already existed and was ignored.
    summary = {}
    cur = conn.cursor()
    for pk, stage, status in pending:
        cur.execute(
            "INSERT OR IGNORE INTO queue (pk, stage, status, attempts, "
            "updated_at) VALUES (?, ?, ?, 0, ?)",
            (pk, stage, status, now),
        )
        if cur.rowcount > 0:
            summary[(stage, status)] = summary.get((stage, status), 0) + 1
    conn.commit()
    return summary


def claim_batch(conn, stage, limit, max_attempts=3):
    # Atomically lease up to `limit` items for `stage`. Eligible rows are
    # 'pending' OR ('failed' AND attempts < max_attempts). We flip them to
    # 'running' inside a single IMMEDIATE transaction so concurrent workers
    # can't claim the same pk (the UPDATE...WHERE pk IN (...) is the lease).
    # attempts is left untouched here; it is only bumped on the NEXT failure.
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT pk FROM queue "
            "WHERE stage = ? AND ("
            "  status = 'pending' OR (status = 'failed' AND attempts < ?)"
            ") ORDER BY updated_at ASC LIMIT ?",
            (stage, max_attempts, limit),
        ).fetchall()
        pks = [r["pk"] for r in rows]
        if pks:
            placeholders = ",".join("?" for _ in pks)
            conn.execute(
                f"UPDATE queue SET status = 'running', updated_at = ? "
                f"WHERE stage = ? AND pk IN ({placeholders})",
                (now, stage, *pks),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if not pks:
        return []
    placeholders = ",".join("?" for _ in pks)
    # JOIN with reels so process() receives caption/transcript/etc. We also
    # expose the queue's own bookkeeping under non-colliding names.
    claimed = conn.execute(
        f"SELECT r.*, q.attempts AS queue_attempts, q.status AS queue_status "
        f"FROM queue q JOIN reels r ON r.pk = q.pk "
        f"WHERE q.stage = ? AND q.pk IN ({placeholders})",
        (stage, *pks),
    ).fetchall()
    return [dict(row) for row in claimed]


def mark(conn, pk, stage, status, error=None, inc_attempts=False):
    if inc_attempts:
        conn.execute(
            "UPDATE queue SET status = ?, error = ?, attempts = attempts + 1, "
            "updated_at = ? WHERE pk = ? AND stage = ?",
            (status, error, int(time.time()), pk, stage),
        )
    else:
        conn.execute(
            "UPDATE queue SET status = ?, error = ?, updated_at = ? "
            "WHERE pk = ? AND stage = ?",
            (status, error, int(time.time()), pk, stage),
        )
    conn.commit()


def release(conn, pk, stage):
    # Put a claimed-but-unprocessed item back to 'pending' (used on throttle
    # abort) so a later run retries it without burning an attempt.
    conn.execute(
        "UPDATE queue SET status = 'pending', updated_at = ? "
        "WHERE pk = ? AND stage = ?",
        (int(time.time()), pk, stage),
    )
    conn.commit()


def queue_counts(conn):
    cur = conn.execute(
        "SELECT stage, status, COUNT(*) AS n FROM queue "
        "GROUP BY stage, status ORDER BY stage, status"
    )
    return [dict(row) for row in cur]


def record_stage_run(conn, stage, started_at, ended_at, processed, done,
                     failed, skipped):
    """Append one benchmarking row for a completed drain() run."""
    seconds = float(ended_at - started_at)
    conn.execute(
        "INSERT INTO stage_runs (stage, started_at, ended_at, processed, done, "
        "failed, skipped, seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (stage, int(started_at), int(ended_at), processed, done, failed,
         skipped, seconds),
    )
    conn.commit()


def stage_run_summary(conn):
    """Aggregate stage_runs per stage: total processed/done, total seconds, and
    throughput in items/min. Returns a list of dicts ordered by stage."""
    cur = conn.execute(
        "SELECT stage, "
        "  COUNT(*) AS runs, "
        "  SUM(processed) AS processed, "
        "  SUM(done) AS done, "
        "  SUM(failed) AS failed, "
        "  SUM(skipped) AS skipped, "
        "  SUM(seconds) AS seconds "
        "FROM stage_runs GROUP BY stage ORDER BY stage"
    )
    out = []
    for row in cur:
        d = dict(row)
        secs = d.get("seconds") or 0.0
        proc = d.get("processed") or 0
        d["items_per_min"] = (proc / secs * 60.0) if secs > 0 else 0.0
        out.append(d)
    return out


def all_reels(conn):
    cur = conn.execute("SELECT * FROM reels ORDER BY taken_at DESC")
    out = []
    for row in cur:
        d = dict(row)
        d["categories"] = json.loads(d["categories"]) if d["categories"] else []
        d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
        d["transcript"] = d.get("transcript") or ""
        d["visual"] = d.get("visual") or ""
        out.append(d)
    return out


if __name__ == "__main__":
    # Smoke test with an in-memory DB, no third-party deps.
    c = connect(":memory:")
    init_db(c)
    upsert_reel(c, {"pk": "1", "shortcode": "abc", "url": "u", "source": "saved",
                    "caption": "hello", "thumbnail_url": "t", "taken_at": 0})
    upsert_reel(c, {"pk": "1", "shortcode": "abc"})  # ignored duplicate
    assert len(all_reels(c)) == 1
    assert [r["pk"] for r in iter_uncategorized(c)] == ["1"]
    set_categories(c, "1", ["other"])
    assert list(iter_uncategorized(c)) == []
    assert all_reels(c)[0]["categories"] == ["other"]
    print("db.py self-test ok")
