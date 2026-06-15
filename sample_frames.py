"""Frame-sampling stage: mp4 -> ordered frame jpgs on disk + a tiny manifest.

The cheap, ffmpeg-only half of the old monolithic `vision` stage. It samples
frames from the already-downloaded mp4 by ffmpeg SCENE-CHANGE detection (one
frame per visual cut), falling back to 25/50/75% proportional snapshots when no
scene cuts are found. The frames persist on disk under `storage.frames_dir(pk)`
so the expensive VLM description half (describe_frames) can retry / observe /
parallelize independently.

The persisted intermediate is a per-reel frames directory plus a manifest.json:
  {"sampling": "scene"|"proportional", "frames": ["0000.jpg", "0001.jpg", ...]}
filenames relative to the frames dir, in display order. An empty "frames": [] is
valid (no usable frames) and is still a 'done', NOT a skip.

The mp4 must exist; if missing, process() raises (mirroring the old vision stage
and transcribe.transcribe_process) so the item is marked 'failed' and retried
once download catches up. The queue 'done' status is the only marker
(output_col=None); the write callback is a no-op.
"""

import json
import os
import shutil

from ffmpeg import duration, proportional_frames, scene_frames
from storage import frames_dir, media_path

SCENE_THRESHOLD = 0.3   # ffmpeg scene score above which a cut is declared
MAX_FRAMES = 8          # cap sampled frames per reel

MANIFEST = "manifest.json"


def sample_frames_process(item, ctx):
    """Sample frames from the already-downloaded mp4 into a fresh frames_dir(pk),
    normalize them to ordered 0000.jpg, 0001.jpg, ... and write manifest.json.

    Requires media/<pk>.mp4 on disk (produced by the download stage). Raises if
    the mp4 is missing so the item is marked 'failed' and retried once download
    catches up. `ctx` is unused.

    Returns a small dict {"frames_dir", "count", "sampling"}. 0 frames is a
    valid 'done' (a manifest with an empty frame list is written), NOT a skip.
    """
    pk = item["pk"]
    mp4_path = media_path(pk)
    if not (os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0):
        raise FileNotFoundError(f"no media for {pk}; download stage not done yet")

    fdir = frames_dir(pk)
    # Fresh directory: drop any stale frames/manifest from a prior run.
    shutil.rmtree(fdir, ignore_errors=True)
    os.makedirs(fdir, exist_ok=True)

    pairs = scene_frames(mp4_path, fdir, max_frames=MAX_FRAMES,
                         threshold=SCENE_THRESHOLD)
    sampling = "scene"
    if not pairs:
        pairs = proportional_frames(mp4_path, duration(mp4_path) or 1.0, fdir)
        sampling = "proportional"

    # Normalize the ffmpeg-named jpgs to ordered 0000.jpg, 0001.jpg, ... in the
    # order returned (display order), then remove any other files in the dir.
    ordered = []
    keep = set()
    for i, (_t, jpg) in enumerate(pairs or []):
        dst_name = f"{i:04d}.jpg"
        dst = os.path.join(fdir, dst_name)
        src = jpg if os.path.isabs(jpg) else os.path.join(fdir, jpg)
        if os.path.abspath(src) != os.path.abspath(dst):
            os.replace(src, dst)
        ordered.append(dst_name)
        keep.add(dst_name)
    keep.add(MANIFEST)
    for fn in os.listdir(fdir):
        if fn not in keep:
            os.remove(os.path.join(fdir, fn))

    with open(os.path.join(fdir, MANIFEST), "w") as f:
        json.dump({"sampling": sampling, "frames": ordered}, f)

    return {"frames_dir": fdir, "count": len(ordered), "sampling": sampling}
