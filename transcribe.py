"""Local speech-to-text (ASR) for reels — pure whisper, no network.

The expensive, CPU-bound half of the old transcribe module: it runs
``whisper-cli`` against a local GGML Whisper model over the 16 kHz mono wav that
the extract_audio stage already wrote to disk. The plain text is stored in the
``transcript`` column. (The cheap ffmpeg mp4->wav extraction now lives in
extract_audio.py; the network-bound download half lives in download.py.)

Runs are resumable via the queue: an empty-string transcript is a valid
terminal so a reel is never retried forever. The legacy helper transcribe_wav
is kept intact for the standalone benchmark.

The wav must exist; if missing, process() raises (extract_audio not done yet)
so the item is marked 'failed' and retried once extract_audio catches up. The
wav is NOT deleted here — its lifecycle is the `clean` command.

Env vars:
- REELS_DB: sqlite path (default "reels.db").
- REELS_MEDIA_DIR: where downloaded mp4s are kept (see storage.py).
- REELS_WHISPER_MODEL: GGML model path (default: the Handy large-v3 model).
"""

import os
import shutil
import subprocess

import db as dbm
# MEDIA_DIR is re-exported here so db.backfill_queue (which reads
# transcribe.MEDIA_DIR) and tests that monkeypatch it keep working unchanged.
from storage import MEDIA_DIR, wav_path  # noqa: F401

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
    """Local whisper stage (NO network): transcribe the already-extracted wav.

    Requires the 16 kHz mono wav on disk (produced by the extract_audio stage).
    Returns the transcript text (possibly ""). Raises if the wav is missing so
    the item is marked 'failed' and retried once extract_audio catches up. The
    wav is NOT deleted here (the `clean` command owns its lifecycle).
    """
    pk = item["pk"]
    wav = wav_path(pk)
    if not (os.path.exists(wav) and os.path.getsize(wav) > 0):
        raise FileNotFoundError(
            f"no audio for {pk}; extract_audio stage not done yet")
    return transcribe_wav(wav)


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
