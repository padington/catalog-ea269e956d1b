"""Media download + local speech-to-text (ASR) for reels.

This stage is split in two so the network-bound and CPU-bound halves run
independently under the queue driver (pipeline.py):

- ``download_process`` (IG-paced): fetch the reel's media info, download the
  video to a cache dir. The ONLY Instagram call in these two stages.
- ``transcribe_process`` (local, no network): extract a 16 kHz mono wav with
  ffmpeg and run ``whisper-cli`` against a local GGML Whisper model. The plain
  text is stored in the ``transcript`` column.

Runs are resumable via the queue: download failures are retried, reels with no
video (gone/private) are 'skipped', and an empty-string transcript is a valid
terminal so a reel is never retried forever. The legacy helpers (fetch_video,
download_video, extract_wav, transcribe_wav, transcribe_reel) are kept intact
for the standalone benchmark.

Env vars:
- REELS_DB: sqlite path (default "reels.db").
- REELS_MEDIA_DIR: where downloaded mp4s are kept (default "media/" here).
- REELS_WHISPER_MODEL: GGML model path (default: the Handy large-v3 model).
"""

import os
import shutil
import subprocess
import tempfile
import urllib.request

import db as dbm

_HERE = os.path.dirname(os.path.abspath(__file__))

MEDIA_DIR = os.environ.get("REELS_MEDIA_DIR", os.path.join(_HERE, "media"))

# Local GGML Whisper model shipped by Handy.app; overridable via env.
WHISPER_MODEL = os.environ.get(
    "REELS_WHISPER_MODEL",
    os.path.expanduser(
        "~/Library/Application Support/com.pais.handy/models/ggml-large-v3-q5_0.bin"
    ),
)

WHISPER_CLI = shutil.which("whisper-cli")
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


def fetch_video(cl, pk):
    """Return (url, duration_seconds) for a reel, or (None, None) if no video."""
    res = cl.private_request(f"media/{pk}/info/")
    items = res.get("items") or []
    if not items:
        return None, None
    item = items[0] or {}
    versions = item.get("video_versions") or []
    if not versions:
        return None, None
    url = (versions[0] or {}).get("url")
    duration = item.get("video_duration")
    return url, duration


def download_video(url, pk):
    """Download the reel mp4 to MEDIA_DIR/<pk>.mp4 (kept). Returns the path."""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    path = os.path.join(MEDIA_DIR, f"{pk}.mp4")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(path, "wb") as f:
            shutil.copyfileobj(resp, f)
    return path


def extract_wav(mp4_path, wav_path):
    # 16 kHz mono PCM wav is what whisper.cpp expects.
    subprocess.run(
        [FFMPEG, "-y", "-i", mp4_path, "-ar", "16000", "-ac", "1",
         "-c:a", "pcm_s16le", wav_path],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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


def transcribe_reel(cl, pk):
    """Full chain for one reel. Returns the transcript text ("" if no video).

    Retained for the standalone benchmark; the canonical pipeline path now
    splits this into download_process (IG) + transcribe_process (local).
    """
    url, _duration = fetch_video(cl, pk)
    if not url:
        return ""
    mp4_path = download_video(url, pk)
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        extract_wav(mp4_path, wav_path)
        return transcribe_wav(wav_path)
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)


def media_path(pk):
    """Path to the cached mp4 for a reel (does not check existence)."""
    return os.path.join(MEDIA_DIR, f"{pk}.mp4")


# Sentinel returned by download_process when the reel has no downloadable video
# (gone/private). The driver maps this to a 'skipped' terminal so it is never
# retried, yet downstream stages still proceed on caption-only.
NO_VIDEO = object()


def download_process(item, ctx):
    """IG-paced stage: fetch the reel's video URL and download the mp4.

    Returns {"media_path": path} on success, or NO_VIDEO when the reel has no
    video. Raises ThrottleError on an IG rate-limit/action-block; any other
    failure propagates as a normal Exception (marked 'failed' + retried).
    This is the ONLY Instagram call across the download/transcribe stages.
    """
    from pipeline import ThrottleError, is_throttle

    pk = item["pk"]
    path = media_path(pk)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return {"media_path": path}  # already downloaded; idempotent
    try:
        url, _duration = fetch_video(ctx.client, pk)
        if not url:
            return NO_VIDEO
        return {"media_path": download_video(url, pk)}
    except Exception as exc:
        if is_throttle(exc):
            raise ThrottleError(str(exc)) from exc
        raise


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
