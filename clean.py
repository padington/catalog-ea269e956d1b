"""Reclaim disk by deleting intermediate artifacts whose consuming stage is done.

The wav written by extract_audio is safe to delete once transcribe is terminal
(done/skipped); the frames dir written by sample_frames is safe once
describe_frames is terminal. The mp4 is kept (source of truth + download
idempotency). Pure local disk + queue reads; no network.
"""

import os
import shutil

from storage import frames_dir, wav_path


def _terminal_pks(conn, stage):
    """pks whose `stage` is in a terminal state (done/skipped) in the queue."""
    rows = conn.execute(
        "SELECT pk FROM queue WHERE stage = ? AND status IN ('done', 'skipped')",
        (stage,),
    ).fetchall()
    return [r["pk"] for r in rows]


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def clean_intermediates(conn):
    """Delete wavs (transcribe terminal) + frame dirs (describe_frames terminal).

    The mp4 is never touched. Returns a summary dict with keys
    {"wavs_removed", "frames_removed", "bytes_freed"}.
    """
    wavs = frames = freed = 0
    for pk in _terminal_pks(conn, "transcribe"):
        w = wav_path(pk)
        if os.path.exists(w):
            freed += os.path.getsize(w)
            os.remove(w)
            wavs += 1
    for pk in _terminal_pks(conn, "describe_frames"):
        d = frames_dir(pk)
        if os.path.isdir(d):
            freed += _dir_size(d)
            shutil.rmtree(d)
            frames += 1
    return {"wavs_removed": wavs, "frames_removed": frames, "bytes_freed": freed}
