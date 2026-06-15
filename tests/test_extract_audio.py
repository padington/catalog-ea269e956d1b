"""Tests for the ffmpeg `extract_audio` stage and the whisper `transcribe` stage.

Stdlib unittest, no real ffmpeg/whisper: ffmpeg.extract_wav (used by
extract_audio) and transcribe.transcribe_wav are monkeypatched, and
storage.MEDIA_DIR points at a tmp dir. Asserts a wav is produced for a present
mp4, a missing mp4 raises, extraction is idempotent, and transcribe consumes
the persisted wav (raising when it is absent).

Run:
    PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extract_audio
import ffmpeg
import pipeline
import storage
import transcribe


def _write_mp4(media_dir, pk):
    mp4 = os.path.join(media_dir, f"{pk}.mp4")
    with open(mp4, "wb") as f:
        f.write(b"\x00\x01\x02fakevideo")
    return mp4


def _write_wav(media_dir, pk):
    wav = os.path.join(media_dir, f"{pk}.wav")
    with open(wav, "wb") as f:
        f.write(b"RIFFfakewav")
    return wav


class ExtractAudioWiringTests(unittest.TestCase):
    def test_extract_audio_stage_registered(self):
        st = pipeline.stages()
        self.assertIn("extract_audio", st)
        s = st["extract_audio"]
        self.assertEqual(s.depends_on, ["download"])
        self.assertIsNone(s.output_col)
        self.assertFalse(s.ig_paced)


class ExtractAudioProcessTests(unittest.TestCase):
    def test_missing_mp4_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = storage.MEDIA_DIR
            try:
                storage.MEDIA_DIR = tmp
                with self.assertRaises(FileNotFoundError):
                    extract_audio.extract_audio_process(
                        {"pk": "nope-does-not-exist"}, None)
            finally:
                storage.MEDIA_DIR = saved

    def test_extracts_wav_for_present_mp4(self):
        calls = []

        def fake_extract(mp4_path, wav_path):
            calls.append((mp4_path, wav_path))
            with open(wav_path, "wb") as f:
                f.write(b"RIFFfakewav")

        with tempfile.TemporaryDirectory() as tmp:
            _write_mp4(tmp, "x")
            saved = storage.MEDIA_DIR
            orig = extract_audio.extract_wav
            try:
                storage.MEDIA_DIR = tmp
                extract_audio.extract_wav = fake_extract
                result = extract_audio.extract_audio_process({"pk": "x"}, None)
            finally:
                storage.MEDIA_DIR = saved
                extract_audio.extract_wav = orig

            wav = os.path.join(tmp, "x.wav")
            self.assertEqual(result["wav_path"], wav)
            self.assertTrue(os.path.exists(wav) and os.path.getsize(wav) > 0)
            self.assertEqual(len(calls), 1)

    def test_idempotent_noop_when_wav_exists(self):
        def boom_extract(*a, **k):
            raise AssertionError("extract_wav must not run when wav exists")

        with tempfile.TemporaryDirectory() as tmp:
            _write_mp4(tmp, "x")
            _write_wav(tmp, "x")
            saved = storage.MEDIA_DIR
            orig = extract_audio.extract_wav
            try:
                storage.MEDIA_DIR = tmp
                extract_audio.extract_wav = boom_extract
                result = extract_audio.extract_audio_process({"pk": "x"}, None)
            finally:
                storage.MEDIA_DIR = saved
                extract_audio.extract_wav = orig

            self.assertEqual(result["wav_path"], os.path.join(tmp, "x.wav"))


class TranscribeProcessTests(unittest.TestCase):
    def test_transcribe_consumes_persisted_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_wav(tmp, "x")
            saved = storage.MEDIA_DIR
            saved_t = transcribe.MEDIA_DIR
            orig = transcribe.transcribe_wav
            try:
                storage.MEDIA_DIR = tmp
                transcribe.MEDIA_DIR = tmp
                transcribe.transcribe_wav = lambda wav: "hello world"
                result = transcribe.transcribe_process({"pk": "x"}, None)
            finally:
                storage.MEDIA_DIR = saved
                transcribe.MEDIA_DIR = saved_t
                transcribe.transcribe_wav = orig

            self.assertEqual(result, "hello world")

    def test_missing_wav_raises(self):
        def boom_transcribe(*a, **k):
            raise AssertionError("transcribe_wav must not run without a wav")

        with tempfile.TemporaryDirectory() as tmp:
            saved = storage.MEDIA_DIR
            saved_t = transcribe.MEDIA_DIR
            orig = transcribe.transcribe_wav
            try:
                storage.MEDIA_DIR = tmp
                transcribe.MEDIA_DIR = tmp
                transcribe.transcribe_wav = boom_transcribe
                with self.assertRaises(FileNotFoundError):
                    transcribe.transcribe_process(
                        {"pk": "nope-does-not-exist"}, None)
            finally:
                storage.MEDIA_DIR = saved
                transcribe.MEDIA_DIR = saved_t
                transcribe.transcribe_wav = orig


if __name__ == "__main__":
    unittest.main()
