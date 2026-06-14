"""Driver + queue tests. Stdlib unittest, in-memory sqlite, no IG/ollama/whisper.

Run:
    PYTHONPATH=. .venv/bin/python -m pytest tests/
    PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as dbm
import pipeline
from pipeline import Stage, ThrottleError


def _seed(conn, *pks):
    for pk in pks:
        dbm.upsert_reel(conn, {"pk": pk, "shortcode": pk, "caption": "c-" + pk})


def _make_conn():
    conn = dbm.connect(":memory:")
    dbm.init_db(conn)
    return conn


class StubCtx:
    """Context stand-in; .client raises so we prove local stages never use it."""

    def __init__(self, conn):
        self.conn = conn

    @property
    def client(self):
        raise AssertionError("stub stages must not touch the IG client")


def _stub_stage(name, process, depends_on=(), ig_paced=False,
                output_col="caption"):
    def write(conn, pk, result):
        dbm.update_reel(conn, pk, {"caption": result})
    return Stage(name, depends_on, ig_paced, output_col, process, write)


def _install(monkey_stages):
    """Swap pipeline's lazy STAGES registry for a fixed dict, restore on exit."""
    saved = pipeline._STAGES
    pipeline._STAGES = monkey_stages
    return saved


class QueueHelperTests(unittest.TestCase):
    def test_enqueue_idempotent_and_no_reset(self):
        conn = _make_conn()
        _seed(conn, "1")
        dbm.enqueue(conn, "1", "s")
        dbm.mark(conn, "1", "s", "done")
        dbm.enqueue(conn, "1", "s")  # must NOT reset
        rows = dbm.queue_counts(conn)
        self.assertEqual(rows, [{"stage": "s", "status": "done", "n": 1}])

    def test_claim_mark_release_roundtrip(self):
        conn = _make_conn()
        _seed(conn, "1", "2")
        dbm.enqueue(conn, "1", "s")
        dbm.enqueue(conn, "2", "s")
        claimed = dbm.claim_batch(conn, "s", 10)
        self.assertEqual({c["pk"] for c in claimed}, {"1", "2"})
        # claimed rows are now 'running' and joined with reels
        self.assertTrue(all("caption" in c for c in claimed))
        running = {r["status"] for r in dbm.queue_counts(conn)}
        self.assertEqual(running, {"running"})
        # nothing left to claim while running
        self.assertEqual(dbm.claim_batch(conn, "s", 10), [])
        dbm.mark(conn, "1", "s", "done")
        dbm.release(conn, "2", "s")
        # released item is claimable again
        again = dbm.claim_batch(conn, "s", 10)
        self.assertEqual([c["pk"] for c in again], ["2"])

    def test_mark_increments_attempts(self):
        conn = _make_conn()
        _seed(conn, "1")
        dbm.enqueue(conn, "1", "s")
        dbm.claim_batch(conn, "s", 10)
        dbm.mark(conn, "1", "s", "failed", error="boom", inc_attempts=True)
        row = conn.execute(
            "SELECT attempts, status, error FROM queue WHERE pk='1'"
        ).fetchone()
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "boom")


class DriverTests(unittest.TestCase):
    def test_enqueue_ready_respects_depends_on(self):
        conn = _make_conn()
        _seed(conn, "1")
        # caption NULL gates "a"; transcript NULL gates "b".
        conn.execute("UPDATE reels SET caption=NULL, transcript=NULL WHERE pk='1'")
        conn.commit()
        stages = {
            "a": _stub_stage("a", lambda i, c: "A", output_col="caption"),
            "b": _stub_stage("b", lambda i, c: "B", depends_on=["a"],
                             output_col="transcript"),
        }
        saved = _install(stages)
        try:
            # b should NOT enqueue until a is done
            pipeline.enqueue_ready(conn, "b")
            self.assertEqual(dbm.queue_counts(conn), [])
            pipeline.enqueue_ready(conn, "a")
            dbm.mark(conn, "1", "a", "done")
            pipeline.enqueue_ready(conn, "b")
            pend = [r for r in dbm.queue_counts(conn)
                    if r["stage"] == "b" and r["status"] == "pending"]
            self.assertEqual(pend[0]["n"], 1)
        finally:
            pipeline._STAGES = saved

    def test_success_marks_done_and_writes(self):
        conn = _make_conn()
        _seed(conn, "1")
        # set caption NULL so output_col gating works, then write fills it
        conn.execute("UPDATE reels SET caption=NULL WHERE pk='1'")
        conn.commit()
        stages = {"a": _stub_stage("a", lambda i, c: "WROTE")}
        saved = _install(stages)
        try:
            pipeline.drain(conn, "a", StubCtx(conn))
            row = conn.execute(
                "SELECT status FROM queue WHERE pk='1' AND stage='a'"
            ).fetchone()
            self.assertEqual(row["status"], "done")
            cap = conn.execute(
                "SELECT caption FROM reels WHERE pk='1'"
            ).fetchone()["caption"]
            self.assertEqual(cap, "WROTE")
        finally:
            pipeline._STAGES = saved

    def test_failure_marks_failed_and_increments(self):
        conn = _make_conn()
        _seed(conn, "1")
        conn.execute("UPDATE reels SET caption=NULL WHERE pk='1'")
        conn.commit()

        def boom(item, ctx):
            raise RuntimeError("kaboom")

        stages = {"a": _stub_stage("a", boom)}
        saved = _install(stages)
        try:
            pipeline.drain(conn, "a", StubCtx(conn))
            row = conn.execute(
                "SELECT status, attempts FROM queue WHERE pk='1'"
            ).fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["attempts"], 1)
        finally:
            pipeline._STAGES = saved

    def test_failed_retried_until_max_attempts(self):
        conn = _make_conn()
        _seed(conn, "1")
        # pre-set the row to failed at the cap; claim_batch must skip it
        dbm.enqueue(conn, "1", "s")
        conn.execute(
            "UPDATE queue SET status='failed', attempts=3 WHERE pk='1'"
        )
        conn.commit()
        self.assertEqual(dbm.claim_batch(conn, "s", 10, max_attempts=3), [])
        # one below the cap IS claimable
        conn.execute("UPDATE queue SET attempts=2 WHERE pk='1'")
        conn.commit()
        self.assertEqual(
            [c["pk"] for c in dbm.claim_batch(conn, "s", 10, max_attempts=3)],
            ["1"],
        )

    def test_throttle_releases_and_breaks(self):
        conn = _make_conn()
        _seed(conn, "1", "2")
        conn.execute("UPDATE reels SET caption=NULL")
        conn.commit()
        calls = []

        def proc(item, ctx):
            calls.append(item["pk"])
            raise ThrottleError("please wait")

        stages = {"a": _stub_stage("a", proc, ig_paced=True)}
        saved = _install(stages)
        try:
            pipeline.drain(conn, "a", StubCtx(conn))
            # only the first item was attempted (drain broke)
            self.assertEqual(len(calls), 1)
            statuses = {r["status"] for r in dbm.queue_counts(conn)}
            # the throttled item was released back to pending; nothing 'running'
            self.assertNotIn("running", statuses)
            self.assertIn("pending", statuses)
        finally:
            pipeline._STAGES = saved

    def test_network_wall_releases_without_penalty(self):
        # N consecutive ConnectionError("Connection refused") from an ig_paced
        # stage -> drain aborts after net_abort_after; rows stay pending with
        # attempts unchanged (the reels did not fail on their own merits).
        conn = _make_conn()
        _seed(conn, "1", "2", "3", "4")
        conn.execute("UPDATE reels SET caption=NULL")
        conn.commit()
        calls = []

        def proc(item, ctx):
            calls.append(item["pk"])
            raise ConnectionError("Connection refused")

        stages = {"a": _stub_stage("a", proc, ig_paced=True)}
        saved = _install(stages)
        try:
            pipeline.drain(conn, "a", StubCtx(conn), net_abort_after=3)
            # aborted after the 3rd consecutive network error
            self.assertEqual(len(calls), 3)
            rows = conn.execute(
                "SELECT status, attempts FROM queue WHERE stage='a'"
            ).fetchall()
            self.assertEqual({r["status"] for r in rows}, {"pending"})
            self.assertTrue(all(r["attempts"] == 0 for r in rows))
        finally:
            pipeline._STAGES = saved

    def test_network_blip_then_success_does_not_abort(self):
        # A single network blip then successes -> no abort, counter resets.
        # The blip item (first in batch) is released to pending and not
        # re-hammered this run; the rest succeed.
        conn = _make_conn()
        _seed(conn, "1", "2", "3")
        conn.execute("UPDATE reels SET caption=NULL")
        conn.commit()
        calls = []

        def proc(item, ctx):
            calls.append(item["pk"])
            if item["pk"] == "1":
                raise ConnectionError("Connection refused")
            return "OK"

        stages = {"a": _stub_stage("a", proc, ig_paced=True)}
        saved = _install(stages)
        try:
            pipeline.drain(conn, "a", StubCtx(conn), net_abort_after=3)
            statuses = {r["status"] for r in dbm.queue_counts(conn)}
            # never aborted: nothing left running, no wall reached
            self.assertNotIn("running", statuses)
            # "1" hit one blip; counter reset on each subsequent success so the
            # 3-error wall is never reached. 2 & 3 went done.
            done_pks = conn.execute(
                "SELECT pk FROM queue WHERE stage='a' AND status='done'"
            ).fetchall()
            self.assertEqual({r["pk"] for r in done_pks}, {"2", "3"})
            # the blip item was released without burning an attempt
            r1 = conn.execute(
                "SELECT status, attempts FROM queue WHERE pk='1'"
            ).fetchone()
            self.assertEqual(r1["attempts"], 0)
            # it was only attempted once this run (in seen, not re-hammered)
            self.assertEqual(calls.count("1"), 1)
        finally:
            pipeline._STAGES = saved

    def test_non_network_exception_still_fails(self):
        # A normal (non-network) exception -> still 'failed' with attempts++.
        conn = _make_conn()
        _seed(conn, "1")
        conn.execute("UPDATE reels SET caption=NULL WHERE pk='1'")
        conn.commit()

        def boom(item, ctx):
            raise ValueError("bad value")

        stages = {"a": _stub_stage("a", boom, ig_paced=True)}
        saved = _install(stages)
        try:
            pipeline.drain(conn, "a", StubCtx(conn), net_abort_after=3)
            row = conn.execute(
                "SELECT status, attempts FROM queue WHERE pk='1'"
            ).fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["attempts"], 1)
        finally:
            pipeline._STAGES = saved

    def test_empty_transcript_is_done_not_retried(self):
        # transcribe returning "" is a valid terminal -> 'done', writes "".
        conn = _make_conn()
        _seed(conn, "1")
        conn.execute("UPDATE reels SET transcript=NULL WHERE pk='1'")
        conn.commit()

        def write(conn_, pk, result):
            dbm.set_transcript(conn_, pk, result)

        stages = {
            "transcribe": Stage("transcribe", None, False, "transcript",
                                lambda i, c: "", write)
        }
        saved = _install(stages)
        try:
            pipeline.drain(conn, "transcribe", StubCtx(conn))
            row = conn.execute(
                "SELECT status FROM queue WHERE pk='1'"
            ).fetchone()
            self.assertEqual(row["status"], "done")
            t = conn.execute(
                "SELECT transcript FROM reels WHERE pk='1'"
            ).fetchone()["transcript"]
            self.assertEqual(t, "")
        finally:
            pipeline._STAGES = saved


if __name__ == "__main__":
    unittest.main()
