"""Tests for the additive VLM `vision` stage + per-stage benchmarking stats.

Stdlib unittest, in-memory sqlite, no real ollama/ffmpeg/IG: vlm_describe and
describe_video are monkeypatched so nothing local actually runs.

Run:
    PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import categorize
import db as dbm
import llm
import pipeline
import vision


def _make_conn():
    conn = dbm.connect(":memory:")
    dbm.init_db(conn)
    return conn


class VisionStageWiringTests(unittest.TestCase):
    def test_vision_stage_registered(self):
        st = pipeline.stages()
        self.assertIn("vision", st)
        v = st["vision"]
        self.assertEqual(v.depends_on, "download")
        self.assertEqual(v.output_col, "visual")
        self.assertFalse(v.ig_paced)
        # vision sits right after transcribe, before categorize/tags.
        names = list(st)
        self.assertEqual(
            names,
            ["enrich", "download", "transcribe", "vision", "categorize",
             "tags"],
        )
        self.assertLess(names.index("transcribe"), names.index("vision"))
        self.assertLess(names.index("vision"), names.index("categorize"))


class VisionDedupTests(unittest.TestCase):
    def test_dedup_collapses_normalized_duplicates(self):
        scenes = [
            {"t": 0.0, "desc": "Kitchen; baking bread"},
            {"t": 1.0, "desc": "kitchen, baking bread."},
            {"t": 2.0, "desc": "Gym, lifting weights"},
            {"t": 3.0, "desc": "Kitchen; baking bread"},
        ]
        self.assertEqual(
            [s["desc"] for s in vision.dedup_scenes(scenes)],
            ["Kitchen; baking bread", "Gym, lifting weights"],
        )

    def test_dedup_drops_empty(self):
        scenes = [{"t": 0.0, "desc": ""}, {"t": 1.0, "desc": "  "}]
        self.assertEqual(vision.dedup_scenes(scenes), [])


class VisionProcessTests(unittest.TestCase):
    def test_process_returns_deduped_blob(self):
        # Fake 4 frames; vlm returns a 4x-repeated kitchen scene then a gym one.
        descs = iter([
            "Kitchen; baking bread",
            "kitchen, baking bread.",
            "Kitchen; baking bread",
            "Gym, lifting weights",
        ])
        orig_scene = vision.scene_frames
        orig_dur = vision.duration
        orig_vlm = vision.vlm_describe
        try:
            vision.duration = lambda mp4: 10.0
            vision.scene_frames = lambda mp4, outdir, **kw: [
                (0.0, "a.jpg"), (1.0, "b.jpg"), (2.0, "c.jpg"), (3.0, "d.jpg")]
            vision.vlm_describe = lambda jpg: next(descs)
            blob, scenes, sampling = vision.describe_video("/fake.mp4")
        finally:
            vision.scene_frames = orig_scene
            vision.duration = orig_dur
            vision.vlm_describe = orig_vlm
        self.assertEqual(sampling, "scene")
        self.assertEqual(blob, "Kitchen; baking bread Gym, lifting weights")
        self.assertEqual(len(scenes), 2)

    def test_process_drops_failed_frames_no_error_in_blob(self):
        def flaky(jpg):
            if jpg == "bad.jpg":
                raise RuntimeError("non-zero exit status 234")
            return "Street; walking"
        orig_scene = vision.scene_frames
        orig_dur = vision.duration
        orig_vlm = vision.vlm_describe
        try:
            vision.duration = lambda mp4: 5.0
            vision.scene_frames = lambda mp4, outdir, **kw: [
                (0.0, "good.jpg"), (1.0, "bad.jpg")]
            vision.vlm_describe = flaky
            blob, scenes, _ = vision.describe_video("/fake.mp4")
        finally:
            vision.scene_frames = orig_scene
            vision.duration = orig_dur
            vision.vlm_describe = orig_vlm
        self.assertEqual(blob, "Street; walking")
        self.assertNotIn("234", blob)

    def test_process_missing_mp4_raises(self):
        with self.assertRaises(FileNotFoundError):
            vision.process({"pk": "nope-does-not-exist"}, None)

    def test_process_empty_blob_is_valid(self):
        # No usable frames -> "" (a valid 'done', not a skip). Patch the helpers
        # plus os.path checks so the missing-mp4 guard passes.
        import os as _os
        orig_exists = _os.path.exists
        orig_size = _os.path.getsize
        orig_describe = vision.describe_video
        try:
            vision.describe_video = lambda mp4: ("", [], "proportional")
            _os.path.exists = lambda p: True
            _os.path.getsize = lambda p: 100
            self.assertEqual(vision.process({"pk": "x"}, None), "")
        finally:
            vision.describe_video = orig_describe
            _os.path.exists = orig_exists
            _os.path.getsize = orig_size


class CategorizeVisualSignalTests(unittest.TestCase):
    def test_signal_text_includes_visual_nullsafe(self):
        item = {"caption": "cap", "transcript": "tr", "visual": "vis"}
        text = llm.signal_text(item)
        self.assertIn("cap", text)
        self.assertIn("tr", text)
        self.assertIn("vis", text)

    def test_signal_text_visual_null_ok(self):
        # visual NULL/missing must not raise and must still carry caption+transcript.
        item = {"caption": "cap", "transcript": "tr", "visual": None}
        text = llm.signal_text(item)
        self.assertIn("cap", text)
        self.assertIn("tr", text)
        # missing key entirely
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
        # default NULL
        v = conn.execute("SELECT visual FROM reels WHERE pk='1'").fetchone()["visual"]
        self.assertIsNone(v)
        dbm.set_visual(conn, "1", "kitchen; baking")
        v = conn.execute("SELECT visual FROM reels WHERE pk='1'").fetchone()["visual"]
        self.assertEqual(v, "kitchen; baking")
        # "" is a valid stored terminal
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
        dbm.set_visual(conn, "1", "")  # "" is non-NULL -> vision done
        summary = dbm.backfill_queue(conn)
        self.assertEqual(summary.get(("vision", "done")), 1)
        row = conn.execute(
            "SELECT status FROM queue WHERE pk='1' AND stage='vision'"
        ).fetchone()
        self.assertEqual(row["status"], "done")

    def test_record_and_summary_roundtrip(self):
        conn = _make_conn()
        dbm.record_stage_run(conn, "vision", 1000, 1060, processed=10, done=8,
                             failed=1, skipped=1)
        dbm.record_stage_run(conn, "vision", 2000, 2060, processed=2, done=2,
                             failed=0, skipped=0)
        rows = dbm.stage_run_summary(conn)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["stage"], "vision")
        self.assertEqual(r["processed"], 12)
        self.assertEqual(r["done"], 10)
        self.assertEqual(r["seconds"], 120.0)
        # 12 items / 120s = 6/min
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
        stages = {"vision": Stage("vision", None, False, "visual",
                                  lambda i, c: "blob", write)}
        saved = pipeline._STAGES
        pipeline._STAGES = stages
        try:
            pipeline.drain(conn, "vision", object())
        finally:
            pipeline._STAGES = saved
        runs = conn.execute(
            "SELECT stage, processed, done FROM stage_runs WHERE stage='vision'"
        ).fetchall()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["done"], 1)


if __name__ == "__main__":
    unittest.main()
