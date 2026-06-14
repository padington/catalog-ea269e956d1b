"""Local speech-to-text (ASR) for reels — pure whisper, no network.

The CPU-bound half of the old transcribe module (the network-bound download
half now lives in download.py). This stage extracts a 16 kHz mono wav with
ffmpeg and runs ``whisper-cli`` against a local GGML Whisper model. The plain
text is stored in the ``transcript`` column.

Runs are resumable via the queue: an empty-string transcript is a valid
terminal so a reel is never retried forever. The legacy helper transcribe_wav
is kept intact for the standalone benchmark.

Env vars:
- REELS_DB: sqlite path (default "reels.db").
- REELS_MEDIA_DIR: where downloaded mp4s are kept (see storage.py).
- REELS_WHISPER_MODEL: GGML model path (default: the Handy large-v3 model).
"""

import os
import shutil
import subprocess
import tempfile

import db as dbm
from ffmpeg import extract_wav
# MEDIA_DIR is re-exported here so db.backfill_queue (which reads
# transcribe.MEDIA_DIR) and tests that monkeypatch it keep working unchanged.
from storage import MEDIA_DIR, media_path  # noqa: F401

# Local GGML Whisper model shipped by Handy.app; overridable via env.
WHISPER_MODEL = os.environ.get(
    "REELS_WHISPER_MODEL",
    os.path.expanduser(
        "~/Library/Application Support/com.pais.handy/models/ggml-large-v3-q5_0.bin"
    ),
)

WHISPER_CLI = shutil.which("whisper-cli")


def transcribe_wav(wav_path):
    """Run whisper-cli on a wav and return the plain-text transcript."""
    if not WHISPER_CLI:
        raise RuntimeError("whisper-cli not found on PATH (brew install whisper-cpp)")
    # -nt drops timestamps; -otxt writes <wav>.txt; -of sets the output base.
    out_base = wav_path  # whisper writes out_base + ".txt"
    subprocess.run(
        [WHISPER_CLI, "-m", WHISPER_MODEL, "-f", wav_path,
         "-nt", "-otxt", "-of", out_base],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    txt_path = out_base + ".txt"
    try:
        with open(txt_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    finally:
        if os.path.exists(txt_path):
            os.remove(txt_path)


def transcribe_process(item, ctx):
    """Local whisper stage (NO network): transcribe the already-downloaded mp4.

    Requires media/<pk>.mp4 on disk (produced by the download stage). Returns
    the transcript text (possibly ""). Raises if the mp4 is missing so the item
    is marked 'failed' and retried once download catches up.
    """
    pk = item["pk"]
    mp4_path = media_path(pk)
    if not (os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0):
        raise FileNotFoundError(f"no media for {pk}; download stage not done yet")
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        extract_wav(mp4_path, wav_path)
        return transcribe_wav(wav_path)
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)


def run(db_path=None, delay=0.0, limit=None):
    # Thin shim so callers that still import transcribe.run keep working. The
    # canonical path is the generic driver; this just drains the transcribe
    # stage (local whisper, no network).
    import pipeline

    db_path = db_path or os.environ.get("REELS_DB", "reels.db")
    conn = dbm.connect(db_path)
    dbm.init_db(conn)
    ctx = pipeline.Context(conn)
    pipeline.drain(conn, "transcribe", ctx, limit=limit, delay=delay)


if __name__ == "__main__":
    run(os.environ.get("REELS_DB", "reels.db"))
