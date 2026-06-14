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
import os
import re
import tempfile

import llm
from ffmpeg import duration, proportional_frames, scene_frames
from storage import media_path

VISION_MODEL = os.environ.get("REELS_VLM_MODEL", "qwen2.5vl")

SCENE_THRESHOLD = 0.3   # ffmpeg scene score above which a cut is declared
MAX_FRAMES = 8          # cap described frames per reel

FRAME_PROMPT = (
    "Describe the scene in this video frame: the kind of place or setting "
    "(e.g. kitchen, gym, street, office, nature, studio), who is present, and "
    "what activity they are doing. Answer with one short phrase. Focus on the "
    "scene and action. Do NOT transcribe or mention any on-screen text."
)


def vlm_describe(jpg_path):
    with open(jpg_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return llm.generate(FRAME_PROMPT, images=[b64], model=VISION_MODEL)


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
        frames = scene_frames(mp4, tmp, max_frames=MAX_FRAMES,
                              threshold=SCENE_THRESHOLD)
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
    mp4_path = media_path(pk)
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
