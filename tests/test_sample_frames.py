"""Tests for the ffmpeg `sample_frames` stage.

Stdlib unittest, no real ffmpeg: scene_frames/proportional_frames/duration are
monkeypatched (each writes its named jpgs into outdir like the real helpers),
and storage.MEDIA_DIR points at a tmp dir. Asserts a manifest with ordered
frames is written and that a missing mp4 raises.

Run:
    PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline
import sample_frames
import storage


def _write_mp4(media_dir, pk):
    mp4 = os.path.join(media_dir, f"{pk}.mp4")
    with open(mp4, "wb") as f:
        f.write(b"\x00\x01\x02fakevideo")
    return mp4


class SampleFramesWiringTests(unittest.TestCase):
    def test_sample_frames_stage_registered(self):
        st = pipeline.stages()
        self.assertIn("sample_frames", st)
        s = st["sample_frames"]
        self.assertEqual(s.depends_on, ["download"])
        self.assertIsNone(s.output_col)
        self.assertFalse(s.ig_paced)


class SampleFramesProcessTests(unittest.TestCase):
    def test_missing_mp4_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = storage.MEDIA_DIR
            try:
                storage.MEDIA_DIR = tmp
                with self.assertRaises(FileNotFoundError):
                    sample_frames.sample_frames_process(
                        {"pk": "nope-does-not-exist"}, None)
            finally:
                storage.MEDIA_DIR = saved

    def test_scene_frames_writes_ordered_manifest(self):
        def fake_scene(mp4, outdir, **kw):
            names = ["s_001.jpg", "s_002.jpg", "s_003.jpg"]
            out = []
            for i, n in enumerate(names):
                p = os.path.join(outdir, n)
                with open(p, "wb") as f:
                    f.write(b"\xff\xd8\xff")
                out.append((float(i), p))
            return out

        with tempfile.TemporaryDirectory() as tmp:
            _write_mp4(tmp, "x")
            saved = storage.MEDIA_DIR
            orig_scene = sample_frames.scene_frames
            orig_prop = sample_frames.proportional_frames
            try:
                storage.MEDIA_DIR = tmp
                sample_frames.scene_frames = fake_scene
                sample_frames.proportional_frames = lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("proportional fallback must not run"))
                result = sample_frames.sample_frames_process({"pk": "x"}, None)
                fdir = result["frames_dir"]
            finally:
                storage.MEDIA_DIR = saved
                sample_frames.scene_frames = orig_scene
                sample_frames.proportional_frames = orig_prop

            self.assertEqual(result["count"], 3)
            self.assertEqual(result["sampling"], "scene")
            with open(os.path.join(fdir, "manifest.json")) as f:
                manifest = json.load(f)
            self.assertEqual(manifest["sampling"], "scene")
            self.assertEqual(manifest["frames"],
                             ["0000.jpg", "0001.jpg", "0002.jpg"])
            # Ordered jpgs exist; the original ffmpeg-named files are gone.
            for fn in manifest["frames"]:
                self.assertTrue(os.path.exists(os.path.join(fdir, fn)))
            leftovers = [fn for fn in os.listdir(fdir)
                         if fn not in manifest["frames"] and fn != "manifest.json"]
            self.assertEqual(leftovers, [])

    def test_proportional_fallback_when_no_scenes(self):
        def fake_prop(mp4, dur, outdir):
            out = []
            for i, frac in enumerate(("p25", "p50")):
                p = os.path.join(outdir, f"{frac}.jpg")
                with open(p, "wb") as f:
                    f.write(b"\xff\xd8\xff")
                out.append((float(i), p))
            return out

        with tempfile.TemporaryDirectory() as tmp:
            _write_mp4(tmp, "x")
            saved = storage.MEDIA_DIR
            orig_scene = sample_frames.scene_frames
            orig_prop = sample_frames.proportional_frames
            orig_dur = sample_frames.duration
            try:
                storage.MEDIA_DIR = tmp
                sample_frames.scene_frames = lambda *a, **k: None
                sample_frames.proportional_frames = fake_prop
                sample_frames.duration = lambda mp4: 12.0
                result = sample_frames.sample_frames_process({"pk": "x"}, None)
                fdir = result["frames_dir"]
            finally:
                storage.MEDIA_DIR = saved
                sample_frames.scene_frames = orig_scene
                sample_frames.proportional_frames = orig_prop
                sample_frames.duration = orig_dur

            self.assertEqual(result["sampling"], "proportional")
            with open(os.path.join(fdir, "manifest.json")) as f:
                manifest = json.load(f)
            self.assertEqual(manifest["sampling"], "proportional")
            self.assertEqual(manifest["frames"], ["0000.jpg", "0001.jpg"])

    def test_no_frames_writes_empty_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_mp4(tmp, "x")
            saved = storage.MEDIA_DIR
            orig_scene = sample_frames.scene_frames
            orig_prop = sample_frames.proportional_frames
            orig_dur = sample_frames.duration
            try:
                storage.MEDIA_DIR = tmp
                sample_frames.scene_frames = lambda *a, **k: None
                sample_frames.proportional_frames = lambda *a, **k: []
                sample_frames.duration = lambda mp4: 12.0
                result = sample_frames.sample_frames_process({"pk": "x"}, None)
                fdir = result["frames_dir"]
            finally:
                storage.MEDIA_DIR = saved
                sample_frames.scene_frames = orig_scene
                sample_frames.proportional_frames = orig_prop
                sample_frames.duration = orig_dur

            self.assertEqual(result["count"], 0)
            with open(os.path.join(fdir, "manifest.json")) as f:
                manifest = json.load(f)
            self.assertEqual(manifest["frames"], [])

    def test_stale_frames_dir_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_mp4(tmp, "x")
            saved = storage.MEDIA_DIR
            orig_scene = sample_frames.scene_frames
            orig_prop = sample_frames.proportional_frames
            try:
                storage.MEDIA_DIR = tmp
                # lay down a stale frames dir with junk
                fd = storage.frames_dir("x")
                os.makedirs(fd, exist_ok=True)
                with open(os.path.join(fd, "stale.jpg"), "wb") as f:
                    f.write(b"old")

                def fake_scene(mp4, outdir, **kw):
                    p = os.path.join(outdir, "s_001.jpg")
                    with open(p, "wb") as f:
                        f.write(b"\xff\xd8\xff")
                    return [(0.0, p)]

                sample_frames.scene_frames = fake_scene
                sample_frames.proportional_frames = lambda *a, **k: []
                result = sample_frames.sample_frames_process({"pk": "x"}, None)
                fd = result["frames_dir"]
            finally:
                storage.MEDIA_DIR = saved
                sample_frames.scene_frames = orig_scene
                sample_frames.proportional_frames = orig_prop

            self.assertFalse(os.path.exists(os.path.join(fd, "stale.jpg")))
            self.assertTrue(os.path.exists(os.path.join(fd, "0000.jpg")))


if __name__ == "__main__":
    unittest.main()
