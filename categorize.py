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

import llm
from llm import OLLAMA_HOST, OLLAMA_MODEL, _ollama_chat, signal_text

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


def process(item, ctx):
    """Local LLM stage: categories from caption + transcript combined.

    Thin wrapper over the hand-tuned categorize_caption (prompts unchanged).
    The driver's `write` persists via db.set_categories.
    """
    return categorize_caption(llm.signal_text(item))


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
