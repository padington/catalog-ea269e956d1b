"""Audio-extraction stage: mp4 -> a 16 kHz mono wav on disk.

The cheap, ffmpeg-only half of the old monolithic `transcribe` stage. It pulls
the audio track out of the already-downloaded mp4 with ffmpeg into a 16 kHz mono
PCM wav (exactly what whisper.cpp expects). The wav persists on disk at
`storage.wav_path(pk)` so the expensive whisper half (transcribe) can retry /
observe / parallelize independently of this cheap extraction.

The mp4 must exist; if missing, process() raises (mirroring sample_frames and
the old transcribe stage) so the item is marked 'failed' and retried once
download catches up. Extraction is idempotent: a present non-empty wav is reused
as-is rather than re-extracted. The queue 'done' status is the only marker
(output_col=None); the write callback is a no-op.

The wav is NOT deleted here — its lifecycle (cleanup of intermediates) is the
`clean` command, not this stage.
"""

import os

from ffmpeg import extract_wav
from storage import media_path, wav_path


def extract_audio_process(item, ctx):
    """Extract the audio of the already-downloaded mp4 into wav_path(pk).

    Requires media/<pk>.mp4 on disk (produced by the download stage). Raises if
    the mp4 is missing so the item is marked 'failed' and retried once download
    catches up. `ctx` is unused.

    Idempotent: if a non-empty wav already exists it is returned as-is (no
    re-extract). Returns a small dict {"wav_path"}.
    """
    pk = item["pk"]
    mp4_path = media_path(pk)
    if not (os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0):
        raise FileNotFoundError(f"no media for {pk}; download stage not done yet")

    wav = wav_path(pk)
    if os.path.exists(wav) and os.path.getsize(wav) > 0:
        return {"wav_path": wav}

    extract_wav(mp4_path, wav)
    return {"wav_path": wav}
