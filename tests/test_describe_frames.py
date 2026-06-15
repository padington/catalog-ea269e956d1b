"""Tests for the VLM `describe_frames` stage + per-stage benchmarking stats.

Stdlib unittest, in-memory sqlite, no real ollama/ffmpeg/IG: vlm_describe is
monkeypatched and a tmp frames_dir(pk) with a manifest + dummy jpgs is laid down
so nothing local actually runs.

Run:
    PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import categorize
import db as dbm
import describe_frames
import llm
import pipeline
import storage


def _make_conn():
    conn = dbm.connect(":memory:")
    dbm.init_db(conn)
    return conn


def _lay_frames(media_dir, pk, frame_names, sampling="scene"):
    """Create a frames_dir(pk) under media_dir with dummy jpgs + a manifest."""
    fdir = os.path.join(media_dir, f"{pk}.frames")
    os.makedirs(fdir, exist_ok=True)
    for fn in frame_names:
        with open(os.path.join(fdir, fn), "wb") as f:
            f.write(b"\xff\xd8\xff")  # tiny dummy jpg bytes
    with open(os.path.join(fdir, "manifest.json"), "w") as f:
        json.dump({"sampling": sampling, "frames": frame_names}, f)
    return fdir


class DescribeFramesWiringTests(unittest.TestCase):
    def test_describe_frames_stage_registered(self):
        st = pipeline.stages()
        self.assertIn("describe_frames", st)
        d = st["describe_frames"]
        self.assertEqual(d.depends_on, ["sample_frames"])
        self.assertEqual(d.output_col, "visual")
        self.assertFalse(d.ig_paced)
        names = list(st)
        self.assertEqual(
            names,
            ["enrich", "download", "transcribe", "sample_frames",
             "describe_frames", "categorize", "tags"],
        )
        self.assertLess(names.index("sample_frames"),
                        names.index("describe_frames"))
        self.assertLess(names.index("describe_frames"),
                        names.index("categorize"))


class DescribeFramesDedupTests(unittest.TestCase):
    def test_dedup_collapses_normalized_duplicates(self):
        scenes = [
            {"desc": "Kitchen; baking bread"},
            {"desc": "kitchen, baking bread."},
            {"desc": "Gym, lifting weights"},
            {"desc": "Kitchen; baking bread"},
        ]
        self.assertEqual(
            [s["desc"] for s in describe_frames.dedup_scenes(scenes)],
            ["Kitchen; baking bread", "Gym, lifting weights"],
        )

    def test_dedup_drops_empty(self):
        scenes = [{"desc": ""}, {"desc": "  "}]
        self.assertEqual(describe_frames.dedup_scenes(scenes), [])


class DescribeFramesProcessTests(unittest.TestCase):
    def test_process_returns_deduped_blob(self):
        descs = iter([
            "Kitchen; baking bread",
            "kitchen, baking bread.",
            "Kitchen; baking bread",
            "Gym, lifting weights",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            _lay_frames(tmp, "x",
                        ["0000.jpg", "0001.jpg", "0002.jpg", "0003.jpg"])
            saved_media = storage.MEDIA_DIR
            orig_vlm = describe_frames.vlm_describe
            try:
                storage.MEDIA_DIR = tmp
                describe_frames.vlm_describe = lambda jpg: next(descs)
                blob = describe_frames.describe_frames_process({"pk": "x"}, None)
            finally:
                storage.MEDIA_DIR = saved_media
                describe_frames.vlm_describe = orig_vlm
        self.assertEqual(blob, "Kitchen; baking bread Gym, lifting weights")

    def test_process_drops_failed_frames_no_error_in_blob(self):
        def flaky(jpg):
            if jpg.endswith("0001.jpg"):
                raise RuntimeError("non-zero exit status 234")
            return "Street; walking"
        with tempfile.TemporaryDirectory() as tmp:
            _lay_frames(tmp, "x", ["0000.jpg", "0001.jpg"])
            saved_media = storage.MEDIA_DIR
            orig_vlm = describe_frames.vlm_describe
            try:
                storage.MEDIA_DIR = tmp
                describe_frames.vlm_describe = flaky
                blob = describe_frames.describe_frames_process({"pk": "x"}, None)
            finally:
                storage.MEDIA_DIR = saved_media
                describe_frames.vlm_describe = orig_vlm
        self.assertEqual(blob, "Street; walking")
        self.assertNotIn("234", blob)

    def test_process_missing_manifest_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved_media = storage.MEDIA_DIR
            try:
                storage.MEDIA_DIR = tmp
                with self.assertRaises(FileNotFoundError):
                    describe_frames.describe_frames_process(
                        {"pk": "nope-does-not-exist"}, None)
            finally:
                storage.MEDIA_DIR = saved_media

    def test_process_empty_frames_is_valid_blob(self):
        # An empty manifest frame list -> "" (a valid 'done', not a skip).
        with tempfile.TemporaryDirectory() as tmp:
            _lay_frames(tmp, "x", [], sampling="proportional")
            saved_media = storage.MEDIA_DIR
            orig_vlm = describe_frames.vlm_describe
            try:
                storage.MEDIA_DIR = tmp
                describe_frames.vlm_describe = lambda jpg: "should not run"
                blob = describe_frames.describe_frames_process({"pk": "x"}, None)
            finally:
                storage.MEDIA_DIR = saved_media
                describe_frames.vlm_describe = orig_vlm
        self.assertEqual(blob, "")


class CategorizeVisualSignalTests(unittest.TestCase):
    def test_signal_text_includes_visual_nullsafe(self):
        item = {"caption": "cap", "transcript": "tr", "visual": "vis"}
        text = llm.signal_text(item)
        self.assertIn("cap", text)
        self.assertIn("tr", text)
        self.assertIn("vis", text)

    def test_signal_text_visual_null_ok(self):
        item = {"caption": "cap", "transcript": "tr", "visual": None}
        text = llm.signal_text(item)
        self.assertIn("cap", text)
        self.assertIn("tr", text)
        text2 = llm.signal_text({"caption": "cap"})
        self.assertIn("cap", text2)

    def test_process_and_tags_use_visual(self):
        import tags as tagsmod
        captured = {}
        orig_cat = categorize.categorize_caption
        orig_tags = tagsmod.generate_tags
        try:
            categorize.categorize_caption = lambda t: captured.setdefault("cat", t) or ["other"]
            tagsmod.generate_tags = lambda t: captured.setdefault("tag", t) or []
            item = {"pk": "1", "caption": "cap", "transcript": "tr", "visual": "kitchen scene"}
            categorize.process(item, None)
            tagsmod.tags_process(item, None)
        finally:
            categorize.categorize_caption = orig_cat
            tagsmod.generate_tags = orig_tags
        self.assertIn("kitchen scene", captured["cat"])
        self.assertIn("kitchen scene", captured["tag"])


class VisualColumnAndStatsTests(unittest.TestCase):
    def test_visual_column_migration_and_set_visual(self):
        conn = _make_conn()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(reels)")}
        self.assertIn("visual", cols)
        dbm.upsert_reel(conn, {"pk": "1", "shortcode": "a", "caption": "c"})
        v = conn.execute("SELECT visual FROM reels WHERE pk='1'").fetchone()["visual"]
        self.assertIsNone(v)
        dbm.set_visual(conn, "1", "kitchen; baking")
        v = conn.execute("SELECT visual FROM reels WHERE pk='1'").fetchone()["visual"]
        self.assertEqual(v, "kitchen; baking")
        dbm.set_visual(conn, "1", "")
        v = conn.execute("SELECT visual FROM reels WHERE pk='1'").fetchone()["visual"]
        self.assertEqual(v, "")

    def test_visual_flows_into_claimed_item(self):
        conn = _make_conn()
        dbm.upsert_reel(conn, {"pk": "1", "shortcode": "a", "caption": "c"})
        dbm.set_visual(conn, "1", "gym scene")
        dbm.enqueue(conn, "1", "categorize")
        claimed = dbm.claim_batch(conn, "categorize", 10)
        self.assertEqual(claimed[0].get("visual"), "gym scene")

    def test_backfill_marks_vision_done_for_nonnull_visual(self):
        conn = _make_conn()
        dbm.upsert_reel(conn, {"pk": "1", "shortcode": "a", "caption": "c"})
        dbm.set_visual(conn, "1", "")  # "" is non-NULL -> both stages done
        summary = dbm.backfill_queue(conn)
        self.assertEqual(summary.get(("sample_frames", "done")), 1)
        self.assertEqual(summary.get(("describe_frames", "done")), 1)
        sf = conn.execute(
            "SELECT status FROM queue WHERE pk='1' AND stage='sample_frames'"
        ).fetchone()
        df = conn.execute(
            "SELECT status FROM queue WHERE pk='1' AND stage='describe_frames'"
        ).fetchone()
        self.assertEqual(sf["status"], "done")
        self.assertEqual(df["status"], "done")

    def test_record_and_summary_roundtrip(self):
        conn = _make_conn()
        dbm.record_stage_run(conn, "describe_frames", 1000, 1060, processed=10,
                             done=8, failed=1, skipped=1)
        dbm.record_stage_run(conn, "describe_frames", 2000, 2060, processed=2,
                             done=2, failed=0, skipped=0)
        rows = dbm.stage_run_summary(conn)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["stage"], "describe_frames")
        self.assertEqual(r["processed"], 12)
        self.assertEqual(r["done"], 10)
        self.assertEqual(r["seconds"], 120.0)
        self.assertAlmostEqual(r["items_per_min"], 6.0, places=3)

    def test_drain_records_a_stage_run(self):
        # A real drain on a stub stage must append exactly one stage_runs row.
        conn = _make_conn()
        dbm.upsert_reel(conn, {"pk": "1", "shortcode": "a", "caption": "c"})
        conn.execute("UPDATE reels SET visual=NULL WHERE pk='1'")
        conn.commit()

        def write(c, pk, result):
            dbm.set_visual(c, pk, result)

        from pipeline import Stage
        stages = {"describe_frames": Stage("describe_frames", [], False, "visual",
                                           lambda i, c: "blob", write)}
        saved = pipeline._STAGES
        pipeline._STAGES = stages
        try:
            pipeline.drain(conn, "describe_frames", object())
        finally:
            pipeline._STAGES = saved
        runs = conn.execute(
            "SELECT stage, processed, done FROM stage_runs "
            "WHERE stage='describe_frames'"
        ).fetchall()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["done"], 1)


if __name__ == "__main__":
    unittest.main()
