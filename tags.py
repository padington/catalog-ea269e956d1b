"""Free-form fine-grained tag stage (local LLM via ollama).

Unlike categorize.py (a closed 26-category set), these tags are open vocabulary
— specific things like `kettlebell`, `posture`, `sourdough`. Moved out of
categorize.py so each stage gets its own file; the shared ollama plumbing lives
in llm.py.

Env vars:
- OLLAMA_HOST / OLLAMA_MODEL: see llm.py.
"""

import json
import os
import re
import urllib.request

from llm import OLLAMA_HOST, OLLAMA_MODEL, signal_text

_TAG_CLEAN = re.compile(r"[^a-z0-9 -]")


def _normalize_tags(raw):
    out = []
    seen = set()
    for t in raw:
        t = _TAG_CLEAN.sub("", str(t).strip().lower())
        t = re.sub(r"\s+", "-", t).strip("-")
        if len(t) < 2 or len(t) > 30 or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= 10:
            break
    return out


def generate_tags(caption):
    """Free-form fine-grained tags (5-10) describing one reel, via ollama.

    Unlike `categorize_caption` (a closed 26-category set), these are open
    vocabulary — specific things like `kettlebell`, `posture`, `sourdough`.
    Returns [] when there is nothing to work with or the model is unreachable.
    """
    caption = (caption or "").strip()
    if not caption:
        return []
    system = (
        "You extract topic tags from the caption and spoken transcript of an "
        "Instagram reel. Return 5 to 10 short, lowercase tags that together "
        "capture what the reel is about: BOTH concrete things (objects, "
        "ingredients, equipment, places, techniques) AND abstract topics or "
        "themes (concepts, emotions, fields of knowledge, the message). Prefer "
        "specific over generic. Each tag is 1-3 words; join multi-word tags "
        "with hyphens. Respond with ONLY a JSON array of strings, no prose.\n"
        "The pairs below show the FORMAT on UNRELATED reels — never reuse these "
        "tags unless they genuinely describe the reel you are given:\n"
        '  morning skincare routine -> ["skincare","retinol","moisturizer","beauty-routine","sunscreen"]\n'
        '  weekend trip to lisbon   -> ["travel","lisbon","city-guide","portugal","itinerary"]\n'
        '  python async tutorial    -> ["python","async","coding","programming","tutorial"]\n'
        '  index-fund investing tip  -> ["investing","index-funds","personal-finance","compounding","portfolio"]\n'
        '  raised-bed gardening      -> ["gardening","raised-beds","composting","vegetables","soil"]\n'
        '  stand-up comedy clip      -> ["comedy","stand-up","crowd-work","joke","timing"]'
    )
    user = "Caption/transcript:\n" + caption
    payload = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_HOST.rstrip("/") + "/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = body.get("message", {}).get("content", "")
        match = re.search(r"\[.*?\]", text, re.DOTALL)
        if not match:
            return []
        items = json.loads(match.group(0))
        return _normalize_tags(items)
    except Exception:
        return []


def tags_process(item, ctx):
    """Local LLM stage: free-form tags from caption + transcript combined.

    Thin wrapper over the hand-tuned generate_tags (prompts unchanged). The
    driver's `write` persists via db.set_tags.
    """
    return generate_tags(signal_text(item))


def run_tags(db_path="reels.db"):
    import time
    from datetime import datetime
    import db as dbm

    def _log(msg):
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

    conn = dbm.connect(db_path)
    dbm.init_db(conn)
    reels = list(dbm.iter_untagged_with_caption(conn))
    total = len(reels)
    _log(f"tags: {total} captioned reel(s) to tag")
    n = 0
    start = time.time()
    for reel in reels:
        tags = generate_tags(reel.get("caption"))
        dbm.set_tags(conn, reel["pk"], tags)
        n += 1
        if n % 50 == 0 or n == total:
            rate = n / max(time.time() - start, 1e-9)
            eta = (total - n) / rate if rate else 0
            _log(f"tags: {n}/{total} | {rate*60:.0f}/min | eta {eta/60:.0f}m")
    _log(f"tags: done, {n} reel(s) tagged")


if __name__ == "__main__":
    run_tags(os.environ.get("REELS_DB", "reels.db"))
