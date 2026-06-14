"""Backfill real captions for reels scraped from DMs.

Reels shared in DMs often arrive with no caption (the placeholder is literally
``Reel by @<handle>``), so the categorizer has nothing useful to work with. The
raw private endpoint ``media/<pk>/info/`` returns the canonical media, including
the real caption text, shortcode, creator handle and a thumbnail URL.

For each row that needs it we fetch that info, update caption/shortcode/url/
thumbnail, and RESET categories to NULL so the categorize step re-runs against
the better text. Runs are resumable: failures are caught per-item and printed.
"""

import os
import time
from datetime import datetime

import db as dbm


def _log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

SESSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ig_session.json")


def load_client():
    from instagrapi import Client

    cl = Client()
    cl.load_settings(SESSION_PATH)
    return cl


def needs_enrichment(conn):
    cur = conn.execute(
        "SELECT pk FROM reels WHERE caption IS NULL OR caption LIKE 'Reel by @%'"
    )
    for row in cur:
        yield row["pk"]


def fetch_caption(cl, pk):
    try:
        res = cl.private_request(f"media/{pk}/info/")
        items = res.get("items") or []
        if not items:
            return None
        item = items[0] or {}

        caption_obj = item.get("caption") or {}
        caption = caption_obj.get("text") if isinstance(caption_obj, dict) else None

        code = item.get("code")

        user = item.get("user") or {}
        username = user.get("username") if isinstance(user, dict) else None

        thumbnail_url = None
        iv2 = item.get("image_versions2") or {}
        candidates = iv2.get("candidates") or [] if isinstance(iv2, dict) else []
        if candidates:
            thumbnail_url = (candidates[0] or {}).get("url")

        return {
            "caption": caption,
            "code": code,
            "username": username,
            "thumbnail_url": thumbnail_url,
        }
    except Exception:
        return None


def run(db_path="reels.db", delay=2.0, limit=None):
    conn = dbm.connect(db_path)
    dbm.init_db(conn)
    cl = load_client()

    pks = list(needs_enrichment(conn))
    total = len(pks)
    _log(f"enrich: {total} reel(s) need captions")
    enriched = 0
    processed = 0
    start = time.time()
    for pk in pks:
        if limit is not None and enriched >= limit:
            break
        try:
            info = fetch_caption(cl, pk)
            caption = info.get("caption") if info else None
            if caption:
                fields = {"caption": caption, "categories": None}
                code = info.get("code")
                if code:
                    fields["shortcode"] = code
                    fields["url"] = f"https://www.instagram.com/reel/{code}/"
                if info.get("thumbnail_url"):
                    fields["thumbnail_url"] = info["thumbnail_url"]
                dbm.update_reel(conn, pk, fields)
                enriched += 1
        except Exception as exc:
            print(f"  skip {pk}: {exc}", flush=True)
        processed += 1
        if processed % 25 == 0 or processed == total:
            rate = processed / max(time.time() - start, 1e-9)
            eta = (total - processed) / rate if rate else 0
            _log(f"enrich: {processed}/{total} processed | {enriched} captioned | "
                 f"{rate*60:.0f}/min | eta {eta/60:.0f}m")
        time.sleep(delay)

    _log(f"enrich: done, {enriched} reel(s) captioned out of {total}")


if __name__ == "__main__":
    run(os.environ.get("REELS_DB", "reels.db"))
