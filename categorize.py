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


def categorize_caption(caption):
    if REELS_CATEGORIZER == "stub":
        return _stub_backend(caption)
    try:
        return _ollama_backend(caption)
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
        "You extract specific topic tags from an Instagram reel caption. "
        "Return 5 to 10 short, lowercase tags naming concrete things in the "
        "reel: techniques, objects, places, ingredients, equipment, styles. "
        "Prefer specific over generic (e.g. 'kettlebell' not 'fitness'). "
        "Each tag is 1-2 words. Respond with ONLY a JSON array of strings, "
        'e.g. ["kettlebell","deadlift","home-workout"].'
    )
    user = "Caption:\n" + caption
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
