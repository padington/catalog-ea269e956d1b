"""Lossless backfill/migration tests. Stdlib unittest, in-memory sqlite.

No IG/ollama/whisper, no real media: the mp4-on-disk branch points
transcribe.MEDIA_DIR at a tempdir holding a fake <pk>.mp4.

Run:
    PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as dbm
import pipeline
import transcribe


def _make_conn():
    conn = dbm.connect(":memory:")
    dbm.init_db(conn)
    return conn


def _set(conn, pk, **cols):
    """Directly seed reels columns (categories/tags stored as JSON like prod)."""
    dbm.upsert_reel(conn, {"pk": pk, "shortcode": pk})
    for col, val in cols.items():
        if col in ("categories", "tags") and val is not None:
            val = json.dumps(val)
        conn.execute(f"UPDATE reels SET {col} = ? WHERE pk = ?", (val, pk))
    conn.commit()


def _statuses(conn):
    return {(r["pk"], r["stage"]): r["status"]
            for r in conn.execute(
                "SELECT pk, stage, status FROM queue").fetchall()}


def _snapshot_reels(conn):
    """Full byte-level snapshot of the reels table for content-loss assertions."""
    rows = conn.execute("SELECT * FROM reels ORDER BY pk").fetchall()
    return [tuple(r) for r in rows]


class _RaisingStages:
    """Real STAGES facade but every process() raises if invoked.

    Proves a drain processes nothing when everything is already terminal.
    """

    def __init__(self):
        base = pipeline.stages()
        self._d = {}
        for name, s in base.items():
            def boom(item, ctx, _name=name):
                raise AssertionError(f"{_name}.process called — reprocessing!")
            self._d[name] = pipeline.Stage(
                s.name, s.depends_on, s.ig_paced, s.output_col,
                boom, s.write, s.ready_predicate)

    def __getitem__(self, k):
        return self._d[k]

    def __iter__(self):
        return iter(self._d)

    def __contains__(self, k):
        return k in self._d

    def items(self):
        return self._d.items()

    def keys(self):
        return self._d.keys()

    def values(self):
        return self._d.values()


class StubCtx:
    def __init__(self, conn):
        self.conn = conn

    @property
    def client(self):
        raise AssertionError("backfill drains must not touch the IG client")


class BackfillTests(unittest.TestCase):

    def test_fully_populated_reel_all_done_and_no_reprocessing(self):
        conn = _make_conn()
        _set(conn, "1", caption="A real caption", transcript="hello world",
             categories=["cooking"], tags=["pasta"])
        before = _snapshot_reels(conn)

        summary = dbm.backfill_queue(conn)
        st = _statuses(conn)
        self.assertEqual(st[("1", "enrich")], "done")
        self.assertEqual(st[("1", "download")], "done")
        self.assertEqual(st[("1", "transcribe")], "done")
        self.assertEqual(st[("1", "categorize")], "done")
        self.assertEqual(st[("1", "tags")], "done")
        self.assertEqual(summary.get(("enrich", "done")), 1)
        self.assertEqual(summary.get(("categorize", "done")), 1)

        # Drain every stage with process()es that RAISE if called. Nothing is
        # pending, so nothing is processed -> proves no reprocessing.
        saved = pipeline._STAGES
        pipeline._STAGES = _RaisingStages()
        try:
            for stage in ("enrich", "download", "transcribe",
                          "categorize", "tags"):
                processed = pipeline.drain(conn, stage, StubCtx(conn))
                self.assertEqual(processed, 0)
        finally:
            pipeline._STAGES = saved

        # reels content is byte-identical: backfill never touched it.
        self.assertEqual(_snapshot_reels(conn), before)

    def test_empty_transcript_skipped_not_retried(self):
        conn = _make_conn()
        _set(conn, "1", caption="real", transcript="")
        dbm.backfill_queue(conn)
        st = _statuses(conn)
        self.assertEqual(st[("1", "download")], "skipped")
        self.assertEqual(st[("1", "transcribe")], "skipped")
        # enqueue_ready must not re-add a pending row for a skipped stage.
        pipeline.enqueue_ready(conn, "download")
        pipeline.enqueue_ready(conn, "transcribe")
        st2 = _statuses(conn)
        self.assertEqual(st2[("1", "download")], "skipped")
        self.assertEqual(st2[("1", "transcribe")], "skipped")

    def test_placeholder_caption_enrich_pending(self):
        conn = _make_conn()
        _set(conn, "1", caption="Reel by @someone")
        dbm.backfill_queue(conn)
        st = _statuses(conn)
        # enrich NOT done — placeholder is not real content.
        self.assertNotIn(("1", "enrich"), st)
        pipeline.enqueue_ready(conn, "enrich")
        st2 = _statuses(conn)
        self.assertEqual(st2[("1", "enrich")], "pending")

    def test_real_caption_only_downstream_gated(self):
        conn = _make_conn()
        _set(conn, "1", caption="Genuinely useful caption text")
        dbm.backfill_queue(conn)
        st = _statuses(conn)
        self.assertEqual(st[("1", "enrich")], "done")
        # no transcript/categories/tags -> only enrich is terminal
        self.assertNotIn(("1", "download"), st)
        self.assertNotIn(("1", "transcribe"), st)
        self.assertNotIn(("1", "categorize"), st)
        self.assertNotIn(("1", "tags"), st)

        # download is pending after enrich; transcribe/categorize/tags gated.
        for stage in ("enrich", "download", "transcribe", "categorize", "tags"):
            pipeline.enqueue_ready(conn, stage)
        st2 = _statuses(conn)
        self.assertEqual(st2[("1", "download")], "pending")
        # transcribe blocked until download done; categorize/tags until transcribe
        self.assertNotIn(("1", "transcribe"), st2)
        self.assertNotIn(("1", "categorize"), st2)
        self.assertNotIn(("1", "tags"), st2)

    def test_mp4_on_disk_marks_download_done(self):
        conn = _make_conn()
        _set(conn, "1", caption="real", transcript=None)
        with tempfile.TemporaryDirectory() as tmp:
            mp4 = os.path.join(tmp, "1.mp4")
            with open(mp4, "wb") as f:
                f.write(b"\x00\x01\x02fakevideo")
            saved = transcribe.MEDIA_DIR
            transcribe.MEDIA_DIR = tmp
            try:
                dbm.backfill_queue(conn)
            finally:
                transcribe.MEDIA_DIR = saved
        st = _statuses(conn)
        # download done even though transcript IS NULL (mp4 present on disk).
        self.assertEqual(st[("1", "download")], "done")
        # transcript still NULL, so transcribe is NOT terminal.
        self.assertNotIn(("1", "transcribe"), st)

    def test_backfill_idempotent_never_flips_done(self):
        conn = _make_conn()
        _set(conn, "1", caption="real", transcript="t",
             categories=["x"], tags=["y"])
        s1 = dbm.backfill_queue(conn)
        q1 = sorted(
            tuple(r) for r in conn.execute(
                "SELECT pk, stage, status, attempts FROM queue").fetchall())
        s2 = dbm.backfill_queue(conn)
        q2 = sorted(
            tuple(r) for r in conn.execute(
                "SELECT pk, stage, status, attempts FROM queue").fetchall())
        self.assertEqual(q1, q2)            # identical queue contents
        self.assertEqual(s2, {})            # second run inserts nothing
        self.assertTrue(s1)                 # first run did insert
        # No status flipped away from done.
        for (_pk, _stage, status, _att) in q2:
            self.assertIn(status, ("done", "skipped", "pending"))

    def test_migrate_command_is_lossless_and_idempotent(self):
        # Exercise run.cmd_migrate end-to-end against a throwaway file DB
        # (NOT reels.db). Assert the reels table is byte-identical before and
        # after, the queue gets seeded, and a second run is a no-op.
        import run

        tmpdir = tempfile.mkdtemp()
        dbpath = os.path.join(tmpdir, "mig.db")
        seed = dbm.connect(dbpath)
        dbm.init_db(seed)
        # Seed like the old pipeline did, directly on this file DB.
        for pk, cap, tr, cat, tg in [
            ("1", "Real caption", "spoken words", ["cooking"], ["pasta"]),
            ("2", "Reel by @x", None, None, None),          # placeholder, bare
            ("3", "Another real one", "", None, None),       # no-video sentinel
        ]:
            dbm.upsert_reel(seed, {"pk": pk, "shortcode": pk})
            seed.execute("UPDATE reels SET caption=?, transcript=? WHERE pk=?",
                         (cap, tr, pk))
            if cat is not None:
                dbm.set_categories(seed, pk, cat)
            if tg is not None:
                dbm.set_tags(seed, pk, tg)
        seed.commit()
        before = _snapshot_reels(seed)
        seed.close()

        saved_db = run.DB
        saved_media = transcribe.MEDIA_DIR
        run.DB = dbpath
        transcribe.MEDIA_DIR = tmpdir   # empty -> no stray mp4 matches
        try:
            run.cmd_migrate(None)   # first migration
            run.cmd_migrate(None)   # idempotent second run
        finally:
            run.DB = saved_db
            transcribe.MEDIA_DIR = saved_media

        check = dbm.connect(dbpath)
        try:
            self.assertEqual(_snapshot_reels(check), before)  # byte-identical
            st = _statuses(check)
            self.assertEqual(st[("1", "categorize")], "done")
            self.assertEqual(st[("1", "tags")], "done")
            self.assertEqual(st[("3", "download")], "skipped")
            self.assertEqual(st[("2", "enrich")], "pending")  # placeholder redo
        finally:
            check.close()

    def test_existing_categories_done_even_if_upstream_pending(self):
        conn = _make_conn()
        # placeholder caption (enrich will be pending) but categories already set.
        _set(conn, "1", caption="Reel by @x", categories=["tech"])
        dbm.backfill_queue(conn)
        st = _statuses(conn)
        self.assertEqual(st[("1", "categorize")], "done")
        self.assertNotIn(("1", "enrich"), st)  # enrich not done (placeholder)

        # Running enqueue_ready for everything must NOT re-enqueue categorize
        # (it's already terminal) and must NOT mark enrich done.
        for stage in ("enrich", "download", "transcribe", "categorize", "tags"):
            pipeline.enqueue_ready(conn, stage)
        st2 = _statuses(conn)
        self.assertEqual(st2[("1", "categorize")], "done")  # preserved
        self.assertEqual(st2[("1", "enrich")], "pending")   # placeholder -> redo


if __name__ == "__main__":
    unittest.main()
