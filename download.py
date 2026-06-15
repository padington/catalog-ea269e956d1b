"""Media download stage (IG-paced) for reels.

The network-bound half of the old transcribe module: fetch the reel's media
info and download the video to a cache dir. This is the ONLY Instagram call in
the download/transcribe/sample_frames/describe_frames stages.

Runs are resumable via the queue: download failures are retried, and reels with
no video (gone/private) return NO_VIDEO so the driver maps them to 'skipped' and
never retries them. The legacy helpers (fetch_video, download_video) are kept
intact for the standalone benchmark.

Env vars:
- REELS_MEDIA_DIR: where downloaded mp4s are kept (see storage.py).
"""

import os
import shutil
import urllib.request

from storage import MEDIA_DIR, media_path


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
