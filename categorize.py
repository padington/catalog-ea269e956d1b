"""Caption-based categorizer.

The public interface is `categorize_caption(caption) -> list[str]`.

The default backend is a LOCAL LLM served by ollama (http://localhost:11434),
called over its HTTP API with only the stdlib (no third-party SDK). If ollama
is unreachable or errors, it falls back to a dependency-free keyword stub so
the pipeline keeps working offline.

Env vars:
- REELS_CATEGORIZER: "ollama" (default) or "stub" to force the keyword backend.
- OLLAMA_HOST: ollama base URL (default "http://localhost:11434").
- OLLAMA_MODEL: model name (default "llama3.1").
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
REELS_CATEGORIZER = os.environ.get("REELS_CATEGORIZER", "ollama")

# Starter taxonomy: category -> keywords that map to it.
TAXONOMY = {
    "architecture": ["architecture", "building", "facade", "interior", "design house"],
    "cooking": ["recipe", "cook", "cooking", "bake", "kitchen", "dish", "meal", "food"],
    "fitness": ["workout", "dumbbell", "gym", "exercise", "fitness", "training", "reps"],
    "travel": ["travel", "trip", "destination", "hotel", "flight", "wanderlust", "beach"],
    "fashion": ["fashion", "outfit", "style", "ootd", "wardrobe", "dress"],
    "tech": ["tech", "gadget", "software", "coding", "ai", "startup", "app"],
    "art": ["art", "painting", "drawing", "sketch", "illustration", "sculpture"],
    "finance": ["finance", "investing", "stocks", "money", "budget", "crypto"],
}


# The closed set of categories the model is allowed to emit. Anything outside
# this set (invented categories, echoed usernames, languages) is dropped by the
# post-filter below, so junk categories can never reach the DB.
ALLOWED = set(TAXONOMY.keys()) | {
    "music", "comedy", "education", "pets", "sports", "beauty", "diy",
    "gaming", "nature", "cars", "health", "relationships", "dance",
    "science", "photography", "gardening", "motivation", "other",
}


def _stub_backend(caption):
    text = (caption or "").lower()
    hits = [cat for cat, words in TAXONOMY.items() if any(w in text for w in words)]
    return hits or ["other"]


def _ollama_backend(caption):
    categories = ", ".join(sorted(ALLOWED))
    system = (
        "You classify short Instagram reel captions into topic categories. "
        f"Choose 1-2 categories STRICTLY from this allowed list: {categories}. "
        "Respond with ONLY a JSON array of lowercase category strings from that "
        'list, nothing else, e.g. ["cooking"]. '
        'If nothing fits, return ["other"]. '
        "Do NOT invent new categories. "
        "Do NOT use the creator's username as a category."
    )
    user = "Classify this caption:\n" + (caption or "")
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
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    text = body.get("message", {}).get("content", "")

    # The model may wrap the array in prose/markdown; grab the first [...] block.
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if not match:
        return ["other"]
    try:
        items = json.loads(match.group(0))
    except (ValueError, TypeError):
        return ["other"]
    cats = [str(x).strip().lower() for x in items if str(x).strip()]
    # Post-filter: keep only categories from the allowed set so the model can
    # never inject junk (invented slugs, usernames, languages, etc.).
    cats = [c for c in cats if c in ALLOWED]
    return cats or ["other"]


def _ollama_chat(system, user):
    """One chat turn against ollama; returns the raw message content string."""
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
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body.get("message", {}).get("content", "")


_DESCRIBE_SYSTEM = (
    "You are given the caption and/or spoken transcript of an Instagram reel. "
    "In 1-2 factual sentences, describe what the reel is actually about: its "
    "subject, the activity shown or discussed, and the main message or point. "
    "Be concrete and neutral. Output only the description, no preamble."
)


def _describe_reel(text):
    """Pass 1: a short meta-description of what the reel is about."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        return _ollama_chat(_DESCRIBE_SYSTEM, "Reel caption/transcript:\n" + text).strip()
    except Exception:
        return ""


def _classify_from_description(description):
    """Pass 2: pick 1-2 allowed categories for a reel description."""
    categories = ", ".join(sorted(ALLOWED))
    system = (
        "You assign topic categories to an Instagram reel given a short "
        "description of it. Choose the 1-2 categories that best fit, STRICTLY "
        f"from this allowed list: {categories}. Respond with ONLY a JSON array "
        "of lowercase category strings from that list and nothing else. If "
        'nothing fits, return ["other"]. Do NOT invent new categories and do '
        "NOT use a person's name as a category."
    )
    text = _ollama_chat(system, "Description:\n" + description)
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if not match:
        return ["other"]
    try:
        items = json.loads(match.group(0))
    except (ValueError, TypeError):
        return ["other"]
    cats = [str(x).strip().lower() for x in items if str(x).strip()]
    cats = [c for c in cats if c in ALLOWED]
    return cats or ["other"]


def categorize_caption(caption):
    # Two-pass: describe the reel first, then classify that description. The
    # description step gives the classifier clean subject matter to work from
    # instead of a junk/emoji caption, and keeps the closed-set post-filter.
    if REELS_CATEGORIZER == "stub":
        return _stub_backend(caption)
    try:
        desc = _describe_reel(caption) or (caption or "")
        if not desc.strip():
            return ["other"]
        return _classify_from_description(desc)
    except Exception:
        return _stub_backend(caption)


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


def _signal_text(item):
    # Feed all three signals to the LLM: the caption, the spoken transcript,
    # and the VLM scene description (visual). Each is NULL-safe, so a missing
    # column simply contributes an empty line.
    return (
        (item.get("caption") or "")
        + "\n" + (item.get("transcript") or "")
        + "\n" + (item.get("visual") or "")
    )


def process(item, ctx):
    """Local LLM stage: categories from caption + transcript combined.

    Thin wrapper over the hand-tuned categorize_caption (prompts unchanged).
    The driver's `write` persists via db.set_categories.
    """
    return categorize_caption(_signal_text(item))


def tags_process(item, ctx):
    """Local LLM stage: free-form tags from caption + transcript combined.

    Thin wrapper over the hand-tuned generate_tags (prompts unchanged). The
    driver's `write` persists via db.set_tags.
    """
    return generate_tags(_signal_text(item))


def run(db_path="reels.db"):
    import time
    from datetime import datetime
    import db as dbm

    def _log(msg):
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

    conn = dbm.connect(db_path)
    dbm.init_db(conn)
    reels = list(dbm.iter_uncategorized(conn))
    total = len(reels)
    _log(f"categorize: {total} reel(s) to classify (backend={REELS_CATEGORIZER})")
    n = 0
    start = time.time()
    for reel in reels:
        cats = categorize_caption(reel.get("caption"))
        dbm.set_categories(conn, reel["pk"], cats)
        n += 1
        if n % 50 == 0 or n == total:
            rate = n / max(time.time() - start, 1e-9)
            eta = (total - n) / rate if rate else 0
            _log(f"categorize: {n}/{total} | {rate*60:.0f}/min | eta {eta/60:.0f}m")
    _log(f"categorize: done, {n} reel(s) classified")


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
    if "--selftest" in sys.argv:
        # Force the offline keyword backend so the self-test never needs ollama.
        assert _stub_backend("Great dumbbell workout for arms") == ["fitness"]
        assert _stub_backend("Easy pasta recipe") == ["cooking"]
        assert _stub_backend("") == ["other"]
        print("categorize.py self-test ok")
    elif "--check-ollama" in sys.argv:
        sample = "Easy 15-minute pasta recipe for a quick weeknight dinner"
        print(f"host={OLLAMA_HOST} model={OLLAMA_MODEL}")
        print("result:", _ollama_backend(sample))
    else:
        run(os.environ.get("REELS_DB", "reels.db"))
