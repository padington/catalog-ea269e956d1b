"""Shared ffmpeg/ffprobe helpers for the media stages.

Holds the frame-extraction and audio-extraction primitives that several stages
share:
  * duration            — ffprobe a clip's length in seconds
  * scene_frames        — ffmpeg scene-change frames (cap max_frames, even-sample)
  * proportional_frames — 25/50/75% fallback when no scene cuts; failed grabs skip
  * extract_wav         — 16 kHz mono PCM wav for whisper.cpp

scene_frames/proportional_frames were moved out of the old vision stage;
extract_wav from transcribe.py. scene_frames takes max_frames/threshold as
parameters (defaults 8 and 0.3) so sample_frames can pass its own module-level
MAX_FRAMES/SCENE_THRESHOLD and behavior stays identical.
"""

import glob
import os
import re
import subprocess

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"


def duration(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def scene_frames(mp4, outdir, max_frames=8, threshold=0.3):
    """Extract one JPG per scene change. Returns [(timestamp, jpg_path), ...]
    or None if no scene cuts were detected (caller falls back to proportional).
    Any ffmpeg failure -> None (guarded; never raises into the blob)."""
    pattern = os.path.join(outdir, "s_%03d.jpg")
    try:
        p = subprocess.run(
            [FFMPEG, "-y", "-i", mp4, "-vf",
             f"select='gt(scene,{threshold})',showinfo,scale=512:-1",
             "-vsync", "vfr", "-q:v", "3", pattern],
            capture_output=True, text=True)
    except Exception:
        return None
    times = re.findall(r"pts_time:([0-9.]+)", p.stderr)
    files = sorted(glob.glob(os.path.join(outdir, "s_*.jpg")))
    pairs = list(zip(times, files))
    if not pairs:
        return None
    if len(pairs) > max_frames:
        step = (len(pairs) - 1) / (max_frames - 1)
        idx = sorted({round(i * step) for i in range(max_frames)})
        pairs = [pairs[i] for i in idx]
    return [(float(t), f) for t, f in pairs]


def proportional_frames(mp4, dur, outdir):
    """Fallback: 25/50/75% snapshots. Failed grabs are skipped, not recorded."""
    out = []
    for frac in (0.25, 0.5, 0.75):
        t = dur * frac
        jpg = os.path.join(outdir, f"p{int(frac*100)}.jpg")
        try:
            subprocess.run(
                [FFMPEG, "-y", "-ss", f"{t:.2f}", "-i", mp4, "-frames:v", "1",
                 "-q:v", "3", "-vf", "scale=512:-1", jpg],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            out.append((t, jpg))
        except Exception:
            continue  # GUARD: drop the failed frame, no error text
    return out


def extract_wav(mp4_path, wav_path):
    # 16 kHz mono PCM wav is what whisper.cpp expects.
    subprocess.run(
        [FFMPEG, "-y", "-i", mp4_path, "-ar", "16000", "-ac", "1",
         "-c:a", "pcm_s16le", wav_path],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
