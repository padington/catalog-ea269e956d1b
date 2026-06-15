"""Skip-propagation tests. Stdlib unittest, in-memory sqlite, REAL registry.

A no-video reel (download 'skipped') must cascade the skip down every
media-consuming stage (extract_audio, transcribe, sample_frames,
describe_frames) so those artifacts are never waited on forever — while
categorize/tags still run on caption alone (they do NOT cascade).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as dbm
import pipeline


def _make_conn():
    conn = dbm.connect(":memory:")
    dbm.init_db(conn)
    return conn


def _statuses(conn):
    return {(r["pk"], r["stage"]): r["status"]
            for r in conn.execute(
                "SELECT pk, stage, status FROM queue").fetchall()}


# DAG topo order used to drive enqueue_ready in every test.
_ORDER = ("enrich", "download", "extract_audio", "transcribe",
          "sample_frames", "describe_frames", "categorize", "tags")


class SkipPropagationTests(unittest.TestCase):
    def test_no_video_reel_cascades_skip_but_categorize_tags_run(self):
        conn = _make_conn()
        dbm.upsert_reel(conn, {"pk": "1", "shortcode": "1",
                               "caption": "a real caption"})
        # enrich done (real caption already present); download SKIPPED (no video).
        dbm.enqueue(conn, "1", "enrich")
        dbm.mark(conn, "1", "enrich", "done")
        dbm.enqueue(conn, "1", "download")
        dbm.mark(conn, "1", "download", "skipped")

        for stage in _ORDER:
            pipeline.enqueue_ready(conn, stage)

        st = _statuses(conn)
        # the four media stages cascade-skipped
        self.assertEqual(st[("1", "extract_audio")], "skipped")
        self.assertEqual(st[("1", "transcribe")], "skipped")
        self.assertEqual(st[("1", "sample_frames")], "skipped")
        self.assertEqual(st[("1", "describe_frames")], "skipped")
        # categorize/tags still run on caption alone — pending, NOT skipped
        self.assertEqual(st[("1", "categorize")], "pending")
        self.assertEqual(st[("1", "tags")], "pending")

    def test_normal_reel_no_spurious_skips(self):
        conn = _make_conn()
        dbm.upsert_reel(conn, {"pk": "2", "shortcode": "2",
                               "caption": "another real caption"})
        # enrich done; download DONE (video present).
        dbm.enqueue(conn, "2", "enrich")
        dbm.mark(conn, "2", "enrich", "done")
        dbm.enqueue(conn, "2", "download")
        dbm.mark(conn, "2", "download", "done")

        for stage in _ORDER:
            pipeline.enqueue_ready(conn, stage)

        st = _statuses(conn)
        # nothing got spuriously skipped: media stages are pending (ready to run)
        self.assertEqual(st[("2", "extract_audio")], "pending")
        self.assertEqual(st[("2", "sample_frames")], "pending")
        # downstream of those stays gated (parent not yet done), so not skipped
        self.assertNotEqual(st.get(("2", "transcribe")), "skipped")
        self.assertNotEqual(st.get(("2", "describe_frames")), "skipped")
        self.assertNotEqual(st.get(("2", "categorize")), "skipped")
        self.assertNotEqual(st.get(("2", "tags")), "skipped")


if __name__ == "__main__":
    unittest.main()
