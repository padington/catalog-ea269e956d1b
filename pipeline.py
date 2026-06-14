"""Stage registry + generic queue-driven worker for the reels pipeline.

The old pipeline discovered work implicitly ("rows where my output column IS
NULL"). This module replaces that with an EXPLICIT per-stage `queue` table
(see db.py) plus one generic `drain()` loop that every stage shares.

A `Stage` declares: its name, the stage it `depends_on`, whether it is
`ig_paced` (touches Instagram, so jittered sleeps + abort-on-throttle apply),
the reels column it fills (`output_col`, or None for download whose only marker
is the queue 'done' status), and the `process`/`write` callables. The DAG lives
in `STAGES`.

`drain(stage)`:
  1. enqueue_ready  — insert 'pending' rows for reels whose dependency is done
     and that still need this stage's output.
  2. loop claim_batch -> process -> write/mark, with the historical
     [HH:MM:SS] logging, periodic progress/eta line, jittered IG sleeps, and
     abort-on-throttle (release the claimed item, break so a later run resumes).
"""

import os
import random
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

import db as dbm


def _log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Throttle detection (moved out of transcribe.py so every IG-paced stage can
# reuse it).
# --------------------------------------------------------------------------- #

_THROTTLE_MARKERS = (
    "feedback_required",
    "please wait",
    "rate limit",
    "ratelimit",
    "429",
    "challenge_required",
)


def is_throttle(exc):
    """True if the exception looks like an IG rate-limit / action-block."""
    haystack = f"{type(exc).__name__} {exc}".lower()
    return any(marker in haystack for marker in _THROTTLE_MARKERS)


class ThrottleError(Exception):
    """Raised by an IG-paced stage's process() on a rate-limit/action-block.

    The driver catches this, releases the claimed item back to 'pending', and
    breaks the drain so a later run resumes the remaining work.
    """


# --------------------------------------------------------------------------- #
# Stage registry.
# --------------------------------------------------------------------------- #

@dataclass
class Stage:
    name: str
    depends_on: Optional[str]
    ig_paced: bool
    output_col: Optional[str]      # reels column this stage fills, or None
    process: Callable              # (item: dict, ctx) -> result
    write: Callable                # (conn, pk, result) -> None
    # Optional raw SQL boolean fragment over alias `r` used INSTEAD OF the
    # `r.{output_col} IS NULL` readiness clause in enqueue_ready. This is a
    # FIXED code constant (never user input), so it is parameter-free and may
    # be string-interpolated safely. enrich uses it to re-enrich placeholder
    # `Reel by @<handle>` captions, not just NULL ones.
    ready_predicate: Optional[str] = None


class Context:
    """Lazily-initialised shared resources passed into every process().

    The instagrapi client is only built when an ig_paced stage first touches
    `.client`, so purely-local stages (transcribe/categorize/tags) never load
    a session or hit the network.
    """

    def __init__(self, conn):
        self.conn = conn
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from enrich import load_client
            self._client = load_client()
        return self._client


# --- write callbacks -------------------------------------------------------- #

def _write_enrich(conn, pk, result):
    dbm.update_reel(conn, pk, result)


def _write_download(conn, pk, result):
    # The queue 'done' status IS the marker; nothing to persist to reels.
    pass


def _write_transcript(conn, pk, result):
    dbm.set_transcript(conn, pk, result)


def _write_categories(conn, pk, result):
    dbm.set_categories(conn, pk, result)


def _write_tags(conn, pk, result):
    dbm.set_tags(conn, pk, result)


def _build_stages():
    import enrich
    import categorize
    import transcribe

    stages = [
        Stage("enrich", None, True, "caption",
              enrich.process, _write_enrich,
              # Re-enrich NULL captions AND DM placeholders (mirrors the old
              # enrich.needs_enrichment). Fixed constant, parameter-free.
              ready_predicate="(r.caption IS NULL OR r.caption LIKE 'Reel by @%')"),
        Stage("download", "enrich", True, None,
              transcribe.download_process, _write_download),
        Stage("transcribe", "download", False, "transcript",
              transcribe.transcribe_process, _write_transcript),
        Stage("categorize", "transcribe", False, "categories",
              categorize.process, _write_categories),
        Stage("tags", "transcribe", False, "tags",
              categorize.tags_process, _write_tags),
    ]
    return OrderedDict((s.name, s) for s in stages)


# Built lazily to avoid import cycles (transcribe imports pipeline).
_STAGES = None


def stages():
    global _STAGES
    if _STAGES is None:
        _STAGES = _build_stages()
    return _STAGES


class _LazyStages:
    """Mapping facade so `STAGES["enrich"]` / iteration work, but the stage
    objects (which import transcribe/categorize) are only built on access."""

    def __getitem__(self, key):
        return stages()[key]

    def __iter__(self):
        return iter(stages())

    def __contains__(self, key):
        return key in stages()

    def items(self):
        return stages().items()

    def keys(self):
        return stages().keys()

    def values(self):
        return stages().values()


STAGES = _LazyStages()


# --------------------------------------------------------------------------- #
# Enqueue + drive.
# --------------------------------------------------------------------------- #

def enqueue_ready(conn, stage_name):
    """Insert 'pending' queue rows for every reel ready to run `stage_name`.

    A reel is ready when (a) its depends_on stage is 'done'/'skipped' in the
    queue (or depends_on is None), AND (b) it is not already present in the
    queue for this stage, AND (c) it still needs this stage's output (output_col
    IS NULL; for download — output_col None — readiness is just dependency +
    not-yet-queued, and download_process no-ops if the mp4 already exists).
    """
    stage = stages()[stage_name]

    wheres = []
    params = []

    # (b) not already queued for this stage
    wheres.append(
        "r.pk NOT IN (SELECT pk FROM queue WHERE stage = ?)"
    )
    params.append(stage_name)

    # (a) dependency satisfied
    if stage.depends_on is not None:
        wheres.append(
            "r.pk IN (SELECT pk FROM queue WHERE stage = ? "
            "AND status IN ('done', 'skipped'))"
        )
        params.append(stage.depends_on)

    # (c) still needs this stage's output. A stage may override the default
    # `r.{output_col} IS NULL` clause with a ready_predicate (a fixed,
    # code-controlled SQL fragment — never user input — so safe to inline).
    if stage.ready_predicate is not None:
        wheres.append(stage.ready_predicate)
    elif stage.output_col is not None:
        # output_col is a fixed, code-controlled identifier (never user input).
        wheres.append(f"r.{stage.output_col} IS NULL")

    sql = (
        "INSERT OR IGNORE INTO queue (pk, stage, status, attempts, updated_at) "
        "SELECT r.pk, ?, 'pending', 0, ? FROM reels r WHERE "
        + " AND ".join(wheres)
    )
    conn.execute(sql, (stage_name, int(time.time()), *params))
    conn.commit()


def _is_empty_terminal(stage, result):
    """A stage may signal a no-op terminal that must never be retried.

    download returns transcribe.NO_VIDEO when the reel has no video. We map it
    to 'skipped'. (transcribe's empty "" transcript is NOT empty-terminal here:
    it is a valid 'done' state handled in the success path below.)
    """
    if stage.name == "download":
        import transcribe
        return result is transcribe.NO_VIDEO
    return False


def drain(conn, stage_name, ctx, limit=None, delay=0.0, max_attempts=3,
          batch=25):
    """Generic worker loop for one stage. See module docstring."""
    stage = stages()[stage_name]
    enqueue_ready(conn, stage_name)

    processed = 0
    done = 0
    start = time.time()
    aborted = False
    seen = set()  # pks handled this drain; never re-process within one run
    while True:
        remaining = None if limit is None else max(limit - processed, 0)
        if remaining == 0:
            break
        take = batch if remaining is None else min(batch, remaining)
        items = dbm.claim_batch(conn, stage_name, take, max_attempts=max_attempts)
        # Drop anything we already handled this drain (e.g. a row we just marked
        # 'failed' that is still retry-eligible) so a failed item is retried on
        # a LATER run, not hammered in this one. claim_batch already flipped it
        # to 'running', so restore its prior status before dropping it.
        fresh = []
        for it in items:
            if it["pk"] in seen:
                # We only re-see items we marked 'failed' this drain; claim_batch
                # just flipped them to 'running', so restore 'failed'.
                dbm.mark(conn, it["pk"], stage_name, "failed")
            else:
                fresh.append(it)
        items = fresh
        if not items:
            break
        for item in items:
            pk = item["pk"]
            seen.add(pk)
            try:
                result = stage.process(item, ctx)
            except ThrottleError as exc:
                # Release this item AND every still-claimed item from the same
                # batch that we have not processed yet, then abort the drain.
                dbm.release(conn, pk, stage_name)
                for other in items:
                    if other["pk"] != pk and other["pk"] not in seen:
                        dbm.release(conn, other["pk"], stage_name)
                _log(f"{stage_name}: throttled on {pk} ({exc}); aborting run so "
                     f"a later run can resume the remaining reels")
                aborted = True
                break
            except Exception as exc:
                dbm.mark(conn, pk, stage_name, "failed", error=str(exc),
                         inc_attempts=True)
                print(f"  fail {pk}: {exc}", flush=True)
                processed += 1
            else:
                if _is_empty_terminal(stage, result):
                    dbm.mark(conn, pk, stage_name, "skipped")
                else:
                    stage.write(conn, pk, result)
                    dbm.mark(conn, pk, stage_name, "done")
                    done += 1
                processed += 1

            if processed and (processed % batch == 0):
                rate = processed / max(time.time() - start, 1e-9)
                _log(f"{stage_name}: {processed} processed | {done} done | "
                     f"{rate*60:.0f}/min")
            if stage.ig_paced and delay:
                time.sleep(delay * random.uniform(0.6, 1.6))
        if aborted:
            break

    _log(f"{stage_name}: done, {done} reel(s) succeeded out of {processed} "
         f"processed")
    return processed


def print_status(conn):
    rows = dbm.queue_counts(conn)
    if not rows:
        print("queue is empty")
        return
    by_stage = OrderedDict()
    for row in rows:
        by_stage.setdefault(row["stage"], {})[row["status"]] = row["n"]
    statuses = ["pending", "running", "done", "skipped", "failed"]
    header = f"{'stage':<12} " + " ".join(f"{s:>8}" for s in statuses)
    print(header)
    print("-" * len(header))
    for stage, counts in by_stage.items():
        line = f"{stage:<12} " + " ".join(
            f"{counts.get(s, 0):>8}" for s in statuses
        )
        print(line)
