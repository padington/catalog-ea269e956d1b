"""Local VLM "vision" stage: scene descriptions of a reel's video.

A purely-local stage (no Instagram; only local ollama) that fills the reels
`visual` column. It samples frames from the already-downloaded mp4 by ffmpeg
SCENE-CHANGE detection (one frame per visual cut), describes each with a local
vision model (qwen2.5vl via ollama), de-duplicates near-identical consecutive
scenes, and joins them into one short blob. categorize/tags then consume
caption + transcript + visual together (augment, never replace).

The proven extraction/description logic is copied from the validated prototype
/tmp/vlm_qwen.py:
  * scene_frames        — ffmpeg scene-change frames (cap MAX_FRAMES, even-sample)
  * proportional_frames — 25/50/75% fallback when no scene cuts; failed grabs skip
  * vlm_describe        — POST a base64 frame to ollama /api/generate
  * describe_video      — FAILED-FRAME GUARD: a frame that fails extraction OR
                          description is DROPPED; its error text NEVER enters blob

One improvement over the prototype: near-duplicate scene dedup (normalize +
collapse exact-normalized consecutive/global duplicates, order preserved).

A "" visual (no usable frames) is a valid 'done', NOT a skip. The mp4 must
exist; if missing, process() raises (mirroring transcribe.transcribe_process)
so the item is marked 'failed' and retried once download catches up.

Env vars:
- REELS_VLM_MODEL: ollama vision model name (default "qwen2.5vl").
"""

import base64
import glob
import json
import os
import re
import subprocess
import tempfile
import urllib.request

import categorize as cz
import transcribe

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"

VISION_MODEL = os.environ.get("REELS_VLM_MODEL", "qwen2.5vl")

SCENE_THRESHOLD = 0.3   # ffmpeg scene score above which a cut is declared
MAX_FRAMES = 8          # cap described frames per reel

FRAME_PROMPT = (
    "Describe the scene in this video frame: the kind of place or setting "
    "(e.g. kitchen, gym, street, office, nature, studio), who is present, and "
    "what activity they are doing. Answer with one short phrase. Focus on the "
    "scene and action. Do NOT transcribe or mention any on-screen text."
)


def duration(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def scene_frames(mp4, outdir):
    """Extract one JPG per scene change. Returns [(timestamp, jpg_path), ...]
    or None if no scene cuts were detected (caller falls back to proportional).
    Any ffmpeg failure -> None (guarded; never raises into the blob)."""
    pattern = os.path.join(outdir, "s_%03d.jpg")
    try:
        p = subprocess.run(
            [FFMPEG, "-y", "-i", mp4, "-vf",
             f"select='gt(scene,{SCENE_THRESHOLD})',showinfo,scale=512:-1",
             "-vsync", "vfr", "-q:v", "3", pattern],
            capture_output=True, text=True)
    except Exception:
        return None
    times = re.findall(r"pts_time:([0-9.]+)", p.stderr)
    files = sorted(glob.glob(os.path.join(outdir, "s_*.jpg")))
    pairs = list(zip(times, files))
    if not pairs:
        return None
    if len(pairs) > MAX_FRAMES:
        step = (len(pairs) - 1) / (MAX_FRAMES - 1)
        idx = sorted({round(i * step) for i in range(MAX_FRAMES)})
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


def vlm_describe(jpg_path):
    with open(jpg_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = json.dumps({
        "model": VISION_MODEL, "stream": False, "prompt": FRAME_PROMPT,
        "images": [b64],
    }).encode()
    req = urllib.request.Request(
        cz.OLLAMA_HOST.rstrip("/") + "/api/generate",
        data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = json.loads(resp.read().decode())
    return (body.get("response") or "").strip()


_NORM = re.compile(r"[^a-z0-9 ]")


def _norm(desc):
    """Normalize a scene description for duplicate comparison: lowercase, drop
    punctuation, collapse whitespace."""
    return re.sub(r"\s+", " ", _NORM.sub("", (desc or "").lower())).strip()


def dedup_scenes(scenes):
    """Drop near-identical scenes (exact-normalized duplicates), order kept.

    e.g. "Kitchen; baking bread" repeated 4x collapses to one. Operates on the
    list of {"t", "desc"} dicts; keeps the FIRST occurrence of each normalized
    description.
    """
    out = []
    seen = set()
    for s in scenes:
        key = _norm(s.get("desc"))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def describe_video(mp4):
    """Return (visual_blob, scenes, sampling) where scenes = [{t, desc}, ...].

    Frames that fail extraction OR description are dropped silently (the
    FAILED-FRAME GUARD); their error text never enters the blob. Scenes are
    de-duplicated. A "" blob (no usable frames) is a valid result.
    """
    dur = duration(mp4) or 1.0
    tmp = tempfile.mkdtemp()
    try:
        frames = scene_frames(mp4, tmp)
        sampling = "scene"
        if not frames:
            frames = proportional_frames(mp4, dur, tmp)
            sampling = "proportional"
        scenes = []
        for t, jpg in frames:
            try:
                desc = vlm_describe(jpg)
            except Exception:
                continue  # GUARD: skip a frame the VLM choked on
            if desc:
                scenes.append({"t": round(t, 1), "desc": desc})
    finally:
        for f in os.listdir(tmp):
            os.remove(os.path.join(tmp, f))
        os.rmdir(tmp)
    scenes = dedup_scenes(scenes)
    blob = " ".join(s["desc"] for s in scenes)
    return blob, scenes, sampling


def process(item, ctx):
    """Local VLM stage (NO network beyond local ollama): describe the scenes of
    the already-downloaded mp4. Returns the deduped visual blob string (may be
    "" when there are no usable frames — a valid 'done', NOT a skip).

    Requires media/<pk>.mp4 on disk (produced by the download stage). Raises if
    the mp4 is missing so the item is marked 'failed' and retried once download
    catches up (mirrors transcribe.transcribe_process). `ctx` is unused.
    """
    pk = item["pk"]
    mp4_path = transcribe.media_path(pk)
    if not (os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0):
        raise FileNotFoundError(f"no media for {pk}; download stage not done yet")
    blob, _scenes, _sampling = describe_video(mp4_path)
    return blob


if __name__ == "__main__":
    # Smoke test the dedup logic with no ffmpeg/ollama.
    scenes = [
        {"t": 0.0, "desc": "Kitchen; baking bread"},
        {"t": 1.0, "desc": "kitchen, baking bread."},
        {"t": 2.0, "desc": "Gym, lifting weights"},
        {"t": 3.0, "desc": "Kitchen; baking bread"},
    ]
    assert [s["desc"] for s in dedup_scenes(scenes)] == [
        "Kitchen; baking bread", "Gym, lifting weights"]
    print("vision.py self-test ok")
