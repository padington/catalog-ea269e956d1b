"""clean.clean_intermediates tests. Stdlib unittest, in-memory sqlite, tmp dir.

storage.wav_path / storage.frames_dir read storage.MEDIA_DIR live (at call
time), so monkeypatching storage.MEDIA_DIR redirects the artifact paths even
though clean.py imported wav_path/frames_dir by name (they still call through to
storage's module global). The mp4 is never deleted.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clean
import db as dbm
import storage


def _make_conn():
    conn = dbm.connect(":memory:")
    dbm.init_db(conn)
    return conn


def _seed_queue(conn, pk, stage, status):
    dbm.upsert_reel(conn, {"pk": pk, "shortcode": pk})
    dbm.enqueue(conn, pk, stage)
    dbm.mark(conn, pk, stage, status)


class CleanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved_media = storage.MEDIA_DIR
        storage.MEDIA_DIR = self.tmp

    def tearDown(self):
        storage.MEDIA_DIR = self._saved_media

    def _write_wav(self, pk, data=b"RIFFfakewav"):
        path = storage.wav_path(pk)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _write_frames(self, pk, data=b"\xff\xd8jpegbytes"):
        d = storage.frames_dir(pk)
        os.makedirs(d)
        with open(os.path.join(d, "frame_000.jpg"), "wb") as f:
            f.write(data)
        return d

    def _write_mp4(self, pk, data=b"\x00mp4"):
        path = storage.media_path(pk)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_removes_artifacts_when_stages_terminal(self):
        conn = _make_conn()
        wav = self._write_wav("1", b"0123456789")          # 10 bytes
        frames = self._write_frames("1", b"abcdef")        # 6 bytes
        mp4 = self._write_mp4("1")
        _seed_queue(conn, "1", "transcribe", "done")
        _seed_queue(conn, "1", "describe_frames", "skipped")

        summary = clean.clean_intermediates(conn)

        self.assertFalse(os.path.exists(wav))
        self.assertFalse(os.path.isdir(frames))
        self.assertTrue(os.path.exists(mp4))  # mp4 is kept
        self.assertEqual(summary["wavs_removed"], 1)
        self.assertEqual(summary["frames_removed"], 1)
        self.assertEqual(summary["bytes_freed"], 16)

    def test_keeps_artifacts_when_stages_not_terminal(self):
        conn = _make_conn()
        wav = self._write_wav("2")
        frames = self._write_frames("2")
        # stages exist but are NOT terminal -> artifacts must survive.
        _seed_queue(conn, "2", "transcribe", "pending")
        _seed_queue(conn, "2", "describe_frames", "running")

        summary = clean.clean_intermediates(conn)

        self.assertTrue(os.path.exists(wav))
        self.assertTrue(os.path.isdir(frames))
        self.assertEqual(summary["wavs_removed"], 0)
        self.assertEqual(summary["frames_removed"], 0)
        self.assertEqual(summary["bytes_freed"], 0)


if __name__ == "__main__":
    unittest.main()
