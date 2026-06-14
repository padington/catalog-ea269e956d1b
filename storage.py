"""Media file locations shared across stages.

`MEDIA_DIR` is where downloaded reel mp4s are cached; `media_path(pk)` maps a
reel pk to its on-disk mp4 path. Moved here verbatim from transcribe.py so the
download/transcribe/vision stages can share one home for media locations.

Env vars:
- REELS_MEDIA_DIR: where downloaded mp4s are kept (default "media/" here).
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))

MEDIA_DIR = os.environ.get("REELS_MEDIA_DIR", os.path.join(_HERE, "media"))


def media_path(pk):
    """Path to the cached mp4 for a reel (does not check existence)."""
    return os.path.join(MEDIA_DIR, f"{pk}.mp4")
