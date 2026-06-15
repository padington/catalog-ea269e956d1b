"""Frame-description stage: sampled frame jpgs -> VLM `visual` blob.

The expensive, GPU half of the old monolithic `vision` stage (no Instagram;
only local ollama). It reads the manifest written by sample_frames, describes
each frame in display order with a local vision model (qwen2.5vl via ollama),
de-duplicates near-identical scenes, and joins them into one short blob.
categorize/tags then consume caption + transcript + visual together (augment,
never replace).

The proven description logic is copied from the validated prototype:
  * vlm_describe — POST a base64 frame to ollama /api/generate
  * FAILED-FRAME GUARD — a frame whose description raises is DROPPED; its error
    text NEVER enters the blob

One improvement over the prototype: near-duplicate scene dedup (normalize +
collapse exact-normalized duplicates, order preserved).

A "" visual (no usable frames) is a valid 'done', NOT a skip. The frames dir +
manifest must exist; if missing, process() raises (sample_frames not done yet)
so the item is marked 'failed' and retried once sample_frames catches up.

Env vars:
- REELS_VLM_MODEL: ollama vision model name (default "qwen2.5vl").
"""

import base64
import json
import os
import re

import llm
from storage import frames_dir

VISION_MODEL = os.environ.get("REELS_VLM_MODEL", "qwen2.5vl")

FRAME_PROMPT = (
    "Describe the scene in this video frame: the kind of place or setting "
    "(e.g. kitchen, gym, street, office, nature, studio), who is present, and "
    "what activity they are doing. Answer with one short phrase. Focus on the "
    "scene and action. Do NOT transcribe or mention any on-screen text."
)

MANIFEST = "manifest.json"


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
    list of {"desc"} dicts; keeps the FIRST occurrence of each normalized
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


def describe_frames_process(item, ctx):
    """Describe the sampled frames of a reel into the deduped visual blob.

    Reads frames_dir(pk)/manifest.json (written by sample_frames), VLM-describes
    each listed frame IN ORDER under a try/except that DROPS a frame whose
    description raises (the FAILED-FRAME GUARD — its error text never enters the
    blob), dedups, and joins -> blob. Returns the blob string ("" when there are
    no usable frames — a valid 'done', NOT a skip).

    Raises FileNotFoundError if the frames dir or manifest is missing
    (sample_frames not done yet) so the item is marked 'failed' and retried.
    `ctx` is unused.
    """
    pk = item["pk"]
    fdir = frames_dir(pk)
    manifest_path = os.path.join(fdir, MANIFEST)
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(
            f"no frames manifest for {pk}; sample_frames stage not done yet")
    with open(manifest_path) as f:
        manifest = json.load(f)

    descs = []
    for fn in manifest.get("frames", []):
        try:
            desc = vlm_describe(os.path.join(fdir, fn))
        except Exception:
            continue  # GUARD: skip a frame the VLM choked on
        descs.append(desc)

    scenes = [{"desc": d} for d in descs if d]
    deduped = dedup_scenes(scenes)
    blob = " ".join(s["desc"] for s in deduped)
    return blob


if __name__ == "__main__":
    # Smoke test the dedup logic with no ffmpeg/ollama.
    scenes = [
        {"desc": "Kitchen; baking bread"},
        {"desc": "kitchen, baking bread."},
        {"desc": "Gym, lifting weights"},
        {"desc": "Kitchen; baking bread"},
    ]
    assert [s["desc"] for s in dedup_scenes(scenes)] == [
        "Kitchen; baking bread", "Gym, lifting weights"]
    print("describe_frames.py self-test ok")
